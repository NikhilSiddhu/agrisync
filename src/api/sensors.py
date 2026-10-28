"""Sensor reading ingest and retrieval.

POST is what the ESP32 calls every cycle. GET is what you call to prove
the pipeline works.

Readings arrive two ways: live, a few seconds after they were taken, and from
the board's flash queue after an outage, hours or days late. Everything here
is about putting the late ones at the right time and storing each reading
exactly once.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

from src.auth import require_read_key
from src.hmac_auth import require_device_auth

from src.db import get_db
from src.ml.leaf_temp import leaf_temp_from_reading
from src.ml.wetness import (dew_point_c, estimated_leaf_temp_c, leaf_wetness_index,
                            measured_wetness_pct, sensor_calibration)
from src.validation import check as quality_check

bp = Blueprint("sensors", __name__, url_prefix="/api/v1/sensors")

NUMERIC_FIELDS = (
    "air_temp_c",
    "humidity_pct",
    "pressure_hpa",
    "leaf_temp_c",
    "leaf_temp_raw_c",
    "mlx_ambient_c",
    "dew_point_c",
    "leaf_wetness",
    "leaf_wet_mv",
    "rain_analog",
    "rain_tips",
    "soil_moisture_pct",
    "soil_temp_c",
    "voc_index",
    "battery_v",
    "rssi",
    "buffered_count",
)

# Whole numbers from firmware 1.5.0 on. boot counts power-ups and up_s is
# seconds since this one, so together they name a reading exactly and let a
# reading taken before the clock was set be placed in time later.
INTEGER_FIELDS = ("boot", "up_s", "legacy")

# How far in the past a device-supplied timestamp may be and still be trusted.
# The 1.5.0 queue holds about two weeks of five-minute readings, so this
# covers it with room to spare. Older than this and the clock is suspect.
MAX_BACKFILL_DAYS = 30

# Where a reading's time came from. Only the first two are trusted enough to
# place other readings against.
TS_DEVICE = "device"            # the board's clock, set from NTP
TS_SERVER = "server"            # live reading, stamped on arrival
TS_UPTIME = "device_uptime"     # queued before the clock was set, placed by up_s
TS_QUEUED = "server_queued"     # queued, and nothing to place it by: time unknown


@bp.post("/readings")
@require_device_auth
def post_reading():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    db = get_db()
    doc, error = build_reading(payload, datetime.now(timezone.utc), db=db)
    if error:
        return jsonify({"error": error}), 400

    # The board re-sends anything it did not see a reply for. Answer 200 so it
    # drops the copy from its queue, and store nothing.
    existing = find_duplicate(db, doc)
    if existing is not None:
        return jsonify({"status": "duplicate", "id": str(existing["_id"]),
                        "ts": doc["ts"].isoformat(), "ts_source": doc["ts_source"]}), 200

    flags = doc.get("quality_flags", [])
    if flags:
        current_app.logger.warning("suspect reading from %s: %s", doc["device_id"], flags)

    result = db.readings.insert_one(doc)
    return jsonify({
        "status": "stored",
        "id": str(result.inserted_id),
        "ts": doc["ts"].isoformat(),
        "ts_source": doc["ts_source"],
        "quality_flags": flags,
    }), 201


def build_reading(payload, now, db=None, max_backfill_days=MAX_BACKFILL_DAYS,
                  sampled_at=None, ts_source=None):
    """One board payload in, the stored document out: (doc, None) or (None, error).

    Shared by the API and scripts/recover_buffer.py, so a reading recovered
    from a flash dump gets exactly the same derived values as a live one.
    sampled_at and ts_source let the recovery script supply a time it worked
    out itself."""
    device_id = payload.get("device_id")
    if not device_id:
        return None, "device_id is required"

    doc = {"device_id": str(device_id), "received_at": now}

    for key in NUMERIC_FIELDS:
        if key in payload and payload[key] is not None:
            try:
                doc[key] = float(payload[key])
            except (TypeError, ValueError):
                return None, f"{key} must be a number"

    for key in INTEGER_FIELDS:
        if key in payload and payload[key] is not None:
            try:
                doc[key] = int(payload[key])
            except (TypeError, ValueError):
                return None, f"{key} must be a whole number"

    if sampled_at is None:
        ts, source = _resolve_timestamp(payload.get("sampled_at"), now, max_backfill_days)
    else:
        ts, source = sampled_at, ts_source or TS_DEVICE

    # A reading that sat in the board's queue was not taken now. If its own
    # clock could not say when, place it against a reading from the same
    # power-up whose time is known.
    queued = bool(payload.get("queued"))
    if queued:
        doc["queued"] = True
    if queued and source not in (TS_DEVICE, "recovered"):
        placed = _place_by_uptime(db, doc, now, max_backfill_days)
        if placed is not None:
            ts, source = placed, TS_UPTIME
        else:
            source = TS_QUEUED

    doc["ts"] = ts
    doc["ts_source"] = source

    # A buffered reading arriving late is not a live reading. Mark it so you
    # can tell a real-time series from a backfilled one when analysing gaps.
    if (now - ts) > timedelta(minutes=5):
        doc["backfilled"] = True
        doc["delay_seconds"] = int((now - ts).total_seconds())

    # Which sensors the board actually found on the bus this cycle. This is
    # what makes "the MLX90614 fell off in October" visible instead of just
    # leaf_wetness quietly disappearing from the data.
    sensors = payload.get("sensors")
    if isinstance(sensors, dict):
        doc["sensors"] = {str(k): bool(v) for k, v in sensors.items()}

    if payload.get("firmware"):
        doc["firmware"] = str(payload["firmware"])[:32]
    if payload.get("reset"):
        doc["reset_reason"] = str(payload["reset"])[:24]

    _derive(doc)

    flags = quality_check(doc)
    if source == TS_QUEUED:
        # Stored, so nothing is lost, but kept out of every model until
        # something can say when it was taken.
        flags.append("ts:unknown")
    if flags:
        doc["quality_flags"] = flags
    return doc, None


def _derive(doc):
    """Recompute derived values server side. The firmware sends them too, but
    the server value is authoritative: if you improve the model you do not
    want to reflash every board to fix historical logic."""
    air = doc.get("air_temp_c")
    rh = doc.get("humidity_pct")

    # Prefer the board's raw reading and correct it here. The correction needs
    # the sky temperature, which the board cannot estimate well, and doing it
    # server side means the whole historical dataset can be recomputed if the
    # model improves - without reflashing a box on a pole.
    raw_leaf = doc.get("leaf_temp_raw_c", doc.get("leaf_temp_c"))
    if raw_leaf is not None:
        leaf, method = leaf_temp_from_reading(
            raw_leaf, air, rh, doc.get("mlx_ambient_c")
        )
        doc["leaf_temp_raw_c"] = raw_leaf
        doc["leaf_temp_c"] = round(leaf, 3)
        doc["leaf_temp_method"] = method

    leaf = doc.get("leaf_temp_c")

    if air is not None and rh is not None:
        doc["dew_point_c"] = round(dew_point_c(air, rh), 3)
        if leaf is not None:
            doc["leaf_wetness"] = round(leaf_wetness_index(leaf, doc["dew_point_c"]), 1)
            doc["leaf_wetness_source"] = "proxy"
        else:
            est = estimated_leaf_temp_c(air)
            doc["leaf_temp_c"] = round(est, 3)
            doc["leaf_temp_method"] = "air_proxy"
            doc["leaf_wetness"] = round(leaf_wetness_index(est, doc["dew_point_c"]), 1)
            doc["leaf_wetness_source"] = "air_proxy"
            doc["leaf_wetness_estimated"] = True

    # The measured signal, once the sensor is fitted and calibrated. Kept in
    # its own fields: leaf_wetness stays the proxy so its history keeps one
    # meaning, and the two can be compared night by night.
    mv = doc.get("leaf_wet_mv")
    cal = sensor_calibration()
    if mv is not None and cal is not None:
        dry, wet, threshold = cal
        pct = measured_wetness_pct(mv, dry, wet)
        doc["leaf_wet_pct"] = round(pct, 1)
        doc["leaf_wet_sensor"] = pct >= threshold


def find_duplicate(db, doc):
    """The stored copy of this exact reading, if the board already sent it.

    A reading is named by the board's own clock, or failing that by its
    power-up and uptime. Readings stamped by the server on arrival carry
    neither, so they cannot be matched and are always stored."""
    if doc["ts_source"] in (TS_DEVICE, "recovered"):
        return db.readings.find_one({"device_id": doc["device_id"], "ts": doc["ts"]}, {"_id": 1})
    if "boot" in doc and "up_s" in doc:
        return db.readings.find_one({"device_id": doc["device_id"], "boot": doc["boot"],
                                     "up_s": doc["up_s"]}, {"_id": 1})
    return None


def _place_by_uptime(db, doc, now, max_backfill_days):
    """When a queued reading was taken, from a reading of the same power-up
    whose time is known: that reading's time plus the difference in uptime.

    The board's own clock is preferred as the reference. A live reading
    stamped by the server on arrival is nearly as good, a few seconds late."""
    if db is None or "boot" not in doc or "up_s" not in doc:
        return None
    for source in (TS_DEVICE, TS_SERVER):
        ref = db.readings.find_one(
            {"device_id": doc["device_id"], "boot": doc["boot"], "ts_source": source,
             "up_s": {"$exists": True}},
            sort=[("received_at", -1)])
        if ref is None:
            continue
        ref_ts = ref["ts"] if ref["ts"].tzinfo else ref["ts"].replace(tzinfo=timezone.utc)
        ts = ref_ts + timedelta(seconds=doc["up_s"] - ref["up_s"])
        if now - timedelta(days=max_backfill_days) <= ts <= now + timedelta(minutes=5):
            return ts
    return None


def _resolve_timestamp(sampled_at, now, max_backfill_days=MAX_BACKFILL_DAYS):
    """Trust the device's clock only when it looks sane.

    The board sets its clock from NTP (over WiFi, or through the modem on
    cellular from 1.5.0) and keeps ticking while offline, so a queued reading
    knows when it was actually taken. But a board that has never reached NTP
    reports something near 1970, and one with a corrupt clock can report the
    future. Both fall back to server time.
    """
    if sampled_at is None:
        return now, TS_SERVER

    try:
        stamp = datetime.fromtimestamp(float(sampled_at), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return now, TS_SERVER

    if stamp > now + timedelta(minutes=5):
        return now, "server_device_clock_ahead"
    if stamp < now - timedelta(days=max_backfill_days):
        return now, "server_device_clock_behind"

    return stamp, TS_DEVICE


@bp.get("/readings")
@require_read_key
def get_readings():
    """Most recent readings. ?device_id= ?limit= ?clean=1 all optional.

    Read only and intentionally unauthenticated. This is the command you run
    to check the box is alive.
    """
    query = {}
    device_id = request.args.get("device_id")
    if device_id:
        query["device_id"] = device_id

    if request.args.get("clean") in ("1", "true", "yes"):
        query["quality_flags"] = {"$exists": False}

    try:
        limit = min(int(request.args.get("limit", 20)), 500)
    except ValueError:
        limit = 20

    cursor = get_db().readings.find(query).sort("ts", -1).limit(limit)

    rows = []
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        for key in ("ts", "received_at"):
            if key in doc and hasattr(doc[key], "isoformat"):
                doc[key] = doc[key].isoformat()
        rows.append(doc)

    return jsonify({"count": len(rows), "readings": rows})
