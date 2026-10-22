"""Unusual conditions, judged against the field's own recent behaviour.

WHAT IT DOES
    For each hour of the day, the median and spread (MAD) of the last 14 days
    of station readings. A recent hour is flagged when it sits far outside
    that band: a robust z-score beyond Z_LIMIT AND a gap large enough to
    matter in the field, so a quiet week with a tiny spread cannot turn a
    0.4 degree wobble into an alarm.

WHAT IT IS NOT
    Disease detection. It knows nothing about plants. It says "this hour is
    not like the last two weeks at this time of day", which is worth a look
    because the infection models only see the thresholds they were given.
    The learned baseline the project is aiming for needs labelled outcomes;
    this is the statistical floor underneath it.
"""
from collections import defaultdict
from statistics import median

Z_LIMIT = 3.5
MIN_GAP = {"air_temp_c": 3.0, "humidity_pct": 15.0, "dew_point_c": 3.0}
LABELS = {"air_temp_c": "Air temperature", "humidity_pct": "Humidity",
          "dew_point_c": "Dew point"}
UNITS = {"air_temp_c": "°C", "humidity_pct": "%", "dew_point_c": "°C"}
MIN_BASELINE_DAYS = 5


def _mad(values, mid):
    return median(abs(v - mid) for v in values)


def baseline(history):
    """history: [(local_hour_0_23, {var: value})]. Returns {var: {hour: (median, mad, n)}}."""
    buckets = defaultdict(lambda: defaultdict(list))
    for hour, values in history:
        for var, v in values.items():
            if v is not None:
                buckets[var][hour].append(float(v))
    out = {}
    for var, by_hour in buckets.items():
        out[var] = {}
        for hour, vals in by_hour.items():
            if len(vals) >= MIN_BASELINE_DAYS:
                mid = median(vals)
                out[var][hour] = (mid, _mad(vals, mid), len(vals))
    return out


def check(recent, base):
    """recent: [(label, local_hour, {var: value})]. Returns flags, newest first."""
    flags = []
    for label, hour, values in recent:
        for var, v in values.items():
            ref = base.get(var, {}).get(hour)
            if v is None or ref is None:
                continue
            mid, mad, n = ref
            spread = 1.4826 * mad
            gap = float(v) - mid
            z = gap / spread if spread > 1e-9 else (0.0 if abs(gap) < 1e-9 else float("inf"))
            if abs(z) >= Z_LIMIT and abs(gap) >= MIN_GAP.get(var, 0.0):
                flags.append({
                    "when": label, "variable": var, "label": LABELS.get(var, var),
                    "value": round(float(v), 1), "typical": round(mid, 1),
                    "unit": UNITS.get(var, ""),
                    "direction": "higher" if gap > 0 else "lower",
                    "z": round(z, 1) if z != float("inf") else None,
                })
    return flags
