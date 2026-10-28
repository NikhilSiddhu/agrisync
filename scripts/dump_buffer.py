"""Copy every reading queued in the box's flash to a file. Deletes nothing.

    sudo systemctl stop agrisync-serial-bridge     # it holds the port
    python -m scripts.dump_buffer                  # default /dev/ttyUSB0
    python -m scripts.recover_buffer data/dumps/<file>.jsonl

Sends the board one 'D', which prints the queue as [payload] lines (firmware
1.4.0) or [payload] and [legacy] lines (1.5.0), and saves them as JSON lines
with a header recording when the dump started. It never sends 'C'. Clearing
the board is a separate, deliberate step, done only once the recovery has
been checked.

Why the bridge must be stopped: the bridge forwards every [payload] line to
the API as a live reading. Readings from 1.4.0 carry no time, so the server
would stamp three weeks of field data with the minute of the dump.
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PREFIXES = ("[payload] ", "[legacy] ")
END = re.compile(r"\[dump\] end, (\d+) readings")
OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "dumps"


def bridge_running():
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "agrisync-serial-bridge"],
                           timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def collect(lines, started):
    """(payload lines, expected count or None, saw end) from the board's output."""
    inside, got, expected, ended = False, [], None, False
    for line in lines:
        line = line.strip()
        if line.startswith("[dump] begin"):
            inside, got = True, []
            continue
        m = END.search(line)
        if m:
            expected, ended = int(m.group(1)), True
            break
        if line.startswith("[dump] empty"):
            expected, ended = 0, True
            break
        if inside and line.startswith(PREFIXES):
            kind = "legacy" if line.startswith("[legacy] ") else "queued"
            body = line.split("] ", 1)[1]
            try:
                obj = json.loads(body)
            except json.JSONDecodeError:
                continue
            got.append((kind, obj))
    return got, expected, ended


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", default=os.getenv("SERIAL_PORT", "/dev/ttyUSB0"))
    # The board reads commands between cycles, and a cycle that cannot send
    # retries for up to about five minutes, so allow longer than that.
    ap.add_argument("--quiet-s", type=float, default=420.0,
                    help="give up after this many seconds with nothing new")
    ap.add_argument("--force", action="store_true", help="run even if the bridge looks active")
    args = ap.parse_args(argv)

    if bridge_running() and not args.force:
        print("agrisync-serial-bridge is running and would forward the dump as live readings.")
        print("Stop it first:  sudo systemctl stop agrisync-serial-bridge")
        return 1

    os.environ["SERIAL_PORT"] = args.port
    from scripts import serial_bridge
    serial_bridge.PORT = args.port
    ser = serial_bridge.open_port()
    ser.timeout = 1

    started = datetime.now(timezone.utc)
    time.sleep(1.0)
    ser.reset_input_buffer()
    ser.write(b"D")
    ser.flush()
    print("asked the board for its queue. It answers between cycles, so this can take five minutes.")

    raw, last = [], time.time()
    while time.time() - last < args.quiet_s:
        b = ser.readline()
        if not b:
            continue
        last = time.time()
        line = b.decode("utf-8", errors="replace")
        raw.append(line)
        if END.search(line) or "[dump] empty" in line:
            break
    ser.close()

    got, expected, ended = collect(raw, started)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    device = got[0][1].get("device_id", "box") if got else "box"
    stem = f"{device}-{started:%Y%m%d-%H%M}"
    (OUT_DIR / f"{stem}.log").write_text("".join(raw))
    meta = {"_dump": {"started": started.isoformat(), "port": args.port, "device_id": device,
                      "expected": expected, "received": len(got)}}
    with open(OUT_DIR / f"{stem}.jsonl", "w") as f:
        f.write(json.dumps(meta) + "\n")
        for kind, obj in got:
            f.write(("[legacy] " if kind == "legacy" else "[payload] ") + json.dumps(obj) + "\n")

    print(f"saved {len(got)} readings to {OUT_DIR / (stem + '.jsonl')}")
    if not ended:
        print("WARNING: the board never said the dump ended. Run it again before trusting this file.")
        return 2
    if expected is not None and expected != len(got):
        print(f"WARNING: the board said {expected}, {len(got)} arrived intact. Run it again.")
        return 2
    print("complete. Nothing was deleted from the board.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
