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
