"""
Backend Server Module for Offline UPI Payment System
Maintains a SQLite database for central settlement, enforces a trusted payer public key registry,
strict payee binding, atomic transactions with savepoints and IntegrityError handling for 100% duplicate detection,
and 24-hour token aging checks.
"""

import sqlite3
import time
import uuid
import os
from typing import Dict, Any, List, Optional
from crypto_utils import load_public_key_from_pem, verify_signature, validate_token_payload


class BackendServer:
    """
    Simulates the central UPI Switch / Banking Core backend.
    Enforces trusted public key registry, atomic duplicate detection with SQLite IntegrityError handling,
    payee binding, and 24-hour token aging checks.
    """

    MAX_TOKEN_AGE_SECONDS = 86400.0  # 24 Hours

    def __init__(self, db_path: str = "offline_upi_backend.db", trusted_registry: Optional[Dict[str, str]] = None):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._initialize_schema()

        if trusted_registry:
            for pid, pem in trusted_registry.items():
                self.register_payer(pid, pem)

    def _initialize_schema(self) -> None:
        """Initializes tables for settled tokens, trusted payers, and batch audit logs."""
        with self.conn:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS settled_tokens (
                    token_id TEXT PRIMARY KEY,
                    payer_id TEXT NOT NULL,
                    payee_id TEXT NOT NULL,
                    amount REAL NOT NULL,
                    token_timestamp REAL NOT NULL,
                    token_age_hours REAL NOT NULL,
                    status TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    review_reason TEXT,
                    settled_at REAL NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS trusted_payers (
                    payer_id TEXT PRIMARY KEY,
                    public_key_pem TEXT NOT NULL,
                    registered_at REAL NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS sync_audit_log (
                    batch_id TEXT PRIMARY KEY,
                    merchant_id TEXT NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    settled_count INTEGER NOT NULL,
                    duplicate_count INTEGER NOT NULL,
                    flagged_count INTEGER NOT NULL,
                    rejected_count INTEGER NOT NULL,
                    processed_at REAL NOT NULL
                );
            """)

    def register_payer(self, payer_id: str, public_key_pem: str) -> None:
        """Registers or updates a trusted payer's public key in the central ledger."""
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO trusted_payers (payer_id, public_key_pem, registered_at)
                VALUES (?, ?, ?)
            """, (payer_id.strip(), public_key_pem.strip(), time.time()))

    def get_trusted_payer_key(self, payer_id: str) -> Optional[str]:
        """Retrieves registered public key for a payer ID from SQLite."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT public_key_pem FROM trusted_payers WHERE payer_id = ?", (payer_id.strip(),))
        row = cursor.fetchone()
        return row["public_key_pem"] if row else None

    def sync_batch(self, merchant_id: str, tokens: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Synchronizes a batch of offline payment tokens submitted by a merchant.
        
        Performs:
        1. Strict Input Validation (structure, types, positive finite amounts, non-future timestamps).
        2. Payee Binding Check (payeeID == submitting merchant_id).
        3. Trusted Payer Registry Lookup (rejects unknown payers; never trusts sent key alone).
        4. Cryptographic Signature Verification using trusted key.
        5. Double-Spend & Duplicate Detection:
           - In-batch duplicate checks.
           - SQLite atomic insertion protected with SAVEPOINTs catching sqlite3.IntegrityError.
        6. Age Check: Tokens > 24 hours old are recorded as FLAGGED_FOR_REVIEW.
        
        Returns structured batch results with explicit counts and details.
        """
        batch_id = str(uuid.uuid4())
        current_time = time.time()
        start_perf = time.perf_counter()

        settled: List[Dict[str, Any]] = []
        duplicates: List[Dict[str, Any]] = []
        flagged_for_review: List[Dict[str, Any]] = []
        rejected_invalid: List[Dict[str, Any]] = []

        seen_in_current_batch = set()
        cursor = self.conn.cursor()

        # Wrap batch processing in a database transaction
        with self.conn:
            for idx, token_pkg in enumerate(tokens):
                if not isinstance(token_pkg, dict):
                    rejected_invalid.append({
                        "token_id": None,
                        "reason": "Token package must be a dictionary."
                    })
                    continue

                payload = token_pkg.get("payload")
                signature = token_pkg.get("signature")

                # 1. Strict Input Validation
                is_valid, validation_err = validate_token_payload(payload)
                if not is_valid:
                    token_id = payload.get("tokenID") if isinstance(payload, dict) else None
                    rejected_invalid.append({
                        "token_id": token_id,
                        "reason": f"Input validation failed: {validation_err}"
                    })
                    continue

                token_id = payload["tokenID"]
                payer_id = payload["payerID"]
                payee_id = payload["payeeID"]
                amount = payload["amount"]
                token_timestamp = payload["timestamp"]

                # 2. Payee Binding Check
                if payee_id != merchant_id:
                    rejected_invalid.append({
                        "token_id": token_id,
                        "reason": f"Payee mismatch: token intended for '{payee_id}', but submitted by merchant '{merchant_id}'."
                    })
                    continue

                # 3. In-batch Duplicate Check
                if token_id in seen_in_current_batch:
                    duplicates.append({
                        "token_id": token_id,
                        "payer_id": payer_id,
                        "amount": amount,
                        "reason": f"Double-spend detected: duplicate token ID '{token_id}' within the same batch."
                    })
                    continue

                seen_in_current_batch.add(token_id)

                # 4. Trusted Payer Registry Lookup
                trusted_pub_pem = self.get_trusted_payer_key(payer_id)
                if not trusted_pub_pem:
                    rejected_invalid.append({
                        "token_id": token_id,
                        "reason": f"Unknown payer identity '{payer_id}': not registered in central backend."
                    })
                    continue

                # 5. Cryptographic Signature Verification using Trusted Registry Key
                try:
                    trusted_pub_key = load_public_key_from_pem(trusted_pub_pem)
                    if not verify_signature(payload, signature, trusted_pub_key):
                        rejected_invalid.append({
                            "token_id": token_id,
                            "reason": "Cryptographic signature verification failed against trusted public key."
                        })
                        continue
                except Exception as e:
                    rejected_invalid.append({
                        "token_id": token_id,
                        "reason": f"Signature verification error: {str(e)}"
                    })
                    continue

                # 6. Check Token Age (24-Hour Expiry Rule)
                token_age_seconds = current_time - token_timestamp
                token_age_hours = round(token_age_seconds / 3600.0, 2)

                if token_age_seconds > self.MAX_TOKEN_AGE_SECONDS:
                    status = "FLAGGED_FOR_REVIEW"
                    review_reason = f"Token age ({token_age_hours:.1f}h) exceeds 24h threshold. Flagged for manual audit."
                else:
                    status = "SETTLED"
                    review_reason = None

                # 7. Atomic Insert with Savepoint & IntegrityError Handling
                sp_name = f"sp_{idx}"
                cursor.execute(f"SAVEPOINT {sp_name}")
                try:
                    cursor.execute("""
                        INSERT INTO settled_tokens (
                            token_id, payer_id, payee_id, amount, token_timestamp,
                            token_age_hours, status, signature, review_reason, settled_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        token_id, payer_id, payee_id, amount, token_timestamp,
                        token_age_hours, status, signature, review_reason, current_time
                    ))
                    cursor.execute(f"RELEASE SAVEPOINT {sp_name}")

                    if status == "FLAGGED_FOR_REVIEW":
                        flagged_for_review.append({
                            "token_id": token_id,
                            "payer_id": payer_id,
                            "amount": amount,
                            "age_hours": token_age_hours,
                            "status": status,
                            "review_reason": review_reason
                        })
                    else:
                        settled.append({
                            "token_id": token_id,
                            "payer_id": payer_id,
                            "amount": amount,
                            "status": status
                        })

                except sqlite3.IntegrityError:
                    cursor.execute(f"ROLLBACK TO SAVEPOINT {sp_name}")
                    cursor.execute(f"RELEASE SAVEPOINT {sp_name}")
                    duplicates.append({
                        "token_id": token_id,
                        "payer_id": payer_id,
                        "amount": amount,
                        "reason": f"Double-spend detected (IntegrityError: duplicate token ID '{token_id}')."
                    })

            # Record batch audit log
            self.conn.execute("""
                INSERT INTO sync_audit_log (
                    batch_id, merchant_id, total_tokens, settled_count, duplicate_count,
                    flagged_count, rejected_count, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                batch_id,
                merchant_id,
                len(tokens),
                len(settled),
                len(duplicates),
                len(flagged_for_review),
                len(rejected_invalid),
                current_time
            ))

        elapsed_ms = (time.perf_counter() - start_perf) * 1000.0

        return {
            "batch_id": batch_id,
            "merchant_id": merchant_id,
            "total_processed": len(tokens),
            "settled_count": len(settled),
            "duplicate_count": len(duplicates),
            "flagged_count": len(flagged_for_review),
            "rejected_count": len(rejected_invalid),
            "processing_time_ms": round(elapsed_ms, 2),
            "settled": settled,
            "duplicates": duplicates,
            "flagged_for_review": flagged_for_review,
            "rejected_invalid": rejected_invalid
        }

    def get_token_record(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves a single token record from the database."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM settled_tokens WHERE token_id = ?", (token_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

    def get_all_settled_tokens(self) -> List[Dict[str, Any]]:
        """Returns all persisted tokens."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM settled_tokens ORDER BY settled_at DESC")
        return [dict(row) for row in cursor.fetchall()]

    def reset_database(self) -> None:
        """Purges all records from tables for a fresh simulation run."""
        with self.conn:
            self.conn.execute("DELETE FROM settled_tokens;")
            self.conn.execute("DELETE FROM sync_audit_log;")
            self.conn.execute("DELETE FROM trusted_payers;")

    def close(self) -> None:
        """Closes the SQLite database connection."""
        self.conn.close()

    def delete_db(self) -> None:
        """Closes and removes database file."""
        self.close()
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass
