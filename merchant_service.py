"""
Merchant Service (Device 2) — Business Owner POS Terminal
Operates independently on the merchant device/terminal.
Verifies signed offline payment tokens locally in sub-second latency using its persistent SQLite trusted registry,
enforces strict payee binding, queues verified transactions in a persistent local SQLite database under 'Pending Sync',
and synchronizes with the central Bank Service via authenticated HTTP LAN requests only when connectivity is available.
Never contacts the bank or payer during offline token acceptance.
"""

import os
import sys
import socket
import argparse
import time
import requests
from typing import Dict, Any, List, Optional
from flask import Flask, render_template, request, jsonify

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from merchant import MerchantDevice
from reset_demo import reset_merchant_db, reset_all_demo_data
from sample_data import load_sample_demo_data


def get_lan_ip() -> str:
    """Discovers the host machine's primary local IP address on the Wi-Fi/LAN."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


def create_merchant_app(
    merchant_id: str = "merchant_bob@upi",
    api_key: Optional[str] = None,
    queue_db_path: str = "merchant_queue_bob_upi.db",
    bank_url: str = "http://localhost:5003",
    trusted_registry: Optional[Dict[str, str]] = None
) -> Flask:
    """Factory creating the Merchant Service Flask application."""
    app = Flask(__name__, template_folder="templates", static_folder="static")

    merchant = MerchantDevice(
        merchant_id=merchant_id,
        trusted_registry=trusted_registry,
        queue_db_path=queue_db_path
    )

    configured_api_key = api_key or os.getenv("MERCHANT_API_KEY", "merchant-bob-secret-key-2026")

    state = {
        "merchant": merchant,
        "bank_url": bank_url.rstrip("/"),
        "merchant_id": merchant_id,
        "api_key": configured_api_key,
        "db_path": queue_db_path
    }
    app.config["MERCHANT_STATE"] = state

    def check_bank_connectivity() -> bool:
        """Pings the configured Bank Service over LAN to assess reachability."""
        url = f"{state['bank_url']}/health"
        try:
            resp = requests.get(url, timeout=0.4)
            return resp.status_code == 200
        except Exception:
            return False

    # -------------------------------------------------------------------------
    # CORS & Cross-Origin Headers for Multi-Port LAN Simulation
    # -------------------------------------------------------------------------
    @app.after_request
    def apply_cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS, PUT, DELETE"
        return response

    # -------------------------------------------------------------------------
    # HTML Dashboard Routes & Smart Cross-Service Navigation
    # -------------------------------------------------------------------------
    @app.route("/")
    @app.route("/merchant")
    def merchant_dashboard():
        return render_template("merchant.html")

    @app.route("/payer")
    def redirect_to_payer():
        host_ip = request.host.split(":")[0]
        return jsonify({"redirect": f"http://{host_ip}:5001/payer"}), 302, {"Location": f"http://{host_ip}:5001/payer"}

    @app.route("/bank")
    def redirect_to_bank():
        target_bank = state["bank_url"]
        return jsonify({"redirect": f"{target_bank}/bank"}), 302, {"Location": f"{target_bank}/bank"}

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "merchant"}), 200

    @app.route("/api/reset_demo", methods=["POST"])
    def reset_demo():
        """Resets disposable demo records while preserving keys, registries, and accounts."""
        local_only = request.args.get("local_only", "false").lower() == "true"
        if local_only:
            report = reset_merchant_db(state["db_path"])
        else:
            report = reset_all_demo_data(merchant_db=state["db_path"])
        return jsonify(report), 200

    @app.route("/api/load_sample_data", methods=["POST"])
    def load_sample_data():
        """Loads sample demo records on explicit user action."""
        report = load_sample_demo_data(merchant_db=state["db_path"])
        return jsonify(report), 200

    # -------------------------------------------------------------------------
    # System Info API
    # -------------------------------------------------------------------------
    @app.route("/api/network_info", methods=["GET"])
    def network_info():
        return jsonify({
            "service": "merchant",
            "lan_ip": get_lan_ip(),
            "port": request.host.split(":")[-1] if ":" in request.host else 5002,
            "bank_url": state["bank_url"],
            "bank_reachable": check_bank_connectivity()
        })

    # -------------------------------------------------------------------------
    # Merchant Core APIs
    # -------------------------------------------------------------------------
    @app.route("/api/merchant/status", methods=["GET"])
    def get_merchant_status():
        m = state["merchant"]
        summary = m.summary()

        # Query queue rows ordered newest first
        cursor = m.conn.cursor()
        cursor.execute("""
            SELECT token_id, payer_id, payee_id, amount, status, received_at, verification_time_ms
            FROM local_queue
            ORDER BY received_at DESC
        """)
        queue_rows = [dict(row) for row in cursor.fetchall()]

        bank_reachable = check_bank_connectivity()

        return jsonify({
            "merchant_id": m.merchant_id,
            "bank_url": state["bank_url"],
            "total_queued": summary["total_queued"],
            "pending_sync": summary["pending_sync"],
            "settled": summary["settled"],
            "pending_sync_amount": summary["pending_sync_amount"],
            "bank_reachable": bank_reachable,
            "queue": queue_rows
        })

    @app.route("/api/merchant/queue", methods=["GET"])
    def get_merchant_queue():
        """Returns the merchant's local SQLite pending queue."""
        m = state["merchant"]
        cursor = m.conn.cursor()
        cursor.execute("""
            SELECT token_id, payer_id, payee_id, amount, status, received_at, verification_time_ms
            FROM local_queue
            WHERE status = 'Pending Sync'
            ORDER BY received_at ASC
        """)
        pending_rows = [dict(row) for row in cursor.fetchall()]
        return jsonify({
            "merchant_id": m.merchant_id,
            "count": len(pending_rows),
            "pending_tokens": pending_rows
        }), 200

    @app.route("/api/merchant/receive", methods=["POST"])
    def receive_payment():
        """
        Receives an offline payment token package (via JSON paste, upload, or QR).
        Executes local RSA signature verification and persistent queueing in < 1ms.
        Completely autonomous: works when Bank and Payer are unreachable.
        """
        raw_text = request.get_data(as_text=True)
        data = request.get_json(silent=True)
        token_pkg = None
        if isinstance(data, dict):
            if "token_package" in data:
                token_pkg = data["token_package"]
            else:
                token_pkg = data
        elif raw_text and raw_text.strip():
            token_pkg = raw_text

        if not token_pkg:
            return jsonify({
                "accepted": False,
                "status": "REJECTED",
                "reason": "Missing payment token in request body."
            }), 400

        result = state["merchant"].verify_and_accept_token(token_pkg)
        return jsonify(result), 200

    @app.route("/api/merchant/sync", methods=["POST"])
    def sync_with_bank():
        """
        Attempts delayed batch synchronization with the central Bank Service over LAN.
        Uses authenticated merchant API credentials (X-Merchant-Key).
        If the Bank Service is stopped, unreachable, or simulated offline:
        - Fails gracefully with HTTP 503
        - Preserves 100% of the local queue intact
        - Never marks unsynced tokens as settled
        When the Bank Service confirms settlement:
        - Updates confirmed token IDs to 'Settled' in local SQLite
        - Returns settlement report including bank-signed receipts
        """
        m = state["merchant"]
        pending_tokens = m.get_pending_sync_tokens()

        if not pending_tokens:
            return jsonify({
                "message": "No pending tokens in local queue to sync.",
                "settled_count": 0,
                "duplicate_count": 0,
                "flagged_count": 0,
                "processing_time_ms": 0.0,
                "settled": [],
                "receipts": []
            }), 200

        target_url = f"{state['bank_url']}/api/bank/sync_batch"
        payload = {
            "merchant_id": m.merchant_id,
            "tokens": pending_tokens
        }
        headers = {
            "Content-Type": "application/json",
            "X-Merchant-Key": state["api_key"]
        }

        try:
            resp = requests.post(target_url, json=payload, headers=headers, timeout=6.0)
        except requests.exceptions.RequestException as e:
            return jsonify({
                "error": "Bank network unavailable.",
                "message": f"Central banking switch is unreachable at {state['bank_url']} ({str(e)}). Pending tokens are safely retained in local SQLite."
            }), 503

        if resp.status_code == 503:
            return jsonify({
                "error": "Bank network unavailable.",
                "message": resp.json().get("message", "Bank switch is currently offline. Pending tokens retained.")
            }), 503

        if resp.status_code != 200:
            return jsonify({
                "error": f"Bank returned error (HTTP {resp.status_code})",
                "details": resp.text
            }), resp.status_code

        sync_report = resp.json()

        # Mark confirmed settled tokens in local SQLite queue
        settled_ids = [item["token_id"] for item in sync_report.get("settled", [])]
        if settled_ids:
            m.mark_tokens_synced(settled_ids)

        return jsonify(sync_report), 200

    @app.route("/api/merchant/sync_trusted_payers", methods=["POST"])
    def sync_trusted_payers():
        """
        Synchronizes trusted payer public keys from the authoritative Bank registry.
        Uses authenticated merchant credentials.
        """
        target_url = f"{state['bank_url']}/api/bank/trusted_payers"
        headers = {
            "X-Merchant-Key": state["api_key"],
            "X-Merchant-ID": state["merchant_id"]
        }
        try:
            resp = requests.get(target_url, headers=headers, timeout=5.0)
            if resp.status_code != 200:
                return jsonify({"error": f"Bank returned HTTP {resp.status_code}", "details": resp.text}), resp.status_code

            data = resp.json()
            payers = data.get("trusted_payers", [])
            for p in payers:
                state["merchant"].register_payer(p["payer_id"], p["public_key_pem"])

            return jsonify({
                "success": True,
                "synced_count": len(payers),
                "message": f"Successfully synced {len(payers)} trusted payer keys from authoritative bank registry."
            }), 200
        except Exception as e:
            return jsonify({"error": f"Failed to sync with bank: {str(e)}"}), 503

    @app.route("/api/merchant/purge_settled", methods=["POST"])
    def purge_settled():
        """Removes settled tokens from active view in local SQLite."""
        removed_count = state["merchant"].remove_synced_tokens()
        return jsonify({"success": True, "removed_count": removed_count})

    @app.route("/api/merchant/provision_payer", methods=["POST"])
    def provision_payer():
        """
        Controlled provisioning endpoint for local/offline testing.
        Registers or updates a trusted payer's public key in the merchant's local registry.
        """
        data = request.get_json(silent=True) or {}
        pid = data.get("payer_id", "").strip()
        pem = data.get("public_key_pem", "").strip()

        if not pid or not pem:
            return jsonify({"error": "payer_id and public_key_pem required."}), 400

        state["merchant"].register_payer(pid, pem)
        return jsonify({
            "success": True,
            "payer_id": pid,
            "message": f"Payer '{pid}' public key registered in merchant's trusted local registry."
        })

    return app


def main():
    parser = argparse.ArgumentParser(description="Offline UPI - Merchant POS Service (Device 2)")
    parser.add_argument("--host", default=os.getenv("MERCHANT_HOST", "0.0.0.0"), help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("MERCHANT_PORT", 5002)), help="Port to bind (default: 5002)")
    parser.add_argument("--merchant-id", default=os.getenv("MERCHANT_ID", "merchant_bob@upi"), help="Merchant ID (default: merchant_bob@upi)")
    parser.add_argument("--api-key", default=os.getenv("MERCHANT_API_KEY", "merchant-bob-secret-key-2026"), help="Merchant secret API key for bank sync")
    parser.add_argument("--db", default=os.getenv("MERCHANT_DB", "merchant_queue_bob_upi.db"), help="SQLite queue database path")
    parser.add_argument("--bank-url", default=os.getenv("BANK_URL", "http://localhost:5003"), help="Central Bank Service URL")

    args = parser.parse_args()
    app = create_merchant_app(
        merchant_id=args.merchant_id,
        api_key=args.api_key,
        queue_db_path=args.db,
        bank_url=args.bank_url
    )

    lan_ip = get_lan_ip()
    print("\n" + "=" * 80)
    print("  DEVICE 2 — MERCHANT POS SERVICE")
    print("=" * 80)
    print(f"  Local Dashboard:   http://localhost:{args.port}/merchant")
    print(f"  LAN Access:        http://{lan_ip}:{args.port}/merchant")
    print(f"  Merchant ID:       {args.merchant_id}")
    print(f"  Target Bank URL:   {args.bank_url}")
    print(f"  Queue Database:    {args.db}")
    print("=" * 80 + "\n")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
