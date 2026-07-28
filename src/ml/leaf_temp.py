"""Emissivity correction for the MLX90614, done properly.

WHY THIS LIVES ON THE SERVER AND NOT ON THE BOARD

The MLX90614 reports a temperature computed as if the leaf were a perfect
blackbody:

    T_meas^4 = e * T_leaf^4 + (1 - e) * T_reflected^4

Rearranged, recovering the true leaf temperature needs THREE things: the
measured value, the emissivity, and the temperature of whatever the leaf is
reflecting. The first two are easy. The third is the whole problem.

The obvious choice, the sensor's own ambient reading, is almost useless. It
sits within a couple of degrees of the target, so the correction comes out
around 0.1 C. Not worth the code.

What the leaf actually reflects is the sky. And on a clear night the sky's
effective radiating temperature is not the air temperature, it is somewhere
between -10 and -40 C. Run that through the equation and the correction is
over a degree, which on a 2 C wetness scale moves the wetness index by 50
points or more.

Clear calm nights are exactly when dew forms. So the error is not spread
evenly across the season, it is concentrated in the hours that drive every
disease number. Uncorrected, the system systematically overstates wetness on
precisely the nights it is trying to detect.

The board therefore stores the RAW reading and the ambient temperature and
lets the Pi do this. That way the model can be improved, or redone across the
whole historical dataset, without reflashing a box on a pole in a field.
"""
import math

LEAF_EMISSIVITY = 0.96  # cabbage leaves sit around 0.94-0.98


def saturation_vapour_pressure_hpa(temp_c: float) -> float:
    """Tetens equation."""
    return 6.1078 * math.exp((17.27 * temp_c) / (temp_c + 237.3))


def sky_temperature_c(air_temp_c: float, humidity_pct: float,
                      cloud_fraction: float = 0.0) -> float:
    """Effective radiating temperature of the sky.

    Uses Brutsaert's clear-sky emissivity, then blends toward air temperature
    as cloud cover increases, because clouds radiate roughly like the air.

    We have no cloud sensor, so cloud_fraction defaults to clear. That is the
    conservative choice for this purpose: it produces the largest correction,
    on the nights when it matters.
    """
    t_k = air_temp_c + 273.15
    vapour_hpa = saturation_vapour_pressure_hpa(air_temp_c) * (humidity_pct / 100.0)
    vapour_hpa = max(vapour_hpa, 0.05)

    clear_emissivity = 1.24 * (vapour_hpa / t_k) ** (1.0 / 7.0)
    clear_emissivity = min(max(clear_emissivity, 0.5), 1.0)

    cloud = min(max(cloud_fraction, 0.0), 1.0)
    effective = clear_emissivity + (1.0 - clear_emissivity) * cloud

    return t_k * (effective ** 0.25) - 273.15


def correct_leaf_temp(measured_c: float, reflected_c: float,
                      emissivity: float = LEAF_EMISSIVITY) -> float:
    """Recover true leaf temperature from the sensor's blackbody assumption."""
    if not (0.5 < emissivity < 0.999):
        return measured_c

    k = 273.15
    t_meas = measured_c + k
    t_refl = reflected_c + k

    numerator = t_meas ** 4 - (1.0 - emissivity) * t_refl ** 4
    if numerator <= 0:
        return measured_c

    return (numerator / emissivity) ** 0.25 - k


def leaf_temp_from_reading(raw_leaf_c, air_temp_c, humidity_pct,
                           mlx_ambient_c=None, cloud_fraction=0.0):
    """Best available leaf temperature, plus what was assumed to get there.

    Returns (leaf_temp_c, method).
    """
    if raw_leaf_c is None:
        return None, "unavailable"

    if air_temp_c is not None and humidity_pct is not None:
        reflected = sky_temperature_c(air_temp_c, humidity_pct, cloud_fraction)
        return correct_leaf_temp(raw_leaf_c, reflected), "sky_model"

    if mlx_ambient_c is not None:
        # Weak fallback. Better than nothing, worth about 0.1 C.
        return correct_leaf_temp(raw_leaf_c, mlx_ambient_c), "sensor_ambient"

    return raw_leaf_c, "uncorrected"
