/**
 * Merchant POS Device Dashboard JavaScript
 * Handles local offline signature verification, persistent queue display,
 * and delayed synchronization with the central bank ledger.
 */

document.addEventListener("DOMContentLoaded", () => {
  loadMerchantStatus();

  // Button: Verify Token
  const btnVerify = document.getElementById("btn-verify-token");
  if (btnVerify) {
    btnVerify.addEventListener("click", handleVerifyToken);
  }

  // Button: Sync with Bank
  const btnSync = document.getElementById("btn-sync-bank");
  if (btnSync) {
    btnSync.addEventListener("click", handleSyncWithBank);
  }

  // Button: Refresh Queue
  const btnRefresh = document.getElementById("btn-refresh-queue");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", loadMerchantStatus);
  }

  // Button: Purge Settled
  const btnPurge = document.getElementById("btn-purge-settled");
  if (btnPurge) {
    btnPurge.addEventListener("click", handlePurgeSettled);
  }
});

function switchInputTab(tab) {
  const pasteTab = document.getElementById("tab-paste");
  const fileTab = document.getElementById("tab-file");
  const btnPaste = document.getElementById("tab-btn-paste");
  const btnFile = document.getElementById("tab-btn-file");

  if (tab === "paste") {
    pasteTab.style.display = "block";
    fileTab.style.display = "none";
    btnPaste.classList.add("active");
    btnFile.classList.remove("active");
  } else {
    pasteTab.style.display = "none";
    fileTab.style.display = "block";
    btnPaste.classList.remove("active");
    btnFile.classList.add("active");
  }
}

async function loadMerchantStatus() {
  try {
    const res = await fetch("/api/merchant/status");
    if (!res.ok) throw new Error("Failed to load merchant status");
    const data = await res.json();

    document.getElementById("merchant-id-display").innerText = data.merchant_id;
    document.getElementById("stat-total-count").innerText = data.total_queued;
    document.getElementById("stat-pending-count").innerText = data.pending_sync;
    document.getElementById("stat-pending-amount").innerText = `₹${data.pending_sync_amount.toFixed(2)}`;
    document.getElementById("stat-settled-count").innerText = data.settled;

    // Connectivity pill
    const pill = document.getElementById("merchant-conn-pill");
    if (data.bank_reachable) {
      pill.innerHTML = '<span class="status-pill status-online"><span class="status-dot"></span> Bank Network Reachable</span>';
    } else {
      pill.innerHTML = '<span class="status-pill status-offline"><span class="status-dot"></span> Bank Network Unreachable (Offline Mode)</span>';
    }

    // Queue summary text
    document.getElementById("queue-summary-text").innerText = `${data.pending_sync} tokens pending (₹${data.pending_sync_amount.toFixed(2)})`;

    // Render table
    renderMerchantQueue(data.queue || []);
  } catch (err) {
    console.error("Error loading merchant status:", err);
  }
}

async function handleVerifyToken() {
  const resultBox = document.getElementById("verification-result-box");
  resultBox.style.display = "none";

  const isPasteActive = document.getElementById("tab-paste").style.display !== "none";
  let tokenPackage = null;

  if (isPasteActive) {
    const rawText = document.getElementById("token-paste-input").value.trim();
    if (!rawText) {
      showVerificationResult(false, "Please paste a token JSON string.");
      return;
    }
    try {
      tokenPackage = JSON.parse(rawText);
    } catch (parseErr) {
      showVerificationResult(false, "Invalid JSON format: " + parseErr.message);
      return;
    }
    executeTokenVerification(tokenPackage);
  } else {
    // Read from file upload
    const fileInput = document.getElementById("token-file-input");
    if (!fileInput.files || fileInput.files.length === 0) {
      showVerificationResult(false, "Please select a token JSON file.");
      return;
    }
    const file = fileInput.files[0];
    const reader = new FileReader();
    reader.onload = (event) => {
      try {
        tokenPackage = JSON.parse(event.target.result);
        executeTokenVerification(tokenPackage);
      } catch (parseErr) {
        showVerificationResult(false, "Invalid JSON in uploaded file: " + parseErr.message);
      }
    };
    reader.readAsText(file);
  }
}

async function executeTokenVerification(tokenPkg) {
  try {
    const res = await fetch("/api/merchant/receive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token_package: tokenPkg })
    });
    const data = await res.json();

    if (data.accepted) {
      showVerificationResult(
        true,
        `✓ ${data.message} Verified in ${data.verification_time_ms.toFixed(3)}ms (NFR <= 1000ms: ${data.within_nfr_latency}). Queued as 'Pending Sync'.`
      );
      // Clear inputs
      document.getElementById("token-paste-input").value = "";
      document.getElementById("token-file-input").value = "";
    } else {
      showVerificationResult(false, `❌ REJECTED: ${data.reason}`);
    }

    // Refresh queue & statistics
    loadMerchantStatus();
  } catch (err) {
    showVerificationResult(false, "Communication error: " + err.message);
  }
}

function showVerificationResult(isSuccess, message) {
  const box = document.getElementById("verification-result-box");
  box.className = isSuccess ? "alert alert-success" : "alert alert-danger";
  box.innerText = message;
  box.style.display = "block";
}

async function handleSyncWithBank() {
  const syncBtn = document.getElementById("btn-sync-bank");
  const reportBox = document.getElementById("sync-report-box");
  const indicator = document.getElementById("sync-status-indicator");

  syncBtn.disabled = true;
  syncBtn.innerText = "⏳ Syncing with Bank Network...";
  indicator.className = "badge badge-created";
  indicator.innerText = "SYNCING";

  try {
    const res = await fetch("/api/merchant/sync", { method: "POST" });
    const data = await res.json();

    if (res.status === 503) {
      // Bank is simulated offline or network unreachable
      reportBox.className = "alert alert-warning";
      reportBox.innerHTML = `⚠️ <strong>Bank Unreachable:</strong> ${data.message || "Central banking network is currently offline. Queued tokens safely retained in local SQLite queue."}`;
      reportBox.style.display = "block";
      indicator.className = "badge badge-pending";
      indicator.innerText = "PENDING SYNC";
    } else if (res.ok) {
      const settledCount = data.settled_count || 0;
      const dupCount = data.duplicate_count || 0;
      const flaggedCount = data.flagged_count || 0;

      reportBox.className = "alert alert-success";
      reportBox.innerHTML = `
        <strong>Sync Completed Successfully:</strong><br>
        &bull; Settled Transactions: <strong>${settledCount}</strong><br>
        &bull; Duplicates Blocked: <strong>${dupCount}</strong><br>
        &bull; Flagged for 24h Review: <strong>${flaggedCount}</strong><br>
        &bull; Batch Processing Time: <strong>${data.processing_time_ms} ms</strong>
      `;
      reportBox.style.display = "block";
      indicator.className = "badge badge-settled";
      indicator.innerText = "SYNC COMPLETE";
    } else {
      reportBox.className = "alert alert-danger";
      reportBox.innerText = `Sync Error: ${data.error || "Unknown server error."}`;
      reportBox.style.display = "block";
      indicator.className = "badge badge-rejected";
      indicator.innerText = "SYNC FAILED";
    }

    loadMerchantStatus();
  } catch (err) {
    reportBox.className = "alert alert-warning";
    reportBox.innerText = `Bank connection error: ${err.message}. Pending payments preserved locally.`;
    reportBox.style.display = "block";
  } finally {
    syncBtn.disabled = false;
    syncBtn.innerText = "🌐 Sync with Bank Switch";
  }
}

async function handlePurgeSettled() {
  if (!confirm("Remove settled payments from the active local queue view?")) return;
  try {
    const res = await fetch("/api/merchant/purge_settled", { method: "POST" });
    if (res.ok) {
      loadMerchantStatus();
    }
  } catch (err) {
    console.error(err);
  }
}

function renderMerchantQueue(queue) {
  const tbody = document.getElementById("merchant-queue-tbody");
  if (!queue || queue.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 20px;">No offline payments in local queue.</td></tr>`;
    return;
  }

  tbody.innerHTML = queue.map(item => {
    const statusBadgeClass =
      item.status === "Settled" ? "badge-settled" :
      item.status === "Pending Sync" ? "badge-pending" : "badge-rejected";

    const timeFormatted = new Date(item.received_at * 1000).toLocaleTimeString();
    const latencyFormatted = item.verification_time_ms ? `${item.verification_time_ms.toFixed(2)} ms` : "N/A";

    return `
      <tr>
        <td><span class="token-code">${item.token_id.slice(0, 8)}...</span></td>
        <td>${item.payer_id}</td>
        <td><strong>₹${item.amount.toFixed(2)}</strong></td>
        <td>${timeFormatted}</td>
        <td><span style="font-family: monospace; font-size: 0.8rem;">${latencyFormatted}</span></td>
        <td><span class="badge ${statusBadgeClass}">${item.status.toUpperCase()}</span></td>
      </tr>
    `;
  }).join("");
}
