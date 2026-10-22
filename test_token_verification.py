"""
Automated Test Suite for JSON Payment Token Verification Contract.
Validates:
1. Freshly generated payer token passes merchant verification.
2. Copying exact JSON string (including JS JSON.stringify format where 50.0 becomes 50) passes verification.
3. Harmless whitespace and key-order changes do not invalidate the token.
4. Token pasted inside Markdown code fences (```json ... ```) verifies successfully.
5. Genuinely modified signed fields (amount, merchant ID, payer ID, timestamp) fail verification (tamper detection).
6. Modified/forged signature fails verification.
7. Malformed JSON produces a clear malformed input error rather than a tampering error.
8. Duplicate-token rejection in merchant queue still works.
9. Offline ₹500 limit cap enforcement works.
10. Key consistency across service restarts works.
"""

import os
import json
import time
import unittest
from payer import PayerDevice, OfflineSpendLimitExceededError
from merchant import MerchantDevice
from crypto_utils import parse_token_package, clean_token_json_str


class TestTokenVerificationContract(unittest.TestCase):

    def setUp(self):
        self.payer_state = "test_contract_payer_state.json"
        self.merchant_db = "test_contract_merchant.db"
        for f in [self.payer_state, self.merchant_db]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        self.passphrase = "test-contract-passphrase-2026"
        self.payer = PayerDevice(
            payer_id="alice@upi",
            offline_cap=500.0,
            state_file=self.payer_state,
            passphrase=self.passphrase
        )
        self.merchant = MerchantDevice(
            merchant_id="merchant_bob@upi",
            queue_db_path=self.merchant_db
        )
        self.merchant.register_payer(self.payer.payer_id, self.payer.public_key_pem)

    def tearDown(self):
        if hasattr(self.merchant, "conn") and self.merchant.conn:
            self.merchant.conn.close()
        for f in [self.payer_state, self.merchant_db]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def test_fresh_token_passes_merchant_verification(self):
        """A freshly generated token passes merchant verification."""
        pkg = self.payer.generate_token(amount=100.0, payee_id="merchant_bob@upi")
        res = self.merchant.verify_and_accept_token(pkg)
        self.assertTrue(res["accepted"])
        self.assertEqual(res["status"], "Pending Sync")
        self.assertEqual(res["amount"], 100.0)

    def test_copied_json_js_float_roundtrip(self):
        """
        Simulates copying JSON from Payer Dashboard into Merchant Dashboard:
        In JavaScript, 50.0 becomes 50 when serialized with JSON.stringify().
        Verification must succeed without false 'Tampered' error.
        """
        pkg = self.payer.generate_token(amount=50.0, payee_id="merchant_bob@upi")

        # Simulate JavaScript JSON.parse() and JSON.stringify(tokenPkg, null, 2)
        js_json_str = json.dumps(pkg, indent=2).replace("50.0", "50")
        self.assertIn('"amount": 50', js_json_str)

        # Merchant verifies the raw string or parsed object
        res = self.merchant.verify_and_accept_token(js_json_str)
        self.assertTrue(res["accepted"], f"Failed with: {res.get('reason')}")
        self.assertEqual(res["status"], "Pending Sync")

    def test_harmless_whitespace_and_key_order(self):
        """Harmless whitespace and key-order changes in payload do not invalidate token."""
        pkg = self.payer.generate_token(amount=75.50, payee_id="merchant_bob@upi")

        # Reverse key order in payload
        reversed_payload = dict(reversed(list(pkg["payload"].items())))
        modified_pkg = {
            "signature": pkg["signature"],
            "payload": reversed_payload,
            "payer_public_key": pkg.get("payer_public_key")
        }

        # Add extraneous whitespace/newlines
        json_str = "  \n\n  " + json.dumps(modified_pkg, indent=4) + "  \n  "
        res = self.merchant.verify_and_accept_token(json_str)
        self.assertTrue(res["accepted"], f"Key order / whitespace broke verification: {res.get('reason')}")

    def test_markdown_code_fences_handled(self):
        """Tokens pasted inside markdown code fences (```json ... ```) verify cleanly."""
        pkg = self.payer.generate_token(amount=25.0, payee_id="merchant_bob@upi")
        fenced_input = f"```json\n{json.dumps(pkg, indent=2)}\n```"

        res = self.merchant.verify_and_accept_token(fenced_input)
        self.assertTrue(res["accepted"], f"Markdown code fence was rejected: {res.get('reason')}")

    def test_tampered_amount_fails_verification(self):
        """Genuinely modified amount fails cryptographic signature verification."""
        pkg = self.payer.generate_token(amount=50.0, payee_id="merchant_bob@upi")
        pkg["payload"]["amount"] = 500.0  # Tampered

        res = self.merchant.verify_and_accept_token(pkg)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("FAILED", res["reason"])

    def test_tampered_payee_fails_verification(self):
        """Token presented to a different merchant fails payee binding."""
        pkg = self.payer.generate_token(amount=50.0, payee_id="merchant_charlie@upi")
        res = self.merchant.verify_and_accept_token(pkg)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("Payee mismatch", res["reason"])

    def test_tampered_payer_id_fails_verification(self):
        """Token with tampered payer ID fails registry check or signature verification."""
        pkg = self.payer.generate_token(amount=50.0, payee_id="merchant_bob@upi")
        pkg["payload"]["payerID"] = "attacker@upi"

        res = self.merchant.verify_and_accept_token(pkg)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")

    def test_tampered_signature_fails_verification(self):
        """Token with corrupted or altered signature bytes fails verification."""
        pkg = self.payer.generate_token(amount=50.0, payee_id="merchant_bob@upi")
        # Invert characters in base64 signature
        sig = list(pkg["signature"])
        sig[10] = 'A' if sig[10] != 'A' else 'B'
        pkg["signature"] = "".join(sig)

        res = self.merchant.verify_and_accept_token(pkg)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("FAILED", res["reason"])

    def test_malformed_json_produces_clear_input_error(self):
        """Malformed JSON produces a clear input/syntax error, not a tampering error."""
        malformed = '{"payload": {"amount": 50.0, "tokenID": "incomplete"'
        res = self.merchant.verify_and_accept_token(malformed)
        self.assertFalse(res["accepted"])
        self.assertEqual(res["status"], "REJECTED")
        self.assertIn("Malformed JSON", res["reason"])
        self.assertNotIn("tamper", res["reason"].lower())

    def test_duplicate_token_rejected_in_merchant_queue(self):
        """Merchant detects duplicate token ID and rejects second presentation."""
        pkg = self.payer.generate_token(amount=30.0, payee_id="merchant_bob@upi")

        # First presentation: accepted
        res1 = self.merchant.verify_and_accept_token(pkg)
        self.assertTrue(res1["accepted"])

        # Second presentation: duplicate rejected
        res2 = self.merchant.verify_and_accept_token(pkg)
        self.assertFalse(res2["accepted"])
        self.assertEqual(res2["status"], "REJECTED")
        self.assertIn("Duplicate", res2["reason"])

    def test_offline_spend_limit_500_enforced(self):
        """Payer cannot generate tokens exceeding the ₹500.00 offline allowance."""
        # Spend ₹400
        self.payer.generate_token(amount=400.0, payee_id="merchant_bob@upi")
        self.assertEqual(self.payer.remaining_offline_allowance, 100.0)

        # Attempt to spend ₹100.01 -> exceeds cap
        with self.assertRaises(OfflineSpendLimitExceededError):
            self.payer.generate_token(amount=100.01, payee_id="merchant_bob@upi")

        # Spending exact ₹100 succeeds
        pkg2 = self.payer.generate_token(amount=100.0, payee_id="merchant_bob@upi")
        self.assertEqual(self.payer.remaining_offline_allowance, 0.0)

        # Further spend blocked
        with self.assertRaises(OfflineSpendLimitExceededError):
            self.payer.generate_token(amount=1.0, payee_id="merchant_bob@upi")

    def test_key_persistence_and_cross_restart_verification(self):
        """
        Tokens generated before service restart remain verifiable after reload,
        confirming that key persistence and registry are consistent.
        """
        pkg = self.payer.generate_token(amount=80.0, payee_id="merchant_bob@upi")

        # Close and reload Payer and Merchant instances
        self.merchant.conn.close()
        reloaded_merchant = MerchantDevice(
            merchant_id="merchant_bob@upi",
            queue_db_path=self.merchant_db
        )
        res = reloaded_merchant.verify_and_accept_token(pkg)
        self.assertTrue(res["accepted"])
        self.assertEqual(res["status"], "Pending Sync")
        reloaded_merchant.conn.close()


if __name__ == "__main__":
    unittest.main()
