/**
 * Offline UPI Payment Simulator — Bank Switch Dashboard Controller
 * Powers the 5 Bank sections:
 *   Section A: Bank Overview Metrics
 *   Section B: Merchant Synchronisation Gateway
 *   Section C: Master Central Settlement Ledger (with filtering & empty states)
 *   Section D: Settlement & Reconciliation Lifecycle
 *   Section E: Technical Details, Security Registries & Simulation Lab
 */

let allLedgerItems = [];
let currentFilter = "ALL";
let bankPublicKeyPem = "";

document.addEventListener("DOMContentLoaded", () => {
  // Initialize shared common components
  if (window.OfflineUPI) {
    window.OfflineUPI.initNavigation("bank");
    window.OfflineUPI.initDemoGuide();
    window.OfflineUPI.initResetModal(loadBankStatus);
    window.OfflineUPI.initSampleDataLoader(loadBankStatus);
  }

  // Load initial bank data
  loadBankStatus();
  loadBankPublicKey();

  // Button: Refresh Ledger
  const btnRefresh = document.getElementById("btn-refresh-ledger");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", loadBankStatus);
  }

  // Button: Check Merchant Status
  const btnCheckMerchant = document.getElementById("btn-check-merchant-status");
  if (btnCheckMerchant) {
    btnCheckMerchant.addEventListener("click", loadBankStatus);
  }

  // Button: Sync Pending Transactions from Merchant
  const btnSyncPending = document.getElementById("btn-sync-pending-transactions");
  if (btnSyncPending) {
    btnSyncPending.addEventListener("click", handleTriggerMerchantSync);
  }

  // Button: Toggle Network Outage Simulation
  const btnToggleNet = document.getElementById("btn-toggle-network");
  if (btnToggleNet) {
    btnToggleNet.addEventListener("click", handleToggleNetwork);
  }

  // Button: Simulate Double-Spend Replay
  const btnSimDup = document.getElementById("btn-simulate-duplicate");
  if (btnSimDup) {
    btnSimDup.addEventListener("click", handleSimulateDuplicate);
  }

  // Copy Bank Public Key
  const btnCopyKey = document.getElementById("btn-copy-bank-key");
  if (btnCopyKey) {
    btnCopyKey.addEventListener("click", () => {
      if (bankPublicKeyPem && window.OfflineUPI) {
        window.OfflineUPI.copyToClipboard(bankPublicKeyPem, null);
        btnCopyKey.innerText = "✓ Copied Key!";
        setTimeout(() => { btnCopyKey.innerText = "📋 Copy Public Key PEM"; }, 2000);
      }
    });
  }

  // Collapsible Section E (Tech Lab)
  const techToggle = document.getElementById("tech-lab-toggle");
  const techBody = document.getElementById("tech-lab-body");
  if (techToggle && techBody) {
    techToggle.addEventListener("click", () => {
      const isVisible = techBody.style.display !== "none";
      techBody.style.display = isVisible ? "none" : "block";
      techToggle.classList.toggle("open", !isVisible);
    });
  }

  // Ledger Filter Pills
  document.querySelectorAll(".filter-pill[data-filter]").forEach(pill => {
    pill.addEventListener("click", () => {
      document.querySelectorAll(".filter-pill").forEach(p => p.classList.remove("active"));
      pill.classList.add("active");
      currentFilter = pill.getAttribute("data-filter");
      renderMasterLedger();
    });
  });
});

/**
 * Loads comprehensive Bank status and metrics from server.
 */
async function loadBankStatus() {
  try {
    const res = await fetch("/api/bank/status");
    if (!res.ok) throw new Error("Failed to load bank switch status.");
    const data = await res.json();

    // -----------------------------------------------------------------------
    // Section A: Overview Metrics
    // -----------------------------------------------------------------------
    document.getElementById("stat-total-received").innerText = data.total_received || 0;
    document.getElementById("stat-pending-merchant-syncs").innerText = data.pending_merchant_syncs || 0;
    document.getElementById("stat-pending-merchant-amount").innerText = `₹${(data.pending_merchant_amount || 0).toFixed(2)} awaiting bank sync`;
    document.getElementById("stat-settled-count").innerText = data.settled_count || 0;
    document.getElementById("stat-total-value").innerText = `Total Value: ₹${(data.total_settled_value || 0).toFixed(2)}`;
    document.getElementById("stat-duplicates-count").innerText = (data.duplicate_count || 0) + (data.rejected_count || 0);
    document.getElementById("stat-rejection-subtext").innerText = `${data.duplicate_count || 0} duplicate / ${data.rejected_count || 0} invalid`;

    // Network Status Pill & Badges
    const netBadge = document.getElementById("current-net-badge");
    const netToggleLabel = document.getElementById("net-toggle-label");
    const bankPill = document.getElementById("bank-status-pill");

    if (data.network_online) {
      if (netBadge) {
        netBadge.className = "badge badge-settled";
        netBadge.innerText = "ONLINE";
      }
      if (netToggleLabel) netToggleLabel.innerText = "Simulate Outage";
      if (bankPill) {
        bankPill.className = "status-pill status-online";
        bankPill.innerHTML = '<span class="status-dot"></span> Core Switch Active (Online)';
      }
    } else {
      if (netBadge) {
        netBadge.className = "badge badge-rejected";
        netBadge.innerText = "OFFLINE (SIMULATED)";
      }
      if (netToggleLabel) netToggleLabel.innerText = "Restore Connectivity";
      if (bankPill) {
        bankPill.className = "status-pill status-offline";
        bankPill.innerHTML = '<span class="status-dot"></span> Core Switch Simulated Offline';
      }
    }

    // -----------------------------------------------------------------------
    // Section B: Merchant Synchronisation Status
    // -----------------------------------------------------------------------
    const syncSummary = document.getElementById("merchant-sync-summary-text");
    const syncBadge = document.getElementById("merchant-sync-status-badge");
    const syncEmptyState = document.getElementById("merchant-sync-empty-state");
    const syncBtn = document.getElementById("btn-sync-pending-transactions");

    const pendingCount = data.pending_merchant_syncs || 0;
    const pendingAmount = data.pending_merchant_amount || 0.0;

    const syncBanner = document.getElementById("merchant-sync-banner");
    if (syncBanner) {
      syncBanner.style.display = pendingCount > 0 ? "block" : "none";
    }
    if (pendingCount > 0) {
      if (syncSummary) {
        syncSummary.innerHTML = `<strong>${pendingCount} offline payment(s) waiting for bank sync</strong> (Total value: <strong>₹${pendingAmount.toFixed(2)}</strong>).`;
      }
      if (syncBadge) {
        syncBadge.className = "badge badge-pending";
        syncBadge.innerText = `${pendingCount} PENDING SYNC`;
      }
      if (syncEmptyState) syncEmptyState.style.display = "none";
      if (syncBtn) {
        syncBtn.disabled = !data.network_online;
        syncBtn.innerHTML = `⚡ Sync ${pendingCount} Pending Transaction(s) Now`;
      }
    } else {
      if (syncSummary) {
        syncSummary.innerText = "All offline payments from the Merchant POS are currently synchronized and settled.";
      }
      if (syncBadge) {
        syncBadge.className = "badge badge-settled";
        syncBadge.innerText = "UP TO DATE";
      }
      if (syncEmptyState) syncEmptyState.style.display = "block";
      if (syncBtn) {
        syncBtn.disabled = true;
        syncBtn.innerText = "✓ No Pending Transactions Waiting";
      }
    }

    // -----------------------------------------------------------------------
    // Section C: Master Ledger & Filter Counts
    // -----------------------------------------------------------------------
    allLedgerItems = data.ledger || [];
    updateFilterCounts();
    renderMasterLedger();

    // -----------------------------------------------------------------------
    // Section E: Trusted Payers & Batches
    // -----------------------------------------------------------------------
    renderTrustedPayers(data.trusted_payers || []);
    renderSyncBatches(data.recent_batches || []);

  } catch (err) {
    console.error("Error loading bank status:", err);
  }
}

/**
 * Loads the Bank's public key PEM for display and inspection.
 */
async function loadBankPublicKey() {
  try {
    const res = await fetch("/api/bank/public_key");
    if (res.ok) {
      const data = await res.json();
      bankPublicKeyPem = data.public_key_pem || "";
      const display = document.getElementById("bank-pubkey-display");
      if (display) display.innerText = bankPublicKeyPem;
    }
  } catch (err) {
    console.warn("Could not load bank public key:", err);
  }
}

/**
 * Triggers delayed synchronization of pending merchant payments.
 */
async function handleTriggerMerchantSync() {
  const syncBtn = document.getElementById("btn-sync-pending-transactions");
  const resultBox = document.getElementById("merchant-sync-result-box");

  if (syncBtn) {
    syncBtn.disabled = true;
    syncBtn.innerHTML = "⏳ Transmitting & Settling Batch...";
  }

  if (resultBox) {
    resultBox.style.display = "block";
    resultBox.className = "alert alert-info";
    resultBox.innerText = "Contacting Merchant POS and settling queued offline payments...";
  }

  try {
    const res = await fetch("/api/bank/trigger_merchant_sync", { method: "POST" });
    const data = await res.json();

    if (res.status === 503) {
      if (resultBox) {
        resultBox.className = "alert alert-warning";
        resultBox.innerHTML = `⚠️ <strong>Bank Network Simulated Offline:</strong> ${data.message || "Cannot synchronize while bank network is offline. Restore network connectivity to settle."}`;
      }
    } else if (res.ok) {
      const settled = data.settled_count || 0;
      const dups = data.duplicate_count || 0;
      const flagged = data.flagged_count || 0;
      const rejected = data.rejected_count || 0;
      const timeMs = data.processing_time_ms ? data.processing_time_ms.toFixed(2) : "0.00";

      if (resultBox) {
        resultBox.className = "alert alert-success";
        resultBox.innerHTML = `
          <strong>✓ Merchant Synchronization Completed:</strong><br>
          &bull; Successfully Settled: <strong>${settled}</strong> payment(s)<br>
          &bull; Double-Spends Blocked: <strong>${dups}</strong><br>
          &bull; Flagged for 24h Review: <strong>${flagged}</strong><br>
          &bull; Invalid / Rejected: <strong>${rejected}</strong><br>
          &bull; Batch Settlement Latency: <strong>${timeMs} ms</strong>
        `;
      }
      loadBankStatus();
    } else {
      if (resultBox) {
        resultBox.className = "alert alert-danger";
        resultBox.innerHTML = `<strong>❌ Sync Error:</strong> ${data.error || "Failed to settle batch."} ${data.message ? `<br><small>${data.message}</small>` : ""}`;
      }
    }
  } catch (err) {
    if (resultBox) {
      resultBox.className = "alert alert-warning";
      resultBox.innerHTML = `⚠️ <strong>Merchant Service Unreachable:</strong> Could not connect to Merchant POS at port 5002 (${err.message}). Ensure Merchant Service is running.`;
    }
  } finally {
    if (syncBtn) {
      syncBtn.disabled = false;
      syncBtn.innerHTML = "⚡ Sync Pending Transactions Now";
    }
  }
}

/**
 * Toggles Bank simulated network outage.
 */
async function handleToggleNetwork() {
  try {
    const res = await fetch("/api/bank/toggle_network", { method: "POST" });
    const data = await res.json();
    loadBankStatus();
  } catch (err) {
    alert("Error toggling bank network: " + err.message);
  }
}

/**
 * Simulates a double-spend replay attack on the central switch.
 */
async function handleSimulateDuplicate() {
  const resultBox = document.getElementById("duplicate-sim-result");
  if (!resultBox) return;

  resultBox.style.display = "block";
  resultBox.className = "alert alert-info";
  resultBox.innerText = "Re-submitting an already settled payment token to test atomic double-spend defense...";

  try {
    const res = await fetch("/api/bank/simulate_duplicate", { method: "POST" });
    const data = await res.json();

    if (!res.ok) {
      resultBox.className = "alert alert-warning";
      resultBox.innerText = data.error || "Cannot run simulation. Settle at least one payment first.";
      return;
    }

    resultBox.className = "alert alert-danger";
    resultBox.innerHTML = `
      <strong>🚨 Double-Spend Attack Prevented (100% Detection Rate):</strong><br>
      &bull; Duplicate Token ID: <code>${data.token_id}</code><br>
      &bull; Defense Mechanism: <strong>SQLite Atomic SAVEPOINT & IntegrityError Catch</strong><br>
      &bull; Switch Outcome: <strong>${data.result.reason}</strong><br>
      &bull; Security Guarantee: Token was NOT settled twice. Balance integrity maintained.
    `;
    loadBankStatus();
  } catch (err) {
    resultBox.className = "alert alert-danger";
    resultBox.innerText = "Simulation error: " + err.message;
  }
}

/**
 * Updates filter badge counts for the Master Ledger.
 */
function updateFilterCounts() {
  const total = allLedgerItems.length;
  const settled = allLedgerItems.filter(i => i.status === "SETTLED").length;
  const flagged = allLedgerItems.filter(i => i.status === "FLAGGED_FOR_REVIEW").length;
  const rejected = allLedgerItems.filter(i => i.status === "REJECTED").length;

  const elAll = document.getElementById("count-filter-all");
  const elSettled = document.getElementById("count-filter-settled");
  const elFlagged = document.getElementById("count-filter-flagged");
  const elRejected = document.getElementById("count-filter-rejected");

  if (elAll) elAll.innerText = total;
  if (elSettled) elSettled.innerText = settled;
  if (elFlagged) elFlagged.innerText = flagged;
  if (elRejected) elRejected.innerText = rejected;
}

/**
 * Renders the Master Central Settlement Ledger table.
 */
function renderMasterLedger() {
  const tbody = document.getElementById("master-ledger-tbody");
  const emptyState = document.getElementById("ledger-empty-state");
  if (!tbody) return;

  let filtered = allLedgerItems;
  if (currentFilter === "SETTLED") {
    filtered = allLedgerItems.filter(i => i.status === "SETTLED");
  } else if (currentFilter === "FLAGGED") {
    filtered = allLedgerItems.filter(i => i.status === "FLAGGED_FOR_REVIEW");
  } else if (currentFilter === "REJECTED") {
    filtered = allLedgerItems.filter(i => i.status === "REJECTED");
  }

  if (filtered.length === 0) {
    tbody.innerHTML = "";
    if (emptyState) emptyState.style.display = "block";
    return;
  }

  if (emptyState) emptyState.style.display = "none";

  tbody.innerHTML = filtered.map(item => {
    const statusBadgeClass =
      item.status === "SETTLED" ? "badge-settled" :
      item.status === "FLAGGED_FOR_REVIEW" ? "badge-flagged" : "badge-rejected";

    const shortId = item.token_id ? item.token_id.slice(0, 8) + "..." : "—";
    const amt = Number(item.amount || 0).toFixed(2);
    const merchant = item.payee_id || "merchant_bob@upi";

    return `
      <tr>
        <td><code>${shortId}</code></td>
        <td><strong>${merchant}</strong></td>
        <td><strong>₹${amt}</strong></td>
        <td><span class="badge ${statusBadgeClass}">${item.status}</span></td>
      </tr>
    `;
  }).join("");
}

/**
 * Copies token ID to clipboard.
 */
function copyTokenId(tokenId) {
  if (window.OfflineUPI) {
    window.OfflineUPI.copyToClipboard(tokenId, null);
  }
}

/**
 * Renders the pre-provisioned trusted payers table.
 */
function renderTrustedPayers(payers) {
  const tbody = document.getElementById("trusted-payers-tbody");
  const countBadge = document.getElementById("trusted-payers-count");
  if (!tbody) return;

  if (countBadge) countBadge.innerText = `${payers.length} Registered`;

  if (!payers || payers.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:14px;">No trusted payers registered.</td></tr>`;
    return;
  }

  tbody.innerHTML = payers.map(p => `
    <tr>
      <td><span class="token-code">${p.payer_id}</span></td>
      <td><code title="${p.public_key_pem}">${p.public_key_pem.slice(27, 52)}...</code></td>
      <td><span class="badge badge-settled">ACTIVE</span></td>
    </tr>
  `).join("");
}

/**
 * Renders recent sync batches audit log table.
 */
function renderSyncBatches(batches) {
  const tbody = document.getElementById("sync-batches-tbody");
  if (!tbody) return;

  if (!batches || batches.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 14px;">No sync batches recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = batches.map(b => `
    <tr>
      <td><span class="token-code">${b.batch_id.slice(0, 8)}...</span></td>
      <td><code>${b.merchant_id}</code></td>
      <td><strong>${b.total_tokens}</strong></td>
      <td><span style="color:var(--success);font-weight:600;">${b.settled_count}</span></td>
      <td><span style="color:var(--danger);font-weight:600;">${b.duplicate_count}</span></td>
      <td><span style="color:var(--purple);font-weight:600;">${b.flagged_count}</span></td>
    </tr>
  `).join("");
}
