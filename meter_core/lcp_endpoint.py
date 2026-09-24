"""
LCP meter endpoint: maps PandaBox LCP commands to the Python simulator state.

Ported from meter.c / lcr_host.c in the PandaBox BLE firmware.
The simulator register state is accessed via a register_getter callable
that returns the active RegisterBase instance.

LCP field numbers (all volumes in tenths of a gallon, big-endian 32-bit signed):
  #2   GrossQty        current delivery volume
  #3   NetQty          (0, not compensated)
  #4   FlowRate        flow rate in tenths gal/min
  #5   GrossPreset_PL  gross preset (0 = none)
  #6   NetPreset_PL    net preset
  #17  GrossTotal_WM   live totalizer
  #18  NetTotal_WM     (0)
  #22  SaleNumber
  #23  TicketNumber
  #25  NoFlowTimer     uint16, seconds
  #27  PresetType      uint8  (0=Clear; NOTE: 0x1B = field 27 must be escaped!)
  #37  TicketRequired  uint8  (0=required, 1=print-if-available, 2=never)
  #38  Units           uint8  (0=gallons)
  #39  Decimals        uint8  (1=tenths)
  #44  GrossQtyTot     same as #2 when > 0
  #45  NetQtyTot       (0)
  #92  RemainingPreset remaining volume to preset
  #100 PrevGross       totalizer at start of last delivery
  #101 PrevNet         (0)
  #102 LCRNode         2-byte: {0x00, node_address}
"""

import struct
import threading
from . import lcp

# ── Return codes ────────────────────────────────────────────────────────────
RC_OK               = 0
RC_INVALID_FIELD    = 33
RC_BAD_DATA         = 34
RC_NOT_SET_MODE     = 35
RC_INVALID_CMD      = 36
RC_INVALID_ADDR     = 37
RC_RANGE            = 113
RC_READ_ONLY        = 117
RC_STATE            = 120
RC_TICKET_PENDING   = 121

# ── Machine state (devStatus bits 4–6) ─────────────────────────────────────
MS_RUN         = 0x00   # delivery active, valve open, product flowing
MS_STOP        = 0x10   # delivery active, valve closed / paused
MS_END         = 0x20   # no active delivery (idle)
MS_WAIT_NOFLOW = 0x60   # ending: valve closed, waiting for flow to decay to 0
SW_RUN         = 0x01   # switch-position bit (always 1 in our simulation)

# ── Delivery code bits ──────────────────────────────────────────────────────
DC_TICKET_PENDING   = 0x0001
DC_FLOW_ACTIVE      = 0x0004
DC_DELIVERY_ACTIVE  = 0x0008
DC_GROSS_PRESET     = 0x0010
DC_GROSS_PRESET_HIT = 0x0040

# ── Delivery status bits ────────────────────────────────────────────────────
DS_PRESET_REACHED = 0x0080
DS_NOFLOW_STOP    = 0x0100
DS_STOP_REQUEST   = 0x0200
DS_END_REQUEST    = 0x0400


def _be32(v: int) -> bytes:
    return struct.pack('>i', int(v))

def _from_be32(b: bytes) -> int:
    return struct.unpack('>i', b[:4])[0]


class LcpEndpoint:
    """
    Sits between the serial port and the Python simulator.
    Call handle_frame() with every inbound LCP frame bytes;
    it returns the response bytes to send back, or b'' if no response.
    """

    def __init__(self, node: int = 1):
        self._lock = threading.Lock()
        self.node = node                # LCP node address (1–250)
        self.product_id = "SR200b2.05" # shown in Get Product ID response
        self._register = None           # set by attach()

        # Delivery state (mirrors meter.c fields)
        self._state        = MS_END
        self._paused       = False
        self._del_status   = 0
        self._del_code     = 0
        self._gross        = 0          # current delivery (tenths)
        self._flow         = 0          # instantaneous flow (tenths/min)
        self._gross_total  = 50000      # initialise to 5000.0 gal (like the bench meter)
        self._net_total    = 0
        self._prev_gross   = 50000
        self._prev_net     = 0
        self._sale_no      = 25
        self._ticket_no    = 1
        self._noflow_s     = 180        # no-flow timer (seconds)
        self._preset       = 0          # gross preset (tenths, 0 = none)
        self._net_preset   = 0
        self._preset_type  = 0          # 0 = Clear
        self._ticket_req   = 1          # 1 = print if available
        self._ticket_pending = False
        self._decimals     = 1          # tenths

        # RX/TX counters
        self.rx_frames = 0
        self.tx_frames = 0

    def attach(self, register_getter):
        """
        Attach a callable that returns the active RegisterBase instance.
        When attached, meter state is derived from the Python register on every
        handle_frame() call so the two stay in sync.
        """
        self._register = register_getter

    def _sync_from_register(self):
        """Pull current state from the Python simulator into LCP fields."""
        if self._register is None:
            return
        try:
            reg = self._register()
            snap = reg.pulser.snapshot()

            # Flow: pulser flow_rate is in units/min → tenths/min
            self._flow = int(snap['flow_rate'] * 10)

            # Current delivery gross (tenths)
            self._gross = int(getattr(reg, '_delivery_units', 0) * 10)

            # Gross total: base + all completed deliveries + current
            base = getattr(reg, '_gross_total_tenths', 50000)
            self._gross_total = base + self._gross

            # Machine state
            if not reg.delivery_active:
                self._state = MS_END
                self._paused = False
            elif self._flow > 0:
                self._state = MS_RUN
            else:
                self._state = MS_STOP

            # Delivery flags
            if reg.delivery_active:
                self._del_code |= DC_DELIVERY_ACTIVE
                if self._flow > 0:
                    self._del_code |= DC_FLOW_ACTIVE
                else:
                    self._del_code &= ~DC_FLOW_ACTIVE
            else:
                self._del_code &= ~(DC_DELIVERY_ACTIVE | DC_FLOW_ACTIVE)

            if getattr(reg, 'delivery_pending_print', False):
                self._del_code |= DC_TICKET_PENDING
                self._ticket_pending = True
            else:
                self._del_code &= ~DC_TICKET_PENDING
                self._ticket_pending = False

            # Preset from active product
            p = reg.active_product()
            preset_units = getattr(p, 'preset_units', 0) or 0
            self._preset = int(preset_units * 10)
            if self._preset > 0:
                self._del_code |= DC_GROSS_PRESET
            else:
                self._del_code &= ~DC_GROSS_PRESET

        except Exception:
            pass  # graceful degradation if register isn't ready

    def _push_to_register(self, cmd: int) -> int:
        """Forward a Start/Stop/Pause/Print command to the Python register. Returns RC."""
        if self._register is None:
            return RC_OK
        try:
            reg = self._register()
            if cmd == 0:  # Start / Resume
                if self._state == MS_END:
                    if self._ticket_pending and self._ticket_req == 0:
                        return RC_TICKET_PENDING
                    reg.start_delivery()
                    self._sale_no += 1
                    self._prev_gross = self._gross_total
                    self._prev_net = self._net_total
                    self._gross = 0
                    self._del_status = 0
                    return RC_OK
                elif self._state == MS_WAIT_NOFLOW:
                    return RC_STATE
                else:
                    # Resume from pause
                    reg.pulser.set_mode('flowing')
                    self._paused = False
                    self._del_status &= ~DS_STOP_REQUEST
                    return RC_OK
            elif cmd == 1:  # Pause
                if self._state == MS_END:
                    return RC_STATE
                reg.pulser.set_mode('idle')
                self._paused = True
                self._del_status |= DS_STOP_REQUEST
                return RC_OK
            elif cmd == 2:  # End delivery + ticket
                if self._state == MS_END:
                    return RC_OK
                reg.stop_delivery()
                self._ticket_no += 1
                self._del_status |= DS_END_REQUEST
                self._del_status &= ~DS_STOP_REQUEST
                self._paused = False
                self._state = MS_WAIT_NOFLOW
                # After stop_delivery() flow ramps to 0; next sync will update
                return RC_OK
            elif cmd in (3, 4):
                return RC_OK
            elif cmd == 6:  # Print
                if self._state != MS_END:
                    return RC_STATE
                if self._ticket_pending:
                    self._ticket_pending = False
                    self._del_code &= ~DC_TICKET_PENDING
                    if self._register:
                        self._register().delivery_pending_print = False
                self._ticket_no += 1
                return RC_OK
            else:
                return RC_INVALID_CMD
        except Exception:
            return RC_INVALID_CMD

    def dev_status(self) -> int:
        return (self._state & 0x70) | SW_RUN

    def _field_get(self, fld: int) -> bytes | None:
        """Return raw field bytes or None for invalid field."""
        if fld == 2:   return _be32(max(0, self._gross))
        if fld == 3:   return _be32(0)
        if fld == 4:   return _be32(max(0, self._flow))
        if fld == 5:   return _be32(self._preset)
        if fld == 6:   return _be32(self._net_preset)
        if fld == 17:  return _be32(self._gross_total)
        if fld == 18:  return _be32(self._net_total)
        if fld == 22:  return _be32(self._sale_no)
        if fld == 23:  return _be32(self._ticket_no)
        if fld == 25:  return struct.pack('>H', self._noflow_s)
        if fld == 27:  return bytes([self._preset_type])      # 0x1B — LCP will escape it!
        if fld == 37:  return bytes([self._ticket_req])
        if fld == 38:  return bytes([0])                      # gallons
        if fld == 39:  return bytes([self._decimals])
        if fld == 44:  return _be32(max(0, self._gross))
        if fld == 45:  return _be32(0)
        if fld == 92:
            remaining = max(0, self._preset - self._gross) if self._preset > 0 else 0
            return _be32(remaining)
        if fld == 100: return _be32(self._prev_gross)
        if fld == 101: return _be32(self._prev_net)
        if fld == 102: return bytes([0x00, self.node])
        return None

    def _field_set(self, fld: int, data: bytes) -> int:
        if fld == 5:
            if len(data) != 4:
                return RC_BAD_DATA
            if self._state in (MS_RUN, MS_WAIT_NOFLOW):
                return RC_NOT_SET_MODE
            v = _from_be32(data)
            if v < 0:
                return RC_RANGE
            self._preset = v
            if self._register:
                try:
                    p = self._register().active_product()
                    p.preset_units = v / 10.0
                except Exception:
                    pass
            if self._state != MS_END:
                if v > 0:
                    self._del_code |= DC_GROSS_PRESET
                else:
                    self._del_code &= ~DC_GROSS_PRESET
            return RC_OK
        if fld == 6:
            if len(data) != 4:
                return RC_BAD_DATA
            if self._state in (MS_RUN, MS_WAIT_NOFLOW):
                return RC_NOT_SET_MODE
            v = _from_be32(data)
            if v < 0:
                return RC_RANGE
            self._net_preset = v
            return RC_OK
        if fld == 25:
            if len(data) != 2:
                return RC_BAD_DATA
            self._noflow_s = struct.unpack('>H', data[:2])[0]
            return RC_OK
        if fld == 27:
            self._preset_type = data[0]
            return RC_OK
        if fld == 37:
            self._ticket_req = data[0]
            return RC_OK
        if fld == 102:
            if len(data) != 2 or data[1] == 0 or data[1] > 250:
                return RC_INVALID_ADDR
            self.node = data[1]
            return RC_OK
        if fld in (2, 3, 4, 17, 18, 100, 101):
            return RC_READ_ONLY
        return RC_INVALID_FIELD

    def handle_frame(self, raw: bytes) -> bytes:
        """
        Process one inbound LCP frame.  Returns response bytes to send,
        or b'' if the frame is not addressed to this node or is invalid.
        """
        parsed = lcp.parse(raw)
        if parsed is None:
            return b''
        to, from_, status, payload = parsed
        if status & lcp.LCP_ST_RESPONSE:
            return b''                  # ignore response frames
        if not payload:
            return b''
        if to != self.node:
            return b''                  # not our address (0 = broadcast, no reply)

        self._sync_from_register()
        self.rx_frames += 1

        old_node = self.node
        cmd_byte = payload[0]
        d = bytearray()
        resp_status = lcp.LCP_ST_RESPONSE | (status & lcp.LCP_ST_MSGID)
        unsupported = False

        if cmd_byte == 0x00:  # Get Product ID
            d += bytes([RC_OK, 0x02])
            d += self.product_id.encode('ascii') + b'\x00'

        elif cmd_byte in (0x20, 0x40):  # Get Field Data (1-byte or 2-byte field number)
            if cmd_byte == 0x20:
                fld = payload[1] if len(payload) > 1 else -1
            else:
                fld = ((payload[1] << 8) | payload[2]) if len(payload) > 2 else -1
            fdata = self._field_get(fld) if fld >= 0 else None
            d.append(RC_OK if fdata else RC_INVALID_FIELD)
            d.append(self.dev_status())
            if fdata:
                d += fdata

        elif cmd_byte in (0x21, 0x41):  # Set Field Data
            hdr = 2 if cmd_byte == 0x21 else 3
            if cmd_byte == 0x21:
                fld = payload[1] if len(payload) > 1 else -1
            else:
                fld = ((payload[1] << 8) | payload[2]) if len(payload) > 2 else -1
            if fld >= 0 and len(payload) > hdr:
                rc = self._field_set(fld, payload[hdr:])
            else:
                rc = RC_BAD_DATA
            d.append(rc)
            d.append(self.dev_status())

        elif cmd_byte == 0x23:  # Get Machine Status
            d.append(RC_OK)
            d.append(self.dev_status())
            d.append(0x20)              # no print processor
            d += struct.pack('>H', self._del_status)
            d += struct.pack('>H', self._del_code)

        elif cmd_byte == 0x24:  # Issue Command
            cmd = payload[1] if len(payload) >= 2 else 0xFF
            rc = self._push_to_register(cmd) if cmd != 0xFF else RC_INVALID_CMD
            d.append(rc)
            d.append(self.dev_status())

        elif cmd_byte == 0x25:  # Set Device Address
            if len(payload) >= 2 and 1 <= payload[1] <= 250:
                self.node = payload[1]
                d.append(RC_OK)
            else:
                d.append(RC_INVALID_ADDR)
            d.append(self.dev_status())

        elif cmd_byte == 0x26:  # Get Version
            d += bytes([RC_OK, self.dev_status(), 2, 5])

        elif cmd_byte == 0x27:  # Get Security Level
            level = 0x01 if self._state == MS_END else (0x00 if self._paused else 0x81)
            d += bytes([RC_OK, self.dev_status(), level])

        elif cmd_byte == 0x28:  # Get Delivery Status
            d.append(RC_OK)
            d.append(self.dev_status())
            d += struct.pack('>H', self._del_status)
            d += struct.pack('>H', self._del_code)

        else:
            unsupported = True

        self.tx_frames += 1
        if unsupported:
            resp_status |= 0x40         # "not supported" flag
            return lcp.build(from_, old_node, resp_status, b'')
        return lcp.build(from_, old_node, resp_status, bytes(d))

    def status_dict(self) -> dict:
        """For the /api/serial/status endpoint."""
        return {
            'node':          self.node,
            'state':         {MS_END: 'END', MS_STOP: 'STOP', MS_RUN: 'RUN',
                              MS_WAIT_NOFLOW: 'WAIT_NOFLOW'}.get(self._state, 'UNKNOWN'),
            'gross_tenths':  self._gross,
            'flow_tenths_min': self._flow,
            'gross_total_tenths': self._gross_total,
            'preset_tenths': self._preset,
            'del_status':    hex(self._del_status),
            'del_code':      hex(self._del_code),
            'sale_no':       self._sale_no,
            'ticket_no':     self._ticket_no,
            'rx_frames':     self.rx_frames,
            'tx_frames':     self.tx_frames,
        }
