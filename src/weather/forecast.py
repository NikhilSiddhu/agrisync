"""Forecast fetch from Open-Meteo.

Open-Meteo is free, needs no API key, and no signup. That matters for a
project that has to keep running after any trial credit expires.

Results are cached in memory for CACHE_MINUTES so a burst of risk requests
does not hammer the API, and so a brief internet outage does not immediately
break the risk endpoint.
"""
import time

import requests

ENDPOINT = "https://api.open-meteo.com/v1/forecast"
CACHE_MINUTES = 30
_cache = {"fetched_at": 0.0, "data": None}


def fetch_forecast(lat: float, lon: float, tz: str = "Asia/Kolkata", hours: int = 72):
    """Return hourly temp / RH / precip / dewpoint, or None if unreachable."""
    now = time.time()
    if _cache["data"] and (now - _cache["fetched_at"]) < CACHE_MINUTES * 60:
        return _cache["data"]

    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m,precipitation",
        "timezone": tz,
        "forecast_days": 4,
    }

    try:
        resp = requests.get(ENDPOINT, params=params, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        # Return the stale cache if we have one. A three hour old forecast
        # beats no forecast at all.
        return _cache["data"]

    hourly = payload.get("hourly", {})
    rows = []
    for i, stamp in enumerate(hourly.get("time", [])[:hours]):
        rows.append({
            "time": stamp,
            "temp_c": hourly["temperature_2m"][i],
            "humidity_pct": hourly["relative_humidity_2m"][i],
            "dew_point_c": hourly["dew_point_2m"][i],
            "precip_mm": hourly["precipitation"][i],
        })

    _cache["fetched_at"] = now
    _cache["data"] = rows
    return rows
