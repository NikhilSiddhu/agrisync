"""Device health assessment, shared by the API endpoint and the watchdog.

One implementation so the number you see in a browser and the number that
triggers an alert can never disagree.
"""
import os
from datetime import datetime, timedelta, timezone

# Sensors the build is supposed to have. Anything here that stops appearing
# is a fault, not a config change.
CORE_SENSORS = ("bme280", "mlx90614", "ads1115")

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"   # reporting, but something is wrong
STATUS_STALE = "stale"         # has not reported recently
STATUS_MISSING = "missing"     # never reported at all

SEVERITY = {STATUS_OK: 0, STATUS_DEGRADED: 1, STATUS_STALE: 2, STATUS_MISSING: 3}


def stale_minutes():
    return int(os.getenv("ALERT_STALE_MINUTES", "30"))


def assess_device(db, device_id, window_hours=6):
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    latest = db.readings.find_one({"device_id": device_id}, sort=[("ts", -1)])
    if latest is None:
        return {
            "device_id": device_id,
            "status": STATUS_MISSING,
            "problems": ["no readings have ever arrived from this device"],
        }

    recent = list(db.readings.find({"device_id": device_id, "ts": {"$gte": since}}))

    last_ts = latest["ts"]
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)
    age_min = (now - last_ts).total_seconds() / 60.0

    problems = []
    status = STATUS_OK

    if age_min > stale_minutes():
        status = STATUS_STALE
        problems.append(f"no reading for {age_min:.0f} minutes")

    # Which sensors did the board report finding on its last cycle
    sensors = latest.get("sensors") or {}
    missing = [s for s in CORE_SENSORS if sensors.get(s) is False]
    if missing:
        problems.append("sensor not detected on the bus: " + ", ".join(missing))
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    # Suspect data rate over the window
    flagged = [r for r in recent if r.get("quality_flags")]
    flag_rate = (len(flagged) / len(recent)) if recent else 0.0
    if flag_rate > 0.2:
        problems.append(
            f"{flag_rate * 100:.0f}% of the last {len(recent)} readings failed "
            "plausibility checks"
        )
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    # leaf_wetness silently vanishing is the failure mode that costs you the
    # most, because the data keeps flowing and looks healthy.
    if "leaf_wetness" not in latest:
        problems.append("leaf_wetness absent - MLX90614 or humidity reading is down")
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    battery = latest.get("battery_v")
    if battery is not None and 0 < battery < 11.8:
        problems.append(f"battery at {battery:.1f} V - below the safe SLA floor")
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    rssi = latest.get("rssi")
    if rssi is not None and rssi < -85:
        problems.append(f"WiFi signal {rssi} dBm - marginal, expect dropouts")
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    backfilled = sum(1 for r in recent if r.get("backfilled"))

    return {
        "device_id": device_id,
        "status": status,
        "problems": problems,
        "last_reading_at": last_ts.isoformat(),
        "minutes_since_last_reading": round(age_min, 1),
        "readings_last_%dh" % window_hours: len(recent),
        "backfilled_last_%dh" % window_hours: backfilled,
        "suspect_rate": round(flag_rate, 3),
        "sensors": sensors,
        "battery_v": battery,
        "rssi": rssi,
        "firmware": latest.get("firmware"),
    }


def assess_all(db, window_hours=6):
    device_ids = db.readings.distinct("device_id")
    return [assess_device(db, d, window_hours) for d in sorted(device_ids)]
