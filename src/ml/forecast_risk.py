"""Disease pressure for the next 12 to 72 hours, from a weather forecast.

HOW IT WORKS
    The same physics-floor models as the live score (src/ml/physics_baseline),
    fed with forecast hours instead of measured ones. For each lead time the
    question is: over the 24 hours ending then, how many hours were wet, and
    how warm was it while they were? Hours that are already past come from
    the station; hours still ahead come from the forecast. A past hour the
    station missed (an outage) falls back to the model's own value for it.

    A forecast hour counts as wet exactly the way a station reading does:
    leaf temperature estimated as air minus the offset, wet when that is at
    or below the dew point (wetness index 50 or more). Same proxy, same
    weakness, stated once in src/ml/wetness.py.

THE RANGE
    Run the same scoring once per member of a weather ensemble and count how
    many members land on each level. "High in 34 of 51 runs" is a spread in
    the WEATHER, carried through the model. It is not a calibrated chance of
    infection, and nothing here pretends otherwise: calibration needs a
    season of recorded outcomes, which is what the field log is for.
"""
from datetime import timedelta

from src.ml.physics_baseline import DISEASES
from src.ml.wetness import estimated_leaf_temp_c, leaf_wetness_index

LEADS_H = (12, 24, 36, 48, 60, 72)
WINDOW_H = 24
WET_INDEX = 50.0
HIGH, RISING = 0.66, 0.33          # same cut-offs the dashboard uses


def level(pressure):
    if pressure is None:
        return None
    if pressure > HIGH:
        return "high"
    if pressure > RISING:
        return "rising"
    return "low"


def forecast_hour_wet(temp_c, dew_c):
    """1.0 if this forecast hour would be scored wet, else 0.0."""
    if temp_c is None or dew_c is None:
        return None
    leaf = estimated_leaf_temp_c(temp_c)
    return 1.0 if leaf_wetness_index(leaf, dew_c) >= WET_INDEX else 0.0


def score_window(hours):
    """Pressure per disease for one window.

    hours: iterable of (wet_fraction 0..1, temp_c). Wet hours are summed from
    the fractions; the temperature that matters is the mean while wet.
    """
    wet_h = 0.0
    weighted_t = 0.0
    for wet, temp in hours:
        if wet is None or temp is None:
            continue
        wet_h += wet
        weighted_t += wet * temp
    mean_wet_t = (weighted_t / wet_h) if wet_h > 0 else None
    out = {}
    for key, model in DISEASES.items():
        p = model.pressure(mean_wet_t, wet_h) if mean_wet_t is not None else 0.0
        out[key] = round(p, 4)
    return out, round(wet_h, 1), (round(mean_wet_t, 1) if mean_wet_t is not None else None)


def _timeline(observed, forecast):
    """Merge into {hour_start: (wet_fraction, temp)}. Forecast never overwrites
    an hour the station already measured."""
    line = {}
    for t, wet, temp in forecast:
        line[t] = (wet, temp)
    for t, wet, temp in observed:
        line[t] = (wet, temp)
    return line


def curve(observed, forecast, now, leads=LEADS_H):
    """Deterministic risk curve.

    observed: [(hour_start_utc, wet_fraction, temp_c)] for recent hours.
    forecast: [(hour_start_utc, wet_fraction, temp_c)] for coming hours.
    now:      timezone-aware UTC datetime, floored to the hour by the caller.
    """
    line = _timeline(observed, forecast)
    points = []
    for lead in leads:
        end = now + timedelta(hours=lead)
        start = end - timedelta(hours=WINDOW_H)
        window = [v for t, v in line.items() if start <= t < end]
        if len(window) < WINDOW_H * 0.75:       # not enough hours to say anything
            points.append({"lead_h": lead, "at": end, "available": False})
            continue
        pressures, wet_h, mean_t = score_window(window)
        points.append({
            "lead_h": lead, "at": end, "available": True,
            "wet_hours": wet_h, "mean_wet_temp": mean_t,
            "diseases": {k: {"pressure": p, "level": level(p)}
                         for k, p in pressures.items()},
        })
    return points


def ensemble_curve(observed, members, now, leads=LEADS_H):
    """Same scoring per ensemble member; returns level counts per disease.

    members: list of forecast lists, each shaped like `forecast` in curve().
    """
    per_member = [curve(observed, m, now, leads) for m in members]
    points = []
    for i, lead in enumerate(leads):
        usable = [pm[i] for pm in per_member if pm[i].get("available")]
        if not usable:
            points.append({"lead_h": lead, "available": False})
            continue
        diseases = {}
        for key in DISEASES:
            levels = [u["diseases"][key]["level"] for u in usable]
            pressures = sorted(u["diseases"][key]["pressure"] for u in usable)
            diseases[key] = {
                "n": len(usable),
                "low": levels.count("low"),
                "rising": levels.count("rising"),
                "high": levels.count("high"),
                "median": pressures[len(pressures) // 2],
            }
        points.append({"lead_h": lead, "available": True, "diseases": diseases})
    return points


def combine(det, ens):
    """One row per disease with a cell per lead, ready for the page."""
    rows = []
    for key, model in DISEASES.items():
        cells = []
        for i, point in enumerate(det):
            cell = {"lead_h": point["lead_h"], "at": point.get("at")}
            if point.get("available"):
                cell.update(point["diseases"][key])
            e = ens[i] if i < len(ens) else None
            if e and e.get("available"):
                cell["ens"] = e["diseases"][key]
            cells.append(cell)
        rows.append({"key": key, "name": model.name, "pathogen": model.pathogen,
                     "sourced": model.sourced, "cells": cells})
    return rows
