"""
pulser.py — Simulates the quadrature pulser feeding the register's J8
connector, including the three fault signatures documented in the skill's
pulser fault voltage table (measured at J8 #32/#33/#34, ground ref #37/#38):

    Condition                      | #32     | #33         | #34
    No flow / idle                 | +5 VDC  | 0 or +5 VDC | 0 or +5 VDC
    Pulser stalled / shaft locked  | 0 VDC   | +1-3 VDC    | +1-3 VDC
    Good pulser, product flowing   | +5 VDC  | 0 or +5 VDC | 0 or +5 VDC

This is a background thread: it advances a running pulse counter at a
caller-set flow rate, with optional jitter (vibration) and stall injection,
and exposes the instantaneous "what would a voltmeter read right now at J8"
values so the simulator's diagnostics screen can show genuinely consistent
fault readings rather than canned text.
"""
from __future__ import annotations
import threading
import time
import random


class Pulser:
    MODE_IDLE = "idle"
    MODE_FLOWING = "flowing"
    MODE_STALLED = "stalled"
    MODE_VIBRATION = "vibration"  # flowing, but with noisy/jittery edges

    def __init__(self, k_factor: float = 100.0):
        self._lock = threading.Lock()
        self.k_factor = k_factor          # pulses per unit (PULSES/UNIT)
        self.mode = self.MODE_IDLE
        self.flow_rate_units_per_min = 0.0
        self.total_pulses = 0
        self._quad_phase = 0              # 0..3, for the #33/#34 toggle illusion
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._last_tick = time.monotonic()
        self._fault_threshold_pulses = 0  # set by caller for PULSER FAILURE logic
        self._pulses_since_fault_check = 0
        # Carries the fractional leftover from int(round(...)) truncation
        # across ticks. Without this, any flow rate low enough that a
        # single ~50ms tick contributes less than 0.5 pulses NEVER produces
        # any pulses at all, no matter how long it runs (0.49999 rounds to
        # 0 forever). With the carry, the fractional remainder accumulates
        # tick over tick until it crosses a whole-pulse boundary, exactly
        # like a real quadrature encoder's edges would actually arrive.
        self._pulse_remainder = 0.0

    def stop(self):
        self._running = False

    def set_mode(self, mode: str):
        with self._lock:
            self.mode = mode

    def set_flow_rate(self, units_per_min: float):
        """Setting a positive rate while idle/flowing auto-promotes to
        flowing mode (mirrors a real pulser: it spins because the truck's
        PTO/pump is running, not because of a separate software flag).
        Setting rate to 0 while flowing demotes back to idle. STALLED is a
        distinct fault mode and is left alone here — only set_mode() can
        clear a stall, matching the real diagnostic workflow (you fix or
        replace the encoder, the pulser doesn't un-stall itself)."""
        with self._lock:
            self.flow_rate_units_per_min = max(0.0, units_per_min)
            if self.mode == self.MODE_STALLED:
                return
            if self.flow_rate_units_per_min > 0 and self.mode == self.MODE_IDLE:
                self.mode = self.MODE_FLOWING
            elif self.flow_rate_units_per_min <= 0 and self.mode in (self.MODE_FLOWING, self.MODE_VIBRATION):
                self.mode = self.MODE_IDLE

    def set_k_factor(self, k: float):
        with self._lock:
            self.k_factor = max(0.0001, k)

    def reset_totalizer(self):
        with self._lock:
            self.total_pulses = 0

    def snapshot(self) -> dict:
        """Everything the UI / register state machine needs this tick."""
        with self._lock:
            mode = self.mode
            pulses = self.total_pulses
            rate = self.flow_rate_units_per_min
            k = self.k_factor
            phase = self._quad_phase

        # Reproduce the exact voltage table from the skill. "0 or +5 VDC" in
        # the source table reflects the quadrature channel's natural toggle
        # as pulses occur, which is exactly what _quad_phase models.
        if mode == self.MODE_STALLED:
            v32, v33, v34 = 0.0, round(random.uniform(1.0, 3.0), 2), round(random.uniform(1.0, 3.0), 2)
        else:
            v32 = 5.0
            v33 = 5.0 if (phase & 1) else 0.0
            v34 = 5.0 if (phase & 2) else 0.0

        return {
            "mode": mode,
            "total_pulses": pulses,
            "flow_rate_units_per_min": rate,
            "k_factor": k,
            "units_total": pulses / k if k else 0.0,
            "j8_v32": v32,
            "j8_v33": v33,
            "j8_v34": v34,
        }

    def _run(self):
        while self._running:
            time.sleep(0.05)  # 20 Hz internal tick — plenty smooth for a sim
            now = time.monotonic()
            dt = now - self._last_tick
            self._last_tick = now

            with self._lock:
                mode = self.mode
                rate = self.flow_rate_units_per_min
                k = self.k_factor

            if mode == self.MODE_STALLED or mode == self.MODE_IDLE or rate <= 0:
                continue

            # pulses this tick = (units/min) * k(pulses/unit) * (dt/60)
            base_pulses = rate * k * (dt / 60.0)

            if mode == self.MODE_VIBRATION:
                # Vibration adds +/-15% jitter pulse-to-pulse, simulating the
                # high-vibration-environment pulser-failure scenario the
                # manual calls out (still counts as legitimate flow, but
                # noisy enough to plausibly trip a 0.1%-of-delivery check
                # if the caller wires that up).
                base_pulses *= random.uniform(0.85, 1.15)

            with self._lock:
                # Carry the fractional remainder forward (see __init__'s
                # comment on _pulse_remainder) so low flow rates correctly
                # accumulate whole pulses over time instead of every tick's
                # sub-0.5 contribution being silently discarded forever.
                total_fractional = base_pulses + self._pulse_remainder
                add = int(total_fractional)  # floor, not round -- the remainder already carries the rest
                self._pulse_remainder = total_fractional - add
                if add > 0:
                    self.total_pulses += add
                    self._quad_phase = (self._quad_phase + add) % 4
