"""Time-bucketed averages, computed in the database.

WHY THIS IS NOT DONE IN PYTHON

The obvious version pulls every reading into the process and averages them in a
loop. That is fine at ten thousand rows and miserable at a season's worth. A
Raspberry Pi also running Mongo, a tunnel and two web services should not be
asked to hold 250,000 documents in memory to draw a chart with 200 points on it.

$dateTrunc does the grouping inside MongoDB, so the wire carries a few hundred
pre-averaged points instead of a quarter of a million raw ones.

WHY THERE IS A FALLBACK

$dateTrunc needs MongoDB 5.0+. mongomock, used throughout the test suite, does
not implement it at all. Rather than leave the tests unable to exercise this
code, an OperationFailure drops through to the Python path — same output, same
shape, just slower. Production takes the fast path; the tests cover the logic.

THE PUBLIC CAP DOES NOT LIVE HERE

This module never decides a time range. Callers build the query, which is how
the dashboard's hard 7-day limit stays in the dashboard and cannot leak into
the authenticated view by way of a shared helper.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from pymongo.errors import OperationFailure

# MongoDB's $dateTrunc bins from 2000-01-01, NOT the Unix epoch. The Python
# fallback must use the same origin or the two paths disagree.
#
# They happen to agree for any bin that divides evenly into a day (60, 360,
# 1440 minutes) because 1970-01-01 to 2000-01-01 is a whole number of days.
# They do NOT agree at 4320 minutes (3 days), where the fallback came out
# exactly one day early — wrong only on multi-season ranges, which is precisely
# the kind of fault that ships unnoticed.
DATE_TRUNC_ORIGIN = datetime(2000, 1, 1, tzinfo=timezone.utc)

FIELDS = {
    "air": "air_temp_c",
    "humidity": "humidity_pct",
    "dew": "dew_point_c",
    "leaf": "leaf_temp_c",
    "wetness": "leaf_wetness",
}


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def bucketed(collection, query, bucket_minutes):
    """[{bucket: datetime, air: float|None, ...}] sorted oldest first."""
    try:
        return _via_aggregation(collection, query, bucket_minutes)
    except OperationFailure:
        return _via_python(collection, query, bucket_minutes)


def _via_aggregation(collection, query, bucket_minutes):
    group = {"_id": {"$dateTrunc": {"date": "$ts", "unit": "minute",
                                    "binSize": int(bucket_minutes)}},
             "n": {"$sum": 1}}
    for out, field in FIELDS.items():
        group[out] = {"$avg": f"${field}"}

    rows = collection.aggregate(
        [{"$match": query}, {"$group": group}, {"$sort": {"_id": 1}}],
        allowDiskUse=True,
    )

    out = []
    for r in rows:
        item = {"bucket": _aware(r["_id"]), "n": r["n"]}
        for key in FIELDS:
            v = r.get(key)
            item[key] = round(v, 2) if v is not None else None
        out.append(item)
    return out


def _via_python(collection, query, bucket_minutes):
    """Only reached under mongomock, or a MongoDB older than 5.0."""
    projection = {"_id": 0, "ts": 1}
    projection.update({f: 1 for f in FIELDS.values()})
    docs = list(collection.find(query, projection).sort("ts", 1))

    step = timedelta(minutes=bucket_minutes).total_seconds()
    buckets = defaultdict(list)
    for d in docs:
        ts = _aware(d.get("ts"))
        if ts is None:
            continue
        # Floor to the same grid $dateTrunc uses, anchored at 2000-01-01.
        offset = (ts - DATE_TRUNC_ORIGIN).total_seconds()
        buckets[int(offset // step)].append(d)

    out = []
    for index in sorted(buckets):
        items = buckets[index]
        item = {"bucket": DATE_TRUNC_ORIGIN + timedelta(seconds=index * step),
                "n": len(items)}
        for key, field in FIELDS.items():
            vals = [i[field] for i in items if i.get(field) is not None]
            item[key] = round(sum(vals) / len(vals), 2) if vals else None
        out.append(item)
    return out


def bucket_minutes_for(hours):
    """Keep a chart under roughly 300 points however long the window."""
    if hours <= 48:
        return 60
    if hours <= 24 * 14:
        return 360        # 6 hours
    if hours <= 24 * 90:
        return 1440       # daily
    return 4320           # 3 days, for a multi-season record
