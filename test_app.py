"""
Integration Tests for Flask Web Application and Dashboards
Tests routes, API endpoints, payment generation, local verification,
delayed sync, network simulation, and duplicate prevention.
"""

import unittest
import json
import os
from app import app, payer, merchant, backend


class TestOfflineUPIDashboards(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()
        app.config["TESTING"] = True

    def test_pages_render_successfully(self):
        """Tests that portal and all three dashboard HTML pages return 200 OK."""
        for path in ["/", "/payer", "/merchant", "/bank"]:
            res = self.client.get(path)
            self.assertEqual(res.status_code, 200, f"Route {path} failed with status {res.status_code}")
            self.assertIn(b"Offline UPI", res.data)

    def test_network_info_endpoint(self):
        """Tests the LAN info discovery endpoint."""
        res = self.client.get("/api/network_info")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("lan_ip", data)
        self.assertIn("port", data)
        self.assertIn("network_online", data)

    def test_payer_workflow_and_spend_cap(self):
        """Tests payer token generation, balance tracking, and ₹500 cap enforcement."""
        # 1. Reset balance for test
        payer.reset_offline_balance()

        res_status = self.client.get("/api/payer/status")
        self.assertEqual(res_status.status_code, 200)
        status = res_status.get_json()
        self.assertEqual(status["unsynced_spent"], 0.0)
        self.assertEqual(status["remaining_allowance"], 500.0)

        # 2. Generate valid payment of ₹200
        res_gen = self.client.post("/api/payer/generate", json={
            "amount": 200.0,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(res_gen.status_code, 200)
        gen_data = res_gen.get_json()
        self.assertTrue(gen_data["success"])
        token_pkg = gen_data["token_package"]
        self.assertEqual(token_pkg["payload"]["amount"], 200.0)
        self.assertEqual(token_pkg["payload"]["payeeID"], "merchant_bob@upi")

        # 3. Check updated allowance
        res_status2 = self.client.get("/api/payer/status")
        self.assertEqual(res_status2.get_json()["unsynced_spent"], 200.0)
        self.assertEqual(res_status2.get_json()["remaining_allowance"], 300.0)

        # 4. Attempt to exceed cap with ₹350 (200 + 350 = 550 > 500)
        res_exceed = self.client.post("/api/payer/generate", json={
            "amount": 350.0,
            "payee_id": "merchant_bob@upi"
        })
        self.assertEqual(res_exceed.status_code, 400)
        self.assertIn("Offline limit exceeded", res_exceed.get_json()["error"])

    def test_merchant_receive_and_local_verification(self):
        """Tests merchant receiving and locally verifying tokens without bank contact."""
        payer.reset_offline_balance()
        merchant.reset_queue()

        # Generate token
        res_gen = self.client.post("/api/payer/generate", json={
            "amount": 100.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = res_gen.get_json()["token_package"]

        # Merchant receives and verifies
        res_receive = self.client.post("/api/merchant/receive", json={
            "token_package": token_pkg
        })
        self.assertEqual(res_receive.status_code, 200)
        rec_data = res_receive.get_json()
        self.assertTrue(rec_data["accepted"])
        self.assertEqual(rec_data["status"], "Pending Sync")
        self.assertTrue(rec_data["within_nfr_latency"])

        # Check queue
        res_m_status = self.client.get("/api/merchant/status")
        m_status = res_m_status.get_json()
        self.assertEqual(m_status["pending_sync"], 1)
        self.assertEqual(m_status["pending_sync_amount"], 100.0)

    def test_merchant_rejects_payee_mismatch(self):
        """Tests that merchant rejects token intended for another merchant."""
        payer.reset_offline_balance()
        res_gen = self.client.post("/api/payer/generate", json={
            "amount": 50.0,
            "payee_id": "wrong_merchant@upi"
        })
        token_pkg = res_gen.get_json()["token_package"]

        res_receive = self.client.post("/api/merchant/receive", json={
            "token_package": token_pkg
        })
        rec_data = res_receive.get_json()
        self.assertFalse(rec_data["accepted"])
        self.assertEqual(rec_data["status"], "REJECTED")
        self.assertIn("Payee mismatch", rec_data["reason"])

    def test_network_outage_simulation_and_delayed_sync(self):
        """Tests that bank outage retains queue (503) and sync succeeds when online."""
        payer.reset_offline_balance()
        merchant.reset_queue()

        # Ingest one token
        res_gen = self.client.post("/api/payer/generate", json={
            "amount": 80.0,
            "payee_id": "merchant_bob@upi"
        })
        token_pkg = res_gen.get_json()["token_package"]
        self.client.post("/api/merchant/receive", json={"token_package": token_pkg})

        # 1. Simulate network outage
        import app as flask_app
        flask_app.network_online = False

        res_sync_offline = self.client.post("/api/merchant/sync")
        self.assertEqual(res_sync_offline.status_code, 503)
        self.assertIn("unavailable", res_sync_offline.get_json()["error"])

        # Confirm queue still has 1 pending token
        res_m = self.client.get("/api/merchant/status")
        self.assertEqual(res_m.get_json()["pending_sync"], 1)

        # 2. Restore network connectivity
        flask_app.network_online = True
        res_sync_online = self.client.post("/api/merchant/sync")
        self.assertEqual(res_sync_online.status_code, 200)
        sync_data = res_sync_online.get_json()
        self.assertEqual(sync_data["settled_count"], 1)

        # Queue now has 0 pending and 1 settled
        res_m_after = self.client.get("/api/merchant/status")
        self.assertEqual(res_m_after.get_json()["pending_sync"], 0)
        self.assertEqual(res_m_after.get_json()["settled"], 1)

    def test_double_spend_simulation_endpoint(self):
        """Tests the interactive double-spend simulator endpoint on Bank dashboard."""
        res = self.client.post("/api/bank/simulate_duplicate")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["success"])
        self.assertIn("Double-spend detected", data["result"]["reason"])


if __name__ == "__main__":
    unittest.main()
