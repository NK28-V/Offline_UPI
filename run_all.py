"""
Three-Device Local Runner
Launches all three independent services as separate processes:
  - Device 1: Payer Service (Port 5001)
  - Device 2: Merchant Service (Port 5002)
  - Device 3: Bank Service (Port 5003)
Provides a single-command local demonstration of the three-device architecture.
Press Ctrl+C to terminate all services.
"""

import sys
import os
import time
import subprocess
import signal

from provision_keys import provision_direct_db
from payer import PayerDevice


def get_lan_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def main():
    print("\n" + "=" * 80)
    print("  LAUNCHING TRUE THREE-DEVICE OFFLINE UPI SERVICES")
    print("=" * 80)

    # --- Enforce required credentials from environment (never from CLI args) ---
    payer_passphrase = os.getenv("PAYER_PASSPHRASE", "")
    bank_admin_token = os.getenv("BANK_ADMIN_TOKEN", "")

    missing = []
    if not payer_passphrase:
        missing.append("PAYER_PASSPHRASE")
    if not bank_admin_token:
        missing.append("BANK_ADMIN_TOKEN")

    if missing:
        print(
            "[FATAL] Required environment variables are not set: " + ", ".join(missing) + "\n"
            "  Set them before running:\n"
            "    Windows:\n"
            "      set PAYER_PASSPHRASE=your-strong-passphrase\n"
            "      set BANK_ADMIN_TOKEN=your-strong-admin-token\n"
            "    Linux/Mac:\n"
            "      export PAYER_PASSPHRASE=your-strong-passphrase\n"
            "      export BANK_ADMIN_TOKEN=your-strong-admin-token\n"
            "  DEMO ONLY: Any non-empty strings work for the academic demonstration.",
            file=sys.stderr
        )
        sys.exit(1)

    # 1. Provision default payer identity into merchant and bank registries.
    # PayerDevice reads PAYER_PASSPHRASE from the environment automatically.
    payer = PayerDevice(payer_id="alice@upi", state_file="payer_state_alice_upi.json")
    print("\n[PROVISIONING] Registering default payer 'alice@upi' across registries...")
    provision_direct_db(
        payer_id=payer.payer_id,
        public_key_pem=payer.public_key_pem,
        merchant_db="merchant_queue_bob_upi.db",
        bank_db="offline_upi_backend.db"
    )

    lan_ip = get_lan_ip()
    python_exe = sys.executable

    procs = []
    try:
        # Start Bank Service (Device 3) on Port 5003
        print("[STARTING] Device 3: Bank Service on Port 5003...")
        p_bank = subprocess.Popen([
            python_exe, "bank_service.py",
            "--port", "5003",
            "--db", "offline_upi_backend.db"
        ])
        procs.append(("Bank Service", p_bank))

        # Start Merchant Service (Device 2) on Port 5002
        print("[STARTING] Device 2: Merchant Service on Port 5002...")
        p_merchant = subprocess.Popen([
            python_exe, "merchant_service.py",
            "--port", "5002",
            "--merchant-id", "merchant_bob@upi",
            "--db", "merchant_queue_bob_upi.db",
            "--bank-url", "http://localhost:5003"
        ])
        procs.append(("Merchant Service", p_merchant))

        # Start Payer Service (Device 1) on Port 5001
        print("[STARTING] Device 1: Payer Service on Port 5001...")
        p_payer = subprocess.Popen([
            python_exe, "payer_service.py",
            "--port", "5001",
            "--payer-id", "alice@upi",
            "--state-file", "payer_state_alice_upi.json"
        ])
        procs.append(("Payer Service", p_payer))

        time.sleep(1.0)
        print("\n" + "=" * 80)
        print("  ALL THREE SERVICES RUNNING AS INDEPENDENT PROCESSES")
        print("=" * 80)
        print(f"  📱 Customer Payer Device:      http://localhost:5001/payer   (LAN: http://{lan_ip}:5001/payer)")
        print(f"  🏪 Business Owner Merchant:   http://localhost:5002/merchant (LAN: http://{lan_ip}:5002/merchant)")
        print(f"  🏦 Central Bank Core Switch:   http://localhost:5003/bank    (LAN: http://{lan_ip}:5003/bank)")
        print("=" * 80)
        print("  Demonstration steps:")
        print("  1. Stop the Bank (Device 3) anytime to prove 100% offline payment generation & verification.")
        print("  2. Generate a token on Device 1 (Payer).")
        print("  3. Transfer via QR or JSON to Device 2 (Merchant).")
        print("  4. Restart Device 3 (Bank) and sync queued tokens from Device 2.")
        print("=" * 80)
        print("  Press Ctrl+C to shut down all three services.\n")

        # Wait on processes
        while True:
            time.sleep(1.0)
            for name, proc in procs:
                if proc.poll() is not None:
                    print(f"\n[NOTICE] {name} exited with code {proc.returncode}.")

    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping all three services...")
    finally:
        for name, proc in procs:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except Exception:
                    proc.kill()
        print("  ✓ All services stopped cleanly.\n")


if __name__ == "__main__":
    main()
