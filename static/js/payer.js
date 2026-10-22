/**
 * Offline UPI Payment Simulator — Payer Device Dashboard Controller
 * Handles offline token generation, QR rendering, spend allowance tracking,
 * and token cancellation.
 */

let currentTokenPackage = null;
let qrcodeInstance = null;

document.addEventListener("DOMContentLoaded", () => {
  // Initialize shared common components
  if (window.OfflineUPI) {
    window.OfflineUPI.initNavigation("payer");
    window.OfflineUPI.initDemoGuide();
    window.OfflineUPI.initResetModal(loadPayerStatus);
  }

  loadPayerStatus();

  // Setup payment form submission
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
    const nameEl = document.getElementById("payer-name");
    if (nameEl) nameEl.innerText = data.payer_name || "Alice";

    const idEl = document.getElementById("payer-id-display");
    if (idEl) idEl.innerText = data.payer_id || "alice@upi";

    document.getElementById("allowance-display").innerText = `₹${(data.remaining_allowance || 0).toFixed(2)}`;
    document.getElementById("unsynced-display").innerText = `₹${(data.unsynced_spent || 0).toFixed(2)}`;
    document.getElementById("outstanding-count-display").innerText = data.outstanding_count || 0;

    // Progress bar calculations
    const cap = data.offline_cap || 500.0;
    const spent = data.unsynced_spent || 0.0;
    const percentage = Math.min(100, Math.round((spent / cap) * 100));

    const progressFill = document.getElementById("spend-progress-fill");
    if (progressFill) {
      progressFill.style.width = `${percentage}%`;
      progressFill.className = "progress-bar-fill";
      if (spent >= cap) {
        progressFill.classList.add("danger");
      } else if (spent >= cap * 0.8) {
        progressFill.classList.add("warning");
      }
    }

    const spendRatioText = document.getElementById("spend-ratio-text");
    if (spendRatioText) {
      spendRatioText.innerText = `₹${spent.toFixed(2)} / ₹${cap.toFixed(2)} (${percentage}%)`;
    }

    // Limit warning banner
    const warningBanner = document.getElementById("limit-warning-banner");
    const btnGenerate = document.getElementById("btn-generate");
    if (spent >= cap) {
      if (warningBanner) warningBanner.style.display = "block";
      if (btnGenerate) {
        btnGenerate.disabled = true;
        btnGenerate.innerText = "🛑 Offline Spend Limit (₹500.00) Reached";
      }
    } else {
      if (warningBanner) warningBanner.style.display = "none";
      if (btnGenerate) {
        btnGenerate.disabled = false;
        btnGenerate.innerText = "🔒 Generate & Sign Offline Token";
      }
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
  if (errorBox) errorBox.style.display = "none";

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

  const btnGenerate = document.getElementById("btn-generate");
  if (btnGenerate) {
    btnGenerate.disabled = true;
    btnGenerate.innerHTML = "⏳ Signing Token with RSA-2048...";
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
  } finally {
    if (btnGenerate) {
      btnGenerate.disabled = false;
      btnGenerate.innerText = "🔒 Generate & Sign Offline Token";
    }
  }
}

function displayTokenHandoff(tokenPkg) {
  const placeholder = document.getElementById("no-token-placeholder");
  const detailsBox = document.getElementById("token-details-box");
  const badge = document.getElementById("handoff-status-badge");

  if (placeholder) placeholder.style.display = "none";
  if (detailsBox) detailsBox.style.display = "block";
  if (badge) badge.style.display = "inline-block";

  const payload = tokenPkg.payload;
  document.getElementById("token-id-text").innerText = payload.tokenID;
  document.getElementById("token-payee-text").innerText = payload.payeeID;
  document.getElementById("token-amount-text").innerText = `₹${payload.amount.toFixed(2)}`;
  document.getElementById("token-time-text").innerText = new Date(payload.timestamp * 1000).toLocaleTimeString();

  const rawPreview = document.getElementById("raw-token-json-preview");
  if (rawPreview) {
    rawPreview.innerText = JSON.stringify(tokenPkg, null, 2);
  }

  // Generate QR Code
  const qrContainer = document.getElementById("qrcode");
  if (qrContainer) {
    qrContainer.innerHTML = "";
    try {
      const jsonStr = JSON.stringify(tokenPkg);
      qrcodeInstance = new QRCode(qrContainer, {
        text: jsonStr,
        width: 200,
        height: 200,
        colorDark: "#0f172a",
        colorLight: "#ffffff",
        correctLevel: QRCode.CorrectLevel.M
      });
    } catch (qrErr) {
      qrContainer.innerHTML = `<p style="font-size:16px;color:var(--text-muted);">QR code display error. Please use Copy Payment JSON below.</p>`;
    }
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
    if (indicator) {
      indicator.style.display = "block";
      setTimeout(() => { indicator.style.display = "none"; }, 5000);
    }
  });
}

function renderPayerTransactions(txList) {
  const tbody = document.getElementById("payer-transactions-tbody");
  const emptyState = document.getElementById("payer-empty-state");
  if (!tbody) return;

  if (!txList || txList.length === 0) {
    tbody.innerHTML = "";
    if (emptyState) emptyState.style.display = "block";
    return;
  }

  if (emptyState) emptyState.style.display = "none";

  tbody.innerHTML = txList.map(tx => {
    const statusBadgeClass =
      tx.status === "SETTLED" ? "badge-settled" :
      tx.status === "EXPORTED" ? "badge-pending" :
      tx.status === "CREATED" ? "badge-pending" :
      tx.status === "CANCELLED" ? "badge-rejected" : "badge-pending";

    const shortId = tx.token_id ? tx.token_id.slice(0, 8) + "..." : "—";

    return `
      <tr>
        <td><code>${shortId}</code></td>
        <td><strong>${tx.payee_id}</strong></td>
        <td><strong>₹${Number(tx.amount).toFixed(2)}</strong></td>
        <td><span class="badge ${statusBadgeClass}">${tx.status}</span></td>
      </tr>
    `;
  }).join("");
}

async function cancelToken(tokenId) {
  if (!confirm(`Cancel this un-exported token and restore its amount to your offline allowance?`)) {
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
  if (!errorBox) return;
  errorBox.innerText = msg;
  errorBox.style.display = "block";
}
