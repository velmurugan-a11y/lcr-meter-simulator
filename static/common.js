/* common.js — shared polling loop + small helpers used by every meter page.
   Each product page defines window.PRODUCT_KEY and window.onState(state)
   before this file's DOMContentLoaded handler kicks off polling. */

const POLL_MS = 250;
let _pollTimer = null;
let _lastErrorsHash = "";

async function apiGet(path) {
  const res = await fetch(path);
  return res.json();
}

async function apiPost(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!data.ok) {
    flashError(data.error || "Unknown error");
  }
  return data;
}

function flashError(msg) {
  const el = document.getElementById("transientError");
  if (!el) return;
  el.textContent = msg;
  el.style.opacity = "1";
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.opacity = "0"; }, 3500);
}

function fmt(n, decimals = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return Number(n).toFixed(decimals);
}

function startPolling(productKey, onState) {
  async function poll() {
    try {
      const data = await apiGet(`/api/${productKey}/state`);
      if (data.ok) onState(data.state);
    } catch (e) {
      // transient network hiccup — keep polling, don't spam errors
    } finally {
      _pollTimer = setTimeout(poll, POLL_MS);
    }
  }
  poll();
}

function stopPolling() {
  if (_pollTimer) clearTimeout(_pollTimer);
}

function renderErrorBanner(state, productKey, containerEl) {
  if (!containerEl) return;
  if (!state.errors || state.errors.length === 0) {
    containerEl.innerHTML = "";
    return;
  }
  containerEl.innerHTML = `
    <div class="error-banner">
      <span>&#9888; ${state.errors.join(" &middot; ")}</span>
      <button class="clear" onclick="apiPost('/api/${productKey}/clear_errors')">Clear</button>
    </div>`;
}

document.addEventListener("DOMContentLoaded", () => {
  const resetBtn = document.getElementById("resetBtn");
  if (resetBtn && window.PRODUCT_KEY) {
    resetBtn.addEventListener("click", async () => {
      if (!confirm("Reset this meter back to factory defaults? This clears calibration, totalizers, and shift data for this meter only.")) return;
      await apiPost(`/api/${window.PRODUCT_KEY}/reset`);
      location.reload();
    });
  }
});
