"""
Offline / Low-Connectivity UPI Payment System - Flask Application
Serves three distinct dashboards:
  1. /payer    - Customer Device (Token generation, cap enforcement, QR/JSON handoff)
  2. /merchant - Business Owner Terminal (Sub-second local verification, SQLite queue, delayed sync)
  3. /bank     - Central Bank Switch (Ledger, 100% duplicate detection, network simulator)
"""

import os
import socket
import time
from typing import Dict, Any, List
from flask import Flask, render_template, request, jsonify

from payer import PayerDevice, OfflineSpendLimitExceededError
from merchant import MerchantDevice
from backend import BackendServer
from crypto_utils import sign_payload

app = Flask(__name__)

# System Persistence Files
PAYER_STATE_FILE = "payer_state_alice_upi.json"
MERCHANT_DB_FILE = "merchant_queue_bob_upi.db"
BACKEND_DB_FILE = "offline_upi_backend.db"

# Initialize Core Domain Modules
payer = PayerDevice(payer_id="alice@upi", offline_cap=500.0, state_file=PAYER_STATE_FILE)
merchant = MerchantDevice(merchant_id="merchant_bob@upi", queue_db_path=MERCHANT_DB_FILE)
backend = BackendServer(db_path=BACKEND_DB_FILE)

# Provision Trusted Payer Registry at Merchant and Backend
merchant.register_payer(payer.payer_id, payer.public_key_pem)
backend.register_payer(payer.payer_id, payer.public_key_pem)

# Simulated Network Status (True = Online, False = Outage/Disconnected)
network_online = True


def get_lan_ip() -> str:
    """Discovers the host machine's primary local IP address on the Wi-Fi/LAN."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Using Google DNS address as a dummy destination to determine outbound network interface
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# =========================================================================
# HTML Page Routes (Three Distinct Dashboards + Portal)
# =========================================================================

@app.route("/")
def index_portal():
    return render_template("index.html")


@app.route("/payer")
def payer_dashboard():
    return render_template("payer.html")


@app.route("/merchant")
def merchant_dashboard():
    return render_template("merchant.html")


@app.route("/bank")
def bank_dashboard():
    return render_template("bank.html")


# =========================================================================
# System & LAN Info API
# =========================================================================

@app.route("/api/network_info", methods=["GET"])
def get_network_info():
    return jsonify({
        "lan_ip": get_lan_ip(),
        "port": 5000,
        "network_online": network_online
    })


# =========================================================================
# Payer APIs
# =========================================================================

@app.route("/api/payer/status", methods=["GET"])
def get_payer_status():
    outstanding_list = [
        {
            "token_id": tid,
            "payee_id": info.get("payee_id", "merchant_bob@upi"),
            "amount": info.get("amount", 0.0),
            "timestamp": info.get("timestamp", time.time()),
            "status": "OUTSTANDING"
        }
        for tid, info in payer.outstanding_tokens.items()
    ]
    # Sort newest first
    outstanding_list.sort(key=lambda x: x["timestamp"], reverse=True)

    return jsonify({
        "payer_id": payer.payer_id,
        "payer_name": "Alice",
        "offline_cap": payer.offline_cap,
        "unsynced_spent": round(payer.unsynced_spent, 2),
        "remaining_allowance": round(payer.remaining_offline_allowance, 2),
        "outstanding_count": len(payer.outstanding_tokens),
        "transactions": outstanding_list
    })


@app.route("/api/payer/generate", methods=["POST"])
def generate_payer_payment():
    data = request.get_json() or {}
    amount = data.get("amount")
    payee_id = data.get("payee_id", "").strip()

    if not payee_id:
        return jsonify({"error": "Payee / Merchant ID is required for signed binding."}), 400

    try:
        amt = float(amount)
    except (ValueError, TypeError):
        return jsonify({"error": "Invalid amount format."}), 400

    try:
        token_package = payer.generate_token(amount=amt, payee_id=payee_id)
        return jsonify({
            "success": True,
            "token_package": token_package,
            "unsynced_spent": payer.unsynced_spent,
            "remaining_allowance": payer.remaining_offline_allowance
        })
    except OfflineSpendLimitExceededError as e:
        return jsonify({"error": str(e)}), 400
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": f"Internal error: {str(e)}"}), 500


@app.route("/api/payer/cancel", methods=["POST"])
def cancel_payer_token():
    data = request.get_json() or {}
    token_id = data.get("token_id")
    if not token_id:
        return jsonify({"error": "token_id is required."}), 400

    success = payer.cancel_failed_token(token_id)
    if success:
        return jsonify({
            "success": True,
            "unsynced_spent": payer.unsynced_spent,
            "remaining_allowance": payer.remaining_offline_allowance
        })
    return jsonify({"error": f"Token '{token_id}' not found among outstanding tokens."}), 404


# =========================================================================
# Merchant APIs
# =========================================================================

@app.route("/api/merchant/status", methods=["GET"])
def get_merchant_status():
    summary = merchant.summary()

    # Query full queue list from local SQLite
    cursor = merchant.conn.cursor()
    cursor.execute("""
        SELECT token_id, payer_id, payee_id, amount, status, received_at, verification_time_ms
        FROM local_queue
        ORDER BY received_at DESC
    """)
    queue_rows = [dict(row) for row in cursor.fetchall()]

    return jsonify({
        "merchant_id": merchant.merchant_id,
        "total_queued": summary["total_queued"],
        "pending_sync": summary["pending_sync"],
        "settled": summary["settled"],
        "pending_sync_amount": summary["pending_sync_amount"],
        "bank_reachable": network_online,
        "queue": queue_rows
    })


@app.route("/api/merchant/receive", methods=["POST"])
def receive_merchant_payment():
    data = request.get_json() or {}
    token_pkg = data.get("token_package")
    if not token_pkg:
        return jsonify({"accepted": False, "status": "REJECTED", "reason": "Missing token package."}), 400

    result = merchant.verify_and_accept_token(token_pkg)
    return jsonify(result)


@app.route("/api/merchant/sync", methods=["POST"])
def sync_merchant_with_bank():
    global network_online
    if not network_online:
        return jsonify({
            "error": "Bank network unavailable.",
            "message": "Central banking switch is currently simulated offline. Pending tokens are safely retained in merchant local SQLite."
        }), 503

    pending_tokens = merchant.get_pending_sync_tokens()
    if not pending_tokens:
        return jsonify({
            "message": "No pending tokens in local queue to sync.",
            "settled_count": 0,
            "duplicate_count": 0,
            "flagged_count": 0,
            "processing_time_ms": 0.0,
            "settled": []
        })

    sync_report = backend.sync_batch(merchant.merchant_id, pending_tokens)

    # Mark settled on merchant local queue
    settled_ids = [item["token_id"] for item in sync_report["settled"]]
    merchant.mark_tokens_synced(settled_ids)

    # Reconcile on payer device if matching outstanding tokens
    payer.reconcile_settled_tokens(settled_ids)

    return jsonify(sync_report)


@app.route("/api/merchant/purge_settled", methods=["POST"])
def purge_merchant_settled():
    removed_count = merchant.remove_synced_tokens()
    return jsonify({"success": True, "removed_count": removed_count})


# =========================================================================
# Bank APIs
# =========================================================================

@app.route("/api/bank/status", methods=["GET"])
def get_bank_status():
    cursor = backend.conn.cursor()

    # Ledger tokens
    cursor.execute("""
        SELECT token_id, payer_id, payee_id, amount, token_timestamp, token_age_hours, status, review_reason, settled_at
        FROM settled_tokens
        ORDER BY settled_at DESC
    """)
    ledger = [dict(row) for row in cursor.fetchall()]

    # Batches
    cursor.execute("SELECT * FROM sync_audit_log ORDER BY processed_at DESC LIMIT 10")
    batches = [dict(row) for row in cursor.fetchall()]

    # Trusted Payers
    cursor.execute("SELECT payer_id, public_key_pem FROM trusted_payers")
    trusted_payers = [dict(row) for row in cursor.fetchall()]

    # Metrics
    cursor.execute("SELECT COUNT(*) AS c, SUM(amount) AS v FROM settled_tokens WHERE status = 'SETTLED'")
    settled_row = cursor.fetchone()
    settled_count = settled_row["c"] or 0
    total_settled_value = settled_row["v"] or 0.0

    cursor.execute("SELECT COUNT(*) AS c FROM settled_tokens WHERE status = 'FLAGGED_FOR_REVIEW'")
    flagged_count = cursor.fetchone()["c"] or 0

    cursor.execute("SELECT SUM(duplicate_count) AS d, SUM(rejected_count) AS r, SUM(total_tokens) AS t FROM sync_audit_log")
    audit_sums = cursor.fetchone()
    duplicate_count = audit_sums["d"] or 0
    rejected_count = audit_sums["r"] or 0
    total_received = audit_sums["t"] or 0

    return jsonify({
        "network_online": network_online,
        "settled_count": settled_count,
        "total_settled_value": round(float(total_settled_value), 2),
        "duplicate_count": duplicate_count,
        "flagged_count": flagged_count,
        "rejected_count": rejected_count,
        "total_received": total_received,
        "trusted_payers": trusted_payers,
        "recent_batches": batches,
        "ledger": ledger
    })


@app.route("/api/bank/toggle_network", methods=["POST"])
def toggle_network():
    global network_online
    network_online = not network_online
    return jsonify({"network_online": network_online})


@app.route("/api/bank/simulate_duplicate", methods=["POST"])
def simulate_duplicate_submission():
    """
    Takes an existing settled token and submits it again to verify double-spend rejection.
    If no settled tokens exist yet, mints a test token, settles it, and replays it.
    """
    cursor = backend.conn.cursor()
    cursor.execute("SELECT token_id, payer_id, payee_id, amount, token_timestamp, signature FROM settled_tokens LIMIT 1")
    row = cursor.fetchone()

    if not row:
        # Create and settle one token first
        t = payer.generate_token(10.0, payee_id=merchant.merchant_id)
        backend.sync_batch(merchant.merchant_id, [t])
        target_token = t
    else:
        # Construct token package using exact original DB record
        target_token = {
            "payload": {
                "tokenID": row["token_id"],
                "payerID": row["payer_id"],
                "payeeID": row["payee_id"],
                "amount": row["amount"],
                "timestamp": row["token_timestamp"]
            },
            "signature": row["signature"],
            "payer_public_key": payer.public_key_pem
        }

    # Re-submit to trigger double-spend rejection
    report = backend.sync_batch(merchant.merchant_id, [target_token])

    if report["duplicates"]:
        return jsonify({
            "success": True,
            "token_id": target_token["payload"]["tokenID"],
            "result": report["duplicates"][0]
        })

    return jsonify({"error": "Duplicate was unexpectedly not caught."}), 500


@app.route("/api/bank/register_payer", methods=["POST"])
def register_bank_payer():
    data = request.get_json() or {}
    pid = data.get("payer_id")
    pem = data.get("public_key_pem")
    if not pid or not pem:
        return jsonify({"error": "payer_id and public_key_pem required."}), 400
    backend.register_payer(pid, pem)
    merchant.register_payer(pid, pem)
    return jsonify({"success": True, "payer_id": pid})


if __name__ == "__main__":
    lan_ip = get_lan_ip()
    port = 5000
    print("\n" + "=" * 80)
    print("  OFFLINE UPI PAYMENT SYSTEM — THREE-DEVICE DASHBOARD APPLICATION")
    print("=" * 80)
    print(f"  Local Access:      http://localhost:{port}")
    print(f"  LAN Access:        http://{lan_ip}:{port}")
    print(f"  Payer Dashboard:   http://{lan_ip}:{port}/payer")
    print(f"  Merchant Terminal: http://{lan_ip}:{port}/merchant")
    print(f"  Bank Network:      http://{lan_ip}:{port}/bank")
    print("=" * 80)
    print("  Zero Internet Required. Connect multiple devices to the same local Wi-Fi.")
    print("=" * 80 + "\n")
    app.run(host="0.0.0.0", port=port, debug=False)
