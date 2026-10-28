"""Placing a 1.4.0 board's stuck readings back in time.

A simulated box runs for a fortnight: five-minute cycles, sends that fail in
bursts, replies that get lost, and power cuts that reset its counters. The
recovery must put what it can within minutes of the truth and say plainly
what it could not.

    pytest -q tests/test_recover_buffer.py
"""
import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from scripts import recover_buffer as rb

T0 = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)


def simulate(days=14, seed=1, outages=((3.2, 50), (8.6, 140)), ghost_p=0.03):
    """(dump lines, received readings, true time of each dump line)."""
    rnd = random.Random(seed)
    t, tips, buf, truth, recv = T0, 0, [], [], []
    failing = False
    cut = [(T0 + timedelta(days=d), timedelta(minutes=m)) for d, m in outages]
    while t < T0 + timedelta(days=days):
        # A power cut: the box is dead for a while, then boots afresh.
        if cut and t >= cut[0][0]:
            t += cut[0][1]
            cut.pop(0)
            tips = 0
        hour = (t + timedelta(hours=5.5)).hour
        night = hour >= 20 or hour < 6
        p_fail = (0.85 if failing else (0.12 if night else 0.03))
        failing = rnd.random() < p_fail
        tips += rnd.randint(500, 4000)                  # the chattering plate
        reading = {
            "device_id": "box", "firmware": "1.4.0",
            "air_temp_c": round(22 + 6 * math.sin((t.hour - 9) / 24 * 2 * math.pi) + rnd.gauss(0, .3), 2),
            "humidity_pct": round(rnd.uniform(40, 99), 5),
            "pressure_hpa": round(rnd.uniform(988, 996), 4),
            "rain_tips": tips, "buffered_count": len(buf),
        }
        if failing:
            buf.append(reading)
            truth.append(t)
            if rnd.random() < ghost_p:                   # it landed; the reply did not
                recv.append({**reading, "ts": t + timedelta(seconds=rnd.uniform(20, 70)),
                             "count": reading["buffered_count"]})
            took = sum(rnd.uniform(5, 70) for _ in range(4)) + 7.5
        else:
            recv.append({**reading, "ts": t + timedelta(seconds=rnd.uniform(3, 25)),
                         "count": reading["buffered_count"]})
            took = rnd.uniform(5, 30)
        t += timedelta(seconds=max(300.0, took) + rnd.uniform(0, 2))
    return buf, recv, truth


def errors(results, truth, keep):
    out = []
    for r, t in zip(results, truth):
        if r["ts"] is not None and keep(r):
            out.append(abs((r["ts"] - t).total_seconds()))
    return sorted(out)


@pytest.fixture(scope="module")
def run():
    lines, recv, truth = simulate()
    results, cycle = rb.place(lines, recv)
    return lines, recv, truth, results, cycle


def test_the_simulation_is_a_fair_test(run):
    lines, recv, _, results, _ = run
    assert len(lines) > 400                         # plenty stuck in flash
    assert any(r["dup"] for r in results)           # some lost replies
    assert sum(1 for r in recv if r["rain_tips"] < 4000) >= 2   # reboots happened


def test_lines_between_two_received_readings_land_within_minutes(run):
    _, _, truth, results, _ = run
    err = errors(results, truth, lambda r: r["how"] == "between" and not r["dup"])
    assert len(err) > 200
    assert err[len(err) // 2] < 90                  # median under a minute and a half
    assert err[int(len(err) * .95)] < 300           # 95% within five minutes
    assert err[-1] < 900


def test_every_trusted_line_is_close(run):
    """Whatever is stored without ts:approximate goes into the models, so it
    has to be right."""
    _, _, truth, results, _ = run
    err = errors(results, truth, lambda r: not r["approx"] and not r["dup"])
    assert err[-1] < 1200, err[-20:]


def test_dead_time_is_never_smeared_across_a_gap(run):
    """Lines next to a power cut are counted from the side they belong to."""
    _, _, truth, results, _ = run
    one_sided = [r for r in results if r["how"] in ("after", "before") and not r["dup"]]
    assert one_sided, "the outages should force one-sided placement"
    err = errors(results, truth, lambda r: r["how"] in ("after", "before") and not r["approx"])
    assert err and err[-1] < 1200


def test_the_cycle_is_learned_from_the_data(run):
    cycle = run[4]
    assert 295 <= cycle <= 340


def test_a_reply_that_was_lost_is_not_stored_twice(run):
    lines, recv, _, results, _ = run
    dups = [r for r in results if r["dup"]]
    keyed = {(r["count"], r["rain_tips"]) for r in recv}
    for r in dups:
        assert (r["payload"]["buffered_count"], r["payload"]["rain_tips"]) in keyed


def test_nothing_is_placed_outside_the_record(run):
    _, recv, _, results, _ = run
    lo = min(r["ts"] for r in recv) - timedelta(hours=1)
    hi = max(r["ts"] for r in recv) + timedelta(hours=6)
    for r in results:
        if r["ts"] is not None:
            assert lo <= r["ts"] <= hi


def test_a_trimmed_queue_is_matched_from_the_newest_end():
    """1.4.0 dropped the oldest half when its buffer filled: both the lines
    and the received counts restart lower, and the last parts still match."""
    lines, recv, truth = simulate(days=6, seed=4, outages=())
    cut = len(lines) // 2
    kept = lines[cut:]
    n_old = len(kept)
    after = []
    t = truth[-1] + timedelta(minutes=5)
    for k in range(30):                                  # new failures after the trim
        after.append({"device_id": "box", "air_temp_c": 20.0, "humidity_pct": 90.0 + k / 100,
                      "pressure_hpa": 990.0, "rain_tips": 10 ** 7 + k, "buffered_count": n_old + k})
    late = [{"device_id": "box", "air_temp_c": 21.0, "humidity_pct": 50.0, "pressure_hpa": 990.5,
             "rain_tips": 10 ** 7 - 1, "buffered_count": n_old, "count": n_old,
             "ts": t - timedelta(minutes=5)},
            {"device_id": "box", "air_temp_c": 21.0, "humidity_pct": 51.0, "pressure_hpa": 990.6,
             "rain_tips": 10 ** 7 + 40, "buffered_count": n_old + 30, "count": n_old + 30,
             "ts": t + timedelta(minutes=5 * 30)}]
    results, _ = rb.place(kept + after, recv + late)
    new = results[n_old:]
    assert all(r["how"] == "between" for r in new)
    for k, r in enumerate(new):
        want = t + timedelta(minutes=5 * k)
        assert abs((r["ts"] - want).total_seconds()) < 60


def test_parse_dump_reads_serial_logs_and_saved_files():
    raw = ["=== AgriSync Board A fw 1.4.0 ===", "[dump] begin",
           '[payload] {"device_id":"box","air_temp_c":20.5,"buffered_count":0}',
           "garbage", '[legacy] {"device_id":"box","air_temp_c":21,"buffered_count":1}',
           '{"_dump": {"started": "2026-12-01T10:00:00+00:00", "device_id": "box"}}',
           "[dump] end, 2 readings."]
    meta, payloads = rb.parse_dump(raw)
    assert [p["buffered_count"] for p in payloads] == [0, 1]
    assert meta["device_id"] == "box"


def test_lines_after_the_last_received_reading_count_back_from_the_dump():
    """The box brought home: no reading received after these, and a reboot
    on the way. The newest lines are placed back from when the dump began."""
    base = T0
    recv = [{"device_id": "box", "air_temp_c": 20.0, "humidity_pct": 60.0, "pressure_hpa": 990.0,
             "rain_tips": 1000, "buffered_count": 0, "count": 0, "ts": base}]
    lines = [{"device_id": "box", "air_temp_c": 20.0 + k / 10, "humidity_pct": 70.0, "pressure_hpa": 990.0,
              "rain_tips": 2000 + k, "buffered_count": k} for k in range(10)]
    # Power cut, then the bench: counters restart.
    lines += [{"device_id": "box", "air_temp_c": 25.0, "humidity_pct": 40.0 + k, "pressure_hpa": 991.0,
               "rain_tips": 5 + k, "buffered_count": 10 + k} for k in range(6)]
    dump = base + timedelta(days=9)
    results, cycle = rb.place(lines, recv, cycle_s=300, dump_time=dump)
    first, bench = results[:10], results[10:]
    for k, r in enumerate(first, start=1):
        assert r["how"] == "after"
        assert r["ts"] == base + timedelta(seconds=300 * k)
    for k, r in enumerate(bench):
        assert r["how"] == "before dump"
        assert r["ts"] == dump + timedelta(seconds=300) - timedelta(seconds=300 * (6 - k))


def test_a_reboot_just_after_the_left_reading_counts_back_from_the_right():
    """The box lost power right after a reading went through. Everything it
    queued after the restart belongs against the next reading received."""
    def reading(k, tips):
        return {"device_id": "box", "air_temp_c": 20.0 + k / 10, "humidity_pct": 70.0, "pressure_hpa": 990.0,
                "rain_tips": tips, "buffered_count": k}
    left = {**reading(0, 9_000_000), "count": 0, "ts": T0}
    lines = [reading(k, 100 + k) for k in range(8)]             # after the power cut
    right = {**reading(8, 200), "count": 8, "ts": T0 + timedelta(hours=5)}
    results, _ = rb.place(lines, [left, right], cycle_s=300)
    for k, r in enumerate(results):
        assert r["how"] == "before"
        assert r["ts"] == right["ts"] - timedelta(seconds=300 * (8 - k))


def test_write_is_idempotent(run):
    import mongomock
    lines, recv, _, results, _ = run
    db = mongomock.MongoClient()["t"]
    stored, skipped = rb.write(db, results[:50], "dump-a.jsonl")
    again, skipped2 = rb.write(db, results[:50], "dump-a.jsonl")
    assert stored > 0 and again == 0 and skipped2 == stored
    doc = db.readings.find_one({"recovered.dump": "dump-a.jsonl"})
    assert doc["ts_source"] == "recovered"
    assert doc["backfilled"] is True
    approx = [r for r in results[:50] if r["approx"] and not r["dup"] and r["ts"] is not None]
    flagged = db.readings.count_documents({"quality_flags": "ts:approximate"})
    assert flagged == len(approx)
