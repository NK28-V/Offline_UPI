# Offline / Low-Connectivity UPI Payment System — Multi-Device Dashboard Simulation

An academic core-mechanics simulation of an **Offline / Low-Connectivity UPI Payment Protocol** featuring a complete, functional web application with three distinct dashboards for separate devices connected over a local Wi-Fi / LAN network.

---

## 🏛️ System Architecture

```
                  +--------------------------------------------------------------+
                  |                  Local Network (Wi-Fi / LAN)                 |
                  +--------------------------------------------------------------+
                                                 |
         +---------------------------------------+---------------------------------------+
         |                                                                               |
         v                                                                               v
+-------------------------------+   Offline Handoff (QR / JSON File)   +-------------------------------+
|        Payer Dashboard        | ===================================> |       Merchant Dashboard      |
|            (/payer)           |                                      |          (/merchant)          |
|  - Customer: Alice            |                                      |  - Business: Bob's POS        |
|  - On-device RSA-2048 signing |                                      |  - Local sub-second verify    |
|  - ₹500.00 offline spend cap  |                                      |  - Persistent SQLite queue    |
|  - Payee binding enforcement  |                                      |  - Status: "Pending Sync"     |
+-------------------------------+                                      +-------------------------------+
                                                                                       |
                                                            Online Sync Attempt        |
                                                            (When Network Available)   v
                                                                       +-------------------------------+
                                                                       |         Bank Dashboard        |
                                                                       |             (/bank)           |
                                                                       |  - Central Switch & Ledger    |
                                                                       |  - 100% duplicate rejection   |
                                                                       |  - 24h stale token auditing   |
                                                                       |  - Network online/offline sim |
                                                                       +-------------------------------+
```

---

## 📁 Project Structure

```
d:/Codes/SE/Mini-Project/
├── app.py                 # Flask server with REST APIs for Payer, Merchant, and Bank
├── payer.py               # Payer Device module (cap tracking, RSA signing, payee binding, persistence)
├── merchant.py            # Merchant Device module (trusted registry lookup, sub-second verify, SQLite queue)
├── backend.py             # Central Backend module (trusted registry, atomic SAVEPOINT duplicate checks, 24h aging)
├── crypto_utils.py        # RSA-2048 keygen, canonical JSON encoding, PSS/SHA-256, strict payload validation
├── simulation.py          # End-to-end command-line simulation runner (11 core scenarios)
├── test_offline_upi.py    # Unit & security test suite (21 automated tests)
├── test_app.py            # Integration test suite for Flask routes and API workflows (7 automated tests)
├── requirements.txt       # Project dependencies (Flask, Cryptography)
├── templates/
│   ├── index.html         # System portal & LAN connection guide
│   ├── payer.html         # Customer Payer Device dashboard
│   ├── merchant.html      # Business Owner POS Terminal dashboard
│   └── bank.html          # Central Bank Switch & Ledger dashboard
└── static/
    ├── css/
    │   └── style.css      # Clean modern fintech styling (responsive, accessible status badges)
    └── js/
        ├── qrcode.min.js  # Standalone client-side offline QR generator
        ├── payer.js       # Payer UI logic (token creation, QR rendering, spend tracking)
        ├── merchant.js    # Merchant UI logic (local verification, persistent queue, sync)
        └── bank.js        # Bank UI logic (ledger monitoring, network simulator, duplicate attack demo)
```

---

## ⚡ Quick Start

### 1. Prerequisites
- Python 3.9+
- Install dependencies:
  ```bash
  pip install -r requirements.txt
  ```

### 2. Launch the Application
```bash
python app.py
```
By default, the server binds to `0.0.0.0:5000` so it can be accessed locally and by other devices on your local Wi-Fi/LAN.

### 3. Open the Dashboards
Open the following URLs in separate browser tabs, windows, or on separate physical devices:
- **System Portal:** `http://localhost:5000/`
- **Payer Device:** `http://localhost:5000/payer`
- **Merchant Device:** `http://localhost:5000/merchant`
- **Bank Network:** `http://localhost:5000/bank`

---

## 🌐 Running on Three Physical Devices (Wi-Fi / LAN)

You can run this demonstration across three physical devices (e.g. laptop + smartphone 1 + smartphone 2) without any active internet connection:

1. **Connect Devices to Same Wi-Fi / Hotspot:**
   - Connect your host laptop and the other devices to the same local Wi-Fi router or phone mobile hotspot. **No internet connection is needed.**
2. **Find Host IP:**
   - The terminal output of `python app.py` and the portal page at `http://localhost:5000/` will automatically display the host's LAN IP address (e.g. `http://192.168.1.15:5000`).
3. **Open Each Role on a Device:**
   - **Device 1 (Customer Phone):** Open `http://<HOST_LAN_IP>:5000/payer`
   - **Device 2 (Merchant POS / Tablet):** Open `http://<HOST_LAN_IP>:5000/merchant`
   - **Device 3 (Bank / Host Laptop):** Open `http://<HOST_LAN_IP>:5000/bank`

---

## 🔄 Two Demonstration Modes

### Mode 1 — Completely Disconnected Payment Transfer
1. **Generate Token on Payer Device:**
   - On the **Payer Dashboard** (`/payer`), enter Amount: `₹150.00` and Payee ID: `merchant_bob@upi`.
   - Click **Generate & Sign Offline Token**.
   - An on-screen QR code and token package are generated.
2. **Offline Handoff:**
   - Scan the QR code or click **Download JSON File** / **Copy JSON Token**.
3. **Merchant Local Verification:**
   - On the **Merchant Dashboard** (`/merchant`), paste the JSON token or upload the file.
   - Click **Verify & Accept Offline Payment**.
   - The merchant verifies the cryptographic signature locally in **< 1 millisecond** using the pre-provisioned trusted public key registry.
   - The token is queued as `PENDING SYNC` in local SQLite (`merchant_queue_bob_upi.db`).

### Mode 2 — Bank Outage & Delayed Synchronization
1. **Simulate Network Outage:**
   - On the **Bank Dashboard** (`/bank`), click **Toggle Network State** &rarr; status turns `OFFLINE (SIMULATED)`.
2. **Attempt Sync While Disconnected:**
   - On the **Merchant Dashboard** (`/merchant`), click **Sync with Bank Switch**.
   - The sync fails gracefully with an alert: *"Bank network unavailable. Pending payments safely retained in local SQLite."*
   - No queued tokens are lost.
3. **Restore Network & Settle:**
   - On the **Bank Dashboard** (`/bank`), toggle status back to `ONLINE`.
   - On the **Merchant Dashboard**, click **Sync with Bank Switch**.
   - The batch is synchronized and settled atomically.
   - Tokens on the merchant screen update from `PENDING SYNC` to `SETTLED`.
   - The customer's offline spending allowance is automatically restored!

---

## 🛡️ Security Mechanics & Edge Cases

| Feature | Enforcement Mechanism | Failure Outcome |
| :--- | :--- | :--- |
| **Tamper-Evident IOUs** | RSA-2048 with PSS padding & SHA-256 over canonical JSON | Altering amount or ID invalidates digital signature. |
| **Trusted Payer Registry** | Merchant and backend check pre-provisioned public keys | Unregistered payer or key forgery is rejected (`REJECTED: Unknown payer identity`). |
| **Strict Payee Binding** | Mandatory `payeeID` field in signed payload | Token presented to wrong merchant or submitted by wrong merchant is rejected (`REJECTED: Payee mismatch`). |
| **Safe Offline Spend Cap** | Cumulative unsynced spend counter capped at ₹500.00 | Exact ₹500.00 allowed; ₹500.01 is blocked (`Offline limit exceeded`). |
| **100% Double-Spend Defense** | SQLite `PRIMARY KEY` uniqueness with `SAVEPOINT`s and `IntegrityError` handling | Replaying previously settled token ID is rejected (`Double-spend detected`). |
| **Stale Token Expiry** | Check if `timestamp > 24 hours` old | Token is NOT settled automatically; flagged as `FLAGGED_FOR_REVIEW` for manual audit. |
| **Crash Recovery** | SQLite queue for merchant; persistent JSON state for payer | Queued tokens and spend allowance survive device reboots. |

---

## 🧪 Testing & Verification

Run the automated test discovery command to execute all 28 tests:

```bash
python -m unittest discover -v -p "test_*.py"
```

### Test Suite Summary (28 Tests Total)
- **`test_app.py` (7 Integration Tests):**
  - Page rendering for `/`, `/payer`, `/merchant`, `/bank`.
  - Network discovery endpoint (`/api/network_info`).
  - Payer token generation and ₹500 spend cap enforcement.
  - Merchant local verification and queueing without contacting bank.
  - Payee mismatch rejection.
  - Network outage simulation (503 status code and queue retention).
  - Bank duplicate simulation endpoint.
- **`test_offline_upi.py` (21 Unit & Security Tests):**
  - FR-1 through FR-8 and NFR-1 through NFR-6.
  - Unknown payer rejection by merchant and backend.
  - Identity spoofing with attacker keypair.
  - Altered payload and altered signature detection.
  - Payee mismatch rejection at POS and backend switch.
  - Strict input validation (NaN, infinity, zero, negative, future timestamps).
  - Spend cap boundary (₹500.00 allowed, ₹500.01 blocked).
  - Failed transfer cancellation (`cancel_failed_token`).
  - Repeated reconciliation protection.
  - State persistence across payer and merchant process restarts.
  - In-batch and cross-batch atomic duplicate detection.

### Run Console Simulation
```bash
python simulation.py
```
Executes all 9 terminal scenarios and benchmarks a 1,000-token batch settlement.

---

## ⚠️ Academic Assumptions & Limitations

1. **Hardware Keystore:** Private keys are stored in a local state JSON file for simulation purposes. In production, keys would be non-exportable and guarded inside an Android StrongBox or Apple Secure Enclave.
2. **Offline Key Discovery:** The simulation pre-provisions payer public keys in the merchant's local SQLite registry. A production offline scheme would utilize digital identity certificates signed by a central root CA (such as NPCI).
3. **Clock Skew:** A 5-second clock skew tolerance is permitted for timestamp validation. In production, synchronized secure hardware RTCs are necessary to avoid clock manipulation attacks.
