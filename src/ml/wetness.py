"""Dew point and the leaf wetness proxy.

IMPORTANT: this logic is duplicated in firmware/synthetic_wifi/src/main.cpp
so the board can report a value even when the Pi is unreachable. If you
change the model here, change it there too, or your historical data will
have two different definitions of the same field.
"""
import math

# Magnus-Tetens coefficients (Sonntag 1990, over water).
MAGNUS_A = 17.62
MAGNUS_B = 243.12

# Leaf is considered fully wet at or below dew point, fully dry this many
# degrees above it. Straight from the manual.
DRY_MARGIN_C = 2.0


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


def is_wet(leaf_wetness: float, threshold: float = 50.0) -> bool:
    """Does this reading count as a wet hour? Threshold is a judgement call."""
    return float(leaf_wetness) >= threshold
