// Unified front-end logic for Offline UPI (Payer, Merchant, Bank)
const role = document.body.dataset.role;
let activeToken = null;
let qrInstance = null;

function showBanner(msg, type = "success") {
  const b = document.getElementById("alert-banner");
  const t = document.getElementById("alert-text");
  if (!b || !t) return;
  b.className = `banner banner-${type}`;
  t.textContent = msg;
  b.style.display = "flex";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// One place that maps every status in the system to a badge colour.
const BADGE = {
  SETTLED: "badge-settled", CANCELLED: "badge-settled",
  CREATED: "badge-pending", PENDING: "badge-pending", CANCEL_PENDING: "badge-pending", VOIDED: "badge-pending", FLAGGED: "badge-pending",
  DUPLICATE: "badge-duplicate", REJECTED: "badge-rejected", CANCELLED_BY_PAYER: "badge-rejected",
};
const badge = (s) => `<span class="badge ${BADGE[s] || "badge-rejected"}">${String(s).replace(/_/g, " ")}</span>`;
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Clipboard API only works on https or 127.0.0.1. On a LAN IP (http://192.168.x.x) fall back to execCommand.
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch (_) {}
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
  document.body.appendChild(ta); ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) {}
  ta.remove();
  return ok;
}

// Accepts whatever the merchant pastes: extra spaces/text around it, smart quotes from chat apps, or a wrapper object.
function parseTokenText(raw) {
  let t = raw.trim().replace(/[\u201C\u201D]/g, '"').replace(/[\u2018\u2019]/g, "'");
  const a = t.indexOf("{"), b = t.lastIndexOf("}");
  if (a === -1 || b <= a) throw new Error("No JSON object found. Paste the full token starting with { and ending with }.");
  let obj;
  try { obj = JSON.parse(t.slice(a, b + 1)); }
  catch (e) { throw new Error("Token JSON is incomplete or corrupted (" + e.message + "). Copy it again from the payer."); }
  if (obj && obj.token_package) obj = obj.token_package;
  if (!obj || !obj.payload || !obj.signature) throw new Error("This JSON is not a payment token (needs \"payload\" and \"signature\").");
  return obj;
}

async function cancelToken(tid) {
  const res = await api("/api/payer/cancel_token", { token_id: tid });
  if (res.success) showBanner(res.message, res.level || "warning");
  return res;
}

// api() already shows the error in the red banner, so don't also dump it to the console as "uncaught".
window.addEventListener("unhandledrejection", (e) => e.preventDefault());

async function api(url, data = null) {
  try {
    const opts = { method: data ? "POST" : "GET", headers: { "Content-Type": "application/json" } };
    if (data) opts.body = JSON.stringify(data);
    const res = await fetch(url, opts);
    const json = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(json.error || json.message || `Request failed (HTTP ${res.status})`);
    return json;
  } catch (err) {
    showBanner(err.message, "danger");
    throw err;
  }
}

// --- PAYER ---
if (role === "payer") {
  async function loadPayer() {
    try {
      const data = await api("/api/payer/state");
      const onboard = document.getElementById("payer-onboard");
      const main = document.getElementById("payer-main");
      if (!data.registered) {
        onboard.style.display = "block";
        main.style.display = "none";
        return;
      }
      onboard.style.display = "none";
      main.style.display = "block";

      document.getElementById("disp-payer-id").textContent = data.payer_id;
      document.getElementById("disp-bank-url").textContent = data.bank_url;
      document.getElementById("disp-remaining").textContent = `₹${(data.remaining_paise / 100).toFixed(2)}`;
      document.getElementById("disp-spent").textContent = `₹${(data.spent_paise / 100).toFixed(2)}`;
      document.getElementById("disp-paid").textContent = `₹${((data.paid_paise || 0) / 100).toFixed(2)}`;
      const pct = Math.min(100, Math.round((data.spent_paise / data.cap_paise) * 100));
      document.getElementById("allowance-bar").style.width = `${pct}%`;

      if (activeToken && !data.tokens.some(t => t.token_id === activeToken.payload.tokenID && ["CREATED", "CANCEL_PENDING"].includes(t.status))) {
        document.getElementById("token-card").style.display = "none"; activeToken = null;
      }
      const tbody = document.getElementById("payer-tokens-tbody");
      if (!data.tokens || data.tokens.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);">No offline payments yet.</td></tr>';
      } else {
        tbody.innerHTML = data.tokens.map(t => {
          const view = `<button class="btn btn-secondary btn-view-row" data-tid="${esc(t.token_id)}" style="padding:4px 8px;font-size:12px;">View</button>`;
          const cancel = t.status === "CREATED" ? ` <button class="btn btn-secondary btn-cancel-row" data-tid="${esc(t.token_id)}" style="padding:4px 8px;font-size:12px;">Request cancel</button>` : "";
          const act = view + cancel;
          return `<tr><td><code>${esc(t.token_id.slice(0, 8))}...</code></td><td>${esc(t.payee_id)}</td><td>₹${(t.amount_paise / 100).toFixed(2)}</td><td>${badge(t.status)}</td><td>${act}</td></tr>`;
        }).join("");

        document.querySelectorAll(".btn-view-row").forEach(btn => {
          btn.addEventListener("click", async () => {
            try {
              const res = await api(`/api/payer/token/${encodeURIComponent(btn.dataset.tid)}`);
              showToken(res.token_package, res.status);
            } catch (_) {}
          });
        });

        document.querySelectorAll(".btn-cancel-row").forEach(btn => {
          btn.addEventListener("click", async () => {
            btn.disabled = true;
            try { await cancelToken(btn.dataset.tid); } catch (_) {}
            loadPayer();
          });
        });
      }
    } catch (_) {}
  }

  document.getElementById("btn-reg-payer")?.addEventListener("click", async () => {
    const pid = document.getElementById("reg-payer-id").value.trim();
    const bUrl = document.getElementById("reg-bank-url").value.trim();
    if (!pid) return showBanner("Please enter a UPI ID", "danger");
    const res = await api("/api/payer/register", { payer_id: pid, bank_url: bUrl });
    if (res.success) { showBanner(res.message, "success"); loadPayer(); }
  });

  document.getElementById("btn-create-token")?.addEventListener("click", async () => {
    const amt = parseFloat(document.getElementById("pay-amount").value);
    const payee = document.getElementById("pay-payee").value.trim();
    if (!amt || amt <= 0) return showBanner("Please enter a valid amount", "danger");
    if (!payee) return showBanner("Please enter a Merchant UPI ID", "danger");

    const res = await api("/api/payer/create_token", { amount: amt, payee_id: payee });
    if (res.success) {
      showToken(res.token_package, "CREATED");
      showBanner("Payment token generated and signed offline!", "success");
      loadPayer();
    }
  });

  // Shows (or re-shows) a token: QR + the exact JSON text, so it can be checked or copied again any time.
  function showToken(pkg, status) {
    activeToken = pkg;
    const card = document.getElementById("token-card");
    card.style.display = "block";
    document.getElementById("ready-amt").textContent = `₹${(pkg.payload.amount_paise / 100).toFixed(2)}`;
    document.getElementById("ready-payee").textContent = pkg.payload.payeeID;
    document.getElementById("ready-tid").textContent = pkg.payload.tokenID.slice(0, 8) + "... (" + String(status).replace(/_/g, " ") + ")";
    document.getElementById("token-json-view").value = JSON.stringify(pkg);
    document.getElementById("btn-cancel-token").style.display = status === "CREATED" ? "" : "none";
    const qrBox = document.getElementById("qrcode");
    qrBox.innerHTML = "";
    qrInstance = new QRCode(qrBox, { text: JSON.stringify(pkg), width: 220, height: 220, correctLevel: QRCode.CorrectLevel.L });
    card.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  document.getElementById("token-json-view")?.addEventListener("click", (e) => e.target.select());
  document.getElementById("btn-hide-token")?.addEventListener("click", () => {
    document.getElementById("token-card").style.display = "none"; activeToken = null;
  });

  document.getElementById("btn-copy-token")?.addEventListener("click", async () => {
    if (!activeToken) return;
    const ok = await copyText(JSON.stringify(activeToken));
    if (ok) showBanner("Payment JSON copied! Paste it into Merchant POS.", "success");
    else { document.getElementById("token-json-view").select(); showBanner("Browser blocked auto-copy. The JSON box is selected, so press Ctrl+C.", "warning"); }
  });

  document.getElementById("btn-dl-token")?.addEventListener("click", () => {
    if (!activeToken) return;
    const blob = new Blob([JSON.stringify(activeToken, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `offline_token_${activeToken.payload.tokenID.slice(0, 8)}.json`;
    a.click();
    showBanner("Downloaded payment JSON file.", "success");
  });

  document.getElementById("btn-cancel-token")?.addEventListener("click", async () => {
    if (!activeToken) return;
    try {
      const res = await cancelToken(activeToken.payload.tokenID);
      if (res.success) {
        document.getElementById("token-card").style.display = "none";
        activeToken = null;
      }
    } catch (_) {}
    loadPayer();
  });

  document.getElementById("btn-refresh-payer")?.addEventListener("click", async () => {
    const res = await api("/api/payer/refresh", {});
    if (res.success) {
      showBanner(res.message, "success");
      loadPayer();
    }
  });

  loadPayer();
}

// --- MERCHANT ---
if (role === "merchant") {
  async function loadMerchant() {
    try {
      const data = await api("/api/merchant/state");
      const onboard = document.getElementById("merchant-onboard");
      const main = document.getElementById("merchant-main");
      if (!data.registered) {
        onboard.style.display = "block";
        main.style.display = "none";
        return;
      }
      onboard.style.display = "none";
      main.style.display = "block";

      document.getElementById("disp-merchant-id").textContent = data.merchant_id;
      document.getElementById("disp-payer-count").textContent = data.trusted_payers_count;

      const pending = (data.queue || []).filter(q => q.status === "PENDING");
      const pendingPaise = pending.reduce((sum, q) => sum + q.amount_paise, 0);
      document.getElementById("disp-sync-summary").textContent = `${pending.length} pending offline payment(s) awaiting bank sync (₹${(pendingPaise / 100).toFixed(2)}).`;

      const tbody = document.getElementById("merchant-queue-tbody");
      if (!data.queue || data.queue.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--muted);">No payments received yet.</td></tr>';
      } else {
        tbody.innerHTML = data.queue.map(q => {
          return `<tr><td><code>${esc(q.token_id.slice(0, 8))}...</code></td><td>${esc(q.payer_id)}</td><td>₹${(q.amount_paise / 100).toFixed(2)}</td><td>${badge(q.status)}</td><td><small>${esc(q.reason || "—")}</small></td></tr>`;
        }).join("");
      }
    } catch (_) {}
  }

  document.getElementById("btn-reg-merchant")?.addEventListener("click", async () => {
    const mid = document.getElementById("reg-merchant-id").value.trim();
    const bUrl = document.getElementById("reg-merchant-bank-url").value.trim();
    const mUrl = document.getElementById("reg-merchant-my-url").value.trim();
    if (!mid) return showBanner("Please enter a Shop UPI ID", "danger");
    const res = await api("/api/merchant/register", { merchant_id: mid, bank_url: bUrl, merchant_url: mUrl });
    if (res.success) { showBanner(res.message, "success"); loadMerchant(); }
  });

  document.getElementById("btn-refresh-cache")?.addEventListener("click", async () => {
    const res = await api("/api/merchant/refresh_payers", {});
    if (res.success) { showBanner(res.message, "success"); loadMerchant(); }
  });

  document.getElementById("token-file-input")?.addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (evt) => { document.getElementById("token-paste").value = evt.target.result; };
    reader.readAsText(file);
  });

  document.getElementById("btn-verify-token")?.addEventListener("click", async () => {
    const raw = document.getElementById("token-paste").value.trim();
    if (!raw) return showBanner("Please paste token JSON or select a file", "danger");
    let pkg;
    try { pkg = parseTokenText(raw); } catch (e) { return showBanner(e.message, "danger"); }

    const res = await api("/api/merchant/verify_token", { token_package: pkg });
    if (res.success) {
      showBanner(res.message, "success");
      document.getElementById("token-paste").value = "";
      loadMerchant();
    }
  });

  document.getElementById("btn-sync-bank")?.addEventListener("click", async () => {
    const res = await api("/api/merchant/sync", {});
    showBanner(res.message, res.success ? "success" : "warning");
    loadMerchant();
  });

  loadMerchant();
}

// --- BANK ---
if (role === "bank") {
  async function loadBank() {
    try {
      const data = await api("/api/bank/state");
      const st = data.stats || {};

      document.getElementById("bank-stat-total").textContent = st.total_received || 0;
      document.getElementById("bank-stat-settled").textContent = st.settled_count || 0;
      document.getElementById("bank-stat-settled-amt").textContent = `₹${((st.settled_amount_paise || 0) / 100).toFixed(2)}`;
      document.getElementById("bank-stat-duplicates").textContent = st.duplicate_count || 0;
      document.getElementById("bank-stat-rejected").textContent = st.rejected_count || 0;

      const netBadge = document.getElementById("bank-net-badge");
      const toggleBtn = document.getElementById("btn-toggle-outage");
      if (data.network_online) {
        netBadge.textContent = "ONLINE";
        netBadge.className = "badge badge-settled";
        toggleBtn.textContent = "Simulate Outage";
      } else {
        netBadge.textContent = "OFFLINE (OUTAGE)";
        netBadge.className = "badge badge-rejected";
        toggleBtn.textContent = "Restore Network";
      }

      const lbody = document.getElementById("bank-ledger-tbody");
      if (!st.ledger || st.ledger.length === 0) {
        lbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--muted);">No transactions in ledger yet.</td></tr>';
      } else {
        lbody.innerHTML = st.ledger.map(row => {
          return `<tr><td><code>${esc(String(row.token_id).slice(0, 8))}...</code></td><td>${esc(row.payer_id)}</td><td>${esc(row.payee_id)}</td><td>₹${(row.amount_paise / 100).toFixed(2)}</td><td>${badge(row.status)}</td><td><small>${esc(row.reason || "—")}</small></td></tr>`;
        }).join("");
      }

      document.getElementById("bank-payer-count").textContent = (st.payers || []).length;
      document.getElementById("bank-payers-tbody").innerHTML = (st.payers || []).map(p => `<tr><td>${esc(p.payer_id)}</td><td>${new Date(p.registered_at * 1000).toLocaleTimeString()}</td></tr>`).join("") || '<tr><td colspan="2" style="text-align:center;color:var(--muted);">No payers registered.</td></tr>';

      document.getElementById("bank-merchant-count").textContent = (st.merchants || []).length;
      document.getElementById("bank-merchants-tbody").innerHTML = (st.merchants || []).map(m => `<tr><td>${esc(m.merchant_id)}</td><td><small>${esc(m.merchant_url)}</small></td></tr>`).join("") || '<tr><td colspan="2" style="text-align:center;color:var(--muted);">No merchants registered.</td></tr>';
    } catch (_) {}
  }

  document.getElementById("btn-toggle-outage")?.addEventListener("click", async () => {
    const res = await api("/api/bank/toggle_network", {});
    showBanner(res.message, "warning");
    loadBank();
  });

  document.getElementById("btn-trigger-sync")?.addEventListener("click", async () => {
    const res = await api("/api/bank/trigger_merchant_sync", {});
    showBanner(res.message, res.success ? "success" : "warning");
    loadBank();
  });

  document.getElementById("btn-sim-duplicate")?.addEventListener("click", async () => {
    const res = await api("/api/bank/simulate_duplicate", {});
    showBanner(res.message, res.status === "DUPLICATE" ? "warning" : (res.success ? "success" : "danger"));
    loadBank();
  });

  document.getElementById("btn-copy-bank-key")?.addEventListener("click", async () => {
    const res = await api("/api/bank/public_key");
    if (res.public_key_pem) {
      const ok = await copyText(res.public_key_pem);
      showBanner(ok ? "Bank public key copied to clipboard!" : "Browser blocked copying. Use http://127.0.0.1 or copy manually.", ok ? "success" : "warning");
    }
  });

  document.getElementById("btn-refresh-bank")?.addEventListener("click", () => {
    loadBank();
    showBanner("Bank ledger refreshed.", "success");
  });

  loadBank();
}
