# LectroCount Register Family Simulator

A working simulation of three Liquid Controls LectroCount register
generations (LCR-II, LCR 600, LCR.iQ), built from the engineering manuals,
for testing PandaBox (or any other integration) without needing the real
hardware on the bench.

---

## 1. What you need installed

### Required (to run the simulator today, on-screen control)

| Software | Version | Why | Download |
|---|---|---|---|
| Python | 3.10 or newer | Runs the simulator backend | https://www.python.org/downloads/ |
| pip | (comes with Python) | Installs the one Python package needed (Flask) | — |
| A web browser | Any recent Chrome, Edge, or Firefox | The simulator's display | — |

That's it for today's build. You do **not** need Node.js, a database, or
Docker.

### Also required (for the RS-232 serial bridge — V5, wired up and ready)

| Software | Why |
|---|---|
| `pyserial` (included in `requirements.txt`, installed by `pip install -r requirements.txt`) | Lets Python talk to COM7 (or whichever port your USB-to-RS232 converter appears on) |
| USB driver for your RS-232 converter (FTDI / CH340 / CP2102 — depends on converter chipset) | Without this, Windows won't show a COM port for the converter at all |

The `pip install -r requirements.txt` step (Step 3 below) installs `pyserial`
automatically. The USB driver must be installed separately — check Device Manager
if the COM port doesn't appear after plugging in the converter.

---

## 2. Windows: exact CMD steps

Open Command Prompt (`Win + R`, type `cmd`, Enter) and run these one at a time.

**Step 1 — confirm Python is installed and on PATH:**
```
python --version
```
If this errors with "not recognized," install Python from the link above.
**Important during install:** tick the box "Add Python to PATH" on the first
installer screen — this is the single most common reason `python` doesn't
work afterward.

**Step 2 — go into the simulator folder** (adjust the path to wherever you
unzipped it):
```
cd C:\Users\YourName\Downloads\lcr-meter-simulator
```

**Step 3 — install the one required package:**
```
pip install -r requirements.txt
```

**Step 4 — start the simulator:**
```
python app.py
```

You'll see something like:
```
 * Running on http://127.0.0.1:5000
```
Leave this Command Prompt window open — closing it stops the simulator.

**Step 5 — open it in your browser:**
Go to `http://localhost:5000`

**To stop the simulator:** click back into the Command Prompt window and
press `Ctrl + C`.

---

## 3. macOS / Linux: exact terminal steps

```
python3 --version
cd ~/Downloads/lcr-meter-simulator
pip3 install -r requirements.txt
python3 app.py
```
Then open `http://localhost:5000` in a browser. `Ctrl + C` in the terminal
to stop it.

---

## 4. Using it

- The top nav switches between LCR-II, LCR 600, and LCR.iQ. Each runs as
  its own independent instance — calibrating the LCR-II doesn't touch the
  LCR 600's state, and you can leave all three "powered on" at once.
- Each meter page has an **Original View / Custom View** toggle in the
  top-right of the instrument panel.
  - **Original View** recreates the real physical meter as closely as the
    manuals' own photos allow: the actual switch plate / keypad layout,
    a continuously-rotating selector dial (drag it, don't click a label),
    and the real screen colors (yellow only during active fueling on
    LCR.iQ, matching the real unit; white/gray at idle).
  - **Custom View** is the simplified dashboard layout with extra panels
    (VCF tables, multi-point calibration entry, raw diagnostics) that
    doesn't try to look like the physical unit, just exposes every
    function directly. Useful once you know what you're testing and don't
    need to "operate" it like a field technician would.
- The printer popup appears any time the simulated meter would print a
  real ticket (delivery / shift / diagnostic). The dropdown lets you switch
  between the Epson Slip Printer and Epson Roll Printer layouts (the two
  printer types shown in Liquid Controls' own installation manuals);
  Epson Slip Printer is the default, matching what ships with most LCR
  systems per the manuals.
- The metal bolt / lock icon on each instrument represents the real
  Weights & Measures tamper seal. When locked, calibration fields are
  read-only in the UI, mirroring the real unit's physical seal that a
  field technician would have to break (and re-seal, and log) to recalibrate.

---

## 5. What's real vs. approximated

See `SIMULATION_NOTES.md` for the detailed list of what's modeled exactly
against the source manuals (k-Factor math, RTD curve, fault voltage tables,
etc.) vs. what's an engineering approximation (VCF curve magnitudes). Short
version: the workflow, calibration math, and fault-diagnosis logic are
real; this is not a Weights & Measures-traceable instrument.

---

## 6. V5 — LCP serial bridge (USB-to-RS232, COM7, 19200 8N1)

V5 implements the full **Liquid Controls Protocol (LCP)** binary protocol so
PandaBox firmware can communicate with this simulator exactly as it would with
a real LCR meter over RS-232.

**Physical path:**
```
Mobile App / PandaBox Tester
        ↓ BLE
PandaBox firmware (GD32F305VCT6)
        ↓ USART1/2 (19200 8N1) → RS-232 DB25 J1/J2
USB-to-RS232 converter (COM7)
        ↓
LCR Simulator (this program, python app.py)
```

**What's implemented:**

| File | What it does |
|---|---|
| `meter_core/lcp.py` | LCP frame build / parse / CRC-16 / byte stuffing / 5 self-test vectors |
| `meter_core/lcp_endpoint.py` | All LCP commands (GetProductID, Get/SetField, GetMachineStatus, IssueCommand, SetAddress, GetVersion, GetSecurityLevel, GetDeliveryStatus, Extended Get/SetField) |
| `meter_core/serial_bridge.py` | Background thread: opens COM7 at 19200 8N1, scans for 7E 7E frames, dispatches to LcpEndpoint, writes response back |

**How to connect (V5):**
1. Plug USB-to-RS232 into COM7 (check Device Manager if unsure)
2. Wire: PandaBox DB25 J1 pin 14 (TX) → RS232 RX, J1 pin 15 (RX) → RS232 TX, J1 pin 11 → GND
3. Run `python app.py`
4. Open `http://localhost:5000/serial`
5. Select COM7 / 19200 / Node 1 / LCR-II → click **Connect**

PandaBox will then receive valid LCP responses for all poll fields (#2 GrossQty,
#4 FlowRate, #17 GrossTotal, #18 NetTotal, #100 PrevGross, #101 PrevNet) and
all IssueCommand codes (Start=0, Pause=1, End=2, Print=6).

See `SERIAL_BRINGUP.md` for the full wiring and loopback-test procedure.

---

## 7. Project layout

```
app.py                    Flask routes (thin — delegates all logic to meter_core)
meter_core/
  lcp.py                   LCP frame builder/parser/CRC + 5 self-test vectors  ← V5
  lcp_endpoint.py           LCP command handler (all 10 commands)               ← V5
  serial_bridge.py          SerialBridge: COM7 19200 8N1, background RX thread  ← V5
  rtd_vcf.py               Pt100 curve + VCF compensation (shared physics)
  pulser.py                 Background thread simulating the J8 pulser
  register_base.py          Shared calibration/delivery/error-dictionary logic
  printer.py                Ticket formatting + printer-model definitions
  lcr2.py                   LCR-II: rotary selector + SELECT/INCREASE menu
  lcr600.py                 LCR-600: rotary + alphanumeric keypad, POS engine
  lcriq.py                  LCR.iQ: wireless, SENSEiQ, digital valve ramp
templates/
  serial_monitor.html       LCP serial monitor: live frame log, LCP status      ← V5
  (others)                  Per-product simulator UI pages
static/                     Shared CSS + polling JS + Original View assets
test_core.py                Standalone tests (66 checks incl. LCP self-test)
browser_check.py            Playwright-based check that drives the real pages
tools/
  serial_loopback_test.py   USB-RS232 loopback verification (run before wiring PandaBox)
  serial_heartbeat.py       Sends heartbeat bytes to confirm TX→PandaBox link
```
