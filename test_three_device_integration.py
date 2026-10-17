"""
Live Multi-Process Three-Device Integration Test Suite
Launches Bank Service (5803), Payer Service (5801), and Merchant Service (5802) as separate OS subprocesses.
Executes the mandatory real-world offline demonstration:
  1. Starts Bank, Merchant, and Payer services on separate ports.
  2. Pre-provisions trusted keys.
  3. STOPS the Bank Service process completely (process is terminated).
  4. Generates an offline payment token on Payer Service (Device 1).
  5. STOPS the Payer Service process to prove complete caller independence.
  6. Transfers the signed token via JSON payload to Merchant Service (Device 2).
  7. Merchant verifies and queues the token offline in SQLite (Status: 'Pending Sync').
  8. Restarts Merchant Service process to confirm persistent queue recovery across reboot.
  9. Attempts sync while Bank is still stopped -> confirms graceful 503 and zero data loss.
  10. RESTARTS the Bank Service process.
  11. Merchant triggers batch sync over HTTP LAN to Bank Service.
  12. Verifies atomic settlement (settled_count = 1).
  13. Replays the exact same token -> verifies 100% duplicate rejection by Bank.
  14. Terminates all child processes cleanly and cleans up disk files.
"""

import unittest
import os
import sys
import time
import json
import socket
import subprocess
import requests

# Inject test credentials via environment variables (not CLI args).
# DEMO/TEST ONLY — these values must NOT be used in production deployments.
os.environ.setdefault("PAYER_PASSPHRASE", "test-passphrase-do-not-use-in-production")
os.environ.setdefault("BANK_ADMIN_TOKEN", "test-admin-token-do-not-use-in-production")

from provision_keys import provision_direct_db
from payer import PayerDevice


def kill_proc_tree(proc):
    """Guarantees process tree termination on Windows and Unix."""
    if not proc:
        return
    try:
        if sys.platform == "win32":
            subprocess.call(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        else:
            proc.terminate()
            proc.kill()
    except Exception:
        pass


def free_port(port: int):
    """Ensures a port is completely free on Windows."""
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(f"netstat -ano | findstr :{port}", shell=True).decode()
            for line in out.strip().splitlines():
                parts = line.split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    subprocess.call(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


class TestThreeDeviceIntegration(unittest.TestCase):
    PAYER_PORT = 5801
    MERCHANT_PORT = 5802
    BANK_PORT = 5803

    PAYER_STATE = "test_int_payer_state.json"
    MERCHANT_DB = "test_int_merchant_queue.db"
    BANK_DB = "test_int_bank.db"
    ADMIN_TOKEN = "test-int-admin-2026"

    @classmethod
    def setUpClass(cls):
        cls.python_exe = sys.executable
        # Environment that subprocess services inherit—credentials via env vars, never CLI args.
        cls.child_env = dict(os.environ)
        cls.child_env.setdefault("PAYER_PASSPHRASE", os.environ.get("PAYER_PASSPHRASE", ""))
        cls.child_env.setdefault("BANK_ADMIN_TOKEN", os.environ.get("BANK_ADMIN_TOKEN", ""))

        # Free any existing listening sockets on test ports
        for port in [cls.PAYER_PORT, cls.MERCHANT_PORT, cls.BANK_PORT]:
            free_port(port)

        # Clean old files
        for f in [cls.PAYER_STATE, cls.MERCHANT_DB, cls.BANK_DB]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

        # Pre-provision Alice in databases (passphrase comes from env)
        payer = PayerDevice(payer_id="alice@upi", state_file=cls.PAYER_STATE)
        provision_direct_db(
            payer_id="alice@upi",
            public_key_pem=payer.public_key_pem,
            merchant_db=cls.MERCHANT_DB,
            bank_db=cls.BANK_DB
        )

        cls.p_bank = None
        cls.p_merchant = None
        cls.p_payer = None

    @classmethod
    def tearDownClass(cls):
        # Terminate any running processes
        for p in [cls.p_payer, cls.p_merchant, cls.p_bank]:
            kill_proc_tree(p)

        # Clean files
        time.sleep(0.5)
        for f in [cls.PAYER_STATE, cls.MERCHANT_DB, cls.BANK_DB]:
            if os.path.exists(f):
                try:
                    os.remove(f)
                except Exception:
                    pass

    def wait_for_service(self, url: str, timeout: float = 8.0) -> bool:
        """Polls until a service responds with HTTP 200 or timeout expires."""
        start = time.time()
        while time.time() - start < timeout:
            try:
                r = requests.get(url, timeout=0.5)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(0.2)
        return False

    def start_bank(self):
        free_port(self.BANK_PORT)
        # SECURITY: admin token passed via env var, NOT as CLI argument
        self.p_bank = subprocess.Popen(
            [
                self.python_exe, "bank_service.py",
                "--host", "127.0.0.1",
                "--port", str(self.BANK_PORT),
                "--db", self.BANK_DB,
            ],
            env=self.child_env
        )
        ready = self.wait_for_service(f"http://127.0.0.1:{self.BANK_PORT}/health")
        self.assertTrue(ready, f"Bank service failed to start on port {self.BANK_PORT}")

    def stop_bank(self):
        kill_proc_tree(self.p_bank)
        self.p_bank = None
        time.sleep(0.3)

    def start_merchant(self):
        free_port(self.MERCHANT_PORT)
        self.p_merchant = subprocess.Popen([
            self.python_exe, "merchant_service.py",
            "--host", "127.0.0.1",
            "--port", str(self.MERCHANT_PORT),
            "--merchant-id", "merchant_bob@upi",
            "--db", self.MERCHANT_DB,
            "--bank-url", f"http://127.0.0.1:{self.BANK_PORT}"
        ])
        ready = self.wait_for_service(f"http://127.0.0.1:{self.MERCHANT_PORT}/health")
        self.assertTrue(ready, f"Merchant service failed to start on port {self.MERCHANT_PORT}")

    def stop_merchant(self):
        kill_proc_tree(self.p_merchant)
        self.p_merchant = None
        time.sleep(0.3)

    def start_payer(self):
        free_port(self.PAYER_PORT)
        # SECURITY: passphrase passed via env var, NOT as CLI argument
        self.p_payer = subprocess.Popen(
            [
                self.python_exe, "payer_service.py",
                "--host", "127.0.0.1",
                "--port", str(self.PAYER_PORT),
                "--payer-id", "alice@upi",
                "--state-file", self.PAYER_STATE
            ],
            env=self.child_env
        )
        ready = self.wait_for_service(f"http://127.0.0.1:{self.PAYER_PORT}/health")
        self.assertTrue(ready, f"Payer service failed to start on port {self.PAYER_PORT}")

    def stop_payer(self):
        kill_proc_tree(self.p_payer)
        self.p_payer = None
        time.sleep(0.3)

    def test_complete_three_device_offline_lifecycle(self):
        """
        Executes end-to-end multi-process offline test verifying:
        1. Bank stopped -> Payer generates token.
        2. Payer stopped -> Merchant verifies and queues token.
        3. Merchant reboot -> Queue survives.
        4. Sync while bank stopped -> 503, queue intact.
        5. Bank restart -> Delayed sync succeeds.
        6. Replay duplicate -> Bank rejects duplicate.
        """
        payer_url = f"http://127.0.0.1:{self.PAYER_PORT}"
        merchant_url = f"http://127.0.0.1:{self.MERCHANT_PORT}"
        bank_url = f"http://127.0.0.1:{self.BANK_PORT}"

        # -------------------------------------------------------------
        # STEP 1: Start All Three Services
        # -------------------------------------------------------------
        self.start_bank()
        self.start_merchant()
        self.start_payer()

        # -------------------------------------------------------------
        # STEP 2: Kill the Bank Service Completely!
        # -------------------------------------------------------------
        self.stop_bank()
        with self.assertRaises(requests.exceptions.RequestException):
            requests.get(f"{bank_url}/health", timeout=0.5)

        # -------------------------------------------------------------
        # STEP 3: Generate Token on Device 1 (Payer) with Bank DEAD
        # -------------------------------------------------------------
        res_gen = requests.post(f"{payer_url}/api/payer/generate", json={
            "amount": 175.50,
            "payee_id": "merchant_bob@upi"
        }, timeout=2.0)
        self.assertEqual(res_gen.status_code, 200, "Payer failed to generate token while bank was down")
        gen_data = res_gen.json()
        token_package = gen_data["token_package"]
        token_id = token_package["payload"]["tokenID"]
        self.assertEqual(gen_data["unsynced_spent"], 175.50)

        # -------------------------------------------------------------
        # STEP 4: Stop Payer Process (Prove Merchant doesn't call Payer)
        # -------------------------------------------------------------
        self.stop_payer()

        # -------------------------------------------------------------
        # STEP 5: Offline Handoff to Merchant (Device 2) with Bank & Payer DEAD
        # -------------------------------------------------------------
        res_rec = requests.post(f"{merchant_url}/api/merchant/receive", json={
            "token_package": token_package
        }, timeout=2.0)
        self.assertEqual(res_rec.status_code, 200)
        rec_data = res_rec.json()
        self.assertTrue(rec_data["accepted"], f"Merchant rejected valid token: {rec_data}")
        self.assertEqual(rec_data["status"], "Pending Sync")

        # Confirm queue state in Merchant
        res_m_stat = requests.get(f"{merchant_url}/api/merchant/status", timeout=2.0).json()
        self.assertEqual(res_m_stat["pending_sync"], 1)
        self.assertEqual(res_m_stat["pending_sync_amount"], 175.50)

        # -------------------------------------------------------------
        # STEP 6: Simulate Merchant Terminal Restart (Persistence Recovery)
        # -------------------------------------------------------------
        self.stop_merchant()
        self.start_merchant()

        res_m_recovered = requests.get(f"{merchant_url}/api/merchant/status", timeout=2.0).json()
        self.assertEqual(res_m_recovered["pending_sync"], 1, "Merchant queue lost pending tokens after reboot")
        self.assertEqual(res_m_recovered["pending_sync_amount"], 175.50)

        # -------------------------------------------------------------
        # STEP 7: Attempt Sync while Bank is still Stopped -> Graceful 503
        # -------------------------------------------------------------
        res_sync_offline = requests.post(f"{merchant_url}/api/merchant/sync", timeout=3.0)
        self.assertEqual(res_sync_offline.status_code, 503, "Sync should return 503 when bank is stopped")

        # Confirm 100% of queue is still preserved
        res_m_intact = requests.get(f"{merchant_url}/api/merchant/status", timeout=2.0).json()
        self.assertEqual(res_m_intact["pending_sync"], 1)

        # -------------------------------------------------------------
        # STEP 8: Restart the Bank Service Process!
        # -------------------------------------------------------------
        self.start_bank()

        # -------------------------------------------------------------
        # STEP 9: Synchronize Merchant Queue with Bank
        # -------------------------------------------------------------
        res_sync_online = requests.post(f"{merchant_url}/api/merchant/sync", timeout=5.0)
        self.assertEqual(res_sync_online.status_code, 200, f"Sync failed: {res_sync_online.text}")
        sync_report = res_sync_online.json()
        self.assertEqual(sync_report["settled_count"], 1)
        self.assertEqual(sync_report["duplicate_count"], 0)

        # Merchant queue updates to 0 pending, 1 settled
        res_m_settled = requests.get(f"{merchant_url}/api/merchant/status", timeout=2.0).json()
        self.assertEqual(res_m_settled["pending_sync"], 0)
        self.assertEqual(res_m_settled["settled"], 1)

        # Bank ledger contains the settled token
        res_bank_stat = requests.get(f"{bank_url}/api/bank/status", timeout=2.0).json()
        self.assertEqual(res_bank_stat["settled_count"], 1)
        self.assertEqual(res_bank_stat["total_settled_value"], 175.50)

        # -------------------------------------------------------------
        # STEP 10: Replay Duplicate Token -> 100% Duplicate Rejection
        # -------------------------------------------------------------
        res_dup_sync = requests.post(f"{bank_url}/api/bank/sync_batch", headers={
            "X-Merchant-Key": "merchant-bob-secret-key-2026"
        }, json={
            "merchant_id": "merchant_bob@upi",
            "tokens": [token_package]
        }, timeout=3.0)
        self.assertEqual(res_dup_sync.status_code, 200)
        dup_report = res_dup_sync.json()
        self.assertEqual(dup_report["settled_count"], 0, "Duplicate token was settled!")
        self.assertEqual(dup_report["duplicate_count"], 1, "Duplicate token was not caught!")
        self.assertIn("Double-spend detected", dup_report["duplicates"][0]["reason"])

        # -------------------------------------------------------------
        # STEP 11: Payer Reconciles with Bank-Signed Settlement Receipt
        # -------------------------------------------------------------
        res_rc = requests.get(f"{bank_url}/api/bank/receipts/{token_id}", timeout=2.0)
        self.assertEqual(res_rc.status_code, 200)
        receipt_pkg = res_rc.json()
        self.assertIn("receipt", receipt_pkg)
        self.assertIn("signature", receipt_pkg)

        # Fetch the bank public key from the bank service and provision it
        # into the payer state file so that the payer can pin and verify the receipt.
        res_bank_pk = requests.get(f"{bank_url}/api/bank/public_key", timeout=2.0)
        self.assertEqual(res_bank_pk.status_code, 200)
        bank_public_key_pem = res_bank_pk.json()["public_key_pem"]

        # Restart payer with the bank's public key pinned via env var
        self.stop_payer()
        env_with_bank_key = dict(self.child_env)
        env_with_bank_key["BANK_PUBLIC_KEY_PEM"] = bank_public_key_pem
        self.p_payer = subprocess.Popen(
            [
                self.python_exe, "payer_service.py",
                "--host", "127.0.0.1",
                "--port", str(self.PAYER_PORT),
                "--payer-id", "alice@upi",
                "--state-file", self.PAYER_STATE
            ],
            env=env_with_bank_key
        )
        ready = self.wait_for_service(f"http://127.0.0.1:{self.PAYER_PORT}/health")
        self.assertTrue(ready, "Payer service failed to restart for reconciliation step")

        res_recon = requests.post(f"{payer_url}/api/payer/reconcile", json={
            "receipt": receipt_pkg
        }, timeout=2.0)
        self.assertEqual(res_recon.status_code, 200)
        recon_data = res_recon.json()
        self.assertTrue(recon_data["success"])
        self.assertEqual(recon_data["token_id"], token_id)
        self.assertEqual(recon_data["remaining_allowance"], 500.0)
        self.assertEqual(recon_data["unsynced_spent"], 0.0)

        # Clean shutdown of remaining processes
        self.stop_payer()
        self.stop_merchant()
        self.stop_bank()


if __name__ == "__main__":
    unittest.main()
