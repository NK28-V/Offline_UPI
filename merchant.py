"""
Merchant Device Module for Offline UPI Payment System
Receives offline payment tokens, enforces trusted payer public key registry,
verifies RSA signatures locally in under 1 second, enforces payee binding,
and persists verified transactions in a local SQLite queue under 'Pending Sync'.
"""

import time
import sqlite3
import json
import re
import os
from typing import Dict, Any, List, Optional
from crypto_utils import load_public_key_from_pem, verify_signature, validate_token_payload


class MerchantDevice:
    """
    Simulates a merchant terminal (POS / Soundbox / smartphone) operating offline.
    Verifies payer signatures using a pre-provisioned trusted public key registry,
    enforces strict payee binding, and persists local queue across device restarts.
    """

    def __init__(
        self,
        merchant_id: str,
        trusted_registry: Optional[Dict[str, str]] = None,
        queue_db_path: Optional[str] = None
    ):
        self.merchant_id = merchant_id.strip()
        # Pre-provisioned trusted public keys: {payer_id: public_key_pem}
        self.trusted_registry: Dict[str, str] = dict(trusted_registry or {})

        # Setup local persistent queue in SQLite
        if queue_db_path:
            self.queue_db_path = queue_db_path
        else:
            safe_id = re.sub(r'[^a-zA-Z0-9_-]', '_', self.merchant_id)
            self.queue_db_path = f"merchant_queue_{safe_id}.db"

        self.conn = sqlite3.connect(self.queue_db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._initialize_storage()

    def _initialize_storage(self) -> None:
        """Initializes persistent tables for local offline queue and trusted registry."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS local_queue (
                    token_id TEXT PRIMARY KEY,
                    payer_id TEXT NOT NULL,
                    payee_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    status TEXT NOT NULL,
                    token_pkg_json TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    verification_time_ms REAL NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS trusted_payers (
                    payer_id TEXT PRIMARY KEY,
                    public_key_pem TEXT NOT NULL
                );
            """)

        # Sync any in-memory registry to DB, or load from DB if DB already has records
        cursor = self.conn.cursor()
        cursor.execute("SELECT payer_id, public_key_pem FROM trusted_payers")
        for row in cursor.fetchall():
            if row["payer_id"] not in self.trusted_registry:
                self.trusted_registry[row["payer_id"]] = row["public_key_pem"]

        with self.conn:
            for pid, pem in self.trusted_registry.items():
                self.conn.execute(
                    "INSERT OR REPLACE INTO trusted_payers (payer_id, public_key_pem) VALUES (?, ?)",
                    (pid, pem)
                )

    def register_payer(self, payer_id: str, public_key_pem: str) -> None:
        """Adds or updates a trusted payer's public key in the merchant's registry."""
        self.trusted_registry[payer_id] = public_key_pem
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO trusted_payers (payer_id, public_key_pem) VALUES (?, ?)",
                (payer_id, public_key_pem)
            )

    def verify_and_accept_token(self, token_package: Any) -> Dict[str, Any]:
        """
        Processes an incoming offline token with strict validation:
        1. Validates package structure and types.
        2. Validates payload attributes (positive amount, non-future timestamp, non-empty IDs).
        3. Enforces Payee Binding: payload['payeeID'] == self.merchant_id.
        4. Enforces Trusted Payer Registry: payer must be pre-registered; ignores untrusted sent key.
        5. Verifies RSA-PSS digital signature using the registered public key.
        6. Enforces local queue duplicate detection.
        7. Persists verified token in SQLite queue with status 'Pending Sync'.
        
        Returns structured rejection results instead of throwing unhandled exceptions.
        """
        start_time = time.perf_counter()

        if not isinstance(token_package, dict):
            return {
                "accepted": False,
                "token_id": None,
                "status": "REJECTED",
                "reason": "Token package must be a dictionary.",
                "verification_time_ms": (time.perf_counter() - start_time) * 1000.0
            }

        payload = token_package.get("payload")
        signature_b64 = token_package.get("signature")

        # 1. Strict Payload Validation
        is_valid_payload, validation_error = validate_token_payload(payload)
        if not is_valid_payload:
            return {
                "accepted": False,
                "token_id": payload.get("tokenID") if isinstance(payload, dict) else None,
                "status": "REJECTED",
                "reason": f"Input validation failed: {validation_error}",
                "verification_time_ms": (time.perf_counter() - start_time) * 1000.0
            }

        if not isinstance(signature_b64, str) or not signature_b64.strip():
            return {
                "accepted": False,
                "token_id": payload.get("tokenID"),
                "status": "REJECTED",
                "reason": "Missing or invalid cryptographic signature in token package.",
                "verification_time_ms": (time.perf_counter() - start_time) * 1000.0
            }

        token_id = payload["tokenID"]
        payer_id = payload["payerID"]
        payee_id = payload["payeeID"]
        amount = payload["amount"]

        # 2. Payee Binding Check
        if payee_id != self.merchant_id:
            return {
                "accepted": False,
                "token_id": token_id,
                "status": "REJECTED",
                "reason": f"Payee mismatch: token intended for '{payee_id}', but presented to '{self.merchant_id}'.",
                "verification_time_ms": (time.perf_counter() - start_time) * 1000.0
            }

        # 3. Trusted Payer Registry Lookup (NEVER trust key sent with token alone)
        trusted_public_pem = self.trusted_registry.get(payer_id)
        if not trusted_public_pem:
            return {
                "accepted": False,
                "token_id": token_id,
                "status": "REJECTED",
                "reason": f"Unknown payer identity '{payer_id}': not found in merchant's trusted public key registry.",
                "verification_time_ms": (time.perf_counter() - start_time) * 1000.0
            }

        # 4. Cryptographic Signature Verification using Trusted Registry Key
        try:
            trusted_public_key = load_public_key_from_pem(trusted_public_pem)
            is_sig_valid = verify_signature(payload, signature_b64, trusted_public_key)
        except Exception as e:
            is_sig_valid = False

        verification_latency_sec = time.perf_counter() - start_time
        verification_time_ms = verification_latency_sec * 1000.0

        if not is_sig_valid:
            return {
                "accepted": False,
                "token_id": token_id,
                "status": "REJECTED",
                "reason": "Cryptographic signature verification FAILED (tamper detected or forged identity).",
                "verification_time_ms": verification_time_ms
            }

        # 5. Local Queue Duplicate Check in Persistent SQLite Storage
        cursor = self.conn.cursor()
        cursor.execute("SELECT token_id, status FROM local_queue WHERE token_id = ?", (token_id,))
        existing = cursor.fetchone()
        if existing:
            return {
                "accepted": False,
                "token_id": token_id,
                "status": "REJECTED",
                "reason": f"Duplicate token ID '{token_id}' already present in merchant queue (status: {existing['status']}).",
                "verification_time_ms": verification_time_ms
            }

        # 6. Atomic Persistence into Local SQLite Queue
        now = time.time()
        pkg_json = json.dumps(token_package)
        with self.conn:
            self.conn.execute("""
                INSERT INTO local_queue (
                    token_id, payer_id, payee_id, amount, status,
                    token_pkg_json, received_at, verification_time_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                token_id, payer_id, payee_id, amount, "Pending Sync",
                pkg_json, now, verification_time_ms
            ))

        return {
            "accepted": True,
            "token_id": token_id,
            "payer_id": payer_id,
            "payee_id": payee_id,
            "amount": amount,
            "status": "Pending Sync",
            "verification_time_ms": verification_time_ms,
            "within_nfr_latency": verification_latency_sec <= 1.0,
            "message": f"Payment of ₹{amount:.2f} verified offline in {verification_time_ms:.2f}ms. Queued as 'Pending Sync'."
        }

    def get_pending_sync_tokens(self) -> List[Dict[str, Any]]:
        """Returns all tokens currently in 'Pending Sync' status from persistent storage."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT token_pkg_json FROM local_queue WHERE status = 'Pending Sync' ORDER BY received_at ASC")
        rows = cursor.fetchall()
        return [json.loads(row["token_pkg_json"]) for row in rows]

    def mark_tokens_synced(self, settled_token_ids: List[str]) -> None:
        """
        Updates status of confirmed settled tokens to 'Settled'.
        Only records explicitly settled by the backend are marked as 'Settled'.
        """
        if not settled_token_ids:
            return
        with self.conn:
            self.conn.executemany(
                "UPDATE local_queue SET status = 'Settled' WHERE token_id = ?",
                [(tid,) for tid in settled_token_ids]
            )

    def remove_synced_tokens(self) -> int:
        """Purges settled tokens from the persistent queue, returning count of removed entries."""
        with self.conn:
            cursor = self.conn.execute("DELETE FROM local_queue WHERE status = 'Settled'")
            return cursor.rowcount

    def summary(self) -> Dict[str, Any]:
        """Provides a status breakdown from the persistent database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS total, SUM(amount) AS total_amt FROM local_queue")
        row_all = cursor.fetchone()
        total_queued = row_all["total"] or 0

        cursor.execute("SELECT COUNT(*) AS pending, SUM(amount) AS pending_amt FROM local_queue WHERE status = 'Pending Sync'")
        row_pending = cursor.fetchone()
        pending = row_pending["pending"] or 0
        pending_amt = row_pending["pending_amt"] or 0.0

        cursor.execute("SELECT COUNT(*) AS settled FROM local_queue WHERE status = 'Settled'")
        settled = cursor.fetchone()["settled"] or 0

        return {
            "merchant_id": self.merchant_id,
            "total_queued": total_queued,
            "pending_sync": pending,
            "settled": settled,
            "pending_sync_amount": round(float(pending_amt), 2)
        }

    def close(self) -> None:
        """Closes SQLite connection."""
        self.conn.close()

    def reset_queue(self) -> None:
        """Purges local queue records (useful for test resets)."""
        with self.conn:
            self.conn.execute("DELETE FROM local_queue;")

    def delete_db(self) -> None:
        """Closes and removes local queue database file."""
        self.close()
        if os.path.exists(self.queue_db_path):
            try:
                os.remove(self.queue_db_path)
            except Exception:
                pass
