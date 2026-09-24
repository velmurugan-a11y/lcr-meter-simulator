#!/usr/bin/env python3
"""
serial_heartbeat.py
─────────────────────────────────────────────────────────────────────────────
STEP 2: Run this after the loopback test passes, with PandaBox connected.

What it does:
  Sends a human-readable heartbeat line out the serial port every second.
  PandaBox should be able to see this text arriving on its RX line using
  its own serial monitor or by capturing raw bytes.

  This confirms:
    1. Physical wiring between the simulator machine and PandaBox is correct
    2. Both ends are using the same baud/parity/stopbits
    3. TX from the simulator side is reaching PandaBox's RX pin

  It does NOT test PandaBox's TX → simulator RX (that needs the response
  monitor in the main simulator app once the LCP protocol layer is filled in).

Run:
  python3 tools/serial_heartbeat.py --port /dev/ttyUSB0 --baud 115200

Press Ctrl+C to stop.
─────────────────────────────────────────────────────────────────────────────
"""
import argparse
import sys
import time
import signal

try:
    import serial
except ImportError:
    print("ERROR: pyserial not installed. Run:  pip install pyserial")
    sys.exit(1)


def run_heartbeat(port: str, baud: int, interval: float = 1.0):
    print(f"\nOpening {port} at {baud} baud ...")
    try:
        ser = serial.Serial(port=port, baudrate=baud, bytesize=8,
                            parity="N", stopbits=1, timeout=0.2,
                            rtscts=False, dsrdtr=False)
    except serial.SerialException as e:
        print(f"FAIL: Could not open {port}: {e}")
        sys.exit(1)

    print(f"Port open. Sending heartbeat every {interval}s. Press Ctrl+C to stop.\n")
    count = 0
    try:
        while True:
            count += 1
            ts = time.strftime("%H:%M:%S")
            # Format: a fixed-width line a serial monitor can parse easily.
            # "LCR_SIM" is a sentinel the PandaBox team can grep for to
            # confirm they're receiving from this simulator specifically.
            line = f"LCR_SIM HEARTBEAT #{count:05d} ts={ts} baud={baud}\r\n"
            ser.write(line.encode("ascii"))
            ser.flush()
            print(f"  TX [{count:05d}]: {line.strip()}")

            # Also try to read any bytes PandaBox has sent back
            incoming = ser.read(256)
            if incoming:
                print(f"  RX [{count:05d}]: {incoming.hex(' ').upper()}")
                try:
                    print(f"           ASCII: {incoming.decode('ascii', errors='replace').strip()}")
                except Exception:
                    pass

            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\nStopped after {count} heartbeats.")
    finally:
        ser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Serial heartbeat sender for LCR simulator bringup")
    parser.add_argument("--port", required=True, help="e.g. COM4 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between heartbeats")
    args = parser.parse_args()
    run_heartbeat(args.port, args.baud, args.interval)
