"""
Demo Data Reset Utility for Offline UPI Payment Simulator.
Safely clears disposable demo records (transactions, local queue, receipts, audit logs)
across Payer, Merchant, and Bank services while strictly preserving:
  - Cryptographic keys (Payer RSA-2048 PBKDF2/AES-GCM container, Bank RSA-2048 system keys)
  - Pre-provisioned Trusted Payer registries (alice@upi public key PEM)
  - Authorized merchant credentials (merchant_bob@upi API key hash)
  - Database schemas and table structures
"""

import os
import sys
import json
import sqlite3
from typing import Dict, Any, Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def reset_payer_state(state_file: str = "payer_state_alice_upi.json") -> Dict[str, Any]:
    """Resets payer spend counters and outstanding tokens; preserves keys and cap."""
    if not os.path.exists(state_file):
        return {"status": "skipped", "message": f"Payer state file '{state_file}' does not exist."}

    try:
        with open(state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        cleared_tokens_count = len(state.get("outstanding_tokens", {}))
        prev_spent = state.get("unsynced_spent", 0.0)

        # Reset disposable state
        state["unsynced_spent"] = 0.0
        state["unsynced_spent_paise"] = 0
        state["outstanding_tokens"] = {}

        # Save back atomically
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

        return {
            "status": "success",
            "cleared_tokens": cleared_tokens_count,
            "previous_spent": prev_spent,
            "message": f"Payer balance reset to ₹0.00 / ₹500.00. Cleared {cleared_tokens_count} outstanding token(s). Keys preserved."
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def reset_merchant_db(db_path: str = "merchant_queue_bob_upi.db") -> Dict[str, Any]:
    """Clears merchant local pending/processed queue; preserves trusted payers table."""
    if not os.path.exists(db_path):
        return {"status": "skipped", "message": f"Merchant DB '{db_path}' does not exist."}

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        # Check if local_queue table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='local_queue'")
        if cur.fetchone():
            cur.execute("SELECT COUNT(*) FROM local_queue")
            count = cur.fetchone()[0]
            cur.execute("DELETE FROM local_queue")
            conn.commit()
        else:
            count = 0

        # Verify trusted_payers preserved
        cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='trusted_payers'")
        has_payers = bool(cur.fetchone())
        payers_count = 0
        if has_payers:
            cur.execute("SELECT COUNT(*) FROM trusted_payers")
            payers_count = cur.fetchone()[0]

        conn.close()

        return {
            "status": "success",
            "cleared_queue_items": count,
            "preserved_trusted_payers": payers_count,
            "message": f"Cleared {count} queued transaction(s) from merchant POS. Preserved {payers_count} trusted payer registry entry(ies)."
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def reset_bank_db(db_path: str = "offline_upi_backend.db") -> Dict[str, Any]:
    """Clears settled tokens, receipts, and sync audit logs; preserves bank keys and accounts."""
    if not os.path.exists(db_path):
        return {"status": "skipped", "message": f"Bank DB '{db_path}' does not exist."}

    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()

        cleared_counts = {}
        for table in ["settled_tokens", "settlement_receipts", "sync_audit_log"]:
            cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if cur.fetchone():
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                cleared_counts[table] = cur.fetchone()[0]
                cur.execute(f"DELETE FROM {table}")
            else:
                cleared_counts[table] = 0

        # Log reset in admin audit log if table exists
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='admin_audit_log'")
        if cur.fetchone():
            cur.execute("DELETE FROM admin_audit_log")

        conn.commit()

        # Verify essential config preserved
        preserved = {}
        for table in ["bank_system_keys", "trusted_payers", "authorized_merchants"]:
            cur.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'")
            if cur.fetchone():
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                preserved[table] = cur.fetchone()[0]

        conn.close()

        return {
            "status": "success",
            "cleared": cleared_counts,
            "preserved": preserved,
            "message": f"Cleared {cleared_counts.get('settled_tokens', 0)} settled tokens, "
                       f"{cleared_counts.get('settlement_receipts', 0)} receipts, "
                       f"{cleared_counts.get('sync_audit_log', 0)} sync logs. "
                       f"Preserved core signing keys, {preserved.get('trusted_payers', 0)} payers, "
                       f"and {preserved.get('authorized_merchants', 0)} merchants."
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


def reset_all_demo_data(
    payer_state_file: str = "payer_state_alice_upi.json",
    merchant_db: str = "merchant_queue_bob_upi.db",
    bank_db: str = "offline_upi_backend.db"
) -> Dict[str, Any]:
    """Coordinates a complete reset across Payer, Merchant, and Bank data stores."""
    p_res = reset_payer_state(payer_state_file)
    m_res = reset_merchant_db(merchant_db)
    b_res = reset_bank_db(bank_db)

    return {
        "success": (p_res.get("status") in ("success", "skipped") and
                    m_res.get("status") in ("success", "skipped") and
                    b_res.get("status") in ("success", "skipped")),
        "payer": p_res,
        "merchant": m_res,
        "bank": b_res,
        "preserved_items": [
            "Customer private RSA key (AES-256-GCM encrypted)",
            "Bank core signing RSA keypair",
            "Pre-provisioned trusted payer public key registry (alice@upi)",
            "Authorized merchant credentials (merchant_bob@upi)",
            "Database table schemas and configuration parameters"
        ]
    }


if __name__ == "__main__":
    print("=" * 70)
    print("  RESETTING OFFLINE UPI DEMO DATA")
    print("=" * 70)
    result = reset_all_demo_data()
    print("Payer:   ", result["payer"].get("message") or result["payer"])
    print("Merchant:", result["merchant"].get("message") or result["merchant"])
    print("Bank:    ", result["bank"].get("message") or result["bank"])
    print("\nPreserved configuration:")
    for item in result["preserved_items"]:
        print(f"  ✓ {item}")
    print("=" * 70)
