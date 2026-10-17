"""
Optional Sample Data Loader for Offline UPI Payment Simulator.
Disabled by default. Only triggered when the user explicitly clicks "Load Sample Data".
Generates realistic, cryptographically valid sample transactions:
  - 1 Settled Transaction (₹120.00) in Bank Ledger with signed receipt
  - 1 Pending Sync Transaction (₹80.00) in Merchant Local Queue
  - Updates Payer balance accordingly (₹200.00 spent out of ₹500.00 cap)
"""

import os
import sys
import time
import json
import uuid
import sqlite3
from typing import Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from payer import PayerDevice
from merchant import MerchantDevice
from backend import BackendServer


def load_sample_demo_data(
    payer_id: str = "alice@upi",
    merchant_id: str = "merchant_bob@upi",
    payer_state_file: str = "payer_state_alice_upi.json",
    merchant_db: str = "merchant_queue_bob_upi.db",
    bank_db: str = "offline_upi_backend.db",
    passphrase: Optional[str] = None
) -> Dict[str, Any]:
    """Generates explicit sample transactions for demonstration purposes."""
    resolved_passphrase = passphrase or os.getenv("PAYER_PASSPHRASE") or "demo-payer-passphrase"
    
    # 1. Initialize Payer
    try:
        payer = PayerDevice(
            payer_id=payer_id,
            offline_cap=500.0,
            state_file=payer_state_file,
            passphrase=resolved_passphrase
        )
    except Exception as e:
        return {"status": "error", "error": f"Failed to initialize payer: {str(e)}"}

    # 2. Initialize Merchant & Bank
    merchant = MerchantDevice(merchant_id=merchant_id, queue_db_path=merchant_db)
    backend = BackendServer(db_path=bank_db)

    # Ensure payer is registered in both registries
    merchant.register_payer(payer.payer_id, payer.public_key_pem)
    backend.register_payer(payer.payer_id, payer.public_key_pem, allow_update=True)

    # Reset any existing disposable data first for a predictable sample state
    from reset_demo import reset_all_demo_data
    reset_all_demo_data(payer_state_file, merchant_db, bank_db)

    # Reload payer to reflect 0 spend
    payer = PayerDevice(
        payer_id=payer_id,
        offline_cap=500.0,
        state_file=payer_state_file,
        passphrase=resolved_passphrase
    )

    # Sample 1: ₹120.00 payment -> Generated, Accepted by Merchant, and Settled by Bank
    token_1 = payer.generate_token(amount=120.0, payee_id=merchant_id)
    payer.mark_token_exported(token_1["payload"]["tokenID"])
    merchant.verify_and_accept_token(token_1)
    sync_report = backend.sync_batch(merchant_id=merchant_id, tokens=[token_1])
    
    # Update merchant queue status to 'Settled'
    with merchant.conn:
        merchant.conn.execute(
            "UPDATE local_queue SET status = 'Settled' WHERE token_id = ?",
            (token_1["payload"]["tokenID"],)
        )

    # Reconcile token 1 at payer using bank receipt
    receipt = backend.get_receipt(token_1["payload"]["tokenID"])
    if receipt:
        payer.reconcile_with_bank_receipt(receipt, bank_public_key=backend.bank_public_key)

    # Sample 2: ₹80.00 payment -> Generated, Accepted by Merchant, Awaiting Bank Sync
    token_2 = payer.generate_token(amount=80.0, payee_id=merchant_id)
    payer.mark_token_exported(token_2["payload"]["tokenID"])
    merchant.verify_and_accept_token(token_2)

    return {
        "status": "success",
        "message": "Sample demonstration data loaded: 1 settled payment (₹120.00) in Bank ledger and 1 pending payment (₹80.00) in Merchant POS queue.",
        "sample_tokens": [
            {"token_id": token_1["payload"]["tokenID"], "amount": 120.0, "status": "Settled"},
            {"token_id": token_2["payload"]["tokenID"], "amount": 80.0, "status": "Pending Sync"}
        ],
        "payer_state": {
            "spent": payer.unsynced_spent,
            "allowance": payer.remaining_offline_allowance
        }
    }


if __name__ == "__main__":
    res = load_sample_demo_data()
    print(json.dumps(res, indent=2))
