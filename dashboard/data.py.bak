"""Read-only data layer for the public dashboard.

Deliberately separate from src/. This process must never be able to write to
the database or reach the device API, because it is the one thing on the box
that is exposed to the internet without authentication.

Everything returned here is safe to publish. No API keys, no device tokens,
no internal IPs, no firmware paths.
"""
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timedelta, timezone

from pymongo import MongoClient

from src.bucketing import bucket_minutes_for, bucketed

WET_THRESHOLD = 50.0
STALE_MINUTES = int(os.getenv("DASH_STALE_MINUTES", "30"))

# Hard public limit. Not a default - a ceiling. Anyone can ask this service for
# a longer window and it will quietly give them 7 days, because the deeper
# history lives behind Cloudflare Access on api.agrisync.tech.
MAX_PUBLIC_HOURS = 7 * 24
API_URL = os.getenv("API_PUBLIC_URL", "https://api.agrisync.tech")


def clamp(hours):
    """Never return more than the public window, whatever was asked for."""
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return MAX_PUBLIC_HOURS
    return max(1, min(hours, MAX_PUBLIC_HOURS))

_client = None


def _db():
    global _client
    if _client is None:
        uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/agrisync")
        _client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    return _client.get_default_database()


def _local(dt):
    """Site local time. Mongo stores UTC and pymongo returns naive datetimes,
    so the tzinfo line is required — without it astimezone() assumes system
    local and silently shifts nothing on a Pi already set to IST."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(os.getenv("SITE_TZ", "Asia/Kolkata")))
    except Exception:
        return dt + timedelta(hours=5, minutes=30)


def site():
    return {
        "name": os.getenv("SITE_NAME", "Hapur"),
        "region": "Uttar Pradesh, India",
        "lat": float(os.getenv("SITE_LAT", "28.7170")),
        "lon": float(os.getenv("SITE_LON", "77.7740")),
        "crop": "Cabbage",
        "season": "Rabi",
    }


def latest():
    """Most recent reading, trimmed to fields that are safe to publish."""
    doc = _db().readings.find_one(sort=[("ts", -1)])
    if not doc:
        return None

    ts = doc.get("ts")
    local = _local(ts)
    age_min = None
    if ts is not None:
        stamp = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - stamp).total_seconds() / 60.0

    return {
        "air_temp_c": doc.get("air_temp_c"),
        "humidity_pct": doc.get("humidity_pct"),
        "pressure_hpa": doc.get("pressure_hpa"),
        "dew_point_c": doc.get("dew_point_c"),
        "leaf_temp_c": doc.get("leaf_temp_c"),
        "leaf_wetness": doc.get("leaf_wetness"),
        "rain_analog": doc.get("rain_analog"),
        "observed_at": local.strftime("%d %b %Y, %H:%M") if local else None,
        "age_minutes": round(age_min, 1) if age_min is not None else None,
        "online": bool(age_min is not None and age_min < STALE_MINUTES),
        "sensors": doc.get("sensors") or {},
        "suspect": bool(doc.get("quality_flags")),
    }


def series(hours=MAX_PUBLIC_HOURS):
    """Grouped in the database. The cap is applied here, before the query is
    built, which is why src/bucketing.py never needs to know about it."""
    hours = clamp(hours)
    query = {"ts": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)},
             "quality_flags": {"$exists": False}}

    minutes = bucket_minutes_for(hours)
    fmt = "%H:%M" if hours <= 48 else "%d %b %H:%M"

    out = {"labels": [], "air": [], "humidity": [], "dew": [], "leaf": [], "wetness": []}
    for r in bucketed(_db().readings, query, minutes):
        out["labels"].append(_local(r["bucket"]).strftime(fmt))
        for key in ("air", "humidity", "dew", "leaf", "wetness"):
            out[key].append(r[key])
    return out


def wet_hours(hours=MAX_PUBLIC_HOURS):
    """Hours the leaf was wet. The number every disease model actually consumes."""
    hours = clamp(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list(
        _db().readings.find(
            {"ts": {"$gte": since}, "leaf_wetness": {"$exists": True},
             "quality_flags": {"$exists": False}},
            {"leaf_wetness": 1, "ts": 1, "_id": 0},
        )
    )
    if len(rows) < 2:
        return {"hours": None, "available": False}

    wet = sum(1 for r in rows if (r.get("leaf_wetness") or 0) >= WET_THRESHOLD)
    stamps = sorted(r["ts"] for r in rows)
    span_h = (stamps[-1] - stamps[0]).total_seconds() / 3600.0
    per_sample = span_h / max(len(rows) - 1, 1)
    return {"hours": round(wet * per_sample, 1), "available": True}


def stats(hours=MAX_PUBLIC_HOURS):
    """Headline numbers for the summary strip."""
    hours = clamp(hours)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list(
        _db().readings.find(
            {"ts": {"$gte": since}},
            {"air_temp_c": 1, "humidity_pct": 1, "quality_flags": 1, "_id": 0},
        )
    )
    temps = [r["air_temp_c"] for r in rows if r.get("air_temp_c") is not None]
    hums = [r["humidity_pct"] for r in rows if r.get("humidity_pct") is not None]

    expected = hours * 60  # one reading a minute
    return {
        "readings": len(rows),
        "coverage_pct": round(100.0 * len(rows) / expected, 1) if expected else None,
        "temp_min": round(min(temps), 1) if temps else None,
        "temp_max": round(max(temps), 1) if temps else None,
        "humidity_mean": round(sum(hums) / len(hums), 1) if hums else None,
        "total_readings": _db().readings.estimated_document_count(),
    }


def deeper_access():
    """What the page offers when someone wants more than the public window."""
    return {
        "max_days": MAX_PUBLIC_HOURS // 24,
        "api_url": API_URL,
        "note": ("The last 7 days are public. The full record, the daily "
                 "breakdown and the raw CSV are behind a sign-in."),
    }


def risk():
    """Disease pressure, with the honesty attached rather than bolted on.

    Imported lazily so the dashboard still renders if the model changes.
    """
    try:
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from src.ml.physics_baseline import DISEASES
    except Exception:
        return {"available": False, "diseases": []}

    wh = wet_hours(24)
    if not wh["available"]:
        return {"available": False, "diseases": [],
                "reason": "leaf wetness needs the MLX90614, which is not fitted yet"}

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    rows = list(_db().readings.find(
        {"ts": {"$gte": since}, "leaf_wetness": {"$gte": WET_THRESHOLD},
         "quality_flags": {"$exists": False}},
        {"air_temp_c": 1, "_id": 0}))
    temps = [r["air_temp_c"] for r in rows if r.get("air_temp_c") is not None]
    if not temps:
        return {"available": False, "diseases": [],
                "reason": "no wet hours recorded in the last 24 hours"}

    mean_temp = sum(temps) / len(temps)
    out = []
    for key, model in DISEASES.items():
        out.append({
            "name": model.name,
            "pathogen": model.pathogen,
            "pressure": round(model.pressure(mean_temp, wh["hours"]), 3),
            "sourced": model.sourced,
        })
    out.sort(key=lambda d: -d["pressure"])
    return {"available": True, "diseases": out,
            "wet_hours": wh["hours"], "mean_temp": round(mean_temp, 1)}


def healthy():
    try:
        _db().command("ping")
        return True
    except Exception:
        return False
