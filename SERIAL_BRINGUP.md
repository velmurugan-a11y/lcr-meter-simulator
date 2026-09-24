# Serial Bridge Bring-Up Guide
## Getting the simulator talking to PandaBox over USB → RS-232

---

## Why the simulator must run on your laptop/PC, not in the cloud

The simulator's Flask server runs wherever you run `python app.py`.
The USB-to-RS232 converter is plugged into **your machine's USB port**.
These need to be on the **same physical machine** — the browser can be
anywhere, but the Python process talking to the COM port must be local.

---

## Step 0 — Find your port name

**Windows:**
Open Device Manager → Ports (COM & LPT). Your converter will appear as
something like "USB Serial Port (COM4)" or "Silicon Labs CP210x (COM7)".
The COM number is what you need.

**Linux:**
```
ls /dev/ttyUSB* /dev/ttyACM*
```
Usually `/dev/ttyUSB0`. If nothing appears: `dmesg | grep tty` right after
plugging in to see what the kernel named it.

Common chip driver names to look for in dmesg:
- FTDI FT232: `ftdi_sio`
- Silicon Labs CP2102: `cp210x`
- WCH CH340/CH341: `ch341`
- Prolific PL2303: `pl2303`

**macOS:**
```
ls /dev/cu.usbserial*  /dev/cu.wchusbserial*
```

---

## Step 1 — Loopback test (no PandaBox, just a jumper wire)

This confirms the USB converter works end-to-end before involving PandaBox.

**Wire the loopback:**
On the DB9 male connector side (the RS-232 side of your converter):
```
Pin 2 (RXD) ←—— short with a wire ——→ Pin 3 (TXD)
```
If your converter has a terminal block instead of DB9:
- Short the TX screw terminal to the RX screw terminal.

**Run the test:**
```
python3 tools/serial_loopback_test.py --port /dev/ttyUSB0 --baud 115200
```
Replace `/dev/ttyUSB0` with your actual port from Step 0.

Expected: `ALL 5 LOOPBACK TESTS PASSED`

If you get TIMEOUT: the jumper wire is missing or on wrong pins.
If you get MISMATCH: wrong baud rate — try 9600 or 19200.

---

## Step 2 — Connect to PandaBox and confirm wiring polarity

RS-232 crossover wiring (what you need for a DTE-to-DTE connection, which
is what this is — both the LCR simulator and PandaBox are data terminal
equipment, not modems):

```
Simulator side (DB9)          PandaBox side (RS-232 header)
─────────────────────         ──────────────────────────────
Pin 2  RXD ────────────────── TXD  (PandaBox's transmit)
Pin 3  TXD ────────────────── RXD  (PandaBox's receive)
Pin 5  GND ────────────────── GND  (MUST be connected)
```

**The GND connection is mandatory.** An isolated converter only isolates
the signal ground from the power ground — the RS-232 signal reference
(pin 5) still needs to be common between the two devices or neither end
can correctly read the other's signal levels.

**If your converter is already a USB-to-RS232 adapter with a DB9 female
connector**, and PandaBox has a DB9 male port: a straight-through DB9
cable connects them correctly for DTE-to-DTE (pins 2-2, 3-3, 5-5).

**If PandaBox has a custom RS-232 header (not DB9)**: check the PandaBox
schematic for which pin is TXD, RXD, and GND, and connect accordingly.

---

## Step 3 — Confirm PandaBox receives the heartbeat

Remove the loopback jumper from Step 1. Connect to PandaBox.

```
python3 tools/serial_heartbeat.py --port /dev/ttyUSB0 --baud 115200
```

This sends one line per second:
```
LCR_SIM HEARTBEAT #00001 ts=14:32:01 baud=115200
LCR_SIM HEARTBEAT #00002 ts=14:32:02 baud=115200
...
```

On the PandaBox side, capture whatever arrives on its serial RX:
- If PandaBox has a debug UART or serial monitor mode, enable it.
- If PandaBox has a raw-bytes capture mode for the LCR port, use that.
- You should see the heartbeat lines arriving at 1 Hz.

**If PandaBox receives nothing:**
- Confirm GND is connected (step 2).
- Try swapping TX and RX wires — if polarity is wrong you'll get nothing.
- Try a different baud rate: `--baud 9600` first (most devices default to 9600).

**If PandaBox receives garbage (wrong characters):**
- Baud rate mismatch. Try 9600, then 19200, then 57600.
- Parity mismatch (unlikely — 8N1 is universal for industrial RS-232).

The heartbeat script also prints any bytes it receives from PandaBox, so
you can see both directions at once.

---

## Step 4 — Start the full simulator on your machine

Once the heartbeat confirms both directions work:

```
# Terminal 1: start the simulator
python3 app.py

# Terminal 2: open the browser
# http://localhost:5000
# Click "Serial" in the nav to get to the serial bridge panel
```

In the Serial Bridge panel on the home page:
1. Select your port from the dropdown.
2. Set baud to match what worked in the heartbeat test.
3. Mode: "Act as Meter" (the simulator responds to PandaBox's commands).
4. Meter to simulate: whichever product (LCR-II, LCR 600, or LCR.iQ).
5. Click "Start Bridge".

The Live Monitor will show every byte received from PandaBox (as raw hex)
and every byte the simulator sends back. Until the LCP protocol layer is
filled in (see SIMULATION_NOTES.md and meter_core/serial_bridge.py), the
simulator will NOT send protocol-level responses — but you'll see PandaBox's
transmissions arriving, which confirms the physical link is working.

---

## Step 5 — Enable the LCP protocol layer (when ready)

Open `meter_core/serial_bridge.py` and look for the two stub functions at
the bottom:

```python
def parse_frame(raw: bytes) -> Optional[dict]:
    # STUB — fill in the actual LCP framing here
    return None

def build_response(parsed_cmd: dict, register_state: dict) -> bytes:
    # STUB — fill in the actual response format here
    return b""
```

Fill in `parse_frame` with the real LCP framing (SOF/EOF bytes, checksum
algorithm, command ID extraction) from the LCP Host Interface Document.
Fill in `build_response` to return the correct bytes for each command ID
using values from `register_state` (which is the full `to_dict()` output
from the simulator's active register — delivery total, k-factor, errors, etc).

The LCP data type widths confirmed from the manuals are already in the file:
```python
LCP_TYPE_WIDTHS = {
    "UINT1": 1, "UINT2": 2, "UINT4": 4,
    "SINT1": 1, "SINT2": 2,
    "BCD2": 2, "BCD4": 4,
    "ASCII8": 8, "ASCII16": 16, "ASCII20": 20,
}
```

Everything else (port management, threading, mode switching, the browser
monitor) is already complete and will work immediately once those two
functions return real data.

---

## Baud rate reference from the LCR.iQ product manual

| Use case | Baud rate |
|---|---|
| LCP service (default) | 115200 |
| Epson printer | 9600 |
| Available options | 9600 / 19200 / 115200 |

The LCR-II and LCR-600 manuals document RS-232 (EIA-232E) and RS-485
(SAE J1708) at configurable rates; 9600 is the traditional default for
LCR-II field installations, but your specific PandaBox firmware may use
a different rate — check the PandaBox serial configuration screen or the
source code for `UART_BAUD` or similar.

---

## Checklist summary

- [ ] USB converter appears in device list (Step 0)
- [ ] Loopback test passes at target baud rate (Step 1)
- [ ] GND connected between simulator machine and PandaBox (Step 2)
- [ ] Heartbeat lines appear on PandaBox's RX side (Step 3)
- [ ] Heartbeat script shows PandaBox's TX bytes arriving too (Step 3)
- [ ] Serial bridge starts in the simulator UI without error (Step 4)
- [ ] Live Monitor shows incoming bytes from PandaBox (Step 4)
- [ ] LCP parse_frame and build_response filled in (Step 5, when spec available)
