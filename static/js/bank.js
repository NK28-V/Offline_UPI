/**
 * Bank Network Dashboard JavaScript
 * Handles central ledger monitoring, sync batch auditing, network simulation toggle,
 * and double-spend replay attack demonstration.
 */

document.addEventListener("DOMContentLoaded", () => {
  loadBankStatus();

  // Button: Refresh Bank
  const btnRefresh = document.getElementById("btn-refresh-bank");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", loadBankStatus);
  }

  // Button: Toggle Network Simulation
  const btnToggleNet = document.getElementById("btn-toggle-network");
  if (btnToggleNet) {
    btnToggleNet.addEventListener("click", handleToggleNetwork);
  }

  // Button: Simulate Duplicate
  const btnSimDup = document.getElementById("btn-simulate-duplicate");
  if (btnSimDup) {
    btnSimDup.addEventListener("click", handleSimulateDuplicate);
  }
});

async function loadBankStatus() {
  try {
    const res = await fetch("/api/bank/status");
    if (!res.ok) throw new Error("Failed to load bank status");
    const data = await res.json();

    // Stats
    document.getElementById("stat-total-value").innerText = `₹${data.total_settled_value.toFixed(2)}`;
    document.getElementById("stat-settled-count").innerText = data.settled_count;
    document.getElementById("stat-duplicates-count").innerText = data.duplicate_count;
    document.getElementById("stat-total-received").innerText = data.total_received;
    document.getElementById("stat-flagged-count").innerText = data.flagged_count;
    document.getElementById("stat-invalid-count").innerText = data.rejected_count;

    // Network Toggle State
    const netBadge = document.getElementById("current-net-badge");
    const netToggleLabel = document.getElementById("net-toggle-label");
    const bankPill = document.getElementById("bank-status-pill");

    if (data.network_online) {
      netBadge.className = "badge badge-settled";
      netBadge.innerText = "ONLINE";
      netToggleLabel.innerText = "Set Offline";
      bankPill.className = "status-pill status-online";
      bankPill.innerHTML = '<span class="status-dot"></span> Core Switch Active (Online)';
    } else {
      netBadge.className = "badge badge-rejected";
      netBadge.innerText = "OFFLINE (SIMULATED)";
      netToggleLabel.innerText = "Set Online";
      bankPill.className = "status-pill status-offline";
      bankPill.innerHTML = '<span class="status-dot"></span> Core Switch Simulated Offline';
    }

    // Trusted Payers
    renderTrustedPayers(data.trusted_payers || []);

    // Batches
    renderSyncBatches(data.recent_batches || []);

    // Ledger
    renderMasterLedger(data.ledger || []);
  } catch (err) {
    console.error("Error loading bank status:", err);
  }
}

async function handleToggleNetwork() {
  try {
    const res = await fetch("/api/bank/toggle_network", { method: "POST" });
    const data = await res.json();
    loadBankStatus();
  } catch (err) {
    alert("Error toggling network: " + err.message);
  }
}

async function handleSimulateDuplicate() {
  const resultBox = document.getElementById("duplicate-sim-result");
  resultBox.style.display = "block";
  resultBox.className = "alert alert-info";
  resultBox.innerText = "Re-submitting an existing token ID to test double-spend defense...";

  try {
    const res = await fetch("/api/bank/simulate_duplicate", { method: "POST" });
    const data = await res.json();

    if (!res.ok) {
      resultBox.className = "alert alert-warning";
      resultBox.innerText = data.error || "Cannot run simulation.";
      return;
    }

    resultBox.className = "alert alert-danger";
    resultBox.innerHTML = `
      <strong>🚨 Double-Spend Attack Prevented (100% Detection):</strong><br>
      &bull; Duplicate Token ID: <code>${data.token_id}</code><br>
      &bull; Backend Verdict: <strong>${data.result.reason}</strong><br>
      &bull; Settlement Prevented: Token was NOT settled twice.
    `;
    loadBankStatus();
  } catch (err) {
    resultBox.className = "alert alert-danger";
    resultBox.innerText = "Simulation error: " + err.message;
  }
}

function renderTrustedPayers(payers) {
  const tbody = document.getElementById("trusted-payers-tbody");
  const countBadge = document.getElementById("trusted-payers-count");
  countBadge.innerText = `${payers.length} Registered`;

  if (!payers || payers.length === 0) {
    tbody.innerHTML = `<tr><td colspan="3" style="text-align:center;color:var(--text-muted);padding:14px;">No trusted payers registered.</td></tr>`;
    return;
  }

  tbody.innerHTML = payers.map(p => `
    <tr>
      <td><span class="token-code">${p.payer_id}</span></td>
      <td><code title="${p.public_key_pem}">${p.public_key_pem.slice(27, 48)}...</code></td>
      <td><span class="badge badge-settled">ACTIVE</span></td>
    </tr>
  `).join("");
}

function renderSyncBatches(batches) {
  const tbody = document.getElementById("sync-batches-tbody");
  if (!batches || batches.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-muted); padding: 14px;">No sync batches recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = batches.map(b => `
    <tr>
      <td><span class="token-code">${b.batch_id.slice(0, 8)}...</span></td>
      <td>${b.merchant_id}</td>
      <td><strong>${b.total_tokens}</strong></td>
      <td><span style="color:var(--success);font-weight:600;">${b.settled_count}</span></td>
      <td><span style="color:var(--danger);font-weight:600;">${b.duplicate_count}</span></td>
      <td><span style="color:var(--purple);font-weight:600;">${b.flagged_count}</span></td>
    </tr>
  `).join("");
}

function renderMasterLedger(ledger) {
  const tbody = document.getElementById("master-ledger-tbody");
  if (!ledger || ledger.length === 0) {
    tbody.innerHTML = `<tr><td colspan="8" style="text-align: center; color: var(--text-muted); padding: 24px;">No settled transactions in ledger.</td></tr>`;
    return;
  }

  tbody.innerHTML = ledger.map(item => {
    const statusBadgeClass =
      item.status === "SETTLED" ? "badge-settled" :
      item.status === "FLAGGED_FOR_REVIEW" ? "badge-flagged" : "badge-rejected";

    const settledTime = new Date(item.settled_at * 1000).toLocaleTimeString();
    const ageText = item.token_age_hours ? `${item.token_age_hours.toFixed(1)}h` : "< 1h";
    const auditNotes = item.review_reason || "&mdash;";

    return `
      <tr>
        <td><span class="token-code">${item.token_id.slice(0, 8)}...</span></td>
        <td>${item.payer_id}</td>
        <td>${item.payee_id}</td>
        <td><strong>₹${item.amount.toFixed(2)}</strong></td>
        <td>${ageText}</td>
        <td>${settledTime}</td>
        <td><span class="badge ${statusBadgeClass}">${item.status}</span></td>
        <td style="font-size:0.78rem;color:var(--text-secondary);">${auditNotes}</td>
      </tr>
    `;
  }).join("");
}
