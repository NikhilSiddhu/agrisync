"""Put the readings stuck in the box's flash back at the right times.

WHY THEY ARE STUCK

Firmware 1.4.0 on cellular never sent its queue: the flush checked for WiFi,
which is switched off in the field. It also never knew the time, because the
modem's clock was never copied to the board. So every reading that failed to
send sits in flash with no timestamp. On 22 Oct 2026 that was 3,340 readings,
including a whole week (3 to 10 Oct) and many of the night hours the server
is missing.

HOW THEY CAN STILL BE PLACED

Every reading the server DID receive carries buffered_count: how many lines
were in the queue when it was taken. The queue only grew. So the lines
numbered a .. b-1 were all taken between the last received reading that saw
a and the first that saw b, one per five-minute cycle, and spacing them
evenly across that gap puts each within a few minutes of its true time.

rain_tips makes it safer. It counts from zero at every power-up, so a drop
between two lines marks a reboot, and the dead time around a reboot is never
smeared across the lines. Lines on either side are counted back or forward
from the nearest received reading instead.

Anything placed from one side only and more than three hours from its
reference is stored with the quality flag ts:approximate, which keeps it out
of every model. Nothing is guessed silently.

    1. Capture the dump (serial bridge stopped):
           python -m scripts.dump_buffer
    2. Dry run, prints what it would do:
           python -m scripts.recover_buffer data/dumps/<file>.jsonl
    3. Write:
           python -m scripts.recover_buffer data/dumps/<file>.jsonl --write

Nothing is written without --write, running it twice stores nothing twice,
and nothing is ever deleted from the board.
"""
import argparse
import json
import statistics
import sys
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# One cycle of the field firmware. Measured from the data where possible.
DEFAULT_CYCLE_S = 300.0
# A gap whose spacing is this far from the usual cycle had dead time in it.
TIGHT_TOLERANCE = 0.25
# Counted from one side only, a line this many cycles from its reference is
# too uncertain for the models (about three hours at five minutes).
MAX_ONE_SIDED = 36
# Fields that together identify one reading. rain_tips is a free-running
# counter, so two different cycles never share it.
IDENTITY = ("air_temp_c", "humidity_pct", "pressure_hpa", "rain_tips")
PREFIXES = ("[payload] ", "[legacy] ")


def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_dump(lines):
    """(meta, payloads) from a capture: raw JSON lines or serial [payload] lines."""
    meta, out = {}, []
    for raw in lines:
        line = raw.strip()
        for p in PREFIXES:
            if line.startswith(p):
                line = line[len(p):]
                break
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "_dump" in obj:
            meta = obj["_dump"]
        elif isinstance(obj, dict) and obj.get("device_id"):
            out.append(obj)
    return meta, out


def _same(a, b):
    for k in IDENTITY:
        x, y = a.get(k), b.get(k)
        if x is None and y is None:
            continue
        if x is None or y is None or abs(float(x) - float(y)) > 1e-6:
            return False
    return True


def _epochs(values):
    """Split a sequence where it goes down: the queue was trimmed or cleared."""
    cuts, prev = [0], None
    for i, v in enumerate(values):
        if prev is not None and v < prev:
            cuts.append(i)
        prev = v
    cuts.append(len(values))
    return [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]


def _runs(anchors):
    """Received readings grouped by the queue size they saw, in time order."""
    runs = []
    for a in anchors:
        if runs and runs[-1]["count"] == a["count"]:
            runs[-1]["last"] = a
        else:
            runs.append({"count": a["count"], "first": a, "last": a})
    return runs


def _reboot_between(prev_tips, tips):
    return prev_tips is not None and tips is not None and tips < prev_tips


def place(payloads, anchors, cycle_s=None, dump_time=None):
    """Work out when each dumped line was taken.

    payloads: dumped lines in file order. anchors: received readings, any
    order, each {"ts", "count", ...payload fields}. Returns one dict per line:
    {"slot", "payload", "ts" or None, "how", "approx", "dup"}."""
    anchors = sorted(({**a, "ts": aware(a["ts"])} for a in anchors
                      if a.get("count") is not None), key=lambda a: a["ts"])
    slots = []
    for i, p in enumerate(payloads):
        bc = p.get("buffered_count")
        slots.append(int(bc) if bc is not None else None)
    if any(s is None for s in slots):
        # Older lines without a count: assume the file is the whole queue.
        base = next((s - i for i, s in enumerate(slots) if s is not None), 0)
        slots = [s if s is not None else base + i for i, s in enumerate(slots)]

    results = [{"slot": slots[i], "payload": p, "ts": None, "how": "unplaced",
                "approx": False, "dup": False, "spacing_s": None}
               for i, p in enumerate(payloads)]

    line_epochs = _epochs(slots)
    anchor_epochs = _epochs([a["count"] for a in anchors]) if anchors else []
    # Match epochs from the newest back: the last trim splits both the same way.
    pairs = list(zip(reversed(line_epochs), reversed(anchor_epochs)))

    gaps = []
    for (l0, l1), (a0, a1) in pairs:
        runs = _runs(anchors[a0:a1])
        counts = [r["count"] for r in runs]
        groups = {}
        for i in range(l0, l1):
            n = slots[i]
            j = bisect_right(counts, n) - 1          # last run that saw <= n
            left = runs[j] if j >= 0 else None
            right = runs[j + 1] if j + 1 < len(runs) else None
            g = groups.setdefault(j, {"left": left, "right": right, "lines": [], "ghost": False})
            # A line the server got even though the board thought it failed:
            # the reply was lost, so the board queued its own copy.
            if left is not None and n == left["count"] and _same(payloads[i], left["last"]):
                results[i]["dup"] = True
                results[i]["ts"] = left["last"]["ts"]
                results[i]["how"] = "already stored"
                g["ghost"] = True
                continue
            g["lines"].append(i)
        gaps.extend(groups.values())

    # The usual cycle, from clean two-sided gaps.
    if cycle_s is None:
        seen = []
        for g in gaps:
            left, right = g["left"], g["right"]
            if left is None or right is None or len(g["lines"]) < 3:
                continue
            if _has_reboot(g, payloads):
                continue
            span = (right["first"]["ts"] - left["last"]["ts"]).total_seconds()
            seen.append(span / (right["count"] - left["count"] + (0 if g["ghost"] else 1)))
        cycle_s = statistics.median(seen) if seen else DEFAULT_CYCLE_S

    for g in gaps:
        _place_gap(g, payloads, slots, results, cycle_s, dump_time)
    return results, cycle_s


def _has_reboot(g, payloads):
    prev = g["left"]["last"].get("rain_tips") if g["left"] else None
    for i in g["lines"]:
        t = payloads[i].get("rain_tips")
        if _reboot_between(prev, t):
            return True
        prev = t if t is not None else prev
    if g["right"] is not None and _reboot_between(prev, g["right"]["first"].get("rain_tips")):
        return True
    return False


def _place_gap(g, payloads, slots, results, cycle_s, dump_time):
    left, right, lines = g["left"], g["right"], g["lines"]
    if not lines:
        return
    # Split the lines where the box rebooted. A reboot before the first line
    # means none of them can be counted forward from the left reference.
    segments, cur, lead_reboot = [], [], False
    prev = left["last"].get("rain_tips") if left else None
    for i in lines:
        t = payloads[i].get("rain_tips")
        if _reboot_between(prev, t):
            if cur:
                segments.append(cur)
                cur = []
            elif not segments:
                lead_reboot = True
        cur.append(i)
        prev = t if t is not None else prev
    segments.append(cur)
    tail_reboot = bool(right) and _reboot_between(prev, right["first"].get("rain_tips"))

    if left and right and len(segments) == 1 and not lead_reboot and not tail_reboot:
        t0 = left["last"]["ts"]
        span = (right["first"]["ts"] - t0).total_seconds()
        # Cycles in the gap: one per queued line, plus the received one at
        # the right. A lost reply means the left reading is also line one.
        ghost = 0 if g.get("ghost") else 1
        step = span / (right["count"] - left["count"] + ghost)
        tight = abs(step - cycle_s) <= TIGHT_TOLERANCE * cycle_s
        for i in lines:
            k = slots[i] - left["count"] + ghost
            _set(results[i], t0 + timedelta(seconds=k * step),
                 "between" if tight else "between, uneven", not tight, step)
        return

    # One-sided: count forward from the left reference until the first
    # reboot, and back from the right reference after the last one.
    first, last = segments[0], segments[-1]
    forward = left is not None and not lead_reboot
    if forward:
        t0 = left["last"]["ts"]
        for k, i in enumerate(first, start=1):
            _set(results[i], t0 + timedelta(seconds=k * cycle_s), "after", k > MAX_ONE_SIDED, cycle_s)
    end = None
    if right and not tail_reboot:
        end, how = right["first"]["ts"], "before"
    elif right is None and dump_time is not None:
        # Nothing received after these: the newest was taken within one
        # cycle of the dump, if the box ran without a break until then.
        end, how = dump_time + timedelta(seconds=cycle_s), "before dump"
    if end is not None and (last is not first or not forward):
        n = len(last)
        for k, i in enumerate(last):
            back = n - k
            _set(results[i], end - timedelta(seconds=back * cycle_s), how, back > MAX_ONE_SIDED, cycle_s)


def _set(r, ts, how, approx, step):
    r["ts"], r["how"], r["approx"], r["spacing_s"] = ts, how, approx, round(step, 1)


def anchors_from_db(db, device_id):
    q = {"device_id": device_id, "buffered_count": {"$exists": True},
         "ts_source": {"$in": ["server", "device"]}}
    fields = {"ts": 1, "buffered_count": 1, "_id": 0, **{k: 1 for k in IDENTITY}}
    out = []
    for r in db.readings.find(q, fields):
        out.append({**r, "count": int(r["buffered_count"])})
    return out


def temperature_check(db, placed):
    """How well placed temperatures follow the outside record, hour by hour.

    Placed correctly they track ERA5 and the forecast's past hours closely
    (r well above 0.8); shifted by hours, the daily cycle falls out of step."""
    try:
        from scripts.update_feeds import outside_hours
    except Exception:
        return None
    hours = {}
    for r in placed:
        if r["ts"] is None or r["dup"] or r["payload"].get("air_temp_c") is None:
            continue
        h = r["ts"].replace(minute=0, second=0, microsecond=0)
        hours.setdefault(h, []).append(float(r["payload"]["air_temp_c"]))
    if len(hours) < 24:
        return None
    lo, hi = min(hours), max(hours) + timedelta(hours=1)
    out = outside_hours(db, lo, hi)
    xs, ys = [], []
    for h, v in hours.items():
        o = (out.get(h) or {}).get("temp_c")
        if o is not None:
            xs.append(sum(v) / len(v))
            ys.append(float(o))
    if len(xs) < 24:
        return None
    return round(statistics.correlation(xs, ys), 3), len(xs)


def write(db, results, dump_name, now=None):
    from src.api.sensors import build_reading
    now = now or datetime.now(timezone.utc)
    stored = skipped = 0
    for r in results:
        if r["dup"] or r["ts"] is None:
            continue
        key = {"recovered.dump": dump_name, "recovered.slot": r["slot"]}
        if db.readings.find_one(key, {"_id": 1}):
            skipped += 1
            continue
        doc, error = build_reading(r["payload"], now, db=db, max_backfill_days=3650,
                                   sampled_at=r["ts"], ts_source="recovered")
        if error:
            skipped += 1
            continue
        doc["backfilled"] = True
        doc["recovered"] = {"dump": dump_name, "slot": r["slot"], "how": r["how"],
                            "spacing_s": r["spacing_s"]}
        if r["approx"]:
            doc.setdefault("quality_flags", []).append("ts:approximate")
        db.readings.insert_one(doc)
        stored += 1
    return stored, skipped


def summarise(results, cycle_s):
    total = len(results)
    dup = sum(r["dup"] for r in results)
    unplaced = sum(1 for r in results if r["ts"] is None)
    approx = sum(1 for r in results if r["approx"] and not r["dup"])
    good = total - dup - unplaced - approx
    when = [r["ts"] for r in results if r["ts"] is not None and not r["dup"]]
    lines = [
        f"lines in dump        {total}",
        f"usual cycle          {cycle_s:.0f} s",
        f"placed, trusted      {good}",
        f"placed, approximate  {approx}   (stored with ts:approximate, kept out of models)",
        f"already on server    {dup}",
        f"could not place      {unplaced}",
    ]
    if when:
        lines.append(f"time span            {min(when):%d %b %H:%M} to {max(when):%d %b %H:%M} UTC")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("dump", help="file written by scripts.dump_buffer, or a saved serial log")
    ap.add_argument("--device", default=None, help="device_id, if not in the dump")
    ap.add_argument("--write", action="store_true", help="store the placed readings")
    args = ap.parse_args(argv)

    path = Path(args.dump)
    meta, payloads = parse_dump(path.read_text(errors="replace").splitlines())
    if not payloads:
        print("no readings in that file")
        return 1
    device = args.device or meta.get("device_id") or payloads[0]["device_id"]
    dump_time = None
    if meta.get("started"):
        dump_time = aware(datetime.fromisoformat(meta["started"]))

    from main.app import create_app
    from src.db import get_db
    create_app()
    db = get_db()

    results, cycle_s = place(payloads, anchors_from_db(db, device), dump_time=dump_time)
    print(summarise(results, cycle_s))
    check = temperature_check(db, results)
    if check:
        r, n = check
        verdict = "follows the outside record" if r >= 0.8 else "DOES NOT follow the outside record - do not write"
        print(f"temperature check    r = {r} over {n} hours: {verdict}")
        if r < 0.8 and args.write:
            print("refusing to write. Send the dump file to be looked at.")
            return 2

    if not args.write:
        print("\ndry run. Add --write to store them.")
        return 0
    stored, skipped = write(db, results, path.name)
    print(f"\nstored {stored}, skipped {skipped} already stored")
    return 0


if __name__ == "__main__":
    sys.exit(main())
