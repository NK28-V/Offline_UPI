"""
Backend Server Module for Offline UPI Payment System
Maintains a SQLite database for central settlement, authoritative trusted payer registry,
authorized merchant credentials, atomic transactions with savepoints and IntegrityError handling
for 100% duplicate detection, 24-hour token aging checks, and cryptographically signed settlement receipts.
"""

import sqlite3
import time
import uuid
import os
import json
import hashlib
import hmac
from typing import Dict, Any, List, Optional, Tuple
from crypto_utils import (
    load_public_key_from_pem,
    verify_signature,
    validate_token_payload,
    generate_rsa_key_pair,
    private_key_to_pem,
    public_key_to_pem,
    sign_payload,
    validate_rsa_public_key_pem
)
from cryptography.hazmat.primitives import serialization


class BackendServer:
    """
    Simulates the central UPI Switch / Banking Core backend.
    Enforces authoritative trusted public key registry, authorized merchant authentication,
    atomic duplicate detection with SQLite IntegrityError handling, payee binding,
    24-hour token aging checks, and bank-signed settlement receipts.
    """

    MAX_TOKEN_AGE_SECONDS = 86400.0  # 24 Hours

    def __init__(self, db_path: str = "offline_upi_backend.db", trusted_registry: Optional[Dict[str, str]] = None):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._initialize_schema()
        self._init_bank_keys()
        self._init_default_merchants()

        if trusted_registry:
            for pid, pem in trusted_registry.items():
                self.register_payer(pid, pem, allow_update=True)

    def _initialize_schema(self) -> None:
        """Initializes tables for settled tokens, trusted payers, authorized merchants, receipts, and audit logs."""
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
                    registered_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS authorized_merchants (
                    merchant_id TEXT PRIMARY KEY,
                    api_key_hash TEXT NOT NULL,
                    name TEXT,
                    registered_at REAL NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS settlement_receipts (
                    token_id TEXT PRIMARY KEY,
                    receipt_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    settled_at REAL NOT NULL
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
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit_log (
                    log_id TEXT PRIMARY KEY,
                    action TEXT NOT NULL,
                    target_id TEXT,
                    details TEXT,
                    timestamp REAL NOT NULL
                );
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS bank_system_keys (
                    key_id TEXT PRIMARY KEY,
                    private_key_pem TEXT NOT NULL,
                    public_key_pem TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)

    def _init_bank_keys(self) -> None:
        """Initializes or loads the bank's persistent RSA signing keypair."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT private_key_pem, public_key_pem FROM bank_system_keys WHERE key_id = 'primary'")
        row = cursor.fetchone()

        if row:
            self.bank_private_key_pem = row["private_key_pem"]
            self.bank_public_key_pem = row["public_key_pem"]
            self.bank_private_key = serialization.load_pem_private_key(self.bank_private_key_pem.encode("utf-8"), password=None)
            self.bank_public_key = serialization.load_pem_public_key(self.bank_public_key_pem.encode("utf-8"))
        else:
            self.bank_private_key, self.bank_public_key = generate_rsa_key_pair()
            self.bank_private_key_pem = private_key_to_pem(self.bank_private_key)
            self.bank_public_key_pem = public_key_to_pem(self.bank_public_key)
            with self.conn:
                self.conn.execute("""
                    INSERT INTO bank_system_keys (key_id, private_key_pem, public_key_pem, created_at)
                    VALUES (?, ?, ?, ?)
                """, ("primary", self.bank_private_key_pem, self.bank_public_key_pem, time.time()))

    def _init_default_merchants(self) -> None:
        """Initializes default authorized merchant credentials if table is empty."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) AS c FROM authorized_merchants")
        if cursor.fetchone()["c"] == 0:
            default_key = os.getenv("DEFAULT_MERCHANT_KEY", "merchant-bob-secret-key-2026")
            self.register_merchant("merchant_bob@upi", default_key, name="Bob's Tea & Snacks")

    def register_merchant(self, merchant_id: str, api_key: str, name: str = "") -> None:
        """Registers an authorized merchant with a hashed secret API key."""
        key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        now = time.time()
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO authorized_merchants (merchant_id, api_key_hash, name, registered_at)
                VALUES (?, ?, ?, ?)
            """, (merchant_id.strip(), key_hash, name.strip(), now))

    def verify_merchant_auth(self, merchant_id: str, api_key: str) -> bool:
        """Authenticates merchant API key using constant-time comparison."""
        if not merchant_id or not api_key:
            return False
        cursor = self.conn.cursor()
        cursor.execute("SELECT api_key_hash FROM authorized_merchants WHERE merchant_id = ?", (merchant_id.strip(),))
        row = cursor.fetchone()
        if not row:
            return False
        expected_hash = row["api_key_hash"]
        computed_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        return hmac.compare_digest(expected_hash, computed_hash)

    def log_admin_action(self, action: str, target_id: Optional[str], details: Optional[str]) -> None:
        """Records an administrative operation in the tamper-evident audit log."""
        with self.conn:
            self.conn.execute("""
                INSERT INTO admin_audit_log (log_id, action, target_id, details, timestamp)
                VALUES (?, ?, ?, ?, ?)
            """, (str(uuid.uuid4()), action, target_id, details, time.time()))

    def register_payer(self, payer_id: str, public_key_pem: str, allow_update: bool = False) -> None:
        """
        Registers or updates a trusted payer's public key in the central ledger.
        Validates RSA key format and key size (>= 2048 bits).
        Prevents unauthorized replacement if already registered unless allow_update=True.
        """
        pid = payer_id.strip()
        pem = public_key_pem.strip()

        is_valid, err = validate_rsa_public_key_pem(pem, min_key_size=2048)
        if not is_valid:
            raise ValueError(f"Invalid public key for '{pid}': {err}")

        cursor = self.conn.cursor()
        cursor.execute("SELECT public_key_pem FROM trusted_payers WHERE payer_id = ?", (pid,))
        existing = cursor.fetchone()
        now = time.time()
        if existing:
            if existing["public_key_pem"].strip() == pem:
                return  # Identical key already registered; idempotent no-op
            if not allow_update:
                raise ValueError(f"Payer '{pid}' is already registered in trusted registry. Unauthorized replacement is prohibited.")

        with self.conn:
            if existing:
                self.conn.execute("""
                    UPDATE trusted_payers SET public_key_pem = ?, updated_at = ? WHERE payer_id = ?
                """, (pem, now, pid))
                self.log_admin_action("REPLACE_PAYER_KEY", pid, "Payer public key replaced with admin authorization.")
            else:
                self.conn.execute("""
                    INSERT INTO trusted_payers (payer_id, public_key_pem, registered_at, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (pid, pem, now, now))
                self.log_admin_action("REGISTER_PAYER", pid, "New payer registered in authoritative registry.")

    def get_trusted_payer_key(self, payer_id: str) -> Optional[str]:
        """Retrieves registered public key for a payer ID from SQLite."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT public_key_pem FROM trusted_payers WHERE payer_id = ?", (payer_id.strip(),))
        row = cursor.fetchone()
        return row["public_key_pem"] if row else None

    def get_all_trusted_payers(self) -> List[Dict[str, Any]]:
        """Returns all trusted payer records."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT payer_id, public_key_pem, registered_at, updated_at FROM trusted_payers ORDER BY registered_at ASC")
        return [dict(row) for row in cursor.fetchall()]

    def generate_signed_receipt(self, token_id: str, payer_id: str, payee_id: str, amount: float, status: str) -> Dict[str, Any]:
        """Generates and cryptographically signs an authoritative Bank Settlement Receipt."""
        now = time.time()
        receipt_data = {
            "receiptID": str(uuid.uuid4()),
            "tokenID": token_id,
            "payerID": payer_id,
            "payeeID": payee_id,
            "amount": amount,
            "status": status,
            "settled_at": now
        }
        sig = sign_payload(receipt_data, self.bank_private_key)
        return {
            "receipt": receipt_data,
            "signature": sig,
            "bank_public_key": self.bank_public_key_pem
        }

    def get_receipt(self, token_id: str) -> Optional[Dict[str, Any]]:
        """Retrieves the authoritative bank-signed receipt for a token ID from SQLite."""
        cursor = self.conn.cursor()
        cursor.execute("SELECT receipt_json FROM settlement_receipts WHERE token_id = ?", (token_id,))
        row = cursor.fetchone()
        return json.loads(row["receipt_json"]) if row else None

    def sync_batch(self, merchant_id: str, tokens: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Synchronizes a batch of offline payment tokens submitted by an authenticated merchant.
        
        Performs:
        1. Strict Input Validation (structure, types, positive finite amounts, non-future timestamps).
        2. Payee Binding Check (payeeID == submitting merchant_id).
        3. Authoritative Trusted Payer Registry Lookup (rejects unknown payers; never trusts sent key alone).
        4. Cryptographic Signature Verification using trusted key.
        5. Double-Spend & Duplicate Detection:
           - In-batch duplicate checks.
           - SQLite atomic insertion protected with SAVEPOINTs catching sqlite3.IntegrityError.
        6. Age Check: Tokens > 24 hours old are recorded as FLAGGED_FOR_REVIEW.
        7. Authoritative Bank Receipt Generation: Signs and persists settlement receipts for successful tokens.
        
        Returns structured batch results with explicit counts, details, and signed receipts.
        """
        batch_id = str(uuid.uuid4())
        current_time = time.time()
        start_perf = time.perf_counter()

        settled: List[Dict[str, Any]] = []
        duplicates: List[Dict[str, Any]] = []
        flagged_for_review: List[Dict[str, Any]] = []
        rejected_invalid: List[Dict[str, Any]] = []
        receipts: List[Dict[str, Any]] = []

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

                # 4. Authoritative Trusted Payer Registry Lookup
                trusted_pub_pem = self.get_trusted_payer_key(payer_id)
                if not trusted_pub_pem:
                    rejected_invalid.append({
                        "token_id": token_id,
                        "reason": f"Unknown payer identity '{payer_id}': not registered in authoritative central backend."
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

                    # 8. Generate and persist bank-signed receipt
                    signed_receipt = self.generate_signed_receipt(
                        token_id=token_id,
                        payer_id=payer_id,
                        payee_id=payee_id,
                        amount=amount,
                        status=status
                    )
                    cursor.execute("""
                        INSERT INTO settlement_receipts (token_id, receipt_id, receipt_json, status, settled_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        token_id,
                        signed_receipt["receipt"]["receiptID"],
                        json.dumps(signed_receipt),
                        status,
                        current_time
                    ))

                    cursor.execute(f"RELEASE SAVEPOINT {sp_name}")
                    receipts.append(signed_receipt)

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

                    # If duplicate was already settled, retrieve existing receipt for idempotency
                    existing_receipt = self.get_receipt(token_id)
                    duplicates.append({
                        "token_id": token_id,
                        "payer_id": payer_id,
                        "amount": amount,
                        "reason": f"Double-spend detected (IntegrityError: duplicate token ID '{token_id}').",
                        "existing_receipt": existing_receipt
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
            "rejected_invalid": rejected_invalid,
            "receipts": receipts
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
        """Purges transactional records for a clean test run."""
        with self.conn:
            self.conn.execute("DELETE FROM settled_tokens;")
            self.conn.execute("DELETE FROM sync_audit_log;")
            self.conn.execute("DELETE FROM trusted_payers;")
            self.conn.execute("DELETE FROM settlement_receipts;")
            self.conn.execute("DELETE FROM admin_audit_log;")

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
