"""
Trusted Identity Provisioning Tool for Offline UPI Simulation
Pre-provisions trusted payer public keys to Merchant and Bank registries.
Can operate directly against local SQLite databases (offline provisioning)
or over HTTP APIs across LAN devices using administrative authorization.
"""

import os
import sys
import argparse
import json
import sqlite3
import time
import requests
from typing import Optional

from payer import PayerDevice


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def provision_direct_db(
    payer_id: str,
    public_key_pem: str,
    merchant_db: Optional[str] = "merchant_queue_bob_upi.db",
    bank_db: Optional[str] = "offline_upi_backend.db"
) -> bool:
    """Inserts payer public key directly into Merchant and Bank SQLite databases."""
    success = True

    # 1. Merchant DB
    if merchant_db:
        try:
            conn = sqlite3.connect(merchant_db)
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS trusted_payers (
                        payer_id TEXT PRIMARY KEY,
                        public_key_pem TEXT NOT NULL
                    );
                """)
                conn.execute("""
                    INSERT OR REPLACE INTO trusted_payers (payer_id, public_key_pem)
                    VALUES (?, ?);
                """, (payer_id.strip(), public_key_pem.strip()))
            conn.close()
            print(f"  [OK] Registered '{payer_id}' in Merchant DB: {merchant_db}")
        except Exception as e:
            print(f"  [ERROR] Failed to register in Merchant DB ({merchant_db}): {e}")
            success = False

    # 2. Bank DB
    if bank_db:
        try:
            conn = sqlite3.connect(bank_db)
            now = time.time()
            with conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS trusted_payers (
                        payer_id TEXT PRIMARY KEY,
                        public_key_pem TEXT NOT NULL,
                        registered_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                """)
                conn.execute("""
                    INSERT OR REPLACE INTO trusted_payers (payer_id, public_key_pem, registered_at, updated_at)
                    VALUES (?, ?, ?, ?);
                """, (payer_id.strip(), public_key_pem.strip(), now, now))
            conn.close()
            print(f"  [OK] Registered '{payer_id}' in Bank DB: {bank_db}")
        except Exception as e:
            print(f"  [ERROR] Failed to register in Bank DB ({bank_db}): {e}")
            success = False

    return success


def provision_over_api(
    payer_id: str,
    public_key_pem: str,
    merchant_url: Optional[str] = None,
    bank_url: Optional[str] = None,
    admin_token: str = "demo-admin-secret-2026"
) -> bool:
    """Provisions trusted keys over HTTP APIs."""
    success = True

    # Merchant API
    if merchant_url:
        try:
            url = f"{merchant_url.rstrip('/')}/api/merchant/provision_payer"
            resp = requests.post(url, json={"payer_id": payer_id, "public_key_pem": public_key_pem}, timeout=3.0)
            if resp.status_code == 200:
                print(f"  [OK] Registered '{payer_id}' with Merchant Service at {merchant_url}")
            else:
                print(f"  [ERROR] Merchant returned HTTP {resp.status_code}: {resp.text}")
                success = False
        except Exception as e:
            print(f"  [ERROR] Could not reach Merchant Service at {merchant_url}: {e}")
            success = False

    # Bank API
    if bank_url:
        try:
            url = f"{bank_url.rstrip('/')}/api/bank/register_payer"
            headers = {"X-Admin-Token": admin_token}
            resp = requests.post(url, json={"payer_id": payer_id, "public_key_pem": public_key_pem}, headers=headers, timeout=3.0)
            if resp.status_code == 200:
                print(f"  [OK] Registered '{payer_id}' with Bank Service at {bank_url}")
            else:
                print(f"  [ERROR] Bank returned HTTP {resp.status_code}: {resp.text}")
                success = False
        except Exception as e:
            print(f"  [ERROR] Could not reach Bank Service at {bank_url}: {e}")
            success = False

    return success


def main():
    parser = argparse.ArgumentParser(description="Provision Trusted Payer Public Keys across Services/DBs")
    parser.add_argument("--payer-id", default="alice@upi", help="Payer ID (default: alice@upi)")
    parser.add_argument("--state-file", default="payer_state_alice_upi.json", help="Payer state file")
    parser.add_argument("--merchant-db", default="merchant_queue_bob_upi.db", help="Merchant SQLite database path")
    parser.add_argument("--bank-db", default="offline_upi_backend.db", help="Bank SQLite database path")
    parser.add_argument("--merchant-url", default=None, help="Merchant service URL (optional, e.g. http://192.168.1.12:5002)")
    parser.add_argument("--bank-url", default=None, help="Bank service URL (optional, e.g. http://192.168.1.10:5003)")
    parser.add_argument("--admin-token", default="demo-admin-secret-2026", help="Bank admin secret token")

    args = parser.parse_args()

    # Load or initialize payer to obtain public key PEM
    payer = PayerDevice(payer_id=args.payer_id, state_file=args.state_file)
    pub_pem = payer.public_key_pem

    print("\n" + "=" * 70)
    print("  TRUSTED PAYER IDENTITY PROVISIONING")
    print("=" * 70)
    print(f"  Payer ID:       {payer.payer_id}")
    print(f"  Public Key PEM: {pub_pem.splitlines()[1][:30]}...")
    print("-" * 70)

    # If DB files specified, write directly
    if args.merchant_db or args.bank_db:
        provision_direct_db(
            payer_id=payer.payer_id,
            public_key_pem=pub_pem,
            merchant_db=args.merchant_db,
            bank_db=args.bank_db
        )

    # If URLs specified, call HTTP APIs
    if args.merchant_url or args.bank_url:
        provision_over_api(
            payer_id=payer.payer_id,
            public_key_pem=pub_pem,
            merchant_url=args.merchant_url,
            bank_url=args.bank_url,
            admin_token=args.admin_token
        )

    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
