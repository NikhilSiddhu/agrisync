"""Device health assessment, shared by the API endpoint and the watchdog.

One implementation so the number you see in a browser and the number that
triggers an alert can never disagree.

MUTING A KNOWN-ABSENT SENSOR
    Set IGNORED_SENSORS in .env, comma separated:

        IGNORED_SENSORS=mlx90614

    That sensor stops counting as a fault, and any check that depends on it
    is skipped too. Nothing else is silenced.

    This exists because an alert you have decided to ignore is worse than no
    alert: it trains you to dismiss the whole channel, and the real one gets
    dismissed with it. Muting deliberately, in config, with a note, beats
    learning to skim past it.

    Clear the variable when the hardware is fixed and the check comes back.
"""
import os
from datetime import datetime, timedelta, timezone

# Every sensor this build can have.
ALL_SENSORS = ("bme280", "mlx90614", "ads1115")

STATUS_OK = "ok"
STATUS_DEGRADED = "degraded"   # reporting, but something is wrong
STATUS_STALE = "stale"         # has not reported recently
STATUS_MISSING = "missing"     # never reported at all

SEVERITY = {STATUS_OK: 0, STATUS_DEGRADED: 1, STATUS_STALE: 2, STATUS_MISSING: 3}


def ignored_sensors():
    """Sensors deliberately excluded from health checks, from .env."""
    raw = os.getenv("IGNORED_SENSORS", "")
    return {s.strip().lower() for s in raw.split(",") if s.strip()}


def core_sensors():
    """Sensors whose absence is a genuine fault right now."""
    skip = ignored_sensors()
    return tuple(s for s in ALL_SENSORS if s not in skip)


# Kept for anything that imported the old name.
CORE_SENSORS = ALL_SENSORS


# The rain plate reads 4095 dry and falls toward 0 as water bridges it.
RAIN_WET_BELOW = 2000
# A running count past a million is a chattering input, not rain.
RAIN_TIPS_CHATTER = 1_000_000


def _debounced(firmware):
    """1.6.0 and later debounce the rain input, so a big count is real."""
    try:
        return tuple(int(x) for x in str(firmware).split(".")) >= (1, 6, 0)
    except ValueError:
        return False


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

    skip = ignored_sensors()
    problems = []
    status = STATUS_OK

    if age_min > stale_minutes():
        status = STATUS_STALE
        problems.append(f"no reading for {age_min:.0f} minutes")

    # Which sensors did the board report finding on its last cycle
    sensors = latest.get("sensors") or {}
    missing = [s for s in core_sensors() if sensors.get(s) is False]
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
    #
    # Skipped when the MLX is deliberately ignored - it cannot be computed
    # without leaf temperature, so alerting on it would just be noise.
    if "leaf_wetness" not in latest and "mlx90614" not in skip:
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
        problems.append(f"signal {rssi} dBm - marginal, expect dropouts")
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    # Faults that leave data flowing but wrong (found in the field, Oct 2026).
    # Neither feeds the odds: rain comes from ERA5. They are here so a broken
    # part is named, not discovered in December.
    plate = [r["rain_analog"] for r in db.readings.find(
        {"device_id": device_id, "ts": {"$gte": now - timedelta(hours=48)},
         "rain_analog": {"$exists": True}}, {"rain_analog": 1, "_id": 0})
        if r.get("rain_analog") is not None]
    if len(plate) >= 100 and max(plate) < RAIN_WET_BELOW:
        problems.append("rain plate has read wet for 48 hours without a break: "
                        "corroded or shorted; it says nothing until it is cleaned or replaced")
        if status == STATUS_OK:
            status = STATUS_DEGRADED
    tips = latest.get("rain_tips")
    if tips is not None and tips > RAIN_TIPS_CHATTER and not _debounced(latest.get("firmware")):
        problems.append(f"rain counter reads {tips:,.0f}: the input chatters and the count means "
                        "nothing; firmware 1.6.0 debounces it")
        if status == STATUS_OK:
            status = STATUS_DEGRADED

    backfilled = sum(1 for r in recent if r.get("backfilled"))

    result = {
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
        # 1.6.0: the interval it runs at, its memory, and the last word on an update
        "interval_s": latest.get("interval_s"),
        "free_heap": latest.get("free_heap"),
        "ota": (db.feeds.find_one({"_id": f"ota:{device_id}"}) or {}).get("last"),
    }

    # Visible in the API and the dashboard, so a muted sensor is never a
    # silent one. You should always be able to see what is not being checked.
    if skip:
        result["ignored_sensors"] = sorted(skip)

    return result


def assess_all(db, window_hours=6):
    device_ids = db.readings.distinct("device_id")
    return [assess_device(db, d, window_hours) for d in sorted(device_ids)]
