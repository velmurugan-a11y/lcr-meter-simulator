/* original-view.js — shared behavior for Original View across all three
   products: a continuously-draggable rotary dial (mouse + touch), the W&M
   seal/lock toggle, the printer popup, and view-mode switching. Each
   product page wires these helpers to its own DOM ids and API routes;
   this file has no product-specific knowledge baked in. */

/**
 * Makes `el` a continuously-rotatable dial. `positions` is an ordered list
 * of {key, angleDeg} the dial should snap to on release (angleDeg measured
 * clockwise from 12 o'clock, matching how a real rotary switch's detents
 * are laid out) -- the user can drag anywhere in between, but releasing
 * always settles on the nearest defined position, just like a real
 * detented rotary switch (it doesn't stop at arbitrary angles).
 * `onSettle(key)` fires once per settle, only when the key actually changes.
 */
function makeRotary(el, knobEl, positions, onSettle) {
  let dragging = false;
  let currentAngle = positions[0].angleDeg;
  let lastSettledKey = null;

  function angleFromEvent(evt) {
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const point = evt.touches ? evt.touches[0] : evt;
    const dx = point.clientX - cx;
    const dy = point.clientY - cy;
    // atan2 gives angle from 3 o'clock counter-clockwise; convert to
    // "clockwise from 12 o'clock" to match how positions[].angleDeg is authored.
    let deg = Math.atan2(dx, -dy) * (180 / Math.PI);
    if (deg < 0) deg += 360;
    return deg;
  }

  function nearestPosition(angle) {
    let best = positions[0];
    let bestDist = 360;
    positions.forEach(p => {
      let d = Math.abs(p.angleDeg - angle);
      if (d > 180) d = 360 - d;
      if (d < bestDist) { bestDist = d; best = p; }
    });
    return best;
  }

  function render(angle) {
    knobEl.style.transform = `rotate(${angle}deg)`;
  }

  function start(evt) {
    dragging = true;
    el.style.cursor = "grabbing";
    evt.preventDefault();
  }
  function move(evt) {
    if (!dragging) return;
    currentAngle = angleFromEvent(evt);
    render(currentAngle);
  }
  function end() {
    if (!dragging) return;
    dragging = false;
    el.style.cursor = "grab";
    const settled = nearestPosition(currentAngle);
    currentAngle = settled.angleDeg;
    render(currentAngle);
    if (settled.key !== lastSettledKey) {
      lastSettledKey = settled.key;
      onSettle(settled.key);
    }
  }

  el.addEventListener("mousedown", start);
  window.addEventListener("mousemove", move);
  window.addEventListener("mouseup", end);
  el.addEventListener("touchstart", start, { passive: false });
  window.addEventListener("touchmove", move, { passive: false });
  window.addEventListener("touchend", end);

  // Also allow clicking directly on a position's label (if the page draws
  // them) to jump straight there, same as dragging would.
  return {
    /** Programmatically set the dial to a known key (e.g. when state.json
        says the selector is on STOP because some OTHER control changed it,
        like the Custom View buttons) without re-firing onSettle. */
    setToKey(key) {
      const pos = positions.find(p => p.key === key);
      if (!pos) return;
      lastSettledKey = key;
      currentAngle = pos.angleDeg;
      render(currentAngle);
    },
  };
}

/**
 * Wires up the W&M seal/lock icon. clickable toggles locked state via the
 * given API path; state comes from the polled register state on each render.
 */
function wireSeal(sealEl, productKey) {
  sealEl.addEventListener("click", async () => {
    const nowLocked = sealEl.classList.contains("locked");
    await apiPost(`/api/${productKey}/set_locked`, { locked: !nowLocked });
  });
}
function renderSeal(sealEl, locked) {
  sealEl.classList.toggle("locked", !!locked);
  const label = sealEl.querySelector(".ov-seal-label");
  if (label) label.textContent = locked ? "SEALED" : "UNSEALED";
}

/**
 * Printer popup: shared backdrop + paper render + printer-model dropdown,
 * reused by all three product pages. Call ensurePrinterPopup() once per
 * page to inject the DOM, then showTicket(state) whenever a fresh ticket
 * should pop up (the page decides when -- typically whenever
 * state.last_ticket's timestamp changes).
 */
let _printerPopupBuilt = false;
let _lastShownTicketTimestamp = null;

function ensurePrinterPopup(productKey) {
  if (_printerPopupBuilt) return;
  const backdrop = document.createElement("div");
  backdrop.className = "ov-printer-popup-backdrop";
  backdrop.id = "printerPopupBackdrop";
  backdrop.innerHTML = `
    <div class="ov-printer-popup">
      <div class="ov-printer-popup-head">
        <span>Printer Output</span>
        <button id="printerPopupClose">&times;</button>
      </div>
      <div class="ov-printer-select-row">
        <label style="font-size:11px; color:#555;">Printer</label>
        <select id="printerModelSelect"></select>
      </div>
      <div id="ticketPaperContainer"></div>
    </div>`;
  document.body.appendChild(backdrop);
  document.getElementById("printerPopupClose").addEventListener("click", () => {
    backdrop.classList.remove("open");
  });
  backdrop.addEventListener("click", (e) => { if (e.target === backdrop) backdrop.classList.remove("open"); });
  document.getElementById("printerModelSelect").addEventListener("change", (e) => {
    apiPost(`/api/${productKey}/set_printer`, { printer_key: e.target.value });
  });
  _printerPopupBuilt = true;
}

function populatePrinterDropdown(state) {
  const sel = document.getElementById("printerModelSelect");
  if (!sel || sel.options.length > 0) return;
  Object.entries(state.printer_models).forEach(([key, model]) => {
    const opt = document.createElement("option");
    opt.value = key;
    opt.textContent = model.label;
    if (key === state.selected_printer) opt.selected = true;
    sel.appendChild(opt);
  });
}

function maybeShowPrinterPopup(state) {
  if (!state.last_ticket || !state.printed_ticket_text) return;
  const ts = state.last_ticket.timestamp;
  if (ts === _lastShownTicketTimestamp) return; // already shown this exact ticket
  _lastShownTicketTimestamp = ts;

  populatePrinterDropdown(state);
  const sel = document.getElementById("printerModelSelect");
  if (sel) sel.value = state.selected_printer;

  const formFactor = (state.printer_models[state.selected_printer] || {}).form_factor || "slip";
  const container = document.getElementById("ticketPaperContainer");
  container.innerHTML = `<div class="ov-ticket-paper ${formFactor}">${escapeHtml(state.printed_ticket_text)}</div>`;
  document.getElementById("printerPopupBackdrop").classList.add("open");
}

function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** View-mode toggle (Original / Custom), shared markup + wiring. */
function wireViewToggle(productKey, onModeChange) {
  const origBtn = document.getElementById("viewToggleOriginal");
  const custBtn = document.getElementById("viewToggleCustom");
  async function setMode(mode) {
    await apiPost(`/api/${productKey}/set_view_mode`, { mode });
    origBtn.classList.toggle("active", mode === "original");
    custBtn.classList.toggle("active", mode === "custom");
    onModeChange(mode);
  }
  origBtn.addEventListener("click", () => setMode("original"));
  custBtn.addEventListener("click", () => setMode("custom"));
  return setMode;
}
