/**
 * Offline UPI Payment Simulator — Merchant POS Terminal Controller
 * Handles local offline signature verification, persistent queue display,
 * and delayed synchronization with the central bank ledger.
 */

document.addEventListener("DOMContentLoaded", () => {
  // Initialize shared common components
  if (window.OfflineUPI) {
    window.OfflineUPI.initNavigation("merchant");
    window.OfflineUPI.initDemoGuide();
    window.OfflineUPI.initResetModal(loadMerchantStatus);
  }

  loadMerchantStatus();

  // Button: Verify Token
  const btnVerify = document.getElementById("btn-verify-token");
  if (btnVerify) {
    btnVerify.addEventListener("click", handleVerifyToken);
  }

  // Button: Sync with Bank Switch
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
    if (pasteTab) pasteTab.style.display = "block";
    if (fileTab) fileTab.style.display = "none";
    if (btnPaste) btnPaste.classList.add("active");
    if (btnFile) btnFile.classList.remove("active");
  } else {
    if (pasteTab) pasteTab.style.display = "none";
    if (fileTab) fileTab.style.display = "block";
    if (btnPaste) btnPaste.classList.remove("active");
    if (btnFile) btnFile.classList.add("active");
  }
}

async function loadMerchantStatus() {
  try {
    const res = await fetch("/api/merchant/status");
    if (!res.ok) throw new Error("Failed to load merchant POS status");
    const data = await res.json();

    const merchantIdEl = document.getElementById("merchant-id-display");
    if (merchantIdEl) merchantIdEl.innerText = data.merchant_id || "merchant_bob@upi";

    document.getElementById("stat-total-count").innerText = data.total_queued || 0;
    document.getElementById("stat-pending-count").innerText = data.pending_sync || 0;
    document.getElementById("stat-pending-amount").innerText = `₹${(data.pending_sync_amount || 0).toFixed(2)}`;
    document.getElementById("stat-settled-count").innerText = data.settled || 0;

    // Connectivity pill
    const pill = document.getElementById("merchant-conn-pill");
    if (pill) {
      if (data.bank_reachable) {
        pill.innerHTML = '<span class="status-pill status-online"><span class="status-dot"></span> Bank Switch Reachable (Online)</span>';
      } else {
        pill.innerHTML = '<span class="status-pill status-offline"><span class="status-dot"></span> Bank Switch Offline (Queuing Locally)</span>';
      }
    }

    // Queue summary text
    const queueSummary = document.getElementById("queue-summary-text");
    if (queueSummary) {
      queueSummary.innerText = `${data.pending_sync || 0} tokens pending (₹${(data.pending_sync_amount || 0).toFixed(2)})`;
    }

    // Sync button state
    const syncBtn = document.getElementById("btn-sync-bank");
    const syncIndicator = document.getElementById("sync-status-indicator");
    if (syncBtn && syncIndicator) {
      if ((data.pending_sync || 0) > 0) {
        syncBtn.disabled = false;
        syncBtn.innerText = `🌐 Sync ${data.pending_sync} Queued Payment(s) with Bank Switch`;
        syncIndicator.className = "badge badge-pending";
        syncIndicator.innerText = `${data.pending_sync} PENDING SYNC`;
      } else {
        syncBtn.disabled = true;
        syncBtn.innerText = "✓ Local Queue Up to Date (0 Pending)";
        syncIndicator.className = "badge badge-settled";
        syncIndicator.innerText = "ALL SETTLED";
      }
    }

    // Render table
    renderMerchantQueue(data.queue || []);
  } catch (err) {
    console.error("Error loading merchant status:", err);
  }
}

async function handleVerifyToken() {
  const resultBox = document.getElementById("verification-result-box");
  if (resultBox) resultBox.style.display = "none";

  const isPasteActive = document.getElementById("tab-paste").style.display !== "none";
  let tokenPackage = null;

  if (isPasteActive) {
    const rawText = document.getElementById("token-paste-input").value.trim();
    if (!rawText) {
      showVerificationResult(false, "Please paste a signed token JSON string from the Payer Device.");
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
      showVerificationResult(false, "Please select an offline token JSON file.");
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
  const resultBox = document.getElementById("verification-result-box");
  try {
    const res = await fetch("/api/merchant/receive", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token_package: tokenPkg })
    });
    const data = await res.json();

    if (data.accepted) {
      const latMs = data.verification_time_ms ? data.verification_time_ms.toFixed(2) : "0.00";
      showVerificationResult(
        true,
        `✓ Offline Token Accepted & Verified in ${latMs} ms (Sub-Second Latency NFR Met)! Stored in local SQLite queue as 'Pending Sync'.`
      );
      // Clear inputs
      document.getElementById("token-paste-input").value = "";
      const fileInput = document.getElementById("token-file-input");
      if (fileInput) fileInput.value = "";
    } else {
      showVerificationResult(false, `❌ Token Rejected: ${data.reason}`);
    }

    loadMerchantStatus();
  } catch (err) {
    showVerificationResult(false, "Communication error: " + err.message);
  }
}

function showVerificationResult(isSuccess, message) {
  const box = document.getElementById("verification-result-box");
  if (!box) return;
  box.className = isSuccess ? "alert alert-success" : "alert alert-danger";
  box.innerHTML = message;
  box.style.display = "block";
}

async function handleSyncWithBank() {
  const syncBtn = document.getElementById("btn-sync-bank");
  const reportBox = document.getElementById("sync-report-box");
  const indicator = document.getElementById("sync-status-indicator");

  if (syncBtn) {
    syncBtn.disabled = true;
    syncBtn.innerText = "⏳ Transmitting Queued Tokens to Bank Switch...";
  }

  if (indicator) {
    indicator.className = "badge badge-created";
    indicator.innerText = "SYNCING";
  }

  try {
    const res = await fetch("/api/merchant/sync", { method: "POST" });
    const data = await res.json();

    if (res.status === 503) {
      if (reportBox) {
        reportBox.className = "alert alert-warning";
        reportBox.innerHTML = `⚠️ <strong>Bank Network Unavailable:</strong> ${data.message || "Central banking switch is currently offline. Queued tokens safely retained in local SQLite database."}`;
        reportBox.style.display = "block";
      }
      if (indicator) {
        indicator.className = "badge badge-pending";
        indicator.innerText = "PENDING SYNC";
      }
    } else if (res.ok) {
      const settledCount = data.settled_count || 0;
      const dupCount = data.duplicate_count || 0;
      const flaggedCount = data.flagged_count || 0;
      const procTime = data.processing_time_ms ? data.processing_time_ms.toFixed(2) : "0.00";

      if (reportBox) {
        reportBox.className = "alert alert-success";
        reportBox.innerHTML = `
          <strong>✓ Synchronization Completed with Bank Switch:</strong><br>
          &bull; Settled Payments: <strong>${settledCount}</strong><br>
          &bull; Duplicates Blocked: <strong>${dupCount}</strong><br>
          &bull; Flagged for 24h Review: <strong>${flaggedCount}</strong><br>
          &bull; Batch Settlement Latency: <strong>${procTime} ms</strong>
        `;
        reportBox.style.display = "block";
      }
      if (indicator) {
        indicator.className = "badge badge-settled";
        indicator.innerText = "SYNC COMPLETE";
      }
    } else {
      if (reportBox) {
        reportBox.className = "alert alert-danger";
        reportBox.innerText = `Sync Error: ${data.error || "Unknown server error."}`;
        reportBox.style.display = "block";
      }
      if (indicator) {
        indicator.className = "badge badge-rejected";
        indicator.innerText = "SYNC FAILED";
      }
    }

    loadMerchantStatus();
  } catch (err) {
    if (reportBox) {
      reportBox.className = "alert alert-warning";
      reportBox.innerText = `Bank connection error: ${err.message}. Queued offline payments safely retained locally.`;
      reportBox.style.display = "block";
    }
  } finally {
    if (syncBtn) {
      syncBtn.disabled = false;
      syncBtn.innerText = "🌐 Sync Queued Payments with Bank Switch";
    }
  }
}

async function handlePurgeSettled() {
  if (!confirm("Purge settled payments from the active local POS queue view? (Does not affect bank records)")) return;
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
  const emptyState = document.getElementById("merchant-empty-state");
  if (!tbody) return;

  if (!queue || queue.length === 0) {
    tbody.innerHTML = "";
    if (emptyState) emptyState.style.display = "block";
    return;
  }

  if (emptyState) emptyState.style.display = "none";

  tbody.innerHTML = queue.map(item => {
    const isSettled = item.status === "Settled";
    const statusBadgeClass = isSettled ? "badge-settled" : "badge-pending";
    const statusLabel = isSettled ? "SETTLED BY BANK" : "PENDING SYNC (VERIFIED)";

    const timeFormatted = item.received_at ? new Date(item.received_at * 1000).toLocaleTimeString() : "N/A";
    const latencyFormatted = item.verification_time_ms ? `${item.verification_time_ms.toFixed(2)} ms` : "N/A";
    const shortId = item.token_id ? item.token_id.slice(0, 8) : "—";

    return `
      <tr>
        <td>
          <span class="token-code" title="${item.token_id}">${shortId}...</span>
        </td>
        <td><strong>${item.payer_id}</strong></td>
        <td><strong style="color: var(--text-primary);">₹${item.amount.toFixed(2)}</strong></td>
        <td>${timeFormatted}</td>
        <td><span style="font-family: monospace; font-size: 0.82rem; color: var(--success); font-weight: 600;">⚡ ${latencyFormatted}</span></td>
        <td><span class="badge ${statusBadgeClass}">${statusLabel}</span></td>
      </tr>
    `;
  }).join("");
}
