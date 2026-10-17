/**
 * Payer Device Dashboard JavaScript
 * Handles offline token generation, QR rendering, spend allowance tracking,
 * and token cancellation.
 */

let currentTokenPackage = null;
let qrcodeInstance = null;

document.addEventListener("DOMContentLoaded", () => {
  loadPayerStatus();

  // Setup form submission
  const paymentForm = document.getElementById("payment-form");
  if (paymentForm) {
    paymentForm.addEventListener("submit", handleGeneratePayment);
  }

  // Setup download JSON button
  const btnDownload = document.getElementById("btn-download-json");
  if (btnDownload) {
    btnDownload.addEventListener("click", downloadTokenJson);
  }

  // Setup copy JSON button
  const btnCopy = document.getElementById("btn-copy-token");
  if (btnCopy) {
    btnCopy.addEventListener("click", copyTokenJson);
  }

  // Setup refresh button
  const btnRefresh = document.getElementById("btn-refresh-payer");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", loadPayerStatus);
  }
});

async function loadPayerStatus() {
  try {
    const res = await fetch("/api/payer/status");
    if (!res.ok) throw new Error("Failed to load payer status");
    const data = await res.json();

    // Update balances
    document.getElementById("payer-name").innerText = data.payer_name || "Alice";
    document.getElementById("payer-id-display").innerText = data.payer_id;
    document.getElementById("allowance-display").innerText = `₹${data.remaining_allowance.toFixed(2)}`;
    document.getElementById("unsynced-display").innerText = `₹${data.unsynced_spent.toFixed(2)}`;
    document.getElementById("outstanding-count-display").innerText = data.outstanding_count;

    // Progress bar calculations
    const cap = data.offline_cap || 500.0;
    const spent = data.unsynced_spent || 0.0;
    const percentage = Math.min(100, Math.round((spent / cap) * 100));

    const progressFill = document.getElementById("spend-progress-fill");
    progressFill.style.width = `${percentage}%`;
    progressFill.className = "progress-bar-fill";
    if (spent >= cap) {
      progressFill.classList.add("danger");
    } else if (spent >= cap * 0.8) {
      progressFill.classList.add("warning");
    }

    document.getElementById("spend-ratio-text").innerText = `₹${spent.toFixed(2)} / ₹${cap.toFixed(2)} (${percentage}%)`;

    // Limit warning banner
    const warningBanner = document.getElementById("limit-warning-banner");
    const btnGenerate = document.getElementById("btn-generate");
    if (spent >= cap) {
      warningBanner.style.display = "block";
      btnGenerate.disabled = true;
      btnGenerate.innerText = "🛑 Offline Spend Limit (₹500) Reached";
    } else {
      warningBanner.style.display = "none";
      btnGenerate.disabled = false;
      btnGenerate.innerText = "🔒 Generate & Sign Offline Token";
    }

    // Render transactions table
    renderPayerTransactions(data.transactions || []);
  } catch (err) {
    console.error("Error loading payer status:", err);
  }
}

async function handleGeneratePayment(e) {
  e.preventDefault();
  const errorBox = document.getElementById("form-error-msg");
  errorBox.style.display = "none";

  const payeeId = document.getElementById("payee-id-input").value.trim();
  const amount = parseFloat(document.getElementById("amount-input").value);

  if (!payeeId) {
    showError("Please enter a valid Merchant / Payee ID.");
    return;
  }

  if (isNaN(amount) || amount <= 0) {
    showError("Please enter a positive numeric amount.");
    return;
  }

  try {
    const res = await fetch("/api/payer/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ amount, payee_id: payeeId })
    });

    const data = await res.json();
    if (!res.ok) {
      showError(data.error || "Failed to generate token.");
      return;
    }

    // Success: store token package and render preview
    currentTokenPackage = data.token_package;
    displayTokenHandoff(currentTokenPackage);

    // Reset amount input
    document.getElementById("amount-input").value = "";

    // Refresh balance stats
    loadPayerStatus();
  } catch (err) {
    showError("Network/Server error: " + err.message);
  }
}

function displayTokenHandoff(tokenPkg) {
  const placeholder = document.getElementById("no-token-placeholder");
  const detailsBox = document.getElementById("token-details-box");
  const badge = document.getElementById("handoff-status-badge");

  placeholder.style.display = "none";
  detailsBox.style.display = "block";
  badge.style.display = "inline-block";

  const payload = tokenPkg.payload;
  document.getElementById("token-id-text").innerText = payload.tokenID;
  document.getElementById("token-payee-text").innerText = payload.payeeID;
  document.getElementById("token-amount-text").innerText = `₹${payload.amount.toFixed(2)}`;
  document.getElementById("token-time-text").innerText = new Date(payload.timestamp * 1000).toLocaleTimeString();

  // Generate QR Code
  const qrContainer = document.getElementById("qrcode");
  qrContainer.innerHTML = "";
  try {
    const jsonStr = JSON.stringify(tokenPkg);
    qrcodeInstance = new QRCode(qrContainer, {
      text: jsonStr,
      width: 180,
      height: 180,
      colorDark: "#0f172a",
      colorLight: "#ffffff",
      correctLevel: QRCode.CorrectLevel.M
    });
  } catch (qrErr) {
    qrContainer.innerHTML = `<p style="font-size:0.8rem;color:var(--text-muted);">QR rendering fallback: Use JSON download or copy below.</p>`;
  }
}

function downloadTokenJson() {
  if (!currentTokenPackage) return;
  const tid = currentTokenPackage.payload.tokenID;
  fetch("/api/payer/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_id: tid })
  }).then(() => loadPayerStatus()).catch(() => {});

  const dataStr = "data:text/json;charset=utf-8," + encodeURIComponent(JSON.stringify(currentTokenPackage, null, 2));
  const downloadAnchor = document.createElement("a");
  downloadAnchor.setAttribute("href", dataStr);
  downloadAnchor.setAttribute("download", `offline_token_${tid.slice(0, 8)}.json`);
  document.body.appendChild(downloadAnchor);
  downloadAnchor.click();
  downloadAnchor.remove();
}

function copyTokenJson() {
  if (!currentTokenPackage) return;
  const tid = currentTokenPackage.payload.tokenID;
  fetch("/api/payer/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token_id: tid })
  }).then(() => loadPayerStatus()).catch(() => {});

  const jsonStr = JSON.stringify(currentTokenPackage, null, 2);
  navigator.clipboard.writeText(jsonStr).then(() => {
    const indicator = document.getElementById("copy-success-indicator");
    indicator.style.display = "block";
    setTimeout(() => { indicator.style.display = "none"; }, 3000);
  });
}

function renderPayerTransactions(txList) {
  const tbody = document.getElementById("payer-transactions-tbody");
  if (!txList || txList.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 20px;">No offline payments recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = txList.map(tx => {
    const statusBadgeClass =
      tx.status === "SETTLED" ? "badge-settled" :
      tx.status === "EXPORTED" ? "badge-pending" :
      tx.status === "CREATED" ? "badge-created" :
      tx.status === "CANCELLED" ? "badge-rejected" : "badge-pending";

    const isCancellable = tx.status === "CREATED";
    const actionBtn = isCancellable
      ? `<button class="btn btn-outline btn-sm" style="color:var(--danger);" onclick="cancelToken('${tx.token_id}')">Cancel</button>`
      : tx.status === "EXPORTED"
      ? `<span style="font-size:0.75rem;color:var(--text-muted); font-style:italic;" title="Reservation locked until bank settlement">Locked (In Transit)</span>`
      : `<span style="font-size:0.75rem;color:var(--text-muted);">&mdash;</span>`;

    const timeFormatted = new Date(tx.timestamp * 1000).toLocaleTimeString();

    return `
      <tr>
        <td><span class="token-code">${tx.token_id.slice(0, 8)}...</span></td>
        <td>${tx.payee_id}</td>
        <td><strong>₹${tx.amount.toFixed(2)}</strong></td>
        <td>${timeFormatted}</td>
        <td><span class="badge ${statusBadgeClass}">${tx.status}</span></td>
        <td>${actionBtn}</td>
      </tr>
    `;
  }).join("");
}

async function cancelToken(tokenId) {
  if (!confirm(`Cancel this outstanding token and restore its amount to your offline allowance?`)) {
    return;
  }

  try {
    const res = await fetch("/api/payer/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token_id: tokenId })
    });
    const data = await res.json();
    if (res.ok) {
      loadPayerStatus();
    } else {
      alert("Error cancelling token: " + data.error);
    }
  } catch (err) {
    alert("Network error: " + err.message);
  }
}

function showError(msg) {
  const errorBox = document.getElementById("form-error-msg");
  errorBox.innerText = msg;
  errorBox.style.display = "block";
}
