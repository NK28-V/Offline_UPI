import argparse, json, os, secrets, subprocess, sys, time, urllib.error, urllib.request
from typing import Any, Dict, Optional, Tuple
from flask import Flask, jsonify, render_template, request
import core
from core import Bank, Merchant, Payer, generate_keypair

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

def get_cfg(obj, key):
    cur = obj.conn.cursor()
    cur.execute("SELECT value FROM config WHERE key = ?", (key,))
    row = cur.fetchone()
    return row["value"] if row else None

def adopt_lock(obj, lock_s):
    """Payer & merchant use the BANK's lock window so all three devices agree on the deadlines."""
    if isinstance(lock_s, int) and lock_s > 0:
        core.LOCK_S = lock_s
        with obj.conn:
            obj.conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('lock_s', ?)", (str(lock_s),))

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
        if get_cfg(payer, "lock_s"): core.LOCK_S = int(get_cfg(payer, "lock_s"))

        @app.route("/api/payer/state")
        def payer_state():
            return jsonify(payer.get_info())

        @app.route("/api/payer/register", methods=["POST"])
        def payer_register():
            d = request.get_json(silent=True) or {}
            pid, b_url = str(d.get("payer_id") or "").strip(), str(d.get("bank_url") or bank_url).rstrip("/")
            if not pid: return jsonify({"error": "Payer ID required"}), 400
            if payer.get_info()["registered"]: return jsonify({"error": "This wallet is already registered. Delete payer_*.db to start over."}), 409
            st, res = http_json(f"{b_url}/api/bank/public_key")
            if st != 200: return jsonify({"error": f"Failed reaching bank: {res.get('error')}"}), 502
            priv_pem, pub_pem = generate_keypair()
            st, reg_res = http_json(f"{b_url}/api/bank/register_payer", {"payer_id": pid, "public_key_pem": pub_pem})
            if st != 200: return jsonify({"error": f"Registration rejected by bank: {reg_res.get('error')}"}), (409 if st == 409 else 502)
            payer.register(pid, b_url, res["public_key_pem"], priv_pem, pub_pem)
            adopt_lock(payer, res.get("lock_s"))
            return jsonify({"success": True, "message": f"Wallet registered for {pid}"})

        @app.route("/api/payer/create_token", methods=["POST"])
        def payer_token():
            d = request.get_json(silent=True) or {}
            try:
                amt = float(d.get("amount", 0))
                if amt != amt or amt in (float("inf"), float("-inf")): raise ValueError("Invalid amount.")
                amt_paise = int(round(amt * 100))
                pkg = payer.create_token(str(d.get("payee_id") or ""), amt_paise)
                return jsonify({"success": True, "token_package": pkg})
            except (ValueError, TypeError, OverflowError) as e:
                return jsonify({"error": str(e) if "could not convert" not in str(e) else "Amount must be a number."}), 400

        def send_cancel(cancel_pkg, b_url):
            """Ask the bank to void the token. Returns the payer-side status after the answer."""
            tid = cancel_pkg["payload"]["tokenID"]
            st, res = http_json(f"{b_url}/api/bank/cancel", {"cancel": cancel_pkg})
            if st in (200, 409) and res.get("status"):
                return payer.apply_cancel_result(tid, res["status"]), res.get("message", "")
            return "CANCEL_PENDING", res.get("error", "Bank unreachable")

        @app.route("/api/payer/token/<token_id>")
        def payer_get_token(token_id):
            t = payer.get_token(token_id)
            if not t: return jsonify({"error": "Token not found in this wallet."}), 404
            return jsonify({"success": True, **t})

        @app.route("/api/payer/cancel_token", methods=["POST"])
        def payer_cancel():
            tid = (request.get_json(silent=True) or {}).get("token_id")
            info = payer.get_info()
            if not info["registered"]: return jsonify({"error": "Wallet not registered"}), 400
            if not isinstance(tid, str): return jsonify({"error": "token_id required"}), 400
            cancel_pkg, why = payer.request_cancel(tid)
            if not cancel_pkg: return jsonify({"error": why}), 409
            outcome, detail = send_cancel(cancel_pkg, info["bank_url"])
            msgs = {
                "CANCELLED": (detail or "Reclaim confirmed by bank. Amount restored.", "success"),
                "REFUNDED": ("The merchant already refunded this payment.", "success"),
                "SETTLED": ("Cannot reclaim: the merchant synced this payment in time, so it stays PAID.", "danger"),
                "FLAGGED": ("Cannot reclaim: the bank already received this token. It stays paid.", "danger"),
                "CREATED": (detail or "Still locked: the merchant window is open.", "warning"),
                "CANCEL_PENDING": ("Bank unreachable, so the reclaim is PENDING. Amount stays blocked until the bank confirms. "
                                   "Click 'Refresh from Bank' when online.", "warning"),
            }
            msg, level = msgs.get(outcome, (detail, "warning"))
            return jsonify({"success": True, "outcome": outcome, "level": level, "message": msg})

        @app.route("/api/payer/refresh", methods=["POST"])
        def payer_refresh():
            info = payer.get_info()
            if not info["registered"]: return jsonify({"error": "Wallet not registered"}), 400
            before = {t["token_id"]: t["status"] for t in info["tokens"]}
            # 1) Retry any cancellations made while the bank was offline (first to reach the bank wins).
            for pkg in payer.pending_cancels():
                send_cancel(pkg, info["bank_url"])
            # 2) Pull the bank-signed statement and free allowance only for tokens the bank has decided on.
            st, res = http_json(f"{info['bank_url']}/api/bank/payer_settlement/{info['payer_id']}")
            if st != 200: return jsonify({"error": f"Failed to fetch statement: {res.get('error')}"}), 502
            try:
                payer.reconcile(res)
            except ValueError as e:
                return jsonify({"error": str(e)}), 400
            after = payer.get_info()["tokens"]
            c = {"SETTLED": 0, "CANCELLED": 0, "FLAGGED": 0, "REFUNDED": 0}
            amt = {"SETTLED": 0, "CANCELLED": 0, "FLAGGED": 0, "REFUNDED": 0}
            for t in after:
                if before.get(t["token_id"]) != t["status"] and t["status"] in c:
                    c[t["status"]] += 1; amt[t["status"]] += t["amount_paise"]
            still = sum(1 for t in after if t["status"] == "CANCEL_PENDING")
            parts = []
            paid = amt["SETTLED"] + amt["FLAGGED"]
            if paid: parts.append(f"₹{paid/100:.2f} PAID to merchant ({c['SETTLED'] + c['FLAGGED']} token(s), debited from your bank account). "
                                  f"Your offline limit is refilled because the bank has collected it. This is NOT a refund.")
            back = amt["CANCELLED"] + amt["REFUNDED"]
            if back: parts.append(f"₹{back/100:.2f} RETURNED to you ({c['CANCELLED'] + c['REFUNDED']} token(s): merchant refund or unused-token reclaim).")
            if still: parts.append(f"{still} reclaim(s) still pending.")
            msg = " ".join(parts) or "Up to date. Nothing new from the bank."
            return jsonify({"success": True, "reconciled": c, "message": msg})

    elif role == "merchant":
        merchant = Merchant(f"merchant_{port}.db")
        if get_cfg(merchant, "lock_s"): core.LOCK_S = int(get_cfg(merchant, "lock_s"))

        @app.route("/api/merchant/state")
        def merchant_state():
            return jsonify(merchant.get_info())

        @app.route("/api/merchant/register", methods=["POST"])
        def merchant_register():
            d = request.get_json(silent=True) or {}
            mid = str(d.get("merchant_id") or "").strip()
            b_url = str(d.get("bank_url") or bank_url).rstrip("/")
            m_url = str(d.get("merchant_url") or f"http://127.0.0.1:{port}").rstrip("/")
            if not mid: return jsonify({"error": "Merchant ID required"}), 400
            if merchant.get_info()["registered"]: return jsonify({"error": "This shop is already registered. Delete merchant_*.db to start over."}), 409
            st, res = http_json(f"{b_url}/api/bank/register_merchant", {"merchant_id": mid, "merchant_url": m_url})
            if st != 200: return jsonify({"error": f"Bank registration failed: {res.get('error')}"}), (409 if st == 409 else 502)
            merchant.register(mid, b_url, m_url, res["api_key"])
            adopt_lock(merchant, res.get("lock_s"))
            _, p_res = http_json(f"{b_url}/api/bank/trusted_payers")
            if isinstance(p_res, list): merchant.update_trusted_payers(p_res)
            return jsonify({"success": True, "message": f"Shop {mid} registered."})

        @app.route("/api/merchant/refresh_payers", methods=["POST"])
        def merchant_refresh_payers():
            info = merchant.get_info()
            if not info["registered"]: return jsonify({"error": "Shop not registered"}), 400
            st, res = http_json(f"{info['bank_url']}/api/bank/trusted_payers")
            if st != 200: return jsonify({"error": f"Failed fetching payers: {res.get('error')}"}), 502
            if not isinstance(res, list): return jsonify({"error": "Bank returned an invalid payer list"}), 502
            merchant.update_trusted_payers(res)
            return jsonify({"success": True, "count": len(res), "message": f"Downloaded {len(res)} trusted payer keys."})

        @app.route("/api/merchant/verify_token", methods=["POST"])
        def merchant_verify():
            body = request.get_json(silent=True)
            pkg = body.get("token_package") if isinstance(body, dict) else None
            if isinstance(pkg, str):
                try: pkg = json.loads(pkg)
                except Exception: return jsonify({"error": "Invalid JSON format"}), 400
            if isinstance(pkg, dict) and "token_package" in pkg: pkg = pkg["token_package"]
            ok, msg = merchant.verify_and_queue(pkg)
            if not ok and msg.startswith("Unknown payer"):
                # Payer may have registered after this shop. If the bank happens to be reachable, refresh keys once and retry.
                info = merchant.get_info()
                st, res = http_json(f"{info['bank_url']}/api/bank/trusted_payers")
                if st == 200 and isinstance(res, list):
                    merchant.update_trusted_payers(res)
                    ok, msg = merchant.verify_and_queue(pkg)
                else:
                    msg += " Bank unreachable, so keys could not be refreshed. Click 'Refresh Trusted Payers' when online."
            if ok: return jsonify({"success": True, "message": msg})
            return jsonify({"success": False, "error": msg, "message": msg}), 400

        @app.route("/api/merchant/refund", methods=["POST"])
        def merchant_refund():
            info = merchant.get_info()
            if not info["registered"]: return jsonify({"error": "Shop not registered"}), 400
            tid = (request.get_json(silent=True) or {}).get("token_id")
            t = merchant.get_token_pkg(tid) if isinstance(tid, str) else None
            if not t: return jsonify({"error": "Payment not found in this shop's queue."}), 404
            if t["status"] not in ("PENDING", "SETTLED"): return jsonify({"error": f"Cannot refund a payment that is {t['status']}."}), 409
            st, res = http_json(f"{info['bank_url']}/api/bank/merchant_refund", {"merchant_id": info["merchant_id"], "token_package": t["token_package"]}, {"X-Merchant-Key": merchant.get_api_key()})
            if st != 200: return jsonify({"error": f"Refund needs the bank online: {res.get('error', 'unreachable')}"}), 502
            merchant.mark_refunded(tid, res.get("message", "Refunded"))
            return jsonify({"success": True, "message": res.get("message", "Refunded.")})

        @app.route("/api/merchant/sync", methods=["POST"])
        def merchant_sync():
            info = merchant.get_info()
            if not info["registered"]: return jsonify({"error": "Shop not registered"}), 400
            batch = merchant.get_pending_batch()
            if not batch: return jsonify({"success": True, "message": "No pending offline tokens to sync.", "results": []})
            api_key = merchant.get_api_key()
            st, res = http_json(f"{info['bank_url']}/api/bank/sync", {"merchant_id": info["merchant_id"], "batch": batch}, {"X-Merchant-Key": api_key})
            if st != 200:
                return jsonify({"success": False, "message": "Bank is offline or unreachable. Queued payments are safely kept locally.", "error": res.get("error")}), 200
            results = res.get("results", []) if isinstance(res, dict) else []
            merchant.apply_sync_results(results)
            counts = {}
            for r in results: counts[r.get("status")] = counts.get(r.get("status"), 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in counts.items())
            return jsonify({"success": True, "results": results, "message": f"Synced {len(batch)} payment(s) with bank: {summary}."})

    elif role == "bank":
        bank = Bank(f"bank_{port}.db")
        net = {"online": True}

        @app.route("/api/bank/state")
        def bank_state():
            stats = bank.get_stats()
            return jsonify({"network_online": net["online"], "public_key": bank.get_public_key(), "lock_s": core.LOCK_S, "stats": stats})

        @app.route("/api/bank/public_key")
        def bank_pub():
            return jsonify({"public_key_pem": bank.get_public_key(), "lock_s": core.LOCK_S})

        @app.route("/api/bank/register_payer", methods=["POST"])
        def bank_reg_payer():
            d = request.get_json(silent=True) or {}
            ok, msg = bank.register_payer(d.get("payer_id"), d.get("public_key_pem"))
            if not ok: return jsonify({"error": msg}), (409 if "already registered" in msg else 400)
            return jsonify({"success": True, "bank_public_key": bank.get_public_key()})

        @app.route("/api/bank/register_merchant", methods=["POST"])
        def bank_reg_merch():
            d = request.get_json(silent=True) or {}
            key = secrets.token_hex(16)
            ok, msg = bank.register_merchant(d.get("merchant_id"), d.get("merchant_url"), key)
            if not ok: return jsonify({"error": msg}), (409 if "already registered" in msg else 400)
            return jsonify({"success": True, "api_key": key, "lock_s": core.LOCK_S})

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
            d = request.get_json(silent=True)
            if not isinstance(d, dict): return jsonify({"error": "JSON body required"}), 400
            mid = d.get("merchant_id", "")
            key = request.headers.get("X-Merchant-Key") or d.get("api_key", "")
            if not bank.verify_merchant(mid, key): return jsonify({"error": "Invalid merchant API credentials"}), 401
            results = bank.settle_batch(mid, d.get("batch", []))
            return jsonify({"success": True, "results": results})

        @app.route("/api/bank/cancel", methods=["POST"])
        def bank_cancel():
            if not net["online"]: return jsonify({"error": "Bank network offline (Simulated Outage)"}), 503
            d = request.get_json(silent=True)
            ok, status, msg = bank.cancel_token(d.get("cancel") if isinstance(d, dict) else None)
            if status == "INVALID": return jsonify({"error": msg}), 400
            return jsonify({"success": ok, "status": status, "message": msg}), (200 if ok else 409)

        @app.route("/api/bank/merchant_refund", methods=["POST"])
        def bank_merchant_refund():
            if not net["online"]: return jsonify({"error": "Bank network offline (Simulated Outage)"}), 503
            d = request.get_json(silent=True)
            if not isinstance(d, dict): return jsonify({"error": "JSON body required"}), 400
            mid = d.get("merchant_id", "")
            if not bank.verify_merchant(mid, request.headers.get("X-Merchant-Key", "")): return jsonify({"error": "Invalid merchant API credentials"}), 401
            ok, status, msg = bank.merchant_refund(mid, d.get("token_package"))
            if not ok: return jsonify({"error": msg}), 400
            return jsonify({"success": True, "status": status, "message": msg})

        @app.route("/api/bank/payer_settlement/<payer_id>")
        def bank_stmt(payer_id):
            if not net["online"]: return jsonify({"error": "Bank network offline (Simulated Outage)"}), 503
            return jsonify(bank.get_payer_settlement_statement(payer_id))

        @app.route("/api/bank/simulate_duplicate", methods=["POST"])
        def bank_sim_dup():
            return jsonify(bank.simulate_duplicate())

        @app.route("/api/bank/trigger_merchant_sync", methods=["POST"])
        def bank_trigger_sync():
            merchants = bank.get_stats()["merchants"]
            if not merchants: return jsonify({"success": False, "message": "No registered merchants to sync."})
            reports = []
            for m in merchants:
                st, res = http_json(f"{m['merchant_url']}/api/merchant/sync", {})
                reports.append({"merchant_id": m["merchant_id"], "status": st, "report": res})
            details = "; ".join(f"{r['merchant_id']}: {r['report'].get('message') or r['report'].get('error', 'unreachable')}" for r in reports)
            return jsonify({"success": True, "reports": reports, "message": f"Triggered sync on {len(merchants)} merchant(s). {details}"})

    return app

def main():
    parser = argparse.ArgumentParser(description="Offline UPI Multi-Role Server")
    parser.add_argument("role", choices=["payer", "merchant", "bank", "all"])
    parser.add_argument("--port", type=int)
    parser.add_argument("--bank-url", default="http://127.0.0.1:5003")
    parser.add_argument("--lock-seconds", type=int, default=48 * 3600,
                        help="Merchant settlement window (bank side). Default 48h; use e.g. 120 for a live demo.")
    args = parser.parse_args()
    core.LOCK_S = args.lock_seconds

    default_ports = {"payer": 5001, "merchant": 5002, "bank": 5003}
    if args.role == "all":
        here = os.path.abspath(__file__)
        print("Starting Offline UPI — All 3 roles: Payer (5001), Merchant (5002), Bank (5003)...")
        procs = [
            subprocess.Popen([sys.executable, here, "bank", "--port", "5003", "--lock-seconds", str(args.lock_seconds)]),
            subprocess.Popen([sys.executable, here, "merchant", "--port", "5002", "--bank-url", "http://127.0.0.1:5003"]),
            subprocess.Popen([sys.executable, here, "payer", "--port", "5001", "--bank-url", "http://127.0.0.1:5003"]),
        ]
        try:
            for p in procs: p.wait()
        except KeyboardInterrupt:
            for p in procs: p.terminate()
        return

    port = args.port or default_ports[args.role]
    app = create_app(args.role, port, args.bank_url)
    lock = f" · merchant window {args.lock_seconds}s" if args.role == "bank" else ""
    print(f"Offline UPI [{args.role.upper()}] running at http://127.0.0.1:{port}/{lock}")
    app.run(host="0.0.0.0", port=port, debug=False)

if __name__ == "__main__":
    main()
