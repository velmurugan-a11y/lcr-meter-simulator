"""
register_base.py — Shared state and logic across all simulated LectroCount
register models: product/calibration storage, k-Factor math, multi-point
calibration validation, VCF application, totalizers, the no-flow timer, the
common error-message dictionary, and ticket generation. Product-specific
subclasses (LCR-II, LCR-600, LCR.iQ) layer their own menu/selector/keypad
state machine on top of this.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from .rtd_vcf import temp_c_to_ohms, ohms_to_temp_c, rtd_continuity_ok, vcf_factor, VCF_TYPES
from .pulser import Pulser


# ---------------------------------------------------------------------------
# Per-product electrical/series facts, pulled directly from the skill's
# per-product reference tables. Kept as data so the UI can show genuinely
# correct nameplate figures rather than hard-coded prose.
# ---------------------------------------------------------------------------
PRODUCT_SPECS = {
    "lcr2": {
        "name": "LectroCount LCR-II",
        "series": "E3657/E3658",
        "cpu_board": "840405 (schematic title) / 840450 (board silkscreen — manufacturer's own documentation discrepancy)",
        "pulse_hz_max": 10000,
        "current_a_max": 4.5,
        "display": '1" backlit LCD, 6-character',
        "num_products": 4,
        "has_pos": False,
        "has_wireless": False,
        "voltage_min": 9, "voltage_max": 28, "voltage_optimum": 12,
        "hazardous_area": "Class I Div 2 Gr C&D, IP66; ATEX/IECEx/INMETRO: II 3G Ex nA ic IIB T5 Gc",
        "nameplate_ratings_note": 'Nameplate prints "9-28 VDC, 3A" — conflicts with the 4.5A body-text spec; this is a real inconsistency in the source manual, not a simulator bug.',
    },
    "lcr600": {
        "name": "LectroCount LCR 600",
        "series": "E3708/E3709",
        "cpu_board": "840405",
        "pulse_hz_max": 10000,
        "current_a_max": 4.5,
        "display": '5.7" QVGA (320x240) transflective LCD',
        "num_products": 16,
        "has_pos": True,
        "has_wireless": False,
        "voltage_min": 9, "voltage_max": 28, "voltage_optimum": 12,
        "hazardous_area": "UL E180172, Class I Div 2 Gr C&D, T6 (<=85C)",
        "nameplate_ratings_note": None,
    },
    "lcriq": {
        "name": "LCR.iQ",
        "series": "CENTRILOGiQ platform",
        "cpu_board": "84353 I/O board (Rev J)",
        "pulse_hz_max": 7500,
        "current_a_max": 5.0,
        "display": '7" TFT/LCD, 800x480',
        "num_products": 16,
        "has_pos": True,
        "has_wireless": True,
        "voltage_min": 9, "voltage_max": 28, "voltage_optimum": 12,
        "hazardous_area": "UL E180172, Class I Div 2/Zone 2; ATEX/IECEx II 3G Ex ec ic Gc T4",
        "nameplate_ratings_note": None,
    },
}

PROD_TYPES = ["", "Gasoline", "Distillate", "Aviation", "LPG", "Ammonia", "Methanol", "Lube Oil"]


@dataclass
class CalPoint:
    flow_rate: float
    pct_error: float = 0.0


@dataclass
class Product:
    number: int
    name: str = ""
    prod_type: str = ""
    pulses_per_unit: float = 100.0     # k-Factor
    vcf_type: str = "NONE"
    vcf_param: float = 0.0
    s1_close: float = 0.0
    aux_mult_sg: float = 0.0           # specific gravity, for AUX MULT = SpGr * 8.345
    cal_points: list = field(default_factory=list)  # list[CalPoint], multi-point cal
    shift_gross: float = 0.0
    shift_net: float = 0.0
    shift_deliveries: int = 0
    # LCR-600/iQ POS fields
    price_per_unit: float = 0.0
    tax_category: int = 0

    def aux_mult(self) -> float:
        return round(self.aux_mult_sg * 8.345, 4) if self.aux_mult_sg else 0.0


class RegisterError(Exception):
    """Raised internally to surface one of the register's own error strings
    (RANGE ERROR, VCF DOMAIN ERROR, etc.) up to the UI layer unchanged."""
    pass


class RegisterBase:
    """
    Shared engine. Selector-position / menu-navigation state machines are
    implemented per-product in subclasses, but everything about *what a
    delivery, calibration, or fault actually does* lives here so the three
    product models can't silently diverge on the underlying physics/math —
    only on how the operator gets to it.
    """

    PRODUCT_KEY = "base"

    def __init__(self):
        spec = PRODUCT_SPECS[self.PRODUCT_KEY]
        self.spec = spec
        n = spec["num_products"]
        self.products: list[Product] = [Product(number=i + 1) for i in range(n)]
        self.active_product_idx = 0

        self.pulser = Pulser(k_factor=self.products[0].pulses_per_unit)

        self.delivery_total_units = 0.0
        self.preset_units: float | None = None
        self.delivery_active = False
        self.delivery_pending_print = False
        self.last_ticket: dict | None = None
        self.gross_total = 0.0  # lifetime, 10-digit totalizer concept
        self.no_flow_timer_minutes = 5.0
        self._no_flow_since: float | None = None
        self._last_pulses_seen = 0

        # UI/operational state shared across all three product models.
        self.view_mode = "original"   # "original" | "custom"
        self.selected_printer = "epson_slip"
        self.ticket_header_lines: list[str] = []
        # W&M tamper-seal concept: when locked, calibration-affecting
        # actions are rejected with a dedicated error rather than silently
        # applied — mirrors the real fillister-hole lead-seal mechanism
        # described in the LCR-II bill of materials. Field techs would
        # need to physically break this seal (and log it) before
        # recalibrating a real unit.
        self.locked = False

        # Simulated "true" environment temperature (what the liquid actually
        # is), separate from the RTD's *reported* ohms, so we can model
        # probe-fault / range-adjustment scenarios honestly.
        self.true_temp_c = 15.0
        self.rtd_offset_c = 0.0           # field-trim, +/-0.3C ceiling
        self.rtd_probe_connected = True
        self.errors: list[str] = []
        self.last_calibrated = "—"
        self.power_voltage = spec["voltage_optimum"]

    # ------------------------------------------------------------------ #
    # Power / RTD
    # ------------------------------------------------------------------ #
    def rtd_reading_ohms(self) -> float:
        if not self.rtd_probe_connected:
            return -1.0  # open circuit -> sentinel the UI renders as "OPEN"
        return temp_c_to_ohms(self.true_temp_c)

    def rtd_reading_c(self) -> float:
        if not self.rtd_probe_connected:
            return float("nan")
        return ohms_to_temp_c(self.rtd_reading_ohms()) + self.rtd_offset_c

    def rtd_continuity_pass(self) -> bool:
        return self.rtd_probe_connected and rtd_continuity_ok(self.rtd_reading_ohms())

    def set_rtd_offset(self, requested_offset_c: float):
        """Mirrors the manual's System Calibration Screen 3 TEMP/OFFSET
        field: adjustment limited to +/-0.3C, else RANGE ERROR and the
        manual's prescribed remedy is 'replace the RTD probe'."""
        if self.locked:
            self._raise_error("CALIBRATION LOCKED - BREAK W&M SEAL TO PROCEED")
        if abs(requested_offset_c) > 0.3:
            self._raise_error("RANGE ERROR")
        self.rtd_offset_c = requested_offset_c

    def power_fault_messages(self) -> list[str]:
        msgs = []
        if self.power_voltage < self.spec["voltage_min"]:
            msgs.append("Power Failure")
        elif self.power_voltage < 11:
            msgs.append("LOW VOLTAGE — below 11V minimum recommended under load")
        return msgs

    # ------------------------------------------------------------------ #
    # Calibration — single point (k-Factor / PROVER QTY workflow)
    # ------------------------------------------------------------------ #
    def active_product(self) -> Product:
        return self.products[self.active_product_idx]

    def select_product(self, idx: int):
        if not (0 <= idx < len(self.products)):
            self._raise_error("RANGE ERROR")
        self.active_product_idx = idx
        self.pulser.set_k_factor(self.active_product().pulses_per_unit)

    def recalibrate_from_prover(self, prover_qty: float):
        """The actual k-Factor recalibration the manual describes: overwrite
        PROVER QTY with the true prover reading; the register backs out a
        new k-Factor from (pulses counted this proving run) / prover_qty."""
        if self.locked:
            self._raise_error("CALIBRATION LOCKED - BREAK W&M SEAL TO PROCEED")
        p = self.active_product()
        snap = self.pulser.snapshot()
        pulses = snap["total_pulses"]
        if prover_qty <= 0:
            self._raise_error("RANGE ERROR")
        meter_qty = pulses / p.pulses_per_unit if p.pulses_per_unit else 0.0
        if meter_qty <= 0:
            self._raise_error("METER CALIB ERROR")

        new_k = pulses / prover_qty
        pct_error = (prover_qty - meter_qty) * 100.0 / prover_qty

        p.pulses_per_unit = round(new_k, 4)
        self.pulser.set_k_factor(p.pulses_per_unit)
        self.last_calibrated = time.strftime("%Y-%m-%d %H:%M:%S")
        return {"new_k_factor": p.pulses_per_unit, "meter_qty": meter_qty,
                "prover_qty": prover_qty, "pct_error": round(pct_error, 4)}

    def set_vcf(self, vcf_type: str, param: float):
        if self.locked:
            self._raise_error("CALIBRATION LOCKED - BREAK W&M SEAL TO PROCEED")
        if vcf_type not in VCF_TYPES:
            self._raise_error("RANGE ERROR")
        vt = VCF_TYPES[vcf_type]
        if vt.code != "NONE" and not (vt.param_min <= param <= vt.param_max) and vt.param_label != "—":
            self._raise_error("RANGE ERROR")
        p = self.active_product()
        p.vcf_type = vcf_type
        p.vcf_param = param

    # ------------------------------------------------------------------ #
    # Multi-point calibration — duplicate-rate and adjacent-0.25% checks
    # ------------------------------------------------------------------ #
    def add_cal_point(self, flow_rate: float, pct_error: float):
        if self.locked:
            self._raise_error("CALIBRATION LOCKED - BREAK W&M SEAL TO PROCEED")
        p = self.active_product()
        if len(p.cal_points) >= 10:
            self._raise_error("RANGE ERROR")
        for cp in p.cal_points:
            if abs(cp.flow_rate - flow_rate) < 1e-9:
                self._raise_error("DUPLICATE FLOW RATE ERROR")
        pts = sorted(p.cal_points + [CalPoint(flow_rate, pct_error)], key=lambda c: c.flow_rate)
        idx = pts.index(next(c for c in pts if c.flow_rate == flow_rate))
        if idx > 0 and abs(pts[idx].pct_error - pts[idx - 1].pct_error) > 0.25:
            self._raise_error("ADJACENT POINTS OUT OF 0.25% RANGE")
        if idx < len(pts) - 1 and abs(pts[idx].pct_error - pts[idx + 1].pct_error) > 0.25:
            self._raise_error("ADJACENT POINTS OUT OF 0.25% RANGE")
        p.cal_points = pts

    # ------------------------------------------------------------------ #
    # Delivery lifecycle (RUN / STOP / PRINT concept — selector position
    # interpretation differs per-product but the underlying delivery math
    # below is identical across the family)
    # ------------------------------------------------------------------ #
    def start_delivery(self):
        if self.preset_units is not None and self.preset_units <= 0:
            self._raise_error("RANGE ERROR")
        p = self.active_product()
        if p.pulses_per_unit <= 0:
            self._raise_error("METER CALIB ERROR")
        self.delivery_active = True
        self.delivery_total_units = 0.0
        self._last_pulses_seen = self.pulser.snapshot()["total_pulses"]
        self._no_flow_since = None
        self.pulser.set_mode(Pulser.MODE_FLOWING if self.pulser.flow_rate_units_per_min > 0 else Pulser.MODE_IDLE)

    def tick(self):
        """Call periodically (the Flask app calls this once per /state poll)
        to advance delivery accounting, evaluate the no-flow timer, evaluate
        VCF domain, and auto-close a preset delivery exactly like the real
        firmware would. Internal faults are caught here and recorded in
        self.errors rather than propagating — tick() is called every poll
        from the Flask layer and must never raise."""
        if not self.delivery_active:
            return

        snap = self.pulser.snapshot()
        pulses_now = snap["total_pulses"]
        new_pulses = pulses_now - self._last_pulses_seen
        self._last_pulses_seen = pulses_now

        p = self.active_product()
        if new_pulses > 0:
            self._no_flow_since = None
            raw_units = new_pulses / p.pulses_per_unit
            factor, vcf_err = vcf_factor(p.vcf_type, p.vcf_param, self.rtd_reading_c())
            if vcf_err:
                # VCF DOMAIN ERROR: record the fault but, like the real
                # register, keep counting raw (uncorrected) volume rather
                # than silently dropping it — the operator still owes for
                # what physically went through the meter.
                if vcf_err not in self.errors:
                    self.errors.append(vcf_err)
                factor = 1.0
            self.delivery_total_units += raw_units * factor
        else:
            if self._no_flow_since is None:
                self._no_flow_since = time.monotonic()
            elif self.no_flow_timer_minutes > 0:
                elapsed_min = (time.monotonic() - self._no_flow_since) / 60.0
                if elapsed_min >= min(self.no_flow_timer_minutes, 60):
                    if "NO-FLOW STOP ERROR" not in self.errors:
                        self.errors.append("NO-FLOW STOP ERROR")
                    self.stop_delivery(auto=True)
                    return

        if self.preset_units is not None and self.delivery_total_units >= self.preset_units:
            self.pulser.set_mode(Pulser.MODE_IDLE)
            self.stop_delivery(auto=True)

    def stop_delivery(self, auto: bool = False):
        if not self.delivery_active:
            return
        if not auto:
            # Bring delivery_total_units fully current before we stop and
            # build a ticket, rather than depending on some other caller
            # (e.g. the browser's polling loop) having recently called
            # tick(). auto=True stops are already mid-tick() (called from
            # inside tick() itself), so re-entering would double-count.
            self.tick()
        self.delivery_active = False
        self.gross_total += self.delivery_total_units
        p = self.active_product()
        p.shift_gross += self.delivery_total_units
        p.shift_net += self.delivery_total_units  # net == gross in this sim (no meter-factor split modeled)
        p.shift_deliveries += 1
        self.last_ticket = self._build_ticket(p, auto)
        self.delivery_pending_print = True
        self.pulser.set_mode(Pulser.MODE_IDLE)

    def _build_ticket(self, p: Product, auto_stopped: bool) -> dict:
        snap = self.pulser.snapshot()
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "product_number": p.number,
            "product_name": p.name or f"PROD {p.number}",
            "delivery_total": round(self.delivery_total_units, 2),
            "temperature_c": round(self.rtd_reading_c(), 2) if self.rtd_probe_connected else None,
            "vcf_type": p.vcf_type,
            "k_factor": p.pulses_per_unit,
            "price_per_unit": p.price_per_unit if self.spec["has_pos"] else None,
            "total_price": round(self.delivery_total_units * p.price_per_unit, 2) if self.spec["has_pos"] and p.price_per_unit else None,
            "auto_stopped": auto_stopped,
            "errors_during_delivery": list(self.errors),
        }

    # ------------------------------------------------------------------ #
    # Error dictionary — exact vocabulary from the skill
    # ------------------------------------------------------------------ #
    def _raise_error(self, code: str):
        """Records the error code in the persistent error list (so it shows
        on a diagnostic ticket / error banner even after this call returns)
        AND raises RegisterError so the calling method halts immediately —
        mirroring how the real firmware both displays an error message and
        refuses to proceed with whatever was rejected."""
        if code not in self.errors:
            self.errors.append(code)
        raise RegisterError(code)

    def clear_errors(self):
        self.errors = []

    # ------------------------------------------------------------------ #
    # W&M tamper seal, view mode, printer selection
    # ------------------------------------------------------------------ #
    def set_locked(self, locked: bool):
        self.locked = locked
        if locked:
            # Breaking back to locked also clears any stale "locked" error
            # so the banner doesn't show a contradictory state.
            self.errors = [e for e in self.errors if "CALIBRATION LOCKED" not in e]

    def set_view_mode(self, mode: str):
        if mode not in ("original", "custom"):
            self._raise_error("RANGE ERROR")
        self.view_mode = mode

    def set_printer(self, printer_key: str):
        from .printer import PRINTER_MODELS
        if printer_key not in PRINTER_MODELS:
            self._raise_error("RANGE ERROR")
        self.selected_printer = printer_key

    def set_ticket_header(self, lines: list[str]):
        self.ticket_header_lines = list(lines)[:12]  # manual's own 12-line ceiling

    # ------------------------------------------------------------------ #
    # J8 / J13 / J14 diagnostic snapshots — exact voltage tables from skill
    # ------------------------------------------------------------------ #
    def diagnostics_snapshot(self) -> dict:
        pulser_snap = self.pulser.snapshot()
        ohms = self.rtd_reading_ohms()
        return {
            "j8": {
                "v32": pulser_snap["j8_v32"],
                "v33": pulser_snap["j8_v33"],
                "v34": pulser_snap["j8_v34"],
                "mode": pulser_snap["mode"],
                "interpretation": self._interpret_j8(pulser_snap),
            },
            "j14_rtd": {
                "ohms": None if ohms < 0 else round(ohms, 2),
                "continuity_pass": self.rtd_continuity_pass(),
                "expected_band": "100 ohm +/- 20 ohm",
            },
            "power": {
                "voltage": self.power_voltage,
                "min_required": self.spec["voltage_min"],
                "optimum": self.spec["voltage_optimum"],
                "faults": self.power_fault_messages(),
            },
        }

    @staticmethod
    def _interpret_j8(snap: dict) -> str:
        if snap["mode"] == Pulser.MODE_STALLED:
            return "Pulser stalled / shaft locked — #32 reads 0V, #33/#34 read 1-3V. Replace encoder harness if this persists with the truck stationary and engine off."
        return "No flow / good pulser signature — #32 reads +5V, #33/#34 toggle 0V or +5V with each pulse edge."

    # ------------------------------------------------------------------ #
    # Serialization for the UI
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        from .printer import PRINTER_MODELS, format_delivery_ticket
        p = self.active_product()
        pulser_snap = self.pulser.snapshot()
        printed_ticket_text = None
        if self.last_ticket:
            printed_ticket_text = format_delivery_ticket(
                self.last_ticket, self.selected_printer, self.ticket_header_lines)
        return {
            "product_key": self.PRODUCT_KEY,
            "spec": self.spec,
            "view_mode": self.view_mode,
            "locked": self.locked,
            "selected_printer": self.selected_printer,
            "printer_models": {k: {"label": v.label, "form_factor": v.form_factor, "notes": v.notes}
                                for k, v in PRINTER_MODELS.items()},
            "printed_ticket_text": printed_ticket_text,
            "active_product": {
                "number": p.number,
                "name": p.name,
                "prod_type": p.prod_type,
                "pulses_per_unit": p.pulses_per_unit,
                "vcf_type": p.vcf_type,
                "vcf_type_label": VCF_TYPES[p.vcf_type].label,
                "vcf_param": p.vcf_param,
                "s1_close": p.s1_close,
                "aux_mult": p.aux_mult(),
                "cal_points": [{"flow_rate": c.flow_rate, "pct_error": c.pct_error} for c in p.cal_points],
                "shift_gross": round(p.shift_gross, 2),
                "shift_net": round(p.shift_net, 2),
                "shift_deliveries": p.shift_deliveries,
                "price_per_unit": p.price_per_unit,
                "tax_category": p.tax_category,
            },
            "products_brief": [{"number": pr.number, "name": pr.name or f"PROD {pr.number}"} for pr in self.products],
            "delivery_total_units": round(self.delivery_total_units, 3),
            "preset_units": self.preset_units,
            "delivery_active": self.delivery_active,
            "delivery_pending_print": self.delivery_pending_print,
            "last_ticket": self.last_ticket,
            "gross_total": round(self.gross_total, 2),
            "no_flow_timer_minutes": self.no_flow_timer_minutes,
            "true_temp_c": round(self.true_temp_c, 2),
            "rtd_reading_c": None if not self.rtd_probe_connected else round(self.rtd_reading_c(), 2),
            "rtd_offset_c": self.rtd_offset_c,
            "rtd_probe_connected": self.rtd_probe_connected,
            "errors": list(self.errors),
            "last_calibrated": self.last_calibrated,
            "power_voltage": self.power_voltage,
            "vcf_types": {k: {"label": v.label, "param_label": v.param_label,
                               "param_min": v.param_min, "param_max": v.param_max,
                               "tmin": v.tmin, "tmax": v.tmax, "unit": v.unit}
                          for k, v in VCF_TYPES.items()},
            "pulser": pulser_snap,
            "diagnostics": self.diagnostics_snapshot(),
        }
