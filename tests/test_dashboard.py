"""Dashboard tests. No Mongo, no network."""
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import data as ddata


def seed(db, days=7):
    """Realistic diurnal data, naive UTC exactly as pymongo returns it."""
    random.seed(3)
    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = []
    for m in range(days * 24 * 60):
        ts = start + timedelta(minutes=m)
        lh = (ts.hour + ts.minute / 60.0 + 5.5) % 24
        air = 27.0 + 6.5 * math.cos((lh - 15) / 24 * 2 * math.pi)
        rh = max(30.0, min(99.0, 62.0 - 30.0 * math.cos((lh - 15) / 24 * 2 * math.pi)))
        leaf = (air - 3.0 if (lh < 6.5 or lh > 20.5) else air + 1.6)
        dew = air - (100 - rh) / 5.0
        rows.append({
            "device_id": "esp32-smoke-01", "ts": ts,
            "air_temp_c": round(air, 2), "humidity_pct": round(rh, 2),
            "pressure_hpa": 975.0, "dew_point_c": round(dew, 2),
            "leaf_temp_c": round(leaf, 2),
            "leaf_wetness": 100.0 if leaf <= dew else 0.0,
            "sensors": {"bme280": True, "mlx90614": True, "ads1115": True},
        })
    db.readings.insert_many(rows)
    return rows


@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["agrisync"]
    monkeypatch.setattr(ddata, "_db", lambda: fake)
    seed(fake)
    return fake


@pytest.fixture
def client(db):
    from dashboard.app import create_app
    return create_app().test_client()


# --- the public cap, which is the security-relevant bit ---

def test_cap_is_enforced_not_just_defaulted():
    assert ddata.clamp(24) == 24
    assert ddata.clamp(168) == 168
    assert ddata.clamp(720) == 168        # 30 days requested
    assert ddata.clamp(99999) == 168
    assert ddata.clamp("nonsense") == 168
    assert ddata.clamp(None) == 168
    assert ddata.clamp(-10) == 1


def test_route_cannot_be_pushed_past_seven_days(client):
    r = client.get("/?hours=8760")           # a year
    assert r.status_code == 200
    assert b"7 days" in r.data


def test_json_endpoint_also_capped(client):
    body = client.get("/data.json?hours=100000").get_json()
    assert body["hours"] == 168


# --- rendering ---

def test_page_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"AgriSync" in r.data
    assert b"Hapur" in r.data


def test_shows_current_conditions(client):
    r = client.get("/")
    for label in (b"Temperature", b"Humidity", b"Dew point", b"Wetness"):
        assert label in r.data


def test_uses_the_main_site_design_tokens(client):
    """If these drift, the dashboard stops looking like agrisync.tech."""
    body = client.get("/").data
    for token in (b"--paper:#f4f2ec", b"--green:#3f5e39", b"--moss:#5c7340",
                  b"Instrument+Serif", b"--maxw:1080px"):
        assert token in body


def test_nav_points_at_the_real_anchors(client):
    body = client.get("/").data
    for anchor in (b"agrisync.tech/#what", b"agrisync.tech/#how",
                   b"agrisync.tech/#engine", b"agrisync.tech/#about",
                   b"agrisync.tech/#stack", b"agrisync.tech/#connect"):
        assert anchor in body


def test_keeps_the_minimum_honesty_on_the_numbers(client):
    """The long explanation lives on agrisync.tech now, but a bare percentage
    labelled 'disease pressure' with no qualifier would be misleading."""
    body = client.get("/").data.lower()
    assert b"not a prediction" in body
    assert b"uncalibrated" in body


def test_links_back_to_the_main_site_for_the_explanation(client):
    r = client.get("/")
    assert b"agrisync.tech" in r.data
    assert b"How this is calculated" in r.data


def test_offers_the_authenticated_route_for_more(client):
    """More than the public week is behind sign-in: the Connect button, and
    path links (never a hostname) for the CSV and the photographs."""
    r = client.get("/")
    assert b"Connect" in r.data
    assert b"/export/readings.csv" in r.data
    assert b'data-ag-request="data"' in r.data


def test_no_secrets_leak_into_the_page(client):
    """This page is public. Nothing sensitive may appear in it."""
    body = client.get("/").data.lower()
    for leak in (b"device_api_key", b"read_api_key", b"cf-access", b"192.168.",
                 b"mongodb://", b"client_secret"):
        assert leak not in body


# --- data shaping ---

def test_series_buckets_to_six_hours_over_long_windows(db):
    day = ddata.series(24)
    week = ddata.series(168)
    assert len(day["labels"]) <= 25
    # 7 days at 6-hour buckets is ~28 points, not 168
    assert len(week["labels"]) < 40


def test_wet_hours_counted(db):
    w = ddata.wet_hours(24)
    assert w["available"] is True
    assert 0 < w["hours"] <= 24


def test_suspect_rows_excluded_from_charts(db):
    db.readings.insert_one({
        "device_id": "esp32-smoke-01", "ts": datetime.now(timezone.utc).replace(tzinfo=None),
        "air_temp_c": 0.0, "humidity_pct": 0.0, "pressure_hpa": 0.0,
        "quality_flags": ["i2c:all_zero"],
    })
    s = ddata.series(24)
    assert 0.0 not in [v for v in s["air"] if v is not None]


def test_latest_reports_online_state(db):
    l = ddata.latest()
    assert l is not None
    assert l["online"] is True
    assert l["air_temp_c"] is not None


def test_health_endpoint(client):
    assert client.get("/health").status_code == 200


def test_survives_an_empty_database(monkeypatch):
    fake = mongomock.MongoClient()["empty"]
    monkeypatch.setattr(ddata, "_db", lambda: fake)
    from dashboard.app import create_app
    r = create_app().test_client().get("/")
    assert r.status_code == 200
    assert b"No data yet" in r.data or b"Waiting for the first reading" in r.data


def test_background_layers_cannot_intercept_clicks(client):
    """Fixed decorative layers must never sit in front of the content."""
    body = client.get("/").data.decode()
    assert "pointer-events:none" in body
    assert "z-index:-1" in body and "z-index:-2" in body


def test_no_background_attachment_fixed(client):
    """background-attachment:fixed janks badly on iOS. Fixed pseudo-elements
    give the same effect for free."""
    body = client.get("/").data.decode()
    assert "background-attachment:" not in body   # the property, not the comment


def test_social_links_are_the_owners_only(client):
    body = client.get("/").data
    assert b"linkedin.com/in/nikhil-siddhu" in body
    assert b"x.com/nikhilsiddhu_" in body
    for other in (b"facebook.com", b"instagram.com", b"youtube.com", b"twitter.com"):
        assert other not in body
