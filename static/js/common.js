/**
 * Offline UPI Payment Simulator — Common UI Controller
 * Provides unified cross-service navigation, demo reset modal,
 * guided demo stepper, and shared utility helpers across all dashboards.
 */

(function () {
  "use strict";

  // Service Port Configuration
  const PORTS = {
    payer: 5001,
    merchant: 5002,
    bank: 5003
  };

  /**
   * Resolves the target URL for a service based on current host & port.
   */
  function resolveServiceUrl(targetService) {
    const host = window.location.hostname || "localhost";
    const currentPort = parseInt(window.location.port || "80", 10);

    // If running under unified app (port 5000 or similar non-5001/5002/5003)
    if (currentPort === 5000 || !Object.values(PORTS).includes(currentPort)) {
      return `/${targetService}`;
    }

    const port = PORTS[targetService] || 5000;
    return `http://${host}:${port}/${targetService}`;
  }

  /**
   * Initializes the common top navigation bar links.
   */
  function initNavigation(activeRole) {
    const navLinks = document.querySelectorAll(".nav-link[data-role], .nav-item[data-role]");
    navLinks.forEach(link => {
      const role = link.getAttribute("data-role");
      link.href = resolveServiceUrl(role);
      if (role === activeRole) {
        link.classList.add("active");
      } else {
        link.classList.remove("active");
      }
    });

    // Also update any explicit cross-links on the page (e.g. empty state CTA links)
    document.querySelectorAll("[data-cross-link]").forEach(el => {
      const targetRole = el.getAttribute("data-cross-link");
      el.href = resolveServiceUrl(targetRole);
    });
  }

  /**
   * Initializes the interactive "How to Run a Demo" collapsible guide.
   */
  function initDemoGuide() {
    const toggleBtn = document.getElementById("btn-toggle-demo-guide");
    const guidePanel = document.getElementById("demo-guide-panel");
    if (!toggleBtn || !guidePanel) return;

    toggleBtn.addEventListener("click", () => {
      const isVisible = guidePanel.style.display !== "none";
      guidePanel.style.display = isVisible ? "none" : "block";
      toggleBtn.classList.toggle("active", !isVisible);
      toggleBtn.setAttribute("aria-expanded", !isVisible);
    });

    // Handle close button inside the guide if present
    const closeBtn = document.getElementById("btn-close-demo-guide");
    if (closeBtn) {
      closeBtn.addEventListener("click", () => {
        guidePanel.style.display = "none";
        toggleBtn.classList.remove("active");
      });
    }
  }

  /**
   * Initializes the Reset Demo Data modal with strict confirmation and cross-service coordination.
   */
  function initResetModal(onResetCompleted) {
    const openBtn = document.getElementById("btn-open-reset-modal");
    const modal = document.getElementById("reset-demo-modal");
    const cancelBtn = document.getElementById("btn-cancel-reset");
    const confirmBtn = document.getElementById("btn-confirm-reset");
    const backdrop = document.getElementById("reset-modal-backdrop");
    const statusDiv = document.getElementById("reset-modal-status");

    if (!openBtn || !modal) return;

    function openModal() {
      modal.style.display = "flex";
      if (statusDiv) {
        statusDiv.style.display = "none";
        statusDiv.innerHTML = "";
      }
      if (confirmBtn) {
        confirmBtn.disabled = false;
        confirmBtn.innerHTML = "🗑️ Yes, Reset Demo Data";
      }
    }

    function closeModal() {
      modal.style.display = "none";
    }

    openBtn.addEventListener("click", openModal);
    if (cancelBtn) cancelBtn.addEventListener("click", closeModal);
    if (backdrop) backdrop.addEventListener("click", closeModal);

    // Escape key closes modal
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && modal.style.display === "flex") {
        closeModal();
      }
    });

    if (confirmBtn) {
      confirmBtn.addEventListener("click", async () => {
        confirmBtn.disabled = true;
        confirmBtn.innerHTML = "⏳ Resetting data across services...";
        if (statusDiv) {
          statusDiv.style.display = "block";
          statusDiv.className = "alert alert-info";
          statusDiv.innerText = "Clearing transaction histories, queues, and receipts...";
        }

        try {
          // 1. Reset current service
          const res = await fetch("/api/reset_demo", { method: "POST" });
          const data = await res.json();

          // 2. Also attempt to reset other service ports if running multi-device setup
          const host = window.location.hostname || "localhost";
          const siblingPorts = [5001, 5002, 5003].filter(p => p !== parseInt(window.location.port, 10));
          await Promise.allSettled(
            siblingPorts.map(p =>
              fetch(`http://${host}:${p}/api/reset_demo?local_only=true`, { method: "POST", mode: "cors" }).catch(() => {})
            )
          );

          if (statusDiv) {
            statusDiv.className = "alert alert-success";
            statusDiv.innerHTML = `
              <strong>✓ Demo Data Successfully Reset:</strong><br>
              &bull; Transaction queues and settlement records cleared.<br>
              &bull; Payer spend reset to ₹0.00 / ₹500.00 allowance.<br>
              &bull; <em>Cryptographic keys, trusted registries, and schemas preserved.</em>
            `;
          }

          setTimeout(() => {
            closeModal();
            if (typeof onResetCompleted === "function") {
              onResetCompleted();
            } else {
              window.location.reload();
            }
          }, 1200);
        } catch (err) {
          if (statusDiv) {
            statusDiv.className = "alert alert-danger";
            statusDiv.innerText = "Error executing reset: " + err.message;
          }
          confirmBtn.disabled = false;
          confirmBtn.innerHTML = "🗑️ Try Again";
        }
      });
    }
  }

  /**
   * Initializes the optional "Load Sample Data" action (in developer/technical details).
   */
  function initSampleDataLoader(onSampleLoaded) {
    const loadBtn = document.getElementById("btn-load-sample-data");
    const resultBox = document.getElementById("sample-data-result");
    if (!loadBtn) return;

    loadBtn.addEventListener("click", async () => {
      loadBtn.disabled = true;
      loadBtn.innerHTML = "⏳ Loading Sample Data...";
      if (resultBox) {
        resultBox.style.display = "block";
        resultBox.className = "alert alert-info";
        resultBox.innerText = "Generating cryptographically signed sample payments...";
      }

      try {
        const res = await fetch("/api/load_sample_data", { method: "POST" });
        const data = await res.json();

        if (res.ok) {
          if (resultBox) {
            resultBox.className = "alert alert-success";
            resultBox.innerHTML = `<strong>✓ Sample Data Loaded:</strong> ${data.message}`;
          }
          setTimeout(() => {
            if (typeof onSampleLoaded === "function") {
              onSampleLoaded();
            } else {
              window.location.reload();
            }
          }, 1000);
        } else {
          if (resultBox) {
            resultBox.className = "alert alert-danger";
            resultBox.innerText = `Error: ${data.error || "Failed to load sample data."}`;
          }
        }
      } catch (err) {
        if (resultBox) {
          resultBox.className = "alert alert-danger";
          resultBox.innerText = "Communication error: " + err.message;
        }
      } finally {
        loadBtn.disabled = false;
        loadBtn.innerHTML = "📥 Load Sample Demo Data";
      }
    });
  }

  /**
   * Copy text helper with visual tooltip / notification
   */
  function copyToClipboard(text, successElementId) {
    if (!text) return;
    navigator.clipboard.writeText(text).then(() => {
      const el = document.getElementById(successElementId);
      if (el) {
        el.style.display = "inline-block";
        setTimeout(() => { el.style.display = "none"; }, 2500);
      }
    }).catch(err => {
      console.warn("Clipboard copy failed:", err);
    });
  }

  // Export functions to global namespace
  window.OfflineUPI = {
    initNavigation,
    initDemoGuide,
    initResetModal,
    initSampleDataLoader,
    resolveServiceUrl,
    copyToClipboard
  };
})();
