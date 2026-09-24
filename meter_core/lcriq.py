"""
lcriq.py — LCR.iQ specific operator-interface model: full touchscreen-style
menu navigation, wireless (Bluetooth/Wi-Fi) connection state machine, the
SENSEiQ expansion board's six 4-20mA analog inputs (tank level / water
detection use case from the skill), and digital multi-stage valve control
in place of the legacy products' fixed two-stage valve.
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field

from .register_base import RegisterBase, RegisterError


@dataclass
class AnalogInput:
    channel: int
    label: str = ""
    ma_value: float = 4.0          # 4-20mA loop
    use_case: str = "UNUSED"       # UNUSED | TANK_LEVEL | WATER_DETECT


@dataclass
class Tank:
    number: int
    name: str = ""
    level_pct: float = 0.0
    capacity_units: float = 10000.0


class LCRiQRegister(RegisterBase):
    PRODUCT_KEY = "lcriq"

    SCREENS = ["HOME", "DELIVERY", "SETUP_REGISTER", "SETUP_METER", "SETUP_CALIBRATION",
               "SETUP_SECURITY", "SETUP_IO", "WIRELESS", "TANK_INVENTORY", "DIAGNOSTICS"]

    BT_STATES = ["OFF", "SCANNING", "PAIRING", "CONNECTED"]
    WIFI_STATES = ["OFF", "SCANNING", "CONNECTED"]

    def __init__(self):
        super().__init__()
        self.screen = "HOME"
        self.running = False  # iQ has no rotary switch; RUN/STOP are soft buttons
        self.field_cursor = 0

        # SENSEiQ: 1 onboard 4-20mA input + 6 on the expansion board = 7 total
        self.analog_inputs = [AnalogInput(channel=i + 1) for i in range(7)]
        self.tanks = [Tank(number=i + 1) for i in range(12)]

        self.bluetooth_state = "OFF"
        self.bluetooth_paired_device = None
        self.wifi_state = "OFF"
        self.wifi_ssid = None

        # Digital valve control: 0.0 (closed) .. 1.0 (full open), continuously
        # variable, modeling the brochure's "multi-stage variable flow-rate
        # control with ramp-up/ramp-down" vs. the legacy fixed 2-stage valve.
        self.valve_position = 0.0
        self.valve_target = 0.0
        self.valve_ramp_rate = 1.0  # fraction per SECOND (was "per tick" -- see tick() below for why that changed)

        self.home_screen_profile = "CUSTOM"  # LPG | REFINED | AVIATION | CUSTOM
        self.qr_ticket_pending = False
        # Baseline for the wall-clock valve ramp in tick() below -- set at
        # construction time, NOT lazily on first tick(), so that "object
        # existed for N seconds before anyone called tick()" correctly
        # counts as N seconds of ramp time on that first call, rather than
        # always producing zero movement on a cold first tick.
        self._valve_last_tick = time.monotonic()

    # ------------------------------------------------------------------ #
    def navigate(self, screen: str):
        if screen not in self.SCREENS:
            raise RegisterError("RANGE ERROR")
        self.screen = screen
        self.field_cursor = 0

    def start_delivery_button(self):
        if self.running:
            return
        self.running = True
        self.valve_target = 1.0
        # The base class's start_delivery() decides FLOWING vs IDLE by
        # checking self.pulser.flow_rate_units_per_min directly -- but on
        # the iQ, the "real" commanded rate lives in _commanded_flow_rate
        # until tick() scales it by valve position and pushes it into the
        # pulser. Push it in now so the base class sees a true rate at the
        # moment delivery starts, rather than whatever stale value (often
        # 0) happened to be sitting in the pulser already.
        commanded = getattr(self, "_commanded_flow_rate", 0.0)
        if commanded > 0:
            self.pulser.set_flow_rate(commanded * max(self.valve_position, 0.01))
        self.start_delivery()

    def stop_delivery_button(self):
        if not self.running:
            return
        # See lcr2.py's identical comment: this must not depend on an
        # external poller having recently called tick().
        self.tick()
        self.running = False
        self.valve_target = 0.0
        if self.delivery_total_units >= 1.0:
            self.stop_delivery()
            self.qr_ticket_pending = True
        else:
            self.delivery_active = False
            self.delivery_total_units = 0.0
            self.pulser.set_mode("idle")

    def tick(self):
        # Ramp the digital valve continuously toward its target before
        # calling the shared delivery-accounting tick, modeling the
        # multi-stage ramp-up/ramp-down the brochure describes.
        #
        # This is wall-clock-based (elapsed real seconds * a rate-per-second),
        # not "a fixed step every time tick() happens to be called" -- the
        # latter would make the valve's ramp speed depend on how often some
        # external caller polls, which breaks badly for a future
        # serial-bridge integration that might call this far less often
        # than a browser's 250ms poll loop. A single tick() after a long
        # gap (e.g. 1 real second of nobody polling) now correctly ramps
        # the valve the full second's worth, not just one fixed increment.
        now = time.monotonic()
        elapsed = max(0.0, now - self._valve_last_tick)
        self._valve_last_tick = now
        # valve_ramp_rate is "fraction per second" under this model.
        max_step = self.valve_ramp_rate * elapsed

        if self.valve_position < self.valve_target:
            self.valve_position = min(self.valve_target, self.valve_position + max_step)
        elif self.valve_position > self.valve_target:
            self.valve_position = max(self.valve_target, self.valve_position - max_step)

        # Valve position scales the *effective* flow the pulser produces,
        # so the UI visibly shows ramp-up/ramp-down affecting delivery rate.
        if self.delivery_active:
            base_rate = getattr(self, "_commanded_flow_rate", self.pulser.flow_rate_units_per_min)
            self.pulser.set_flow_rate(base_rate * self.valve_position)

        super().tick()

    def set_commanded_flow_rate(self, units_per_min: float):
        self._commanded_flow_rate = max(0.0, units_per_min)

    # ------------------------------------------------------------------ #
    # Wireless — mirrors the skill's documented Bluetooth pairing flow
    # ------------------------------------------------------------------ #
    def bluetooth_toggle(self, on: bool):
        self.bluetooth_state = "OFF" if not on else "SCANNING" if self.bluetooth_state == "OFF" else self.bluetooth_state

    def bluetooth_scan(self):
        if self.bluetooth_state == "OFF":
            raise RegisterError("RANGE ERROR")
        self.bluetooth_state = "SCANNING"

    def bluetooth_connect(self, device_name: str, is_printer: bool):
        if self.bluetooth_state not in ("SCANNING", "PAIRING"):
            raise RegisterError("RANGE ERROR")
        # Printer auto-pairs+connects; 3rd-party device needs explicit pair first.
        self.bluetooth_state = "CONNECTED"
        self.bluetooth_paired_device = {"name": device_name, "role": "printer" if is_printer else "lcp_slave"}

    def bluetooth_disconnect(self):
        self.bluetooth_state = "OFF" if self.bluetooth_paired_device is None else "OFF"
        self.bluetooth_paired_device = None

    def wifi_toggle(self, on: bool):
        self.wifi_state = "SCANNING" if on else "OFF"
        if not on:
            self.wifi_ssid = None

    def wifi_connect(self, ssid: str):
        if self.wifi_state == "OFF":
            raise RegisterError("RANGE ERROR")
        self.wifi_state = "CONNECTED"
        self.wifi_ssid = ssid

    # ------------------------------------------------------------------ #
    # SENSEiQ — tank inventory via 4-20mA analog inputs
    # ------------------------------------------------------------------ #
    def set_analog_input(self, channel: int, ma_value: float, use_case: str):
        ch = next((a for a in self.analog_inputs if a.channel == channel), None)
        if ch is None:
            raise RegisterError("RANGE ERROR")
        if not (4.0 <= ma_value <= 20.0):
            raise RegisterError("RANGE ERROR")
        ch.ma_value = ma_value
        ch.use_case = use_case

    def tank_level_from_ma(self, ma_value: float) -> float:
        """4mA = empty, 20mA = full — standard loop convention."""
        return max(0.0, min(100.0, (ma_value - 4.0) / 16.0 * 100.0))

    def set_tank_ma(self, tank_number: int, ma_value: float):
        tank = next((t for t in self.tanks if t.number == tank_number), None)
        if tank is None or not (4.0 <= ma_value <= 20.0):
            raise RegisterError("RANGE ERROR")
        tank.level_pct = round(self.tank_level_from_ma(ma_value), 2)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        d = super().to_dict()
        d["lcriq"] = {
            "screen": self.screen,
            "running": self.running,
            "valve_position": round(self.valve_position, 3),
            "valve_target": round(self.valve_target, 3),
            "bluetooth_state": self.bluetooth_state,
            "bluetooth_paired_device": self.bluetooth_paired_device,
            "wifi_state": self.wifi_state,
            "wifi_ssid": self.wifi_ssid,
            "home_screen_profile": self.home_screen_profile,
            "qr_ticket_pending": self.qr_ticket_pending,
            "analog_inputs": [{"channel": a.channel, "label": a.label,
                                "ma_value": a.ma_value, "use_case": a.use_case,
                                "pct": round(self.tank_level_from_ma(a.ma_value), 1)}
                               for a in self.analog_inputs],
            "tanks": [{"number": t.number, "name": t.name or f"Tank {t.number}",
                       "level_pct": t.level_pct, "capacity_units": t.capacity_units}
                      for t in self.tanks],
        }
        return d
