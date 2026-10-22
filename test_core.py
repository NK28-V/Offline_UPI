"""Offline UPI tests. Run:  python -m unittest test_core -v
Covers boundary values on the ₹500 cap, input validation, double-spend, and the
cancel-vs-settle race (the bank is the single source of truth: first to reach it wins)."""
import time, unittest
from core import Bank, Merchant, Payer, generate_keypair, validate_payload, sign_data

M = "chai_corner@upi"

class Base(unittest.TestCase):
    def setUp(self):
        self.bank = Bank(":memory:")
        self.payer = Payer(":memory:")
        self.merchant = Merchant(":memory:")
        priv, pub = generate_keypair()
        ok, _ = self.bank.register_payer("ravi@upi", pub)
        self.assertTrue(ok)
        self.payer.register("ravi@upi", "http://bank", self.bank.get_public_key(), priv, pub)
        ok, _ = self.bank.register_merchant(M, "http://shop", "KEY")
        self.assertTrue(ok)
        self.merchant.register(M, "http://bank", "http://shop", "KEY")
        self.merchant.update_trusted_payers(self.bank.get_trusted_payers())

    def tearDown(self):
        for o in (self.bank, self.payer, self.merchant): o.conn.close()

    def remaining(self): return self.payer.get_info()["remaining_paise"]
    def status(self, tid): return next(t["status"] for t in self.payer.get_info()["tokens"] if t["token_id"] == tid)
    def refresh(self): return self.payer.reconcile(self.bank.get_payer_settlement_statement("ravi@upi"))
    def sync(self):
        res = self.bank.settle_batch(M, self.merchant.get_pending_batch())
        self.merchant.apply_sync_results(res)
        return res

class TestBoundaries(Base):
    def test_cap_boundaries(self):
        with self.assertRaises(ValueError): self.payer.create_token(M, 0)          # ₹0
        self.payer.create_token(M, 1)                                              # ₹0.01
        with self.assertRaises(ValueError): self.payer.create_token(M, 50_000)     # would be ₹500.01 total
        self.payer.create_token(M, 49_999)                                         # exactly ₹500 total
        self.assertEqual(self.remaining(), 0)

    def test_single_token_over_cap(self):
        with self.assertRaises(ValueError): self.payer.create_token(M, 50_001)

    def test_validate_payload(self):
        now = int(time.time())
        good = {"tokenID": "t", "payerID": "p", "payeeID": M, "amount_paise": 100, "timestamp": now}
        self.assertIsNone(validate_payload(good, now))
        for bad in ({"amount_paise": "abc"}, {"amount_paise": 0}, {"amount_paise": 50_001}, {"amount_paise": 1.5},
                    {"amount_paise": True}, {"timestamp": "x"}, {"timestamp": now + 3600}, {"payerID": ""}, {"tokenID": None}):
            self.assertIsNotNone(validate_payload({**good, **bad}, now), bad)
        self.assertIsNotNone(validate_payload("not a dict", now))

class TestMerchant(Base):
    def test_rejects_garbage_without_crashing(self):
        for pkg in (None, "x", {}, {"payload": {"amount_paise": "abc"}, "signature": "s"}, {"payload": [], "signature": "s"}):
            ok, _ = self.merchant.verify_and_queue(pkg)
            self.assertFalse(ok)

    def test_tampered_amount_rejected(self):
        pkg = self.payer.create_token(M, 100)
        pkg["payload"]["amount_paise"] = 40_000
        self.assertFalse(self.merchant.verify_and_queue(pkg)[0])

    def test_wrong_payee_and_replay(self):
        pkg = self.payer.create_token("other@upi", 100)
        self.assertFalse(self.merchant.verify_and_queue(pkg)[0])
        pkg = self.payer.create_token(M, 100)
        self.assertTrue(self.merchant.verify_and_queue(pkg)[0])
        self.assertFalse(self.merchant.verify_and_queue(pkg)[0])

class TestScenarios(Base):
    def test_A_merchant_syncs_first_cancel_loses(self):
        pkg = self.payer.create_token(M, 12_000); tid = pkg["payload"]["tokenID"]
        self.assertTrue(self.merchant.verify_and_queue(pkg)[0])
        cancel = self.payer.request_cancel(tid)                 # bank offline: cancel stays pending
        self.assertEqual(self.remaining(), 38_000)               # still blocked
        self.assertEqual(self.sync()[0]["status"], "SETTLED")    # merchant reaches bank first
        ok, st, _ = self.bank.cancel_token(cancel)               # late cancel refused
        self.assertFalse(ok); self.assertEqual(st, "SETTLED")
        self.payer.apply_cancel_result(tid, st)
        self.refresh()
        self.assertEqual(self.status(tid), "SETTLED")
        self.assertEqual(self.remaining(), 50_000)               # cycle complete, money went to merchant once

    def test_B_cancel_reaches_bank_first(self):
        pkg = self.payer.create_token(M, 12_000); tid = pkg["payload"]["tokenID"]
        self.merchant.verify_and_queue(pkg)
        ok, st, _ = self.bank.cancel_token(self.payer.request_cancel(tid))
        self.assertTrue(ok); self.assertEqual(st, "VOIDED")
        self.payer.apply_cancel_result(tid, st)
        self.assertEqual(self.remaining(), 50_000)
        res = self.sync()
        self.assertEqual(res[0]["status"], "CANCELLED_BY_PAYER")
        self.assertEqual(self.bank.get_stats()["settled_count"], 0)

    def test_C_refresh_is_idempotent(self):
        pkg = self.payer.create_token(M, 10_000)
        self.merchant.verify_and_queue(pkg); self.sync()
        self.payer.create_token(M, 5_000)                        # still unsettled
        counts = [self.refresh()["settled"] for _ in range(3)]
        self.assertEqual(counts, [1, 0, 0])
        self.assertEqual(self.remaining(), 45_000)

    def test_D_identity_takeover_blocked(self):
        _, pub = generate_keypair()
        self.assertFalse(self.bank.register_payer("ravi@upi", pub)[0])
        self.assertFalse(self.bank.register_merchant(M, "http://evil", "K2")[0])
        self.assertTrue(self.bank.verify_merchant(M, "KEY"))

    def test_E_bank_rejects_bad_amount(self):
        res = self.bank.settle_batch(M, [{"payload": {"tokenID": "x", "payerID": "ravi@upi", "payeeID": M, "amount_paise": "abc", "timestamp": 1}, "signature": "s"}, "junk"])
        self.assertEqual([r["status"] for r in res], ["REJECTED", "REJECTED"])

    def test_F_double_spend(self):
        pkg = self.payer.create_token(M, 100)
        self.merchant.verify_and_queue(pkg); self.sync()
        self.assertEqual(self.bank.settle_batch(M, [pkg])[0]["status"], "DUPLICATE")
        self.assertEqual(self.bank.simulate_duplicate()["status"], "DUPLICATE")
        self.assertEqual(self.bank.get_stats()["duplicate_count"], 2)

    def test_forged_cancel_rejected(self):
        pkg = self.payer.create_token(M, 100)
        priv, _ = generate_keypair()                             # attacker key
        p = {**pkg["payload"], "action": "CANCEL"}
        ok, st, _ = self.bank.cancel_token({"payload": p, "signature": sign_data(p, priv)})
        self.assertFalse(ok); self.assertEqual(st, "INVALID")

    def test_forged_statement_rejected(self):
        stmt = self.bank.get_payer_settlement_statement("ravi@upi")
        stmt["payload"]["voided_token_ids"] = ["anything"]
        with self.assertRaises(ValueError): self.payer.reconcile(stmt)

if __name__ == "__main__":
    unittest.main()
