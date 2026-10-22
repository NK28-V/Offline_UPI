# Offline UPI Demo

This academic prototype demonstrates a trustless, delayed-settlement offline payment protocol modeled on UPI. Customer devices mint RSA-2048 cryptographically signed payment tokens (IOUs) completely offline within an enforced ₹500 spending cap (50,000 paise). Merchants verify signatures and payee binding in sub-second latency against locally cached public keys, queuing accepted tokens in SQLite. When network connectivity is restored, the merchant synchronizes batches with the central Bank switch, which executes atomic double-spend prevention and batch settlement via SQLite savepoints.

## Architecture

```text
+-----------------------+                    +-----------------------+                    +-----------------------+
|     Customer Payer    |                    |      Merchant POS     |                    |      Bank Switch      |
|      (Port 5001)      |                    |      (Port 5002)      |                    |      (Port 5003)      |
+-----------------------+                    +-----------------------+                    +-----------------------+
            |                                            |                                            |
            |   1. Offline QR / JSON Token               |                                            |
            |   (Zero Network Required)                  |                                            |
            +------------------------------------------->|                                            |
            |                                            |   2. Delayed Batch Sync (LAN HTTP)         |
            |                                            +------------------------------------------->|
            |                                            |                                            |  Atomic Savepoint
            |                                            |   3. Settlement Confirmation               |  Double-Spend Check
            |                                            |<-------------------------------------------+
            |   4. Reconciliation from Bank              |
            +---------------------------------------------------------------------------------------->|
            |<----------------------------------------------------------------------------------------+
```

## Installation

Prerequisites: Python 3.9+

```bash
pip install flask cryptography
```

## Running the System

### Single Machine (Localhost)
Runs all three independent roles as separate processes on ports 5001, 5002, and 5003:
```bash
python server.py all
```
Open in separate browser tabs:
- **Payer Dashboard**: http://127.0.0.1:5001/
- **Merchant POS**: http://127.0.0.1:5002/
- **Bank Switch**: http://127.0.0.1:5003/

### Three Separate Devices (LAN Setup)
1. Find the host machine's LAN IP (run `ipconfig` on Windows or `ip a` on Linux/macOS, e.g. `192.168.1.50`).
2. Run on Device 1 (Bank):
   ```bash
   python server.py bank --port 5003
   ```
3. Run on Device 2 (Merchant):
   ```bash
   python server.py merchant --port 5002 --bank-url http://192.168.1.50:5003
   ```
4. Run on Device 3 (Payer):
   ```bash
   python server.py payer --port 5001 --bank-url http://192.168.1.50:5003
   ```

## User Workflows

### 1. Bank Operator
1. Starts the switch; the bank automatically initializes an RSA-2048 signing keypair and SQLite ledger.
2. Monitors incoming payer registrations and merchant POS connections in real time.
3. Tests network failure resilience using the "Simulate Outage" toggle.
4. Triggers remote merchant batch synchronizations with "Sync Pending Payments Now".

### 2. Merchant (POS Shop)
1. Enters a shop ID (e.g., `chai_corner@upi`) and registers with the Bank switch to obtain an authenticated API key.
2. Clicks "Refresh Trusted Payers" to download customer public keys into local SQLite cache.
3. Accepts customer tokens offline by pasting JSON or loading `.json` files, verifying signatures locally with sub-second latency. If the payer registered after the shop, the POS fetches the new payer keys automatically when the bank is reachable.
4. Clicks "Sync with Bank" once connectivity returns to settle pending transactions.

### 3. Customer / Payer
1. Enters a UPI ID (e.g., `ravi@upi`) and registers their public key with the Bank to activate an offline wallet.
2. Enters a payment amount (e.g., ₹120.00) and Merchant UPI ID to mint a cryptographically signed token.
3. Observes offline allowance decrease (₹500.00 cap tracked in exact paise).
4. Presents the generated QR code or clicks "Copy JSON" to hand off payment to the merchant. Click **View** on any row in "Recent Offline Payments" to see that token's QR and JSON again. If auto-copy is blocked (e.g. when opened via a LAN IP), select the JSON box and press Ctrl+C.
5. Clicks "Refresh from Bank" when online to reconcile settled tokens and restore available spending limit.
6. Can click "Request cancel" on an unsynced payment. This is only a *request*; see "Cancel Rules" below.

## 6-Step End-to-End Demo Script

1. **Register Payer and Merchant**:
   - Open http://127.0.0.1:5001/, register with UPI ID `ravi@upi`.
   - Open http://127.0.0.1:5002/, register with Shop ID `chai_corner@upi`.
   - On Merchant page, click "Refresh Trusted Payers" (cache count becomes 1).
2. **Simulate Outage**:
   - Open http://127.0.0.1:5003/, click "Simulate Outage" (status changes to OFFLINE).
3. **Pay Offline**:
   - On Payer page, enter Amount `120` and Payee `chai_corner@upi`, then click "Generate Signed Payment QR".
   - Remaining allowance drops to ₹380.00. Click "Copy JSON".
4. **Merchant Verifies Offline**:
   - On Merchant page, paste the token JSON into the textarea and click "Verify & Accept Offline".
   - The token is verified offline against the cached public key and saved under status `PENDING`.
5. **Restore Bank & Settle**:
   - On Bank page, click "Restore Network" (status returns to ONLINE).
   - On Merchant page, click "Sync with Bank". The payment moves from `PENDING` to `SETTLED`.
   - On Payer page, click "Refresh from Bank"; the settled ₹120 is cleared, restoring allowance to ₹500.00.
6. **Simulate Double-Spend**:
   - On Bank page, click "Simulate Double-Spend".
   - The bank switch detects the replayed token ID and catches the violation atomically (`DUPLICATE`).

7. **Cancel Race (edge case)**:
   - Bank: "Simulate Outage". Payer: pay ₹50 to `chai_corner@upi`. Merchant: verify and accept it.
   - Payer: click "Request cancel". The status becomes `CANCEL PENDING` and the ₹50 **stays blocked**.
   - Bank: "Restore Network". Then **whoever reaches the bank first wins**:
     - Merchant clicks "Sync with Bank" first: the payment is `SETTLED` and the payer's cancel is refused.
     - Payer clicks "Refresh from Bank" first: the bank marks it `VOIDED`, the payer gets ₹50 back, and the merchant's later sync shows `CANCELLED BY PAYER`.

## Cancel Rules

Offline, the payer cannot know whether the merchant has already synced, so the **bank decides**:

| Payer token status | Counts against ₹500? | Meaning |
|---|---|---|
| `CREATED` | Yes | Signed and handed over, not yet seen by the bank |
| `CANCEL_PENDING` | Yes | Cancel requested while the bank was unreachable |
| `CANCELLED` | No | Bank confirmed the void (`VOIDED` in the ledger) |
| `SETTLED` / `FLAGGED` | No | Bank received it from the merchant; the money is gone |

**Offline limit is not a balance.** The ₹500 is how much you may owe the bank *offline* at once. When a payment is `SETTLED`, the bank has debited your account, so that amount stops counting against the limit. The limit goes back up to ₹500, but "Total paid" goes up too. That is not a refund. Only `CANCELLED` (bank-confirmed void) is a refund.

Allowance is freed **only** by a bank-signed answer: the cancel response or the signed statement. The bank ledger's `token_id` PRIMARY KEY makes "settle" and "cancel" mutually exclusive, so one token can never be both paid and refunded.

## Testing

```bash
python -m unittest test_core -v
```

There are 14 tests:
- ₹500 cap boundaries (₹0, ₹0.01, exactly ₹500, ₹500.01);
- bad input such as `"abc"`, floats, future timestamps and garbage JSON;
- a tampered amount, a wrong payee and replay;
- the cancel race both ways;
- an idempotent refresh;
- blocked ID takeover, double-spend, and a forged cancel or bank statement.

## Resetting the Demo

Stop the servers and delete the `*.db` files in the project folder. IDs are first-come-first-served at the bank, so to re-register an existing ID you must reset `bank_5003.db` too.

## File Map

- `core.py`: Pure domain logic (RSA-2048 crypto, canonical JSON, Payer wallet, Merchant POS, and Bank switch classes).
- `server.py`: Unified Flask application and multi-process CLI runner (`payer`, `merchant`, `bank`, `all`).
- `templates/index.html`: Unified Jinja HTML template with embedded responsive design system for all three roles.
- `static/app.js`: Client-side logic for offline QR generation, clipboard handling, and API integration.
- `static/qrcode.min.js`: Vendored client-side QR code generator (works 100% offline).
- `test_core.py`: Unit tests for the core protocol (standard-library `unittest`, no extra install).
- `README.md`: System documentation, architecture diagram, and operational guides.

## Known Limitations

- **Academic Prototype**: Designed for protocol demonstration; not hardened for production financial environments.
- **Unencrypted Keys on Disk**: Private RSA keys and SQLite databases are stored in plaintext on the local filesystem (production deployments would use hardware keystores / Secure Enclave and password encryption).
- **Offline cancel is a request, not a guarantee**: if the merchant syncs first, the payment stands. This mirrors real UPI Lite, where a handed-over payment cannot be pulled back.
- **IDs are first-come-first-served**: the bank refuses (409) a second registration of the same payer or merchant ID. There is no account recovery flow.
- **Outage simulation** blocks merchant sync, payer cancel and payer statements. Registration still works so setup stays easy.
- **Simulated Settlement**: Ledger updates reflect cryptographic agreement and double-spend tracking; no actual fiat or central bank money movement occurs.
