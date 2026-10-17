# Architecture Audit: Existing Offline UPI Simulation

## 1. Executive Summary

This audit assesses the current state of the Offline / Low-Connectivity UPI Payment System repository (`https://github.com/NK28-V/Offline_UPI`). The existing codebase provides an educational simulation of cryptographic offline payment tokens, payee binding, and duplicate detection.

However, the current architecture runs as a **single-process monolith** inside `app.py`. The payer device, merchant point-of-sale (POS), and bank backend exist as in-memory Python objects sharing a single Flask application runtime and memory space. 

To achieve a **True Three-Device Architecture**, each entity must run as an autonomous, independently deployed service with zero runtime reliance on the others during offline payment generation and merchant verification.

---

## 2. In-Depth Component Audit

### 2.1 Cryptographic Primitives & Canonical Serialization (`crypto_utils.py`)
- **Key Generation (`generate_rsa_key_pair`)**: Generates 2048-bit RSA keys with public exponent $e = 65537$. Standard and secure for this application.
- **Serialization (`private_key_to_pem`, `public_key_to_pem`)**: Uses PKCS#8 format for private keys (unencrypted) and SubjectPublicKeyInfo (X.509) for public keys.
- **Canonical Serialization (`canonical_json_bytes`)**: Uses `json.dumps(data, sort_keys=True, separators=(',', ':')).encode('utf-8')`. This strictly normalizes JSON formatting (no extra whitespace, alphabetically sorted keys) before hashing.
- **Digital Signatures (`sign_payload`, `verify_signature`)**: Uses Probabilistic Signature Scheme (RSA-PSS) with MGF1(SHA-256) and maximum salt length, paired with SHA-256 message hashing.
- **Payload Validation (`validate_token_payload`)**: Enforces required fields (`tokenID`, `payerID`, `payeeID`, `amount`, `timestamp`), verifies positive finite numbers, non-empty string IDs, and checks against future timestamps exceeding a 5.0-second clock skew tolerance.

### 2.2 Payer Device & Balance Accounting (`payer.py`)
- **Key Ownership**: Generates or loads RSA-2048 private/public keys stored in local JSON (`payer_state_<id>.json`).
- **Offline Spending Cap**: Enforces a cumulative unsynced spending limit of ₹500.00.
- **Current Limitation (Floating Point)**: Balance calculations use standard Python `float` with `round(val, 2)`. While `round` mitigates basic IEEE-754 drift, financial systems require integer-based accounting (paise) to guarantee exactness (e.g. 50000 paise vs 50001 paise).
- **Outstanding Token Tracking**: Keeps a dictionary of `outstanding_tokens` indexed by `token_id`.
- **Cancellation & Reconciliation**:
  - `cancel_failed_token`: Restores spending allowance if a transfer fails offline.
  - `reconcile_settled_tokens`: Restores allowance for tokens confirmed settled by the bank, preventing double reconciliation.
- **Fault Tolerance**: State is dumped to disk on updates.

### 2.3 Merchant Terminal & Local Verification (`merchant.py`)
- **Key Registry (`trusted_payers`)**: Maintains a local SQLite table of trusted payer public keys. When receiving a token, the merchant **never** trusts the public key sent inside the token package; it strictly looks up the registered key for `payload['payerID']`.
- **Sub-Second Verification**: Signature verification is executed entirely locally using CPU cryptographic operations without any network roundtrips. Verification latency is typically < 1 ms (well within the 1000 ms NFR).
- **Payee Binding**: Validates `payload['payeeID'] == self.merchant_id`. Tokens addressed to another merchant are rejected.
- **Persistent Queue (`local_queue`)**: Verified tokens are inserted into an SQLite database (`merchant_queue_<id>.db`) with status `Pending Sync`.
- **Queue Recovery**: On restart, existing SQLite queue rows and trusted keys are reloaded.
- **Settlement Marking**: Only marks tokens as `Settled` when explicitly confirmed by the backend batch sync report.

### 2.4 Central Banking Switch & Settlement Ledger (`backend.py`)
- **Central Storage (`offline_upi_backend.db`)**:
  - `settled_tokens`: Ledger of settled tokens, storing `token_id` (PRIMARY KEY), amounts, timestamps, ages, status, signatures, and review reasons.
  - `trusted_payers`: Central directory of authorized payer public keys.
  - `sync_audit_log`: Historical log of all merchant sync batches.
- **Atomic Settlement**: Uses SQLite transactions with per-token `SAVEPOINT`s. If an insertion throws `sqlite3.IntegrityError` due to a duplicate `token_id`, the savepoint is rolled back, the duplicate is recorded, and subsequent tokens in the batch continue processing.
- **Duplicate & Double-Spend Defense**:
  - In-batch duplicates are filtered via a tracking set (`seen_in_current_batch`).
  - Cross-batch and replayed duplicates trigger `sqlite3.IntegrityError` on the PRIMARY KEY.
- **24-Hour Token Aging (FR-8 / NFR-4)**: Checks `current_time - token_timestamp > 86400.0`. Tokens older than 24 hours are assigned status `FLAGGED_FOR_REVIEW` with an explicit reason and are not automatically settled.

---

## 3. Analysis of the Monolithic Flask Process (`app.py`)

In the existing `app.py`, the three conceptual actors are unified inside a single Flask script:

```python
# Lines 28-30 of app.py
payer = PayerDevice(payer_id="alice@upi", offline_cap=500.0, state_file=PAYER_STATE_FILE)
merchant = MerchantDevice(merchant_id="merchant_bob@upi", queue_db_path=MERCHANT_DB_FILE)
backend = BackendServer(db_path=BACKEND_DB_FILE)
```

### Critical Architectural Deficiencies Identified:

1. **Shared In-Memory Execution**:
   - The payer, merchant, and bank reside in the same Python process memory.
   - In `/api/merchant/sync` (lines 229–237 of `app.py`), the merchant synchronization endpoint directly calls `backend.sync_batch()` and immediately executes `payer.reconcile_settled_tokens()` in-memory. In real life, a merchant cannot directly invoke code inside the customer's phone!
2. **Single Failure Domain**:
   - If the Flask application or the bank backend crashes or is stopped, the payer dashboard and merchant dashboard also crash.
   - It is currently impossible to test true offline operation where the bank process is terminated while the payer generates tokens and the merchant verifies them.
3. **Simulated Boolean Network State**:
   - Network connectivity is represented by a Python global variable `network_online = True`.
   - Toggling the network simply checks an `if not network_online:` branch in the Flask handler rather than dealing with genuine HTTP network connections, connection timeouts, or physical network partitions.
4. **Coupled Frontend APIs**:
   - The frontend JavaScript files (`payer.js`, `merchant.js`, `bank.js`) make relative API calls (`fetch('/api/payer/...')`, `fetch('/api/merchant/...')`, `fetch('/api/bank/...')`).
   - All three web pages assume they are served from the same host and port.
5. **Unprotected Administrative Operations**:
   - Endpoints like `/api/bank/register_payer` permit any client on the network to submit and overwrite public keys without authentication or administrative tokens.
6. **Floating-Point Arithmetic in Payer**:
   - Balance math uses floating-point numbers (`round(self.unsynced_spent + amt, 2)`). While functional, integer paise representation avoids IEEE-754 precision issues.

---

## 4. Current vs. Target Execution Boundary

| Operation | Current Execution Point | Target Execution Point (Three Devices) |
| :--- | :--- | :--- |
| **Payer Key Storage** | `payer_state_alice_upi.json` (host machine) | Device 1 (Payer Device isolated filesystem) |
| **Token Generation & RSA Signing** | `app.py` in-memory `payer.generate_token()` | Device 1 Payer Service process |
| **Offline Spend Cap Enforcement** | `app.py` in-memory `payer` object | Device 1 Payer Service (Integer Paise) |
| **Token Export / Display** | Web UI on port 5000 | Device 1 Web UI / QR / JSON file export |
| **Token Ingestion** | Pasted into web UI on port 5000 | Device 2 (Merchant POS via JSON file or QR scan) |
| **Local Signature Verification** | `app.py` in-memory `merchant.verify_and_accept_token()` | Device 2 Merchant Service process |
| **Merchant Queue Persistence** | `merchant_queue_bob_upi.db` (host machine) | Device 2 Merchant SQLite database |
| **Merchant Batch Sync** | In-memory call to `backend.sync_batch()` | Device 2 making LAN HTTP POST to Device 3 (`/api/bank/sync_batch`) |
| **Payer Reconciliation** | Direct in-memory call `payer.reconcile_settled_tokens()` | Device 1 reconciliation (via bank receipt import or payer sync) |
| **Bank Settlement & Double-Spend Check** | In-memory `backend` object in `app.py` | Device 3 Bank Service process & SQLite ledger |
| **Network Availability** | `network_online = True/False` global flag | Real LAN HTTP network availability, timeouts, and process status |

---

## 5. Security & Trust Model Observations

1. **Private Key Isolation**: In the existing implementation, the payer's private key is properly held by `PayerDevice` and never serialized into the token package or sent to the merchant/backend. This property must be rigorously maintained in the distributed service model.
2. **Public Key Provisioning**: Both the merchant and bank maintain separate trusted registries. Neither trusts the `payer_public_key` field in the token package. However, in `app.py`, keys were provisioned at server boot. In the three-device architecture, an explicit provisioning utility or administrative pre-shared secret setup is required.
3. **No Network Dependency for Token Verification**: The cryptographic primitives (`verify_signature`, `canonical_json_bytes`) are fully self-contained. The merchant service will run completely autonomously without LAN or internet.

---

## 6. Conclusion

The domain logic (`payer.py`, `merchant.py`, `backend.py`, `crypto_utils.py`) is functionally sound and passes all 28 existing unit/integration tests. However, the system architecture must be decomposed into three separately runnable services:
1. `payer_service.py` (running on Device 1, e.g. Port 5001)
2. `merchant_service.py` (running on Device 2, e.g. Port 5002)
3. `bank_service.py` (running on Device 3, e.g. Port 5003)

This decomposition will enable real process lifecycle independence (e.g. stopping the bank process completely during payment generation and merchant verification).
