"""Refresh the outside feeds and the numbers computed from them.

    .venv/bin/python -m scripts.update_feeds auto        # what the timer runs
    .venv/bin/python -m scripts.update_feeds all         # everything, now
    .venv/bin/python -m scripts.update_feeds forecast    # or one feed

auto runs the forecast when it is over 3 hours old, the unusual-conditions
check every time, the disease-or-stress check every 6 hours, and
reanalysis, Sentinel-2 and Sentinel-1 once a day. Each feed is independent:
one failing (no internet, a slow server) is recorded in the feeds status
document and the others still run. Nothing here touches the readings
collection except to read it.

Writes, all small:
    feeds         one document per feed (_id forecast, comparison, satellite,
                  radar, anomaly, check, status)
    era5_hourly   ERA5-Land hours for the season plus ERA5 rain, keyed by time
    power_daily   NASA POWER sunlight for the season, keyed by date
    ndvi          one document per Sentinel-2 pass, keyed by scene id
    radar         one document per Sentinel-1 scene, keyed by scene id
    outside_hourly the forecast's own recent past hours (temperature,
                  humidity, rain, wind, cloud, sunshine), keyed by time
    mos_pairs     day-ahead forecasts for hours the station measured, for
                  the field correction
    forecast_archive one document per forecast issue
"""
import os
import sys
import traceback

import numpy as np
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import MongoClient, UpdateOne

from src import alerts as alerts_mod
from src.feeds import sources
from src.ml import anomaly as anomaly_mod
from src.ml import attribution
from src.ml import baseline as baseline_mod
from src.ml import calibration as calibration_mod
from src.ml import forecast_risk
from src.ml import leafwet
from src.ml import mos as mos_mod
from src.ml import odds as odds_mod
from src.ml import pathogens as pz
from src.ml import wetness as wetness_mod

IST = timedelta(hours=5, minutes=30)
WET = 50.0


def field_point():
    lat = float(os.getenv("FIELD_LAT") or os.getenv("SITE_LAT", "28.7170"))
    lon = float(os.getenv("FIELD_LON") or os.getenv("SITE_LON", "77.7740"))
    return lat, lon


def db():
    uri = os.getenv("MONGO_URI", "mongodb://localhost:27017/agrisync")
    return MongoClient(uri, serverSelectionTimeoutMS=5000).get_default_database()


def now_utc():
    return datetime.now(timezone.utc)


def aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def log(msg):
    print(f"{now_utc():%H:%M:%S} {msg}", flush=True)


# ------------------------------------------------------------ station hours

def station_hours(d, since, until=None, centred=False):
    """Clean readings grouped into UTC hours.
    -> {hour: {"n", "temp", "rh", "dew", "wet", "wet_obs"}} where wet is the
    share of readings in that hour the proxy (leaf_wetness) scored wet, and
    wet_obs is the leaf wetness sensor's verdict for the hour
    (leaf_wet_sensor, wetness.sensor_hour_wet): 1.0 wet, 0.0 dry, None when
    no reading in the hour carried it.

    By default an hour is HH:00 to HH:59, keyed by its start. centred=True
    keys HH:00 by the readings from HH-1:30 to HH:29 instead, which is what
    an instantaneous model value stamped HH:00 should be compared with."""
    shift = timedelta(minutes=30) if centred else timedelta(0)
    q = {"ts": {"$gte": since.replace(tzinfo=None)}, "quality_flags": {"$exists": False}}
    if until:
        q["ts"]["$lt"] = until.replace(tzinfo=None)
    acc = defaultdict(lambda: {"n": 0, "temp": 0.0, "rh": 0.0, "dew": 0.0, "wet": 0,
                               "nt": 0, "nr": 0, "nd": 0, "nw": 0, "sw": 0, "ns": 0})
    fields = {"ts": 1, "air_temp_c": 1, "humidity_pct": 1, "dew_point_c": 1,
              "leaf_wetness": 1, "leaf_wet_sensor": 1, "_id": 0}
    for r in d.readings.find(q, fields):
        h = (aware(r["ts"]) + shift).replace(minute=0, second=0, microsecond=0)
        a = acc[h]
        a["n"] += 1
        for src, key, cnt in (("air_temp_c", "temp", "nt"), ("humidity_pct", "rh", "nr"),
                              ("dew_point_c", "dew", "nd")):
            if r.get(src) is not None:
                a[key] += float(r[src])
                a[cnt] += 1
        if r.get("leaf_wetness") is not None:
            a["wet"] += 1 if float(r["leaf_wetness"]) >= WET else 0
            a["nw"] += 1
        if r.get("leaf_wet_sensor") is not None:
            a["sw"] += 1 if r["leaf_wet_sensor"] else 0
            a["ns"] += 1
    out = {}
    for h, a in acc.items():
        out[h] = {
            "n": a["n"],
            "temp": a["temp"] / a["nt"] if a["nt"] else None,
            "rh": a["rh"] / a["nr"] if a["nr"] else None,
            "dew": a["dew"] / a["nd"] if a["nd"] else None,
            "wet": a["wet"] / a["nw"] if a["nw"] else None,
            "wet_obs": wetness_mod.sensor_hour_wet(a["sw"], a["ns"]),
        }
    return out


# ------------------------------------------------------------------ forecast

def run_forecast(d):
    lat, lon = field_point()
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    members = sources.ensemble(lat, lon)
    try:
        det_rows = sources.forecast(lat, lon)
        det_source = "Open-Meteo best-match forecast"
    except Exception as exc:
        log(f"forecast: deterministic feed failed ({exc}); using the ensemble control run")
        det_rows = members[0] if members else []
        det_source = "ECMWF ensemble control run"
    if not det_rows:
        raise RuntimeError("no forecast hours came back")

    obs = station_hours(d, now - timedelta(hours=forecast_risk.WINDOW_H), now)
    observed = [(h, v["wet"], v["temp"]) for h, v in obs.items()
                if v["wet"] is not None and v["temp"] is not None]

    # Model hours from the last day are kept: the station's own hours replace
    # them wherever it has one (forecast_risk._timeline), so they only fill gaps.
    def shape(rows):
        return [(r["time"], forecast_risk.forecast_hour_wet(r["temp_c"], r["dew_c"]), r["temp_c"])
                for r in rows if r["time"] >= now - timedelta(hours=forecast_risk.WINDOW_H)]

    det = forecast_risk.curve(observed, shape(det_rows), now)
    ens = forecast_risk.ensemble_curve(observed, [shape(m) for m in members], now)
    rows = forecast_risk.combine(det, ens)

    hourly = [{"time": r["time"], "temp_c": r["temp_c"], "rh_pct": r["rh_pct"],
               "dew_c": r["dew_c"], "precip_mm": r.get("precip_mm"),
               "wet": forecast_risk.forecast_hour_wet(r["temp_c"], r["dew_c"])}
              for r in det_rows if now <= r["time"] < now + timedelta(hours=72)]

    d.feeds.replace_one({"_id": "forecast"}, {
        "_id": "forecast", "generated_at": now_utc(), "issued_for": now,
        "lat": lat, "lon": lon, "source": det_source,
        "ensemble": {"model": sources.ENSEMBLE_MODEL, "members": len(members)},
        "observed_hours": len(observed),
        "rows": rows, "hourly": hourly,
    }, upsert=True)

    # Every run of the ensemble in full, for the odds engine.
    def columns(rows_):
        return {k: [r.get(src) for r in rows_] for k, src in MEMBER_KEYS}
    d.feeds.replace_one({"_id": "forecast_members"}, {
        "_id": "forecast_members", "generated_at": now_utc(), "issued_for": now,
        "model": sources.ENSEMBLE_MODEL,
        "hours": [r["time"] for r in members[0]] if members else [],
        "members": [columns(m) for m in members],
        "det_source": det_source, "det_hours": [r["time"] for r in det_rows],
        "det": columns(det_rows),
    }, upsert=True)

    # The model's own recent past: the cloud, wind and sunshine the station
    # does not measure, and a stand-in for any hour it missed.
    past_rows = [r for r in det_rows if r["time"] < now and r.get("temp_c") is not None]
    if past_rows:
        d.outside_hourly.bulk_write([UpdateOne({"_id": r["time"]}, {"$set": {
            k: r.get(k) for k in ("temp_c", "rh_pct", "dew_c", "precip_mm", "wind_ms", "cloud_pct", "sw_wm2")}},
            upsert=True) for r in past_rows], ordered=False)

    # Keep every issue, small, so the forecast itself can be scored later:
    # levels per lead for the field log, hours for the field correction.
    def mean(rows_list, key, i):
        vals = [m[i].get(key) for m in rows_list if i < len(m) and m[i].get(key) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None
    d.forecast_archive.replace_one({"_id": now}, {
        "_id": now, "source": det_source,
        "rows": [{"key": r["key"], "cells": [
            {"lead_h": c["lead_h"], "level": c.get("level"),
             "ens": {k: (c.get("ens") or {}).get(k) for k in ("n", "low", "rising", "high")}}
            for c in r["cells"]]} for r in rows],
        "hourly": [{k: r.get(k) for k in ("time", "temp_c", "dew_c", "rh_pct", "cloud_pct",
                                          "wind_ms", "sw_wm2", "precip_mm")}
                   for r in det_rows if now <= r["time"] < now + timedelta(hours=72)],
        "ens_mean": [{"time": r["time"], "temp_c": mean(members, "temp_c", i), "dew_c": mean(members, "dew_c", i)}
                     for i, r in enumerate(members[0]) if now <= r["time"] < now + timedelta(hours=72)]
        if members else [],
    }, upsert=True)
    log(f"forecast: {len(hourly)} hours, {len(members)} ensemble runs, "
        f"{len(observed)} observed hours")


MEMBER_KEYS = (("temp", "temp_c"), ("dew", "dew_c"), ("rh", "rh_pct"), ("rain", "precip_mm"),
               ("wind", "wind_ms"), ("cloud", "cloud_pct"), ("sw", "sw_wm2"))


# ------------------------------------------------------- reanalysis compare

def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / (sxx * syy) ** 0.5, 3) if sxx > 0 and syy > 0 else None


def compare(field, ref, key_f, key_r):
    pairs = [(field[h][key_f], ref[h][key_r]) for h in field
             if h in ref and field[h].get(key_f) is not None and ref[h].get(key_r) is not None]
    if not pairs:
        return None
    fx = [p[0] for p in pairs]
    rx = [p[1] for p in pairs]
    diffs = [a - b for a, b in pairs]
    return {"n_hours": len(pairs),
            "bias": round(sum(diffs) / len(diffs), 2),
            "rmse": round((sum(x * x for x in diffs) / len(diffs)) ** 0.5, 2),
            "r": _pearson(fx, rx)}


def run_reanalysis(d):
    lat, lon = field_point()
    first = d.readings.find_one({}, {"ts": 1}, sort=[("ts", 1)])
    start = (aware(first["ts"]) + IST).date() - timedelta(days=1) if first else sources.season_start()
    today = now_utc().date()

    era, meta = sources.era5_land(lat, lon, start, today)
    if era:
        d.era5_hourly.bulk_write([UpdateOne({"_id": r["time"]}, {"$set": r}, upsert=True)
                                  for r in era], ordered=False)
    log(f"reanalysis: {len(era)} ERA5-Land hours, up to "
        f"{(era[-1]['time'] + IST):%d %b %H:%M} IST" if era else "reanalysis: no ERA5-Land hours")

    try:
        rain = sources.era5_rain(lat, lon, start, today)
        if rain:
            d.era5_hourly.bulk_write([UpdateOne({"_id": r["time"]}, {"$set": {"rain_mm": r["rain_mm"]}},
                                                upsert=True) for r in rain], ordered=False)
        log(f"reanalysis: {len(rain)} ERA5 rain hours")
    except Exception as exc:
        log(f"reanalysis: ERA5 rain failed ({exc})")

    try:
        drv = sources.era5_drivers(lat, lon, start, today)
        if drv:
            d.era5_hourly.bulk_write([UpdateOne({"_id": r["time"]}, {"$set": {
                "wind_ms": r.get("wind_ms"), "cloud_pct": r.get("cloud_pct"), "sw_wm2": r.get("sw_wm2")}},
                upsert=True) for r in drv], ordered=False)
        log(f"reanalysis: {len(drv)} ERA5 hours of wind, cloud and sunshine")
    except Exception as exc:
        log(f"reanalysis: ERA5 wind, cloud and sunshine failed ({exc})")

    try:
        power = sources.nasa_power(lat, lon, start, today)
        if power:
            d.power_daily.bulk_write([UpdateOne({"_id": r["date"]}, {"$set": r}, upsert=True)
                                      for r in power], ordered=False)
        log(f"reanalysis: {len(power)} NASA POWER days")
    except Exception as exc:
        log(f"reanalysis: NASA POWER failed ({exc})")

    since = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
    field = {h: v for h, v in station_hours(d, since, centred=True).items() if v["n"] >= 6}
    ref = {aware(r["_id"]): r for r in d.era5_hourly.find({"_id": {"$gte": since.replace(tzinfo=None)}})}

    stats = {"temp": compare(field, ref, "temp", "temp_c"),
             "rh": compare(field, ref, "rh", "rh_pct"),
             "dew": compare(field, ref, "dew", "dew_c")}

    # Daily means in IST for the chart. A field day needs 12 good hours.
    def daily(rows, key):
        acc = defaultdict(list)
        for h, v in rows.items():
            if v.get(key) is not None:
                acc[(h + IST).date().isoformat()].append(v[key])
        return acc
    f_t, f_r = daily(field, "temp"), daily(field, "rh")
    r_t, r_r = daily(ref, "temp_c"), daily(ref, "rh_pct")
    # Rain values are totals for the hour ending at the stamp: file each one
    # under the IST day its middle falls in. A day needs 20 hours to count.
    rain = defaultdict(list)
    for h, v in ref.items():
        if v.get("rain_mm") is not None:
            rain[(h - timedelta(minutes=30) + IST).date().isoformat()].append(v["rain_mm"])
    pw = {r["_id"]: r for r in d.power_daily.find({"_id": {"$gte": start.isoformat()}})}
    days = sorted(set(r_t) | set(f_t) | set(pw) | set(rain))
    series = []
    for day in days:
        def mean(acc, need):
            v = acc.get(day) or []
            return round(sum(v) / len(v), 2) if len(v) >= need else None
        r = rain.get(day) or []
        series.append({
            "date": day,
            "field_temp": mean(f_t, 12), "era5_temp": mean(r_t, 20),
            "field_rh": mean(f_r, 12), "era5_rh": mean(r_r, 20),
            "rain_mm": round(sum(r), 1) if len(r) >= 20 else None,
            "solar_kwh_m2": (pw.get(day) or {}).get("solar_kwh_m2"),
        })

    # Days the reanalysis has but the station does not (under 12 good hours).
    gap_days = [s["date"] for s in series if s["field_temp"] is None and s["era5_temp"] is not None]
    rain_days = [s for s in series if s["rain_mm"] is not None]
    sun = [s["solar_kwh_m2"] for s in series if s["solar_kwh_m2"] is not None]
    d.feeds.replace_one({"_id": "comparison"}, {
        "_id": "comparison", "generated_at": now_utc(), "lat": lat, "lon": lon,
        "era5": meta, "era5_until": era[-1]["time"] if era else None,
        "stats": stats, "series": series, "modelled_days": gap_days,
        "rain": {"total_mm": round(sum(s["rain_mm"] for s in rain_days), 1),
                 "days": len(rain_days),
                 "wet_days": sum(1 for s in rain_days if s["rain_mm"] >= 1.0),
                 "from": rain_days[0]["date"] if rain_days else None,
                 "until": rain_days[-1]["date"] if rain_days else None},
        "sun": {"mean_kwh_m2": round(sum(sun) / len(sun), 2) if sun else None, "days": len(sun)},
    }, upsert=True)
    t = stats["temp"] or {}
    log(f"reanalysis: field vs ERA5-Land temperature bias {t.get('bias')} C, "
        f"r {t.get('r')}, {t.get('n_hours')} hours")


# ------------------------------------------------------------------- NDVI

def run_satellite(d):
    lat, lon = field_point()
    radius = float(os.getenv("FIELD_RADIUS_M", "15"))
    items = sources.s2_items(lat, lon, sources.season_start(), now_utc().date())
    done = {doc["_id"] for doc in d.ndvi.find({"error": {"$exists": False},
                                               "lat": lat, "lon": lon}, {"_id": 1})}
    new = 0
    for it in items:
        if it["id"] in done:
            continue
        try:
            stats = sources.s2_ndvi(it, lat, lon, radius)
            stats.update({"_id": it["id"], "lat": lat, "lon": lon,
                          "acquired": datetime.fromisoformat(it["datetime"].replace("Z", "+00:00"))})
            d.ndvi.replace_one({"_id": it["id"]}, stats, upsert=True)
            new += 1
        except Exception as exc:
            d.ndvi.replace_one({"_id": it["id"]}, {"_id": it["id"], "lat": lat, "lon": lon,
                                                   "error": str(exc)[:200]}, upsert=True)
            log(f"satellite: {it['id']} failed ({exc})")

    passes = list(d.ndvi.find({"error": {"$exists": False}, "lat": lat, "lon": lon}).sort("acquired", 1))
    clear = [p for p in passes if p.get("ndvi") is not None
             and p["valid_px"] >= 0.6 * p["total_px"]]
    d.feeds.replace_one({"_id": "satellite"}, {
        "_id": "satellite", "generated_at": now_utc(), "lat": lat, "lon": lon,
        "radius_m": radius, "passes": len(passes), "clear": len(clear),
        "series": [{"acquired": p["acquired"], "ndvi": p["ndvi"],
                    "valid": round(p["valid_px"] / p["total_px"], 2),
                    "cloud_cover": p.get("cloud_cover")} for p in clear],
    }, upsert=True)
    log(f"satellite: {len(items)} passes listed, {new} new, {len(clear)} clear")


# ------------------------------------------------------------------ radar

def run_radar(d):
    lat, lon = field_point()
    radius = float(os.getenv("FIELD_RADIUS_M", "15"))
    items = sources.s1_items(lat, lon, sources.season_start(), now_utc().date())
    done = {doc["_id"] for doc in d.radar.find({"error": {"$exists": False},
                                                "lat": lat, "lon": lon}, {"_id": 1})}
    todo = [it for it in items if it["id"] not in done]
    token = sources.mpc_token() if todo else None
    new = 0
    for it in todo:
        try:
            st = sources.s1_backscatter(it, lat, lon, radius, token)
            st.update({"_id": it["id"], "lat": lat, "lon": lon,
                       "acquired": datetime.fromisoformat(it["datetime"].replace("Z", "+00:00"))})
            d.radar.replace_one({"_id": it["id"]}, st, upsert=True)
            new += 1
        except Exception as exc:
            d.radar.replace_one({"_id": it["id"]}, {"_id": it["id"], "lat": lat, "lon": lon,
                                                    "error": str(exc)[:200]}, upsert=True)
            log(f"radar: {it['id']} failed ({exc})")

    # Two slices of one pass can both cover the field: keep one per orbit.
    seen, series = set(), []
    for p in d.radar.find({"error": {"$exists": False}, "lat": lat, "lon": lon,
                           "rvi": {"$ne": None}}).sort("acquired", 1):
        key = (p.get("platform"), p.get("abs_orbit"))
        if key in seen:
            continue
        seen.add(key)
        series.append({"acquired": p["acquired"], "vv_db": p["vv_db"], "vh_db": p["vh_db"],
                       "rvi": p["rvi"], "orbit": p.get("orbit"), "track": p.get("track")})
    d.feeds.replace_one({"_id": "radar"}, {
        "_id": "radar", "generated_at": now_utc(), "lat": lat, "lon": lon,
        "radius_m": radius, "passes": len(series), "series": series,
    }, upsert=True)
    log(f"radar: {len(items)} scenes listed, {new} new, {len(series)} passes")


# ---------------------------------------------------------------- anomaly

def run_anomaly(d):
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    hours = station_hours(d, now - timedelta(days=15), now)

    def local_hour(h):
        return (h + IST).hour

    recent_start = now - timedelta(hours=6)
    history = [(local_hour(h), {"air_temp_c": v["temp"], "humidity_pct": v["rh"],
                                "dew_point_c": v["dew"]})
               for h, v in hours.items() if h < recent_start and v["n"] >= 6]
    recent = [(f"{(h + IST):%d %b %H:00}", local_hour(h),
               {"air_temp_c": v["temp"], "humidity_pct": v["rh"], "dew_point_c": v["dew"]})
              for h, v in sorted(hours.items(), reverse=True) if h >= recent_start and v["n"] >= 6]
    base = anomaly_mod.baseline(history)
    flags = anomaly_mod.check(recent, base)
    d.feeds.replace_one({"_id": "anomaly"}, {
        "_id": "anomaly", "generated_at": now_utc(),
        "baseline_days": len({(h + IST).date() for h in hours if h < recent_start}),
        "checked_hours": len(recent), "flags": flags,
    }, upsert=True)
    log(f"anomaly: {len(recent)} recent hours checked, {len(flags)} flagged")


# ------------------------------------------------- disease, or just stress?

def daily_levels(d, start, end):
    """Infection level per day from the station. Day D is scored on the 24
    hours from noon IST on D-1 to noon on D, so a night's wet spell is never
    split in two. Days with under 18 usable hours are left out."""
    by_day = defaultdict(list)
    for h, v in station_hours(d, start - timedelta(days=1), end).items():
        if v["wet"] is None or v["temp"] is None:
            continue
        by_day[(h + IST + timedelta(hours=12)).date()].append((v["wet"], v["temp"]))
    out = {}
    for day, hrs in by_day.items():
        if len(hrs) < 18:
            continue
        pressures, wet_h, _ = forecast_risk.score_window(hrs)
        out[day] = {"levels": {k: forecast_risk.level(p) for k, p in pressures.items()},
                    "wet_h": wet_h}
    return out


def run_check(d):
    sat = d.feeds.find_one({"_id": "satellite"}) or {}
    clear = [(aware(p["acquired"]), p["ndvi"]) for p in sat.get("series") or []]
    canopy = attribution.canopy_change(clear)
    doc = {"_id": "check", "generated_at": now_utc(), "canopy": canopy}
    if canopy:
        start, end = canopy["from"], canopy["to"]
        levels = daily_levels(d, start, end)
        weather = attribution.weather_between(levels, (start + IST).date(), (end + IST).date())
        doc.update({"weather": weather, **attribution.verdict(canopy, weather)})
        # What the weather has done since that pass, for the "next look" line.
        since = daily_levels(d, end, now_utc())
        doc["since"] = attribution.weather_between(since, (end + IST).date() + timedelta(days=1),
                                                   (now_utc() + IST).date())
    else:
        doc.update(attribution.verdict(None, None))
    d.feeds.replace_one({"_id": "check"}, doc, upsert=True)
    log(f"check: {doc['state']}" + (f", NDVI {canopy['delta']:+.2f} ({canopy['trend']})" if canopy else ""))


# ------------------------------------------------------------- calibration

def season_since():
    return datetime.combine(sources.season_start(), datetime.min.time(), tzinfo=timezone.utc)


def run_calibration(d):
    rows = list(d.field_log.find({}, {"_id": 0}))
    levels = daily_levels(d, season_since(), now_utc())
    result = calibration_mod.calibrate(rows, levels)
    d.feeds.replace_one({"_id": "calibration"},
                        {"_id": "calibration", "generated_at": now_utc(), **result}, upsert=True)
    log(f"calibration: {result['visits']} visits, {result['judged']} judged, "
        f"{result['positives']} with new symptoms, ready={result['ready']}")


# ------------------------------------------------------- healthy baseline

def first_symptoms(d):
    """Datetime of the first field-log visit with symptoms, or None."""
    rows = list(d.field_log.find({"severity": {"$gte": 1}, "disease": {"$in": list(calibration_mod.GROUPS)}},
                                 {"date": 1, "_id": 0}).sort("date", 1).limit(1))
    if not rows:
        return None
    return datetime.combine(date.fromisoformat(rows[0]["date"]), datetime.min.time(),
                            tzinfo=timezone.utc) - IST


def run_baseline(d):
    since = season_since()
    sat = d.feeds.find_one({"_id": "satellite"}) or {}
    clear = [(aware(p["acquired"]), p["ndvi"]) for p in sat.get("series") or []]
    passes = [((t + IST).date().toordinal(), v) for t, v in clear]

    field = {h: v for h, v in station_hours(d, since, centred=True).items()
             if v["n"] >= 6 and v["temp"] is not None and v["dew"] is not None}
    rows = []
    for r in d.era5_hourly.find({"_id": {"$gte": since.replace(tzinfo=None)}}).sort("_id", 1):
        h = aware(r["_id"])
        st = field.get(h)
        if st is None or r.get("temp_c") is None or r.get("dew_c") is None:
            continue
        local = h + IST
        rows.append({"time": h, "local_hour": local.hour + local.minute / 60.0,
                     "ndvi": baseline_mod.ndvi_on(local.date().toordinal(), passes),
                     "st_temp": st["temp"], "st_dew": st["dew"],
                     "era_temp": r["temp_c"], "era_dew": r["dew_c"]})

    micro = {"state": "learning", "trained_hours": 0}
    if rows:
        check_from = rows[-1]["time"] - timedelta(days=baseline_mod.CHECK_DAYS)
        micro = baseline_mod.microclimate(rows, check_from, first_symptoms(d))

    ndvi = baseline_mod.canopy([p[0] for p in passes], [p[1] for p in passes],
                               min_points=5, sigma_floor=0.03, recent=1)
    rad = (d.feeds.find_one({"_id": "radar"}) or {}).get("series") or []
    radar = baseline_mod.canopy([(aware(p["acquired"]) + IST).date().toordinal() for p in rad],
                                [p["rvi"] for p in rad], min_points=9, sigma_floor=0.06, recent=3)
    state = baseline_mod.overall(micro, ndvi, radar)
    d.feeds.replace_one({"_id": "baseline"}, {
        "_id": "baseline", "generated_at": now_utc(), "state": state,
        "micro": micro, "ndvi": ndvi, "radar": radar,
        "checked_until": rows[-1]["time"] if rows else None,
    }, upsert=True)
    log(f"baseline: {state}; microclimate {micro.get('state')} "
        f"({micro.get('trained_hours')} h), NDVI {ndvi.get('state')}, radar {radar.get('state')}")


# ------------------------------------------------------- field correction

OUTSIDE_KEYS = ("temp_c", "rh_pct", "dew_c", "precip_mm", "wind_ms", "cloud_pct", "sw_wm2")
PAIR_KEYS = ("temp_c", "dew_c", "rh_pct", "cloud_pct", "wind_ms", "sw_wm2", "precip_mm")


def wet_check(pairs, field, model_t, model_d, lat, lon, days_back=None):
    """How many hours off the day-ahead wetness forecast was, before and
    after the field correction, on the held-back days.

    Each noon-to-noon day with the station's own hours (20 or more) and the
    day-ahead forecast for all 24 is scored three ways through the same leaf
    model: from what the station measured, from the raw forecast, and from
    the corrected forecast. -> {"days", "mae_raw", "mae_corrected", "bias_raw",
    "bias_corrected"} or None."""
    if not pairs or not (model_t or {}).get("holdout_hours"):
        return None
    n_hold = model_t["holdout_hours"]
    hold = pairs[-n_hold:]
    t0, t1 = hold[0][0], hold[-1][0]
    by_time = dict(pairs)
    hours = []
    h = t0
    while h <= t1:
        hours.append(h)
        h += timedelta(hours=1)
    lh = np.array([local_hour(x) for x in hours])

    def arr(src, key):
        return np.array([(src.get(x) or {}).get(key) if (src.get(x) or {}).get(key) is not None
                         else np.nan for x in hours], dtype=float)
    ft, fd = arr(by_time, "temp_c"), arr(by_time, "dew_c")
    cl, wd = arr(by_time, "cloud_pct"), arr(by_time, "wind_ms")
    sw, rn = arr(by_time, "sw_wm2"), arr(by_time, "precip_mm")
    ot, od = arr(field, "temp"), arr(field, "dew")
    have = np.isfinite(ft) & np.isfinite(fd)
    measured = np.isfinite(ot) & np.isfinite(od)
    cl = np.where(np.isfinite(cl), cl, 50.0)
    wd = np.where(np.isfinite(wd), wd, 1.5)
    sw = np.where(np.isfinite(sw), sw, leafwet.clear_sky_sw(hours, lat, lon) * (1 - 0.75 * cl / 100.0))
    rn = np.nan_to_num(rn, nan=0.0)
    ct = mos_mod.apply(model_t, lh, ft, cl, wd)
    cd = mos_mod.apply(model_d, lh, fd, cl, wd)

    def wet(t, dw):
        t = _interp(t)
        dw = np.minimum(_interp(dw), t)
        return leafwet.simulate(t, leafwet.rh_from_dew(t, dw), wd, cl, sw, rn)["wet"][0]
    w_obs, w_raw, w_cor = wet(ot, od), wet(ft, fd), wet(ct, cd)
    days = {}
    for i, x in enumerate(hours):
        days.setdefault((x + IST + timedelta(hours=12)).date(), []).append(i)
    err_raw, err_cor = [], []
    for day, ix in days.items():
        ix = np.array(ix)
        if len(ix) < 24 or measured[ix].sum() < 20 or not have[ix].all():
            continue
        o = float(w_obs[ix].sum())
        err_raw.append(float(w_raw[ix].sum()) - o)
        err_cor.append(float(w_cor[ix].sum()) - o)
    if not err_raw:
        return None
    return {"days": len(err_raw),
            "mae_raw": round(float(np.mean(np.abs(err_raw))), 1),
            "mae_corrected": round(float(np.mean(np.abs(err_cor))), 1),
            "bias_raw": round(float(np.mean(err_raw)), 1),
            "bias_corrected": round(float(np.mean(err_cor)), 1)}


def local_hour(h):
    return (h + IST).hour


def run_mos(d):
    """Train the correction from forecast to field (src/ml/mos.py).

    Pairs are day-ahead forecasts for hours the station measured: first
    from Open-Meteo's archive of earlier runs, so the correction can start
    with this season instead of next; then from our own forecast archive.
    """
    lat, lon = field_point()
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    since = season_since()
    first = d.readings.find_one({}, {"ts": 1}, sort=[("ts", 1)])
    start = max((aware(first["ts"]) + IST).date() if first else since.date(), since.date())
    last = d.mos_pairs.find_one({"source": "previous_day1"}, sort=[("_id", -1)])
    if last:
        start = max(start, (aware(last["_id"]) + IST).date())
    end = (now + IST).date() - timedelta(days=1)
    if start <= end:
        try:
            rows = sources.previous_runs(lat, lon, start, end)
            if rows:
                d.mos_pairs.bulk_write([UpdateOne({"_id": r["time"]}, {"$set": dict(
                    {k: r.get(k) for k in PAIR_KEYS}, source="previous_day1")}, upsert=True)
                    for r in rows], ordered=False)
            log(f"mos: {len(rows)} day-ahead forecast hours from earlier runs")
        except Exception as exc:
            log(f"mos: earlier runs unavailable ({exc}); using our own archive only")
    added = 0
    for doc in d.forecast_archive.find({"hourly": {"$exists": True}}, {"hourly": 1}):
        issue = aware(doc["_id"])
        for r in doc.get("hourly") or []:
            t = aware(r.get("time"))
            if t is None or r.get("temp_c") is None:
                continue
            lead = (t - issue).total_seconds() / 3600.0
            if 24 <= lead < 48:
                res = d.mos_pairs.update_one({"_id": t}, {"$setOnInsert": dict(
                    {k: r.get(k) for k in PAIR_KEYS}, source="archive")}, upsert=True)
                added += 1 if res.upserted_id is not None else 0

    field = {h: v for h, v in station_hours(d, since, now, centred=True).items()
             if v["n"] >= 6 and v["temp"] is not None and v["dew"] is not None}
    pairs = [(aware(p["_id"]), p) for p in d.mos_pairs.find({}).sort("_id", 1)]
    pairs = [(h, p) for h, p in pairs if h in field and p.get("temp_c") is not None]
    lh = np.array([local_hour(h) for h, _ in pairs], dtype=float)
    cloud = np.array([p.get("cloud_pct") if p.get("cloud_pct") is not None else np.nan for _, p in pairs], dtype=float)
    wind = np.array([p.get("wind_ms") if p.get("wind_ms") is not None else np.nan for _, p in pairs], dtype=float)
    fc_t = np.array([p["temp_c"] for _, p in pairs], dtype=float)
    fc_d = np.array([p["dew_c"] if p.get("dew_c") is not None else np.nan for _, p in pairs], dtype=float)
    ob_t = np.array([field[h]["temp"] for h, _ in pairs], dtype=float)
    ob_d = np.array([field[h]["dew"] for h, _ in pairs], dtype=float)
    days = np.array([(h - pairs[0][0]).total_seconds() / 86400.0 for h, _ in pairs]) if pairs else None
    m_t = mos_mod.fit(lh, fc_t, ob_t, cloud, wind, days) if pairs else {"n": 0, "use": False, "state": "learning"}
    m_d = mos_mod.fit(lh, fc_d, ob_d, cloud, wind, days) if pairs else {"n": 0, "use": False, "state": "learning"}
    try:
        wet = wet_check(pairs, field, m_t, m_d, lat, lon)
    except Exception as exc:
        log(f"mos: wetness check failed ({exc})")
        wet = None
    d.feeds.replace_one({"_id": "mos"}, {
        "_id": "mos", "generated_at": now_utc(), "pairs": len(pairs), "from_archive": added,
        "first": pairs[0][0] if pairs else None, "last": pairs[-1][0] if pairs else None,
        "temp": m_t, "dew": m_d, "wet": wet,
    }, upsert=True)
    log(f"mos: {len(pairs)} pairs; temperature {m_t.get('state')} "
        f"({m_t.get('rmse_raw')} -> {m_t.get('rmse_corrected')} C), "
        f"dew point {m_d.get('state')} ({m_d.get('rmse_raw')} -> {m_d.get('rmse_corrected')} C)"
        + (f"; wet hours a day off by {wet['mae_raw']} h raw, {wet['mae_corrected']} h corrected "
           f"over {wet['days']} days" if wet else ""))


# ------------------------------------------------------------------- odds

def outside_hours(d, since, until):
    """{hour: values}: ERA5 first, the forecast's own past hours on top."""
    q = {"_id": {"$gte": since.replace(tzinfo=None), "$lt": until.replace(tzinfo=None)}}
    out = {}
    for r in d.era5_hourly.find(q):
        out[aware(r["_id"])] = {"temp_c": r.get("temp_c"), "rh_pct": r.get("rh_pct"),
                                "dew_c": r.get("dew_c"), "precip_mm": r.get("rain_mm"),
                                "wind_ms": r.get("wind_ms"), "cloud_pct": r.get("cloud_pct"),
                                "sw_wm2": r.get("sw_wm2")}
    for r in d.outside_hourly.find(q):
        cur = out.setdefault(aware(r["_id"]), {})
        for k in OUTSIDE_KEYS:
            if r.get(k) is not None:
                cur[k] = r[k]
    return out


def _interp(a):
    a = np.asarray(a, dtype=float)
    ok = np.isfinite(a)
    if ok.all() or not ok.any():
        return a
    x = np.arange(len(a))
    return np.interp(x, x[ok], a[ok])


def odds_inputs(d, since, now, lat, lon):
    """Every hour from the first one with any weather to now, gaps filled.

    Temperature and dew point are the station's where it measured (three or
    more readings in the hour), otherwise the outside value shifted by the
    field's usual offset at that local hour. Wind, cloud, sunshine and rain
    always come from outside: the station does not measure them. wet_obs is
    the leaf wetness sensor's hour (1 wet, 0 dry), NaN where it did not
    report; the odds engine puts it in place of the modelled wetness."""
    st = station_hours(d, since, now)
    out = outside_hours(d, since, now)
    starts = [h for h, v in st.items() if v["n"] >= 3] + [h for h, v in out.items() if v.get("temp_c") is not None]
    if not starts:
        return None
    h = max(min(starts), since).replace(minute=0, second=0, microsecond=0)
    hours = []
    while h < now:
        hours.append(h)
        h += timedelta(hours=1)
    lh = np.array([local_hour(x) for x in hours])

    def stv(key):
        return np.array([st[x][key] if x in st and st[x]["n"] >= 3 and st[x][key] is not None
                         else np.nan for x in hours], dtype=float)

    def ov(key):
        return np.array([(out.get(x) or {}).get(key) if (out.get(x) or {}).get(key) is not None
                         else np.nan for x in hours], dtype=float)
    s_t, s_d = stv("temp"), stv("dew")
    o_t, o_d, o_rh = ov("temp_c"), ov("dew_c"), ov("rh_pct")
    with np.errstate(invalid="ignore"):
        o_d = np.where(np.isfinite(o_d), o_d, leafwet.dew_from_rh(o_t, o_rh))
    # The field's offset drifts with the season (see src/ml/mos.py), so learn
    # it from the last 30 days when they hold enough hours.
    recent = np.array([x >= now - timedelta(days=30) for x in hours])

    def offsets(s_, o_):
        both = recent & np.isfinite(s_) & np.isfinite(o_)
        if both.sum() >= 24 * 5:
            return odds_mod.hour_offsets(lh[recent], s_[recent], o_[recent])
        return odds_mod.hour_offsets(lh, s_, o_)
    off_t = offsets(s_t, o_t)
    off_d = offsets(s_d, o_d)
    temp, measured = odds_mod.merge(lh, s_t, o_t, off_t)
    dew, _ = odds_mod.merge(lh, s_d, o_d, off_d)
    temp = _interp(temp)
    dew = np.minimum(_interp(dew), temp)
    cloud = _interp(ov("cloud_pct"))
    cloud = np.where(np.isfinite(cloud), cloud, 50.0)
    wind = _interp(ov("wind_ms"))
    wind = np.where(np.isfinite(wind), wind, 1.5)
    sw = ov("sw_wm2")
    sw = np.where(np.isfinite(sw), sw, leafwet.clear_sky_sw(hours, lat, lon) * (1 - 0.75 * cloud / 100.0))
    rain = np.nan_to_num(ov("precip_mm"), nan=0.0)
    wet_obs = np.array([st[x]["wet_obs"] if x in st and st[x].get("wet_obs") is not None
                        else np.nan for x in hours], dtype=float)
    return {"hours": hours, "measured": measured, "off_t": off_t, "off_d": off_d,
            "station_temp": s_t, "station_dew": s_d,
            "drivers": {"temp": temp, "rh": leafwet.rh_from_dew(temp, dew), "wind": wind,
                        "cloud": cloud, "sw": sw, "rain": rain, "wet_obs": wet_obs}}


def transplant_date(d, rows):
    """TRANSPLANT_DATE in .env, else the first field-log visit that says the
    crop was just transplanted. None if neither: the odds then weigh every
    crop stage evenly, and say so."""
    v = os.getenv("TRANSPLANT_DATE", "").strip()
    if v:
        try:
            return date.fromisoformat(v)
        except ValueError:
            log(f"odds: TRANSPLANT_DATE {v!r} is not YYYY-MM-DD; ignored")
    days = sorted(str(r["date"])[:10] for r in rows if r.get("stage") == "transplanted" and r.get("date"))
    return date.fromisoformat(days[0]) if days else None


def symptom_days(rows):
    out = {}
    for r in rows:
        g = r.get("disease")
        if g in pz.GROUPS and int(r.get("severity") or 0) >= 1 and int(r.get("plants_affected") or 0) >= 1:
            out.setdefault(g, []).append(date.fromisoformat(str(r["date"])[:10]))
    return out


def canopy_cover(d):
    """Share of ground covered, from the latest clear Sentinel-2 NDVI
    (scaled NDVI squared, Carlson & Ripley 1997: bare soil 0.15, full 0.85)."""
    series = (d.feeds.find_one({"_id": "satellite"}) or {}).get("series") or []
    if not series or series[-1].get("ndvi") is None:
        return None
    n = (float(series[-1]["ndvi"]) - 0.15) / (0.85 - 0.15)
    return round(min(max(n, 0.0), 1.0) ** 2, 2)


def corrected_members(d, now, inp, mos_doc):
    """The ensemble runs for the next 72 h, corrected to the field.

    Field correction (src/ml/mos.py) where it has earned its place, the
    field's hour-of-day offsets otherwise; then the station's error at its
    latest hour, faded over six hours. -> (members, how)"""
    doc = d.feeds.find_one({"_id": "forecast_members"})
    if not doc:
        return [], "none"
    issued = aware(doc.get("issued_for"))
    if issued is None or now - issued > timedelta(hours=12):
        return [], "stale"
    use_ens = bool(doc.get("members"))
    hours = [aware(t) for t in (doc["hours"] if use_ens else doc.get("det_hours") or [])]
    runs = doc["members"] if use_ens else ([doc["det"]] if doc.get("det") else [])
    idx = [i for i, h in enumerate(hours) if now <= h < now + timedelta(hours=72)]
    if len(idx) < 24 or not runs:
        return [], "short"
    fut = [hours[i] for i in idx]
    lh = np.array([local_hour(h) for h in fut])
    m_t, m_d = mos_doc.get("temp") or {}, mos_doc.get("dew") or {}

    # The station's latest measured hour, if it is recent.
    err_at = None
    meas = np.flatnonzero(inp["measured"])
    if meas.size:
        j = int(meas[-1])
        if now - inp["hours"][j] <= timedelta(hours=3) and inp["hours"][j] in hours:
            err_at = (inp["hours"][j], hours.index(inp["hours"][j]), j)

    def col(run, key, rows_idx, default=np.nan):
        vals = run.get(key) or []
        return np.array([vals[i] if i < len(vals) and vals[i] is not None else default
                         for i in rows_idx], dtype=float)

    def correct(run, rows_idx, lhs):
        t = col(run, "temp", rows_idx)
        dw = col(run, "dew", rows_idx)
        with np.errstate(invalid="ignore"):
            dw = np.where(np.isfinite(dw), dw, leafwet.dew_from_rh(t, col(run, "rh", rows_idx)))
        cl = col(run, "cloud", rows_idx, 50.0)
        wd = col(run, "wind", rows_idx, 1.5)
        t2 = mos_mod.apply(m_t, lhs, t, cl, wd) if m_t.get("use") else t + inp["off_t"][lhs]
        d2 = mos_mod.apply(m_d, lhs, dw, cl, wd) if m_d.get("use") else dw + inp["off_d"][lhs]
        return t2, d2, cl, wd

    err_t = err_d = None
    lead = np.array([(h - now).total_seconds() / 3600.0 for h in fut])
    if err_at:
        t_now, k, j = err_at
        vals = [correct(r, [k], np.array([local_hour(t_now)])) for r in runs]
        ft = np.nanmean([v[0][0] for v in vals])
        fd = np.nanmean([v[1][0] for v in vals])
        dew_obs = inp["station_dew"][j]
        err_t = float(inp["station_temp"][j] - ft) if np.isfinite(ft) else None
        err_d = float(dew_obs - fd) if np.isfinite(fd) and np.isfinite(dew_obs) else None
        lead = np.array([(h - t_now).total_seconds() / 3600.0 for h in fut])

    out = []
    for r in runs:
        t2, d2, cl, wd = correct(r, idx, lh)
        if err_t is not None:
            t2 = mos_mod.blend_now(t2, lead, err_t)
        if err_d is not None:
            d2 = mos_mod.blend_now(d2, lead, err_d)
        t2 = _interp(t2)
        d2 = np.minimum(_interp(d2), t2)
        sw = col(r, "sw", idx)
        sw = np.where(np.isfinite(sw), sw, leafwet.clear_sky_sw(fut, *field_point()) * (1 - 0.75 * cl / 100.0))
        out.append({"hours": fut, "temp": t2, "rh": leafwet.rh_from_dew(t2, d2), "wind": wd,
                    "cloud": cl, "sw": sw, "rain": np.nan_to_num(col(r, "rain", idx), nan=0.0)})
    how = ("field correction" if m_t.get("use") or m_d.get("use") else "hour offsets")
    how += " + nowcast" if err_t is not None else ""
    return out, how


def climate_rank(d, now, x):
    """Where today's unscaled hazard sits among the same fortnight in past
    years (run_climate). -> {"pct", "years", "n"} or None."""
    doc = d.feeds.find_one({"_id": "climate"})
    if not doc or x is None:
        return None
    today = (now + IST).date()
    pool = []
    for k in range(-7, 8):
        key = (today + timedelta(days=k)).strftime("%m-%d")
        pool.extend(doc.get("by_day", {}).get(key) or [])
    if len(pool) < 30:
        return None
    pct = round(100.0 * sum(1 for v in pool if v < x) / len(pool))
    return {"pct": pct, "years": doc.get("years"), "n": len(pool),
            "first": doc.get("first"), "last": doc.get("last")}


def run_odds(d):
    lat, lon = field_point()
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    inp = odds_inputs(d, season_since(), now, lat, lon)
    if not inp or len(inp["hours"]) < odds_mod.SPIN_UP_H:
        raise RuntimeError("fewer than 72 hours of weather so far; the odds start after that")
    rows = list(d.field_log.find({}, {"_id": 0}))
    transplanted = transplant_date(d, rows)
    stage = pz.stage_of(((now + IST).date() - transplanted).days) if transplanted else None
    check = d.feeds.find_one({"_id": "check"}) or {}
    mos_doc = d.feeds.find_one({"_id": "mos"}) or {}

    def sigma(m, fallback):
        return float(m.get("sigma") or fallback) if m.get("state") in ("on", "off") else fallback
    ctx = {"transplanted": transplanted, "stage": stage, "symptoms": symptom_days(rows),
           "canopy_state": check.get("state"), "cover": canopy_cover(d), "measured": inp["measured"],
           "sigma_temp": sigma(mos_doc.get("temp") or {}, 1.2),
           "sigma_dew": sigma(mos_doc.get("dew") or {}, 1.6)}

    base = odds_mod.season(inp["hours"], inp["drivers"], ctx)
    visits = {}
    for (day, g), y in calibration_mod.outcomes(calibration_mod.visits(rows)).items():
        visits.setdefault(day, {})[g] = y
    pairs = odds_mod.calibration_pairs([{"date": k, "outcomes": v} for k, v in visits.items()],
                                       base["exposure"])
    post = odds_mod.posterior(pairs)
    scale = post["median"]
    season_rows = odds_mod.season_rows(base, scale)
    incubating = odds_mod.incubating(base, scale, now)

    members, how = corrected_members(d, now, inp, mos_doc)
    if not members:
        raise RuntimeError(f"no usable forecast ({how}); run the forecast job first")
    hp = odds_mod.SPIN_UP_H
    past = {k: v[-hp:] for k, v in inp["drivers"].items()}
    fc = odds_mod.forecast(inp["hours"][-hp:], past, members, ctx, scale,
                           n_param=20 if len(members) > 5 else 200,
                           seed=int(now.timestamp()) % (2 ** 31))
    w24 = fc["windows"][0]
    share = w24.get("share") or {}
    top = max(share, key=share.get) if share and max(share.values()) > 0 else None

    ps = [r["p"] for r in season_rows]
    rank = round(100.0 * sum(1 for v in ps if v < w24["p"]) / len(ps)) if ps else None
    hrs, temp = inp["hours"], inp["drivers"]["temp"]
    week = [i for i, h in enumerate(hrs) if h >= now - timedelta(days=7)]
    growing = odds_mod.growing_conditions(
        [temp[i] for i in week if 8 <= local_hour(hrs[i]) < 18],
        [temp[i] for i in week if local_hour(hrs[i]) >= 20 or local_hour(hrs[i]) < 6])
    measured_72 = float(np.mean(inp["measured"][-hp:]))
    spell = fc.get("spell")
    doc = {
        "_id": "odds", "generated_at": now_utc(), "issued_for": now,
        "now": {"p": w24["p"], "lo": w24["lo"], "hi": w24["hi"], "level": w24["level"],
                "words": odds_mod.words(w24["p"]), "window_h": 24},
        "windows": fc["windows"], "hourly": fc["hourly"],
        "top": top, "top_name": pz.GROUP_NAMES.get(top),
        "drivers": odds_mod.drivers_text(fc, ctx, top),
        "spell": ({"from": spell["from"], "to": spell["to"], "hours": spell["hours"],
                   "temp": fc.get("spell_temp")} if spell else None),
        "incubating": incubating,
        "season": season_rows[-150:],
        "context": {"season_rank_pct": rank, "climate": climate_rank(d, now, w24.get("x")),
                    "growing": growing},
        "calibration": {"checked": post["ready"], "visits": post["n"], "outbreaks": post["k"],
                        "scale": round(scale / odds_mod.LAMBDA0, 3),
                        "scale_lo": round(post["lo"] / odds_mod.LAMBDA0, 3),
                        "scale_hi": round(post["hi"] / odds_mod.LAMBDA0, 3),
                        "needs": {"visits": max(0, odds_mod.MIN_JUDGED - post["n"]),
                                  "outbreaks": max(0, odds_mod.MIN_POSITIVE - post["k"])}},
        "inputs": {"members": fc["members"], "worlds": fc["worlds"], "correction": how,
                   "station_share_72h": round(measured_72, 2), "cover": ctx["cover"],
                   "stage": stage, "transplanted": transplanted.isoformat() if transplanted else None,
                   "canopy_state": ctx["canopy_state"], "season_hours": len(hrs),
                   "station_hours": int(np.sum(inp["measured"]))},
    }
    d.feeds.replace_one({"_id": "odds"}, doc, upsert=True)
    log(f"odds: {round(100 * w24['p'])}% next 24 h ({round(100 * w24['lo'])}-{round(100 * w24['hi'])}), "
        f"{w24['level']}, {fc['members']} runs x {fc['worlds'] // max(fc['members'], 1)}, {how}; "
        f"{post['n']} judged visits")


# ---------------------------------------------------------------- climate

CLIMATE_YEARS = int(os.getenv("CLIMATE_YEARS", "20"))


def run_climate(d):
    """The same odds for the same field in past seasons, from ERA5, so a day
    can be compared with its own calendar: "wetter than 17 of the last 20
    late Octobers". Shifted to the field by the station's hour offsets, and
    ranked on unscaled hazard so the field log moving the scale does not
    move the ranking. Monthly; about 20 requests."""
    lat, lon = field_point()
    now = now_utc().replace(minute=0, second=0, microsecond=0)
    inp = odds_inputs(d, season_since(), now, lat, lon)
    off_t = inp["off_t"] if inp else np.zeros(24)
    off_d = inp["off_d"] if inp else np.zeros(24)
    transplanted = transplant_date(d, list(d.field_log.find({}, {"_id": 0})))
    season0 = sources.season_start()
    this = (now + IST).date()
    first_season = this.year if this >= season0.replace(year=this.year) else this.year - 1
    by_day, done = {}, []
    for k in range(1, CLIMATE_YEARS + 1):
        y = first_season - k
        start = season0.replace(year=y)
        end = date(y + 1, 3, 31)
        try:
            rows = sources.era5_history(lat, lon, start - timedelta(days=1), end)
        except Exception as exc:
            log(f"climate: {y} failed ({exc})")
            continue
        if len(rows) < 24 * 60:
            continue
        hours = [r["time"] for r in rows]
        lh = np.array([local_hour(h) for h in hours])
        t = _interp(np.array([r["temp_c"] for r in rows], dtype=float)) + off_t[lh]
        dw = np.array([r["dew_c"] if r.get("dew_c") is not None else np.nan for r in rows], dtype=float)
        dw = np.minimum(_interp(dw) + off_d[lh], t)

        def col(key, default):
            a = _interp(np.array([r.get(key) if r.get(key) is not None else np.nan for r in rows], dtype=float))
            return np.where(np.isfinite(a), a, default)
        drivers = {"temp": t, "rh": leafwet.rh_from_dew(t, dw), "wind": col("wind_ms", 1.5),
                   "cloud": col("cloud_pct", 50.0), "sw": col("sw_wm2", 0.0),
                   "rain": col("precip_mm", 0.0)}
        tp = None
        if transplanted:
            try:
                tp = transplanted.replace(year=y if transplanted.month >= 7 else y + 1)
            except ValueError:
                tp = None
        base = odds_mod.season(hours, drivers, {"transplanted": tp, "symptoms": {}, "cover": 0.5},
                               n=8, seed=k)
        for day in base["days"]:
            x = sum(base["exposure"][g][day] for g in pz.GROUPS)
            by_day.setdefault(day.strftime("%m-%d"), []).append(round(x, 5))
        done.append(y)
    if not done:
        raise RuntimeError("no past season came back")
    d.feeds.replace_one({"_id": "climate"}, {
        "_id": "climate", "generated_at": now_utc(), "years": len(done),
        "first": min(done), "last": max(done), "by_day": by_day,
    }, upsert=True)
    log(f"climate: {len(done)} past seasons, {min(done)} to {max(done)}")


# ------------------------------------------------------------------ alerts

def run_alerts(d):
    fc = d.feeds.find_one({"_id": "forecast"})
    state = d.feeds.find_one({"_id": "alerts"}) or {"_id": "alerts"}
    cal = d.feeds.find_one({"_id": "calibration"})
    odds = d.feeds.find_one({"_id": "odds"})
    result = alerts_mod.evaluate(fc, state, cal, now_utc(), odds)
    for msg in result["send"]:
        msg["sent"] = alerts_mod.deliver(msg)
    result["state"]["_id"] = "alerts"
    result["state"]["generated_at"] = now_utc()
    if result["send"]:
        result["state"].setdefault("history", [])
        result["state"]["history"] = (result["send"] + result["state"]["history"])[:30]
    d.feeds.replace_one({"_id": "alerts"}, result["state"], upsert=True)
    log(f"alerts: {len(result['send'])} to send, "
        f"{sum(1 for m in result['send'] if m['sent'])} delivered")


# ------------------------------------------------------------------ runner

JOBS = {"forecast": run_forecast, "reanalysis": run_reanalysis,
        "satellite": run_satellite, "radar": run_radar,
        "anomaly": run_anomaly, "check": run_check,
        "calibration": run_calibration, "baseline": run_baseline,
        "mos": run_mos, "climate": run_climate,
        "odds": run_odds, "alerts": run_alerts}
MAX_AGE_H = {"forecast": 3, "reanalysis": 20, "satellite": 20, "radar": 20,
             "anomaly": 0, "check": 6, "calibration": 6, "baseline": 12,
             "mos": 24, "climate": 24 * 30, "odds": 0, "alerts": 0}


def main(argv):
    mode = argv[1] if len(argv) > 1 else "auto"
    d = db()
    status = d.feeds.find_one({"_id": "status"}) or {"_id": "status"}
    if mode in JOBS:
        todo = [mode]
    elif mode == "all":
        todo = list(JOBS)
    elif mode == "auto":
        todo = []
        for name, max_age in MAX_AGE_H.items():
            last = aware((status.get(name) or {}).get("last_ok"))
            if last is None or now_utc() - last >= timedelta(hours=max_age):
                todo.append(name)
    else:
        sys.exit(f"unknown mode {mode!r}. Use auto, all, or one of: {', '.join(JOBS)}")

    failed = 0
    for name in todo:
        entry = status.get(name) or {}
        entry["last_run"] = now_utc()
        try:
            JOBS[name](d)
            entry["last_ok"] = now_utc()
            entry.pop("error", None)
        except Exception as exc:
            failed += 1
            entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
            log(f"{name}: FAILED {entry['error']}")
            traceback.print_exc()
        status[name] = entry
        d.feeds.replace_one({"_id": "status"}, status, upsert=True)
    return 1 if failed and failed == len(todo) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
