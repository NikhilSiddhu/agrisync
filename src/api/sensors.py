"""Sensor reading ingest and retrieval.

POST is what the ESP32 calls every cycle. GET is what you call to prove
the pipeline works.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify, request

from src.auth import require_read_key
from src.hmac_auth import require_device_auth

from src.db import get_db
from src.ml.leaf_temp import leaf_temp_from_reading
from src.ml.wetness import dew_point_c, estimated_leaf_temp_c, leaf_wetness_index
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
    "rain_analog",
    "rain_tips",
    "soil_moisture_pct",
    "soil_temp_c",
    "voc_index",
    "battery_v",
    "rssi",
    "buffered_count",
)

# How far in the past a device-supplied timestamp may be and still be trusted.
# Generous, because the whole point of the buffer is to survive a long outage.
MAX_BACKFILL_DAYS = 14


@bp.post("/readings")
@require_device_auth
def post_reading():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "body must be a JSON object"}), 400

    device_id = payload.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id is required"}), 400

    now = datetime.now(timezone.utc)
    ts, ts_source = _resolve_timestamp(payload.get("sampled_at"), now)

    doc = {
        "device_id": str(device_id),
        "ts": ts,
        "ts_source": ts_source,
        "received_at": now,
    }

    # A buffered reading arriving late is not a live reading. Mark it so you
    # can tell a real-time series from a backfilled one when analysing gaps.
    if (now - ts) > timedelta(minutes=5):
        doc["backfilled"] = True
        doc["delay_seconds"] = int((now - ts).total_seconds())

    for key in NUMERIC_FIELDS:
        if key in payload and payload[key] is not None:
            try:
                doc[key] = float(payload[key])
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} must be a number"}), 400

    # Which sensors the board actually found on the bus this cycle. This is
    # what makes "the MLX90614 fell off in October" visible instead of just
    # leaf_wetness quietly disappearing from the data.
    sensors = payload.get("sensors")
    if isinstance(sensors, dict):
        doc["sensors"] = {str(k): bool(v) for k, v in sensors.items()}

    if payload.get("firmware"):
        doc["firmware"] = str(payload["firmware"])[:32]

    # Recompute derived values server side. The firmware sends them too, but
    # the server value is authoritative: if you improve the model you do not
    # want to reflash every board to fix historical logic.
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

    flags = quality_check(doc)
    if flags:
        doc["quality_flags"] = flags
        current_app.logger.warning("suspect reading from %s: %s", device_id, flags)

    result = get_db().readings.insert_one(doc)
    return jsonify({
        "status": "stored",
        "id": str(result.inserted_id),
        "ts": ts.isoformat(),
        "ts_source": ts_source,
        "quality_flags": flags,
    }), 201


def _resolve_timestamp(sampled_at, now):
    """Trust the device's clock only when it looks sane.

    The board syncs to NTP over WiFi and keeps ticking while offline, so a
    buffered reading knows when it was actually taken. But a board that has
    never reached NTP reports something near 1970, and one with a corrupt
    clock can report the future. Both fall back to server time.
    """
    if sampled_at is None:
        return now, "server"

    try:
        stamp = datetime.fromtimestamp(float(sampled_at), tz=timezone.utc)
    except (TypeError, ValueError, OSError, OverflowError):
        return now, "server"

    if stamp > now + timedelta(minutes=5):
        return now, "server_device_clock_ahead"
    if stamp < now - timedelta(days=MAX_BACKFILL_DAYS):
        return now, "server_device_clock_behind"

    return stamp, "device"


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
