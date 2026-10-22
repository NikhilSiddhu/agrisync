"""Plausibility checks on incoming readings.

Design decision worth understanding: suspect values are FLAGGED AND STORED,
not rejected. Three reasons.

1. A rejected reading is gone forever. A flagged one is evidence of when the
   sensor started failing, which you will want in December.
2. The board buffers failed POSTs and retries. Rejecting with a 400 would put
   it in a loop retrying a reading that can never succeed.
3. You cannot tell a broken sensor from genuinely extreme weather at ingest
   time. Flag it, look at it later.

The risk engine skips flagged fields, so bad data never silently reaches a
disease number.
"""

# (minimum, maximum) physically plausible values for the Hapur site.
RANGES = {
    "air_temp_c": (-10.0, 55.0),
    "humidity_pct": (0.0, 100.0),
    "pressure_hpa": (850.0, 1080.0),
    "leaf_temp_c": (-15.0, 70.0),      # sunlit leaves run hotter than air
    "dew_point_c": (-25.0, 45.0),
    "leaf_wetness": (0.0, 100.0),
    "rain_analog": (0.0, 4095.0),      # 12-bit ADC
    # "rain_tips" not range-checked: it is a running counter, not a measurement (10 Oct 2026)
    "soil_moisture_pct": (0.0, 100.0),
    "soil_temp_c": (-15.0, 60.0),
    "voc_index": (0.0, 500.0),
    "battery_v": (0.0, 20.0),
    "rssi": (-120.0, 0.0),
}

# Leaf temperature can differ from air temperature, but not by this much.
# A larger gap means the MLX90614 is pointed at the sky, at the ground, or
# has failed.
MAX_LEAF_AIR_GAP_C = 25.0


def check(doc: dict) -> list:
    """Return a list of quality flag strings. Empty list means the row is clean."""
    flags = []

    for field, (low, high) in RANGES.items():
        if field not in doc:
            continue
        value = doc[field]
        if value != value:  # NaN
            flags.append(f"{field}:nan")
        elif value < low or value > high:
            flags.append(f"{field}:out_of_range")

    air = doc.get("air_temp_c")
    rh = doc.get("humidity_pct")
    leaf = doc.get("leaf_temp_c")
    dew = doc.get("dew_point_c")

    # Dew point above air temperature is physically impossible. Usually means
    # the humidity reading is corrupt.
    if air is not None and dew is not None and dew > air + 0.5:
        flags.append("dew_point:above_air_temp")

    # A humidity sensor stuck at exactly 0 or exactly 100 is almost always
    # failed rather than real. 100 does happen in fog, so this is a flag and
    # not a rejection.
    if rh is not None and rh <= 0.0:
        flags.append("humidity_pct:stuck_low")

    if air is not None and leaf is not None and abs(leaf - air) > MAX_LEAF_AIR_GAP_C:
        flags.append("leaf_temp_c:implausible_gap")

    # All three core sensors reading exactly zero is the classic signature of
    # a dead I2C bus rather than a genuinely freezing, airless field.
    if air == 0.0 and rh == 0.0 and doc.get("pressure_hpa") == 0.0:
        flags.append("i2c:all_zero")

    return flags


def fields_flagged(flags) -> set:
    """Which field names are implicated, so the risk engine can skip them."""
    return {f.split(":", 1)[0] for f in flags}
