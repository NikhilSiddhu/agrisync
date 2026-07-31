"""Full-history queries for the authenticated view.

Deliberately NOT shared with dashboard/data.py. That module enforces a hard
7-day cap and this one has none. Keeping them separate means there is no code
path where the public service could accidentally inherit an uncapped query -
the cap cannot be bypassed because the public service simply does not contain
these functions.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.bucketing import bucket_minutes_for, bucketed
from src.db import get_db

WET_THRESHOLD = 50.0
EXPECTED_INTERVAL_S = 60


def _aware(dt):
    """PyMongo returns naive datetimes even though Mongo stores UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(dt, tz="Asia/Kolkata"):
    dt = _aware(dt)
    if dt is None:
        return None
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo(tz))
    except Exception:
        return dt + timedelta(hours=5, minutes=30)


def span():
    """Whole dataset: first reading, last reading, totals."""
    db = get_db()
    first = db.readings.find_one(sort=[("ts", 1)])
    last = db.readings.find_one(sort=[("ts", -1)])
    total = db.readings.estimated_document_count()

    if not first or not last:
        return {"empty": True, "total": 0}

    a, b = _aware(first["ts"]), _aware(last["ts"])
    hours = (b - a).total_seconds() / 3600.0
    expected = int(hours * 3600 / EXPECTED_INTERVAL_S) or 1

    suspect = db.readings.count_documents({"quality_flags": {"$exists": True}})
    with_wet = db.readings.count_documents({"leaf_wetness": {"$exists": True}})

    return {
        "empty": False,
        "total": total,
        "first": _local(a).strftime("%d %b %Y, %H:%M"),
        "last": _local(b).strftime("%d %b %Y, %H:%M"),
        "days": round(hours / 24.0, 1),
        "hours": round(hours, 1),
        "coverage_pct": round(100.0 * total / expected, 1),
        "suspect": suspect,
        "suspect_pct": round(100.0 * suspect / total, 2) if total else 0,
        "with_wetness": with_wet,
        "wetness_pct": round(100.0 * with_wet / total, 1) if total else 0,
    }


def series(days=None):
    """Whole dataset by default. No cap.

    Grouped inside MongoDB, so the wire carries a few hundred averaged points
    rather than a season of raw documents. See src/bucketing.py.
    """
    db = get_db()
    query = {"quality_flags": {"$exists": False}}
    if days:
        query["ts"] = {"$gte": datetime.now(timezone.utc) - timedelta(days=days)}

    first = db.readings.find_one(query, sort=[("ts", 1)])
    last = db.readings.find_one(query, sort=[("ts", -1)])
    if not first or not last:
        return {"labels": [], "air": [], "humidity": [], "dew": [], "leaf": [], "wetness": []}

    hours = (_aware(last["ts"]) - _aware(first["ts"])).total_seconds() / 3600.0
    minutes = bucket_minutes_for(hours)
    fmt = "%d %b %H:%M" if hours <= 24 * 14 else "%d %b"

    rows = bucketed(db.readings, query, minutes)
    out = {"labels": [], "air": [], "humidity": [], "dew": [], "leaf": [], "wetness": []}
    for r in rows:
        out["labels"].append(_local(r["bucket"]).strftime(fmt))
        for key in ("air", "humidity", "dew", "leaf", "wetness"):
            out[key].append(r[key])
    return out


def daily():
    """Per-day summary. The table you actually read when reviewing a season."""
    db = get_db()
    rows = list(db.readings.find(
        {}, {"ts": 1, "air_temp_c": 1, "humidity_pct": 1,
             "leaf_wetness": 1, "quality_flags": 1, "_id": 0}).sort("ts", 1))

    buckets = defaultdict(list)
    for r in rows:
        ts = _local(r.get("ts"))
        if ts:
            buckets[ts.date()].append(r)

    out = []
    for day in sorted(buckets, reverse=True):
        items = buckets[day]
        clean = [i for i in items if not i.get("quality_flags")]
        src = clean or items
        temps = [i["air_temp_c"] for i in src if i.get("air_temp_c") is not None]
        hums = [i["humidity_pct"] for i in src if i.get("humidity_pct") is not None]
        wet = [i["leaf_wetness"] for i in src if i.get("leaf_wetness") is not None]
        wet_frac = (sum(1 for w in wet if w >= WET_THRESHOLD) / len(wet)) if wet else None

        out.append({
            "date": day.strftime("%d %b %Y"),
            "readings": len(items),
            "coverage": round(100.0 * len(items) / 1440.0, 1),
            "t_min": round(min(temps), 1) if temps else None,
            "t_max": round(max(temps), 1) if temps else None,
            "rh_mean": round(sum(hums) / len(hums), 1) if hums else None,
            "wet_hours": round(wet_frac * 24, 1) if wet_frac is not None else None,
            "suspect": len(items) - len(clean),
        })
    return out


CSV_COLUMNS = [
    "ts", "device_id", "air_temp_c", "humidity_pct", "pressure_hpa", "dew_point_c",
    "leaf_temp_c", "leaf_temp_raw_c", "leaf_wetness", "rain_analog", "rain_tips",
    "battery_v", "rssi", "ts_source", "firmware", "quality_flags",
]


def stream_csv():
    """Yield the whole dataset a row at a time.

    A season is roughly 250,000 rows. Building that in memory on a Pi with
    other services running is how you get an OOM kill at the worst moment,
    so this streams and never holds more than one row.
    """
    yield ",".join(CSV_COLUMNS) + "\n"
    cursor = get_db().readings.find({}, {"_id": 0}).sort("ts", 1).batch_size(500)
    for doc in cursor:
        cells = []
        for col in CSV_COLUMNS:
            v = doc.get(col)
            if col == "ts" and hasattr(v, "isoformat"):
                v = _aware(v).isoformat()
            elif col == "quality_flags":
                v = ";".join(v or [])
            cells.append("" if v is None else str(v))
        yield ",".join(cells) + "\n"
