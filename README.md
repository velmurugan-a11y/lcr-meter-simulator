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

### Will be needed later (for the real RS-232/RS-485-over-USB step, point 2
of your request — not wired up yet, see the "What's not built yet" section
at the bottom)

| Software | Why |
|---|---|
| `pyserial` (a Python package, `pip install pyserial`) | Lets Python talk to a COM port |
| The actual USB driver for your RS-232/RS-485-to-USB converter (e.g. FTDI, CH340, CP2102 chipset driver depending on which converter you bought) | Without this, Windows/your OS won't show a COM port at all for the converter |

You don't need to install these yet. They're listed here so you have
everything ready when we wire up the hardware step.

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

## 6. What's not built yet

You asked for the RS-232/RS-485-over-USB integration (the simulator acting
as a real LCR on the wire, or monitoring a real one) and LCR.iQ daisy-chain
printing. Both are intentionally not in this build:

- **Serial/COM port integration**: this needs the actual LectroCount wire
  protocol (command bytes, message framing, checksums) — the manuals I've
  read document the *physical pins* (which wire is RS-485 A/B, etc.) and
  the *data types* used inside messages (how many bytes a "Volume" field
  is), but not the message structure itself. Building this convincingly
  needs either the real protocol spec or your existing PandaBox serial
  code that already talks to a real LCR, so the simulator's responses can
  be checked against what your code actually expects. This is planned as
  the next phase, not skipped — see the project board / your own notes for
  when to revisit.
- **LCR.iQ daisy-chain printing**: explicitly deferred at your request,
  to be planned later.

---

## 7. Project layout

```
app.py                    Flask routes (thin — delegates all logic to meter_core)
meter_core/
  rtd_vcf.py               Pt100 curve + VCF compensation (shared physics)
  pulser.py                 Background thread simulating the J8 pulser
  register_base.py          Shared calibration/delivery/error-dictionary logic
  printer.py                 Ticket formatting + printer-model definitions
  lcr2.py                    LCR-II: rotary selector + SELECT/INCREASE menu
  lcr600.py                   LCR-600: rotary + alphanumeric keypad, POS engine
  lcriq.py                     LCR.iQ: wireless, SENSEiQ, digital valve ramp
templates/                  Jinja2 pages (one per product + shared base/index)
static/                     Shared CSS + polling JS + Original View assets
test_core.py                Standalone test script, run before touching the UI
browser_check.py            Playwright-based check that drives the real pages
```
