# Implementation Plan: True Three-Device Offline UPI Simulation

## 1. Objectives & Architectural Blueprint

The goal is to transition the offline UPI prototype from a single-process Flask application into three independently runnable services designed for deployment on separate physical devices (or distinct local processes/ports) connected over a local Wi-Fi / LAN, or completely disconnected.

```
+--------------------------------------------------------------------------------------------------+
|                                    DEPLOYMENT ARCHITECTURE                                       |
+--------------------------------------------------------------------------------------------------+

[DEVICE 1: Customer Phone]               [DEVICE 2: Merchant POS]            [DEVICE 3: Bank Server]
Payer Service (Port 5001)                Merchant Service (Port 5002)        Bank Service (Port 5003)
-------------------------                ----------------------------        ------------------------
- payer_service.py                       - merchant_service.py               - bank_service.py
- PayerDevice (Integer Paise)            - MerchantDevice (SQLite queue)     - BackendServer (SQLite ledger)
- RSA Private Key (NEVER leaves)         - Trusted Payer Registry (Public)   - Central Registry & Audit Log
- Offline Spend Cap: ₹500 (50000p)       - Sub-second local verification     - 100% Duplicate Catch Rate
- Serves /payer dashboard                - Serves /merchant dashboard        - Serves /bank dashboard
           |                                          |                                  |
           |   1. Offline Transfer (QR / JSON File)   |                                  |
           +=========================================>|                                  |
           |   (Zero Network / Internet Required)     |                                  |
           |                                          |   2. LAN Delayed Sync            |
           |                                          |      (HTTP POST /api/bank/sync)  |
           |                                          +--------------------------------->|
           |                                          |      (When LAN connected)        |
           |                                          |<---------------------------------+
           |                                          |   3. Settlement Confirmation     |
           |                                          |                                  |
           |   4. Optional Reconcile Receipt          |                                  |
           |< - - - - - - - - - - - - - - - - - - - - +                                  |
```

---

## 2. Service Boundaries & State Ownership

### 2.1 Device 1: Payer Service (`payer_service.py`)
- **Port**: Default `5001` (configurable via `--port` or `PAYER_PORT`).
- **State File**: `payer_state_<id>.json`.
- **State Ownership**:
  - Payer RSA-2048 private key (held in memory & local JSON only; never shared).
  - Payer public key PEM.
  - Cumulative unsynced spend in **integer paise** (50,000 paise = ₹500.00).
  - Outstanding tokens dictionary (`token_id -> metadata`).
- **Network Independence**: Operates with zero network access. Can generate tokens when the Bank service and Merchant service are offline or stopped.
- **APIs**:
  - `GET /` or `GET /payer`: Serves the Customer Payer Dashboard.
  - `GET /api/payer/status`: Returns current balance, remaining allowance, outstanding count, and transaction history.
  - `POST /api/payer/generate`: Takes `{amount, payee_id}`. Validates amounts, checks exact ₹500 cap, signs payload, updates local balance, returns signed token package.
  - `POST /api/payer/cancel`: Takes `{token_id}`. Cancels an un-transmitted or rejected token and restores spending allowance.
  - `POST /api/payer/reconcile`: Takes `{settled_token_ids}`. Restores allowance for confirmed settlements.
  - `GET /api/payer/public_key`: Exports the public key PEM for trusted provisioning.

### 2.2 Device 2: Merchant Service (`merchant_service.py`)
- **Port**: Default `5002` (configurable via `--port` or `MERCHANT_PORT`).
- **Configuration**: Bank service URL (`--bank-url` or `BANK_URL`, default `http://localhost:5003`).
- **Database**: `merchant_queue_<id>.db` (SQLite).
- **State Ownership**:
  - Merchant ID (e.g. `merchant_bob@upi`).
  - Persistent SQLite table `local_queue` (tokens with status `Pending Sync` or `Settled`).
  - Persistent SQLite table `trusted_payers` (registered public keys).
- **Network Independence**: Local verification executes strictly in-process using local CPU cryptographic operations and local SQLite storage. Operates 100% offline.
- **APIs**:
  - `GET /` or `GET /merchant`: Serves the Merchant POS Dashboard.
  - `GET /api/merchant/status`: Returns queue metrics, list of queued tokens, and bank LAN reachability status.
  - `POST /api/merchant/receive`: Ingests a signed token package (from JSON paste, file upload, or QR code). Enforces schema, payee binding, and trusted signature. Writes to SQLite queue as `Pending Sync`.
  - `POST /api/merchant/sync`: Triggers synchronization with the central bank over LAN HTTP POST. If bank is unreachable, fails gracefully with 503 while preserving 100% of the local queue.
  - `POST /api/merchant/purge_settled`: Removes settled records from active display.
  - `POST /api/merchant/provision_payer`: Registers a trusted payer public key.

### 2.3 Device 3: Bank Service (`bank_service.py`)
- **Port**: Default `5003` (configurable via `--port` or `BANK_PORT`).
- **Database**: `offline_upi_backend.db` (SQLite).
- **Security**: Admin token required for sensitive operations (`--admin-token` or `BANK_ADMIN_TOKEN`).
- **State Ownership**:
  - Central ledger table `settled_tokens` with UNIQUE `token_id` constraint.
  - Central registry `trusted_payers`.
  - Audit log table `sync_audit_log`.
- **APIs**:
  - `GET /` or `GET /bank`: Serves the Central Bank Dashboard.
  - `GET /api/bank/status`: Returns system metrics, settled total, double-spend count, flagged count, recent batches, and master ledger.
  - `POST /api/bank/sync_batch`: Core LAN settlement endpoint. Accepts `{merchant_id, tokens}`. Verifies signatures against registered keys, checks payee binding, enforces atomic settlement with SAVEPOINTs, catches duplicates via `sqlite3.IntegrityError`, flags tokens > 24 hours old.
  - `POST /api/bank/register_payer`: Protected endpoint (requires `X-Admin-Token` header) to register authorized payers.
  - `POST /api/bank/toggle_network`: Maintenance/simulation control to disable/enable LAN sync responses.
  - `POST /api/bank/simulate_duplicate`: Demonstration endpoint for replaying settled tokens.

---

## 3. Financial & Cryptographic Specifications

### 3.1 Integer Paise Accounting Policy
To prevent floating point drift and enforce exact boundary limits:
- Standard offline cap: `50000` paise ($₹500.00$).
- Amount calculation: `amount_paise = int(round(float(amount) * 100))`
- Enforcement:
  $$\text{unsynced\_spent\_paise} + \text{amount\_paise} \le 50000$$
  - Exactly ₹500.00 (50,000 paise) is permitted.
  - ₹500.01 (50,001 paise) raises `OfflineSpendLimitExceededError`.
- State persistence retains both `unsynced_spent` (in INR for display) and `unsynced_spent_paise` (for exact math).

### 3.2 Token Lifecycle & Accounting States
1. **CREATED**: Token generated and signed on payer device; allowance decremented.
2. **CANCELLED**: Token cancelled on payer device prior to settlement; allowance restored.
3. **PENDING SYNC**: Token verified and stored in merchant SQLite queue; not yet settled.
4. **SETTLED**: Token confirmed by bank ledger; merchant marks as `Settled`; payer restores allowance upon reconciliation.
5. **FLAGGED_FOR_REVIEW**: Token submitted > 24h after creation; recorded in bank ledger for audit; not automatically settled.
6. **REJECTED**: Signature invalid, unknown payer, payee mismatch, or malformed schema; rejected immediately.

---

## 4. Genuine Offline Transfer Mechanisms

1. **JSON File Export / Import**:
   - Payer dashboard provides a **"Download JSON File"** button producing `offline_token_<id>.json`.
   - File can be moved between devices using USB flash drive, local file drop, or shared directory.
   - Merchant dashboard supports **"Upload JSON File"** and parses/verifies it without network access.
2. **QR Code Handoff**:
   - Payer dashboard encodes the canonical token package into an offline QR code via `qrcode.min.js`.
   - Merchant POS can decode QR content (or paste the JSON payload) and verify offline.
3. **Clipboard Copy**:
   - Payer dashboard provides **"Copy JSON Token"** for instant paste into the Merchant verification box.

---

## 5. Security & Administrative Controls

1. **Private Key Protection**:
   - `payer_service.py` never exposes endpoints that return private keys.
   - Private key is never embedded in token packages or JSON exports.
2. **Public Key Trust Model**:
   - Tokens carry an optional `payer_public_key` field, but **neither merchant nor bank uses it**.
   - Cryptographic verification strictly queries the local trusted registry (`trusted_payers`).
3. **Admin Token Authentication**:
   - Bank administration routes (`/api/bank/register_payer`, `/api/bank/toggle_network`) validate the `X-Admin-Token` header.
   - Prevents unauthorized LAN devices from injecting arbitrary public keys.
4. **Provisioning Tool (`provision_keys.py`)**:
   - A dedicated command-line provisioning script that reads the payer's public key and registers it directly with the merchant and bank databases or services.

---

## 6. Testing Strategy

1. **Preserve Existing Tests**:
   - Keep all 28 existing unit and integration tests passing in `test_offline_upi.py` and `test_app.py`.
2. **New Unit & Component Tests**:
   - `test_payer_service.py`: Verify payer Flask service, integer paise limits, persistence, token generation.
   - `test_merchant_service.py`: Verify merchant Flask service, local verification, queueing, offline resilience.
   - `test_bank_service.py`: Verify bank Flask service, sync batch, admin auth, duplicate detection.
3. **Live Multi-Process Integration Test (`test_three_device_integration.py`)**:
   - Spawn `payer_service.py` on port 5001, `merchant_service.py` on port 5002, `bank_service.py` on port 5003 as separate OS subprocesses.
   - **Step 1**: Provision trusted keys across services.
   - **Step 2**: Terminate the bank service process (`proc.terminate()`).
   - **Step 3**: Verify Payer service can still generate and sign a ₹150 token.
   - **Step 4**: Transfer token to Merchant service; verify Merchant accepts and queues it offline while Bank is completely down.
   - **Step 5**: Attempt sync from Merchant to Bank; verify Merchant gets a graceful 503 and queue is 100% retained.
   - **Step 6**: Restart Bank service process on port 5003.
   - **Step 7**: Sync from Merchant to Bank; verify settlement confirmation and status update.
   - **Step 8**: Replay the same token; verify Bank detects and rejects duplicate.
   - **Step 9**: Clean up background processes and temporary databases.

---

## 7. Execution Steps

1. Update `requirements.txt` to include `requests`.
2. Refactor `payer.py` to use integer paise accounting while preserving existing API.
3. Implement `bank_service.py` with standalone Flask app, LAN API, admin authentication, and bank dashboard.
4. Implement `merchant_service.py` with standalone Flask app, local verification, SQLite queue, HTTP sync client to Bank, and merchant dashboard.
5. Implement `payer_service.py` with standalone Flask app, local token generation, spend cap enforcement, and payer dashboard.
6. Create `provision_keys.py` provisioning CLI.
7. Create `run_all.py` convenience script to start all 3 services in one terminal for local testing.
8. Update `templates/` and `static/js/` to support configurable service ports and cross-service navigation.
9. Implement unit and separate-process integration tests.
10. Execute the complete test suite and record results.
11. Update `README.md` with multi-device startup commands, LAN configuration, and offline handoff instructions.
