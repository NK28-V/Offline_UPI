"""
Comprehensive Unit & Security Test Suite for Offline UPI Payment System
Validates all Functional Requirements (FRs), Non-Functional Requirements (NFRs),
Security Guarantees, Edge Cases, Accounting Rules, and Crash Recovery.
"""

import unittest
import time
import os
import math
from payer import PayerDevice, OfflineSpendLimitExceededError
from merchant import MerchantDevice
from backend import BackendServer
from crypto_utils import (
    sign_payload,
    verify_signature,
    load_public_key_from_pem,
    generate_rsa_key_pair,
    public_key_to_pem
)


class TestOfflineUPISystem(unittest.TestCase):
    def setUp(self):
        self.test_db = "test_upi_backend.db"
        self.merchant_db = "test_merchant_queue.db"
        self.payer_state = "test_payer_state.json"

        # Purge any old test files
        for f in [self.test_db, self.merchant_db, self.payer_state]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        self.backend = BackendServer(db_path=self.test_db)
        self.payer = PayerDevice(payer_id="alice@upi", offline_cap=500.0, state_file=self.payer_state)
        self.merchant = MerchantDevice(merchant_id="merchant_bob@upi", queue_db_path=self.merchant_db)

        # Pre-provision payer in trusted registries
        self.merchant.register_payer(self.payer.payer_id, self.payer.public_key_pem)
        self.backend.register_payer(self.payer.payer_id, self.payer.public_key_pem)

    def tearDown(self):
        self.merchant.delete_db()
        self.payer.delete_state()
        self.backend.delete_db()

        for f in [self.test_db, self.merchant_db, self.payer_state]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    # =========================================================================
    # 1. Functional Requirements (FR-1 through FR-8) & Core Mechanics
    # =========================================================================

    def test_fr1_signed_token_generation_and_payee_binding(self):
        """FR-1 & Payee Binding: Generates signed token with required payee binding."""
        token = self.payer.generate_token(100.0, payee_id="merchant_bob@upi")
        payload = token["payload"]

        self.assertIn("tokenID", payload)
        self.assertEqual(payload["payerID"], "alice@upi")
        self.assertEqual(payload["payeeID"], "merchant_bob@upi")
        self.assertEqual(payload["amount"], 100.0)
        self.assertIsInstance(payload["timestamp"], float)
        self.assertIn("signature", token)

        # Verify signature against trusted public key
        pub_key = load_public_key_from_pem(self.payer.public_key_pem)
        self.assertTrue(verify_signature(payload, token["signature"], pub_key))

    def test_fr4_and_nfr1_subsecond_merchant_verification(self):
        """FR-4 & NFR-1: Merchant verifies signature locally in <= 1.0 second."""
        token = self.payer.generate_token(50.0, payee_id="merchant_bob@upi")
        res = self.merchant.verify_and_accept_token(token)

        self.assertTrue(res["accepted"])
        self.assertEqual(res["status"], "Pending Sync")
        self.assertTrue(res["within_nfr_latency"])
        self.assertLess(res["verification_time_ms"], 1000.0)

    def test_fr6_backend_sync_and_settlement(self):
        """FR-6: Queued tokens are successfully synced and settled at backend."""
        token = self.payer.generate_token(120.0, payee_id="merchant_bob@upi")
        self.merchant.verify_and_accept_token(token)
        pending = self.merchant.get_pending_sync_tokens()

        report = self.backend.sync_batch(self.merchant.merchant_id, pending)
        self.assertEqual(report["settled_count"], 1)
        self.assertEqual(report["duplicate_count"], 0)

        row = self.backend.get_token_record(token["payload"]["tokenID"])
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "SETTLED")
        self.assertEqual(row["amount"], 120.0)
        self.assertEqual(row["payee_id"], "merchant_bob@upi")

    def test_fr8_stale_token_flagged_for_review(self):
        """FR-8 & NFR-4: Tokens older than 24h are flagged for manual review."""
        stale_ts = time.time() - (25 * 3600)  # 25 hours old
        payload = {
            "tokenID": "stale-token-test-id",
            "payerID": self.payer.payer_id,
            "payeeID": self.merchant.merchant_id,
            "amount": 80.0,
            "timestamp": stale_ts
        }
        sig = sign_payload(payload, self.payer._private_key)
        stale_token = {
            "payload": payload,
            "signature": sig,
            "payer_public_key": self.payer.public_key_pem
        }

        report = self.backend.sync_batch(self.merchant.merchant_id, [stale_token])
        self.assertEqual(report["settled_count"], 0)
        self.assertEqual(report["flagged_count"], 1)
        self.assertEqual(report["flagged_for_review"][0]["status"], "FLAGGED_FOR_REVIEW")

        record = self.backend.get_token_record("stale-token-test-id")
        self.assertEqual(record["status"], "FLAGGED_FOR_REVIEW")
        self.assertIn("exceeds 24h threshold", record["review_reason"])

    # =========================================================================
    # 2. Trusted Payer Registry & Cryptographic Security Tests
    # =========================================================================

    def test_security_unknown_payer_rejected_by_merchant(self):
        """Security: Merchant rejects token from payer ID not in trusted registry."""
        att_priv, att_pub = generate_rsa_key_pair()
        payload = {
            "tokenID": "unknown-payer-token",
            "payerID": "charlie_stranger@upi",
            "payeeID": self.merchant.merchant_id,
            "amount": 50.0,
            "timestamp": time.time()
        }
        sig = sign_payload(payload, att_priv)
        token = {
            "payload": payload,
            "signature": sig,
            "payer_public_key": public_key_to_pem(att_pub)
        }

        res = self.merchant.verify_and_accept_token(token)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("Unknown payer identity", res["reason"])

    def test_security_unknown_payer_rejected_by_backend(self):
        """Security: Backend rejects token from payer ID not registered in central backend."""
        att_priv, att_pub = generate_rsa_key_pair()
        payload = {
            "tokenID": "unknown-payer-backend-token",
            "payerID": "charlie_stranger@upi",
            "payeeID": self.merchant.merchant_id,
            "amount": 50.0,
            "timestamp": time.time()
        }
        sig = sign_payload(payload, att_priv)
        token = {
            "payload": payload,
            "signature": sig,
            "payer_public_key": public_key_to_pem(att_pub)
        }

        report = self.backend.sync_batch(self.merchant.merchant_id, [token])
        self.assertEqual(report["settled_count"], 0)
        self.assertEqual(report["rejected_count"], 1)
        self.assertIn("Unknown payer identity", report["rejected_invalid"][0]["reason"])

    def test_security_forged_payer_identity_with_attacker_key(self):
        """Security: Attacker claims to be alice@upi using their own keypair -> rejected."""
        att_priv, att_pub = generate_rsa_key_pair()
        payload = {
            "tokenID": "forged-alice-token",
            "payerID": "alice@upi",  # Legit ID
            "payeeID": self.merchant.merchant_id,
            "amount": 100.0,
            "timestamp": time.time()
        }
        # Signed with attacker's private key, while accompanying attacker's public key
        sig = sign_payload(payload, att_priv)
        token = {
            "payload": payload,
            "signature": sig,
            "payer_public_key": public_key_to_pem(att_pub)
        }

        # Merchant uses pre-registered key for alice@upi, so attacker's signature fails
        res = self.merchant.verify_and_accept_token(token)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("signature verification FAILED", res["reason"])

    def test_security_altered_payload_or_signature(self):
        """Security: Any alteration to payload or signature invalidates verification."""
        token = self.payer.generate_token(40.0, payee_id="merchant_bob@upi")

        # Case A: Amount altered
        tampered_amt = {
            "payload": dict(token["payload"]),
            "signature": token["signature"],
            "payer_public_key": token["payer_public_key"]
        }
        tampered_amt["payload"]["amount"] = 400.0
        res_a = self.merchant.verify_and_accept_token(tampered_amt)
        self.assertFalse(res_a["accepted"])

        # Case B: Signature altered
        tampered_sig = {
            "payload": dict(token["payload"]),
            "signature": "corrupted_signature_base64==",
            "payer_public_key": token["payer_public_key"]
        }
        res_b = self.merchant.verify_and_accept_token(tampered_sig)
        self.assertFalse(res_b["accepted"])

    # =========================================================================
    # 3. Payee Binding Tests
    # =========================================================================

    def test_payee_mismatch_rejected_at_merchant(self):
        """Payee Binding: Merchant rejects token bound for another merchant."""
        token = self.payer.generate_token(25.0, payee_id="other_merchant@upi")
        res = self.merchant.verify_and_accept_token(token)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("Payee mismatch", res["reason"])

    def test_payee_mismatch_rejected_at_backend(self):
        """Payee Binding: Backend rejects token submitted by non-intended merchant."""
        token = self.payer.generate_token(30.0, payee_id="merchant_bob@upi")

        # Submitting through "rogue_merchant@upi"
        report = self.backend.sync_batch("rogue_merchant@upi", [token])
        self.assertEqual(report["settled_count"], 0)
        self.assertEqual(report["rejected_count"], 1)
        self.assertIn("Payee mismatch", report["rejected_invalid"][0]["reason"])

    # =========================================================================
    # 4. Strict Input Validation Tests
    # =========================================================================

    def test_input_validation_negative_zero_nan_infinity_amounts(self):
        """Input Validation: Rejects non-positive, NaN, infinity, and non-numeric amounts."""
        # 1. Payer device validates on generation
        with self.assertRaises(ValueError):
            self.payer.generate_token(-10.0, payee_id="merchant_bob@upi")
        with self.assertRaises(ValueError):
            self.payer.generate_token(0.0, payee_id="merchant_bob@upi")
        with self.assertRaises(ValueError):
            self.payer.generate_token(float("nan"), payee_id="merchant_bob@upi")
        with self.assertRaises(ValueError):
            self.payer.generate_token(float("inf"), payee_id="merchant_bob@upi")
        with self.assertRaises(ValueError):
            self.payer.generate_token(10.0, payee_id="")  # Missing payee

        # 2. Merchant device rejects structured tokens with bad amounts
        for bad_amt in [-5.0, 0.0, float("nan"), float("inf"), "fifty"]:
            bad_pkg = {
                "payload": {
                    "tokenID": "bad-amt-id",
                    "payerID": self.payer.payer_id,
                    "payeeID": self.merchant.merchant_id,
                    "amount": bad_amt,
                    "timestamp": time.time()
                },
                "signature": "dummy_sig"
            }
            res = self.merchant.verify_and_accept_token(bad_pkg)
            self.assertFalse(res["accepted"])
            self.assertEqual(res["status"], "REJECTED")

    def test_input_validation_future_timestamp(self):
        """Input Validation: Tokens with future timestamps beyond clock skew tolerance are rejected."""
        future_ts = time.time() + 300.0  # 5 minutes in future
        future_payload = {
            "tokenID": "future-ts-id",
            "payerID": self.payer.payer_id,
            "payeeID": self.merchant.merchant_id,
            "amount": 20.0,
            "timestamp": future_ts
        }
        sig = sign_payload(future_payload, self.payer._private_key)
        token = {"payload": future_payload, "signature": sig}

        # Merchant rejects
        res = self.merchant.verify_and_accept_token(token)
        self.assertFalse(res["accepted"])
        self.assertIn("Future timestamp detected", res["reason"])

        # Backend also rejects
        report = self.backend.sync_batch(self.merchant.merchant_id, [token])
        self.assertEqual(report["settled_count"], 0)
        self.assertIn("Future timestamp detected", report["rejected_invalid"][0]["reason"])

    def test_input_validation_malformed_structures(self):
        """Input Validation: Rejects malformed or non-dictionary token packages gracefully."""
        self.assertFalse(self.merchant.verify_and_accept_token(None)["accepted"])
        self.assertFalse(self.merchant.verify_and_accept_token("not a dict")["accepted"])
        self.assertFalse(self.merchant.verify_and_accept_token({})["accepted"])

    # =========================================================================
    # 5. Offline Spending Accounting & Exact Balance Limits
    # =========================================================================

    def test_offline_spending_exact_500_allowed_and_500_01_blocked(self):
        """Accounting: Exactly ₹500.00 is allowed; ₹500.01 is blocked."""
        # Spend ₹300.00
        self.payer.generate_token(300.0, payee_id="merchant_bob@upi")
        self.assertEqual(self.payer.unsynced_spent, 300.0)
        self.assertEqual(self.payer.remaining_offline_allowance, 200.0)

        # Spend ₹200.00 -> reaches exactly ₹500.00
        self.payer.generate_token(200.0, payee_id="merchant_bob@upi")
        self.assertEqual(self.payer.unsynced_spent, 500.0)
        self.assertEqual(self.payer.remaining_offline_allowance, 0.0)

        # Attempt to spend ₹0.01 -> Exceeds cap (₹500.01)
        with self.assertRaises(OfflineSpendLimitExceededError):
            self.payer.generate_token(0.01, payee_id="merchant_bob@upi")

    def test_failed_transfer_cancellation(self):
        """Accounting: Payer can cancel a failed transfer and restore unsynced allowance."""
        token = self.payer.generate_token(150.0, payee_id="merchant_bob@upi")
        token_id = token["payload"]["tokenID"]
        self.assertEqual(self.payer.unsynced_spent, 150.0)

        # Simulating failed transfer handoff
        cancelled = self.payer.cancel_failed_token(token_id)
        self.assertTrue(cancelled)
        self.assertEqual(self.payer.unsynced_spent, 0.0)
        self.assertEqual(self.payer.remaining_offline_allowance, 500.0)

    def test_repeated_reconciliation_protection(self):
        """Accounting: Repeated reconciliation of already settled tokens does not double-credit."""
        token = self.payer.generate_token(100.0, payee_id="merchant_bob@upi")
        tid = token["payload"]["tokenID"]

        # First reconciliation restores ₹100.00
        restored_1 = self.payer.reconcile_settled_tokens([tid])
        self.assertEqual(restored_1, 100.0)
        self.assertEqual(self.payer.unsynced_spent, 0.0)

        # Duplicate reconciliation attempt should restore ₹0.00
        restored_2 = self.payer.reconcile_settled_tokens([tid])
        self.assertEqual(restored_2, 0.0)
        self.assertEqual(self.payer.unsynced_spent, 0.0)

    def test_payer_state_persistence_across_restart(self):
        """Persistence: Payer unsynced spend counter and keys persist across device restarts."""
        self.payer.generate_token(250.0, payee_id="merchant_bob@upi")
        orig_key = self.payer.public_key_pem

        # Simulate device restart by creating a new instance with same state file
        rebooted_payer = PayerDevice(payer_id="alice@upi", state_file=self.payer_state)
        self.assertEqual(rebooted_payer.unsynced_spent, 250.0)
        self.assertEqual(rebooted_payer.remaining_offline_allowance, 250.0)
        self.assertEqual(rebooted_payer.public_key_pem, orig_key)

    # =========================================================================
    # 6. Merchant Queue Recovery & State Tests
    # =========================================================================

    def test_merchant_queue_persistence_across_restart(self):
        """Queue Recovery: Merchant queue persists pending tokens across device reboot."""
        t1 = self.payer.generate_token(100.0, payee_id="merchant_bob@upi")
        t2 = self.payer.generate_token(120.0, payee_id="merchant_bob@upi")

        self.merchant.verify_and_accept_token(t1)
        self.merchant.verify_and_accept_token(t2)
        self.assertEqual(len(self.merchant.get_pending_sync_tokens()), 2)

        # Simulate POS terminal crash / restart
        self.merchant.close()
        rebooted_merchant = MerchantDevice(merchant_id="merchant_bob@upi", queue_db_path=self.merchant_db)

        recovered_pending = rebooted_merchant.get_pending_sync_tokens()
        self.assertEqual(len(recovered_pending), 2)
        self.assertEqual(rebooted_merchant.summary()["pending_sync"], 2)
        self.assertEqual(rebooted_merchant.summary()["pending_sync_amount"], 220.0)
        rebooted_merchant.close()

    def test_merchant_only_marks_confirmed_settled(self):
        """Integrity: Merchant marks only tokens explicitly settled by backend."""
        t1 = self.payer.generate_token(60.0, payee_id="merchant_bob@upi")
        t2 = self.payer.generate_token(70.0, payee_id="merchant_bob@upi")
        self.merchant.verify_and_accept_token(t1)
        self.merchant.verify_and_accept_token(t2)

        # Only confirm settlement for t1
        self.merchant.mark_tokens_synced([t1["payload"]["tokenID"]])
        summary = self.merchant.summary()
        self.assertEqual(summary["settled"], 1)
        self.assertEqual(summary["pending_sync"], 1)

    # =========================================================================
    # 7. Atomic Settlement & Double-Spend Prevention Tests
    # =========================================================================

    def test_duplicate_in_same_batch_rejected(self):
        """Atomic Settlement: Duplicate token IDs within the same batch are detected and rejected."""
        t1 = self.payer.generate_token(50.0, payee_id="merchant_bob@upi")
        # Duplicate the exact same token in the batch
        batch = [t1, t1]

        report = self.backend.sync_batch(self.merchant.merchant_id, batch)
        self.assertEqual(report["settled_count"], 1)
        self.assertEqual(report["duplicate_count"], 1)
        self.assertIn("Double-spend detected", report["duplicates"][0]["reason"])

    def test_duplicate_across_multiple_syncs_rejected(self):
        """Atomic Settlement: Re-submitting an already settled token is rejected with 100% precision."""
        t1 = self.payer.generate_token(90.0, payee_id="merchant_bob@upi")

        # First sync succeeds
        report1 = self.backend.sync_batch(self.merchant.merchant_id, [t1])
        self.assertEqual(report1["settled_count"], 1)

        # Second sync rejected
        report2 = self.backend.sync_batch(self.merchant.merchant_id, [t1])
        self.assertEqual(report2["settled_count"], 0)
        self.assertEqual(report2["duplicate_count"], 1)
        self.assertIn("IntegrityError", report2["duplicates"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
