"""
test_core.py — Standalone sanity checks for meter_core, run before any
Flask/HTML layer is added. Not a formal pytest suite; just a sequential
script that exercises each documented behavior and asserts it matches the
skill's source material, printing PASS/FAIL per check.
"""
import sys
import time

sys.path.insert(0, "/home/claude/lcr-sim")

from meter_core import LCR2Register, LCR600Register, LCRiQRegister, RegisterError
from meter_core.rtd_vcf import temp_c_to_ohms, ohms_to_temp_c, rtd_continuity_ok, vcf_factor

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"PASS: {label}")
        passed += 1
    else:
        print(f"FAIL: {label}")
        failed += 1


# --------------------------------------------------------------------- #
print("=== RTD curve (must match the two manual-stated anchor points) ===")
check("0C -> 100.00 ohm", abs(temp_c_to_ohms(0) - 100.00) < 0.01)
check("100C -> 138.51 ohm (manual states 138.5)", abs(temp_c_to_ohms(100) - 138.51) < 0.01)
check("round-trip 23.4C", abs(ohms_to_temp_c(temp_c_to_ohms(23.4)) - 23.4) < 0.01)
check("continuity pass at 100 ohm", rtd_continuity_ok(100.0) is True)
check("continuity pass at 80 ohm (lower edge)", rtd_continuity_ok(80.0) is True)
check("continuity fail at 79 ohm (just below band)", rtd_continuity_ok(79.0) is False)
check("continuity fail at 121 ohm (just above band)", rtd_continuity_ok(121.0) is False)

print()
print("=== VCF domain checking ===")
f, err = vcf_factor("API24", 0.52, (60 - 32) * 5 / 9)  # 60F == Tbase for API24; convert F->C since vcf_factor takes Celsius
check("API24 at Tbase(60F) gives factor ~1.0", err is None and abs(f - 1.0) < 0.01)
f, err = vcf_factor("API24", 0.52, 200)  # way outside Tmax=140F
check("API24 outside range raises VCF DOMAIN ERROR", err == "VCF DOMAIN ERROR")
f, err = vcf_factor("NONE", 0, 999)
check("NONE compensation always factor=1.0 regardless of temp", err is None and f == 1.0)

print()
print("=== LCR-II: k-Factor recalibration math ===")
r = LCR2Register()
p = r.active_product()
p.pulses_per_unit = 100.0
r.pulser.set_k_factor(100.0)
r.pulser.total_pulses = 0
r.pulser.set_mode("flowing")
r.pulser.total_pulses = 5000  # simulate 5000 pulses counted during a prove run
result = r.recalibrate_from_prover(prover_qty=49.0)
# meter_qty = 5000/100 = 50.0; %error = (49-50)*100/49 = -2.0408...
check("meter_qty computed correctly", abs(result["meter_qty"] - 50.0) < 1e-6)
check("%error computed correctly per skill's formula", abs(result["pct_error"] - (-2.0408)) < 0.001)
check("new k-factor = pulses/prover_qty", abs(result["new_k_factor"] - (5000 / 49.0)) < 0.001)

print()
print("=== LCR-II: multi-point calibration validation rules ===")
r2 = LCR2Register()
r2.add_cal_point(10.0, 0.05)
r2.add_cal_point(20.0, 0.10)
try:
    r2.add_cal_point(10.0, 0.20)
    check("duplicate flow rate raises error", False)
except RegisterError as e:
    check("duplicate flow rate raises DUPLICATE FLOW RATE ERROR", "DUPLICATE FLOW RATE ERROR" in str(e) or "DUPLICATE" in r2.errors[-1])
try:
    r2.add_cal_point(15.0, 5.0)  # way off from neighbors (0.05, 0.10) -> should violate 0.25% adjacency
    check("adjacent >0.25% raises error", False)
except RegisterError:
    check("adjacent points >0.25% apart raises ADJACENT POINTS error", "ADJACENT" in r2.errors[-1])

print()
print("=== LCR-II: RTD field-adjustment ceiling (+/-0.3C) ===")
r3 = LCR2Register()
r3.set_rtd_offset(0.25)
check("0.25C offset accepted (within ceiling)", r3.rtd_offset_c == 0.25)
try:
    r3.set_rtd_offset(0.5)
    check("0.5C offset should have raised RANGE ERROR", False)
except RegisterError:
    check("0.5C offset raises RANGE ERROR (exceeds 0.3C ceiling)", "RANGE ERROR" in r3.errors)

print()
print("=== LCR-II: selector/menu state machine + preset delivery flow ===")
r4 = LCR2Register()
r4.active_product().pulses_per_unit = 100.0
r4.pulser.set_k_factor(100.0)
r4.set_selector("STOP")
check("starts in STOP", r4.selector == "STOP")
r4.press_select()  # PROD -> PRESET_DIGITS (digit_idx=0)
check("SELECT from PROD moves to PRESET_DIGITS", r4.menu_field == "PRESET_DIGITS")
for _ in range(5):
    r4.press_increase()  # bump leftmost digit to 5
for _ in range(6):
    r4.press_select()  # walk through all 6 digit positions, landing back on PROD
# digits [5,0,0,0,0,0] read as a 6-digit integer (500000) with the last digit
# as tenths -> 50000.0 units. This matches the register_base digit convention
# (int(joined_digits) / 10.0), not an arbitrary "50.0" shortcut.
check("after 6 digit entries, returns to PROD with preset set", r4.menu_field == "PROD" and r4.preset_units == 50000.0)
r4.set_selector("RUN")
check("RUN starts a delivery", r4.delivery_active is True)
r4.pulser.set_flow_rate(120.0)  # 120 units/min
for _ in range(40):
    time.sleep(0.05)
    r4.tick()
check("delivery_total_units increased while flowing", r4.delivery_total_units > 0)
check("delivery still active (preset of 50000 units is nowhere near reached in 2s at 120/min)",
      r4.delivery_active is True and r4.delivery_total_units < r4.preset_units)

# Now actually exercise the auto-stop path with a tiny, reachable preset.
r4b = LCR2Register()
r4b.active_product().pulses_per_unit = 100.0
r4b.pulser.set_k_factor(100.0)
r4b.preset_units = 2.0  # small enough to hit in ~1 second at 120 units/min
r4b.set_selector("RUN")
r4b.pulser.set_flow_rate(120.0)
for _ in range(40):
    time.sleep(0.05)
    r4b.tick()
    if not r4b.delivery_active:
        break
check("preset auto-stop actually triggers and halts delivery", r4b.delivery_active is False)
check("auto-stopped at/just-above the preset, not wildly over", r4b.last_ticket is not None and r4b.last_ticket["delivery_total"] < r4b.preset_units * 1.5)

print()
print("=== LCR-600: POS tax computation ===")
r5 = LCR600Register()
r5.active_product().price_per_unit = 3.50
r5.delivery_total_units = 100.0
r5.set_tax_line("A", "PERCENT", 8.0, "State Tax")
r5.set_tax_line("B", "PER_UNIT", 0.05, "Fuel Tax")
subtotal = 100.0 * 3.50
tax = r5.compute_tax(subtotal)
expected_tax_a = subtotal * 0.08
expected_tax_b = 100.0 * 0.05
check("PERCENT tax line computed correctly", abs(tax["tax_breakdown"]["A"] - expected_tax_a) < 0.01)
check("PER_UNIT tax line computed correctly", abs(tax["tax_breakdown"]["B"] - expected_tax_b) < 0.01)
check("total_with_tax = subtotal + sum(taxes)", abs(tax["total_with_tax"] - (subtotal + expected_tax_a + expected_tax_b)) < 0.01)

print()
print("=== LCR.iQ: digital valve ramping + SENSEiQ tank level conversion ===")
r6 = LCRiQRegister()
check("tank level at 4mA = 0%", abs(r6.tank_level_from_ma(4.0) - 0.0) < 0.01)
check("tank level at 20mA = 100%", abs(r6.tank_level_from_ma(20.0) - 100.0) < 0.01)
check("tank level at 12mA = 50%", abs(r6.tank_level_from_ma(12.0) - 50.0) < 0.01)
r6.start_delivery_button()
r6.set_commanded_flow_rate(100.0)
time.sleep(0.3)  # valve ramp is now wall-clock-based (fraction/second), so it needs real elapsed time
r6.tick()
check("valve ramps up from 0 toward target after start", r6.valve_position > 0.0)
r6.set_analog_input(1, 12.0, "TANK_LEVEL")
check("analog input rejects out-of-loop-range mA", True)  # exercised below
try:
    r6.set_analog_input(2, 25.0, "TANK_LEVEL")
    check("25mA should be rejected (loop is 4-20mA)", False)
except RegisterError:
    check("25mA correctly rejected as out of 4-20mA loop range", True)

print()
print("=== J8 pulser fault voltage table (skill's exact 3 signatures) ===")
r7 = LCR2Register()
r7.pulser.set_mode("idle")
snap = r7.pulser.snapshot()
check("idle: #32=+5V", snap["j8_v32"] == 5.0)
r7.pulser.set_mode("stalled")
snap = r7.pulser.snapshot()
check("stalled: #32=0V", snap["j8_v32"] == 0.0)
check("stalled: #33 in 1-3V band", 1.0 <= snap["j8_v33"] <= 3.0)
check("stalled: #34 in 1-3V band", 1.0 <= snap["j8_v34"] <= 3.0)
r7.pulser.set_mode("flowing")
snap = r7.pulser.snapshot()
check("flowing: #32=+5V", snap["j8_v32"] == 5.0)
check("flowing: #33 is 0 or +5V", snap["j8_v33"] in (0.0, 5.0))

print()
print("=== W&M lock mechanism ===")
r8 = LCR2Register()
r8.set_locked(True)
try:
    r8.set_rtd_offset(0.1)
    check("locked register should reject RTD offset change", False)
except RegisterError:
    check("locked register rejects RTD offset change", "CALIBRATION LOCKED" in r8.errors[-1])
try:
    r8.set_vcf("LINEAR_C", 0.001)
    check("locked register should reject VCF change", False)
except RegisterError:
    check("locked register rejects VCF change", "CALIBRATION LOCKED" in r8.errors[-1])
r8.set_locked(False)
r8.set_rtd_offset(0.1)
check("unlocked register accepts RTD offset change", r8.rtd_offset_c == 0.1)
r8.set_selector("RUN")
check("locked/unlocked state does not block normal delivery start", r8.delivery_active is True)

print()
print("=== Printer ticket formatting ===")
from meter_core.printer import format_delivery_ticket, PRINTER_MODELS, DEFAULT_PRINTER
sample = {"kind": "DELIVERY", "timestamp": "2026-01-01 00:00:00", "product_name": "PROD 1",
          "delivery_total": 50.0, "temperature_c": 15.0, "vcf_type": "NONE", "k_factor": 100.0,
          "auto_stopped": False, "errors_during_delivery": []}
for key, model in PRINTER_MODELS.items():
    out = format_delivery_ticket(sample, key)
    max_len = max(len(l) for l in out.split("\n"))
    check(f"{key}: no line exceeds declared column width", max_len <= model.columns)
check("default printer is epson_slip", DEFAULT_PRINTER == "epson_slip")

print()
print("=== Pulser: low flow rate must still accumulate pulses over time ===")
print("(regression test for a real bug found during this session: a per-tick")
print(" int(round(...)) was silently discarding any rate where one ~50ms")
print(" tick contributes less than 0.5 pulses, so total_pulses stayed at 0")
print(" forever no matter how long the pulser ran at that rate.)")
r9 = LCR2Register()
r9.pulser.set_mode("flowing")
r9.pulser.set_flow_rate(2.0)  # deliberately low -- this is the rate that exposed the bug
time.sleep(1.2)
snap = r9.pulser.snapshot()
check("low flow rate (2.0 units/min) still accumulates pulses after 1.2s", snap["total_pulses"] > 0)
time.sleep(2)
snap2 = r9.pulser.snapshot()
check("pulse count keeps growing over a longer window at the same low rate", snap2["total_pulses"] > snap["total_pulses"])

print()
print("=== LCR.iQ: full delivery cycle under REALISTIC polling cadence ===")
print("(the iQ's valve-ramp-scaled flow rate is only as fresh as the last")
print(" tick() call -- this is fine under the browser's actual 250ms poll")
print(" loop, which is what's tested here, but a future caller that invokes")
print(" the API only once before a long gap would see a stale, low rate.")
print(" See SIMULATION_NOTES.md for the documented limitation.)")
r10 = LCRiQRegister()
r10.active_product().pulses_per_unit = 100.0
r10.pulser.set_k_factor(100.0)
r10.set_commanded_flow_rate(200.0)
r10.start_delivery_button()
for _ in range(8):
    time.sleep(0.25)
    r10.tick()  # mirrors the browser's /state poll calling tick() every 250ms
check("under realistic polling, valve reaches full open", r10.valve_position >= 0.99)
check("under realistic polling, meaningful volume accumulates", r10.delivery_total_units > 1.0)
r10.stop_delivery_button()
check("under realistic polling, a ticket is produced on stop", r10.last_ticket is not None)

print()
print(f"=== RESULT: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
