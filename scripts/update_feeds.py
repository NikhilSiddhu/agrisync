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
"""
import os
import sys
import traceback
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import MongoClient, UpdateOne

from src.feeds import sources
from src.ml import anomaly as anomaly_mod
from src.ml import attribution
from src.ml import forecast_risk

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
    -> {hour: {"n", "temp", "rh", "dew", "wet"}} where wet is the share of
    readings in that hour scored wet.

    By default an hour is HH:00 to HH:59, keyed by its start. centred=True
    keys HH:00 by the readings from HH-1:30 to HH:29 instead, which is what
    an instantaneous model value stamped HH:00 should be compared with."""
    shift = timedelta(minutes=30) if centred else timedelta(0)
    q = {"ts": {"$gte": since.replace(tzinfo=None)}, "quality_flags": {"$exists": False}}
    if until:
        q["ts"]["$lt"] = until.replace(tzinfo=None)
    acc = defaultdict(lambda: {"n": 0, "temp": 0.0, "rh": 0.0, "dew": 0.0, "wet": 0,
                               "nt": 0, "nr": 0, "nd": 0, "nw": 0})
    fields = {"ts": 1, "air_temp_c": 1, "humidity_pct": 1, "dew_point_c": 1,
              "leaf_wetness": 1, "_id": 0}
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
    out = {}
    for h, a in acc.items():
        out[h] = {
            "n": a["n"],
            "temp": a["temp"] / a["nt"] if a["nt"] else None,
            "rh": a["rh"] / a["nr"] if a["nr"] else None,
            "dew": a["dew"] / a["nd"] if a["nd"] else None,
            "wet": a["wet"] / a["nw"] if a["nw"] else None,
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
    log(f"forecast: {len(hourly)} hours, {len(members)} ensemble runs, "
        f"{len(observed)} observed hours")


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


# ------------------------------------------------------------------ runner

JOBS = {"forecast": run_forecast, "reanalysis": run_reanalysis,
        "satellite": run_satellite, "radar": run_radar,
        "anomaly": run_anomaly, "check": run_check}
MAX_AGE_H = {"forecast": 3, "reanalysis": 20, "satellite": 20, "radar": 20,
             "anomaly": 0, "check": 6}


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
