"""The Pi's side of firmware 1.6.0: signed settings in the reply, batches of
queued readings, firmware served a piece at a time, and the faults the
health check now names.

    pytest -q tests/test_device_link.py
"""
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from src import device_link
from src.health_check import assess_device
from tests.test_api import TEST_KEY, app, client  # noqa: F401  (fixtures)

SECRET = "test-hmac-secret-not-real"
H = {"X-API-Key": TEST_KEY}
URL = "/api/v1/sensors/readings"


@pytest.fixture
def linked(app, monkeypatch, tmp_path):  # noqa: F811
    app.config["DEVICE_HMAC_SECRET"] = SECRET
    monkeypatch.setenv("FIRMWARE_DIR", str(tmp_path / "fw"))
    monkeypatch.delenv("OTA_ENABLED", raising=False)
    for k in ("BOX_INTERVAL_S", "BOX_WET_INTERVAL_S", "BOX_BATCH"):
        monkeypatch.delenv(k, raising=False)
    return app.test_client()


def at_ist(hour):
    """A UTC moment that is `hour` o'clock in India."""
    return datetime(2026, 10, 28, hour, 0, tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)


def verify(body):
    """What the box does: recompute the signature over the exact string."""
    want = hmac.new(SECRET.encode(), f"{body['cfg_ts']}.{body['cfg']}".encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, body["cfg_sig"])


def cfg_dict(cfg):
    return dict(kv.split("=", 1) for kv in cfg.split(";"))


def image(n=10000, seed=7):
    b = bytearray([0xE9]) + bytes((i * seed + 3) % 251 for i in range(n - 1))
    return bytes(b)


def publish(tmp_path, monkeypatch, version="1.6.1", data=None):
    from scripts import publish_firmware
    src = tmp_path / "fw.bin"
    src.write_bytes(data if data is not None else image())
    return publish_firmware.publish(src, version, device_link.firmware_dir())


def signed_get(client, path, secret=SECRET, ts=None):
    ts = int(time.time()) if ts is None else ts
    sig = hmac.new(secret.encode(), f"{ts}.{path}".encode(), hashlib.sha256).hexdigest()
    return client.get(path, headers={"X-Sig": f"{ts}.{sig}"})


# --------------------------------------------------------------- interval

def test_interval_is_five_minutes_by_default():
    assert device_link.desired_interval({"humidity_pct": 60}, at_ist(14)) == 300


def test_wet_night_reads_faster():
    assert device_link.desired_interval({"humidity_pct": 95}, at_ist(2)) == 150
    assert device_link.desired_interval({"leaf_wetness": 80, "humidity_pct": 70}, at_ist(21)) == 150


def test_humid_afternoon_does_not_speed_up():
    assert device_link.desired_interval({"humidity_pct": 95}, at_ist(14)) == 300


def test_low_battery_slows_down_even_on_a_wet_night():
    assert device_link.desired_interval({"humidity_pct": 95, "battery_v": 11.8}, at_ist(2)) == 600
    assert device_link.desired_interval({"humidity_pct": 95, "battery_v": 11.4}, at_ist(2)) == 900


def test_interval_from_env_is_clamped(monkeypatch):
    monkeypatch.setenv("BOX_INTERVAL_S", "10")
    assert device_link.desired_interval({}, at_ist(14)) == 60
    monkeypatch.setenv("BOX_INTERVAL_S", "99999")
    assert device_link.desired_interval({}, at_ist(14)) == 1800


# --------------------------------------------------------------- the reply

def test_old_firmware_gets_no_settings(linked):
    r = linked.post(URL, json={"device_id": "box", "firmware": "1.5.0", "air_temp_c": 22, "humidity_pct": 70}, headers=H)
    assert r.status_code == 201
    assert "cfg" not in r.get_json()


def test_new_firmware_gets_signed_settings(linked):
    r = linked.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70}, headers=H)
    body = r.get_json()
    assert r.status_code == 201
    assert verify(body)
    cfg = cfg_dict(body["cfg"])
    assert cfg["v"] == "1" and cfg["b"] == "12" and 60 <= int(cfg["i"]) <= 1800
    assert "ota" not in cfg
    assert abs(body["cfg_ts"] - time.time()) < 5


def test_a_tampered_setting_fails_the_check(linked):
    body = linked.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70},
                       headers=H).get_json()
    body["cfg"] = body["cfg"].replace("i=", "i=6")
    assert not verify(body)


def test_reply_stays_small_enough_for_the_box(linked, tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch)
    monkeypatch.setenv("OTA_ENABLED", "1")
    r = linked.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70,
                               "sampled_at": int(time.time()), "boot": 3, "up_s": 60}, headers=H)
    assert len(r.data) < 700          # the box reads at most 768 bytes of a reply


def test_duplicate_reply_also_carries_settings(linked):
    p = {"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70, "boot": 2, "up_s": 100}
    linked.post(URL, json=p, headers=H)
    again = linked.post(URL, json={**p, "queued": 1}, headers=H).get_json()
    assert again["status"] == "duplicate" and verify(again)


def test_no_settings_without_a_secret(app, client):  # noqa: F811
    r = client.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70}, headers=H)
    assert r.status_code == 201 and "cfg" not in r.get_json()


def test_new_fields_are_stored(linked):
    linked.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70,
                           "free_heap": 201000, "min_heap": 150000, "flash_free_kb": 1200, "interval_s": 150,
                           "cfg_v": 1, "rain_wet_pct": 12.5, "ota_state": "ok:1.6.0"}, headers=H)
    row = linked.get(URL + "?limit=1").get_json()["readings"][0]
    assert row["free_heap"] == 201000 and row["interval_s"] == 150 and row["rain_wet_pct"] == 12.5
    assert row["ota_state"] == "ok:1.6.0"


# --------------------------------------------------------------- batches

def batch_item(i, **kw):
    t = int(time.time()) - 3600 + i * 300
    return {"device_id": "box", "firmware": "1.6.0", "sampled_at": t, "boot": 5, "up_s": 1000 + i * 300,
            "air_temp_c": 20 + i * .1, "humidity_pct": 80, "queued": 1, **kw}


def test_batch_stores_each_reading_once(linked):
    items = [batch_item(i) for i in range(6)]
    r = linked.post(URL + "/batch", json=items, headers=H)
    body = r.get_json()
    assert r.status_code == 200 and body["stored"] == 6 and body["duplicate"] == 0
    again = linked.post(URL + "/batch", json=items, headers=H).get_json()
    assert again["stored"] == 0 and again["duplicate"] == 6
    assert len(linked.get(URL + "?limit=50").get_json()["readings"]) == 6
    assert verify(body)


def test_one_bad_reading_does_not_hold_back_the_batch(linked):
    items = [batch_item(0), {"air_temp_c": 20}, batch_item(1, air_temp_c="hot"), "junk", batch_item(2)]
    body = linked.post(URL + "/batch", json=items, headers=H).get_json()
    assert body["stored"] == 2 and body["refused"] == 3


def test_batch_must_be_a_list_of_sensible_size(linked):
    assert linked.post(URL + "/batch", json={"device_id": "box"}, headers=H).status_code == 400
    assert linked.post(URL + "/batch", json=[], headers=H).status_code == 400
    assert linked.post(URL + "/batch", json=[batch_item(i) for i in range(40)], headers=H).status_code == 413


def test_batch_needs_the_device_signature_or_key(linked):
    assert linked.post(URL + "/batch", json=[batch_item(0)]).status_code == 401


def test_signed_batch_is_accepted(linked):
    raw = json.dumps([batch_item(0)]).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
    r = linked.post(URL + "/batch", data=raw, content_type="application/json", headers={"X-Sig": f"{ts}.{sig}"})
    assert r.status_code == 200 and r.get_json()["stored"] == 1


# --------------------------------------------------------------- firmware

def test_offer_only_when_enabled_and_newer(linked, tmp_path, monkeypatch):
    m = publish(tmp_path, monkeypatch, "1.6.1")
    payload = {"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70}
    assert "ota" not in cfg_dict(linked.post(URL, json=payload, headers=H).get_json()["cfg"])
    monkeypatch.setenv("OTA_ENABLED", "1")
    cfg = cfg_dict(linked.post(URL, json=payload, headers=H).get_json()["cfg"])
    assert cfg["ota"] == "1.6.1" and int(cfg["osz"]) == m["size"] and cfg["osha"] == m["sha256"]
    same = {**payload, "firmware": "1.6.1"}
    assert "ota" not in cfg_dict(linked.post(URL, json=same, headers=H).get_json()["cfg"])


def test_three_failures_stop_the_offer(linked, tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch, "1.6.1")
    monkeypatch.setenv("OTA_ENABLED", "1")
    for i in range(3):
        linked.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22, "humidity_pct": 70,
                               "boot": 10 + i, "up_s": 30, "ota_state": "fail:sha:1.6.1"}, headers=H)
    cfg = cfg_dict(linked.post(URL, json={"device_id": "box", "firmware": "1.6.0", "air_temp_c": 22,
                                          "humidity_pct": 70}, headers=H).get_json()["cfg"])
    assert "ota" not in cfg


def test_chunks_rebuild_the_image(linked, tmp_path, monkeypatch):
    data = image(10000)
    m = publish(tmp_path, monkeypatch, "1.6.1", data)
    got = b""
    for off in range(0, m["size"], 3072):
        r = signed_get(linked, f"/api/v1/firmware/chunk?v=1.6.1&off={off}&len=3072")
        assert r.status_code == 200, r.data
        got += base64.b64decode(r.data)
    assert got == data and hashlib.sha256(got).hexdigest() == m["sha256"]


def test_chunk_signature_covers_the_path(linked, tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch, "1.6.1")
    ts = int(time.time())
    sig = hmac.new(SECRET.encode(), f"{ts}./api/v1/firmware/chunk?v=1.6.1&off=0&len=3072".encode(),
                   hashlib.sha256).hexdigest()
    other = linked.get("/api/v1/firmware/chunk?v=1.6.1&off=3072&len=3072", headers={"X-Sig": f"{ts}.{sig}"})
    assert other.status_code == 401
    assert signed_get(linked, "/api/v1/firmware/chunk?v=1.6.1&off=0&len=3072", secret="wrong").status_code == 401
    assert signed_get(linked, "/api/v1/firmware/chunk?v=1.6.1&off=0&len=3072", ts=ts - 3600).status_code == 401
    assert linked.get("/api/v1/firmware/chunk?v=1.6.1&off=0&len=3072", headers=H).status_code == 401


def test_chunk_bounds_and_versions(linked, tmp_path, monkeypatch):
    publish(tmp_path, monkeypatch, "1.6.1", image(5000))
    assert signed_get(linked, "/api/v1/firmware/chunk?v=1.6.1&off=5000&len=100").status_code == 416
    assert signed_get(linked, "/api/v1/firmware/chunk?v=1.6.1&off=0&len=9999").status_code == 416
    assert signed_get(linked, "/api/v1/firmware/chunk?v=1.6.0&off=0&len=100").status_code == 404
    last = signed_get(linked, "/api/v1/firmware/chunk?v=1.6.1&off=4096&len=3072")
    assert len(base64.b64decode(last.data)) == 904
    assert signed_get(linked, "/api/v1/firmware/manifest").get_json()["version"] == "1.6.1"


def test_publish_refuses_unsafe_images(tmp_path, monkeypatch):
    from scripts import publish_firmware
    monkeypatch.setenv("FIRMWARE_DIR", str(tmp_path / "fw"))
    monkeypatch.setenv("DEVICE_HMAC_SECRET", "a-very-secret-key-123")
    assert publish_firmware.check_image(image()) == []
    leaked = image() + b"xx a-very-secret-key-123 yy"
    assert any("DEVICE_HMAC_SECRET" in p for p in publish_firmware.check_image(leaked))
    assert publish_firmware.check_image(b"\x00" + image()[1:])
    assert publish_firmware.check_image(b"\xe9" + bytes(0x140000))
    with pytest.raises(SystemExit):
        publish(tmp_path, monkeypatch, "1.6.1", leaked)
    assert device_link.manifest() is None
    with pytest.raises(SystemExit):
        publish(tmp_path, monkeypatch, "latest", image())


# --------------------------------------------------------------- faults

def test_health_names_a_stuck_rain_plate_and_a_chattering_counter(app, client):  # noqa: F811
    from src.db import get_db
    db = get_db()
    now = datetime.now(timezone.utc)
    db.readings.insert_many([{"device_id": "box", "ts": now - timedelta(minutes=5 * i), "rain_analog": 210.0,
                              "rain_tips": 53575447.0, "firmware": "1.4.0", "air_temp_c": 22.0,
                              "humidity_pct": 70.0, "leaf_wetness": 0.0} for i in range(120)])
    out = assess_device(db, "box")
    text = " ".join(out["problems"])
    assert "rain plate" in text and "rain counter" in text and out["status"] != "ok"


def test_health_is_quiet_on_a_dry_plate_and_new_firmware(app, client):  # noqa: F811
    from src.db import get_db
    db = get_db()
    now = datetime.now(timezone.utc)
    db.readings.insert_many([{"device_id": "box", "ts": now - timedelta(minutes=5 * i),
                              "rain_analog": 4095.0 if i % 50 else 300.0, "rain_tips": 2_000_000.0,
                              "firmware": "1.6.0", "air_temp_c": 22.0, "humidity_pct": 70.0,
                              "leaf_wetness": 0.0} for i in range(120)])
    text = " ".join(assess_device(db, "box")["problems"])
    assert "rain plate" not in text and "rain counter" not in text
