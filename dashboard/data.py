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
from src import version

WET_THRESHOLD = 50.0
STALE_MINUTES = int(os.getenv("DASH_STALE_MINUTES", "30"))
SAMPLE_MINUTES = max(1, int(os.getenv("SAMPLE_MINUTES", "5")))

# Hard public limit. Not a default - a ceiling. Anyone can ask this service for
# a longer window and it will quietly give them 7 days, because the deeper
# history lives behind Cloudflare Access on api.agrisync.tech.
MAX_PUBLIC_HOURS = 7 * 24
API_URL = os.getenv("API_PUBLIC_URL", "https://api.agrisync.tech")


# What a signed-in visitor may ask for. Two years is far past "everything" for
# a station that went up this season, so in practice it is no limit at all. It
# exists so that a crafted ?hours=999999999 cannot ask Mongo for a scan that
# never finishes.
MAX_MEMBER_HOURS = 2 * 365 * 24

DEFAULT_HOURS = MAX_PUBLIC_HOURS

# Offered in the range picker. The member ones are drawn locked rather than
# hidden: a range you can see but cannot yet use is an invitation, and one you
# cannot see at all is nothing.
RANGES = [
    {"hours": 24,    "label": "24 hours", "member": False},
    {"hours": 72,    "label": "3 days",   "member": False},
    {"hours": 168,   "label": "7 days",   "member": False},
    {"hours": 720,   "label": "30 days",  "member": True},
    {"hours": 2160,  "label": "90 days",  "member": True},
    {"hours": 17520, "label": "Everything", "member": True},
]


def clamp(hours, member=False):
    """How far back this visitor may look.

    The ceiling is decided here, from a signed cookie, and never from anything
    the browser can set. A stranger who edits ?hours= in the address bar gets
    the public window back rather than more data.
    """
    ceiling = MAX_MEMBER_HOURS if member else MAX_PUBLIC_HOURS
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return min(DEFAULT_HOURS, ceiling)
    return max(1, min(hours, ceiling))

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
    """Newest CLEAN reading, for the tiles and the Live badge.

    Changed 10 Oct 2026. It used to return the newest row of any quality, so a
    box sending nothing but flagged rows still showed Live. Now online means
    good data is arriving, and incoming_flagged means rows are arriving but
    failing the plausibility checks.
    """
    db = _db()
    newest = db.readings.find_one(sort=[("ts", -1)])
    if not newest:
        return None
    doc = db.readings.find_one({"quality_flags": {"$exists": False}},
                               sort=[("ts", -1)]) or newest
    now = datetime.now(timezone.utc)

    def _age(d):
        ts = d.get("ts")
        if ts is None:
            return None
        stamp = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return (now - stamp).total_seconds() / 60.0

    age_min = _age(doc)
    newest_age = _age(newest)
    local = _local(doc.get("ts"))
    clean = not doc.get("quality_flags")
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
        "online": bool(clean and age_min is not None and age_min < STALE_MINUTES),
        "sensors": doc.get("sensors") or {},
        "firmware": newest.get("firmware") or doc.get("firmware"),
        "suspect": not clean,
        "receiving": bool(newest_age is not None and newest_age < STALE_MINUTES),
        "incoming_flagged": bool(newest.get("quality_flags")),
    }


def series(hours=MAX_PUBLIC_HOURS):
    """Grouped in the database. The cap is applied here, before the query is
    built, which is why src/bucketing.py never needs to know about it."""
    # Absolute ceiling only. app.py has already applied the public or
    # member limit from a signed cookie; re-applying the public one here
    # would silently undo it and serve a member seven days.
    hours = clamp(hours, member=True)  # caller already decided the window
    query = {"ts": {"$gte": datetime.now(timezone.utc) - timedelta(hours=hours)},
             "quality_flags": {"$exists": False}}

    minutes = bucket_minutes_for(hours)
    fmt = "%H:%M" if hours <= 48 else "%d %b %H:%M"

    out = {"labels": [], "t": [], "air": [], "humidity": [], "dew": [], "leaf": [], "wetness": []}
    for r in bucketed(_db().readings, query, minutes):
        out["labels"].append(_local(r["bucket"]).strftime(fmt))
        out["t"].append(_ms(r["bucket"]))      # epoch ms, so a gap in the record stays a gap on a time axis
        for key in ("air", "humidity", "dew", "leaf", "wetness"):
            out[key].append(r[key])
    return out


def spark(hours=24):
    """The "Now" tiles' sparklines: the last day in hourly buckets, plus the
    moment the window ends, so the line sits where the hours really were and
    an hour Phytoplex did not send shows as a gap."""
    out = series(hours)
    out["hours"] = hours
    out["now"] = _ms(datetime.now(timezone.utc))
    return out


def wet_hours(hours=MAX_PUBLIC_HOURS):
    """Hours the leaf was wet. The number every disease model actually consumes."""
    # Absolute ceiling only. app.py has already applied the public or
    # member limit from a signed cookie; re-applying the public one here
    # would silently undo it and serve a member seven days.
    hours = clamp(hours, member=True)  # caller already decided the window
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
    # Absolute ceiling only. app.py has already applied the public or
    # member limit from a signed cookie; re-applying the public one here
    # would silently undo it and serve a member seven days.
    hours = clamp(hours, member=True)  # caller already decided the window
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = list(
        _db().readings.find(
            {"ts": {"$gte": since}},
            {"air_temp_c": 1, "humidity_pct": 1, "quality_flags": 1, "_id": 0},
        )
    )
    temps = [r["air_temp_c"] for r in rows if r.get("air_temp_c") is not None]
    hums = [r["humidity_pct"] for r in rows if r.get("humidity_pct") is not None]

    # How often the station actually reports. Board A posts every five
    # minutes, so assuming one a minute made the denominator five times
    # too large and the percentage meaningless. Override with
    # SAMPLE_MINUTES in .env if the firmware interval changes.
    expected = hours * 60 / SAMPLE_MINUTES
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
                "reason": "leaf wetness is estimated from air temperature and dew point"}

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


# ============================================================= members ======

# One page of readings is plenty on screen and keeps the HTML small. For bulk,
# the CSV export is the right tool and it already exists.
TABLE_LIMIT = 500

# What a stranger sees above the fade. Real readings, just not many - the top
# of a real table, cut off. Not invented rows: fake numbers on a page whose
# whole claim is "these are real measurements" would be a bad trade for a
# slightly fuller-looking screenshot.
TABLE_PUBLIC = 5


def table(hours=MAX_PUBLIC_HOURS, limit=TABLE_LIMIT):
    """Recent readings, newest first.

    Suspect rows are excluded exactly as the charts exclude them. A reading of
    179 degrees is a sensor glitch, not data, and putting it in a tidy table
    lends it a credibility it has not earned.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _db().readings.find(
        {"ts": {"$gte": since}, "quality_flags": {"$exists": False}},
        {"_id": 0, "ts": 1, "air_temp_c": 1, "humidity_pct": 1,
         "dew_point_c": 1, "leaf_wetness": 1, "pressure_hpa": 1},
    ).sort("ts", -1).limit(int(limit))

    out = []
    for r in rows:
        ts = r.get("ts")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "when": ts.astimezone().strftime("%d %b, %H:%M") if ts else "\u2014",
            "air": r.get("air_temp_c"),
            "hum": r.get("humidity_pct"),
            "dew": r.get("dew_point_c"),
            "wet": r.get("leaf_wetness"),
            "pres": r.get("pressure_hpa"),
        })
    return out


def span():
    """How much record exists, for the copy on the locked panels.

    "Another 23 days of this" is a reason to sign in. "Sign in for more" is
    not, because it never says how much more and the honest answer might be
    none. So the number is read from the database rather than asserted.
    """
    try:
        first = _db().readings.find_one({}, {"ts": 1, "_id": 0},
                                        sort=[("ts", 1)])
        total = _db().readings.estimated_document_count()
    except Exception:
        return {"available": False}

    if not first or not first.get("ts"):
        return {"available": False}

    ts = first["ts"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    days = max(1, (datetime.now(timezone.utc) - ts).days)
    return {
        "available": True,
        "total_days": days,
        # What signing in actually buys. While the station is younger than the
        # public window this is 0, and the template says something else rather
        # than promising data that does not exist yet.
        "extra_days": max(0, days - MAX_PUBLIC_HOURS // 24),
        "readings": total,
        "since": ts.astimezone().strftime("%d %B %Y"),
    }


# ============================================================== feeds =====
# Written by scripts/update_feeds.py on a timer; read here and nowhere else.
# Every function returns {"available": False} rather than raising, so a feed
# that has not run yet, or failed, costs the page one quiet panel and nothing
# more.

LEAD_LABEL = {12: "12 h", 24: "24 h", 36: "36 h", 48: "48 h", 60: "60 h", 72: "72 h"}


def _feed(name):
    try:
        return _db().feeds.find_one({"_id": name})
    except Exception:
        return None


def _age_hours(doc):
    ts = (doc or {}).get("generated_at")
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def forecast_risk():
    doc = _feed("forecast")
    if not doc or not doc.get("rows"):
        return {"available": False}
    age = _age_hours(doc)
    rows = []
    for r in doc["rows"]:
        cells = []
        for c in r["cells"]:
            ens = c.get("ens") or {}
            n = ens.get("n") or 0
            agree = max(ens.get("low", 0), ens.get("rising", 0), ens.get("high", 0)) if n else 0
            cells.append({
                "lead": LEAD_LABEL.get(c["lead_h"], f"{c['lead_h']} h"),
                "when": _local(c["at"]).strftime("%a %H:%M") if c.get("at") else "",
                "level": c.get("level"),
                "ens_high": ens.get("high"), "ens_rising": ens.get("rising"),
                "ens_low": ens.get("low"), "ens_n": n, "ens_agree": agree,
            })
        rows.append({"name": r["name"], "pathogen": r["pathogen"],
                     "sourced": r["sourced"], "cells": cells})
    order = {"high": 0, "rising": 1, "low": 2, None: 3}
    rows.sort(key=lambda r: min(order[c["level"]] for c in r["cells"]))
    hourly = doc.get("hourly") or []
    return {
        "available": True, "stale": age is not None and age > 8,
        "issued": _local(doc["generated_at"]).strftime("%d %b, %H:%M"),
        "source": doc.get("source"),
        "members": (doc.get("ensemble") or {}).get("members"),
        "rows": rows,
        "wet_hours_ahead": sum(1 for h in hourly if h.get("wet")),
        "chart": {
            "labels": [_local(h["time"]).strftime("%a %H:%M") for h in hourly],
            "temp": [h.get("temp_c") for h in hourly],
            "rh": [h.get("rh_pct") for h in hourly],
            "wet": [100 if h.get("wet") else 0 for h in hourly],
        },
    }


def comparison():
    doc = _feed("comparison")
    if not doc or not doc.get("series"):
        return {"available": False}
    s = doc["series"]
    st = doc.get("stats") or {}
    until = doc.get("era5_until")
    rain = doc.get("rain") or {}
    sun = doc.get("sun") or {}
    day = (lambda iso: datetime.fromisoformat(iso).strftime("%d %b") if iso else None)
    return {
        "available": True,
        "stats": st,
        "era5_until": _local(until).strftime("%d %b") if until else None,
        "grid": doc.get("era5") or {},
        "modelled_days": len(doc.get("modelled_days") or []),
        "chart": {
            "labels": [datetime.fromisoformat(x["date"]).strftime("%d %b") for x in s],
            "field_temp": [x.get("field_temp") for x in s],
            "era5_temp": [x.get("era5_temp") for x in s],
            "field_rh": [x.get("field_rh") for x in s],
            "era5_rh": [x.get("era5_rh") for x in s],
            "rain": [x.get("rain_mm") for x in s],
        },
        "rain": {"total": rain.get("total_mm"), "wet_days": rain.get("wet_days"),
                 "from": day(rain.get("from")), "until": day(rain.get("until"))}
                if rain.get("days") else None,
        "sun": sun.get("mean_kwh_m2") if sun.get("days") else None,
    }


def _ms(dt):
    """Epoch milliseconds, for charts on a shared time axis."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _season_axis():
    """Same x range on every chart from orbit, so dates line up."""
    start = os.getenv("SEASON_START", "2026-08-01")
    try:
        s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc) - timedelta(hours=5, minutes=30)
    except ValueError:
        s = datetime(2026, 7, 31, 18, 30, tzinfo=timezone.utc)
    return [_ms(s), _ms(datetime.now(timezone.utc))]


def satellite():
    doc = _feed("satellite")
    if not doc:
        return {"available": False}
    series = doc.get("series") or []
    latest = series[-1] if series else None
    return {
        "available": bool(series),
        "passes": doc.get("passes", 0), "clear": doc.get("clear", 0),
        "radius_m": doc.get("radius_m"),
        "latest": ({"ndvi": latest["ndvi"],
                    "date": _local(latest["acquired"]).strftime("%d %b")} if latest else None),
        "chart": {"points": [{"x": _ms(x["acquired"]), "y": x["ndvi"]} for x in series],
                  "range": _season_axis()},
    }


def radar():
    doc = _feed("radar")
    if not doc:
        return {"available": False}
    series = [x for x in doc.get("series") or [] if x.get("rvi") is not None]
    if not series:
        return {"available": False}

    def pts(orbit):
        # Descending passes cross Hapur about 06:15 IST, ascending about 18:20.
        return [{"x": _ms(x["acquired"]), "y": x["rvi"]} for x in series if x.get("orbit") == orbit]

    # One pass is mostly speckle. The line is the median of five passes
    # centred on each one, narrowing at the ends.
    vals = [x["rvi"] for x in series]
    trend = []
    for i, x in enumerate(series):
        w = sorted(vals[max(0, i - 2): i + 3])
        trend.append({"x": _ms(x["acquired"]), "y": round(w[len(w) // 2], 3)})
    latest = series[-1]
    return {
        "available": True, "passes": len(series), "radius_m": doc.get("radius_m"),
        "latest": {"rvi": latest["rvi"], "trend": trend[-1]["y"],
                   "date": _local(latest["acquired"]).strftime("%d %b")},
        "chart": {"morning": pts("descending"), "evening": pts("ascending"),
                  "trend": trend, "range": _season_axis()},
    }


def optical_gaps():
    """Days a radar pass looked at the field while no clear optical pass did,
    within two days either side. The reason radar is here at all."""
    try:
        rad = (_feed("radar") or {}).get("series") or []
        sat = (_feed("satellite") or {}).get("series") or []
    except Exception:
        return None
    if not rad:
        return None
    utc = (lambda t: t if t.tzinfo else t.replace(tzinfo=timezone.utc))
    clear = [utc(x["acquired"]) for x in sat]
    near = (lambda t: any(abs((t - c).total_seconds()) <= 2 * 86400 for c in clear))
    return sum(1 for x in rad if not near(utc(x["acquired"])))


CHECK_TEXT = {
    "check": ("Look closer", "warn",
              "The canopy fell while the weather favoured infection. Worth walking the field "
              "and logging what you find."),
    "stress": ("Probably not disease", "",
               "The canopy fell, but the weather never favoured infection. Water, nutrients, "
               "a harvest, or haze the cloud mask missed are likelier."),
    "gap": ("Can't tell", "",
            "The canopy fell, but the station missed most of those days, so the weather "
            "side cannot be checked."),
    "watch": ("Watch", "",
              "The weather favoured infection, but the canopy has not fallen. Nothing visible "
              "from orbit yet."),
    "clear": ("Nothing to explain", "",
              "The canopy has not fallen and the weather has not favoured infection."),
    "waiting": ("Waiting", "", "This needs two clear satellite passes."),
}


def disease_or_stress():
    doc = _feed("check")
    if not doc:
        return {"available": False}
    state = doc.get("state", "waiting")
    tag, tone, text = CHECK_TEXT.get(state, CHECK_TEXT["waiting"])
    c = doc.get("canopy") or {}
    w = doc.get("weather") or {}
    s = doc.get("since") or {}
    return {
        "available": True, "state": state, "tag": tag, "tone": tone, "text": text,
        "canopy": ({"from": _local(c["from"]).strftime("%d %b"), "to": _local(c["to"]).strftime("%d %b"),
                    "ndvi_from": c["ndvi_from"], "ndvi_to": c["ndvi_to"], "delta": c["delta"],
                    "trend": c["trend"]} if c else None),
        "weather": w or None,
        "since": s if s.get("days", 0) > 0 else None,
    }


def anomalies():
    doc = _feed("anomaly")
    if not doc:
        return {"available": False}
    return {"available": True, "flags": doc.get("flags") or [],
            "checked_hours": doc.get("checked_hours", 0),
            "baseline_days": doc.get("baseline_days", 0),
            "stale": (_age_hours(doc) or 0) > 3}


def calibration_summary():
    doc = _feed("calibration")
    if not doc:
        return {"available": False}
    t = doc.get("levels") or {}
    return {
        "available": True, "ready": bool(doc.get("ready")),
        "visits": doc.get("visits", 0), "judged": doc.get("judged", 0),
        "positives": doc.get("positives", 0), "needs": doc.get("needs") or {},
        "levels": [dict(level=lv, **(t.get(lv) or {"n": 0, "k": 0})) for lv in ("low", "rising", "high")],
        "warning": doc.get("warning") or {},
    }


def _events(flags):
    """Consecutive flagged hours of one variable -> one event each."""
    names = {"st_temp": "Temperature", "st_dew": "Dew point"}
    events = []
    for f in sorted(flags, key=lambda x: (x["variable"], x["time"])):
        t = f["time"] if f["time"].tzinfo else f["time"].replace(tzinfo=timezone.utc)
        e = events[-1] if events else None
        if e and e["variable"] == f["variable"] and t - e["_end"] <= timedelta(hours=1):
            e["_end"] = t
            e["hours"] += 1
            if abs(f["z"]) > abs(e["z"]):
                e["z"] = f["z"]
            continue
        events.append({"variable": f["variable"], "name": names.get(f["variable"], f["variable"]),
                       "direction": f["direction"], "z": f["z"], "hours": 1, "_start": t, "_end": t})
    for e in events:
        s, en = _local(e.pop("_start")), _local(e.pop("_end") + timedelta(hours=1))
        e["when"] = f"{s.strftime('%d %b, %H:%M')} to {en.strftime('%H:%M')}"
    events.sort(key=lambda e: e["when"], reverse=True)
    return events


def baseline_summary():
    doc = _feed("baseline")
    if not doc:
        return {"available": False}
    m = doc.get("micro") or {}
    tg = (m.get("targets") or {}).get("st_temp") or {}
    until = doc.get("checked_until")
    return {
        "available": True, "state": doc.get("state", "learning"),
        "trained_days": m.get("trained_days"), "micro_state": m.get("state"),
        "r2": tg.get("r2"), "sigma": tg.get("sigma"),
        "events": _events(m.get("flags") or [])[:3],
        "checked_until": _local(until).strftime("%d %b") if until else None,
        "ndvi": doc.get("ndvi") or {}, "radar": doc.get("radar") or {},
    }


def alerts_summary():
    doc = _feed("alerts")
    if not doc:
        return {"available": False}
    hist = []
    for h in (doc.get("history") or [])[:5]:
        hist.append({"when": _local(h["at"]).strftime("%d %b, %H:%M") if h.get("at") else "",
                     "body": h.get("body", ""), "sent": bool(h.get("sent"))})
    return {"available": True, "channel": bool(doc.get("channel")),
            "active": doc.get("active") or [], "history": hist}


LEVEL_OF = (lambda p: None if p is None else "high" if p > 0.66 else "rising" if p > 0.33 else "low")
RANKS = {"low": 0, "rising": 1, "high": 2}


def summary():
    """The few live numbers agrisync.tech shows. Public, small, cached a minute."""
    out = {"generated_at": datetime.now(timezone.utc).isoformat()}
    l = latest()
    if l:
        try:
            doc = _db().readings.find_one({"quality_flags": {"$exists": False}}, sort=[("ts", -1)]) or {}
            t = _local(doc.get("ts"))
        except Exception:
            t = None
        out["latest"] = {
            "air_temp_c": l.get("air_temp_c"), "humidity_pct": l.get("humidity_pct"),
            "dew_point_c": l.get("dew_point_c"),
            "leaf_wet": (l.get("leaf_wetness") or 0) >= WET_THRESHOLD,
            "observed_at": l.get("observed_at"), "age_minutes": l.get("age_minutes"),
            "time": t.strftime("%I:%M %p").lstrip("0").lower() if t else None,
            "online": l.get("online"),
            "firmware": l.get("firmware"),
        }
    out["versions"] = version.versions(l.get("firmware") if l else None)
    r = risk()
    levels = [LEVEL_OF(x.get("pressure")) for x in r.get("diseases", [])]
    levels = [x for x in levels if x]
    if levels:
        out["now"] = {"level": max(levels, key=RANKS.get)}
    elif l and l.get("online"):
        out["now"] = {"level": "low"}       # no wet hours in the last day
    fc = _feed("forecast")
    if fc and fc.get("rows"):
        ahead = [c.get("level") for row in fc["rows"] for c in row.get("cells", []) if c.get("level")]
        if ahead:
            out["ahead"] = {"level": max(ahead, key=RANKS.get),
                            "wet_hours": sum(1 for h in fc.get("hourly") or [] if h.get("wet"))}
    try:
        optical = []
        for p in _db().ndvi.find({"error": {"$exists": False}}, {"acquired": 1, "ndvi": 1,
                                                                 "valid_px": 1, "total_px": 1}).sort("acquired", 1):
            clear = p.get("ndvi") is not None and p.get("valid_px", 0) >= 0.6 * max(1, p.get("total_px", 1))
            optical.append({"d": _local(p["acquired"]).strftime("%Y-%m-%d"),
                            "ndvi": round(p["ndvi"], 3) if clear else None})
        radar = [{"d": _local(p["acquired"]).strftime("%Y-%m-%d")}
                 for p in ((_feed("radar") or {}).get("series") or [])]
        if optical or radar:
            out["orbit"] = {"optical": optical, "radar": radar}
    except Exception:
        pass
    cal = calibration_summary()
    if cal.get("available"):
        out["calibration"] = {"ready": cal["ready"], "visits": cal["visits"], "judged": cal["judged"]}
    base = baseline_summary()
    if base.get("available"):
        out["baseline"] = {"state": base["state"], "trained_days": base["trained_days"]}
    try:
        o = odds_public()
    except Exception:
        o = None
    if o:
        out["odds"] = o
    return out


# ------------------------------------------------------------------- odds

def _day_label(iso):
    try:
        return datetime.fromisoformat(str(iso)[:10]).strftime("%a %-d %b")
    except ValueError:
        return str(iso)


def odds():
    """The odds engine's latest run (scripts/update_feeds.py run_odds)."""
    doc = _feed("odds")
    if not doc or not doc.get("now"):
        return {"available": False}
    age = _age_hours(doc)
    hourly = doc.get("hourly") or []
    season = doc.get("season") or []
    sp = doc.get("spell")
    cal = doc.get("calibration") or {}
    ctx = doc.get("context") or {}
    inputs = doc.get("inputs") or {}
    windows = [{"hours": w["hours"], "p": w["p"], "lo": w["lo"], "hi": w["hi"],
                "level": w.get("level"), "wet_h": w.get("wet_h"),
                "wet_lo": w.get("wet_h_lo"), "wet_hi": w.get("wet_h_hi"),
                "members_high": w.get("members_high"), "groups": w.get("groups") or {}}
               for w in doc.get("windows") or []]
    shares = (doc.get("windows") or [{}])[0].get("share") or {}
    from src.ml.pathogens import GROUP_NAMES
    groups = sorted(({"key": k, "name": GROUP_NAMES.get(k, k), "share": v,
                      "p": windows[0]["groups"].get(k) if windows else None}
                     for k, v in shares.items()), key=lambda g: -(g["share"] or 0))
    return {
        "available": True, "stale": age is not None and age > 3,
        "updated": _local(doc["generated_at"]).strftime("%d %b, %H:%M"),
        "now": doc["now"], "windows": windows, "groups": groups,
        "top_name": doc.get("top_name"), "drivers": doc.get("drivers") or [],
        "spell": ({"from": _local(sp["from"]).strftime("%a %-I %p"),
                   "to": _local(sp["to"]).strftime("%-I %p"),
                   "hours": sp.get("hours"), "temp": sp.get("temp")} if sp else None),
        "incubating": [dict(e, show_from_label=_day_label(e["show_from"]),
                            show_to_label=_day_label(e["show_to"])) for e in doc.get("incubating") or []],
        "calibration": cal, "context": ctx, "inputs": inputs,
        "chart": {
            "labels": [_local(h["time"]).strftime("%a %H:%M") for h in hourly],
            "times": [_ms(h["time"]) for h in hourly],
            "p": [h["p"] for h in hourly], "lo": [h["lo"] for h in hourly],
            "hi": [h["hi"] for h in hourly], "wet": [h.get("wet") for h in hourly],
        },
        "season": {
            "points": [{"x": _ms(datetime.fromisoformat(r["date"]).replace(tzinfo=timezone.utc)),
                        "p": r["p"], "lo": r["lo"], "hi": r["hi"], "wet_h": r.get("wet_h"),
                        "measured": r.get("measured")} for r in season],
            "range": _season_axis(),
        },
    }


def mos_skill():
    """How much the field correction helped on hours it never trained on."""
    m = _feed("mos") or {}
    t, dw, wet = m.get("temp") or {}, m.get("dew") or {}, m.get("wet") or {}
    if t.get("state") not in ("on", "off") and dw.get("state") not in ("on", "off"):
        return None
    return {"temp_raw": t.get("rmse_raw"), "temp_cor": t.get("rmse_corrected") if t.get("use") else t.get("rmse_raw"),
            "dew_raw": dw.get("rmse_raw"), "dew_cor": dw.get("rmse_corrected") if dw.get("use") else dw.get("rmse_raw"),
            "temp_on": bool(t.get("use")), "dew_on": bool(dw.get("use")),
            "hours": dw.get("holdout_hours") or t.get("holdout_hours"), "pairs": m.get("pairs"),
            "wet_raw": wet.get("mae_raw"), "wet_cor": wet.get("mae_corrected"), "wet_days": wet.get("days")}


def odds_public():
    """What agrisync.tech shows: the headline, never the field's location."""
    o = odds()
    if not o.get("available"):
        return None
    inc = o["incubating"][:2]
    cal = o["calibration"]
    doc = _feed("odds") or {}
    season = [{"d": r["date"], "p": r["p"], "m": r.get("measured")} for r in doc.get("season") or []]
    skill = mos_skill()
    return {
        "p": o["now"]["p"], "lo": o["now"]["lo"], "hi": o["now"]["hi"],
        "level": o["now"]["level"], "words": o["now"]["words"],
        "windows": [{"hours": w["hours"], "p": w["p"], "lo": w["lo"], "hi": w["hi"]} for w in o["windows"]],
        "top": o["top_name"], "drivers": o["drivers"][:3],
        "incubating": [{"name": e["name"], "p": e["p"], "from": e["show_from_label"],
                        "to": e["show_to_label"]} for e in inc],
        "checked": bool(cal.get("checked")), "visits": cal.get("visits", 0),
        "climate": (o["context"] or {}).get("climate"),
        "season_rank_pct": (o["context"] or {}).get("season_rank_pct"),
        "season": season, "skill": skill,
        "updated": o["updated"], "stale": o["stale"],
    }


FEED_NAMES = ("forecast", "reanalysis", "satellite", "radar", "anomaly", "check",
              "calibration", "baseline", "mos", "climate", "odds", "alerts")


def feed_health():
    """Hours since each outside feed last succeeded, and whether its latest
    run failed. For the outside monitor. No error text: this is public."""
    doc = _feed("status")
    if not doc:
        return None
    now = datetime.now(timezone.utc)
    out = {}
    for name in FEED_NAMES:
        e = doc.get(name) or {}
        ok = e.get("last_ok")
        if ok is not None and ok.tzinfo is None:
            ok = ok.replace(tzinfo=timezone.utc)
        out[name] = {"hours_since_ok": round(max(0.0, (now - ok).total_seconds()) / 3600, 1) if ok else None,
                     "failing": "error" in e}
    return out


def field_log_summary():
    try:
        n = _db().field_log.count_documents({})
        last = _db().field_log.find_one({}, sort=[("date", -1)])
    except Exception:
        return {"count": 0, "last": None}
    return {"count": n, "last": last.get("date") if last else None}


def recent_photos(limit=4):
    """Newest canopy image filenames, for the dashboard thumbnails.

    Sorted by modification time rather than filename: the camera has no clock,
    and its counters only order within a single boot - after a reset it starts
    at 000000 again, so filename order would put a fresh image behind an old
    one.
    """
    import os as _os
    from pathlib import Path as _Path
    d = _Path(_os.getenv("PHOTO_DIR", "data/canopy")).expanduser()
    if not d.is_dir():
        return []
    files = [p for p in d.iterdir()
             if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png")]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in files[:limit]]


def photo_count():
    """How many canopy images exist, for the locked images panel."""
    import os as _os
    from pathlib import Path as _Path
    d = _Path(_os.getenv("PHOTO_DIR", "data/canopy")).expanduser()
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir()
               if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
