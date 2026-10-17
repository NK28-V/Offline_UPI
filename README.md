# True Three-Device Offline UPI Simulation

An academic core-mechanics implementation of an **Offline / Low-Connectivity UPI Payment Protocol** operating across **three independent services on three separate physical devices** (Customer Phone, Merchant POS Terminal, and Central Bank Switch).

Demonstrates cryptographically signed offline payment tokens (IOUs), exact integer paise spending cap enforcement, sub-second local merchant verification with zero bank contact, offline token handoff via JSON or QR code, persistent SQLite queue recovery across reboots, and atomic delayed reconciliation with 100% double-spend defense.

---

## 🏛️ System Architecture: True Three-Device Separation

Unlike monolithic web applications where all dashboards share a single server process, this project implements **three separately runnable, autonomous services**:

```
+--------------------------------------------------------------------------------------------------------+
|                                      SYSTEM TOPOLOGY & BOUNDARIES                                      |
+--------------------------------------------------------------------------------------------------------+

  [DEVICE 1: Customer Phone]               [DEVICE 2: Merchant POS]            [DEVICE 3: Bank Server]
  Payer Service (Port 5001)                Merchant Service (Port 5002)        Bank Service (Port 5003)
  -------------------------                ----------------------------        ------------------------
  - payer_service.py                       - merchant_service.py               - bank_service.py
  - PayerDevice (Integer Paise)            - MerchantDevice (SQLite queue)     - BackendServer (SQLite ledger)
  - RSA Private Key (ISOLATED)             - Trusted Payer Registry (Public)   - Central Registry & Audit Log
  - Offline Cap: ₹500 (50,000p)            - Sub-second local verification     - 100% Duplicate Catch Rate
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
             |   4. Settlement Receipt / Reconcile      |                                  |
             |< - - - - - - - - - - - - - - - - - - - - +                                  |
```

### Operational Matrix: What Works Where?

| System Operation | Zero Network (Offline) | Local Wi-Fi / LAN | Bank Service Required |
| :--- | :---: | :---: | :---: |
| **Token Creation & RSA Signing** | ✅ **YES** | Optional | ❌ **NO** (Bank can be powered off) |
| **₹500.00 Limit Enforcement** | ✅ **YES** | Optional | ❌ **NO** (Enforced on Payer phone) |
| **Token Export (JSON / QR)** | ✅ **YES** | Optional | ❌ **NO** |
| **Merchant Signature Verification** | ✅ **YES** | Optional | ❌ **NO** (Verified locally in < 1ms) |
| **Merchant Queue Persistence** | ✅ **YES** | Optional | ❌ **NO** (Stored in local SQLite) |
| **Cancelled Transfer Recovery** | ✅ **YES** | Optional | ❌ **NO** (Payer restores allowance locally) |
| **Batch Synchronization** | ❌ NO | ✅ **YES** | ✅ **YES** (Transfers batch to bank) |
| **Atomic Settlement & Ledger** | ❌ NO | ✅ **YES** | ✅ **YES** (Written to central SQLite) |
| **Duplicate / Double-Spend Catch** | ❌ NO | ✅ **YES** | ✅ **YES** (Caught via SQLite SAVEPOINTs) |
| **24-Hour Stale Token Flagging** | ❌ NO | ✅ **YES** | ✅ **YES** (Evaluated by bank clock) |

---

## 📁 Repository Structure

```
d:/Codes/SE/Mini-Project/
├── payer_service.py              # Device 1: Autonomous Payer Service & Dashboard (Port 5001)
├── merchant_service.py           # Device 2: Autonomous Merchant POS Service & Dashboard (Port 5002)
├── bank_service.py               # Device 3: Autonomous Bank Switch & Central Ledger (Port 5003)
├── run_all.py                    # Multi-process local launcher (spawns all 3 services)
├── provision_keys.py             # Trusted identity provisioning CLI tool
├── payer.py                      # Payer device module (exact integer paise math, RSA signing)
├── merchant.py                   # Merchant POS module (local verification, SQLite persistent queue)
├── backend.py                    # Central bank switch (atomic SAVEPOINTs, duplicate detection)
├── crypto_utils.py               # RSA-2048, RSA-PSS, SHA-256, canonical JSON serializer
├── simulation.py                 # 9-scenario command-line demonstration & 1000-token benchmark
├── test_services.py              # Unit & component test suite for independent services (15 tests)
├── test_three_device_integration.py # Live multi-process lifecycle test (kill bank, transfer, sync)
├── test_offline_upi.py           # Core domain cryptographic & security unit test suite (21 tests)
├── test_app.py                   # Integration test suite for Flask routes (7 tests)
├── architecture_audit.md         # In-depth architectural audit of the repository
├── implementation_plan.md        # Technical design & three-device implementation specification
├── requirements.txt              # Cryptography, Flask, Requests
├── templates/
│   ├── index.html                # System portal & multi-device LAN connection guide
│   ├── payer.html                # Customer Payer Device dashboard
│   ├── merchant.html             # Business Owner POS Terminal dashboard
│   └── bank.html                 # Central Bank Switch & Ledger dashboard
└── static/
    ├── css/style.css             # Modern fintech styling & responsive layout
    └── js/
        ├── qrcode.min.js         # Offline client-side QR generator
        ├── payer.js              # Payer UI logic (token signing, QR rendering, spend tracking)
        ├── merchant.js           # Merchant UI logic (local verification, queue, LAN sync)
        └── bank.js               # Bank UI logic (ledger monitor, simulator, duplicate attack demo)
```

---

## 🚀 Quick Start (Local Machine — Single Command)

To test all three services running as separate processes on a single laptop:

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure required environment variables (DEMO ONLY — use strong credentials in production)
# Windows PowerShell:
$env:PAYER_PASSPHRASE="demo-payer-passphrase"
$env:BANK_ADMIN_TOKEN="demo-admin-secret-2026"

# Windows CMD:
set PAYER_PASSPHRASE=demo-payer-passphrase
set BANK_ADMIN_TOKEN=demo-admin-secret-2026

# Linux / macOS:
export PAYER_PASSPHRASE=demo-payer-passphrase
export BANK_ADMIN_TOKEN=demo-admin-secret-2026

# 3. Launch all three independent services
python run_all.py
```

This starts:
- **Device 1 (Payer):** `http://localhost:5001/payer`
- **Device 2 (Merchant):** `http://localhost:5002/merchant`
- **Device 3 (Bank):** `http://localhost:5003/bank`

---

## 🌐 Three Physical Devices Setup Guide (Wi-Fi / LAN)

Follow these steps to deploy across three separate physical computers, laptops, or tablets connected to the same local Wi-Fi or mobile hotspot (**zero internet required**):

### Step 1: Connect to the Same Network
Connect all three devices to the same local Wi-Fi router or phone mobile hotspot. Internet access is **not** required; only a local IP address is needed.

### Step 2: Start Device 3 (Bank Service)
On the computer acting as the Central Bank Switch:
```bash
# Set Bank Admin Token via environment variable (accepted via X-Admin-Token header only)
export BANK_ADMIN_TOKEN=bank-secret-2026   # Linux/macOS
# or: set BANK_ADMIN_TOKEN=bank-secret-2026 (Windows CMD)
# or: $env:BANK_ADMIN_TOKEN="bank-secret-2026" (PowerShell)

python bank_service.py --host 0.0.0.0 --port 5003
```
Find the bank machine's LAN IP address printed in the terminal (e.g. `192.168.1.100`).

### Step 3: Start Device 2 (Merchant POS Terminal)
On the computer/tablet acting as the Merchant POS Terminal:
```bash
python merchant_service.py --host 0.0.0.0 --port 5002 --merchant-id merchant_bob@upi --bank-url http://192.168.1.100:5003
```

### Step 4: Start Device 1 (Customer Payer Device)
On the smartphone or laptop acting as the Customer:
```bash
# Set Payer Passphrase via environment variable (NEVER passed as CLI arg to protect process table)
export PAYER_PASSPHRASE=my-secure-payer-passphrase   # Linux/macOS
# or: set PAYER_PASSPHRASE=my-secure-payer-passphrase (Windows CMD)
# or: $env:PAYER_PASSPHRASE="my-secure-payer-passphrase" (PowerShell)

# Optional: Pin trusted bank public key directly via environment variable
# export BANK_PUBLIC_KEY_PEM="$(cat bank_public_key.pem)"

python payer_service.py --host 0.0.0.0 --port 5001 --payer-id alice@upi --offline-cap 500.0
```

### Step 5: Provision Trusted Public Keys
Before offline transactions begin, provision the customer's public key into the merchant and bank registries:
```bash
# Supply admin token via header (BANK_ADMIN_TOKEN env var is used automatically by provision_keys.py)
python provision_keys.py --payer-id alice@upi --merchant-url http://<MERCHANT_IP>:5002 --bank-url http://192.168.1.100:5003
```
*(If all devices share a filesystem or USB drive, you can also run `python provision_keys.py` locally on the databases).*

---

## 📦 Genuine Disconnected Transfer Demonstration

This procedure demonstrates the primary objective: **generating and accepting a payment when the bank is completely stopped and no network connection exists**.

### Scenario A: Completely Disconnected (JSON File Transfer)
1. **Kill Bank Service:** Press `Ctrl+C` in the Bank terminal (`bank_service.py`). The bank is now completely dead.
2. **Disconnect Wi-Fi (Optional):** Turn off Wi-Fi on Device 1 and Device 2.
3. **Generate Payment on Device 1 (Payer):**
   - Open `http://localhost:5001/payer` (or `http://<PAYER_IP>:5001/payer`).
   - Enter Amount: `₹125.00`, Payee ID: `merchant_bob@upi`.
   - Click **Generate & Sign Offline Token**.
   - Notice the spend counter updates from `₹0.00` to `₹125.00` (Allowance remaining: `₹375.00`).
   - Click **Download JSON File** (saves `offline_token_*.json`).
4. **Transfer File:**
   - Copy `offline_token_*.json` to Device 2 using a USB drive, Bluetooth, or SD card.
5. **Verify on Device 2 (Merchant):**
   - Open `http://localhost:5002/merchant` (or `http://<MERCHANT_IP>:5002/merchant`).
   - Switch to **Upload JSON File** tab and select the file.
   - Click **Verify & Accept Offline Payment**.
   - The token is verified in **< 1 millisecond** using the pre-provisioned trusted public key registry.
   - The token is queued in local SQLite (`merchant_queue_bob_upi.db`) under status `PENDING SYNC`.
6. **Reboot Merchant Terminal:**
   - Terminate `merchant_service.py` (`Ctrl+C`) and start it again.
   - Reopen `/merchant`: The transaction is still safely present in the persistent SQLite queue!
7. **Attempt Sync While Bank Is Stopped:**
   - Click **Sync with Bank Switch**.
   - The sync fails gracefully with an alert: *"Bank network unavailable. Pending payments safely retained in local SQLite."*
   - Zero data is lost.
8. **Restart Bank & Finalize Settlement:**
   - Start `bank_service.py` on Device 3.
   - Reconnect Device 2 to the LAN.
   - On the Merchant dashboard, click **Sync with Bank Switch**.
   - The batch is synchronized and settled atomically!
   - Status updates from `PENDING SYNC` to `SETTLED`.
9. **Verify Double-Spend Defense:**
   - Re-submit the exact same JSON token or click **Trigger Double-Spend Attempt** on the Bank dashboard.
   - The Bank's SQLite `IntegrityError` defense catches and rejects the duplicate immediately with 100% precision!

### Scenario B: QR Code Offline Transfer
- In Step 3 above, the Payer screen generates a standalone QR code using `qrcode.min.js`.
- The merchant can scan this QR code using a barcode reader, or copy the JSON payload using **Copy JSON Token** on the payer device and paste it directly into the Merchant verification box.

---

## 🔒 Security Hardening & Cryptographic Safeguards

This project includes production-grade security patterns adapted for local academic simulation:

### 1. Protected Payer Private-Key Storage (PBKDF2-HMAC-SHA256 + AES-256-GCM)
- **Zero Plaintext Key Storage:** Private-key PEM bytes are never written to disk unencrypted.
- **Key Derivation:** A 256-bit AES key is derived from an operator-supplied passphrase using PBKDF2-HMAC-SHA256 with a unique 16-byte cryptographically secure random salt and **100,000 iterations**.
- **Authenticated Encryption:** Key material is encrypted with AES-256 in Galois/Counter Mode (GCM) using a unique 12-byte nonce, guaranteeing confidentiality and integrity against offline tampering.
- **No Insecure Passphrase Defaults:** Payer state initialization and decryption require an explicit passphrase via `PAYER_PASSPHRASE`. Passphrases are never passed through CLI arguments (avoiding process table exposure). If an existing encrypted state cannot be decrypted, execution halts safely without creating a new identity or leaking secrets.
- **Academic Limitation Note:** In a production mobile OS (Android / iOS), private keys must be hardware-bound within a Secure Enclave or StrongBox Hardware Security Module (HSM).

### 2. Pinned Bank Public Key & Cryptographic Receipt Verification
- **Pinned Trust Anchor:** The Payer device loads the Bank's authoritative public key from trusted local configuration or authenticated provisioning (`BANK_PUBLIC_KEY_PEM`).
- **Zero Trust for Receipt-Embedded Keys:** The Payer **never** accepts a public key embedded inside a receipt package as a trust anchor. Any `bank_public_key` field in untrusted input is discarded.
- **Strict Signature Verification:** Allowance restoration requires valid RSA-PSS signatures computed with the trusted bank key. Forged receipts signed with attacker-generated key pairs are strictly rejected.
- **Legacy Bypass Elimination:** The unauthenticated `settled_token_ids` HTTP path has been removed from `payer_service.py`. No endpoint allows restoring balance or resetting offline spend without verifiable bank signatures.
- **Replay Protection & Cap Integrity:** Replaying valid receipts is idempotent (restores 0 additional allowance), and total restored allowance cannot exceed the ₹500.00 offline cap.

### 3. Authoritative Bank Public-Key Registry & Admin Protection
- **Authoritative Registry:** The Bank serves as the central trust anchor. Registered public keys are stored in the `trusted_payers` SQLite table.
- **Header-Only Administrator Authentication:** All administrative actions (`register_payer`, `register_merchant`, `toggle_network`, `simulate_duplicate`) strictly require the `X-Admin-Token` HTTP header. Tokens in query strings or JSON request bodies are explicitly rejected.
- **Constant-Time Verification:** Tokens are verified using `hmac.compare_digest` to prevent side-channel timing attacks.
- **Mandatory Admin Token:** `BANK_ADMIN_TOKEN` must be explicitly configured; unauthenticated administrative access is blocked with HTTP 401.

### 4. Authenticated Merchant-to-Bank Synchronization
- **API Key Authentication:** Each authorized merchant is provisioned with a secret API key. The Bank stores a salted SHA-256 hash of each merchant key in the `authorized_merchants` table.
- **Batch Sync Verification:** `POST /api/bank/sync_batch` requires the merchant to supply its key in the `X-Merchant-Key` header. Keys are compared in constant time using `hmac.compare_digest` to prevent timing attacks.
- **Payee Binding Enforcement:** The submitting merchant ID must match the `payeeID` of every token in the batch. Batches containing tokens destined for other merchants are rejected.

### 5. Idempotent Settlement with Bank-Signed Receipts
- **Database Uniqueness:** The `settled_tokens` table enforces `PRIMARY KEY (token_id)`.
- **Atomic SAVEPOINTs:** Batch processing uses SQL `SAVEPOINT`s. If a duplicate token is encountered, SQLite raises an `IntegrityError` which rolls back that single token's insertion, flags it as a duplicate in the response, and permits the remainder of the batch to settle atomically.
- **Integer Paise Arithmetic:** All internal calculations use integer paise (₹500.00 = 50,000 paise) to eliminate IEEE-754 floating-point rounding errors.
- **Receipt Persistence:** Every settled token generates an authoritative settlement receipt signed with the Bank's RSA private key and persisted in the `settlement_receipts` table.

---

## 🧹 Clean First-Run Experience & Demo Data Management

The application is configured to start with **clean, empty transaction and queue histories** for a fresh presentation:
- **No Pre-Generated Mock Data:** Dashboards do not display dummy pre-settled payments or fake queue items on startup.
- **Reset Demo Data Feature:** Accessible from the top navigation bar of every dashboard (or via `python reset_demo.py`).
  - **Requires explicit confirmation** via an interactive modal before wiping disposable state.
  - **Coordinated across all three services** (Payer, Merchant, and Bank).
  - **What is cleared:**
    - Payer: Resets unsynced spent balance to ₹0.00 and clears outstanding tokens.
    - Merchant: Clears all local queued payments and sync records from `merchant_queue_bob_upi.db`.
    - Bank: Clears settled ledger tokens, settlement receipts, and sync audit logs from `offline_upi_backend.db`.
  - **What is preserved:**
    - Customer RSA-2048 private key (encrypted via PBKDF2 / AES-256-GCM).
    - Bank core signing RSA-2048 keypair (`primary`).
    - Pre-provisioned trusted payer public key registry (`alice@upi`).
    - Authorized merchant credentials (`merchant_bob@upi`).
    - Database schemas, table structures, and security configurations.
- **Optional "Load Sample Data":** Disabled by default. Can be triggered explicitly from the Technical Details panel on the Bank Dashboard (or via `python sample_data.py`) if sample data is needed for inspection.

---

## 🧭 Redesigned UI & Step-by-Step Demo Guide

The user interface features a consistent fintech design system across all three services with clear cross-service navigation:
- **Title:** **Offline UPI Payment Simulator**
- **Unified Navigation Bar:** Cross-port links dynamically point to:
  - 📱 **Payer Dashboard** (`http://localhost:5001/payer`)
  - 🏪 **Merchant Dashboard** (`http://localhost:5002/merchant`)
  - 🏦 **Bank Dashboard** (`http://localhost:5003/bank`)
- **How to Run a Demo Guide:** A persistent, collapsible 6-step walkthrough embedded in every dashboard.

### Step-by-Step Demonstration Script:
1. **Payer Device (Port 5001):**
   - Check available offline allowance (₹500.00).
   - Enter payment amount (e.g., ₹150.00) and merchant ID (`merchant_bob@upi`).
   - Click **Generate & Sign Offline Token**. The token is cryptographically signed using the on-device RSA-2048 private key.
   - Click **Copy Token JSON** (or view the generated QR code).
2. **Merchant Device (Port 5002):**
   - Paste the token JSON into the input box.
   - Click **Verify & Queue Token Locally**.
   - Observe the sub-second local verification result (&lt; 1 ms) and notice that the payment is stored locally as `Pending Sync`.
3. **Bank Switch (Port 5003):**
   - Open the Bank Dashboard. Section B shows the pending transaction waiting at the Merchant POS.
   - Click **Sync Pending Transactions Now** (or click Sync on the Merchant POS).
   - Observe Section A metrics update and Section C (Master Transaction Ledger) display the newly settled transaction with status `SETTLED`.
4. **Demonstrating Network Outage (Resilience):**
   - In Section E of the Bank Dashboard, click **Simulate Outage** to set the Bank Switch offline.
   - Generate another payment on the Payer device (e.g. ₹80.00) and verify it at the Merchant POS.
   - Notice that the Merchant POS accepts and safely queues the payment in local SQLite while completely offline.
   - Restore the Bank Network to **Online** and click **Sync Pending Transactions Now** to settle the offline queue!
5. **Demonstrating Double-Spend Rejection:**
   - In Section E of the Bank Dashboard, click **Trigger Double-Spend Attempt**.
   - Observe the bank's atomic SAVEPOINT defense catch and reject the duplicate token with 100% precision.

---

## 🌐 Complete REST API Audit & Specification

### Device 1: Customer Payer Service (`payer_service.py` :5001)

| Method | Endpoint | Auth | Description | Status Codes |
| :--- | :--- | :---: | :--- | :---: |
| `GET` | `/health` | None | Service liveness probe. | `200` |
| `GET` | `/api/payer/status` | None | Payer device status, allowance, remaining cap, and outstanding token counts. | `200` |
| `GET` | `/api/payer/public_key` | None | Retrieves payer's registered RSA-2048 public key in PEM format. | `200` |
| `POST` | `/api/payer/generate` | None | Generates and signs an offline payment token bound to a payee. | `200`, `400` |
| `POST` | `/api/payer/export` | None | Marks a created token as `EXPORTED`, locking cancellation. | `200`, `400` |
| `POST` | `/api/payer/cancel` | None | Cancels an unexported token and restores local allowance. | `200`, `400`, `404` |
| `POST` | `/api/payer/reconcile` | None | Reconciles allowance upon validating a Bank-Signed Settlement Receipt. | `200`, `400` |

### Device 2: Merchant POS Service (`merchant_service.py` :5002)

| Method | Endpoint | Auth | Description | Status Codes |
| :--- | :--- | :---: | :--- | :---: |
| `GET` | `/health` | None | Fast service liveness check. | `200` |
| `GET` | `/api/merchant/status` | None | Terminal status, pending queue depth, and settled amounts. | `200` |
| `GET` | `/api/merchant/queue` | None | Inspects local pending offline tokens stored in SQLite. | `200` |
| `POST` | `/api/merchant/receive` | None | Verifies offline token against trusted keys and queues it in SQLite. | `200`, `400` |
| `POST` | `/api/merchant/sync` | None | Submits queued tokens to Bank using authenticated `X-Merchant-Key`. | `200`, `503` |
| `POST` | `/api/merchant/sync_trusted_payers` | None | Updates local trusted key cache from the authoritative Bank registry. | `200`, `503` |
| `POST` | `/api/merchant/purge_settled` | None | Cleans settled tokens older than retention threshold from local queue. | `200` |

### Device 3: Central Bank Switch (`bank_service.py` :5003)

| Method | Endpoint | Auth | Description | Status Codes |
| :--- | :--- | :---: | :--- | :---: |
| `GET` | `/health` | None | Switch liveness check. | `200` |
| `GET` | `/api/bank/status` | None | Central ledger statistics, settled count, and network state. | `200` |
| `GET` | `/api/bank/public_key` | None | Bank's public key in PEM format (used by payers to verify receipts). | `200` |
| `GET` | `/api/bank/trusted_payers` | `X-Admin-Token` | List of all registered payers in authoritative registry. | `200`, `401` |
| `GET` | `/api/bank/transactions/<token_id>` | None | Authoritative settlement status of a specific token. | `200`, `404` |
| `GET` | `/api/bank/receipts/<token_id>` | None | Authoritative signed receipt for a settled token. | `200`, `404` |
| `POST` | `/api/bank/sync_batch` | `X-Merchant-Key` | Receives and settles a batch of offline payment tokens atomically. | `200`, `400`, `401`, `503` |
| `POST` | `/api/bank/register_payer` | `X-Admin-Token` | Registers or authoritatively updates a payer's public key. | `200`, `400`, `401` |
| `POST` | `/api/bank/toggle_network` | `X-Admin-Token` | Toggles simulated network outage for resilience testing. | `200`, `401` |
| `POST` | `/api/bank/simulate_duplicate` | `X-Admin-Token` | Demonstrates double-spend prevention on settled tokens. | `200`, `400`, `401` |

---

## ⚙️ Environment Variables & Configuration

| Variable | Target Service | Required | Description |
| :--- | :--- | :---: | :--- |
| `PAYER_PASSPHRASE` | Payer (:5001) | **Yes** | Passphrase used to derive AES-256-GCM key for encrypted key store. No insecure defaults. Never passed via CLI arguments. |
| `BANK_ADMIN_TOKEN` | Bank (:5003) | **Yes** | Secret administrator token. Required on all administrative endpoints via `X-Admin-Token` header. No insecure defaults. |
| `BANK_PUBLIC_KEY_PEM` | Payer (:5001) | Optional | Authoritative PEM public key of the Bank. When pinned, Payer verifies bank receipts exclusively against this key. |
| `PAYER_ID` | Payer (:5001) | No (default: `alice@upi`) | Virtual Payment Address (VPA) of the customer. |
| `PAYER_OFFLINE_CAP` | Payer (:5001) | No (default: `500.0`) | Maximum cumulative offline allowance (INR). |
| `MERCHANT_ID` | Merchant (:5002) | No (default: `merchant_bob@upi`) | Registered Merchant VPA. |
| `MERCHANT_API_KEY` | Merchant & Bank | No (default: demo key) | Shared secret key for merchant batch synchronization (`X-Merchant-Key`). |
| `BANK_URL` | Merchant (:5002) | No (default: `:5003`) | Target Bank Switch base URL. |

> **Security Requirement:** `PAYER_PASSPHRASE` and `BANK_ADMIN_TOKEN` have no silent fallback defaults in standard execution. Any demo values in example scripts are strictly for automated testing and must be set via local environment variables.

---

## 🧪 Comprehensive Automated Test Suite (70 Tests)

Execute the full automated test suite using standard Python `unittest`:

```bash
python -m unittest discover -v -p "test_*.py"
```

### Verified Test Execution Summary

```
Ran 70 tests in 22.744s

OK (70 Passed, 0 Failed, 0 Skipped)
```

| Test File | Tests | Focus Area & Verified Guarantees |
| :--- | :---: | :--- |
| **`test_services.py`** | 41 | **Independent Services & Security Regression**: Pinned bank public key enforcement, attacker forged receipt rejection with embedded key, missing bank key safe failure, legacy `settled_token_ids` bypass removal, unauthenticated balance reset rejection, plaintext key state absence verification, passphrase requirement enforcement, header-only `X-Admin-Token` rejection in query/body, constant-time `hmac.compare_digest` auth, partial reconciliation with multi-token reservations, ₹500.00 cap and duplicate-settlement replay protection, thread-safe batch settlement with SQLite savepoints, and full REST API verification. |
| **`test_offline_upi.py`** | 21 | **Core Domain & Cryptography**: RSA-PSS signatures, payee binding, NaN/infinity/negative amount rejects, tamper detection, attacker forged keypair rejection, SQLite queue persistence across reboot, and atomic SAVEPOINT duplicate defense. |
| **`test_app.py`** | 7 | **Web Dashboards & Routes**: Portal rendering, dashboard status routes, LAN IP auto-discovery, interactive simulation controls, and idempotent key initialization. |
| **`test_three_device_integration.py`** | 1 | **Live Multi-Process 3-Device Lifecycle (11 Phases)**: Completely terminates Bank process (`taskkill`), generates signed token offline on Payer, terminates Payer, verifies & queues offline on Merchant, reboots Merchant terminal (verifies SQLite queue persistence), verifies graceful 503 during bank outage, restarts Bank process, performs authenticated LAN sync, verifies 100% duplicate rejection on replay, queries signed receipt, and authoritatively reconciles Payer allowance back to ₹500.00. |

---

## 🛡️ Git History & Clean State Management

This repository's `.gitignore` has been updated to permanently ignore:
- SQLite databases (`*.db`, `*.sqlite3`)
- Encrypted and plaintext payer state files (`payer_state_*.json`)
- Cryptographic key material (`*.pem`, `*.key`, `*.priv`)
- Environment files (`.env`, `.env.*`)

### Untracking Sensitive Files
If sensitive or generated files were previously committed to Git in earlier development iterations, remove them from tracking without deleting local files using:

```bash
git rm --cached --ignore-unmatch *.db *.sqlite3 payer_state_*.json *.pem *.key .env*
git commit -m "chore: untrack generated state, databases, and local key files"
```

### Complete History Scrubbing (Reference)
To purge sensitive files from past commits across the entire history, use [git-filter-repo](https://github.com/newren/git-filter-repo):

```bash
pip install git-filter-repo
git filter-repo --invert-paths --path-glob "*.db" --path-glob "payer_state_*.json" --path-glob "*.pem"
```

---

## ⚠️ Academic Assumptions & Known Limitations

1. **Software vs. Hardware Keystore:** Private keys are stored locally using authenticated AES-256-GCM encryption derived via PBKDF2. While secure against casual filesystem inspection, a true mobile payment application relies on hardware-backed Keymaster / StrongBox / Secure Enclave chips that prevent memory dumping and key extraction even on rooted devices.
2. **Pre-Shared Merchant Credentials:** Merchants authenticate using shared API keys registered in the bank switch. A production banking infrastructure would use Mutual TLS (mTLS) with X.509 device certificates issued by an authorized financial PKI.
3. **Clock Skew Tolerance:** Offline token verification allows a 5-minute clock skew window. In production, tamper-evident hardware RTC clocks or synchronized cellular network time are required to prevent time-tampering attacks.
4. **Risk Boundary:** The ₹500.00 limit strictly caps financial liability to ₹500.00 per compromised/cloned device before online reconciliation catches any discrepancies.
