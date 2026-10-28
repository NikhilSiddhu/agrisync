"""Leaf wetness from the leaf's energy balance: dew, rain and drying, by hour.

WHY NOT JUST THE DEW-POINT RULE
    src/ml/wetness.py calls a leaf wet whenever the air is within about 2 C of
    its dew point. That rule cannot tell two nights apart that a leaf can: a
    clear, still night at 95% humidity (the leaf radiates heat to a cold sky,
    drops below the dew point and gets wet) and an overcast, breezy one at the
    same humidity (the clouds send heat back, the wind keeps the leaf at air
    temperature, and it stays dry). Dew is decided by radiation and wind as
    much as by humidity, and both come with every forecast.

WHAT THIS DOES
    Each hour, the Penman-Monteith equation for a wet surface gives the rate
    at which water condenses on the leaf (negative) or evaporates from it
    (positive), from:
        air temperature and humidity   the station, or a corrected forecast
        cloud cover                    sets how cold the sky looks to the leaf
        wind                           sets how fast air reaches the leaf
        sunshine                       dries the leaf after sunrise
        rain                           wets it directly
    Water on the leaf is kept as a small store in millimetres: dew and rain
    fill it, evaporation empties it, and the leaf counts as wet while there is
    any. This is the approach of Sentelhas et al. (2006), Agricultural and
    Forest Meteorology 141:105-117 (mean error about 1 hour of wetness a day
    against sensors in five crops), with the sky treated as in Brutsaert
    (1975) for clear skies and Unsworth & Monteith (1975) for cloud.

WHAT IS ASSUMED
    A cabbage leaf about 20 cm across that sees most of the sky, wind at leaf
    height 40% of the 10 m value, and stores of 0.4 mm for dew and 0.6 mm for
    rain. Each of these is uncertain, so the odds engine runs the model many
    times with each one varied (LeafParams.sample).

    It is still a model of wetness, not a measurement. The leaf wetness
    sensor planned for the box is what will check it, hour by hour.
"""
from dataclasses import dataclass, replace

import numpy as np

SIGMA = 5.670e-8        # Stefan-Boltzmann, W m-2 K-4
LV = 2.45e6             # latent heat of vaporisation, J kg-1
CP = 1010.0             # specific heat of air, J kg-1 K-1
RD = 287.05             # gas constant for dry air, J kg-1 K-1


def es_kpa(t_c):
    """Saturation vapour pressure, kPa (Tetens)."""
    t = np.asarray(t_c, dtype=float)
    return 0.6108 * np.exp(17.27 * t / (t + 237.3))


def slope_kpa(t_c):
    """Slope of the saturation curve, kPa K-1."""
    t = np.asarray(t_c, dtype=float)
    return 4098.0 * es_kpa(t) / (t + 237.3) ** 2


def rh_from_dew(t_c, dew_c):
    return np.clip(100.0 * es_kpa(dew_c) / es_kpa(t_c), 1.0, 100.0)


def dew_from_rh(t_c, rh_pct):
    t = np.asarray(t_c, dtype=float)
    rh = np.clip(np.asarray(rh_pct, dtype=float), 1.0, 100.0)
    g = np.log(rh / 100.0) + 17.62 * t / (243.12 + t)
    return 243.12 * g / (17.62 - g)


@dataclass(frozen=True)
class LeafParams:
    leaf_m: float = 0.20          # characteristic leaf size
    albedo: float = 0.20          # waxy cabbage leaves reflect a little more than most
    emissivity: float = 0.96      # cabbage leaves sit around 0.94-0.98
    wind_factor: float = 0.40     # wind at the leaf / wind at 10 m, open short crop
    sky_view: float = 0.85        # share of the sky a top leaf sees
    dew_cap_mm: float = 0.4       # most dew the leaf holds before it runs off
    rain_cap_mm: float = 0.6      # most rain it holds (Sentelhas et al. 2006)
    wet_mm: float = 0.01          # wetter than this counts as wet
    min_wind: float = 0.25        # still air is never perfectly still at a leaf

    def with_canopy(self, cover):
        """A closing canopy shields leaves from sky and wind. cover is the
        share of ground covered (0..1), from the satellite NDVI. Shading the
        sky by up to 40% and the wind by up to 50% at full cover are
        assumptions, not measurements."""
        c = float(np.clip(cover if cover is not None else 0.0, 0.0, 1.0))
        return replace(self, sky_view=self.sky_view * (1.0 - 0.4 * c),
                       wind_factor=self.wind_factor * (1.0 - 0.5 * c))


def sample_params(n, rng, base=LeafParams()):
    """n parameter sets around `base`, for the Monte Carlo runs. Each row is
    a dict of arrays (n,), so one call simulates all of them at once."""
    def logn(v, s):
        return v * np.exp(rng.normal(0.0, s, n))
    return {
        "leaf_m": logn(base.leaf_m, 0.3),
        "albedo": np.clip(rng.normal(base.albedo, 0.03, n), 0.1, 0.35),
        "emissivity": np.clip(rng.normal(base.emissivity, 0.01, n), 0.9, 0.99),
        "wind_factor": logn(base.wind_factor, 0.3),
        "sky_view": np.clip(rng.normal(base.sky_view, 0.08, n), 0.3, 1.0),
        "dew_cap_mm": logn(base.dew_cap_mm, 0.3),
        "rain_cap_mm": logn(base.rain_cap_mm, 0.3),
        "wet_mm": logn(base.wet_mm, 0.4),
        "min_wind": np.full(n, base.min_wind),
    }


def params_arrays(p, n):
    if isinstance(p, dict):
        return {k: np.broadcast_to(np.asarray(v, dtype=float), (n,)) for k, v in p.items()}
    return {k: np.full(n, float(getattr(p, k))) for k in LeafParams.__dataclass_fields__}


def _fill(a, value):
    a = np.array(a, dtype=float, copy=True)
    a[~np.isfinite(a)] = value
    return a


def simulate(temp_c, rh_pct, wind_ms=None, cloud_pct=None, sw_wm2=None, rain_mm=None,
             pressure_kpa=99.0, params=LeafParams(), water0=0.0):
    """Hour-by-hour leaf water.

    Inputs are arrays shaped (hours,) or (runs, hours); they broadcast to
    (runs, hours). Missing wind, cloud, sunshine or rain fall back to light
    wind, half cloud, the sun's absence and no rain, and are better supplied.
    Temperature and humidity must be complete: fill gaps before calling.

    Returns a dict of (runs, hours) arrays:
        wet      bool, leaf wet at the end of the hour
        water    mm on the leaf at the end of the hour
        flux     mm/h, negative while dew forms, positive while it dries
        source   0 dry, 1 dew, 2 rain (what last wetted it)
    """
    t = np.atleast_2d(np.asarray(temp_c, dtype=float))
    shape = np.broadcast_shapes(t.shape, np.atleast_2d(np.asarray(rh_pct, dtype=float)).shape)
    n, hours = shape

    def arr(x, default):
        if x is None:
            return np.full(shape, default)
        return np.broadcast_to(_fill(np.atleast_2d(np.asarray(x, dtype=float)), default), shape)

    t = np.broadcast_to(t, shape)
    rh = np.clip(arr(rh_pct, 80.0), 1.0, 100.0)
    wind = np.maximum(arr(wind_ms, 1.5), 0.0)
    cloud = np.clip(arr(cloud_pct, 50.0), 0.0, 100.0) / 100.0
    sw = np.maximum(arr(sw_wm2, 0.0), 0.0)
    rain = np.maximum(arr(rain_mm, 0.0), 0.0)
    p = params_arrays(params, n)

    water = np.broadcast_to(np.asarray(water0, dtype=float), (n,)).copy()
    from_rain = np.zeros(n, dtype=bool)
    out_wet = np.zeros(shape, dtype=bool)
    out_water = np.zeros(shape)
    out_flux = np.zeros(shape)
    out_src = np.zeros(shape, dtype=np.int8)
    gamma = CP * pressure_kpa / (0.622 * LV)                    # kPa K-1

    for h in range(hours):
        ta = t[:, h]
        tk = ta + 273.15
        es = es_kpa(ta)
        ea = es * rh[:, h] / 100.0
        rho_cp = pressure_kpa * 1000.0 / (RD * tk) * CP          # J m-3 K-1
        u = np.maximum(wind[:, h] * p["wind_factor"], p["min_wind"])
        # Boundary layer, both sides of the leaf (Sentelhas et al. 2006).
        rb = np.minimum(307.0 * np.sqrt(p["leaf_m"] / u) / 2.0, 400.0)
        # Radiative exchange linearised about air temperature, both sides.
        rr = rho_cp / (2.0 * 4.0 * p["emissivity"] * SIGMA * tk ** 3)
        rh_res = 1.0 / (1.0 / rb + 1.0 / rr)
        eps_clear = 1.24 * (ea * 10.0 / tk) ** (1.0 / 7.0)
        eps_sky = (1.0 - 0.84 * cloud[:, h]) * eps_clear + 0.84 * cloud[:, h]
        lw = p["emissivity"] * p["sky_view"] * (eps_sky - 1.0) * SIGMA * tk ** 4
        rn = (1.0 - p["albedo"]) * sw[:, h] + lw                  # isothermal net radiation
        # Dew settles on the upper side only; rain wets both.
        rv = np.where(from_rain & (water > 0), rb, 2.0 * rb)
        s = slope_kpa(ta)
        le = (s * rn + rho_cp * (es - ea) / rh_res) / (s + gamma * rv / rh_res)
        flux = le / LV * 3600.0                                    # mm/h

        r = rain[:, h]
        took = np.minimum(r, np.maximum(p["rain_cap_mm"] - water, 0.0))
        water = water + took
        from_rain = np.where(r > 0.05, True, from_rain)
        dew = flux < 0
        water = np.where(dew, np.minimum(water - flux, np.maximum(p["dew_cap_mm"], water)),
                         np.maximum(water - flux, 0.0))
        from_rain = np.where(water <= 0, False, from_rain)

        wet = water > p["wet_mm"]
        out_wet[:, h] = wet
        out_water[:, h] = water
        out_flux[:, h] = flux
        out_src[:, h] = np.where(~wet, 0, np.where(from_rain, 2, 1))
    return {"wet": out_wet, "water": out_water, "flux": out_flux, "source": out_src}


def clear_sky_sw(times, lat, lon, transmissivity=0.75):
    """Rough hour-mean sunshine under a clear sky, W m-2, for hours that
    have no measured or modelled value. times: aware UTC hour starts."""
    from math import cos, pi, radians, sin
    out = []
    phi = radians(lat)
    for t in times:
        doy = t.timetuple().tm_yday
        hour = t.hour + 0.5 + t.minute / 60.0
        decl = radians(23.44) * sin(2 * pi * (284 + doy) / 365.0)
        b = 2 * pi * (doy - 81) / 364.0
        eot = 9.87 * sin(2 * b) - 7.53 * cos(b) - 1.5 * sin(b)          # minutes
        solar = hour + lon / 15.0 + eot / 60.0
        ha = radians((solar - 12.0) * 15.0)
        s = sin(phi) * sin(decl) + cos(phi) * cos(decl) * cos(ha)
        out.append(max(0.0, 1361.0 * transmissivity * s))
    return np.array(out)


def proxy_wet(temp_c, dew_c, offset_c=1.0, margin_c=2.0):
    """The dew-point rule from src/ml/wetness.py, as arrays, for comparison:
    leaf = air - offset; wet when (leaf - dew) is under half the margin."""
    t = np.asarray(temp_c, dtype=float)
    d = np.asarray(dew_c, dtype=float)
    return (t - offset_c - d) <= margin_c / 2.0
