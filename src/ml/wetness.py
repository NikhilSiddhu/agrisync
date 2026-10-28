"""Dew point and the leaf wetness proxy.

Goes at:  ~/agrisync/src/ml/wetness.py

IMPORTANT: dew_point_c and leaf_wetness_index are duplicated in
firmware/synthetic_wifi/src/main.cpp so the board can report a value even when
the Pi is unreachable. If you change either, change it there too, or your
historical data will have two different definitions of the same field.

estimated_leaf_temp_c is server-only and deliberately not in the firmware. It
is a fallback for missing hardware, not part of the model, and it should be
easy to delete when the MLX90614 is fitted.
"""
import math
import os

# Magnus-Tetens coefficients (Sonntag 1990, over water).
MAGNUS_A = 17.62
MAGNUS_B = 243.12

# Leaf is considered fully wet at or below dew point, fully dry this many
# degrees above it. Straight from the manual.
DRY_MARGIN_C = 2.0

# How far below air temperature to assume the leaf sits when there is no
# MLX90614 to measure it. See estimated_leaf_temp_c for why this is not zero.
DEFAULT_LEAF_OFFSET_C = 1.0


def dew_point_c(air_temp_c: float, humidity_pct: float) -> float:
    """Dew point in Celsius from air temperature and relative humidity."""
    rh = max(min(float(humidity_pct), 100.0), 0.1)  # ln(0) guard
    t = float(air_temp_c)
    gamma = math.log(rh / 100.0) + (MAGNUS_A * t) / (MAGNUS_B + t)
    return (MAGNUS_B * gamma) / (MAGNUS_A - gamma)


def leaf_wetness_index(leaf_temp_c: float, dew_pt_c: float) -> float:
    """0 to 100 wetness proxy.

    100 when the leaf surface is at or below the dew point (condensation
    forming), falling linearly to 0 once the leaf is DRY_MARGIN_C above it.

    This is a PROXY, not a measurement. It is the honest weak point of the
    build. A METER PHYTOS-31 on the ADS1115 A0 input replaces it with a real
    measured signal, and until you fit one, every risk number downstream
    inherits this assumption.
    """
    delta = float(leaf_temp_c) - float(dew_pt_c)
    if delta <= 0:
        return 100.0
    if delta >= DRY_MARGIN_C:
        return 0.0
    return 100.0 * (1.0 - delta / DRY_MARGIN_C)


def leaf_offset_c() -> float:
    """Tunable without a redeploy. LEAF_TEMP_OFFSET_C in .env."""
    try:
        return float(os.getenv("LEAF_TEMP_OFFSET_C", DEFAULT_LEAF_OFFSET_C))
    except ValueError:
        return DEFAULT_LEAF_OFFSET_C


def estimated_leaf_temp_c(air_temp_c: float) -> float:
    """Stand-in for leaf temperature when no MLX90614 is present.

    WHY NOT JUST USE AIR TEMPERATURE

    After dark, a leaf radiates heat to a cold sky and settles BELOW the
    surrounding air - typically one to three degrees under clear conditions.
    That is precisely the window in which dew forms and infection happens.

    Using air temperature unadjusted would therefore report the canopy as
    drier than it is, and UNDER-predict wet hours. For a disease-risk model
    that is the dangerous direction to be wrong in: it tells a grower there is
    no risk on exactly the nights there is. So a small offset is subtracted.

    WHAT THIS IS NOT

    It is not a measurement, and it is not good. A fixed offset cannot know
    whether the sky is clear or overcast, and it is simply wrong during the
    day when a sunlit leaf runs WARMER than the air. Everything computed from
    it is marked estimated so it is never mistaken for measured data, and so
    it can be recomputed or thrown away once real leaf temperature arrives.

    Delete this the day an MLX90614 or a PHYTOS-31 is fitted.
    """
    return float(air_temp_c) - leaf_offset_c()


def is_wet(leaf_wetness: float, threshold: float = 50.0) -> bool:
    """Does this reading count as a wet hour? Threshold is a judgement call."""
    return float(leaf_wetness) >= threshold


# ---------------------------------------------------------------------------
# A real leaf wetness sensor on the ADS1115 A0 input (firmware 1.5.0,
# -DLEAF_WET_SENSOR=1). The board sends raw millivolts as leaf_wet_mv.
#
# Raw millivolts mean nothing until you tell the server what dry and wet look
# like for YOUR sensor, so nothing downstream uses them until both numbers
# are in .env:
#
#   LW_DRY_MV             the reading with the sensor clean and dry, in air
#   LW_WET_MV             the reading with the sensor misted until dripping
#   LW_WET_THRESHOLD_PCT  how far along that range counts as wet (default 20)
#
# It works either way round: some sensors rise as they wet, resistive grids
# usually fall. Dew covers only part of the surface, so the wet threshold is
# well below 50. Check it on a dew morning against your own eyes.
# ---------------------------------------------------------------------------

DEFAULT_WET_THRESHOLD_PCT = 20.0


def sensor_calibration():
    """(dry_mv, wet_mv, threshold_pct) from .env, or None until both are set."""
    try:
        dry = float(os.environ["LW_DRY_MV"])
        wet = float(os.environ["LW_WET_MV"])
    except (KeyError, ValueError):
        return None
    if abs(wet - dry) < 1.0:
        return None
    try:
        threshold = float(os.getenv("LW_WET_THRESHOLD_PCT", DEFAULT_WET_THRESHOLD_PCT))
    except ValueError:
        threshold = DEFAULT_WET_THRESHOLD_PCT
    return dry, wet, threshold


def measured_wetness_pct(mv: float, dry_mv: float, wet_mv: float) -> float:
    """0 to 100: where a reading sits between the sensor's dry and wet ends."""
    pct = 100.0 * (float(mv) - dry_mv) / (wet_mv - dry_mv)
    return max(0.0, min(100.0, pct))


# An hour counts as wet when at least half of its leaf_wet_sensor readings
# are wet. Readings without the sensor value do not vote.
SENSOR_HOUR_WET_SHARE = 0.5


def sensor_hour_wet(n_wet: int, n: int):
    """1.0 (wet) or 0.0 (dry) for one hour of sensor readings, or None when
    the hour has none. n_wet of n readings carried leaf_wet_sensor True."""
    if not n:
        return None
    return 1.0 if n_wet / n >= SENSOR_HOUR_WET_SHARE else 0.0
