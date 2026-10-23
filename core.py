import base64, json, sqlite3, time, uuid
from typing import Any, Dict, List, Optional, Tuple
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

OFFLINE_CAP_PAISE = 50_000      # ₹500 offline spending cap
MAX_CLOCK_SKEW_S = 300          # tokens dated >5 min in the future are rejected

# Two non-overlapping time windows per token (counted from the token's timestamp):
#   0 .. LOCK_S      MERCHANT WINDOW: only the merchant can settle; payer reclaim is refused.
#   after LOCK_S     PAYER WINDOW:    only an unclaimed token can be reclaimed by the payer.
# So a merchant who syncs within LOCK_S is ALWAYS paid. server.py can shorten LOCK_S for demos.
LOCK_S = 48 * 3600

def accept_age_s() -> int:
    """Merchant only accepts tokens younger than half the lock, so it always has >= LOCK_S/2 to sync."""
    return LOCK_S // 2

def fmt_time(ts: int) -> str:
    return time.strftime("%d %b %Y, %H:%M:%S", time.localtime(ts))

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

def is_valid_public_key(pem: Any) -> bool:
    try:
        serialization.load_pem_public_key(pem.encode())
        return True
    except Exception:
        return False

def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)

def _is_id(v: Any) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= 100

def validate_payload(payload: Any, now: Optional[int] = None) -> Optional[str]:
    """Shared input check used by Merchant and Bank. Returns an error message, or None if OK."""
    now = int(time.time()) if now is None else now
    if not isinstance(payload, dict): return "Payload must be a JSON object."
    for f in ("tokenID", "payerID", "payeeID"):
        if not _is_id(payload.get(f)): return f"Field '{f}' missing or invalid."
    amt, ts = payload.get("amount_paise"), payload.get("timestamp")
    if not _is_int(amt): return "amount_paise must be a whole number of paise."
    if amt <= 0: return "Token amount must be strictly positive."
    if amt > OFFLINE_CAP_PAISE: return "Token amount exceeds the ₹500 offline cap."
    if not _is_int(ts): return "timestamp must be an integer (unix seconds)."
    if ts > now + MAX_CLOCK_SKEW_S: return "Token timestamp is in the future."
    return None

# --- Payer: Offline Wallet, Cap = 50,000 Paise (₹500) ---
# Token states: CREATED -> SETTLED                    (merchant synced within its window)
#               CREATED -> CANCEL_PENDING -> CANCELLED (payer reclaimed an UNUSED token after the lock)
#               CREATED/SETTLED -> REFUNDED            (merchant chose to refund)
# CREATED and CANCEL_PENDING both still count as SPENT. Allowance is only freed by a bank-signed answer.

SPENT_STATES = ("CREATED", "CANCEL_PENDING")

class Payer:
    OFFLINE_CAP_PAISE = OFFLINE_CAP_PAISE

    def __init__(self, db_path: str = "payer.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.conn.execute("CREATE TABLE IF NOT EXISTS tokens (token_id TEXT PRIMARY KEY, payee_id TEXT NOT NULL, amount_paise INTEGER NOT NULL, status TEXT NOT NULL, created_at INTEGER NOT NULL, token_json TEXT NOT NULL)")

    def _cfg(self, key: str) -> Optional[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = ?", (key,))
        row = cur.fetchone()
        return row["value"] if row else None

    def get_info(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT key, value FROM config")
        cfg = {r["key"]: r["value"] for r in cur.fetchall()}
        if "payer_id" not in cfg:
            return {"registered": False, "cap_paise": self.OFFLINE_CAP_PAISE}
        cur.execute(f"SELECT SUM(amount_paise) FROM tokens WHERE status IN {SPENT_STATES}")
        spent = cur.fetchone()[0] or 0
        cur.execute("SELECT SUM(amount_paise) FROM tokens WHERE status IN ('SETTLED', 'FLAGGED')")
        paid = cur.fetchone()[0] or 0
        cur.execute("SELECT token_id, payee_id, amount_paise, status, created_at FROM tokens ORDER BY created_at DESC")
        now = int(time.time())
        tokens = []
        for r in cur.fetchall():
            t = dict(r)
            t["unlock_at"] = t["created_at"] + LOCK_S        # when the payer may reclaim (if unused)
            t["can_reclaim"] = t["status"] == "CREATED" and now >= t["unlock_at"]
            tokens.append(t)
        # spent_paise = blocked offline (bank hasn't decided yet); paid_paise = already debited from the bank account.
        return {"registered": True, "payer_id": cfg["payer_id"], "bank_url": cfg.get("bank_url"), "cap_paise": self.OFFLINE_CAP_PAISE, "spent_paise": spent, "paid_paise": paid, "remaining_paise": max(0, self.OFFLINE_CAP_PAISE - spent), "lock_s": LOCK_S, "tokens": tokens}

    def register(self, payer_id: str, bank_url: str, bank_pub_key: str, priv_pem: str, pub_pem: str):
        """Called only AFTER the bank accepted the public key, so a rejected ID never leaves a half-registered wallet."""
        with self.conn:
            for k, v in [('payer_id', payer_id.strip()), ('bank_url', bank_url.rstrip("/")), ('private_key', priv_pem), ('public_key', pub_pem), ('bank_public_key', bank_pub_key.strip())]:
                self.conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, v))

    def create_token(self, payee_id: str, amount_paise: int) -> Dict[str, Any]:
        info = self.get_info()
        if not info["registered"]: raise ValueError("Payer wallet not initialized.")
        if not _is_id(payee_id): raise ValueError("Merchant UPI ID is required.")
        if not _is_int(amount_paise) or amount_paise <= 0: raise ValueError("Amount must be greater than zero.")
        if info["spent_paise"] + amount_paise > self.OFFLINE_CAP_PAISE:
            raise ValueError(f"Offline cap exceeded. Limit is ₹500. Remaining: ₹{info['remaining_paise']/100:.2f}.")

        payload = {"tokenID": str(uuid.uuid4()), "payerID": info["payer_id"], "payeeID": payee_id.strip(), "amount_paise": int(amount_paise), "timestamp": int(time.time())}
        pkg = {"payload": payload, "signature": sign_data(payload, self._cfg("private_key"))}
        with self.conn:
            self.conn.execute("INSERT INTO tokens VALUES (?, ?, ?, 'CREATED', ?, ?)", (payload["tokenID"], payload["payeeID"], payload["amount_paise"], payload["timestamp"], json.dumps(pkg)))
        return pkg

    def get_token(self, token_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_json, status FROM tokens WHERE token_id = ?", (token_id,))
        row = cur.fetchone()
        return {"token_package": json.loads(row["token_json"]), "status": row["status"]} if row else None

    def _signed_cancel(self, token_id: str) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_json FROM tokens WHERE token_id = ?", (token_id,))
        p = json.loads(cur.fetchone()["token_json"])["payload"]
        payload = {"action": "CANCEL", "tokenID": p["tokenID"], "payerID": p["payerID"], "payeeID": p["payeeID"], "amount_paise": p["amount_paise"], "timestamp": p["timestamp"]}
        return {"payload": payload, "signature": sign_data(payload, self._cfg("private_key"))}

    def request_cancel(self, token_id: str, now: Optional[int] = None) -> Tuple[Optional[Dict[str, Any]], str]:
        """Reclaim an UNUSED token. Allowed only after the merchant window (LOCK_S) has passed.
        Returns (signed request, "") or (None, reason). The token stays blocked until the bank answers."""
        now = int(time.time()) if now is None else now
        cur = self.conn.cursor()
        cur.execute("SELECT status, created_at FROM tokens WHERE token_id = ?", (token_id,))
        row = cur.fetchone()
        if not row: return None, "Token not found in this wallet."
        if row["status"] not in SPENT_STATES: return None, f"Token is already {row['status']}; nothing to reclaim."
        if now < row["created_at"] + LOCK_S:
            return None, (f"Locked until {fmt_time(row['created_at'] + LOCK_S)}. A handed-over payment is final; "
                          "the merchant has until then to sync. Ask the merchant for a refund instead.")
        with self.conn:
            self.conn.execute("UPDATE tokens SET status = 'CANCEL_PENDING' WHERE token_id = ?", (token_id,))
        return self._signed_cancel(token_id), ""

    def pending_cancels(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_id FROM tokens WHERE status = 'CANCEL_PENDING'")
        return [self._signed_cancel(r["token_id"]) for r in cur.fetchall()]

    def apply_cancel_result(self, token_id: str, bank_status: str) -> str:
        """bank_status is the bank's final word on the token: VOIDED -> CANCELLED (allowance back), SETTLED/FLAGGED -> stays spent."""
        new = {"VOIDED": "CANCELLED", "SETTLED": "SETTLED", "FLAGGED": "FLAGGED", "REFUNDED": "REFUNDED", "LOCKED": "CREATED"}.get(bank_status)
        if new:
            with self.conn:
                self.conn.execute(f"UPDATE tokens SET status = ? WHERE token_id = ? AND status IN {SPENT_STATES}", (new, token_id))
        return new or "CANCEL_PENDING"

    def reconcile(self, statement: Dict[str, Any]) -> Dict[str, int]:
        payload, sig = statement.get("payload", {}), statement.get("signature", "")
        bank_key = self._cfg("bank_public_key")
        if not bank_key or not verify_sig(payload, sig, bank_key):
            raise ValueError("Invalid bank signature on settlement statement.")
        if payload.get("payer_id") != self._cfg("payer_id"):
            raise ValueError("Statement belongs to a different payer.")
        counts = {}
        with self.conn:
            # WHERE status IN (CREATED, CANCEL_PENDING) makes this idempotent: refreshing 3 times frees allowance once.
            for key, new, allowed in (("settled_token_ids", "SETTLED", SPENT_STATES), ("flagged_token_ids", "FLAGGED", SPENT_STATES),
                                      ("voided_token_ids", "CANCELLED", SPENT_STATES),
                                      ("refunded_token_ids", "REFUNDED", SPENT_STATES + ("SETTLED", "FLAGGED"))):
                n = 0
                for tid in payload.get(key, []):
                    n += self.conn.execute(f"UPDATE tokens SET status = ? WHERE token_id = ? AND status IN {allowed}", (new, tid)).rowcount
                counts[new.lower()] = n
        return counts

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
        cur.execute("SELECT token_id, payer_id, payee_id, amount_paise, status, reason, received_at, token_pkg_json FROM queue ORDER BY received_at DESC")
        queue = []
        for r in cur.fetchall():
            q = dict(r)
            ts = json.loads(q.pop("token_pkg_json"))["payload"]["timestamp"]
            q["sync_deadline"] = ts + LOCK_S                   # paid for sure if synced before this
            queue.append(q)
        return {"registered": "merchant_id" in cfg, "merchant_id": cfg.get("merchant_id"), "bank_url": cfg.get("bank_url"), "merchant_url": cfg.get("merchant_url"), "trusted_payers_count": p_count, "lock_s": LOCK_S, "queue": queue}

    def get_api_key(self) -> Optional[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'api_key'")
        row = cur.fetchone()
        return row["value"] if row else None

    def register(self, merchant_id: str, bank_url: str, merchant_url: str, api_key: str):
        with self.conn:
            for k, v in [('merchant_id', merchant_id.strip()), ('bank_url', bank_url.rstrip("/")), ('merchant_url', merchant_url.rstrip("/")), ('api_key', api_key.strip())]:
                self.conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (k, v))

    def update_trusted_payers(self, payers: List[Dict[str, str]]):
        with self.conn:
            for p in payers:
                if isinstance(p, dict) and _is_id(p.get("payer_id")) and isinstance(p.get("public_key_pem"), str):
                    self.conn.execute("INSERT OR REPLACE INTO trusted_payers VALUES (?, ?)", (p["payer_id"], p["public_key_pem"]))

    def verify_and_queue(self, token_pkg: Any) -> Tuple[bool, str]:
        cur = self.conn.cursor()
        cur.execute("SELECT value FROM config WHERE key = 'merchant_id'")
        m_row = cur.fetchone()
        if not m_row: return False, "Merchant shop is not initialized."
        merchant_id = m_row["value"]

        if not isinstance(token_pkg, dict): return False, "Invalid token package format."
        payload, sig = token_pkg.get("payload"), token_pkg.get("signature")
        if not isinstance(sig, str) or not sig: return False, "Invalid token package format (missing signature)."
        now = int(time.time())
        err = validate_payload(payload, now)
        if err: return False, err
        if now - payload["timestamp"] > accept_age_s(): return False, "Token is too old to accept safely; ask the payer for a fresh one."

        tid, payer_id, payee_id, amt = payload["tokenID"], payload["payerID"], payload["payeeID"], payload["amount_paise"]
        if payee_id != merchant_id: return False, f"Payee mismatch: Token intended for '{payee_id}', not '{merchant_id}'."

        cur.execute("SELECT token_id FROM queue WHERE token_id = ?", (tid,))
        if cur.fetchone(): return False, f"Duplicate token: '{tid}' has already been accepted."

        cur.execute("SELECT public_key_pem FROM trusted_payers WHERE payer_id = ?", (payer_id,))
        p_row = cur.fetchone()
        if not p_row: return False, f"Unknown payer '{payer_id}': not in merchant's trusted local cache."
        if not verify_sig(payload, sig, p_row["public_key_pem"]): return False, "Cryptographic signature verification failed."

        with self.conn:
            self.conn.execute("INSERT INTO queue VALUES (?, ?, ?, ?, 'PENDING', 'Awaiting bank sync', ?, ?)", (tid, payer_id, payee_id, amt, now, json.dumps(token_pkg)))
        return True, f"Payment verified offline. Guaranteed if you sync before {fmt_time(payload['timestamp'] + LOCK_S)}."

    def get_pending_batch(self) -> List[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_pkg_json FROM queue WHERE status = 'PENDING' ORDER BY received_at ASC")
        return [json.loads(r["token_pkg_json"]) for r in cur.fetchall()]

    def get_token_pkg(self, token_id: str) -> Optional[Dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute("SELECT token_pkg_json, status FROM queue WHERE token_id = ?", (token_id,))
        row = cur.fetchone()
        return {"token_package": json.loads(row["token_pkg_json"]), "status": row["status"]} if row else None

    def mark_refunded(self, token_id: str, reason: str):
        with self.conn:
            self.conn.execute("UPDATE queue SET status = 'REFUNDED', reason = ? WHERE token_id = ?", (reason, token_id))

    def apply_sync_results(self, results: List[Dict[str, Any]]):
        with self.conn:
            for item in results:
                if isinstance(item, dict) and item.get("token_id") and item.get("status"):
                    self.conn.execute("UPDATE queue SET status = ?, reason = ? WHERE token_id = ? AND status = 'PENDING'", (item["status"], item.get("reason", ""), item["token_id"]))

# --- Bank: Authoritative Ledger, Atomic Savepoints, Signatures ---
# The ledger's token_id PRIMARY KEY is the single source of truth. Settlement is only allowed inside the
# merchant window and payer reclaim only after it, so the two can never race. A merchant may refund at any time.

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
            # Refused attempts (DUPLICATE / REJECTED / CANCELLED_BY_PAYER) are logged separately so the ledger PK stays clean.
            self.conn.execute("CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, token_id TEXT, payer_id TEXT, payee_id TEXT, amount_paise INTEGER, status TEXT NOT NULL, reason TEXT, at INTEGER NOT NULL)")
        cur = self.conn.cursor()
        cur.execute("SELECT key_val FROM keys WHERE key_name = 'private_key'")
        if not cur.fetchone():
            priv_pem, pub_pem = generate_keypair()
            with self.conn:
                self.conn.execute("INSERT INTO keys VALUES ('private_key', ?), ('public_key', ?)", (priv_pem, pub_pem))

    def _key(self, name: str) -> str:
        cur = self.conn.cursor()
        cur.execute("SELECT key_val FROM keys WHERE key_name = ?", (name,))
        return cur.fetchone()["key_val"]

    def get_public_key(self) -> str:
        return self._key("public_key")

    def register_payer(self, payer_id: Any, public_key_pem: Any) -> Tuple[bool, str]:
        if not _is_id(payer_id): return False, "Payer ID required."
        if not is_valid_public_key(public_key_pem): return False, "Invalid public key."
        try:
            with self.conn:
                self.conn.execute("INSERT INTO payers VALUES (?, ?, ?)", (payer_id.strip(), public_key_pem.strip(), int(time.time())))
            return True, "Payer registered."
        except sqlite3.IntegrityError:
            return False, f"Payer ID '{payer_id.strip()}' is already registered with the bank."

    def register_merchant(self, merchant_id: Any, merchant_url: Any, api_key: str) -> Tuple[bool, str]:
        if not _is_id(merchant_id): return False, "Merchant ID required."
        try:
            with self.conn:
                self.conn.execute("INSERT INTO merchants VALUES (?, ?, ?, ?)", (merchant_id.strip(), api_key, str(merchant_url or "").rstrip("/"), int(time.time())))
            return True, "Merchant registered."
        except sqlite3.IntegrityError:
            return False, f"Merchant ID '{merchant_id.strip()}' is already registered with the bank."

    def verify_merchant(self, merchant_id: Any, api_key: Any) -> bool:
        if not isinstance(merchant_id, str) or not isinstance(api_key, str): return False
        cur = self.conn.cursor()
        cur.execute("SELECT api_key FROM merchants WHERE merchant_id = ?", (merchant_id.strip(),))
        row = cur.fetchone()
        return bool(row and row["api_key"] == api_key.strip())

    def get_trusted_payers(self) -> List[Dict[str, str]]:
        cur = self.conn.cursor()
        cur.execute("SELECT payer_id, public_key_pem FROM payers ORDER BY registered_at ASC")
        return [dict(r) for r in cur.fetchall()]

    def _log_attempt(self, payload: Any, status: str, reason: str, tid: Optional[str] = None):
        p = payload if isinstance(payload, dict) else {}
        amt = p.get("amount_paise") if _is_int(p.get("amount_paise")) else None
        with self.conn:
            self.conn.execute("INSERT INTO attempts (token_id, payer_id, payee_id, amount_paise, status, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                              (str(tid or p.get("tokenID") or "unknown"), str(p.get("payerID") or "—"), str(p.get("payeeID") or "—"), amt, status, reason, int(time.time())))

    def _payer_sig_ok(self, payload: Dict[str, Any], sig: Any) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT public_key_pem FROM payers WHERE payer_id = ?", (payload["payerID"],))
        row = cur.fetchone()
        return bool(row and isinstance(sig, str) and verify_sig(payload, sig, row["public_key_pem"]))

    def _claim(self, sp: str, row: tuple) -> Optional[str]:
        """Atomically INSERT the token into the ledger. Returns None on success, or the status that already owns the token."""
        cur = self.conn.cursor()
        cur.execute(f"SAVEPOINT {sp}")
        try:
            cur.execute("INSERT INTO ledger VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
            cur.execute(f"RELEASE SAVEPOINT {sp}")
            self.conn.commit()
            return None
        except sqlite3.IntegrityError:
            cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
            cur.execute(f"RELEASE SAVEPOINT {sp}")
            self.conn.commit()
            cur.execute("SELECT status FROM ledger WHERE token_id = ?", (row[0],))
            return cur.fetchone()["status"]

    def settle_batch(self, merchant_id: str, batch: Any, now: Optional[int] = None) -> List[Dict[str, Any]]:
        results, now = [], int(time.time()) if now is None else now
        if not isinstance(batch, list): return results
        for idx, pkg in enumerate(batch):
            payload = pkg.get("payload") if isinstance(pkg, dict) else None
            sig = pkg.get("signature") if isinstance(pkg, dict) else None
            tid = payload.get("tokenID") if isinstance(payload, dict) and isinstance(payload.get("tokenID"), str) else f"pkg_{idx}"

            err = validate_payload(payload, now)
            if not err and payload["payeeID"] != merchant_id: err = "Payee mismatch"
            if err:
                self._log_attempt(payload, "REJECTED", err, tid)
                results.append({"token_id": tid, "status": "REJECTED", "reason": err})
                continue
            if not self._payer_sig_ok(payload, sig):
                self._log_attempt(payload, "REJECTED", "Unknown payer or invalid signature", tid)
                results.append({"token_id": tid, "status": "REJECTED", "reason": "Unknown payer or invalid signature"})
                continue

            if now - payload["timestamp"] > LOCK_S:
                r = f"Sync deadline passed ({fmt_time(payload['timestamp'] + LOCK_S)}). Token expired; payer may reclaim it."
                self._log_attempt(payload, "EXPIRED", r, tid)
                results.append({"token_id": tid, "status": "EXPIRED", "reason": r})
                continue
            status, reason = "SETTLED", "Settlement completed"
            existing = self._claim(f"sp_{idx}", (tid, payload["payerID"], payload["payeeID"], payload["amount_paise"], status, reason, payload["timestamp"], now))
            if existing is None:
                results.append({"token_id": tid, "status": status, "reason": reason})
            elif existing in ("VOIDED", "REFUNDED"):
                r = "This token was already refunded/voided at the bank."
                self._log_attempt(payload, "CANCELLED_BY_PAYER" if existing == "VOIDED" else "REFUNDED", r, tid)
                results.append({"token_id": tid, "status": "REFUNDED" if existing == "REFUNDED" else "CANCELLED_BY_PAYER", "reason": r})
            else:
                r = f"Double-spend attempt: Token already {existing}"
                self._log_attempt(payload, "DUPLICATE", r, tid)
                results.append({"token_id": tid, "status": "DUPLICATE", "reason": r})
        return results

    def cancel_token(self, cancel_pkg: Any, now: Optional[int] = None) -> Tuple[bool, str, str]:
        """Payer-signed RECLAIM of an unused token. Refused (LOCKED) during the merchant window.
        Returns (voided?, final_status, message)."""
        now = int(time.time()) if now is None else now
        payload = cancel_pkg.get("payload") if isinstance(cancel_pkg, dict) else None
        sig = cancel_pkg.get("signature") if isinstance(cancel_pkg, dict) else None
        err = validate_payload(payload)
        if err or payload.get("action") != "CANCEL": return False, "INVALID", err or "Not a cancel request."
        if not self._payer_sig_ok(payload, sig): return False, "INVALID", "Unknown payer or invalid signature on cancel request."
        tid = payload["tokenID"]
        if now < payload["timestamp"] + LOCK_S:
            return False, "LOCKED", f"Locked until {fmt_time(payload['timestamp'] + LOCK_S)}: the merchant window is still open."
        existing = self._claim("sp_cancel", (tid, payload["payerID"], payload["payeeID"], payload["amount_paise"], "VOIDED", "Unused token reclaimed by payer after merchant window", payload["timestamp"], now))
        if existing is None: return True, "VOIDED", "Reclaim confirmed: no merchant synced this token in time. Amount restored."
        if existing in ("VOIDED", "REFUNDED"): return True, existing, "Token was already refunded."
        return False, existing, f"Cannot reclaim: the merchant synced this payment in time (status {existing}). It stays paid."

    def merchant_refund(self, merchant_id: str, token_pkg: Any) -> Tuple[bool, str, str]:
        """Merchant-initiated refund (authenticated by the merchant API key in server.py).
        Unsynced token -> VOIDED (never paid). Settled token -> REFUNDED (money returned)."""
        payload = token_pkg.get("payload") if isinstance(token_pkg, dict) else None
        sig = token_pkg.get("signature") if isinstance(token_pkg, dict) else None
        err = validate_payload(payload)
        if err: return False, "INVALID", err
        if payload["payeeID"] != merchant_id: return False, "INVALID", "You can only refund payments made to your own shop."
        if not self._payer_sig_ok(payload, sig): return False, "INVALID", "Invalid payer signature on token."
        tid, now = payload["tokenID"], int(time.time())
        existing = self._claim("sp_refund", (tid, payload["payerID"], payload["payeeID"], payload["amount_paise"], "VOIDED", "Refunded by merchant before sync", payload["timestamp"], now))
        if existing is None: return True, "VOIDED", "Refund done: payment cancelled before settlement; customer's amount restored."
        if existing == "SETTLED":
            with self.conn:
                self.conn.execute("UPDATE ledger SET status = 'REFUNDED', reason = 'Refunded by merchant after settlement', settled_at = ? WHERE token_id = ?", (now, tid))
            return True, "REFUNDED", "Refund done: settled payment reversed; money returned to the customer."
        return True, existing, f"Token is already {existing}."

    def simulate_duplicate(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT * FROM ledger WHERE status = 'SETTLED' ORDER BY settled_at DESC LIMIT 1")
        row = cur.fetchone()
        if not row: return {"success": False, "message": "No settled transactions in ledger yet. Please settle a payment first."}
        existing = self._claim("sim_dup", (row["token_id"], row["payer_id"], row["payee_id"], row["amount_paise"], "SETTLED", "Duplicate simulation", row["token_timestamp"], int(time.time())))
        if existing is None: return {"success": True, "message": "Duplicate unexpectedly accepted."}
        self._log_attempt({"tokenID": row["token_id"], "payerID": row["payer_id"], "payeeID": row["payee_id"], "amount_paise": row["amount_paise"]}, "DUPLICATE", "Simulated replay of a settled token")
        return {"success": True, "status": "DUPLICATE", "token_id": row["token_id"], "message": f"Double-spend detected! Token '{row['token_id']}' rejected atomically by SQLite PRIMARY KEY."}

    def get_payer_settlement_statement(self, payer_id: str) -> Dict[str, Any]:
        cur = self.conn.cursor()
        ids = {}
        for status in ("SETTLED", "FLAGGED", "VOIDED", "REFUNDED"):
            cur.execute("SELECT token_id FROM ledger WHERE payer_id = ? AND status = ?", (payer_id, status))
            ids[status] = [r["token_id"] for r in cur.fetchall()]
        payload = {"payer_id": payer_id, "settled_token_ids": ids["SETTLED"], "flagged_token_ids": ids["FLAGGED"], "voided_token_ids": ids["VOIDED"], "refunded_token_ids": ids["REFUNDED"], "timestamp": int(time.time())}
        return {"payload": payload, "signature": sign_data(payload, self._key("private_key"))}

    def get_stats(self) -> Dict[str, Any]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*), SUM(CASE WHEN status='SETTLED' THEN amount_paise ELSE 0 END), SUM(CASE WHEN status='SETTLED' THEN 1 ELSE 0 END) FROM ledger")
        tot, amt, set_c = cur.fetchone()
        cur.execute("SELECT SUM(CASE WHEN status='DUPLICATE' THEN 1 ELSE 0 END), SUM(CASE WHEN status='REJECTED' THEN 1 ELSE 0 END), COUNT(*) FROM attempts")
        dup_c, rej_c, att_c = cur.fetchone()
        cur.execute("SELECT token_id, payer_id, payee_id, amount_paise, status, reason, settled_at FROM ledger")
        rows = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT token_id, payer_id, payee_id, COALESCE(amount_paise, 0) AS amount_paise, status, reason, at AS settled_at FROM attempts")
        rows += [dict(r) for r in cur.fetchall()]
        rows.sort(key=lambda r: r["settled_at"], reverse=True)
        cur.execute("SELECT payer_id, registered_at FROM payers ORDER BY registered_at DESC")
        payers = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT merchant_id, merchant_url, registered_at FROM merchants ORDER BY registered_at DESC")
        merchants = [dict(r) for r in cur.fetchall()]
        return {"total_received": (tot or 0) + (att_c or 0), "settled_count": set_c or 0, "settled_amount_paise": amt or 0, "duplicate_count": dup_c or 0, "rejected_count": rej_c or 0, "ledger": rows, "payers": payers, "merchants": merchants}
