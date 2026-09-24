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

function _renderTicketIntoPopup(state) {
  populatePrinterDropdown(state);
  const sel = document.getElementById("printerModelSelect");
  if (sel) sel.value = state.selected_printer;

  const formFactor = (state.printer_models[state.selected_printer] || {}).form_factor || "slip";
  const container = document.getElementById("ticketPaperContainer");
  container.innerHTML = `<div class="ov-ticket-paper ${formFactor}">${escapeHtml(state.printed_ticket_text)}</div>`;
  document.getElementById("printerPopupBackdrop").classList.add("open");
}

/** Auto-trigger: only pops up once per genuinely NEW ticket (a fresh
    delivery/shift/diagnostic just completed). Called every poll; the
    timestamp dedup is what stops it re-opening on every single poll tick
    for the same still-current ticket. */
function maybeShowPrinterPopup(state) {
  if (!state.last_ticket || !state.printed_ticket_text) return;
  const ts = state.last_ticket.timestamp;
  if (ts === _lastShownTicketTimestamp) return; // already shown this exact ticket
  _lastShownTicketTimestamp = ts;
  _renderTicketIntoPopup(state);
}

/** Explicit trigger: for an operator-initiated "reprint" action, where the
    ticket's timestamp is deliberately unchanged (it's the SAME ticket being
    reprinted, not a new one) -- so the auto-trigger's dedup must not apply
    here, or pressing "Print Last Ticket" a second time would silently do
    nothing, which is not what the real register does (it reliably reprints
    on every press per the manual's section 1.12.10.10). */
function forceShowPrinterPopup(state) {
  if (!state.last_ticket || !state.printed_ticket_text) return;
  _renderTicketIntoPopup(state);
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

/**
 * buildPulserWidget(containerEl, productKey)
 * 
 * Builds the Internal Pulser (PN 82597) animation widget and appends it to
 * containerEl. The shaft rotates continuously while flowing, stops while
 * stalled/idle. Rotation SPEED is tied to real pulser flow rate: faster
 * flow = faster rotation, directly visible just like the real encoder shaft
 * spinning faster as liquid flows faster through the meter.
 *
 * +/- buttons nudge the flow rate in 20-unit steps (a practical increment
 * that's meaningful at typical truck-delivery flow rates of 50-300 units/min).
 * Start/Stop toggle the pulser mode between flowing and idle.
 *
 * The relationship between flow rate and animation period:
 *   At 60 units/min with k=100 pulses/unit → 6000 pulses/min → 100 Hz
 *   One visible shaft revolution per second at "nominal" is a natural
 *   reference point (matches the real encoder spinning ~100 times/second
 *   producing quadrature pulses from a 4-slot disc, so 100 full rev/sec
 *   is realistic but much too fast to see, hence we scale down: one visible
 *   revolution takes 0.5s at 300 u/min, 1s at 150, and 3s at 50 u/min,
 *   making the speed difference clearly visible while still looking fast
 *   at typical delivery flow rates rather than glacially slow).
 */
function buildPulserWidget(containerEl, productKey) {
  const wrap = document.createElement("div");
  wrap.className = "ov-pulser-widget";
  wrap.id = "ovPulserWidget_" + productKey;
  wrap.innerHTML = `
    <div class="ov-pulser-header">Internal Pulser (PN 82597)</div>
    <div class="ov-pulser-body">
      <div class="ov-pulser-assembly">
        <div class="ov-pulser-body-rect"></div>
        <div class="ov-pulser-collar"></div>
        <div class="ov-pulser-disc" id="ovPulserDisc_${productKey}"></div>
        <div class="ov-pulser-shaft" id="ovPulserShaft_${productKey}">
          <div class="ov-pulser-index"></div>
        </div>
      </div>
      <div class="ov-pulser-controls">
        <div class="ov-pulser-rate" id="ovPulserRate_${productKey}">
          <span id="ovPulserRateNum_${productKey}">0.0</span><span class="unit"> u/min</span>
        </div>
        <div class="ov-pulser-btns">
          <button class="ov-pulser-btn" data-action="dec" title="Decrease flow rate">−</button>
          <button class="ov-pulser-btn stop" data-action="stop" title="Stop (pulser idle)">&#9646;</button>
          <button class="ov-pulser-btn run" data-action="start" title="Start (pulser flowing)">&#9654;</button>
          <button class="ov-pulser-btn" data-action="inc" title="Increase flow rate">+</button>
        </div>
        <div class="ov-pulser-label">Shaft speed ∝ real flow rate</div>
        <div class="ov-pulser-fault" id="ovPulserFault_${productKey}"></div>
      </div>
    </div>`;

  wrap.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const action = btn.dataset.action;
    const state = window._lastState;
    const currentRate = state ? state.pulser.flow_rate_units_per_min : 60;
    if (action === "dec") {
      const newRate = Math.max(0, currentRate - 20);
      apiPost(`/api/${productKey}/pulser`, { flow_rate: newRate });
    } else if (action === "inc") {
      apiPost(`/api/${productKey}/pulser`, { flow_rate: currentRate + 20 });
    } else if (action === "stop") {
      apiPost(`/api/${productKey}/pulser`, { mode: "idle" });
    } else if (action === "start") {
      apiPost(`/api/${productKey}/pulser`, { mode: "flowing" });
    }
  });
  containerEl.appendChild(wrap);
}

/**
 * updatePulserWidget(state, productKey)
 * 
 * Called every poll from each product's onState(). Updates the rotation
 * animation speed, the displayed flow rate, and the fault indicator.
 */
function updatePulserWidget(state, productKey) {
  const shaft = document.getElementById(`ovPulserShaft_${productKey}`);
  const disc = document.getElementById(`ovPulserDisc_${productKey}`);
  const rateNum = document.getElementById(`ovPulserRateNum_${productKey}`);
  const fault = document.getElementById(`ovPulserFault_${productKey}`);
  if (!shaft || !disc) return;

  const mode = state.pulser.mode;
  const rate = state.pulser.flow_rate_units_per_min;

  // Animation period: 150 / max(rate, 1) seconds per revolution, clamped
  // to [0.15s, 4s] so it's always visibly different from stopped but never
  // so fast it looks like a solid blur at the top of the realistic range.
  const period = mode === "flowing" || mode === "vibration"
    ? Math.min(4.0, Math.max(0.15, 150 / Math.max(rate, 1)))
    : null;

  // Apply rotation: running sets a CSS variable + the .spinning class;
  // idle/stalled remove it so the element freezes in whatever position the
  // animation happened to stop at (matching the real shaft which also stops
  // wherever it is, not snapping to a "zero" position).
  shaft.style.setProperty("--ov-pulser-period", period ? `${period}s` : "1s");
  disc.style.setProperty("--ov-pulser-period", period ? `${period}s` : "1s");
  shaft.classList.toggle("spinning", !!period);
  disc.classList.toggle("spinning", !!period);
  shaft.classList.toggle("stalled", mode === "stalled");

  rateNum.textContent = rate.toFixed(1);

  // The fault display mirrors the J8 diagnostic table from the skill:
  // stalled = non-zero 1-3V on #33/#34, which is the "shaft locked" fault;
  // idle = legitimate no-flow state, not a fault.
  if (mode === "stalled") {
    fault.textContent = "STALLED — J8 FAULT";
  } else if (mode === "vibration") {
    fault.textContent = "VIBRATION / JITTER";
  } else {
    fault.textContent = "";
  }
}
