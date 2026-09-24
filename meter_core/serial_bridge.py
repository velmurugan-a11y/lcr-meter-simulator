"""
serial_bridge.py — RS-232 / CAN-to-RS232 bridge for the LCR simulator
═══════════════════════════════════════════════════════════════════════════════

CONFIRMED FROM SOURCE DOCUMENTS (all 23 PDFs):
═══════════════════════════════════════════════

LCR-II (EM100-10, EM100-11):
    Port:       J3 terminal block (NOT J1 which is the printer)
    Protocol:   VT100 terminal emulation — TEXT BASED, NOT BINARY LCP
    Baud:       9600
    Format:     8N1 (8 data, No parity, 1 stop bit)
    Flow ctrl:  NONE
    Pins:       J3-48=TXD(orange), J3-49=RXD(gray), J3-47=RTS, J3-50=CTS,
                J3-51=GND(white)
    Jumper:     J10 must be in RS-232 position on the CPU board
    Source:     EM100-11 Appendix B "VT100 Compatible Terminal" section

LCR-600 (EM150-11):
    Port:       J3 terminal block
    Protocol:   LCP (Liquid Controls Protocol) — binary, host-slave model
    Baud:       9600 (confirmed from Node Address setup screen)
    Format:     8N1
    Flow ctrl:  None documented
    Node Addr:  Configurable 1-250 in System Setup → LCP Node Address field
    Host:       DMS i1000, EZCommand, or PandaBox

LCR.iQ (LCR.iQ-MASTERLOAD.iQ-Product-Manual-03SEP2019):
    PRIMARY:    CAN BUS (J8) — "HIGH SPEED CAN BUS"
                CAN-H = J8 pin 46
                CAN-L = J8 pin 45
                EARTH/Shield = J8 pin 43
                Standard: SAE J1939 (truck chassis standard, 250 kbps)
                "Consult the applicable Chassis Builder's Guide from the
                truck chassis manufacturer" — used by PandaBox
    SECONDARY:  RS-232 / RS-485 on J6 (ports COM0-COM4)
                Baud: 9600/19200/115200 configurable
                Service options: LCP, Printer, I/O Boards (115200 required)
    CAN→RS232:  Your USB-to-RS232 FT232RNL converter connects to the CAN
                bus via a CAN-to-RS232 bridge device. The bridge translates
                SAE J1939 CAN frames to a serial byte stream. The exact
                framing of that byte stream depends on which bridge device
                is used (e.g., Kvaser, Peak, Ixxat, or a custom LC device).
                See CAN_BRIDGE_FORMAT below.

CAN-TO-RS232 BRIDGE FRAME FORMAT:
═══════════════════════════════════
Most commercial CAN-to-RS232 bridges use one of these ASCII/binary formats.
The correct one depends on the specific bridge device. The simulator tries
all common formats and reports which one it sees:

  LAWICEL (slcan): ASCII text, most common open-source bridges
    t<CAN_ID_3hex><DLC><DATA_hex>\r    (standard 11-bit CAN)
    T<CAN_ID_8hex><DLC><DATA_hex>\r    (extended 29-bit CAN / J1939)
    Example: T18FEF10008DEADBEEF01020304\r

  KVASER ASCII: Similar to slcan but with checksum
  Peak PCAN: Binary framing with length prefix
  Custom binary: Many industrial CAN gateways use proprietary formats

  SAE J1939 PGN structure (embedded in the 29-bit CAN ID):
    Bits 28-26: Priority (3 bits)
    Bit 25:     Reserved
    Bit 24:     Data Page
    Bits 23-16: PGN high byte (Parameter Group Number)
    Bits 15-8:  PGN low byte
    Bits 7-0:   Source Address
"""

from __future__ import annotations
import os
import re
import struct
import threading
import time
import queue
import logging
from dataclasses import dataclass, field
from typing import Optional, List

log = logging.getLogger(__name__)

# ── Confirmed serial parameters ───────────────────────────────────────────────
# LCR-II: 9600 (confirmed from EM100-11 Appendix B)
# LCR-600: 9600 (confirmed from LCP Node Address setup)
# LCR.iQ CAN bridge: typically 115200 for the serial side of CAN-RS232 bridges
BAUD_LCR2       = 9600
BAUD_LCR600     = 9600
BAUD_LCRIQ_CAN  = 115200   # serial side of the CAN-to-RS232 bridge
BAUD_LCRIQ_LCP  = 9600     # direct RS-232 LCP connection (no CAN bridge)

AUTO_BAUD_RATES  = [9600, 115200, 57600, 38400, 19200]
POLL_INTERVAL_MS = 500
PROBE_TIMEOUT_S  = 0.6

# ── LCR-II VT100 protocol constants ──────────────────────────────────────────
# The LCR-II communicates via VT100 terminal emulation (EM100-11 Appendix B).
# To interact with the register over RS-232:
#   - Connect at 9600 8N1, no flow control
#   - The register sends VT100 escape sequences to draw its screen
#   - You send keystrokes to navigate: Enter, arrow keys, etc.
#   - These are the VT100 codes the Lap Pad maps to (from EM100-11 Appendix B):
VT100_CTRL_L   = b'\x0c'   # Return to Top Level Menu
VT100_CTRL_D   = b'\x04'   # Move to Next Menu Item (i key on Lap Pad)
VT100_CTRL_U   = b'\x15'   # Move to Previous Menu Item (h key on Lap Pad)
VT100_ENTER    = b'\r'     # Enter / confirm selection
VT100_INCREASE = b'+'      # INCREASE button equivalent (scroll list forward)
VT100_SELECT   = b'\r'     # SELECT button = Enter
# Query the register's current display state:
VT100_STATUS_QUERY = VT100_CTRL_L + VT100_CTRL_D  # go to top, then step

# ── CAN frame format constants ────────────────────────────────────────────────
# LAWICEL/slcan is the most common open-source CAN-RS232 bridge protocol.
# T = extended (29-bit) CAN frame — used by SAE J1939
CAN_LAWICEL_EXTENDED_PREFIX = b'T'
CAN_LAWICEL_STANDARD_PREFIX = b't'
CAN_LAWICEL_TERMINATOR      = b'\r'

# SAE J1939 PGN for meter/register data (common PGNs for fuel delivery):
J1939_PGN_VEHICLE_FLUIDS    = 0xFEF1   # Engine fuel delivery pressure
J1939_PGN_FUEL_ECONOMY      = 0xFEF2
J1939_PGN_FUEL_CONSUMPTION  = 0xFEE9
J1939_PGN_ELECTRONIC_ENGINE = 0xF004

# LCP data type widths (confirmed from LCR.iQ product manual)
LCP_TYPE_WIDTHS = {
    "UINT1": 1, "UINT2": 2, "UINT4": 4,
    "SINT1": 1, "SINT2": 2,
    "BCD2": 2,  "BCD4": 4,
    "ASCII8": 8, "ASCII16": 16, "ASCII20": 20,
}

MODE_ACT_AS_METER    = "ACT_AS_METER"
MODE_PASSIVE_MONITOR = "PASSIVE_MONITOR"


def _ascii_safe(data: bytes) -> str:
    out = []
    for b in data:
        if 0x20 <= b <= 0x7E:
            out.append(chr(b))
        elif b == 0x0D: out.append('<CR>')
        elif b == 0x0A: out.append('<LF>')
        elif b == 0x1B: out.append('<ESC>')
        elif b == 0x0C: out.append('<FF>')
        elif b == 0x04: out.append('<CTRL-D>')
        elif b == 0x15: out.append('<CTRL-U>')
        elif b == 0x05: out.append('<ENQ>')
        elif b == 0x06: out.append('<ACK>')
        else: out.append(f'[{b:02X}]')
    return ''.join(out)


@dataclass
class MonitorEntry:
    ts:      float = field(default_factory=time.time)
    direction: str = 'rx'   # 'rx'|'tx'|'info'|'probe'|'error'|'can'
    raw_hex: str   = ''
    decoded: str   = ''
    is_addressed_to_us: bool = False
    byte_count: int = 0


@dataclass
class SerialConfig:
    port:             Optional[str] = None
    baud:             int  = BAUD_LCR2     # 9600 is the confirmed default
    parity:           str  = 'N'
    stopbits:         int  = 1
    bytesize:         int  = 8
    mode:             str  = MODE_PASSIVE_MONITOR  # start in monitor mode
    lcp_node_address: int  = 1
    poll_interval_ms: int  = POLL_INTERVAL_MS
    auto_detect:      bool = True
    product_key:      str  = 'lcriq'  # which meter we're simulating


class SerialBridge:

    def __init__(self):
        self.config     = SerialConfig()
        self._serial    = None
        self._thread: Optional[threading.Thread] = None
        self._running   = False
        self._lock      = threading.Lock()
        self.monitor_queue: queue.Queue[MonitorEntry] = queue.Queue(maxsize=1000)
        self._rx_buffer = bytearray()
        self._rx_total  = 0
        self._tx_total  = 0
        self._last_rx_ts: Optional[float] = None
        self._connected_port: Optional[str] = None
        self._connected_baud: Optional[int] = None
        self._detect_status = 'idle'
        self._detected_protocol = 'unknown'
        self._pyserial_ok = self._check_pyserial()
        self._register_getter = None

    # ── pyserial ──────────────────────────────────────────────────────────────

    def _check_pyserial(self) -> bool:
        try:
            import serial; return True
        except ImportError:
            return False

    @staticmethod
    def list_ports() -> List[dict]:
        try:
            from serial.tools.list_ports import comports
            result = []
            for p in comports():
                result.append({
                    'device':      p.device,
                    'description': p.description or p.device,
                    'hwid':        p.hwid or '',
                    'is_ftdi':     'FTDI' in (p.manufacturer or '') or
                                   'FT232' in (p.description or '') or
                                   '0403' in (p.hwid or ''),
                    'is_can':      'CAN' in (p.description or '').upper() or
                                   'KVASER' in (p.description or '').upper() or
                                   'PEAK'   in (p.description or '').upper(),
                })
            return result
        except Exception:
            return []

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self, register_getter) -> dict:
        if not self._pyserial_ok:
            return {'ok': False, 'error':
                'pyserial not installed. Run: pip install pyserial --break-system-packages'}
        with self._lock:
            if self._running:
                return {'ok': True, 'status': 'already running'}
            self._register_getter = register_getter
            self._running = True
        self._thread = threading.Thread(
            target=self._worker, daemon=True, name='serial-bridge')
        self._thread.start()
        return {'ok': True}

    def stop(self):
        self._running = False
        if self._serial:
            try: self._serial.close()
            except: pass
            self._serial = None
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._connected_port = None
        self._connected_baud = None
        self._detect_status  = 'idle'

    def send_bytes(self, data: bytes):
        with self._lock:
            if not self._serial or not self._running:
                return
            try:
                self._serial.write(data)
                self._tx_total += len(data)
                self._enqueue(MonitorEntry(
                    direction='tx',
                    raw_hex=data.hex(' ').upper(),
                    decoded=_ascii_safe(data),
                    byte_count=self._tx_total))
            except Exception as e:
                log.error('TX error: %s', e)
                self._enqueue(MonitorEntry(direction='error',
                    decoded=f'TX ERROR: {e}'))

    # ── Worker thread ─────────────────────────────────────────────────────────

    def _worker(self):
        if self.config.auto_detect:
            ok = self._auto_detect()
        else:
            ok = self._open_port(self.config.port, self.config.baud)
        if not ok:
            self._detect_status = 'failed'
            self._running = False
            return
        self._detect_status = 'connected'
        self._enqueue(MonitorEntry(direction='info',
            decoded=(
                f'Connected: {self._connected_port} @ {self._connected_baud} baud\n'
                f'Protocol detected: {self._detected_protocol}\n'
                f'Mode: {self.config.mode} | Poll: {self.config.poll_interval_ms}ms\n'
                f'Product: {self.config.product_key}'
            )))
        self._main_loop()

    # ── Auto-detect ───────────────────────────────────────────────────────────

    def _auto_detect(self) -> bool:
        self._detect_status = 'scanning'
        ports = SerialBridge.list_ports()
        if not ports:
            self._enqueue(MonitorEntry(direction='error',
                decoded=(
                    'NO SERIAL PORTS FOUND\n'
                    'Check:\n'
                    '  Windows: Device Manager → Ports (COM & LPT)\n'
                    '  Linux:   ls /dev/ttyUSB*  or  dmesg | grep tty\n'
                    '  macOS:   ls /dev/cu.usbserial*\n'
                    'The FT232RNL chip (your converter) usually shows as:\n'
                    '  Windows: "USB Serial Port" or "FT232R USB UART"\n'
                    '  Linux:   /dev/ttyUSB0\n'
                    '  macOS:   /dev/cu.usbserial-XXXXXX'
                )))
            return False

        # Sort: CAN bridges first, then FTDI, then others
        ports.sort(key=lambda p: (0 if p.get('is_can') else 1 if p.get('is_ftdi') else 2, p['device']))

        # Determine which baud rates to try based on product
        baud_order = {
            'lcr2':    [9600, 19200],
            'lcr600':  [9600, 19200, 115200],
            'lcriq':   [115200, 9600, 57600, 38400, 19200],
        }.get(self.config.product_key, AUTO_BAUD_RATES)

        for p in ports:
            dev = p['device']
            self._enqueue(MonitorEntry(direction='probe',
                decoded=f'Probing {dev} ({p["description"]})...'))
            for baud in baud_order:
                if not self._running:
                    return False
                self._enqueue(MonitorEntry(direction='probe',
                    decoded=f'  {dev} @ {baud} baud...'))
                proto = self._probe_port(dev, baud)
                if proto:
                    self._detected_protocol = proto
                    self._enqueue(MonitorEntry(direction='info',
                        decoded=f'DETECTED: {dev} @ {baud} baud | Protocol: {proto}'))
                    return True

        self._enqueue(MonitorEntry(direction='error',
            decoded=(
                'AUTO-DETECT FAILED — no response on any port/baud\n'
                '\n'
                'Most likely causes:\n'
                '1. Wrong wiring — for LCR-II/600: TX→RX and RX→TX must cross over\n'
                '   LCR-II J3: pin48=TXD(orange), pin49=RXD(gray), pin51=GND(white)\n'
                '   Connect: J3-pin48 → your converter RX\n'
                '            J3-pin49 → your converter TX\n'
                '            J3-pin51 → GND\n'
                '2. For LCR.iQ with CAN bridge: CAN-H(J8 pin46) and CAN-L(J8 pin45)\n'
                '   must connect to your CAN-RS232 bridge, not direct RS-232\n'
                '3. Jumper J10 on the LCR CPU board must be set to RS-232 position\n'
                '4. LCR-II/600 power must be ON for the serial port to respond'
            )))
        return False

    def _probe_port(self, port: str, baud: int) -> Optional[str]:
        """
        Try to open the port and detect what protocol the device is using.
        Returns the detected protocol string, or None if no response.
        """
        try:
            import serial as pyserial
            s = pyserial.Serial(port=port, baudrate=baud, bytesize=8,
                                parity='N', stopbits=1,
                                timeout=PROBE_TIMEOUT_S,
                                rtscts=False, dsrdtr=False)
            s.reset_input_buffer()

            # ── LCR-II: send VT100 Ctrl-L (return to top menu) ──────────────
            # If an LCR-II is connected and powered, it will respond with a
            # VT100 escape sequence re-drawing its current display screen.
            s.write(VT100_CTRL_L)
            time.sleep(0.1)
            s.write(VT100_CTRL_D)  # request next item / status
            resp = s.read(32)

            if resp:
                # Check for VT100 ESC sequence (LCR-II/600 response)
                if b'\x1b[' in resp or b'\x1b' in resp:
                    self._open_port(port, baud)
                    s.close()
                    return 'VT100_TERMINAL (LCR-II or LCR-600)'

                # Check for LAWICEL/slcan CAN format: starts with 't', 'T', or 'z'/'Z'
                if resp[0:1] in (b't', b'T', b'z', b'Z', b'v', b'V'):
                    self._open_port(port, baud)
                    s.close()
                    return 'LAWICEL_CAN (CAN-to-RS232 bridge, likely J1939)'

                # Any response at all — possibly LCP binary or other protocol
                self._open_port(port, baud)
                s.close()
                return f'UNKNOWN_PROTOCOL (got {len(resp)} bytes: {resp[:8].hex(" ").upper()})'

            # ── CAN probe: send slcan open + status request ──────────────────
            s.write(b'O\r')   # LAWICEL: open CAN bus
            time.sleep(0.05)
            s.write(b'F\r')   # LAWICEL: get status flags
            resp2 = s.read(16)
            if resp2:
                s.close()
                self._open_port(port, baud)
                return 'LAWICEL_CAN (responded to slcan open command)'

            s.close()
            return None

        except Exception as e:
            return None

    def _open_port(self, port: str, baud: int) -> bool:
        try:
            import serial as pyserial
            self._serial = pyserial.Serial(
                port=port, baudrate=baud, bytesize=8,
                parity='N', stopbits=1,
                timeout=0.05,
                write_timeout=1.0,
                rtscts=False, dsrdtr=False,
            )
            self._serial.reset_input_buffer()
            self._serial.reset_output_buffer()
            self._connected_port = port
            self._connected_baud = baud
            self._lower_ftdi_latency(port)
            return True
        except Exception as e:
            self._enqueue(MonitorEntry(direction='error',
                decoded=f'Cannot open {port} @ {baud}: {e}'))
            return False

    @staticmethod
    def _lower_ftdi_latency(port: str):
        """Lower FTDI latency timer from 16ms to 1ms on Linux (safe, cosmetic)."""
        try:
            import glob
            dev = os.path.basename(port)
            for p in glob.glob(f'/sys/bus/usb-serial/drivers/ftdi_sio/{dev}/latency_timer'):
                with open(p, 'w') as f:
                    f.write('1\n')
        except Exception:
            pass

    # ── Main loop ─────────────────────────────────────────────────────────────

    def _main_loop(self):
        last_poll = 0.0
        while self._running:
            # ── Active poll ───────────────────────────────────────────────────
            now = time.monotonic()
            if (now - last_poll) * 1000 >= self.config.poll_interval_ms:
                last_poll = now
                if self.config.mode == MODE_ACT_AS_METER:
                    poll = self._build_poll_frame()
                    if poll:
                        self.send_bytes(poll)

            # ── Read incoming bytes ───────────────────────────────────────────
            try:
                chunk = self._serial.read(256)
            except Exception as e:
                self._enqueue(MonitorEntry(direction='error',
                    decoded=f'READ ERROR: {e}'))
                self._running = False
                break

            if chunk:
                self._rx_total += len(chunk)
                self._last_rx_ts = time.time()
                # Log EVERY BYTE immediately — before any framing attempt
                self._enqueue(MonitorEntry(
                    direction='rx',
                    raw_hex=chunk.hex(' ').upper(),
                    decoded=_ascii_safe(chunk),
                    byte_count=self._rx_total,
                ))
                self._rx_buffer.extend(chunk)
                self._process_incoming()

    # ── Protocol dispatcher ───────────────────────────────────────────────────

    def _process_incoming(self):
        proto = self._detected_protocol

        if 'VT100' in proto:
            self._process_vt100()
        elif 'LAWICEL' in proto or 'CAN' in proto:
            self._process_can_lawicel()
        else:
            # Unknown / LCP binary — consume buffer, already logged above
            self._rx_buffer.clear()

    def _process_vt100(self):
        """
        Process VT100 terminal data from an LCR-II or LCR-600.
        The register sends VT100 escape sequences. We parse what we can
        and echo back keystrokes to simulate a terminal operator.
        """
        # Try to extract printable content from VT100 escape sequences
        raw = bytes(self._rx_buffer)
        self._rx_buffer.clear()

        # Strip VT100 escape sequences to get displayable text
        clean = re.sub(rb'\x1b\[[0-9;]*[A-Za-z]', b'', raw)  # CSI sequences
        clean = re.sub(rb'\x1b[^[A-Za-z]', b'', clean)         # ESC + char
        printable = clean.decode('ascii', errors='replace').replace('\x00', '').strip()
        if printable:
            self._enqueue(MonitorEntry(direction='info',
                decoded=f'VT100 SCREEN TEXT: {printable[:200]}'))

        # In ACT_AS_METER mode, respond to common VT100 queries
        if self.config.mode == MODE_ACT_AS_METER and b'\x05' in raw:
            # ENQ received — send a status response
            reg = self._register_getter() if self._register_getter else None
            resp = self._build_vt100_status_response(reg)
            if resp:
                self.send_bytes(resp)

    def _build_vt100_status_response(self, reg) -> bytes:
        """
        Build a VT100 terminal response for the LCR-II/600.
        In VT100 mode, we simulate the register's display by sending
        the current delivery values as if they were on the LCD screen.
        """
        if reg is None:
            return VT100_CTRL_L  # just return to top menu
        state = reg.to_dict()
        # Format a simple single-line status that looks like the LCR display
        total = state.get('delivery_total_units', 0)
        active = state.get('delivery_active', False)
        line = f'{total:>10.1f}\r\n'
        return line.encode('ascii')

    def _process_can_lawicel(self):
        """
        Parse LAWICEL/slcan format CAN frames from a CAN-to-RS232 bridge.
        Each frame ends with \r (0x0D).
        Format: T<29bit_ID_8hex><DLC_1hex><DATA_hex>\r  (extended J1939 frame)
                t<11bit_ID_3hex><DLC_1hex><DATA_hex>\r  (standard frame)
        """
        while b'\r' in self._rx_buffer:
            cr_idx = self._rx_buffer.index(ord('\r'))
            frame_bytes = bytes(self._rx_buffer[:cr_idx])
            self._rx_buffer = self._rx_buffer[cr_idx + 1:]

            if not frame_bytes:
                continue

            parsed = self._parse_lawicel_frame(frame_bytes)
            if parsed is None:
                continue

            # Decode J1939 PGN from extended CAN ID
            if parsed['extended']:
                j1939 = decode_j1939_id(parsed['can_id'])
                pgn_str = f'PGN=0x{j1939["pgn"]:04X} SA=0x{j1939["src_addr"]:02X} Pri={j1939["priority"]}'
                self._enqueue(MonitorEntry(direction='can',
                    raw_hex=frame_bytes.decode('ascii', errors='replace'),
                    decoded=f'J1939 {pgn_str} | DATA: {parsed["data"].hex(" ").upper()}',
                    is_addressed_to_us=True,
                ))

                # Build a J1939 response if in ACT_AS_METER mode
                if self.config.mode == MODE_ACT_AS_METER:
                    reg = self._register_getter() if self._register_getter else None
                    resp = build_j1939_response(j1939, parsed['data'],
                                                reg.to_dict() if reg else {})
                    if resp:
                        self.send_bytes(resp)
            else:
                self._enqueue(MonitorEntry(direction='can',
                    raw_hex=frame_bytes.decode('ascii', errors='replace'),
                    decoded=f'CAN std ID=0x{parsed["can_id"]:03X} DATA: {parsed["data"].hex(" ").upper()}',
                ))

    def _parse_lawicel_frame(self, frame: bytes) -> Optional[dict]:
        """Parse a LAWICEL/slcan CAN frame from ASCII bytes."""
        try:
            s = frame.decode('ascii').strip()
            if not s:
                return None
            if s[0].upper() == 'T' and len(s) >= 10:
                # Extended 29-bit frame: T<8hex><1dlc><data>
                can_id = int(s[1:9], 16)
                dlc    = int(s[9], 16)
                data   = bytes.fromhex(s[10:10 + dlc * 2]) if len(s) >= 10 + dlc * 2 else b''
                return {'extended': True, 'can_id': can_id, 'dlc': dlc, 'data': data}
            elif s[0].lower() == 't' and len(s) >= 5:
                # Standard 11-bit frame: t<3hex><1dlc><data>
                can_id = int(s[1:4], 16)
                dlc    = int(s[4], 16)
                data   = bytes.fromhex(s[5:5 + dlc * 2]) if len(s) >= 5 + dlc * 2 else b''
                return {'extended': False, 'can_id': can_id, 'dlc': dlc, 'data': data}
            return None
        except Exception:
            return None

    # ── Poll frame builder ────────────────────────────────────────────────────

    def _build_poll_frame(self) -> Optional[bytes]:
        proto = self._detected_protocol
        reg   = self._register_getter() if self._register_getter else None
        state = reg.to_dict() if reg else {}

        if 'VT100' in proto:
            # For VT100: send a Ctrl-D keystroke to request the next screen
            # item — this keeps the connection alive and updates our view
            # of the register's current state
            return VT100_CTRL_D

        elif 'LAWICEL' in proto or 'CAN' in proto:
            # Build a J1939 "request" frame to ask the LCR.iQ for its status.
            # PGN 0xEA00 (59904) is the standard J1939 "Request" PGN.
            # We request PGN 0xFEF1 (engine fuel delivery).
            return build_j1939_request(
                pgn=0xFEF1,
                src_addr=0xF9,  # our address (off-board diagnostic tool)
                dst_addr=0xFF,  # global broadcast
            )
        else:
            # Unknown: send a simple ENQ byte
            return bytes([0x05])

    # ── Monitor queue ─────────────────────────────────────────────────────────

    def _enqueue(self, entry: MonitorEntry):
        try:
            self.monitor_queue.put_nowait(entry)
        except queue.Full:
            try:
                self.monitor_queue.get_nowait()
                self.monitor_queue.put_nowait(entry)
            except Exception:
                pass

    def drain_monitor(self) -> list[dict]:
        out = []
        while True:
            try:
                e = self.monitor_queue.get_nowait()
                out.append({
                    'ts': e.ts, 'direction': e.direction,
                    'raw_hex': e.raw_hex, 'decoded': e.decoded,
                    'is_addressed_to_us': e.is_addressed_to_us,
                    'byte_count': e.byte_count,
                })
            except queue.Empty:
                break
        return out

    def to_dict(self) -> dict:
        return {
            'running':             self._running,
            'pyserial_ok':         self._pyserial_ok,
            'detect_status':       self._detect_status,
            'connected_port':      self._connected_port,
            'connected_baud':      self._connected_baud,
            'detected_protocol':   self._detected_protocol,
            'config': {
                'port':              self.config.port,
                'baud':              self.config.baud,
                'mode':              self.config.mode,
                'lcp_node_address':  self.config.lcp_node_address,
                'poll_interval_ms':  self.config.poll_interval_ms,
                'auto_detect':       self.config.auto_detect,
                'product_key':       self.config.product_key,
            },
            'available_ports':     self.list_ports(),
            'monitor_pending':     self.monitor_queue.qsize(),
            'rx_total_bytes':      self._rx_total,
            'tx_total_bytes':      self._tx_total,
            'last_rx_ts':          self._last_rx_ts,
        }


# ═══════════════════════════════════════════════════════════════════════════════
#  SAE J1939 / CAN helpers
# ═══════════════════════════════════════════════════════════════════════════════

def decode_j1939_id(can_id_29bit: int) -> dict:
    """Decode a 29-bit extended CAN ID into J1939 fields."""
    priority   = (can_id_29bit >> 26) & 0x07
    reserved   = (can_id_29bit >> 25) & 0x01
    data_page  = (can_id_29bit >> 24) & 0x01
    pf         = (can_id_29bit >> 16) & 0xFF   # PDU Format
    ps         = (can_id_29bit >>  8) & 0xFF   # PDU Specific
    src_addr   = (can_id_29bit >>  0) & 0xFF   # Source Address
    # PGN calculation: if PF >= 240 (peer-to-peer PDU2), PS is destination addr
    if pf >= 240:
        pgn = (data_page << 17) | (pf << 9) | (ps << 1)
    else:
        pgn = (data_page << 17) | (pf << 9)
    return {
        'priority':  priority,
        'pgn':       pgn >> 1,  # normalized
        'pf':        pf,
        'ps':        ps,
        'src_addr':  src_addr,
        'reserved':  reserved,
        'data_page': data_page,
    }


def build_j1939_request(pgn: int, src_addr: int = 0xF9, dst_addr: int = 0xFF) -> bytes:
    """
    Build a LAWICEL-format J1939 Request PGN frame (PGN 0xEA00).
    This asks the device to respond with the requested PGN's data.
    """
    # J1939 Request PGN = 0xEA00, destination in PS field
    pf = 0xEA
    ps = dst_addr
    can_id = (6 << 26) | (pf << 16) | (ps << 8) | src_addr  # priority=6
    data = struct.pack('<BH', pgn & 0xFF, pgn >> 8)  # 3 bytes: PGN LSB first
    data = data[:3]  # exactly 3 bytes
    frame = f'T{can_id:08X}{len(data):01X}{data.hex().upper()}\r'
    return frame.encode('ascii')


def build_j1939_response(j1939: dict, request_data: bytes,
                          register_state: dict) -> Optional[bytes]:
    """
    Build a LAWICEL-format J1939 response frame.
    This is where the simulator responds to PandaBox's CAN queries
    with real meter data from the register state.

    Currently implemented:
      PGN 0xEA00 (Request) → respond with the requested PGN's data
      PGN 0xFEF1 (Engine Fuel Delivery Pressure) → delivery total as pressure
      Unknown PGNs → no response (let PandaBox time out, as a real LCR would)

    Extend this function with the real PGN mappings once you confirm which
    PGNs PandaBox uses to talk to the LCR meter.
    """
    pgn       = j1939['pgn']
    src_addr  = 0x28   # LCR.iQ source address (arbitrary, non-conflicting)
    dst_addr  = j1939['src_addr']   # respond to whoever asked

    # Request PGN (0xEA00) — PandaBox asking "give me PGN X"
    if pgn == 0xEA00:
        if len(request_data) >= 3:
            requested_pgn = request_data[0] | (request_data[1] << 8)
            # Recursively build the response for the requested PGN
            return build_j1939_response(
                {'pgn': requested_pgn, 'src_addr': j1939['src_addr']},
                b'',
                register_state,
            )

    # Delivery volume — encode the current delivery total
    elif pgn == 0xFEF1:
        total = register_state.get('delivery_total_units', 0.0)
        # Encode as uint16 in 0.1-unit resolution (standard SPN encoding)
        encoded = min(int(total * 10), 0xFFFE)  # 0xFFFF = not available
        data    = struct.pack('<H', encoded) + bytes([0xFF] * 6)
        return _build_lawicel_response(pgn, src_addr, dst_addr, data)

    # Add more PGN handlers here as you discover which ones PandaBox uses:
    # elif pgn == 0xXXXX:
    #     ...

    return None   # no response for unknown PGNs


def _build_lawicel_response(pgn: int, src: int, dst: int, data: bytes) -> bytes:
    """Build a LAWICEL extended CAN frame for the given PGN and data."""
    pf     = (pgn >> 9) & 0xFF
    ps     = dst if pf < 240 else (pgn >> 1) & 0xFF
    can_id = (6 << 26) | (pf << 16) | (ps << 8) | src
    dlc    = min(len(data), 8)
    frame  = f'T{can_id:08X}{dlc:01X}{data[:dlc].hex().upper()}\r'
    return frame.encode('ascii')
