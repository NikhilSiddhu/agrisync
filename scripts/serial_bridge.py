"""USB serial bridge: ESP32 -> Pi, no network involved.

The board already prints every reading to serial as:

    [payload] {"device_id":"esp32-smoke-01","air_temp_c":25.2,...}

This reads those lines and POSTs them to the local API. On the bench, where the
board is powered from the Pi's USB anyway, that cable is a far more reliable
path than WiFi - no ARP, no access point, no firewall, no client isolation.

    sudo systemctl start agrisync-serial-bridge
    journalctl -u agrisync-serial-bridge -f

WHY THIS EXISTS

WiFi is a bench convenience that never ships. The field box runs on 4G through
the Cloudflare tunnel. Debugging a wireless path you are about to delete is
wasted effort, and flaky wireless client-to-client bridging cost a full day of
chasing firewall rules that were never the problem.

IT ALSO REPLACES THE SERIAL MONITOR

Every line the board prints is logged, so `journalctl -f` shows you exactly what
`pio device monitor` would. Only one process can hold a serial port, so stop this
service before running the monitor, or just read the journal instead.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

try:
    import serial
except ImportError:
    print("pyserial missing. Fix with:  pip install pyserial", file=sys.stderr)
    sys.exit(1)

from src.config import Config

PORT = os.getenv("SERIAL_PORT", "/dev/ttyUSB0")
BAUD = int(os.getenv("SERIAL_BAUD", "115200"))
API = os.getenv("BRIDGE_API", "http://127.0.0.1:5000/api/v1/sensors/readings")
PAYLOAD_PREFIX = "[payload] "
RECONNECT_WAIT_S = 5


def log(msg):
    print(f"{datetime.now(timezone.utc):%H:%M:%S} {msg}", flush=True)


def parse_payload(line):
    """Extract a reading from one serial line, or None.

    Split out so it can be tested without a serial port. The cases that matter
    are all real: boot banners, I2C scan output, and truncated lines from
    connecting mid-transmission.
    """
    if not line.startswith(PAYLOAD_PREFIX):
        return None
    try:
        payload = json.loads(line[len(PAYLOAD_PREFIX):])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not payload.get("device_id"):
        return None
    return payload


def forward(payload, session):
    """POST one reading. Returns True on success."""
    try:
        resp = session.post(
            API,
            json=payload,
            headers={"X-API-Key": Config.DEVICE_API_KEY},
            timeout=10,
        )
    except Exception as exc:
        log(f"POST failed: {exc}")
        return False

    if resp.status_code == 201:
        body = resp.json()
        flags = body.get("quality_flags") or []
        note = f"  flags={flags}" if flags else ""
        log(f"stored  ts_source={body.get('ts_source')}{note}")
        return True

    if resp.status_code == 401:
        log("401 UNAUTHORIZED - DEVICE_API_KEY in .env does not match the board's")
    else:
        log(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return False


def open_port():
    """Open the serial port WITHOUT asserting DTR or RTS.

    This is not optional. On most ESP32 dev boards those two lines are wired
    through transistors to EN and GPIO0. Opening the port normally asserts
    them, which resets the chip and can park it in the ROM bootloader - the
    symptom is a flood of undecodable bytes at the correct baud rate, which
    looks exactly like a baud mismatch and is not.

    Same reason `pio device monitor` needs --rts 0 --dtr 0.

    Setting these on a closed Serial object stores them, and pyserial applies
    them at open. Setting them after open is too late; the board has already
    been reset by then.
    """
    ser = serial.Serial()
    ser.port = PORT
    ser.baudrate = BAUD
    ser.timeout = 5
    ser.dtr = False
    ser.rts = False
    ser.open()
    return ser


def looks_like_garbage(line):
    """Undecodable bytes come back as U+FFFD. A line that is mostly those
    means the link is misconfigured, not that the board said something odd."""
    if not line:
        return False
    return line.count("\ufffd") > len(line) / 3


def run_once(session):
    """Open the port and pump lines until it disappears."""
    log(f"opening {PORT} at {BAUD} (DTR/RTS deasserted)")
    ser = open_port()

    # The board is mid-cycle; drop whatever partial line is buffered.
    time.sleep(0.3)
    ser.reset_input_buffer()
    log("connected, waiting for payloads")

    sent = failed = garbage = 0
    warned = False

    while True:
        raw = ser.readline()
        if not raw:
            continue  # timeout, board is just between cycles

        line = raw.decode("utf-8", errors="replace").rstrip()
        if not line:
            continue

        # Do not log or spin on a misconfigured link. Warn once, then stay quiet.
        if looks_like_garbage(line):
            garbage += 1
            if garbage > 50 and not warned:
                warned = True
                log("=" * 60)
                log("UNREADABLE SERIAL. The bytes are not valid UTF-8.")
                log("Almost always one of:")
                log("  1. the board is in ROM bootloader mode - unplug the USB,")
                log("     wait 5s, plug back in, restart this service")
                log(f"  2. wrong baud - firmware uses 115200, this is set to {BAUD}")
                log("  3. another process is fighting for the port")
                log("=" * 60)
            time.sleep(0.05)   # do not burn a core on noise
            continue

        if garbage and not looks_like_garbage(line):
            log(f"link recovered after {garbage} unreadable lines")
            garbage = 0
            warned = False

        # Echo everything, so the journal doubles as the serial monitor.
        log(f"| {line}")

        payload = parse_payload(line)
        if payload is None:
            if line.startswith(PAYLOAD_PREFIX):
                log("unusable payload line, skipping")
            continue

        if forward(payload, session):
            sent += 1
        else:
            failed += 1

        if (sent + failed) % 10 == 0:
            log(f"-- {sent} stored, {failed} failed --")


def main():
    if not Config.DEVICE_API_KEY or Config.DEVICE_API_KEY.startswith("change-me"):
        log("DEVICE_API_KEY is not set in .env - every POST would 401")
        return 1

    session = requests.Session()

    while True:
        try:
            run_once(session)
        except serial.SerialException as exc:
            # Board unplugged, or someone opened the monitor on the same port.
            log(f"serial error: {exc}")
        except KeyboardInterrupt:
            log("stopping")
            return 0
        except Exception as exc:
            log(f"unexpected: {type(exc).__name__}: {exc}")

        log(f"retrying in {RECONNECT_WAIT_S}s")
        time.sleep(RECONNECT_WAIT_S)


if __name__ == "__main__":
    sys.exit(main())
