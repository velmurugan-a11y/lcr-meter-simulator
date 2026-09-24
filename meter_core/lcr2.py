"""
lcr2.py — LCR-II specific operator-interface model: the 6-position rotary
selector switch (RUN / STOP / PRINT / SHIFT PRINT / CALIBRATION, plus the
implicit "off-detent" positions between them ignored here) and the
SELECT/INCREASE two-button menu navigation documented in the skill's menu
map (EM100-11MM) and preset-delivery procedure.
"""
from __future__ import annotations
import time

from .register_base import RegisterBase, RegisterError


class LCR2Register(RegisterBase):
    PRODUCT_KEY = "lcr2"

    SELECTOR_POSITIONS = ["RUN", "STOP", "PRINT", "SHIFT_PRINT", "CALIBRATION"]

    def __init__(self):
        super().__init__()
        self.selector = "STOP"
        # Menu-cursor state for the STOP/PRINT/CALIBRATION button-driven flows
        self.menu_field = "PROD"       # PROD -> PRESET_DIGITS -> (delivery)
        self.preset_digit_idx = 0
        self.preset_digits = [0, 0, 0, 0, 0, 0]  # 6-digit preset entry, leftmost first
        self._shift_print_started_at: float | None = None
        self.password = "00000"

    # ------------------------------------------------------------------ #
    # Selector switch
    # ------------------------------------------------------------------ #
    def set_selector(self, position: str):
        if position not in self.SELECTOR_POSITIONS:
            raise RegisterError("RANGE ERROR")
        # Bring delivery accounting fully current before reading/acting on
        # it below — this must not depend on some other caller (e.g. the
        # browser's polling loop) having recently called tick(), since a
        # future serial-bridge integration may drive this method directly
        # with no concurrent poller at all.
        self.tick()
        prev = self.selector
        self.selector = position

        if position == "RUN":
            if not self.delivery_active:
                self.start_delivery()
        elif position == "STOP":
            if self.delivery_active:
                # Packing-hose semantics: if less than 1 unit was recorded,
                # silently reset with no ticket (manual's explicit rule).
                if self.delivery_total_units < 1.0:
                    self.delivery_active = False
                    self.delivery_total_units = 0.0
                    self.pulser.set_mode("idle")
                else:
                    self.stop_delivery()
            self.menu_field = "PROD"
        elif position == "PRINT":
            self.delivery_pending_print = False
        elif position == "SHIFT_PRINT":
            self._shift_print_started_at = time.monotonic()
        elif position == "CALIBRATION":
            self.menu_field = "K_FACTOR"

        # Leaving SHIFT_PRINT triggers the timed shift-vs-diagnostic split
        # documented in the quick reference card: held >2s -> shift ticket
        # (fires on the transition back to STOP); held <2s -> diagnostic
        # ticket (fires on the transition to PRINT).
        if prev == "SHIFT_PRINT" and self._shift_print_started_at is not None:
            held = time.monotonic() - self._shift_print_started_at
            self._shift_print_started_at = None
            if position == "STOP" and held > 2.0:
                self.last_ticket = self._build_shift_ticket()
                self.delivery_pending_print = True
            elif position == "PRINT" and held <= 2.0:
                self.last_ticket = self._build_diagnostic_ticket()
                self.delivery_pending_print = True

    def _build_shift_ticket(self) -> dict:
        p = self.active_product()
        return {
            "kind": "SHIFT",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "product_number": p.number,
            "shift_gross": round(p.shift_gross, 2),
            "shift_net": round(p.shift_net, 2),
            "shift_deliveries": p.shift_deliveries,
        }

    def _build_diagnostic_ticket(self) -> dict:
        diag = self.diagnostics_snapshot()
        return {
            "kind": "DIAGNOSTIC",
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "last_calibrated": self.last_calibrated,
            "j8": diag["j8"],
            "j14_rtd": diag["j14_rtd"],
            "power": diag["power"],
            "errors": list(self.errors),
        }

    # ------------------------------------------------------------------ #
    # SELECT / INCREASE two-button navigation, mirroring the menu map's
    # documented flows for STOP (preset entry) and CALIBRATION (k-Factor).
    # ------------------------------------------------------------------ #
    def press_increase(self):
        if self.selector == "STOP":
            if self.menu_field == "PROD":
                self.select_product((self.active_product_idx + 1) % len(self.products))
            elif self.menu_field == "PRESET_DIGITS":
                d = self.preset_digit_idx
                self.preset_digits[d] = (self.preset_digits[d] + 1) % 10
        elif self.selector == "CALIBRATION":
            if self.menu_field == "K_FACTOR":
                p = self.active_product()
                p.pulses_per_unit = round(p.pulses_per_unit + 1, 2)
                self.pulser.set_k_factor(p.pulses_per_unit)
            elif self.menu_field == "PROD":
                self.select_product((self.active_product_idx + 1) % len(self.products))

    def press_select(self):
        if self.selector == "STOP":
            if self.menu_field == "PROD":
                self.menu_field = "PRESET_DIGITS"
                self.preset_digit_idx = 0
                self.preset_digits = [0, 0, 0, 0, 0, 0]
            elif self.menu_field == "PRESET_DIGITS":
                if self.preset_digit_idx < 5:
                    self.preset_digit_idx += 1
                else:
                    value = int("".join(str(d) for d in self.preset_digits))
                    self.preset_units = value / 10.0  # last digit = tenths, matching 6-char display convention
                    self.menu_field = "PROD"
        elif self.selector == "CALIBRATION":
            if self.menu_field == "K_FACTOR":
                self.menu_field = "PROD"
            elif self.menu_field == "PROD":
                self.menu_field = "K_FACTOR"

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        d = super().to_dict()
        d["lcr2"] = {
            "selector": self.selector,
            "menu_field": self.menu_field,
            "preset_digits": self.preset_digits,
            "preset_digit_idx": self.preset_digit_idx,
        }
        return d
