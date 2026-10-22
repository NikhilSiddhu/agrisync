"""Fetch and parse the outside feeds. Network calls are thin; parsing is
separate so it can be tested without the internet.

All times are UTC. Open-Meteo is asked for timezone=GMT so its hourly stamps
line up with how Mongo stores readings.
"""
import os
import time
from datetime import date, datetime, timezone

import requests

UA = {"User-Agent": "AgriSync field station (agrisync.tech)"}

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
STAC_URL = "https://earth-search.aws.element84.com/v1/search"

HOURLY_VARS = "temperature_2m,relative_humidity_2m,dew_point_2m"
ENSEMBLE_MODEL = os.getenv("FORECAST_ENSEMBLE_MODEL", "ecmwf_ifs025")


def _get(url, params=None, timeout=60, tries=3, method="GET", json_body=None, raw=False):
    last = None
    for attempt in range(tries):
        try:
            if method == "POST":
                r = requests.post(url, json=json_body, headers=UA, timeout=timeout)
            else:
                r = requests.get(url, params=params, headers=UA, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last = RuntimeError(f"{url.split('?')[0]} answered {r.status_code}")
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.content if raw else r.json()
        except requests.RequestException as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"{url.split('?')[0]} failed: {last}")


def _gdal_env():
    """Settings for reading cloud-optimised GeoTIFFs over HTTPS."""
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    os.environ.setdefault("CPL_VSIL_CURL_ALLOWED_EXTENSIONS", ".tif,.tiff")
    # The rasterio wheel's curl looks for a Red Hat style CA path. On Debian
    # (Raspberry Pi OS) point it at the system bundle or HTTPS reads fail.
    if "CURL_CA_BUNDLE" not in os.environ and os.path.exists("/etc/ssl/certs/ca-certificates.crt"):
        os.environ["CURL_CA_BUNDLE"] = "/etc/ssl/certs/ca-certificates.crt"


def _utc(stamp):
    """Open-Meteo 'YYYY-MM-DDTHH:MM' in GMT -> aware UTC datetime."""
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- Open-Meteo

def parse_hourly(payload, suffix=""):
    h = payload.get("hourly") or {}
    rows = []
    for i, stamp in enumerate(h.get("time", [])):
        def val(name):
            col = h.get(name + suffix)
            return col[i] if col is not None and i < len(col) else None
        rows.append({
            "time": _utc(stamp),
            "temp_c": val("temperature_2m"),
            "rh_pct": val("relative_humidity_2m"),
            "dew_c": val("dew_point_2m"),
            "precip_mm": val("precipitation"),
        })
    return rows


def forecast(lat, lon, days=4):
    # past_days=1: the model's own last 24 hours stand in for any hour the
    # station missed, so a station outage does not blank the 12 h column.
    payload = _get(FORECAST_URL, {
        "latitude": lat, "longitude": lon, "timezone": "GMT",
        "hourly": HOURLY_VARS + ",precipitation", "forecast_days": days, "past_days": 1,
    })
    return parse_hourly(payload)


def parse_ensemble(payload):
    """-> list of member series (control first). Each like parse_hourly()."""
    h = payload.get("hourly") or {}
    suffixes = [""] + sorted({k.split("_member")[1] for k in h if "_member" in k})
    members = []
    for s in suffixes:
        suffix = "" if s == "" else "_member" + s
        if "temperature_2m" + suffix not in h:
            continue
        members.append(parse_hourly(payload, suffix))
    return members


def ensemble(lat, lon, days=4, model=ENSEMBLE_MODEL):
    payload = _get(ENSEMBLE_URL, {
        "latitude": lat, "longitude": lon, "timezone": "GMT",
        "hourly": HOURLY_VARS, "models": model, "forecast_days": days, "past_days": 1,
    }, timeout=90)
    return parse_ensemble(payload)


def era5_land(lat, lon, start, end):
    """Hourly ERA5-Land reanalysis. Runs about five days behind real time;
    the hours it has not reached yet come back empty and are dropped."""
    payload = _get(ARCHIVE_URL, {
        "latitude": lat, "longitude": lon, "timezone": "GMT",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": HOURLY_VARS, "models": "era5_land",
    }, timeout=120)
    rows = [{k: r[k] for k in ("time", "temp_c", "rh_pct", "dew_c")}
            for r in parse_hourly(payload) if r["temp_c"] is not None]
    meta = {"grid_lat": payload.get("latitude"), "grid_lon": payload.get("longitude"),
            "elevation_m": payload.get("elevation")}
    return rows, meta


def parse_rain(payload):
    h = payload.get("hourly") or {}
    return [{"time": _utc(t), "rain_mm": v}
            for t, v in zip(h.get("time", []), h.get("precipitation", [])) if v is not None]


def era5_rain(lat, lon, start, end):
    """Hourly rain from ERA5 (0.25 degree grid). ERA5-Land takes its rain
    from ERA5 anyway, and Open-Meteo returns ERA5-Land's empty. Each value
    is the total for the hour ENDING at that time. A model estimate: the
    station has a wet/dry rain sensor, not a gauge."""
    payload = _get(ARCHIVE_URL, {
        "latitude": lat, "longitude": lon, "timezone": "GMT",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": "precipitation", "models": "era5",
    }, timeout=120)
    return parse_rain(payload)


# ---------------------------------------------------------------- NASA POWER

# Sunlight only. PRECTOTCORR is left out on purpose: for this season the
# near-real-time daily rain came back roughly 24 times what ERA5 shows for the
# same days (608 mm on 11 Aug), so rain comes from ERA5 instead.
POWER_PARAMS = ("T2M", "RH2M", "ALLSKY_SFC_SW_DWN")
MJ_PER_KWH = 3.6


def parse_power(payload):
    p = (payload.get("properties") or {}).get("parameter") or {}
    units = {k: (v or {}).get("units") or "" for k, v in (payload.get("parameters") or {}).items()}
    # The AG community reports sunlight in MJ/m^2/day; other communities in
    # kWh. Go by what the response says, defaulting to AG's MJ.
    sun_unit = units.get("ALLSKY_SFC_SW_DWN") or "MJ/m^2/day"
    sun_scale = 1.0 / MJ_PER_KWH if sun_unit.upper().startswith("MJ") else 1.0
    days = sorted(set().union(*[set(v.keys()) for v in p.values()])) if p else []
    rows = []
    for d in days:
        def val(name):
            v = (p.get(name) or {}).get(d)
            return None if v is None or v <= -998 else v
        sun = val("ALLSKY_SFC_SW_DWN")
        rows.append({
            "date": datetime.strptime(d, "%Y%m%d").date().isoformat(),
            "t2m_c": val("T2M"), "rh_pct": val("RH2M"),
            "solar_kwh_m2": round(sun * sun_scale, 2) if sun is not None else None,
        })
    return rows


def nasa_power(lat, lon, start, end):
    payload = _get(POWER_URL, {
        "parameters": ",".join(POWER_PARAMS), "community": "AG",
        "latitude": lat, "longitude": lon, "format": "JSON",
        "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
    }, timeout=120)
    return [r for r in parse_power(payload) if any(
        r[k] is not None for k in ("t2m_c", "rh_pct", "solar_kwh_m2"))]


# ---------------------------------------------------------------- Sentinel-2

SCL_VALID = (4, 5)      # vegetation, bare soil. Clouds, shadow, water, snow out.


def s2_items(lat, lon, start, end, limit=100):
    body = {
        "collections": ["sentinel-2-l2a"],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": limit,
    }
    payload = _get(STAC_URL, method="POST", json_body=body, timeout=60)
    items = []
    for f in payload.get("features", []):
        a, p = f.get("assets", {}), f.get("properties", {})
        if not all(k in a for k in ("red", "nir", "scl")):
            continue
        items.append({
            "id": f["id"],
            "datetime": p.get("datetime"),
            "cloud_cover": p.get("eo:cloud_cover"),
            "offset_applied": p.get("earthsearch:boa_offset_applied", True),
            "baseline": p.get("s2:processing_baseline"),
            "red": a["red"]["href"], "nir": a["nir"]["href"], "scl": a["scl"]["href"],
        })
    return items


def ndvi_stats(red, nir, scl, offset=0.0):
    """Mean NDVI over pixels the scene classification calls clear land.

    red, nir: arrays of digital numbers; scl: same shape, class codes.
    offset: subtracted from both bands first (1000 for processing baseline
    04.00 and later when the catalogue has not already applied it).
    """
    import numpy as np
    red = red.astype("float64") - offset
    nir = nir.astype("float64") - offset
    valid = np.isin(scl, SCL_VALID) & ((nir + red) > 0)
    total = int(scl.size)
    n = int(valid.sum())
    if n == 0:
        return {"valid_px": 0, "total_px": total, "ndvi": None, "ndvi_median": None}
    ndvi = (nir[valid] - red[valid]) / (nir[valid] + red[valid])
    return {"valid_px": n, "total_px": total,
            "ndvi": round(float(ndvi.mean()), 3),
            "ndvi_median": round(float(np.median(ndvi)), 3)}


def s2_ndvi(item, lat, lon, radius_m=15.0):
    """Read a small window round the field from the cloud-optimised GeoTIFFs.
    Three HTTP range reads, a few kilobytes each."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform
    from rasterio.windows import from_bounds

    _gdal_env()
    with rasterio.Env():
        with rasterio.open(item["red"]) as ds:
            xs, ys = transform("EPSG:4326", ds.crs, [lon], [lat])
            b = (xs[0] - radius_m, ys[0] - radius_m, xs[0] + radius_m, ys[0] + radius_m)
            win = from_bounds(*b, transform=ds.transform).round_offsets().round_lengths()
            red = ds.read(1, window=win)
        with rasterio.open(item["nir"]) as ds:
            nir = ds.read(1, window=from_bounds(*b, transform=ds.transform)
                          .round_offsets().round_lengths(), out_shape=red.shape)
        with rasterio.open(item["scl"]) as ds:
            scl = ds.read(1, window=from_bounds(*b, transform=ds.transform),
                          out_shape=red.shape, resampling=Resampling.nearest)
    baseline = item.get("baseline") or "00.00"
    offset = 1000.0 if (not item.get("offset_applied", True) and baseline >= "04.00") else 0.0
    stats = ndvi_stats(red, nir, scl, offset)
    stats.update({"scene": item["id"], "cloud_cover": item.get("cloud_cover"),
                  "acquired": item["datetime"], "radius_m": radius_m})
    return stats


# ---------------------------------------------------------------- Sentinel-1
#
# Radar sees through cloud, so it keeps passing over the field through the
# monsoon when Sentinel-2 is blind. Source: the Level-1 GRD scenes Microsoft's
# Planetary Computer mirrors from ESA, read with an anonymous access token
# (the terrain-corrected RTC copy there needs an account; the GRD does not,
# and on the flat Gangetic plain the terrain correction changes little).
#
# Per pass: find the field in the scene from its ground control points, read
# a small window of digital numbers, take away thermal noise and calibrate to
# gamma nought with the scene's own look-up tables. Averaged in linear power.
# Azimuth noise is not removed; at this field it is a small, steady factor.

MPC_STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
MPC_TOKEN_URL = "https://planetarycomputer.microsoft.com/api/sas/v1/token/sentinel-1-grd"
GRD_PIXEL_M = 10.0
S1_ASSETS = ("vv", "vh", "schema-calibration-vv", "schema-calibration-vh",
             "schema-noise-vv", "schema-noise-vh")


def mpc_token():
    return _get(MPC_TOKEN_URL, timeout=30)["token"]


def _signed(href, token):
    return href + ("&" if "?" in href else "?") + token


def s1_items(lat, lon, start, end, limit=250):
    body = {
        "collections": ["sentinel-1-grd"],
        "intersects": {"type": "Point", "coordinates": [lon, lat]},
        "datetime": f"{start.isoformat()}T00:00:00Z/{end.isoformat()}T23:59:59Z",
        "limit": limit,
    }
    payload = _get(MPC_STAC_URL, method="POST", json_body=body, timeout=60)
    items = []
    for f in payload.get("features", []):
        a, p = f.get("assets", {}), f.get("properties", {})
        if not all(k in a for k in S1_ASSETS):
            continue
        if p.get("sar:instrument_mode", "IW") != "IW":
            continue
        item = {"id": f["id"], "datetime": p.get("datetime"),
                "orbit": p.get("sat:orbit_state"), "track": p.get("sat:relative_orbit"),
                "abs_orbit": p.get("sat:absolute_orbit"), "platform": p.get("platform")}
        item.update({k: a[k]["href"] for k in S1_ASSETS})
        items.append(item)
    return items


def annotation_lut(xml, row, col, vector_tag, value_tag):
    """A Sentinel-1 annotation look-up table evaluated at one image position:
    linear along each vector's pixels, then linear between vectors by line."""
    import xml.etree.ElementTree as ET

    import numpy as np
    root = ET.fromstring(xml)
    lines, vals = [], []
    for v in root.iter(vector_tag):
        pix, val, line = v.findtext("pixel"), v.findtext(value_tag), v.findtext("line")
        if pix is None or val is None or line is None:
            continue
        pix = np.array(pix.split(), dtype=float)
        val = np.array(val.split(), dtype=float)
        n = min(len(pix), len(val))
        if n == 0:
            continue
        lines.append(int(line))
        vals.append(float(np.interp(col, pix[:n], val[:n])))
    if not lines:
        raise ValueError(f"no {vector_tag}/{value_tag} in the annotation")
    order = np.argsort(lines)
    return float(np.interp(row, np.array(lines)[order], np.array(vals)[order]))


def noise_lut(xml, row, col):
    """Thermal noise power at one position. Newer products split it into
    range and azimuth parts; older ones have a single noiseVector."""
    for vec, val in (("noiseRangeVector", "noiseRangeLut"), ("noiseVector", "noiseLut")):
        try:
            return annotation_lut(xml, row, col, vec, val)
        except ValueError:
            continue
    return 0.0


def radar_stats(dn_vv, dn_vh, cal_vv, cal_vh, noise_vv=0.0, noise_vh=0.0):
    """gamma0 in dB per polarisation, their ratio, and the dual-pol radar
    vegetation index RVI = 4 VH / (VV + VH): low over bare soil, rising as
    leaves and stems add volume scattering.

    dn_*: windows of digital numbers (0 is no data). cal_*: the gamma LUT
    value; noise_*: thermal noise in DN^2. Power is averaged before any log.
    """
    import math

    import numpy as np
    lin, valid = {}, None
    for pol, dn, cal, noise in (("vv", dn_vv, cal_vv, noise_vv), ("vh", dn_vh, cal_vh, noise_vh)):
        dn = np.asarray(dn, dtype="float64")
        ok = dn > 0
        n = int(ok.sum())
        valid = n if valid is None else min(valid, n)
        if n == 0:
            lin[pol] = None
            continue
        power = float((dn[ok] ** 2).mean()) - noise
        lin[pol] = power / cal ** 2 if power > 0 else None   # below the noise floor
    vv, vh = lin["vv"], lin["vh"]
    db = (lambda x: round(10 * math.log10(x), 2) if x else None)
    both = bool(vv and vh)
    return {"vv_db": db(vv), "vh_db": db(vh),
            "ratio_db": round(10 * math.log10(vh / vv), 2) if both else None,
            "rvi": round(4 * vh / (vv + vh), 3) if both else None,
            "valid_px": valid or 0, "total_px": int(np.asarray(dn_vv).size)}


def s1_backscatter(item, lat, lon, radius_m=15.0, token=None):
    import rasterio
    from rasterio.transform import GCPTransformer
    from rasterio.windows import Window

    _gdal_env()
    token = token or mpc_token()
    size = max(3, int(round(2 * radius_m / GRD_PIXEL_M)))
    dn = {}
    with rasterio.Env():
        with rasterio.open(_signed(item["vv"], token)) as ds:
            gcps, _ = ds.gcps
            if not gcps:
                raise ValueError("scene has no ground control points")
            row, col = GCPTransformer(gcps).rowcol(lon, lat)
            r0, c0 = int(row) - size // 2, int(col) - size // 2
            if r0 < 0 or c0 < 0 or r0 + size > ds.height or c0 + size > ds.width:
                raise ValueError("field is outside or at the edge of this scene")
            win = Window(c0, r0, size, size)
            dn["vv"] = ds.read(1, window=win)
        with rasterio.open(_signed(item["vh"], token)) as ds:      # same grid as VV
            dn["vh"] = ds.read(1, window=win)
    cal, noise = {}, {}
    for pol in ("vv", "vh"):
        xml = _get(_signed(item["schema-calibration-" + pol], token), timeout=90, raw=True)
        cal[pol] = annotation_lut(xml, row, col, "calibrationVector", "gamma")
        xml = _get(_signed(item["schema-noise-" + pol], token), timeout=90, raw=True)
        noise[pol] = noise_lut(xml, row, col)
    stats = radar_stats(dn["vv"], dn["vh"], cal["vv"], cal["vh"], noise["vv"], noise["vh"])
    stats.update({"scene": item["id"], "acquired": item["datetime"], "orbit": item.get("orbit"),
                  "track": item.get("track"), "abs_orbit": item.get("abs_orbit"),
                  "platform": item.get("platform"), "row": int(row), "col": int(col),
                  "radius_m": radius_m})
    return stats


def season_start(default=date(2026, 8, 1)):
    try:
        return date.fromisoformat(os.getenv("SEASON_START", default.isoformat()))
    except ValueError:
        return default
