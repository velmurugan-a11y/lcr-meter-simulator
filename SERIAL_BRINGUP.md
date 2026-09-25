# Serial Bridge Bring-Up Guide (V5)
## Connecting the LCR Simulator to PandaBox over USB → RS-232

---

## Architecture

```
Mobile App / PandaBox Tester  (BLE)
        ↓
PandaBox firmware (GD32F305VCT6)
        ↓  USART1 (LCR Port 1): PA2=TX / PA3=RX, 19200 8N1
           RS-232 power: PE5=HIGH (enables BL13232 transceiver)
        ↓  DB25 J1 connector
           pin 14 = TX (from PandaBox)
           pin 15 = RX (to PandaBox)
           pin 11 = GND
        ↓  USB-to-RS232 converter  →  COM7 (Windows default)
        ↓
LCR Simulator (python app.py, http://localhost:5000/serial)
```

The simulator must run on the **same physical machine** the USB-to-RS232
converter is plugged into — the serial port is not accessible over a network.
The browser can be anywhere.

---

## Step 0 — Install the USB converter driver and find the COM port

**Windows:**
Open Device Manager → Ports (COM & LPT). Your converter will appear as
something like "USB Serial Port (COM7)" or "Silicon Labs CP210x (COM7)".
If nothing appears, install the driver for your converter's chipset:

| Chip | Driver |
|---|---|
| FTDI FT232 | ftdi-chip.com / Windows Update |
| Silicon Labs CP2102/CP2104 | silabs.com/developers/usb-to-uart-bridge-vcp-drivers |
| WCH CH340/CH341 | wch.cn or search "ch340 driver" |
| Prolific PL2303 | prolific.com.tw |

**Linux:** `ls /dev/ttyUSB*` — usually `/dev/ttyUSB0`
**macOS:** `ls /dev/cu.usbserial*`

---

## Step 1 — Loopback test (no PandaBox — just verify the USB converter)

Short pin 2 (RXD) to pin 3 (TXD) on the DB9 connector with a jumper wire
(or short TX to RX on screw terminals). This makes every byte you transmit
come straight back to you.

```
python tools\serial_loopback_test.py --port COM7 --baud 19200
```

Expected: `ALL 5 LOOPBACK TESTS PASSED`

If you get TIMEOUT: check the jumper wire / pins.
If you get MISMATCH: try a different baud rate.

---

## Step 2 — Wire to PandaBox (RS-232 crossover)

Remove the loopback jumper. Connect to PandaBox DB25 J1:

```
Simulator side (USB-RS232 DB9)      PandaBox DB25 J1
──────────────────────────────      ─────────────────
Pin 2  RXD  ──────────────────────  Pin 14  TXD  (PandaBox transmits)
Pin 3  TXD  ──────────────────────  Pin 15  RXD  (PandaBox receives)
Pin 5  GND  ──────────────────────  Pin 11  GND  (mandatory reference)
```

**GND is mandatory.** Without it, RS-232 signal levels reference nothing and
neither end reads reliably.

Hardware note: the BL13232 RS-232 transceiver (U603 area) is enabled by
PE5=HIGH in firmware. The SIT3088EESA RS-485 transceiver (U4/U104) is
enabled by PE6=HIGH. Only one should be active at a time — Leo's firmware
keeps PE5 high for LCR Port 1 RS-232 operation.

---

## Step 3 — Confirm the physical link with the heartbeat tool

```
python tools\serial_heartbeat.py --port COM7 --baud 19200
```

This sends one line per second. On the PandaBox debug console (USART0,
115200 8N1) you should see the bytes arriving on the LCR port RX pin.
The heartbeat tool also prints any bytes PandaBox sends back.

If nothing arrives: check GND, try swapping TX↔RX wires.
If garbage arrives: baud rate mismatch — try 9600 then 38400.

---

## Step 4 — Start the full LCP simulator

Once the heartbeat confirms both directions:

```
python app.py
```

Open `http://localhost:5000/serial` in your browser.

In the Serial Bridge panel:
1. **Port**: COM7 (or whichever port your converter is on)
2. **Baud**: 19200 (must match PandaBox USART1/2 config)
3. **Node**: 1 (PandaBox default LCR node)
4. **Meter**: LCR-II (or LCR-600 / LCR.iQ)
5. Click **Connect**

The Live Monitor shows every LCP frame from PandaBox (← RX, blue)
and every response the simulator sends back (→ TX, orange).
The 7E 7E sync bytes are highlighted orange in the hex dump.
The LCP Status banner shows live meter state: node, machine state
(RUN/STOP/END), gross qty, flow rate, totalizer, preset, and frame counts.

---

## Step 5 — Verify PandaBox session establishment

PandaBox initiates the LCR session by sending:
```
7E 7E  [to=node]  [from=0x14]  [status=0x02]  [len=01]  [00]  [crc_lo crc_hi]
```
This is a Get Product ID command with `status=LCP_ST_SYNC` (0x02).

The simulator responds:
```
7E 7E  [to=0x14]  [from=node]  [status=0x80]  [len=0D]
  [00]           ← RC_OK
  [02]           ← device type
  [SR200b2.05]  ← product ID string + null
  [crc_lo crc_hi]
```

After that PandaBox polls fields #2, #4, #17, #18, #100, #101 every ~1 second
and the Live Monitor will show a steady stream of GetField frames and responses.

---

## LCP field reference (polled by PandaBox)

| Field | Name | Format | Units |
|---|---|---|---|
| #2 | GrossQty | int32 BE | tenths of gallon |
| #4 | FlowRate | int32 BE | tenths of gal/min |
| #5 | GrossPreset | int32 BE | tenths of gallon (written by PandaBox) |
| #17 | GrossTotal | int32 BE | tenths of gallon |
| #18 | NetTotal | int32 BE | tenths of gallon |
| #100 | PrevGross | int32 BE | totalizer at start of last delivery |
| #101 | PrevNet | int32 BE | (always 0) |

---

## IssueCommand codes (sent by PandaBox)

| Code | Action |
|---|---|
| 0 | Start delivery / Resume from pause |
| 1 | Pause delivery |
| 2 | End delivery + generate ticket |
| 6 | Print last ticket |

---

## Baud rate reference

| Connection | Baud |
|---|---|
| PandaBox LCR Port 1/2 (USART1/2) | **19200 8N1** |
| PandaBox debug console (USART0) | 115200 8N1 |
| Loopback test (converter self-test) | any — use 19200 to match |

---

## Checklist

- [ ] USB converter appears in Device Manager as COMx (Step 0)
- [ ] Driver installed (Step 0)
- [ ] Loopback test passes at 19200 baud (Step 1)
- [ ] GND connected between PC and PandaBox J1 pin 11 (Step 2)
- [ ] Heartbeat visible on PandaBox console (Step 3)
- [ ] Heartbeat script shows PandaBox TX bytes arriving (Step 3)
- [ ] `python app.py` starts without error (Step 4)
- [ ] Serial Monitor shows port CONNECTED (Step 4)
- [ ] LCP session starts: GetProductID frame + response in monitor (Step 5)
- [ ] Poll frames (#2, #4, #17...) visible at ~1 Hz (Step 5)
