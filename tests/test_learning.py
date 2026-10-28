"""Calibration from the field log, the learned healthy baseline, and alerts.
No network and no database: everything is fed in directly."""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import alerts
from src.ml import baseline, calibration as cal

ALT = ("alternaria_brassicae", "alternaria_brassicicola")


def levels(spec):
    """{date: level} for both Alternaria models -> levels_by_day shape."""
    return {d: {"levels": {k: lv for k in ALT}} for d, lv in spec.items()}


def days(start, n, lv):
    return {start + timedelta(days=i): lv for i in range(n)}


def row(d, disease="none", severity=0, affected=0):
    return {"date": d.isoformat(), "disease": disease, "severity": severity,
            "plants_affected": affected, "plants_checked": 20}


# ------------------------------------------------------------- calibration

def test_wilson_interval():
    lo, hi = cal.wilson(5, 10)
    assert (round(lo, 2), round(hi, 2)) == (0.27, 0.73)
    assert cal.wilson(0, 0) == (None, None)
    assert cal.wilson(0, 4)[0] == 0.0


def test_symptoms_are_judged_against_the_weather_days_before():
    d0 = date(2026, 10, 1)
    lv = levels({**days(d0, 6, "high"), **days(d0 + timedelta(days=6), 20, "low")})
    rows = [row(date(2026, 10, 8), "alternaria", 2, 3),      # window 1-6 Oct: high
            row(date(2026, 10, 15)),                         # healthy, window low
            row(date(2026, 10, 18), "alternaria", 2, 4)]     # back after a clean visit
    r = cal.calibrate(rows, lv)
    t = r["levels"]
    assert (t["high"]["n"], t["high"]["k"]) == (1, 1)
    assert (t["low"]["n"], t["low"]["k"]) == (2, 1)
    assert r["judged"] == 3 and r["positives"] == 2 and not r["ready"]
    assert r["warning"]["caught"] == 1 and r["warning"]["outbreaks"] == 2
    # Downy mildew, black rot and white mould have no model levels here, so
    # they are left out rather than guessed.
    assert r["unscored"] == 9


def test_a_lasting_infection_is_not_counted_twice():
    oc = cal.outcomes(cal.visits([row(date(2026, 10, 1), "alternaria", 2, 2),
                                  row(date(2026, 10, 5), "alternaria", 2, 5),
                                  row(date(2026, 10, 9), "alternaria", 3, 8)]))
    assert [oc[(date(2026, 10, d), "alternaria")] for d in (1, 5, 9)] == [True, None, True]
    assert oc[(date(2026, 10, 5), "black_rot")] is False


def test_too_little_station_data_leaves_a_visit_unscored():
    lv = levels({date(2026, 10, 2): "high"})                  # 1 day of a 6-day window
    r = cal.calibrate([row(date(2026, 10, 8), "alternaria", 1, 1)], lv)
    assert r["judged"] == 0 and r["unscored"] >= 1


def test_ready_needs_enough_visits_and_outbreaks():
    d0 = date(2026, 9, 1)
    lv = levels(days(d0, 60, "rising"))
    rows = [row(d0 + timedelta(days=9 + 2 * i)) for i in range(18)]
    rows += [row(d0 + timedelta(days=50), "alternaria", 1, 1), row(d0 + timedelta(days=52)),
             row(d0 + timedelta(days=54), "alternaria", 1, 1), row(d0 + timedelta(days=56)),
             row(d0 + timedelta(days=58), "alternaria", 2, 2)]
    r = cal.calibrate(rows, lv)
    assert r["positives"] == 3 and r["ready"]


# --------------------------------------------------------- healthy baseline

def test_ridge_recovers_a_linear_relation():
    rng = np.random.default_rng(0)
    X = np.column_stack([np.ones(200), rng.normal(size=200), rng.normal(size=200)])
    y = X @ np.array([2.0, 0.5, -1.5])
    beta = baseline.ridge(X, y, lam=1e-6)
    assert np.allclose(beta, [2.0, 0.5, -1.5], atol=1e-3)


def hourly(n_days, departure_at=None, spike_at=None):
    rng = np.random.default_rng(1)
    t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n_days * 24):
        t = t0 + timedelta(hours=i)
        lh = (i + 5.5) % 24
        era_t = 26 + 6 * np.sin(2 * np.pi * (lh - 9) / 24) + rng.normal(0, .3)
        era_dew = 19 + rng.normal(0, .5)
        st_t = era_t + 0.8 + 1.5 * np.sin(2 * np.pi * (lh - 12) / 24) + rng.normal(0, .4)
        st_dew = era_dew + 0.6 * np.cos(2 * np.pi * lh / 24) + rng.normal(0, .3)
        if departure_at is not None and departure_at <= i < departure_at + 5:
            st_t -= 7.0
        if spike_at is not None and i == spike_at:
            st_t += 9.0
        rows.append({"time": t, "local_hour": lh, "ndvi": 0.5, "st_temp": st_t, "st_dew": st_dew,
                     "era_temp": era_t, "era_dew": era_dew})
    return rows


def test_microclimate_flags_a_sustained_departure_only():
    rows = hourly(30, departure_at=27 * 24 + 6, spike_at=25 * 24 + 3)
    out = baseline.microclimate(rows, rows[-1]["time"] - timedelta(days=7))
    assert out["state"] == "departure"
    assert out["targets"]["st_temp"]["r2"] > 0.8
    flagged = [f for f in out["flags"] if f["variable"] == "st_temp"]
    assert len(flagged) == 5 and all(f["direction"] == "lower" for f in flagged)


def test_microclimate_stays_quiet_on_a_normal_week_and_waits_for_data():
    rows = hourly(30)
    assert baseline.microclimate(rows, rows[-1]["time"] - timedelta(days=7))["state"] == "normal"
    short = hourly(8)
    assert baseline.microclimate(short, short[-1]["time"] - timedelta(days=2))["state"] == "learning"


def test_canopy_curve_flags_a_fall_below_the_season():
    t = [0, 10, 20, 25, 30, 35, 40, 45, 50]
    healthy = [round(float(baseline.logistic(x, 0.3, 0.5, 0.2, 25)), 3) for x in t]
    ok = baseline.canopy(t, healthy, min_points=5, sigma_floor=0.03)
    assert ok["state"] == "normal"
    fallen = healthy[:-1] + [healthy[-1] - 0.25]
    bad = baseline.canopy(t, fallen, min_points=5, sigma_floor=0.03)
    assert bad["state"] == "departure" and bad["z"] < -2.5
    assert baseline.canopy(t[:4], healthy[:4], min_points=5, sigma_floor=0.03)["state"] == "learning"


# ------------------------------------------------------------------ alerts

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def forecast(level="high", high=40, n=51, sourced=True, lead=24):
    return {"rows": [{"key": "alternaria_brassicae", "name": "Alternaria leaf spot (cool strain)",
                      "sourced": sourced, "cells": [
                          {"lead_h": 12, "at": NOW + timedelta(hours=12), "level": "low",
                           "ens": {"n": n, "high": 0, "rising": 0, "low": n}},
                          {"lead_h": lead, "at": NOW + timedelta(hours=lead), "level": level,
                           "ens": {"n": n, "high": high, "rising": 0, "low": n - high}}]}]}


def test_alert_needs_high_soon_and_most_runs_agreeing():
    assert len(alerts.candidates(forecast())) == 1
    assert alerts.candidates(forecast(high=20)) == []              # 20 of 51 runs only
    assert alerts.candidates(forecast(lead=60)) == []              # beyond 48 h
    assert alerts.candidates(forecast(level="rising")) == []
    assert alerts.candidates(forecast(sourced=False), include_estimated=False) == []
    assert len(alerts.candidates(forecast(sourced=False), include_estimated=True)) == 1


def test_alert_once_then_daily_while_it_stays_high():
    r = alerts.evaluate(forecast(), {}, None, NOW)
    assert len(r["send"]) == 1 and "Uncalibrated" in r["send"][0]["body"]
    assert "40 of 51" in r["send"][0]["body"]
    r2 = alerts.evaluate(forecast(), r["state"], None, NOW + timedelta(hours=3))
    assert r2["send"] == []
    r3 = alerts.evaluate(forecast(), r2["state"], None, NOW + timedelta(hours=25))
    assert len(r3["send"]) == 1
    cleared = alerts.evaluate(forecast(level="low"), r3["state"], None, NOW + timedelta(hours=26))
    assert cleared["send"] == [] and not cleared["state"]["per"]["alternaria_brassicae"]["active"]
    again = alerts.evaluate(forecast(), cleared["state"], None, NOW + timedelta(hours=27))
    assert len(again["send"]) == 1                                 # it turned high again


def test_alert_quotes_the_track_record_once_calibrated():
    calib = {"ready": True, "levels": {"high": {"n": 8, "k": 5, "lo": 0.33, "hi": 0.85}}}
    body = alerts.evaluate(forecast(), {}, calib, NOW)["send"][0]["body"]
    assert "5 of 8" in body and "33 to 85%" in body and "Uncalibrated" not in body
