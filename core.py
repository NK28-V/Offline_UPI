import base64, json, sqlite3, time, uuid
from typing import Any, Dict, List, Optional, Tuple
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# --- Crypto: RSA-2048, RSA-PSS SHA-256, Canonical JSON ---

def generate_keypair() -> Tuple[str, str]:
    priv = rsa.generate_private_key(65537, 2048)
    priv_pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub_pem = priv.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv_pem, pub_pem

def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sign_data(data: Any, priv_pem: str) -> str:
    priv = serialization.load_pem_private_key(priv_pem.encode(), password=None)
    sig = priv.sign(canonical_json(data), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
    return base64.b64encode(sig).decode()

def verify_sig(data: Any, sig_b64: str, pub_pem: str) -> bool:
    try:
        pub = serialization.load_pem_public_key(pub_pem.encode())
        pub.verify(base64.b64decode(sig_b64.strip()), canonical_json(data), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH), hashes.SHA256())
        return True
    except Exception:
        return False

# --- Payer: Offline Wallet, Cap = 50,000 Paise (₹500) ---

class Payer:
    OFFLINE_CAP_PAISE = 50_000

    def __init__(self, db_path: str = "payer.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS tokens (token_id TEXT PRIMARY KEY, payee_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, token_json TEXT NOT NULL)")

    def get_info(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM config")
        cfg = {r["key"]: r["value"] for r in cur.fetchall()}
        if "payer_id" not in cfg:
            return {"registered": False, "cap_paise": self.OFFLINE_CAP_PAISE}
        cur.execute("SELECT SUM(amount_paise) FROM tokens WHERE status = 'CREATED'")
        spent = cur.fetchone()[0] or 0
        cur.execute("SELECT * FROM tokens ORDER BY created_at DESC")
        tokens = [dict(r) for r in cur.fetchall()]
        return {"registered": True, "payer_id": cfg["payer_id"], "bank_url": cfg.get("bank_url"), "cap_paise": self.OFFLINE_CAP_PAISE, "spent_paise": spent, "remaining_paise": max(0, self.OFFLINE_CAP_PAISE - spent), "tokens": tokens}

    def register(self, payer_id: str, bank_url: str, bank_pub_key: str) -> str:
        priv_pem, pub_pem = generate_keypair()
        with self.conn:
            for k, v in [('payer_id', payer_id.strip()), ('bank_url', bank_url.rstrip("/")), ('private_key', priv_pem), ('public_key', pub_pem), ('bank_public_key', bank_pub_key.strip())]:
                self.conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, v))
        return pub_pem

    def create_token(self, payee_id: str, amount_paise: int) -> Dict[str, Any]:
        info = self.get_info()
        if not info["registered"]: raise ValueError("Payer wallet not initialized.")
        if amount_paise <= 0: raise ValueError("Amount must be greater than zero.")
        if info["spent_paise"] + amount_paise > self.OFFLINE_CAP_PAISE:
            raise ValueError(f"Offline cap exceeded. Limit is ₹500. Remaining: ₹{info['remaining_paise']/100:.2f}.")

        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'private_key'")
        priv_pem = cur.fetchone()["value"]

        payload = {"tokenID": str(uuid.uuid4()), "payerID": info["payer_id"], "payeeID": payee_id.strip(), "amount_paise": int(amount_paise), "timestamp": int(time.time())}
        pkg = {"payload": payload, "signature": sign_data(payload, priv_pem)}
        with self.conn:
            self.conn.execute("INSERT INTO tokens VALUES (?, ?, ?, 'CREATED', ?, ?)", (payload["tokenID"], payload["payeeID"], payload["amount_paise"], payload["timestamp"], json.dumps(pkg)))
        return pkg

    def cancel_token(self, token_id: str) -> bool:
        with self.conn:
            cur = self.conn.cursor()
            cur.execute("SELECT status FROM tokens WHERE token_id = ?", (token_id,))
            row = cur.fetchone()
            if not row or row["status"] != "CREATED": return False
            cur.execute("UPDATE tokens SET status = 'CANCELLED' WHERE token_id = ?", (token_id,))
            return True

    def reconcile(self, statement: Dict[str, Any]) -> int:
        payload, sig = statement.get("payload", {}), statement.get("signature", "")
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'bank_public_key'")
        row = cur.fetchone()
        if not row or not verify_sig(payload, sig, row["value"]):
            raise ValueError("Invalid bank signature on settlement reconciliation.")
        updated = 0
        with self.conn:
            for tid in payload.get("settled_token_ids", []):
                res = self.conn.execute("UPDATE tokens SET status = 'SETTLED' WHERE token_id = ? AND status = 'CREATED'", (tid,))
                updated += res.rowcount
        return updated

# --- Merchant POS: Offline Queue & Local Public Key Cache ---

class Merchant:
    def __init__(self, db_path: str = "merchant.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS trusted_payers (payer_id TEXT PRIMARY KEY, public_key_pem TEXT NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS queue (token_id TEXT PRIMARY KEY, payer_id TEXT NOT NULL, payee_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT, received_at INTEGER NOT NULL, token_pkg_json TEXT NOT NULL)")

    def get_info(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM config")
        cfg = {r["key"]: r["value"] for r in cur.fetchall()}
        cur.execute("SELECT COUNT(*) FROM trusted_payers")
        p_count = cur.fetchone()[0]
        cur.execute("SELECT * FROM queue ORDER BY received_at DESC")
        return {"registered": "merchant_id" in cfg, "merchant_id": cfg.get("merchant_id"), "bank_url": cfg.get("bank_url"), "merchant_url": cfg.get("merchant_url"), "trusted_payers_count": p_count, "queue": [dict(r) for r in cur.fetchall()]}

    def register(self, merchant_id: str, bank_url: str, merchant_url: str, api_key: str):
        with self.conn:
            for k, v in [('merchant_id', merchant_id.strip()), ('bank_url', bank_url.rstrip("/")), ('merchant_url', merchant_url.rstrip("/")), ('api_key', api_key.strip())]:
                self.conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, v))

    def update_trusted_payers(self, payers: List[Dict[str, str]]):
        with self.conn:
            for p in payers:
                self.conn.execute("INSERT OR REPLACE INTO trusted_payers VALUES (?, ?)", (p["payer_id"], p["public_key_pem"]))

    def verify_and_queue(self, token_pkg: Dict[str, Any]) -> Tuple[bool, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'merchant_id'")
        m_row = cur.fetchone()
        if not m_row: return False, "Merchant shop is not initialized."
        merchant_id = m_row["value"]

        payload = token_pkg.get("payload") if isinstance(token_pkg, dict) else None
        sig = token_pkg.get("signature") if isinstance(token_pkg, dict) else None
        if not payload or not sig: return False, "Invalid token package format."

        tid, payer_id, payee_id, amt = payload.get("tokenID"), payload.get("payerID"), payload.get("payeeID"), payload.get("amount_paise")
        if not tid or not payer_id or amt is None: return False, "Missing required token payload fields."
        if amt <= 0: return False, "Token amount must be strictly positive."
        if payee_id != merchant_id: return False, f"Payee mismatch: Token intended for '{payee_id}', not '{merchant_id}'."

        cur.execute("SELECT token_id FROM queue WHERE token_id = ?", (tid,))
        if cur.fetchone(): return False, f"Duplicate token: '{tid}' has already been accepted."

        cur.execute("SELECT public_key_pem FROM trusted_payers WHERE payer_id = ?", (payer_id,))
        p_row = cur.fetchone()
        if not p_row: return False, f"Unknown payer '{payer_id}': not in merchant's trusted local cache."
        if not verify_sig(payload, sig, p_row["public_key_pem"]): return False, "Cryptographic signature verification failed."

        with self.conn:
            self.conn.execute("INSERT INTO queue VALUES (?, ?, ?, ?, 'PENDING', 'Awaiting bank sync', ?, ?)", (tid, payer_id, payee_id, amt, int(time.time()), json.dumps(token_pkg)))
        return True, "Payment verified and queued offline."

    def get_pending_batch(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_pkg_json FROM queue WHERE status = 'PENDING' ORDER BY received_at ASC")
        return [json.loads(r["token_pkg_json"]) for r in cur.fetchall()]

    def apply_sync_results(self, results: List[Dict[str, Any]]):
        with self.conn:
            for item in results:
                self.conn.execute("UPDATE queue SET status = ?, reason = ? WHERE token_id = ?", (item["status"], item.get("reason", ""), item["token_id"]))

# --- Bank: Authoritative Ledger, Atomic Savepoints, Signatures ---

class Bank:
    def __init__(self, db_path: str = "bank.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS keys (key_name TEXT PRIMARY KEY, key_val TEXT NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS payers (payer_id TEXT PRIMARY KEY, public_key_pem TEXT NOT NULL, registered_at INTEGER NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS merchants (merchant_id TEXT PRIMARY KEY, api_key TEXT NOT NULL, merchant_url TEXT NOT NULL, registered_at INTEGER NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS ledger (token_id TEXT PRIMARY KEY, payer_id TEXT NOT NULL, payee_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL, reason TEXT, token_timestamp INTEGER NOT NULL, settled_at INTEGER NOT NULL)")
        cur = self.conn.cursor()
        cur.execute("SELECT key_val FROM keys WHERE key_name = 'private_key'")
        if not cur.fetchone():
            priv_pem, pub_pem = generate_keypair()
            with self.conn:
                self.conn.execute("INSERT INTO keys VALUES ('private_key', ?), ('public_key', ?)", (priv_pem, pub_pem))

    def get_public_key(self) -> str:
        cur = self.conn.cursor()
        cur.execute("SELECT key_val FROM keys WHERE key_name = 'public_key'")
        return cur.fetchone()["key_val"]

    def register_payer(self, payer_id: str, public_key_pem: str):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO payers VALUES (?, ?, ?)", (payer_id.strip(), public_key_pem.strip(), int(time.time())))

    def register_merchant(self, merchant_id: str, merchant_url: str, api_key: str):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO merchants VALUES (?, ?, ?, ?)", (merchant_id.strip(), api_key.strip(), merchant_url.rstrip("/"), int(time.time())))

    def verify_merchant(self, merchant_id: str, api_key: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT api_key FROM merchants WHERE merchant_id = ?", (merchant_id.strip(),))
        row = cur.fetchone()
        return bool(row and row["api_key"] == api_key.strip())

    def get_trusted_payers(self) -> List[Dict[str, str]]:
        cur = self.conn.cursor()
        cur.execute("SELECT payer_id, public_key_pem FROM payers ORDER BY registered_at ASC")
        return [dict(r) for r in cur.fetchall()]

    def settle_batch(self, merchant_id: str, batch: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        results, now, cur = [], int(time.time()), self.conn.cursor()
        for idx, pkg in enumerate(batch):
            payload, sig = pkg.get("payload", {}), pkg.get("signature", "")
            tid, payer_id, payee_id = payload.get("tokenID"), payload.get("payerID"), payload.get("payeeID")
            amt, t_time = payload.get("amount_paise", 0), payload.get("timestamp", now)

            if not tid or not payer_id or amt <= 0 or payee_id != merchant_id:
                results.append({"token_id": tid or f"pkg_{idx}", "status": "REJECTED", "reason": "Malformed payload or payee mismatch"})
                continue

            cur.execute("SELECT public_key_pem FROM payers WHERE payer_id = ?", (payer_id,))
            p_row = cur.fetchone()
            if not p_row or not verify_sig(payload, sig, p_row["public_key_pem"]):
                results.append({"token_id": tid, "status": "REJECTED", "reason": "Unknown payer or invalid signature"})
                continue

            status, reason = ("FLAGGED", "Token older than 24 hours") if (now - t_time > 86400) else ("SETTLED", "Settlement completed")
            sp = f"sp_{idx}"
            cur.execute(f"SAVEPOINT {sp}")
            try:
                cur.execute("INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (tid, payer_id, payee_id, amt, status, reason, t_time, now))
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                self.conn.commit()
                results.append({"token_id": tid, "status": status, "reason": reason})
            except sqlite3.IntegrityError:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                self.conn.commit()
                results.append({"token_id": tid, "status": "DUPLICATE", "reason": "Double-spend attempt: Token already settled"})
        return results

    def get_payer_settlement_statement(self, payer_id: str) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_id FROM ledger WHERE payer_id = ? AND status = 'SETTLED'", (payer_id,))
        payload = {"payer_id": payer_id, "settled_token_ids": [r["token_id"] for r in cur.fetchall()], "timestamp": int(time.time())}
        cur.execute("SELECT key_val FROM keys WHERE key_name = 'private_key'")
        return {"payload": payload, "signature": sign_data(payload, cur.fetchone()["key_val"])}

    def get_stats(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*), SUM(CASE WHEN status='SETTLED' THEN amount_paise ELSE 0 END), SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END), SUM(CASE WHEN status='DUPLICATE' THEN 1 ELSE 0 END), SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END) FROM ledger")
        tot, amt, set_c, dup_c, rej_c = cur.fetchone()
        cur.execute("SELECT * FROM ledger ORDER BY settled_at DESC")
        ledger = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT payer_id, registered_at FROM payers ORDER BY registered_at DESC")
        payers = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT merchant_id, merchant_url, registered_at FROM merchants ORDER BY registered_at DESC")
        merchants = [dict(r) for r in cur.fetchall()]
        return {"total_received": tot or 0, "settled_count": set_c or 0, "settled_amount_paise": amt or 0, "duplicate_count": dup_c or 0, "rejected_count": rej_c or 0, "ledger": ledger, "payers": payers, "merchants": merchants}
