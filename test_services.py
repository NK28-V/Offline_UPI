"""
Unit and Component Tests for Independent Services:
- Device 1: Payer Service (payer_service.py)
- Device 2: Merchant Service (merchant_service.py)
- Device 3: Bank Service (bank_service.py)
Validates APIs, private-key isolation, integer paise limit boundary (₹500.00 vs ₹500.01),
admin authentication, offline local verification, persistence, and batch settlement.
"""

import unittest
import os
import time
import json
import sqlite3
from typing import Dict, Any

from payer_service import create_payer_app
from merchant_service import create_merchant_app
from bank_service import create_bank_app
from crypto_utils import generate_rsa_key_pair, public_key_to_pem, sign_payload


class TestServices(unittest.TestCase):
    def setUp(self):
        self.payer_state = "test_svc_payer_state.json"
        self.merchant_db = "test_svc_merchant_queue.db"
        self.bank_db = "test_svc_bank.db"
        self.admin_token = "test-secret-token-123"

        # Cleanup
        for f in [self.payer_state, self.merchant_db, self.bank_db]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        self.bank_app = create_bank_app(db_path=self.bank_db, admin_token=self.admin_token)
        self.bank_client = self.bank_app.test_client()

        self.payer_app = create_payer_app(
            payer_id="alice@upi",
            offline_cap=500.0,
            state_file=self.payer_state
        )
        self.payer_client = self.payer_app.test_client()

        self.merchant_app = create_merchant_app(
            merchant_id="merchant_bob@upi",
            queue_db_path=self.merchant_db,
            bank_url="http://localhost:5003"
        )
        self.merchant_client = self.merchant_app.test_client()

        self.merchant_key = "merchant-bob-secret-key-2026"

        # Provision Alice in Merchant and Bank
        pub_res = self.payer_client.get("/api/payer/public_key")
        self.payer_pub_pem = pub_res.get_json()["public_key_pem"]

        self.merchant_client.post("/api/merchant/provision_payer", json={
            "payer_id": "alice@upi",
            "public_key_pem": self.payer_pub_pem
        })

        self.bank_client.post(
            "/api/bank/register_payer",
            headers={"X-Admin-Token": self.admin_token},
            json={
                "payer_id": "alice@upi",
                "public_key_pem": self.payer_pub_pem
            }
        )

    def tearDown(self):
        # Close DB connections
        try:
            self.merchant_app.config["MERCHANT_STATE"]["merchant"].close()
        except Exception:
            pass
        try:
            self.bank_app.config["BANK_STATE"]["backend"].close()
        except Exception:
            pass

        for f in [self.payer_state, self.merchant_db, self.bank_db]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    # =========================================================================
    # PAYER SERVICE TESTS
    # =========================================================================

    def test_payer_local_token_signing_without_bank(self):
        """Payer generates and cryptographically signs token without needing bank contact."""
        res = self.payer_client.post("/api/payer/generate", json={
            "amount": 100.0,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        pkg = data["token_package"]
        self.assertIn("signature", pkg)
        self.assertEqual(pkg["payload"]["amount"], 100.0)
        self.assertEqual(pkg["payload"]["payeeID"], "merchant_bob@upi")

    def test_payer_exact_500_cap_and_500_01_rejection(self):
        """Payer permits exactly ₹500.00 cumulative spend and rejects ₹500.01."""
        # Spend ₹300.00
        r1 = self.payer_client.post("/api/payer/generate", json={
            "amount": 300.0,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(r1.status_code, 200)

        # Spend ₹200.00 -> reaches exactly ₹500.00
        r2 = self.payer_client.post("/api/payer/generate", json={
            "amount": 200.0,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["unsynced_spent"], 500.0)
        self.assertEqual(r2.get_json()["remaining_allowance"], 0.0)

        # Attempt ₹0.01 -> rejected
        r3 = self.payer_client.post("/api/payer/generate", json={
            "amount": 0.01,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(r3.status_code, 400)
        self.assertIn("Offline limit exceeded", r3.get_json()["error"])

    def test_payer_invalid_amount_rejection(self):
        """Payer rejects zero, negative, and non-numeric amounts."""
        for bad_val in [0.0, -10.0, "not_a_number"]:
            r = self.payer_client.post("/api/payer/generate", json={
                "amount": bad_val,
                "payee_id": "merchant_bob@upi"
            })
            self.assertEqual(r.status_code, 400)

    def test_payer_private_key_isolation(self):
        """Payer status and public APIs never leak the private key."""
        r_status = self.payer_client.get("/api/payer/status")
        self.assertNotIn("private", r_status.get_data(as_text=True).lower())

        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 50.0,
            "payee_id": "merchant_bob@upi"
        })
        pkg = r_gen.get_json()["token_package"]
        self.assertNotIn("private", json.dumps(pkg).lower())

    def test_payer_persistence_after_restart(self):
        """Payer balance and outstanding tokens persist across service restart."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 150.0,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(r_gen.status_code, 200)

        # Create new app instance loading the same state file
        new_app = create_payer_app(
            payer_id="alice@upi",
            state_file=self.payer_state
        )
        new_client = new_app.test_client()
        r_status = new_client.get("/api/payer/status")
        data = r_status.get_json()
        self.assertEqual(data["unsynced_spent"], 150.0)
        self.assertEqual(data["remaining_allowance"], 350.0)
        self.assertEqual(data["outstanding_count"], 1)

    # =========================================================================
    # MERCHANT SERVICE TESTS
    # =========================================================================

    def test_merchant_local_verification_and_acceptance(self):
        """Merchant verifies and queues valid token locally without contacting bank."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 75.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]

        r_rec = self.merchant_client.post("/api/merchant/receive", json={
            "token_package": token_pkg
        })
        self.assertEqual(r_rec.status_code, 200)
        data = r_rec.get_json()
        self.assertTrue(data["accepted"])
        self.assertEqual(data["status"], "Pending Sync")

        # Verify queue
        r_status = self.merchant_client.get("/api/merchant/status")
        self.assertEqual(r_status.get_json()["pending_sync"], 1)
        self.assertEqual(r_status.get_json()["pending_sync_amount"], 75.0)

    def test_merchant_rejects_tampered_token(self):
        """Merchant rejects token with tampered amount."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 40.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        token_pkg["payload"]["amount"] = 400.0  # Tampered

        r_rec = self.merchant_client.post("/api/merchant/receive", json={
            "token_package": token_pkg
        })
        data = r_rec.get_json()
        self.assertFalse(data["accepted"])
        self.assertEqual(data["status"], "REJECTED")

    def test_merchant_rejects_unknown_payer(self):
        """Merchant rejects token signed by an un-registered payer."""
        att_priv, att_pub = generate_rsa_key_pair()
        payload = {
            "tokenID": "unknown-token-1",
            "payerID": "unregistered@upi",
            "payeeID": "merchant_bob@upi",
            "amount": 25.0,
            "timestamp": time.time()
        }
        sig = sign_payload(payload, att_priv)
        pkg = {
            "payload": payload,
            "signature": sig,
            "payer_public_key": public_key_to_pem(att_pub)
        }
        r_rec = self.merchant_client.post("/api/merchant/receive", json={"token_package": pkg})
        data = r_rec.get_json()
        self.assertFalse(data["accepted"])
        self.assertIn("Unknown payer identity", data["reason"])

    def test_merchant_rejects_wrong_payee(self):
        """Merchant rejects token bound for another merchant."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 50.0,
            "payee_id": "merchant_other@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]

        r_rec = self.merchant_client.post("/api/merchant/receive", json={"token_package": token_pkg})
        data = r_rec.get_json()
        self.assertFalse(data["accepted"])
        self.assertIn("Payee mismatch", data["reason"])

    def test_merchant_queue_persistence_across_restart(self):
        """Merchant queue persists in SQLite across service restart."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 60.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        self.merchant_client.post("/api/merchant/receive", json={"token_package": token_pkg})

        # Close current app and create new one with same DB
        self.merchant_app.config["MERCHANT_STATE"]["merchant"].close()
        new_m_app = create_merchant_app(
            merchant_id="merchant_bob@upi",
            queue_db_path=self.merchant_db
        )
        new_m_client = new_m_app.test_client()
        r_status = new_m_client.get("/api/merchant/status")
        self.assertEqual(r_status.get_json()["pending_sync"], 1)
        self.assertEqual(r_status.get_json()["pending_sync_amount"], 60.0)
        new_m_app.config["MERCHANT_STATE"]["merchant"].close()

    # =========================================================================
    # BANK SERVICE TESTS
    # =========================================================================

    def test_bank_admin_auth_protection(self):
        """Bank registration endpoint rejects requests without valid admin token."""
        # Missing token
        r_fail = self.bank_client.post("/api/bank/register_payer", json={
            "payer_id": "stranger@upi",
            "public_key_pem": self.payer_pub_pem
        })
        self.assertEqual(r_fail.status_code, 401)

        # Invalid token
        r_bad = self.bank_client.post(
            "/api/bank/register_payer",
            headers={"X-Admin-Token": "wrong-secret"},
            json={
                "payer_id": "stranger@upi",
                "public_key_pem": self.payer_pub_pem
            }
        )
        self.assertEqual(r_bad.status_code, 401)

        # Valid token
        r_ok = self.bank_client.post(
            "/api/bank/register_payer",
            headers={"X-Admin-Token": self.admin_token},
            json={
                "payer_id": "stranger@upi",
                "public_key_pem": self.payer_pub_pem
            }
        )
        self.assertEqual(r_ok.status_code, 200)

    def test_bank_atomic_settlement_and_duplicate_rejection(self):
        """Bank settles valid token atomically and rejects duplicates with IntegrityError."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 90.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]

        # First settlement succeeds
        r_sync1 = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": [token_pkg]
            }
        )
        self.assertEqual(r_sync1.status_code, 200)
        d1 = r_sync1.get_json()
        self.assertEqual(d1["settled_count"], 1)
        self.assertEqual(d1["duplicate_count"], 0)

        # Re-submitting the exact same token triggers duplicate detection
        r_sync2 = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": [token_pkg]
            }
        )
        self.assertEqual(r_sync2.status_code, 200)
        d2 = r_sync2.get_json()
        self.assertEqual(d2["settled_count"], 0)
        self.assertEqual(d2["duplicate_count"], 1)
        self.assertIn("Double-spend detected", d2["duplicates"][0]["reason"])

    def test_bank_in_batch_duplicate_rejection(self):
        """Bank detects duplicates contained within the same batch."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 45.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]

        r_sync = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": [token_pkg, token_pkg]
            }
        )
        d = r_sync.get_json()
        self.assertEqual(d["settled_count"], 1)
        self.assertEqual(d["duplicate_count"], 1)

    def test_bank_stale_token_flagged_for_review(self):
        """Bank flags tokens older than 24 hours for review rather than settling them."""
        stale_ts = time.time() - (30 * 3600)  # 30 hours old
        payload = {
            "tokenID": "stale-service-token-id",
            "payerID": "alice@upi",
            "payeeID": "merchant_bob@upi",
            "amount": 65.0,
            "timestamp": stale_ts
        }
        sig = sign_payload(payload, self.payer_app.config["PAYER_STATE"]["payer"]._private_key)
        pkg = {"payload": payload, "signature": sig}

        r_sync = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": [pkg]
            }
        )
        d = r_sync.get_json()
        self.assertEqual(d["settled_count"], 0)
        self.assertEqual(d["flagged_count"], 1)
        self.assertEqual(d["flagged_for_review"][0]["status"], "FLAGGED_FOR_REVIEW")

    def test_bank_maintenance_mode_toggle(self):
        """When bank network is toggled offline, sync requests return 503."""
        # Unauthenticated toggle rejected
        r_unauth = self.bank_client.post("/api/bank/toggle_network")
        self.assertEqual(r_unauth.status_code, 401)

        # Authenticated toggle
        self.bank_client.post(
            "/api/bank/toggle_network",
            headers={"X-Admin-Token": self.admin_token}
        )

        r_sync = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": []
            }
        )
        self.assertEqual(r_sync.status_code, 503)
        self.assertIn("unavailable", r_sync.get_json()["error"])

        # Toggle back online
        self.bank_client.post(
            "/api/bank/toggle_network",
            headers={"X-Admin-Token": self.admin_token}
        )
        r_sync2 = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": []
            }
        )
        self.assertEqual(r_sync2.status_code, 200)

    # =========================================================================
    # SECURITY HARDENING TESTS
    # =========================================================================

    def test_payer_cannot_cancel_exported_token(self):
        """Payer cannot restore spending allowance for a token that has been exported."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 80.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        token_id = token_pkg["payload"]["tokenID"]

        # Export token (e.g. downloaded QR/JSON or copied)
        r_exp = self.payer_client.post("/api/payer/export", json={"token_id": token_id})
        self.assertEqual(r_exp.status_code, 200)
        self.assertEqual(r_exp.get_json()["status"], "EXPORTED")

        # Cancellation must now be rejected
        r_cancel = self.payer_client.post("/api/payer/cancel", json={"token_id": token_id})
        self.assertEqual(r_cancel.status_code, 400)
        self.assertIn("already been exported", r_cancel.get_json()["error"])

        # Allowance remains spent/reserved
        r_stat = self.payer_client.get("/api/payer/status")
        self.assertEqual(r_stat.get_json()["unsynced_spent"], 80.0)

    def test_payer_can_cancel_unexported_token(self):
        """Payer can safely cancel a token that was created but never exported."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 50.0,
            "payee_id": "merchant_bob@upi"
        })
        token_id = r_gen.get_json()["token_package"]["payload"]["tokenID"]
        self.assertEqual(r_gen.get_json()["unsynced_spent"], 50.0)

        # Cancel before export
        r_cancel = self.payer_client.post("/api/payer/cancel", json={"token_id": token_id})
        self.assertEqual(r_cancel.status_code, 200)
        self.assertEqual(r_cancel.get_json()["remaining_allowance"], 500.0)
        self.assertEqual(r_cancel.get_json()["unsynced_spent"], 0.0)

    def test_payer_reconcile_with_valid_bank_receipt(self):
        """Payer restores allowance only upon verifying a cryptographically signed Bank receipt."""
        # 1. Generate & Export token
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 125.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        token_id = token_pkg["payload"]["tokenID"]
        self.payer_client.post("/api/payer/export", json={"token_id": token_id})

        # 2. Bank settles batch
        r_sync = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={"merchant_id": "merchant_bob@upi", "tokens": [token_pkg]}
        )
        self.assertEqual(r_sync.status_code, 200)
        receipt = r_sync.get_json()["receipts"][0]

        # 3. Payer reconciles with the receipt
        r_rec = self.payer_client.post("/api/payer/reconcile", json={"receipt": receipt})
        self.assertEqual(r_rec.status_code, 200)
        data = r_rec.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["token_id"], token_id)
        self.assertEqual(data["unsynced_spent"], 0.0)
        self.assertEqual(data["remaining_allowance"], 500.0)

    def test_payer_reconcile_rejects_forged_or_mismatched_receipt(self):
        """Payer rejects receipts with forged signatures or tampered amounts."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 70.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        token_id = token_pkg["payload"]["tokenID"]

        # Valid bank sync
        r_sync = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={"merchant_id": "merchant_bob@upi", "tokens": [token_pkg]}
        )
        receipt = r_sync.get_json()["receipts"][0]

        # Case 1: Tampered amount (signature verification will fail because payload changed)
        forged_receipt = json.loads(json.dumps(receipt))
        forged_receipt["receipt"]["amount"] = 700.0
        r1 = self.payer_client.post("/api/payer/reconcile", json={"receipt": forged_receipt})
        self.assertEqual(r1.status_code, 400)
        self.assertIn("signature verification FAILED", r1.get_json()["error"])

        # Case 2: Forged signature
        forged_receipt2 = json.loads(json.dumps(receipt))
        forged_receipt2["signature"] = "MDEyMzQ1Njc4OWFiY2RlZg=="
        r2 = self.payer_client.post("/api/payer/reconcile", json={"receipt": forged_receipt2})
        self.assertEqual(r2.status_code, 400)
        self.assertIn("signature verification FAILED", r2.get_json()["error"])

    def test_payer_encrypted_key_storage_and_passphrase(self):
        """Payer state on disk contains PBKDF2/AES-GCM ciphertext, not plaintext PEM."""
        with open(self.payer_state, "r", encoding="utf-8") as f:
            state_data = json.load(f)

        key_store = state_data["encrypted_private_key"]
        self.assertIn("salt_b64", key_store)
        self.assertIn("nonce_b64", key_store)
        self.assertIn("ciphertext_b64", key_store)
        self.assertEqual(key_store.get("cipher"), "AES-256-GCM")

        # Raw file does not contain PEM header
        with open(self.payer_state, "r", encoding="utf-8") as f:
            raw_text = f.read()
        self.assertNotIn("BEGIN RSA PRIVATE KEY", raw_text)
        self.assertNotIn("BEGIN PRIVATE KEY", raw_text)

        # Reopening with wrong passphrase fails safely
        from payer import PayerDevice
        with self.assertRaises(ValueError):
            PayerDevice(
                payer_id="alice@upi",
                passphrase="wrong-passphrase-attempt",
                state_file=self.payer_state
            )

    def test_bank_sync_rejects_unauthorized_merchant(self):
        """Bank rejects sync batch from merchants lacking valid X-Merchant-Key."""
        r1 = self.bank_client.post("/api/bank/sync_batch", json={
            "merchant_id": "merchant_bob@upi",
            "tokens": []
        })
        self.assertEqual(r1.status_code, 401)

        r2 = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": "completely-invalid-key"},
            json={
                "merchant_id": "merchant_bob@upi",
                "tokens": []
            }
        )
        self.assertEqual(r2.status_code, 401)

    def test_bank_rejects_unauthorized_payer_key_replacement(self):
        """Attempting to replace an existing registered payer key is rejected."""
        _, new_pub = generate_rsa_key_pair()
        new_pem = public_key_to_pem(new_pub)

        # Re-registering with different key without admin replace authorization
        r = self.bank_client.post(
            "/api/bank/register_payer",
            headers={"X-Admin-Token": self.admin_token},
            json={
                "payer_id": "alice@upi",
                "public_key_pem": new_pem
            }
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("already registered", r.get_json()["error"])

    def test_bank_rejects_invalid_or_weak_payer_key(self):
        """Bank rejects non-RSA or weak (<2048 bit) public keys."""
        # Non-RSA string
        r = self.bank_client.post(
            "/api/bank/register_payer",
            headers={"X-Admin-Token": self.admin_token},
            json={
                "payer_id": "weak_user@upi",
                "public_key_pem": "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA0000\n-----END PUBLIC KEY-----"
            }
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid public key", r.get_json()["error"])

    def test_merchant_queue_api(self):
        """Merchant provides GET /api/merchant/queue to inspect local offline queue safely."""
        r = self.merchant_client.get("/api/merchant/queue")
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertIn("pending_tokens", data)
        self.assertIn("count", data)

    def test_bank_transaction_and_receipt_apis(self):
        """Bank provides secure transaction status and receipt retrieval by tokenID."""
        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 35.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        token_id = token_pkg["payload"]["tokenID"]

        # Settle
        self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={"merchant_id": "merchant_bob@upi", "tokens": [token_pkg]}
        )

        # Query transaction
        r_tx = self.bank_client.get(f"/api/bank/transactions/{token_id}")
        self.assertEqual(r_tx.status_code, 200)
        self.assertEqual(r_tx.get_json()["token_id"], token_id)
        self.assertEqual(r_tx.get_json()["status"], "SETTLED")

        # Query receipt
        r_rc = self.bank_client.get(f"/api/bank/receipts/{token_id}")
        self.assertEqual(r_rc.status_code, 200)
        self.assertEqual(r_rc.get_json()["receipt"]["tokenID"], token_id)

    def test_payer_cannot_restore_allowance_with_arbitrary_token_ids(self):
        """Payer cannot restore spending allowance by submitting arbitrary fake token IDs."""
        r_stat_before = self.payer_client.get("/api/payer/status").get_json()
        initial_allowance = r_stat_before["remaining_allowance"]

        # Attempt cancel on arbitrary non-existent token ID
        r_cancel = self.payer_client.post("/api/payer/cancel", json={"token_id": "arbitrary-fake-uuid-9999"})
        self.assertEqual(r_cancel.status_code, 404)

        # Attempt reconcile with arbitrary token ID payload lacking valid signature
        fake_receipt = {
            "receipt": {
                "receiptID": "fake-receipt-id",
                "tokenID": "arbitrary-fake-uuid-9999",
                "payerID": "alice@upi",
                "payeeID": "merchant_bob@upi",
                "amount": 250.0,
                "status": "SETTLED",
                "settled_at": time.time()
            },
            "signature": "fake-invalid-signature"
        }
        r_recon = self.payer_client.post("/api/payer/reconcile", json={"receipt": fake_receipt})
        self.assertEqual(r_recon.status_code, 400)

        # Confirm allowance remained completely unchanged
        r_stat_after = self.payer_client.get("/api/payer/status").get_json()
        self.assertEqual(r_stat_after["remaining_allowance"], initial_allowance)

    def test_concurrent_duplicate_submissions_rejected(self):
        """Concurrent submissions of the same token settle at most once without double-crediting."""
        from concurrent.futures import ThreadPoolExecutor

        r_gen = self.payer_client.post("/api/payer/generate", json={
            "amount": 65.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = r_gen.get_json()["token_package"]
        token_id = token_pkg["payload"]["tokenID"]

        # Define sync task
        def sync_worker():
            return self.bank_client.post(
                "/api/bank/sync_batch",
                headers={"X-Merchant-Key": self.merchant_key},
                json={"merchant_id": "merchant_bob@upi", "tokens": [token_pkg]}
            ).get_json()

        with ThreadPoolExecutor(max_workers=2) as executor:
            fut1 = executor.submit(sync_worker)
            fut2 = executor.submit(sync_worker)
            res1 = fut1.result()
            res2 = fut2.result()

        total_settled = res1.get("settled_count", 0) + res2.get("settled_count", 0)
        total_duplicates = res1.get("duplicate_count", 0) + res2.get("duplicate_count", 0)

        self.assertEqual(total_settled, 1, "Expected exactly 1 settlement across concurrent requests")
        self.assertEqual(total_duplicates, 1, "Expected exactly 1 duplicate rejection across concurrent requests")

    def test_demo_admin_endpoints_reject_unauthorized(self):
        """Simulation administrative endpoints reject calls lacking valid admin credentials."""
        # Toggle network
        r1 = self.bank_client.post("/api/bank/toggle_network")
        self.assertEqual(r1.status_code, 401)
        self.assertIn("error", r1.get_json())

        # Simulate duplicate
        r2 = self.bank_client.post("/api/bank/simulate_duplicate")
        self.assertEqual(r2.status_code, 401)

        # Register payer
        r3 = self.bank_client.post("/api/bank/register_payer", json={"payer_id": "eve@upi"})
        self.assertEqual(r3.status_code, 401)

    def test_invalid_request_bodies_produce_controlled_json(self):
        """Server endpoints produce clean JSON error responses for invalid payloads, not stack traces."""
        # Bad JSON on payer generate
        r_payer = self.payer_client.post(
            "/api/payer/generate",
            data="this is not json",
            content_type="application/json"
        )
        self.assertEqual(r_payer.status_code, 400)
        self.assertTrue(r_payer.is_json)
        self.assertIn("error", r_payer.get_json())

        # Bad body on bank sync
        r_bank = self.bank_client.post(
            "/api/bank/sync_batch",
            headers={"X-Merchant-Key": self.merchant_key},
            json={"merchant_id": ""}
        )
        self.assertEqual(r_bank.status_code, 400)
        self.assertTrue(r_bank.is_json)
        self.assertIn("error", r_bank.get_json())


if __name__ == "__main__":
    unittest.main()
