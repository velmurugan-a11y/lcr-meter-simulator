#!/usr/bin/env python3
"""
serial_loopback_test.py
─────────────────────────────────────────────────────────────────────────────
STEP 1: Run this BEFORE connecting to PandaBox.

What it does:
  Sends known bytes out the serial port and reads them back. If you get them
  back, the USB converter is working, the OS driver is loaded, pyserial can
  talk to it, and the RS-232 signal levels are correct.

How to use it for the loopback test:
  On the DB9 connector (or the wires from your isolated converter):
    Short pin 2 (RXD) to pin 3 (TXD) with a jumper wire or crocodile clip.
    This makes every byte you transmit come straight back to you.

  If your converter has screw terminals instead of DB9:
    Short the TX wire to the RX wire with a short piece of wire.

Run:
  python3 tools/serial_loopback_test.py --port /dev/ttyUSB0 --baud 115200

  On Windows:  python tools\\serial_loopback_test.py --port COM4 --baud 115200
  On macOS:    python3 tools/serial_loopback_test.py --port /dev/cu.usbserial-xxx --baud 115200

Expected output (pass):
  [1/5] TX: AA BB CC DD EE   RX: AA BB CC DD EE   MATCH
  ALL 5 LOOPBACK TESTS PASSED - USB converter OK, driver OK, wiring OK.

If you get TIMEOUT or MISMATCH:
  See the troubleshooting section printed at the bottom of the run.
─────────────────────────────────────────────────────────────────────────────
"""
import argparse
import sys
import time

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed.")
    print("Run:  pip install pyserial")
    sys.exit(1)


TEST_PAYLOADS = [
    bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE]),
    bytes([0x00, 0xFF, 0x55, 0xAA, 0x01]),
    b"LCR_SIM_LOOPBACK_TEST\r\n",
    bytes(range(0, 32)),
    bytes([0xFF] * 16 + [0x00] * 16),
]


def run_loopback(port: str, baud: int, timeout: float = 1.0):
    print(f"\nOpening {port} at {baud} baud, 8N1 ...")
    try:
        ser = serial.Serial(
            port=port, baudrate=baud, bytesize=8,
            parity="N", stopbits=1, timeout=timeout,
            rtscts=False, dsrdtr=False,
        )
    except serial.SerialException as e:
        print(f"\nFAIL: Could not open {port}")
        print(f"  {e}")
        print_port_help(port)
        sys.exit(1)

    print(f"Port open: {ser.name}\n")
    ser.reset_input_buffer()
    ser.reset_output_buffer()

    passed = 0
    failed = 0
    for i, payload in enumerate(TEST_PAYLOADS, 1):
        ser.write(payload)
        ser.flush()
        received = ser.read(len(payload))

        tx_hex = payload.hex(" ").upper()
        rx_hex = received.hex(" ").upper() if received else "(nothing)"
        match = received == payload
        status = "MATCH" if match else ("TIMEOUT - no data received" if not received else "MISMATCH")
        print(f"[{i}/{len(TEST_PAYLOADS)}]")
        print(f"  TX: {tx_hex[:60]}")
        print(f"  RX: {rx_hex[:60]}")
        print(f"  --> {status}")
        if match:
            passed += 1
        else:
            failed += 1
        time.sleep(0.05)

    ser.close()
    print()
    if failed == 0:
        print("=" * 60)
        print("ALL LOOPBACK TESTS PASSED")
        print("USB converter OK  |  driver OK  |  wiring OK")
        print("You can now connect to PandaBox and start the simulator.")
        print("=" * 60)
    else:
        print("=" * 60)
        print(f"{failed}/{len(TEST_PAYLOADS)} TESTS FAILED")
        print_fail_help(port, baud)
        print("=" * 60)
        sys.exit(1)


def print_port_help(port: str):
    print()
    print("Common causes:")
    if "ttyUSB" in port or "ttyACM" in port:
        print("  - USB converter not plugged in, or driver not loaded")
        print("    Check:  ls /dev/ttyUSB*  or  dmesg | grep tty")
        print("  - Permission denied: add yourself to the dialout group:")
        print("    sudo usermod -aG dialout $USER   (then log out and back in)")
    elif "COM" in port.upper():
        print("  - Port does not exist. Open Device Manager, check under")
        print("    'Ports (COM & LPT)' for the actual COM number.")
        print("  - Driver not installed: look for a yellow ! in Device Manager.")
    print()
    print("Available ports right now:")
    from serial.tools.list_ports import comports
    ports = list(comports())
    if ports:
        for p in ports:
            print(f"  {p.device}  -  {p.description}")
    else:
        print("  (none found - USB converter not seen by OS)")


def print_fail_help(port: str, baud: int):
    print()
    print("Troubleshooting:")
    print("  TIMEOUT (nothing received):")
    print("    - No loopback jumper. Short pin 2 to pin 3 on DB9,")
    print("      or TX wire to RX wire on screw terminal block.")
    print()
    print("  MISMATCH (data arrives but wrong bytes):")
    print(f"    - Wrong baud rate. Current: {baud}")
    print("      Try: --baud 9600  or  --baud 19200  or  --baud 57600")
    print()
    print("  PARTIAL (some bytes missing):")
    print("    - Flow control mismatch. Try with --rtscts or check")
    print("      your converter's hardware flow control jumpers.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RS-232 loopback test for the LCR simulator")
    parser.add_argument("--port", required=True, help="e.g. COM4 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default 115200)")
    parser.add_argument("--timeout", type=float, default=1.0, help="Read timeout seconds")
    args = parser.parse_args()
    run_loopback(args.port, args.baud, args.timeout)
