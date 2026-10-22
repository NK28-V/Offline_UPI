import argparse, json, secrets, subprocess, sys, time, urllib.error, urllib.request
from typing import Any, Dict, Optional, Tuple
from flask import Flask, jsonify, render_template, request
from core import Bank, Merchant, Payer

def http_json(url: str, data: Optional[Dict] = None, headers: Optional[Dict] = None) -> Tuple[int, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=2.5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {"error": str(e)}
    except Exception as e:
        return 503, {"error": f"Connection error to {url}: {str(e)}"}

def create_app(role: str, port: int, bank_url: str = "http://127.0.0.1:5003") -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    bank_url = bank_url.rstrip("/")

    @app.after_request
    def cors(resp):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "*"
        return resp

    @app.route("/")
    def index():
        return render_template("index.html", role=role, port=port, bank_url=bank_url)

    if role == "payer":
        payer = Payer(f"payer_{port}.db")

        @app.route("/api/payer/state")
        def payer_state():
            return jsonify(payer.get_info())

        @app.route("/api/payer/register", methods=["POST"])
        def payer_register():
            d = request.get_json(silent=True) or {}
            pid, b_url = d.get("payer_id", "").strip(), d.get("bank_url", bank_url).rstrip("/")
            if not pid: return jsonify({"error": "Payer ID required"}), 400
            st, res = http_json(f"{b_url}/api/bank/public_key")
            if st != 200: return jsonify({"error": f"Failed reaching bank: {res.get('error')}"}), 502
            pub_pem = payer.register(pid, b_url, res["public_key_pem"])
            st, reg_res = http_json(f"{b_url}/api/bank/register_payer", {"payer_id": pid, "public_key_pem": pub_pem})
            if st != 200: return jsonify({"error": f"Registration rejected by bank: {reg_res.get('error')}"}), 502
            return jsonify({"success": True, "message": f"Wallet registered for {pid}"})

        @app.route("/api/payer/create_token", methods=["POST"])
        def payer_token():
            d = request.get_json(silent=True) or {}
            try:
                amt = float(d.get("amount", 0))
                amt_paise = int(round(amt * 100))
                pkg = payer.create_token(d.get("payee_id", ""), amt_paise)
                return jsonify({"success": True, "token_package": pkg})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

        @app.route("/api/payer/cancel_token", methods=["POST"])
        def payer_cancel():
            tid = (request.get_json(silent=True) or {}).get("token_id")
            if payer.cancel_token(tid): return jsonify({"success": True, "message": "Token cancelled, allowance restored."})
            return jsonify({"error": "Could not cancel token (not found or already settled)."}), 400

        @app.route("/api/payer/refresh", methods=["POST"])
        def payer_refresh():
            info = payer.get_info()
            if not info["registered"]: return jsonify({"error": "Wallet not registered"}), 400
            st, res = http_json(f"{info['bank_url']}/api/bank/payer_settlement/{info['payer_id']}")
            if st != 200: return jsonify({"error": f"Failed to fetch statement: {res.get('error')}"}), 502
            try:
                count = payer.reconcile(res)
                return jsonify({"success": True, "reconciled_count": count, "message": f"Reconciled {count} settled tokens."})
            except Exception as e:
                return jsonify({"error": str(e)}), 400

    elif role == "merchant":
        merchant = Merchant(f"merchant_{port}.db")

        @app.route("/api/merchant/state")
        def merchant_state():
            return jsonify(merchant.get_info())

        @app.route("/api/merchant/register", methods=["POST"])
        def merchant_register():
            d = request.get_json(silent=True) or {}
            mid, b_url, m_url = d.get("merchant_id", "").strip(), d.get("bank_url", bank_url).rstrip("/"), d.get("merchant_url", f"http://127.0.0.1:{port}").rstrip("/")
            if not mid: return jsonify({"error": "Merchant ID required"}), 400
            st, res = http_json(f"{b_url}/api/bank/register_merchant", {"merchant_id": mid, "merchant_url": m_url})
            if st != 200: return jsonify({"error": f"Bank registration failed: {res.get('error')}"}), 502
            merchant.register(mid, b_url, m_url, res["api_key"])
            _, p_res = http_json(f"{b_url}/api/bank/trusted_payers")
            if isinstance(p_res, list): merchant.update_trusted_payers(p_res)
            return jsonify({"success": True, "message": f"Shop {mid} registered."})

        @app.route("/api/merchant/refresh_payers", methods=["POST"])
        def merchant_refresh_payers():
            info = merchant.get_info()
            if not info["registered"]: return jsonify({"error": "Shop not registered"}), 400
            st, res = http_json(f"{info['bank_url']}/api/bank/trusted_payers")
            if st != 200: return jsonify({"error": f"Failed fetching payers: {res.get('error')}"}), 502
            merchant.update_trusted_payers(res)
            return jsonify({"success": True, "count": len(res), "message": f"Downloaded {len(res)} trusted payer keys."})

        @app.route("/api/merchant/verify_token", methods=["POST"])
        def merchant_verify():
            pkg = (request.get_json(silent=True) or {}).get("token_package")
            if isinstance(pkg, str):
                try: pkg = json.loads(pkg)
                except Exception: return jsonify({"error": "Invalid JSON format"}), 400
            ok, msg = merchant.verify_and_queue(pkg)
            return jsonify({"success": ok, "message": msg}), (200 if ok else 400)

        @app.route("/api/merchant/sync", methods=["POST"])
        def merchant_sync():
            info = merchant.get_info()
            if not info["registered"]: return jsonify({"error": "Shop not registered"}), 400
            batch = merchant.get_pending_batch()
            if not batch: return jsonify({"success": True, "message": "No pending offline tokens to sync.", "results": []})
            cur = merchant.conn.cursor()
            cur.execute("SELECT value FROM config WHERE key = 'api_key'")
            api_key = cur.fetchone()["value"]
            st, res = http_json(f"{info['bank_url']}/api/bank/sync", {"merchant_id": info["merchant_id"], "batch": batch}, {"X-Merchant-Key": api_key})
            if st != 200:
                return jsonify({"success": False, "message": "Bank is offline or unreachable. Queued payments are safely kept locally.", "error": res.get("error")}), 200
            merchant.apply_sync_results(res.get("results", []))
            return jsonify({"success": True, "results": res.get("results", []), "message": f"Synced {len(batch)} payments with bank."})

    elif role == "bank":
        bank = Bank(f"bank_{port}.db")
        net = {"online": True}

        @app.route("/api/bank/state")
        def bank_state():
            stats = bank.get_stats()
            return jsonify({"network_online": net["online"], "public_key": bank.get_public_key(), "stats": stats})

        @app.route("/api/bank/public_key")
        def bank_pub():
            return jsonify({"public_key_pem": bank.get_public_key()})

        @app.route("/api/bank/register_payer", methods=["POST"])
        def bank_reg_payer():
            d = request.get_json(silent=True) or {}
            bank.register_payer(d.get("payer_id", ""), d.get("public_key_pem", ""))
            return jsonify({"success": True, "bank_public_key": bank.get_public_key()})

        @app.route("/api/bank/register_merchant", methods=["POST"])
        def bank_reg_merch():
            d = request.get_json(silent=True) or {}
            mid, m_url = d.get("merchant_id", "").strip(), d.get("merchant_url", "").strip()
            key = secrets.token_hex(16)
            bank.register_merchant(mid, m_url, key)
            return jsonify({"success": True, "api_key": key})

        @app.route("/api/bank/trusted_payers")
        def bank_payers():
            return jsonify(bank.get_trusted_payers())

        @app.route("/api/bank/toggle_network", methods=["POST"])
        def bank_toggle():
            net["online"] = not net["online"]
            return jsonify({"network_online": net["online"], "message": f"Bank network is now {'ONLINE' if net['online'] else 'OFFLINE'}."})

        @app.route("/api/bank/sync", methods=["POST"])
        def bank_sync():
            if not net["online"]: return jsonify({"error": "Bank network offline (Simulated Outage)"}), 503
            d = request.get_json(silent=True) or {}
            mid = d.get("merchant_id", "")
            key = request.headers.get("X-Merchant-Key") or d.get("api_key", "")
            if not bank.verify_merchant(mid, key): return jsonify({"error": "Invalid merchant API credentials"}), 401
            results = bank.settle_batch(mid, d.get("batch", []))
            return jsonify({"success": True, "results": results})

        @app.route("/api/bank/payer_settlement/<payer_id>")
        def bank_stmt(payer_id):
            return jsonify(bank.get_payer_settlement_statement(payer_id))

        @app.route("/api/bank/simulate_duplicate", methods=["POST"])
        def bank_sim_dup():
            cur = bank.conn.cursor()
            cur.execute("SELECT * FROM ledger WHERE status = 'SETTLED' LIMIT 1")
            row = cur.fetchone()
            if not row: return jsonify({"success": False, "message": "No settled transactions in ledger yet. Please settle a payment first."})
            sp = "sim_dup"
            cur.execute(f"SAVEPOINT {sp}")
            try:
                cur.execute("INSERT INTO ledger VALUES (?, ?, ?, ?, 'SETTLED', 'Duplicate simulation', ?, ?)", (row["token_id"], row["payer_id"], row["payee_id"], row["amount_paise"], row["token_timestamp"], int(time.time())))
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                return jsonify({"success": True, "message": "Duplicate unexpectedly accepted."})
            except Exception:
                cur.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                cur.execute(f"RELEASE SAVEPOINT {sp}")
                return jsonify({"success": True, "status": "DUPLICATE", "token_id": row["token_id"], "message": f"Double-spend detected! Token '{row['token_id']}' rejected atomically by SQLite PRIMARY KEY."})

        @app.route("/api/bank/trigger_merchant_sync", methods=["POST"])
        def bank_trigger_sync():
            merchants = bank.get_stats()["merchants"]
            if not merchants: return jsonify({"success": False, "message": "No registered merchants to sync."})
            reports = []
            for m in merchants:
                st, res = http_json(f"{m['merchant_url']}/api/merchant/sync", {})
                reports.append({"merchant_id": m["merchant_id"], "status": st, "report": res})
            return jsonify({"success": True, "reports": reports, "message": f"Triggered sync on {len(merchants)} merchants."})

    return app

def main():
    parser = argparse.ArgumentParser(description="Offline UPI Multi-Role Server")
    parser.add_argument("role", choices=["payer", "merchant", "bank", "all"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--bank-url", default="http://127.0.0.1:5003")
    args = parser.parse_args()

    default_ports = {"payer": 5001, "merchant": 5002, "bank": 5003}
    if args.role == "all":
        print("Starting Offline UPI — All 3 roles: Payer (5001), Merchant (5002), Bank (5003)...")
        procs = [
            subprocess.Popen([sys.executable, "server.py", "bank", "--port", "5003"]),
            subprocess.Popen([sys.executable, "server.py", "merchant", "--port", "5002", "--bank-url", "http://127.0.0.1:5003"]),
            subprocess.Popen([sys.executable, "server.py", "payer", "--port", "5001", "--bank-url", "http://127.0.0.1:5003"]),
        ]
        try:
            for p in procs: p.wait()
        except KeyboardInterrupt:
            for p in procs: p.terminate()
        return

    port = args.port or default_ports[args.role]
    app = create_app(args.role, port, args.bank_url)
    print(f"Offline UPI [{args.role.upper()}] running at http://127.0.0.1:{port}/")
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()
