"""
app.py — Flask backend for the LCR meter family simulator. Thin layer: owns
one live Register instance per product key (so switching products in the UI
doesn't lose the other two products' state), exposes a small REST API the
frontend polls/calls, and serves the per-product HTML pages.

Run with: python3 app.py
Then open http://localhost:5000/
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, render_template, send_from_directory
from meter_core import REGISTER_CLASSES, RegisterError
from meter_core.serial_bridge import SerialBridge

app = Flask(__name__)

# One serial bridge instance shared across the process — it manages its own
# background reader thread and exposes state/control via /api/serial/* routes.
_serial_bridge = SerialBridge()

# One live instance per product, created lazily and kept for the lifetime of
# the process — this is what makes "switch meter type" not reset the other
# two meters' calibration/totalizers when you switch back.
_registers: dict[str, object] = {}


def get_register(product_key: str):
    if product_key not in REGISTER_CLASSES:
        raise ValueError(f"Unknown product key: {product_key}")
    if product_key not in _registers:
        _registers[product_key] = REGISTER_CLASSES[product_key]()
    return _registers[product_key]


def error_response(exc: Exception, status: int = 400):
    return jsonify({"ok": False, "error": str(exc)}), status


# --------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/meter/<product_key>")
def meter_page(product_key):
    if product_key not in REGISTER_CLASSES:
        return "Unknown product", 404
    return render_template(f"{product_key}.html", product_key=product_key)


# --------------------------------------------------------------------- #
# Shared state/control API — works the same way for all three products;
# product-specific actions are namespaced under /api/<key>/lcr2|lcr600|lcriq/...
# --------------------------------------------------------------------- #
@app.route("/api/<product_key>/state")
def api_state(product_key):
    try:
        reg = get_register(product_key)
        reg.tick()
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e, 500)


@app.route("/api/<product_key>/reset", methods=["POST"])
def api_reset(product_key):
    if product_key not in REGISTER_CLASSES:
        return error_response(ValueError("Unknown product key"), 404)
    old = _registers.pop(product_key, None)
    if old is not None:
        old.pulser.stop()
    reg = get_register(product_key)
    return jsonify({"ok": True, "state": reg.to_dict()})


@app.route("/api/<product_key>/pulser", methods=["POST"])
def api_pulser(product_key):
    """Body: {mode?: idle|flowing|stalled|vibration, flow_rate?: number}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        if "mode" in body:
            reg.pulser.set_mode(body["mode"])
        if "flow_rate" in body:
            if hasattr(reg, "set_commanded_flow_rate"):
                reg.set_commanded_flow_rate(float(body["flow_rate"]))
            else:
                reg.pulser.set_flow_rate(float(body["flow_rate"]))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/environment", methods=["POST"])
def api_environment(product_key):
    """Body: {true_temp_c?, rtd_probe_connected?, power_voltage?}
    Lets the UI's 'environment / fault injection' panel drive scenarios
    like a disconnected RTD probe or a sagging supply voltage."""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        if "true_temp_c" in body:
            reg.true_temp_c = float(body["true_temp_c"])
        if "rtd_probe_connected" in body:
            reg.rtd_probe_connected = bool(body["rtd_probe_connected"])
        if "power_voltage" in body:
            reg.power_voltage = float(body["power_voltage"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/clear_errors", methods=["POST"])
def api_clear_errors(product_key):
    try:
        reg = get_register(product_key)
        reg.clear_errors()
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_locked", methods=["POST"])
def api_set_locked(product_key):
    """Body: {locked: bool} — the W&M tamper-seal toggle."""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.set_locked(bool(body.get("locked")))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_view_mode", methods=["POST"])
def api_set_view_mode(product_key):
    """Body: {mode: 'original'|'custom'}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.set_view_mode(body["mode"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_printer", methods=["POST"])
def api_set_printer(product_key):
    """Body: {printer_key: string}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.set_printer(body["printer_key"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_ticket_header", methods=["POST"])
def api_set_ticket_header(product_key):
    """Body: {lines: [string, ...]}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.set_ticket_header(body.get("lines", []))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/select_product", methods=["POST"])
def api_select_product(product_key):
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.select_product(int(body["index"]))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_preset", methods=["POST"])
def api_set_preset(product_key):
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        val = body.get("preset_units")
        reg.preset_units = None if val in (None, "", "none") else float(val)
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/recalibrate", methods=["POST"])
def api_recalibrate(product_key):
    """Body: {prover_qty: number} — runs the actual k-Factor backout math."""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        result = reg.recalibrate_from_prover(float(body["prover_qty"]))
        return jsonify({"ok": True, "result": result, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_vcf", methods=["POST"])
def api_set_vcf(product_key):
    """Body: {vcf_type: string, param: number}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.set_vcf(body["vcf_type"], float(body.get("param", 0)))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/add_cal_point", methods=["POST"])
def api_add_cal_point(product_key):
    """Body: {flow_rate: number, pct_error: number}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.add_cal_point(float(body["flow_rate"]), float(body["pct_error"]))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/<product_key>/set_rtd_offset", methods=["POST"])
def api_set_rtd_offset(product_key):
    """Body: {offset_c: number}"""
    try:
        reg = get_register(product_key)
        body = request.get_json(force=True) or {}
        reg.set_rtd_offset(float(body["offset_c"]))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


# --------------------------------------------------------------------- #
# LCR-II specific: selector switch + SELECT/INCREASE buttons
# --------------------------------------------------------------------- #
@app.route("/api/lcr2/selector", methods=["POST"])
def lcr2_selector():
    try:
        reg = get_register("lcr2")
        body = request.get_json(force=True) or {}
        reg.set_selector(body["position"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcr2/button", methods=["POST"])
def lcr2_button():
    try:
        reg = get_register("lcr2")
        body = request.get_json(force=True) or {}
        which = body["button"]
        if which == "select":
            reg.press_select()
        elif which == "increase":
            reg.press_increase()
        else:
            raise ValueError("button must be 'select' or 'increase'")
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


# --------------------------------------------------------------------- #
# LCR-600 specific: selector + screen nav + POS
# --------------------------------------------------------------------- #
@app.route("/api/lcr600/selector", methods=["POST"])
def lcr600_selector():
    try:
        reg = get_register("lcr600")
        body = request.get_json(force=True) or {}
        reg.set_selector(body["position"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcr600/screen", methods=["POST"])
def lcr600_screen():
    try:
        reg = get_register("lcr600")
        body = request.get_json(force=True) or {}
        reg.navigate_screen(body["screen"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcr600/set_price", methods=["POST"])
def lcr600_set_price():
    try:
        reg = get_register("lcr600")
        body = request.get_json(force=True) or {}
        reg.active_product().price_per_unit = float(body["price_per_unit"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/lcr600/set_tax_line", methods=["POST"])
def lcr600_set_tax_line():
    """Body: {letter, tax_type, value, header_text?}"""
    try:
        reg = get_register("lcr600")
        body = request.get_json(force=True) or {}
        reg.set_tax_line(body["letter"], body["tax_type"], float(body.get("value", 0)), body.get("header_text", ""))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcr600/build_pos_ticket", methods=["POST"])
def lcr600_build_pos_ticket():
    try:
        reg = get_register("lcr600")
        ticket = reg.build_pos_ticket()
        return jsonify({"ok": True, "ticket": ticket, "state": reg.to_dict()})
    except Exception as e:
        return error_response(e)


# --------------------------------------------------------------------- #
# LCR.iQ specific: soft start/stop, wireless, SENSEiQ
# --------------------------------------------------------------------- #
@app.route("/api/lcriq/run", methods=["POST"])
def lcriq_run():
    try:
        reg = get_register("lcriq")
        body = request.get_json(force=True) or {}
        if body.get("running"):
            reg.start_delivery_button()
        else:
            reg.stop_delivery_button()
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcriq/navigate", methods=["POST"])
def lcriq_navigate():
    try:
        reg = get_register("lcriq")
        body = request.get_json(force=True) or {}
        reg.navigate(body["screen"])
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcriq/reprint_last_ticket", methods=["POST"])
def lcriq_reprint_last_ticket():
    try:
        reg = get_register("lcriq")
        reg.reprint_last_ticket()
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcriq/set_weight_preset", methods=["POST"])
def lcriq_set_weight_preset():
    """Body: {value: number|null}"""
    try:
        reg = get_register("lcriq")
        body = request.get_json(force=True) or {}
        val = body.get("value")
        reg.set_weight_preset(None if val in (None, "", "none") else float(val))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcriq/bluetooth", methods=["POST"])
def lcriq_bluetooth():
    """Body: {action: 'toggle'|'scan'|'connect'|'disconnect', on?, device_name?, is_printer?}"""
    try:
        reg = get_register("lcriq")
        body = request.get_json(force=True) or {}
        action = body["action"]
        if action == "toggle":
            reg.bluetooth_toggle(bool(body.get("on")))
        elif action == "scan":
            reg.bluetooth_scan()
        elif action == "connect":
            reg.bluetooth_connect(body.get("device_name", "Unknown Device"), bool(body.get("is_printer")))
        elif action == "disconnect":
            reg.bluetooth_disconnect()
        else:
            raise ValueError("unknown bluetooth action")
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcriq/wifi", methods=["POST"])
def lcriq_wifi():
    """Body: {action: 'toggle'|'connect', on?, ssid?}"""
    try:
        reg = get_register("lcriq")
        body = request.get_json(force=True) or {}
        action = body["action"]
        if action == "toggle":
            reg.wifi_toggle(bool(body.get("on")))
        elif action == "connect":
            reg.wifi_connect(body.get("ssid", "Unknown Network"))
        else:
            raise ValueError("unknown wifi action")
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)


@app.route("/api/lcriq/analog_input", methods=["POST"])
def lcriq_analog_input():
    """Body: {channel: int, ma_value: number, use_case: string}"""
    try:
        reg = get_register("lcriq")
        body = request.get_json(force=True) or {}
        reg.set_analog_input(int(body["channel"]), float(body["ma_value"]), body.get("use_case", "UNUSED"))
        if body.get("use_case") == "TANK_LEVEL" and "tank_number" in body:
            reg.set_tank_ma(int(body["tank_number"]), float(body["ma_value"]))
        return jsonify({"ok": True, "state": reg.to_dict()})
    except RegisterError as e:
        return error_response(e)
    except Exception as e:
        return error_response(e)




# ─────────────────────────────────────────────────────────────────────────── #
#  RS-232 / RS-485 → USB Serial Bridge                                        #
#  Full infrastructure ready; protocol layer stubbed pending the LCP Host     #
#  Interface Document — see meter_core/serial_bridge.py for details.          #
# ─────────────────────────────────────────────────────────────────────────── #

@app.route("/serial")
def serial_monitor_page():
    """Dedicated full-page serial monitor."""
    return render_template("serial_monitor.html")


@app.route("/api/serial/status", methods=["GET"])
def serial_status():
    return jsonify({"ok": True, "serial": _serial_bridge.to_dict()})


@app.route("/api/serial/config", methods=["POST"])
def serial_config():
    """Body: {port?, baud?, node?, product_key?}"""
    try:
        body = request.get_json(force=True) or {}
        kw = {}
        if "port"             in body: kw["port"]         = body["port"]
        if "baud"             in body: kw["baud"]         = int(body["baud"])
        if "node"             in body: kw["node"]         = int(body["node"])
        if "lcp_node_address" in body: kw["node"]         = int(body["lcp_node_address"])  # compat alias
        if "product_key"      in body: kw["product_key"]  = body["product_key"]
        _serial_bridge.configure(**kw)
        return jsonify({"ok": True, "serial": _serial_bridge.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/serial/start", methods=["POST"])
def serial_start():
    """Body: {product_key?} — which meter's state to serve as the simulated meter."""
    try:
        body = request.get_json(force=True) or {}
        product_key = body.get("product_key", _serial_bridge.cfg.product_key)

        def getter():
            return get_register(product_key)

        _serial_bridge.configure(product_key=product_key)
        _serial_bridge.start(getter)
        return jsonify({"ok": True, "serial": _serial_bridge.to_dict()})
    except Exception as e:
        return error_response(e)


@app.route("/api/serial/stop", methods=["POST"])
def serial_stop():
    _serial_bridge.stop()
    return jsonify({"ok": True, "serial": _serial_bridge.to_dict()})


@app.route("/api/serial/monitor", methods=["GET"])
def serial_monitor():
    """Returns queued monitor entries (raw + decoded frames) and drains them."""
    entries = _serial_bridge.drain_monitor()
    return jsonify({"ok": True, "entries": entries, "serial": _serial_bridge.to_dict()})


@app.route("/api/serial/send", methods=["POST"])
def serial_send():
    """Body: {hex: '0A 1B ...'} — send arbitrary hex bytes (for manual testing)."""
    try:
        body = request.get_json(force=True) or {}
        raw = bytes.fromhex(body.get("hex", "").replace(" ", "").replace(":", ""))
        _serial_bridge.send_raw(raw)
        return jsonify({"ok": True})
    except Exception as e:
        return error_response(e)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
