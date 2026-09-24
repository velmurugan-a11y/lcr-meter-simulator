"""
LCR Serial Bridge — V5: real LCP protocol over RS-232 / RS-485.

Physical path:
  PandaBox (USART1/2, 19200 8N1) → RS-232 DB25 → USB-to-RS232 converter → COM7 → this process

The bridge runs a background thread that:
  1. Opens the configured COM port at 19200 8N1 (matching Leo's USART1/2 config)
  2. Reads bytes into a ring buffer, scanning for 7E 7E frame sync
  3. Passes each complete LCP frame to LcpEndpoint.handle_frame()
  4. Writes the response back to the port immediately

The LcpEndpoint reads live state from the Python simulator register and
responds exactly as a real LCR-II / LCR-600 / LCR.iQ would over the wire.
"""

import threading
import time
import queue
import collections
from dataclasses import dataclass, field
from typing import Optional, Callable

try:
    import serial
    import serial.tools.list_ports
    _PYSERIAL_OK = True
except ImportError:
    _PYSERIAL_OK = False

from .lcp import find_frame, selftest as lcp_selftest, LCP_SYNC
from .lcp_endpoint import LcpEndpoint

# ── Monitor entry ────────────────────────────────────────────────────────────

@dataclass
class MonitorEntry:
    ts: float
    direction: str          # 'rx' | 'tx' | 'info' | 'error'
    hex_data: str
    decoded: str
    byte_count: int = 0


# ── Configuration ─────────────────────────────────────────────────────────────

@dataclass
class SerialConfig:
    port: str           = 'COM7'
    baud: int           = 19200     # LCR standard — Leo's firmware uses 19200 8N1
    bytesize: int       = 8
    parity: str         = 'N'
    stopbits: int       = 1
    node: int           = 1         # LCP meter node address (1–250)
    product_key: str    = 'lcr2'    # which simulator register to expose


# ── Bridge ────────────────────────────────────────────────────────────────────

class SerialBridge:
    """
    Manages the serial port thread and the LcpEndpoint.
    """

    def __init__(self):
        self.cfg = SerialConfig()
        self.endpoint = LcpEndpoint(node=self.cfg.node)
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._port: Optional['serial.Serial'] = None
        self._monitor: queue.Queue = queue.Queue(maxsize=3000)
        self._rx_total = 0
        self._tx_total = 0
        self._running = False
        self._error: Optional[str] = None
        self._connected_port: Optional[str] = None
        self._last_rx_ts: Optional[float] = None
        self._register_getter: Optional[Callable] = None

        # Run LCP self-test at startup
        fails = lcp_selftest()
        if fails:
            self._log('error', b'', f'LCP self-test FAILED ({fails} failures)')
        else:
            self._log('info', b'', f'LCP self-test OK — 19200 8N1, node {self.cfg.node}')

    # ── Public API ────────────────────────────────────────────────────────────

    def configure(self, **kwargs):
        """Update config fields. node change also updates the endpoint."""
        for k, v in kwargs.items():
            if hasattr(self.cfg, k):
                setattr(self.cfg, k, v)
        if 'node' in kwargs:
            self.endpoint.node = self.cfg.node

    def start(self, register_getter: Callable):
        """Open the serial port and start the background worker."""
        if self._running:
            return
        if not _PYSERIAL_OK:
            self._error = 'pyserial not installed — run: pip install pyserial'
            self._log('error', b'', self._error)
            return
        self._register_getter = register_getter
        self.endpoint.attach(register_getter)
        self._stop_evt.clear()
        self._error = None
        self._thread = threading.Thread(target=self._worker, daemon=True, name='lcr-serial')
        self._thread.start()

    def stop(self):
        """Close the port and stop the background thread."""
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._running = False
        self._connected_port = None

    def drain_monitor(self) -> list:
        """Return and clear all buffered monitor entries."""
        entries = []
        while True:
            try:
                entries.append(self._monitor.get_nowait().__dict__)
            except queue.Empty:
                break
        return entries

    def to_dict(self) -> dict:
        return {
            'running':          self._running,
            'pyserial_ok':      _PYSERIAL_OK,
            'error':            self._error,
            'port':             self._connected_port,
            'baud':             self.cfg.baud,
            'node':             self.cfg.node,
            'product_key':      self.cfg.product_key,
            'rx_total':         self._rx_total,
            'tx_total':         self._tx_total,
            'last_rx_ts':       self._last_rx_ts,
            'monitor_pending':  self._monitor.qsize(),
            'available_ports':  self._list_ports(),
            'endpoint':         self.endpoint.status_dict(),
        }

    def send_raw(self, data: bytes):
        """Send arbitrary bytes on the port (for debug/test)."""
        if self._port and self._port.is_open:
            self._port.write(data)
            self._tx_total += len(data)
            self._log('tx', data, '')

    # ── Internal ──────────────────────────────────────────────────────────────

    def _log(self, direction: str, raw: bytes, decoded: str):
        entry = MonitorEntry(
            ts=time.time(),
            direction=direction,
            hex_data=' '.join(f'{b:02X}' for b in raw),
            decoded=decoded,
            byte_count=len(raw),
        )
        try:
            self._monitor.put_nowait(entry)
        except queue.Full:
            try:
                self._monitor.get_nowait()
                self._monitor.put_nowait(entry)
            except queue.Empty:
                pass

    def _list_ports(self) -> list:
        if not _PYSERIAL_OK:
            return []
        try:
            return [p.device for p in serial.tools.list_ports.comports()]
        except Exception:
            return []

    def _worker(self):
        self._running = True
        try:
            self._run_loop()
        except Exception as e:
            self._error = str(e)
            self._log('error', b'', f'Fatal: {e}')
        finally:
            self._running = False
            if self._port and self._port.is_open:
                try:
                    self._port.close()
                except Exception:
                    pass
            self._connected_port = None

    def _run_loop(self):
        self._log('info', b'', f'Opening {self.cfg.port} at {self.cfg.baud} 8N1 …')
        try:
            self._port = serial.Serial(
                port=self.cfg.port,
                baudrate=self.cfg.baud,
                bytesize=self.cfg.bytesize,
                parity=self.cfg.parity,
                stopbits=self.cfg.stopbits,
                timeout=0.05,       # 50 ms read timeout — keeps the loop responsive
                write_timeout=1.0,
                rtscts=False,
                dsrdtr=False,
            )
        except Exception as e:
            self._error = f'Cannot open {self.cfg.port}: {e}'
            self._log('error', b'', self._error)
            return

        self._connected_port = self.cfg.port
        self._log('info', b'', f'Port open — waiting for PandaBox frames (node={self.cfg.node})')

        rx_buf = bytearray()

        while not self._stop_evt.is_set():
            # ── Read available bytes ──────────────────────────────────────────
            try:
                chunk = self._port.read(256)
            except Exception as e:
                self._log('error', b'', f'Read error: {e}')
                break

            if chunk:
                self._rx_total += len(chunk)
                self._last_rx_ts = time.time()
                rx_buf += chunk

                # Log raw RX
                self._log('rx', chunk, self._decode_ascii(chunk))

            # ── Scan for complete LCP frames ──────────────────────────────────
            while True:
                start, end, parsed = find_frame(rx_buf)
                if parsed is None:
                    # Discard everything before the first 7E 7E pair to keep the buffer tidy
                    sync_pos = rx_buf.find(bytes([LCP_SYNC, LCP_SYNC]))
                    if sync_pos > 0:
                        rx_buf = rx_buf[sync_pos:]
                    elif sync_pos == -1:
                        rx_buf.clear()
                    break

                # We have a valid frame at rx_buf[start:end]
                frame_bytes = bytes(rx_buf[start:end])
                rx_buf = rx_buf[end:]          # consume the frame

                to, from_, status, payload = parsed
                cmd_desc = self._describe_request(payload)
                self._log('rx', frame_bytes,
                          f'LCP→node{to} from={from_:#04x} st={status:#04x} '
                          f'len={len(payload)} {cmd_desc}')

                # Hand to endpoint
                response = self.endpoint.handle_frame(frame_bytes)
                if response:
                    try:
                        self._port.write(response)
                        self._tx_total += len(response)
                        _, _, resp_st, resp_pay = parsed  # re-use variables for logging
                        self._log('tx', response,
                                  f'LCP← {self._describe_response(response)}')
                    except Exception as e:
                        self._log('error', b'', f'Write error: {e}')
                        break

        if self._port and self._port.is_open:
            self._port.close()
        self._log('info', b'', 'Port closed')

    @staticmethod
    def _decode_ascii(data: bytes) -> str:
        return ''.join(chr(b) if 0x20 <= b < 0x7F else '.' for b in data)

    @staticmethod
    def _describe_request(payload: bytes) -> str:
        if not payload:
            return ''
        cmd = payload[0]
        names = {
            0x00: 'GetProductID',
            0x20: f'GetField#{payload[1]}' if len(payload) > 1 else 'GetField?',
            0x21: f'SetField#{payload[1]}' if len(payload) > 1 else 'SetField?',
            0x23: 'GetMachineStatus',
            0x24: f'IssueCmd({payload[1]})' if len(payload) > 1 else 'IssueCmd?',
            0x25: f'SetAddress({payload[1]})' if len(payload) > 1 else 'SetAddress?',
            0x26: 'GetVersion',
            0x27: 'GetSecurityLevel',
            0x28: 'GetDeliveryStatus',
            0x40: 'GetExtField',
            0x41: 'SetExtField',
        }
        return names.get(cmd, f'Cmd0x{cmd:02X}')

    @staticmethod
    def _describe_response(raw: bytes) -> str:
        from .lcp import parse as lcp_parse
        parsed = lcp_parse(raw)
        if not parsed:
            return '(invalid)'
        _, _, st, pay = parsed
        rc = pay[0] if pay else '?'
        devst = pay[1] if len(pay) > 1 else '?'
        return f'rc={rc} devSt={devst:#04x} paylen={len(pay)}'
