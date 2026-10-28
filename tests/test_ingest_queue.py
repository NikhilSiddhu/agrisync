"""Readings that arrive late, twice, or without a clock (firmware 1.5.0).

    pytest -q tests/test_ingest_queue.py
"""
import time
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_api import TEST_KEY, app, client  # noqa: F401  (fixtures)

URL = "/api/v1/sensors/readings"
H = {"X-API-Key": TEST_KEY}


def post(client, **payload):
    payload.setdefault("device_id", "box")
    payload.setdefault("air_temp_c", 22.0)
    payload.setdefault("humidity_pct", 80.0)
    return client.post(URL, json=payload, headers=H)


def rows(client):
    return client.get("/api/v1/sensors/readings?limit=50").get_json()["readings"]


# --- each reading stored once ---

def test_resent_reading_with_device_time_is_stored_once(client):
    """The board never saw the reply, so it sends the same reading again."""
    t = int(time.time()) - 600
    first = post(client, sampled_at=t, boot=4, up_s=1200)
    again = post(client, sampled_at=t, boot=4, up_s=1200, queued=1)
    assert first.status_code == 201
    assert again.status_code == 200
    assert again.get_json()["status"] == "duplicate"
    assert len(rows(client)) == 1


def test_resent_reading_without_clock_is_matched_by_boot_and_uptime(client):
    post(client, boot=7, up_s=300)
    again = post(client, boot=7, up_s=300, queued=1)
    assert again.status_code == 200
    assert len(rows(client)) == 1


def test_different_readings_at_close_times_are_both_kept(client):
    t = int(time.time()) - 900
    post(client, sampled_at=t, boot=1, up_s=100)
    post(client, sampled_at=t + 300, boot=1, up_s=400)
    assert len(rows(client)) == 2


def test_old_firmware_without_any_identity_is_always_stored(client):
    post(client)
    post(client)
    assert len(rows(client)) == 2


# --- queued readings placed in time ---

def test_queued_reading_without_clock_is_placed_by_uptime(client):
    """Taken before the clock was set, sent after: placed against a reading
    from the same power-up whose time is known."""
    now = int(time.time())
    post(client, sampled_at=now - 60, boot=9, up_s=5000)           # the reference
    resp = post(client, boot=9, up_s=5000 - 3600, queued=1)         # an hour earlier
    body = resp.get_json()
    assert resp.status_code == 201
    assert body["ts_source"] == "device_uptime"
    placed = datetime.fromisoformat(body["ts"])
    want = datetime.fromtimestamp(now - 60 - 3600, tz=timezone.utc)
    assert abs((placed - want).total_seconds()) < 2
    row = [r for r in rows(client) if r.get("ts_source") == "device_uptime"][0]
    assert row["backfilled"] is True
    assert "quality_flags" not in row


def test_live_server_stamp_is_a_reference_too(client):
    post(client, boot=3, up_s=7200)                                   # live, no clock
    body = post(client, boot=3, up_s=7200 - 900, queued=1).get_json()
    assert body["ts_source"] == "device_uptime"


def test_queued_reading_from_another_boot_is_not_placed_against_this_one(client):
    post(client, sampled_at=int(time.time()), boot=2, up_s=100)
    body = post(client, boot=1, up_s=50, queued=1).get_json()
    assert body["ts_source"] == "server_queued"
    assert "ts:unknown" in body["quality_flags"]


def test_queued_reading_with_unknown_time_stays_out_of_clean_data(client):
    post(client, queued=1)
    assert client.get("/api/v1/sensors/readings?clean=1").get_json()["count"] == 0
    assert client.get("/api/v1/sensors/readings").get_json()["count"] == 1


def test_queued_reading_with_its_own_clock_keeps_it(client):
    t = int(time.time()) - 86400 * 3
    body = post(client, sampled_at=t, boot=5, up_s=10, queued=1).get_json()
    assert body["ts_source"] == "device"


# --- how far back a device time is trusted ---

def test_three_weeks_late_is_still_trusted(client):
    """The 1.5.0 queue holds about two weeks; 14 days was too short."""
    t = int(time.time()) - 86400 * 21
    assert post(client, sampled_at=t).get_json()["ts_source"] == "device"


def test_beyond_thirty_days_falls_back_to_server(client):
    t = int(time.time()) - 86400 * 40
    assert post(client, sampled_at=t).get_json()["ts_source"] == "server_device_clock_behind"


# --- boot facts stored ---

def test_boot_uptime_and_reset_reason_are_stored(client):
    post(client, boot=12, up_s=42, reset="brownout", legacy=3340)
    row = rows(client)[0]
    assert row["boot"] == 12 and row["up_s"] == 42
    assert row["reset_reason"] == "brownout"
    assert row["legacy"] == 3340


def test_non_integer_boot_is_rejected(client):
    assert post(client, boot="soon").status_code == 400


# --- a real leaf wetness sensor ---

def test_raw_millivolts_are_stored_but_unused_until_calibrated(client, monkeypatch):
    monkeypatch.delenv("LW_DRY_MV", raising=False)
    monkeypatch.delenv("LW_WET_MV", raising=False)
    post(client, leaf_wet_mv=812.5)
    row = rows(client)[0]
    assert row["leaf_wet_mv"] == 812.5
    assert "leaf_wet_pct" not in row and "leaf_wet_sensor" not in row


@pytest.mark.parametrize("dry, wet, mv, pct, is_wet", [
    (300, 1500, 300, 0.0, False),      # rises as it wets
    (300, 1500, 900, 50.0, True),
    (300, 1500, 450, 12.5, False),
    (2800, 900, 2420, 20.0, True),     # a resistive grid falls as it wets
    (300, 1500, 2000, 100.0, True),    # clipped
])
def test_calibrated_sensor_gives_percent_and_wet_flag(client, monkeypatch, dry, wet, mv, pct, is_wet):
    monkeypatch.setenv("LW_DRY_MV", str(dry))
    monkeypatch.setenv("LW_WET_MV", str(wet))
    monkeypatch.delenv("LW_WET_THRESHOLD_PCT", raising=False)
    post(client, leaf_wet_mv=mv)
    row = rows(client)[0]
    assert row["leaf_wet_pct"] == pytest.approx(pct, abs=0.05)
    assert row["leaf_wet_sensor"] is is_wet
    # The proxy keeps its own field and meaning.
    assert row["leaf_wetness_source"] in ("proxy", "air_proxy")
