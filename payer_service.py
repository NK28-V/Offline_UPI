"""
Payer Service (Device 1) — Customer Payer Device
Operates independently on the customer smartphone / payer device.
Maintains the payer's private RSA key protected by PBKDF2-HMAC-SHA256 and AES-256-GCM authenticated encryption,
enforces the ₹500 offline limit cap (50,000 paise), manages explicit token lifecycle states
(CREATED -> EXPORTED -> SETTLED), blocks unsafe cancellation of exported tokens,
and authoritatively reconciles offline allowances upon verification of bank-signed settlement receipts.
Runs 100% offline with zero network dependency on the Bank or Merchant services during token generation.
"""

import os
import sys
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

from payer import PayerDevice, OfflineSpendLimitExceededError
from reset_demo import reset_payer_state, reset_all_demo_data
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


def create_payer_app(
    payer_id: str = "alice@upi",
    offline_cap: float = 500.0,
    state_file: str = "payer_state_alice_upi.json",
    passphrase: Optional[str] = None,
    bank_public_key_pem: Optional[str] = None
) -> Flask:
    """Factory creating the Payer Service Flask application.

    Passphrase resolution order (no hardcoded default):
      1. 'passphrase' argument (use only in tests; never from CLI args).
      2. PAYER_PASSPHRASE environment variable (required for production).
    If neither is present and a state file exists, PayerDevice raises RuntimeError.
    """
    app = Flask(__name__, template_folder="templates", static_folder="static")

    # Resolve passphrase: argument takes priority (test injection), then env var.
    # Never fall back to a hardcoded default.
    configured_passphrase = passphrase or os.getenv("PAYER_PASSPHRASE", "")

    payer = PayerDevice(
        payer_id=payer_id,
        offline_cap=offline_cap,
        state_file=state_file,
        passphrase=configured_passphrase,
        bank_public_key_pem=bank_public_key_pem
    )

    state = {
        "payer": payer,
        "payer_id": payer_id,
        "state_file": state_file
    }
    app.config["PAYER_STATE"] = state

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
    @app.route("/payer")
    def payer_dashboard():
        return render_template("payer.html")

    @app.route("/merchant")
    def redirect_to_merchant():
        host_ip = request.host.split(":")[0]
        return jsonify({"redirect": f"http://{host_ip}:5002/merchant"}), 302, {"Location": f"http://{host_ip}:5002/merchant"}

    @app.route("/bank")
    def redirect_to_bank():
        host_ip = request.host.split(":")[0]
        return jsonify({"redirect": f"http://{host_ip}:5003/bank"}), 302, {"Location": f"http://{host_ip}:5003/bank"}

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "service": "payer"}), 200

    @app.route("/api/reset_demo", methods=["POST"])
    def reset_demo():
        """Resets disposable demo records while preserving keys, registries, and accounts."""
        local_only = request.args.get("local_only", "false").lower() == "true"
        if local_only:
            report = reset_payer_state(state["state_file"])
        else:
            report = reset_all_demo_data(payer_state_file=state["state_file"])
        # Update in-memory PayerDevice
        p = state["payer"]
        p.unsynced_spent_paise = 0
        p.outstanding_tokens = {}
        p._save_state()
        return jsonify(report), 200

    @app.route("/api/load_sample_data", methods=["POST"])
    def load_sample_data():
        """Loads sample demo records on explicit user action."""
        report = load_sample_demo_data(payer_state_file=state["state_file"])
        state["payer"]._load_state()
        return jsonify(report), 200

    # -------------------------------------------------------------------------
    # System Info & Public Key APIs
    # -------------------------------------------------------------------------
    @app.route("/api/network_info", methods=["GET"])
    def network_info():
        return jsonify({
            "service": "payer",
            "lan_ip": get_lan_ip(),
            "port": request.host.split(":")[-1] if ":" in request.host else 5001,
            "offline_mode": True
        })

    @app.route("/api/payer/public_key", methods=["GET"])
    def get_public_key():
        """Returns the public key PEM for provisioning into trusted registries."""
        p = state["payer"]
        return jsonify({
            "payer_id": p.payer_id,
            "public_key_pem": p.public_key_pem
        }), 200

    # -------------------------------------------------------------------------
    # Payer Core APIs
    # -------------------------------------------------------------------------
    @app.route("/api/payer/status", methods=["GET"])
    def get_payer_status():
        p = state["payer"]
        outstanding_list = [
            {
                "token_id": tid,
                "payee_id": info.get("payee_id", "merchant_bob@upi"),
                "amount": info.get("amount", 0.0),
                "timestamp": info.get("timestamp", time.time()),
                "status": info.get("status", "CREATED")
            }
            for tid, info in p.outstanding_tokens.items()
        ]
        outstanding_list.sort(key=lambda x: x["timestamp"], reverse=True)

        return jsonify({
            "payer_id": p.payer_id,
            "payer_name": p.payer_id.split("@")[0].capitalize(),
            "offline_cap": p.offline_cap,
            "unsynced_spent": p.unsynced_spent,
            "remaining_allowance": p.remaining_offline_allowance,
            "outstanding_count": len([t for t in p.outstanding_tokens.values() if t.get("status") in ["CREATED", "EXPORTED"]]),
            "transactions": outstanding_list
        })

    @app.route("/api/payer/generate", methods=["POST"])
    def generate_payment():
        """
        Generates and cryptographically signs an offline payment token on this device.
        Requires amount and payee_id (payee binding).
        Operates completely offline without contacting Bank or Merchant.
        """
        data = request.get_json(silent=True) or {}
        amount = data.get("amount")
        payee_id = (data.get("payee_id") or "").strip()

        if not payee_id:
            return jsonify({"error": "Payee / Merchant ID is required for signed binding."}), 400

        try:
            amt = float(amount)
        except (ValueError, TypeError):
            return jsonify({"error": "Invalid amount format. Must be a numeric value."}), 400

        p = state["payer"]
        try:
            token_package = p.generate_token(amount=amt, payee_id=payee_id)
            return jsonify({
                "success": True,
                "token_package": token_package,
                "unsynced_spent": p.unsynced_spent,
                "remaining_allowance": p.remaining_offline_allowance
            }), 200
        except OfflineSpendLimitExceededError as e:
            return jsonify({"error": str(e)}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            return jsonify({"error": f"Internal token generation error: {str(e)}"}), 500

    @app.route("/api/payer/export", methods=["POST"])
    def mark_exported():
        """
        Marks a generated token as EXPORTED (downloaded as JSON or displayed as QR).
        Once exported, cancellation is disallowed to prevent unsafe local double-spending.
        """
        data = request.get_json(silent=True) or {}
        token_id = data.get("token_id")
        if not token_id:
            return jsonify({"error": "token_id is required."}), 400

        p = state["payer"]
        if p.mark_token_exported(token_id):
            return jsonify({
                "success": True,
                "token_id": token_id,
                "status": "EXPORTED",
                "message": "Token marked as exported. Offline reservation is held until settlement reconciliation."
            }), 200
        return jsonify({"error": f"Token '{token_id}' not found or not in CREATED status."}), 400

    @app.route("/api/payer/cancel", methods=["POST"])
    def cancel_token():
        """
        Cancels an outstanding token ONLY if it has never been exported or transferred.
        Rejects cancellation if the token has been exported to prevent double-spending.
        """
        data = request.get_json(silent=True) or {}
        token_id = data.get("token_id")
        if not token_id:
            return jsonify({"error": "token_id is required."}), 400

        p = state["payer"]
        try:
            success = p.cancel_failed_token(token_id)
            if success:
                return jsonify({
                    "success": True,
                    "message": f"Token '{token_id}' safely cancelled. Offline allowance restored.",
                    "unsynced_spent": p.unsynced_spent,
                    "remaining_allowance": p.remaining_offline_allowance
                }), 200
            return jsonify({"error": f"Token '{token_id}' not found among active tokens."}), 404
        except ValueError as ve:
            return jsonify({"error": str(ve)}), 400

    @app.route("/api/payer/reconcile", methods=["POST"])
    def reconcile_tokens():
        """
        Authoritatively reconciles tokens upon receipt of verifiable Bank-Signed Receipts.
        Validates the bank's digital signature, token ID, amount, and payer binding.
        Restores offline spending allowance safely.

        SECURITY: Only cryptographically signed receipts from the trusted bank are accepted.
        Legacy token-ID reconciliation (settled_token_ids) is REMOVED because it allows
        balance restoration without any signature verification.
        """
        data = request.get_json(silent=True) or {}
        p = state["payer"]

        # Single signed receipt package: {"receipt": {...}, "signature": "..."}
        receipt_pkg = data.get("receipt") if ("receipt" in data and isinstance(data.get("receipt"), dict) and "signature" in data["receipt"]) else data
        if isinstance(receipt_pkg, dict) and "receipt" in receipt_pkg and "signature" in receipt_pkg:
            try:
                res = p.reconcile_with_bank_receipt(receipt_pkg)
                return jsonify(res), 200
            except ValueError as ve:
                return jsonify({"error": str(ve)}), 400

        # Batch of signed receipts: {"receipts": [{"receipt": {...}, "signature": "..."}, ...]}
        if "receipts" in data and isinstance(data["receipts"], list):
            results = []
            for r_pkg in data["receipts"]:
                try:
                    res = p.reconcile_with_bank_receipt(r_pkg)
                    results.append(res)
                except ValueError as ve:
                    results.append({"error": str(ve)})
            return jsonify({
                "success": True,
                "reconciled_count": len([r for r in results if r.get("success")]),
                "results": results,
                "unsynced_spent": p.unsynced_spent,
                "remaining_allowance": p.remaining_offline_allowance
            }), 200

        # Legacy settled_token_ids path is intentionally removed — it bypassed
        # cryptographic receipt verification and allowed arbitrary balance restoration.
        if "settled_token_ids" in data:
            return jsonify({
                "error": "The 'settled_token_ids' reconciliation path has been removed. "
                         "Submit a bank-signed receipt package: {\"receipt\": {...}, \"signature\": \"...\"}."
            }), 400

        return jsonify({"error": "A bank-signed receipt package is required: {\"receipt\": {...}, \"signature\": \"...\"}."}), 400

    return app


def main():
    parser = argparse.ArgumentParser(description="Offline UPI - Customer Payer Service (Device 1)")
    parser.add_argument("--host", default=os.getenv("PAYER_HOST", "0.0.0.0"), help="Host address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=int(os.getenv("PAYER_PORT", 5001)), help="Port to bind (default: 5001)")
    parser.add_argument("--payer-id", default=os.getenv("PAYER_ID", "alice@upi"), help="Payer ID (default: alice@upi)")
    parser.add_argument("--offline-cap", type=float, default=float(os.getenv("OFFLINE_CAP", 500.0)), help="Offline spend cap (default: 500.0)")
    parser.add_argument("--state-file", default=os.getenv("PAYER_STATE_FILE", "payer_state_alice_upi.json"), help="State file path")
    # SECURITY: passphrase is NEVER accepted as a CLI argument (visible in process list).
    # Set the PAYER_PASSPHRASE environment variable instead.

    args = parser.parse_args()

    # Enforce explicit passphrase configuration — no hardcoded fallback.
    passphrase = os.getenv("PAYER_PASSPHRASE", "")
    if not passphrase:
        print(
            "[FATAL] PAYER_PASSPHRASE environment variable is not set.\n"
            "  Set it before starting the payer service:\n"
            "    Windows:  set PAYER_PASSPHRASE=your-strong-passphrase\n"
            "    Linux/Mac: export PAYER_PASSPHRASE=your-strong-passphrase\n"
            "  DEMO ONLY: For the academic demo, use any non-empty passphrase.",
            file=sys.stderr
        )
        sys.exit(1)

    app = create_payer_app(
        payer_id=args.payer_id,
        offline_cap=args.offline_cap,
        state_file=args.state_file
    )

    lan_ip = get_lan_ip()
    print("\n" + "=" * 80)
    print("  DEVICE 1 — CUSTOMER PAYER SERVICE")
    print("=" * 80)
    print(f"  Local Dashboard:   http://localhost:{args.port}/payer")
    print(f"  LAN Access:        http://{lan_ip}:{args.port}/payer")
    print(f"  Payer ID:          {args.payer_id}")
    print(f"  Offline Cap:       INR {args.offline_cap:.2f}")
    print(f"  State Persistence: {args.state_file}")
    print(f"  Key Encryption:    PBKDF2-HMAC-SHA256 + AES-256-GCM [ACTIVE]")
    print("=" * 80 + "\n")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
