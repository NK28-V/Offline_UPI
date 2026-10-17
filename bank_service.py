"""
Bank Service (Device 3) — Central UPI Switch & Settlement Network
Operates independently on the bank device/host.
Maintains the central SQLite settlement ledger, processes authenticated merchant synchronization batches,
generates cryptographically signed settlement receipts, enforces 100% duplicate / double-spend defense
via atomic SQLite transactions and SAVEPOINTs, flags stale tokens (> 24h old), and protects sensitive
provisioning and transaction review APIs with admin and merchant authentication.
"""

import os
import sys
import hmac
import socket
import argparse
import time
from typing import Dict, Any, Optional
from flask import Flask, render_template, request, jsonify

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

from backend import BackendServer


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


def create_bank_app(
    db_path: str = "offline_upi_backend.db",
    admin_token: Optional[str] = None,
    initial_network_online: bool = True
) -> Flask:
    """Factory creating the Bank Service Flask application.

    Admin token resolution order (no hardcoded default):
      1. 'admin_token' argument (use only in tests; never from CLI args).
      2. BANK_ADMIN_TOKEN environment variable (required for production).
    If neither is set, an empty-string admin token is used which will cause
    all admin requests to fail — a safe default that requires explicit configuration.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")
    backend = BackendServer(db_path=db_path)

    # Resolve admin token: argument (test injection) then env var. No hardcoded default.
    configured_admin_token = admin_token or os.getenv("BANK_ADMIN_TOKEN", "")

    # State
    state = {
        "network_online": initial_network_online,
        "admin_token": configured_admin_token,
        "backend": backend,
        "db_path": db_path
    }
    app.config["BANK_STATE"] = state

    def check_admin_auth() -> bool:
        """
        Verifies the admin authorization token.

        SECURITY:
          - Token is accepted ONLY from the 'X-Admin-Token' request header.
          - It is NEVER accepted from URL query parameters or JSON request bodies
            to prevent secrets appearing in server access logs or process lists.
          - Comparison is constant-time (hmac.compare_digest) to prevent timing attacks.
          - If no admin token is configured (empty string), ALL admin requests are denied.
        """
        if not state["admin_token"]:
            # No admin token configured — deny all admin requests safely.
            return False
        token = request.headers.get("X-Admin-Token", "")
        if not token:
            return False
        return hmac.compare_digest(
            token.encode("utf-8"),
            state["admin_token"].encode("utf-8")
        )

    def check_merchant_auth(merchant_id: str) -> bool:
        """Verifies merchant API key from request headers or body."""
        api_key = request.headers.get("X-Merchant-Key")
        if not api_key:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header.split(" ", 1)[1]
        if not api_key and request.is_json:
            api_key = (request.get_json(silent=True) or {}).get("merchant_api_key")

        # In dev/testing, if no merchant key is provided, check if client is admin or authorized
        if not api_key:
            return False

        return state["backend"].verify_merchant_auth(merchant_id, api_key)

    # -------------------------------------------------------------------------
    # HTML Dashboard Routes & Smart Cross-Service Navigation
    # -------------------------------------------------------------------------
    @app.route("/")
    @app.route("/bank")
    def bank_dashboard():
        return render_template("bank.html")

    @app.route("/payer")
    def redirect_to_payer():
        host_ip = request.host.split(":")[0]
        return jsonify({"redirect": f"http://{host_ip}:5001/payer"}), 302, {"Location": f"http://{host_ip}:5001/payer"}

    @app.route("/merchant")
    def redirect_to_merchant():
        host_ip = request.host.split(":")[0]
        return jsonify({"redirect": f"http://{host_ip}:5002/merchant"}), 302, {"Location": f"http://{host_ip}:5002/merchant"}

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "ok",
            "service": "bank",
            "network_online": state["network_online"]
        }), 200

    # -------------------------------------------------------------------------
    # System Info & Public Key APIs
    # -------------------------------------------------------------------------
    @app.route("/api/network_info", methods=["GET"])
    def network_info():
        return jsonify({
            "service": "bank",
            "lan_ip": get_lan_ip(),
            "port": request.host.split(":")[-1] if ":" in request.host else 5003,
            "network_online": state["network_online"]
        })

    @app.route("/api/bank/public_key", methods=["GET"])
    def get_bank_public_key():
        """Exports the bank's RSA public key PEM so payers and merchants can verify receipts."""
        return jsonify({
            "bank_id": "central_bank_switch",
            "public_key_pem": state["backend"].bank_public_key_pem
        }), 200

    # -------------------------------------------------------------------------
    # Bank Core Settlement & Audit APIs
    # -------------------------------------------------------------------------
    @app.route("/api/bank/status", methods=["GET"])
    def get_bank_status():
        b = state["backend"]
        cursor = b.conn.cursor()

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
        duplicate_count = audit_sums["d"] or 0 if audit_sums else 0
        rejected_count = audit_sums["r"] or 0 if audit_sums else 0
        total_received = audit_sums["t"] or 0 if audit_sums else 0

        return jsonify({
            "network_online": state["network_online"],
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

    @app.route("/api/bank/sync_batch", methods=["POST"])
    def sync_batch_endpoint():
        """
        Receives an authenticated batch of offline payment tokens from a merchant for settlement.
        Enforces:
        - Network online check (supports simulated maintenance outage)
        - Merchant authentication (via X-Merchant-Key or Bearer token)
        - Payee binding to the submitting merchant ID
        - Digital signature verification against registered payer public keys
        - Atomic settlement with SAVEPOINTs catching duplicate tokens (IntegrityError)
        - Generation and persistence of bank-signed settlement receipts
        - 24-hour token age check (flagging expired tokens)
        """
        if not state["network_online"]:
            return jsonify({
                "error": "Bank network unavailable.",
                "message": "Central banking switch is currently simulated offline. Pending tokens are safely retained in merchant local SQLite."
            }), 503

        data = request.get_json(silent=True) or {}
        merchant_id = data.get("merchant_id", "").strip()
        tokens = data.get("tokens", [])

        if not merchant_id:
            return jsonify({"error": "merchant_id is required."}), 400
        if not isinstance(tokens, list):
            return jsonify({"error": "tokens must be a list."}), 400

        # Merchant Authentication:
        # Require merchant API key if authorized merchants exist, or verify against backend
        merchant_key = request.headers.get("X-Merchant-Key") or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
        if not merchant_key and request.is_json:
            merchant_key = data.get("merchant_api_key")

        # If client passes admin token, allow it as well (for admin testing)
        if not check_admin_auth():
            if not merchant_key or not state["backend"].verify_merchant_auth(merchant_id, merchant_key):
                return jsonify({
                    "error": "Unauthorized: valid merchant API key required in 'X-Merchant-Key' header."
                }), 401

        if not tokens:
            return jsonify({
                "message": "Empty token batch submitted.",
                "settled_count": 0,
                "duplicate_count": 0,
                "flagged_count": 0,
                "rejected_count": 0,
                "processing_time_ms": 0.0,
                "settled": [],
                "duplicates": [],
                "flagged_for_review": [],
                "rejected_invalid": [],
                "receipts": []
            }), 200

        report = state["backend"].sync_batch(merchant_id=merchant_id, tokens=tokens)
        return jsonify(report), 200

    @app.route("/api/bank/register_payer", methods=["POST"])
    def register_payer():
        """
        Registers an authorized payer and public key in the authoritative central bank registry.
        Requires administrator authentication to prevent arbitrary LAN key injection.
        """
        data = request.get_json(silent=True) or {}
        pid = data.get("payer_id", "").strip()
        pem = data.get("public_key_pem", "").strip()
        allow_update = bool(data.get("allow_update", False))

        # Enforce administrative authentication
        if not check_admin_auth():
            return jsonify({
                "error": "Unauthorized: Administrative access token required in 'X-Admin-Token' header."
            }), 401

        if not pid or not pem:
            return jsonify({"error": "payer_id and public_key_pem are required."}), 400

        try:
            state["backend"].register_payer(pid, pem, allow_update=allow_update)
            return jsonify({
                "success": True,
                "payer_id": pid,
                "message": f"Payer '{pid}' successfully registered in central bank authoritative registry."
            }), 200
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400
        except Exception as e:
            return jsonify({"error": f"Failed to register payer: {str(e)}"}), 500

    @app.route("/api/bank/register_merchant", methods=["POST"])
    def register_merchant_endpoint():
        """Registers an authorized merchant with an API key. Requires admin authentication."""
        if not check_admin_auth():
            return jsonify({"error": "Unauthorized: Administrative access token required."}), 401

        data = request.get_json(silent=True) or {}
        mid = data.get("merchant_id", "").strip()
        key = data.get("api_key", "").strip()
        name = data.get("name", "").strip()

        if not mid or not key:
            return jsonify({"error": "merchant_id and api_key are required."}), 400

        state["backend"].register_merchant(mid, key, name=name)
        return jsonify({"success": True, "merchant_id": mid}), 200

    @app.route("/api/bank/trusted_payers", methods=["GET"])
    def get_trusted_payers_list():
        """
        Returns the authoritative list of trusted payers for merchant registry synchronization.
        Requires admin or merchant authentication.
        """
        is_admin = check_admin_auth()
        merchant_key = request.headers.get("X-Merchant-Key")
        merchant_id = request.headers.get("X-Merchant-ID")
        is_merchant = bool(merchant_id and merchant_key and state["backend"].verify_merchant_auth(merchant_id, merchant_key))

        if not (is_admin or is_merchant):
            return jsonify({"error": "Unauthorized: Admin or Merchant authentication required."}), 401

        payers = state["backend"].get_all_trusted_payers()
        return jsonify({
            "bank_id": "central_bank_switch",
            "total_registered": len(payers),
            "trusted_payers": payers
        }), 200

    @app.route("/api/bank/transactions/<token_id>", methods=["GET"])
    def get_transaction_record(token_id: str):
        """Retrieves authoritative outcome for a token from the central ledger."""
        rec = state["backend"].get_token_record(token_id.strip())
        if not rec:
            return jsonify({"error": f"Token '{token_id}' not found in ledger."}), 404
        return jsonify(rec), 200

    @app.route("/api/bank/receipts/<token_id>", methods=["GET"])
    def get_settlement_receipt(token_id: str):
        """Retrieves authoritative bank-signed settlement receipt for a token."""
        receipt_pkg = state["backend"].get_receipt(token_id.strip())
        if not receipt_pkg:
            return jsonify({"error": f"No settlement receipt found for token '{token_id}'."}), 404
        return jsonify(receipt_pkg), 200

    @app.route("/api/bank/toggle_network", methods=["POST"])
    def toggle_network():
        """Toggles network availability for maintenance / simulated outage demonstration (Admin only)."""
        if not check_admin_auth():
            return jsonify({"error": "Unauthorized: Administrative access token required."}), 401

        state["network_online"] = not state["network_online"]
        state["backend"].log_admin_action("TOGGLE_NETWORK", None, f"Network state toggled to {state['network_online']}")
        return jsonify({
            "network_online": state["network_online"],
            "message": f"Bank switch network state set to {'ONLINE' if state['network_online'] else 'OFFLINE (SIMULATED)'}."
        })

    @app.route("/api/bank/simulate_duplicate", methods=["POST"])
    def simulate_duplicate():
        """Demonstrates double-spend prevention by re-submitting an existing settled token (Admin only)."""
        if not check_admin_auth():
            return jsonify({"error": "Unauthorized: Administrative access token required."}), 401

        b = state["backend"]
        cursor = b.conn.cursor()
        cursor.execute("SELECT token_id, payer_id, payee_id, amount, token_timestamp, signature FROM settled_tokens LIMIT 1")
        row = cursor.fetchone()

        if not row:
            return jsonify({
                "error": "No settled tokens exist yet. Synchronize at least one valid payment from the merchant first."
            }), 400

        target_token = {
            "payload": {
                "tokenID": row["token_id"],
                "payerID": row["payer_id"],
                "payeeID": row["payee_id"],
                "amount": row["amount"],
                "timestamp": row["token_timestamp"]
            },
            "signature": row["signature"]
        }

        report = b.sync_batch(row["payee_id"], [target_token])
        if report["duplicates"]:
            return jsonify({
                "success": True,
                "token_id": row["token_id"],
                "result": report["duplicates"][0]
            })

        return jsonify({"error": "Duplicate was unexpectedly not caught."}), 500

    return app


def main():
    parser = argparse.ArgumentParser(description="Offline UPI - Bank Service (Device 3)")
    parser.add_argument("--host", default=os.getenv("BANK_HOST", "0.0.0.0"), help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("BANK_PORT", 5003)), help="Port to bind (default: 5003)")
    parser.add_argument("--db", default=os.getenv("BANK_DB", "offline_upi_backend.db"), help="SQLite database path")
    # SECURITY: admin token is NEVER accepted as a CLI argument (visible in process list).
    # Set the BANK_ADMIN_TOKEN environment variable instead.

    args = parser.parse_args()

    # Enforce explicit admin token configuration — no hardcoded fallback.
    admin_token = os.getenv("BANK_ADMIN_TOKEN", "")
    if not admin_token:
        print(
            "[FATAL] BANK_ADMIN_TOKEN environment variable is not set.\n"
            "  Set it before starting the bank service:\n"
            "    Windows:  set BANK_ADMIN_TOKEN=your-strong-random-token\n"
            "    Linux/Mac: export BANK_ADMIN_TOKEN=your-strong-random-token\n"
            "  DEMO ONLY: For the academic demo, use any non-empty token.",
            file=sys.stderr
        )
        sys.exit(1)

    app = create_bank_app(db_path=args.db, admin_token=admin_token)

    lan_ip = get_lan_ip()
    print("\n" + "=" * 80)
    print("  DEVICE 3 — BANK SERVICE & CENTRAL UPI SWITCH")
    print("=" * 80)
    print(f"  Local Dashboard:   http://localhost:{args.port}/bank")
    print(f"  LAN Access:        http://{lan_ip}:{args.port}/bank")
    print(f"  Sync Batch API:    http://{lan_ip}:{args.port}/api/bank/sync_batch")
    print(f"  Database File:     {args.db}")
    print(f"  Admin Secret Token: [CONFIGURED]")
    print("=" * 80 + "\n")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
