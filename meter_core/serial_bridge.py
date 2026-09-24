"""
serial_bridge.py — RS-232 / RS-485 → USB serial bridge for the LCR simulator.

Two operating modes, switchable at runtime via /api/serial/mode:

  ACT_AS_METER (default when serial is enabled)
    The simulator listens on the configured COM port and responds to incoming
    bytes as if it were a real LCR meter. PandaBox (or any other host) sends
    commands as it would to real hardware; the simulator reads its own register
    state and replies. This is the mode for testing PandaBox's integration
    code without needing real hardware on the bench.

  PASSIVE_MONITOR
    The simulator sniffs bytes from the wire without inserting its own
    responses, forwarding the raw traffic to /api/serial/monitor so the browser
    can display it. Use this when a real LCR is on the wire and you want to
    watch the actual traffic without interfering. Each sniffed frame is also
    decoded as much as possible given the protocol knowledge available.

─────────────────────────────────────────────────────────────────────
IMPORTANT: Why the protocol layer is stubbed
─────────────────────────────────────────────────────────────────────
The Liquid Controls LCP (Liquid Controls Protocol) wire format — message
framing, command IDs, checksum algorithm, request/response structure — is
documented in LC's proprietary "LCP Host Interface Document", which is
only available to registered LC software partners. It is NOT in any of the
27 source PDFs in this project.

What the manuals DO document (and what is implemented here):
  • Physical layer: RS-232 (EIA-232E) or RS-485 (SAE J1708), connector pinout
    per wiring schematics EM100-10WS (LCR-II), EM150-10WS (LCR-600),
    LCR.iQ-Wiring-Rev-J-final (LCR.iQ)
  • Baud rates: 9600 / 19200 / 115200 (configurable; default 115200 per the
    LCR.iQ product manual section on I/O Setup)
  • LCP node addressing: configurable 1–250, used for RS-485 multi-drop
  • Data types used inside LCP messages: UINT1 / UINT2 / UINT4 / SINT1 /
    SINT2 / BCD / ASCII with their byte widths (from the skill file's LCP
    data type table, the one part of the LCP specification that appears in
    the manual)

What is stubbed pending the real spec:
  • parse_frame(raw_bytes) → the actual framing/checksum parser. Returns None
    until you fill in the real framing constants.
  • build_response(parsed_cmd, register_state) → builds the bytes the real
    unit would send back. Returns an empty bytes object until you fill this in.

To fill these in once you have the real LCP Host Interface Document:
  1. Replace FRAME_SOF / FRAME_EOF / checksum constants with the real values.
  2. Implement parse_frame() to match the actual framing format.
  3. Implement build_response() to return the correct bytes for each command ID.
  The rest of the bridge (port management, threading, mode switching, monitor
  queue) will work immediately without any further changes.

─────────────────────────────────────────────────────────────────────
Passive monitor / sniff mode
─────────────────────────────────────────────────────────────────────
When mode = PASSIVE_MONITOR, the bridge logs each byte sequence that looks
like a potential frame to a bounded queue (max 200 entries). The /api/serial/
monitor endpoint returns all queued entries as JSON so the browser can show
a live traffic view. The LCP node address is used to guess which bytes are
addressed to this node vs. another node on the same RS-485 bus.
"""
from __future__ import annotations
import threading
import time
import queue
import struct
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────── Constants (fill in when real spec is available) ───────────── #

FRAME_SOF   = None   # Start-of-frame byte(s). Example: 0x02 for STX-framed protocols.
FRAME_EOF   = None   # End-of-frame byte(s). Example: 0x03 for STX/ETX.
BAUD_DEFAULT = 115200  # Confirmed from LCR.iQ product manual I/O Setup section.
PARITY_DEFAULT = "N"   # 8N1 is standard for RS-232/485 in industrial meters.
STOPBITS_DEFAULT = 1
BYTESIZE_DEFAULT = 8

# LCP data type widths from the manual's data-type table (the one confirmed
# piece of the protocol spec present in the source documents).
LCP_TYPE_WIDTHS = {
    "UINT1": 1, "UINT2": 2, "UINT4": 4,
    "SINT1": 1, "SINT2": 2,
    "BCD2": 2, "BCD4": 4,
    "ASCII8": 8, "ASCII16": 16, "ASCII20": 20,
}

MODE_ACT_AS_METER   = "ACT_AS_METER"
MODE_PASSIVE_MONITOR = "PASSIVE_MONITOR"


@dataclass
class SerialConfig:
    port: Optional[str] = None      # e.g. "COM3" on Windows, "/dev/ttyUSB0" on Linux
    baud: int = BAUD_DEFAULT
    parity: str = PARITY_DEFAULT
    stopbits: int = STOPBITS_DEFAULT
    bytesize: int = BYTESIZE_DEFAULT
    mode: str = MODE_ACT_AS_METER
    lcp_node_address: int = 1       # per manual: configurable 1–250


@dataclass
class MonitorEntry:
    ts: float = field(default_factory=time.time)
    direction: str = "rx"           # "rx" | "tx"
    raw_hex: str = ""
    decoded: str = ""
    is_addressed_to_us: bool = False


class SerialBridge:
    """
    Manages the actual serial port (if pyserial is installed and a port is
    configured) and exposes state/control via a simple dict-based API that
    the Flask layer can call directly.

    The bridge runs a background reader thread when started. The main thread
    (Flask) can call send_bytes() to transmit, or read from monitor_queue.
    """

    def __init__(self):
        self.config = SerialConfig()
        self._serial = None              # pyserial Serial object, or None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self.monitor_queue: queue.Queue[MonitorEntry] = queue.Queue(maxsize=200)
        self._rx_buffer = bytearray()
        self._pyserial_available = self._check_pyserial()

    # ─────────────────────────── pyserial availability ─────────────────── #

    def _check_pyserial(self) -> bool:
        try:
            import serial  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def list_ports() -> list[dict]:
        """Returns the available COM/tty ports this machine currently has,
        regardless of whether the bridge is running. Useful for the UI's
        port dropdown."""
        if not SerialBridge._check_pyserial_static():
            return []
        try:
            from serial.tools.list_ports import comports
            return [
                {"device": p.device, "description": p.description or p.device,
                 "hwid": p.hwid or ""}
                for p in comports()
            ]
        except Exception as e:
            log.warning("list_ports failed: %s", e)
            return []

    @staticmethod
    def _check_pyserial_static() -> bool:
        try:
            import serial  # noqa: F401
            return True
        except ImportError:
            return False

    # ─────────────────────────── start / stop ───────────────────────────── #

    def start(self, register_getter) -> dict:
        """
        Open the configured port and start the reader thread.
        register_getter is a callable () → RegisterBase that the bridge calls
        to read current meter state when building ACT_AS_METER responses.
        Returns {"ok": True} or {"ok": False, "error": "..."}.
        """
        if not self._pyserial_available:
            return {"ok": False, "error":
                "pyserial is not installed. Run: pip install pyserial --break-system-packages"}

        if not self.config.port:
            return {"ok": False, "error":
                "No COM port configured. Use /api/serial/config to set one."}

        with self._lock:
            if self._running:
                return {"ok": True, "status": "already running"}
            try:
                import serial
                self._serial = serial.Serial(
                    port=self.config.port,
                    baudrate=self.config.baud,
                    parity=self.config.parity,
                    stopbits=self.config.stopbits,
                    bytesize=self.config.bytesize,
                    timeout=0.05,
                )
            except Exception as e:
                return {"ok": False, "error": f"Failed to open {self.config.port}: {e}"}

            self._running = True
            self._register_getter = register_getter
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="serial-bridge")
            self._thread.start()
        log.info("Serial bridge started on %s at %d baud", self.config.port, self.config.baud)
        return {"ok": True}

    def stop(self):
        with self._lock:
            self._running = False
            if self._serial:
                try:
                    self._serial.close()
                except Exception:
                    pass
                self._serial = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    # ─────────────────────────── reader thread ──────────────────────────── #

    def _run(self):
        while self._running:
            try:
                chunk = self._serial.read(64)
            except Exception as e:
                log.error("Serial read error: %s", e)
                self._running = False
                break

            if not chunk:
                continue

            self._rx_buffer.extend(chunk)
            self._process_buffer()

    def _process_buffer(self):
        """
        Attempt to parse complete frames out of _rx_buffer.

        ─── STUB ──────────────────────────────────────────────────────────
        FRAME_SOF and FRAME_EOF are None until you fill in the real LCP
        framing constants. Until then, this method logs the raw bytes to
        the monitor queue (useful for passive sniffing with a protocol
        analyzer) but does not attempt to decode or respond.
        ───────────────────────────────────────────────────────────────────
        """
        if FRAME_SOF is None or FRAME_EOF is None:
            # Protocol layer not yet filled in: treat entire buffer as one
            # raw monitor entry, then clear. The UI will show raw hex.
            if self._rx_buffer:
                raw = bytes(self._rx_buffer)
                self._rx_buffer.clear()
                entry = MonitorEntry(
                    direction="rx",
                    raw_hex=raw.hex(" ").upper(),
                    decoded="[LCP framing not yet configured — see serial_bridge.py comments]",
                    is_addressed_to_us=False,
                )
                self._enqueue(entry)
            return

        # ── Real framing logic goes here once FRAME_SOF/EOF are known ────
        # Pattern: scan _rx_buffer for FRAME_SOF, collect until FRAME_EOF,
        # validate checksum, parse command, dispatch.
        #
        # while len(self._rx_buffer) >= MIN_FRAME_LEN:
        #     sof_idx = self._rx_buffer.find(bytes([FRAME_SOF]))
        #     if sof_idx == -1:
        #         self._rx_buffer.clear(); break
        #     if sof_idx > 0:
        #         self._rx_buffer = self._rx_buffer[sof_idx:]
        #     eof_idx = self._rx_buffer.find(bytes([FRAME_EOF]), 1)
        #     if eof_idx == -1:
        #         break  # incomplete frame, wait for more bytes
        #     frame = bytes(self._rx_buffer[:eof_idx + 1])
        #     self._rx_buffer = self._rx_buffer[eof_idx + 1:]
        #     self._dispatch_frame(frame)
        pass

    def _dispatch_frame(self, frame: bytes):
        """
        Dispatches a validated frame:
        - In PASSIVE_MONITOR mode: log it and do nothing else.
        - In ACT_AS_METER mode: parse it, call build_response(), and send.
        """
        parsed = parse_frame(frame)
        if parsed is None:
            self._enqueue(MonitorEntry(
                direction="rx", raw_hex=frame.hex(" ").upper(),
                decoded="[parse failed — bad checksum or unknown command]"))
            return

        is_ours = parsed.get("node_address") in (self.config.lcp_node_address, 0xFF)
        self._enqueue(MonitorEntry(
            direction="rx", raw_hex=frame.hex(" ").upper(),
            decoded=f"cmd={parsed.get('command_id')} node={parsed.get('node_address')}",
            is_addressed_to_us=is_ours))

        if self.config.mode == MODE_PASSIVE_MONITOR or not is_ours:
            return

        # ACT_AS_METER: build and send reply
        reg = self._register_getter()
        response = build_response(parsed, reg.to_dict())
        if response:
            self.send_bytes(response)

    def send_bytes(self, data: bytes):
        """Transmit bytes and log them to the monitor queue as 'tx'."""
        with self._lock:
            if not self._serial or not self._running:
                return
            try:
                self._serial.write(data)
                self._enqueue(MonitorEntry(
                    direction="tx", raw_hex=data.hex(" ").upper()))
            except Exception as e:
                log.error("Serial write error: %s", e)

    def _enqueue(self, entry: MonitorEntry):
        try:
            self.monitor_queue.put_nowait(entry)
        except queue.Full:
            # Drop oldest entry to make room
            try:
                self.monitor_queue.get_nowait()
                self.monitor_queue.put_nowait(entry)
            except Exception:
                pass

    # ─────────────────────────── state snapshot ─────────────────────────── #

    def to_dict(self) -> dict:
        return {
            "running": self._running,
            "pyserial_available": self._pyserial_available,
            "config": {
                "port": self.config.port,
                "baud": self.config.baud,
                "parity": self.config.parity,
                "mode": self.config.mode,
                "lcp_node_address": self.config.lcp_node_address,
            },
            "available_ports": self.list_ports(),
            "monitor_pending": self.monitor_queue.qsize(),
            "protocol_stub": FRAME_SOF is None,
        }

    def drain_monitor(self) -> list[dict]:
        entries = []
        while True:
            try:
                e = self.monitor_queue.get_nowait()
                entries.append({
                    "ts": e.ts, "direction": e.direction,
                    "raw_hex": e.raw_hex, "decoded": e.decoded,
                    "is_addressed_to_us": e.is_addressed_to_us,
                })
            except queue.Empty:
                break
        return entries


# ─────────────────────────────────────────────────────────────────────────── #
#  Protocol stub functions — fill these in when the LCP Host Interface        #
#  Document is available. Everything else in this file will work immediately. #
# ─────────────────────────────────────────────────────────────────────────── #

def parse_frame(raw: bytes) -> Optional[dict]:
    """
    Parse a raw LCP frame into a dict with at least:
        {"command_id": int, "node_address": int, "payload": bytes}
    Return None if the frame is invalid (bad checksum, unknown structure, etc.)

    STUB — returns None until you fill in the real framing format.
    """
    return None


def build_response(parsed_cmd: dict, register_state: dict) -> bytes:
    """
    Build the response bytes the real LCR meter would send for the given
    parsed command, given the current register state.

    register_state is the full to_dict() output from RegisterBase (or its
    subclass), giving you access to all simulated meter values: delivery total,
    k-factor, flow rate, errors, etc.

    STUB — returns empty bytes until you fill in the real response format.
    Known data type widths (from the manual's LCP data type table, which IS
    documented) are available in LCP_TYPE_WIDTHS above for reference when you
    do fill this in.
    """
    return b""
