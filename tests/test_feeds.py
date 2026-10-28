"""Outside feeds: forecast risk, unusual conditions, parsing, the runner and
what the dashboard shows. No network: every source is replaced."""
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard import data as ddata
from scripts import update_feeds as uf
from src.feeds import sources
from src.ml import anomaly, forecast_risk as fr

NOW = datetime(2026, 10, 22, 9, 0, tzinfo=timezone.utc)


def hours(start, n, wet, temp):
    return [(start + timedelta(hours=i), wet, temp) for i in range(n)]


# ------------------------------------------------------------ forecast risk

def test_forecast_hour_wet_uses_the_station_proxy():
    assert fr.forecast_hour_wet(20.0, 20.0) == 1.0     # leaf 19 below dew 20
    assert fr.forecast_hour_wet(30.0, 10.0) == 0.0
    assert fr.forecast_hour_wet(None, 10.0) is None


def test_wet_cool_day_scores_high_for_the_cool_alternaria():
    observed = hours(NOW - timedelta(hours=24), 24, 1.0, 18.0)
    forecast = hours(NOW, 72, 1.0, 18.0)
    pts = fr.curve(observed, forecast, NOW)
    first = pts[0]
    assert first["available"] and first["wet_hours"] == 24.0
    assert first["diseases"]["alternaria_brassicae"]["level"] == "high"


def test_dry_forecast_scores_low():
    observed = hours(NOW - timedelta(hours=24), 24, 0.0, 30.0)
    forecast = hours(NOW, 72, 0.0, 30.0)
    for p in fr.curve(observed, forecast, NOW):
        assert all(d["level"] == "low" for d in p["diseases"].values())


def test_too_few_hours_is_said_rather_than_guessed():
    pts = fr.curve([], hours(NOW, 10, 1.0, 18.0), NOW)
    assert all(not p["available"] for p in pts)


def test_station_hours_are_not_overwritten_by_the_forecast():
    t = NOW - timedelta(hours=1)
    line = fr._timeline([(t, 1.0, 18.0)], [(t, 0.0, 30.0)])
    assert line[t] == (1.0, 18.0)


def test_ensemble_counts_members_per_level():
    observed = hours(NOW - timedelta(hours=24), 24, 0.0, 18.0)
    wet = hours(NOW, 72, 1.0, 18.0)
    dry = hours(NOW, 72, 0.0, 18.0)
    ens = fr.ensemble_curve(observed, [wet, wet, dry], NOW)
    last = ens[-1]["diseases"]["alternaria_brassicae"]
    assert (last["n"], last["high"], last["low"]) == (3, 2, 1)
    rows = fr.combine(fr.curve(observed, wet, NOW), ens)
    assert len(rows) == 4 and len(rows[0]["cells"]) == len(fr.LEADS_H)


# ------------------------------------------------------------------ anomaly

def test_anomaly_flags_a_real_outlier_only():
    random.seed(1)
    history = [(3, {"air_temp_c": 20 + random.uniform(-0.5, 0.5)}) for _ in range(10)]
    base = anomaly.baseline(history)
    flags = anomaly.check([("now", 3, {"air_temp_c": 29.0}),
                           ("then", 3, {"air_temp_c": 20.3})], base)
    assert [f["when"] for f in flags] == ["now"]
    assert flags[0]["direction"] == "higher"


def test_anomaly_ignores_tiny_gaps_even_when_the_spread_is_tiny():
    base = anomaly.baseline([(5, {"humidity_pct": 80.0}) for _ in range(8)])
    assert anomaly.check([("x", 5, {"humidity_pct": 82.0})], base) == []


def test_anomaly_needs_enough_days_of_history():
    base = anomaly.baseline([(5, {"air_temp_c": 20.0}) for _ in range(3)])
    assert anomaly.check([("x", 5, {"air_temp_c": 40.0})], base) == []


# ------------------------------------------------------------------ parsing

def test_parse_hourly_and_ensemble():
    payload = {"hourly": {
        "time": ["2026-10-22T00:00", "2026-10-22T01:00"],
        "temperature_2m": [20.0, 19.5], "relative_humidity_2m": [90, 95],
        "dew_point_2m": [18.2, 18.6],
        "temperature_2m_member01": [21.0, 20.0], "relative_humidity_2m_member01": [80, 85],
        "dew_point_2m_member01": [17.0, 17.5]}}
    rows = sources.parse_hourly(payload)
    assert rows[0]["time"] == datetime(2026, 10, 22, 0, tzinfo=timezone.utc)
    assert rows[1]["temp_c"] == 19.5 and rows[0]["precip_mm"] is None
    members = sources.parse_ensemble(payload)
    assert len(members) == 2 and members[1][0]["temp_c"] == 21.0


def test_parse_power_drops_missing_values_and_converts_sunlight():
    payload = {"parameters": {"ALLSKY_SFC_SW_DWN": {"units": "MJ/m^2/day"}},
               "properties": {"parameter": {
                   "T2M": {"20261020": 25.1, "20261021": -999.0},
                   "ALLSKY_SFC_SW_DWN": {"20261020": 18.0, "20261021": -999.0}}}}
    rows = sources.parse_power(payload)
    assert rows[0] == {"date": "2026-10-20", "t2m_c": 25.1, "rh_pct": None, "solar_kwh_m2": 5.0}
    assert rows[1]["t2m_c"] is None and rows[1]["solar_kwh_m2"] is None
    # Another community answers in kWh already: left alone.
    payload["parameters"]["ALLSKY_SFC_SW_DWN"]["units"] = "kW-hr/m^2/day"
    assert sources.parse_power(payload)[0]["solar_kwh_m2"] == 18.0
    assert "PRECTOTCORR" not in sources.POWER_PARAMS          # rain comes from ERA5


def test_parse_rain_keeps_hours_with_values():
    payload = {"hourly": {"time": ["2026-10-16T22:00", "2026-10-16T23:00", "2026-10-17T00:00"],
                          "precipitation": [0.4, 1.2, None]}}
    rows = sources.parse_rain(payload)
    assert [r["rain_mm"] for r in rows] == [0.4, 1.2]
    assert rows[1]["time"] == datetime(2026, 10, 16, 23, tzinfo=timezone.utc)


CAL_XML = b"""<calibration><calibrationVectorList count="2">
  <calibrationVector><line>0</line><pixel count="3">0 100 200</pixel>
    <sigmaNought>600 650 700</sigmaNought><gamma>500 550 600</gamma></calibrationVector>
  <calibrationVector><line>1000</line><pixel count="3">0 100 200</pixel>
    <sigmaNought>620 670 720</sigmaNought><gamma>520 570 620</gamma></calibrationVector>
</calibrationVectorList></calibration>"""


def test_annotation_lut_interpolates_along_pixels_then_lines():
    assert sources.annotation_lut(CAL_XML, 0, 50, "calibrationVector", "gamma") == 525.0
    assert sources.annotation_lut(CAL_XML, 500, 50, "calibrationVector", "gamma") == 535.0
    assert sources.annotation_lut(CAL_XML, 5000, 300, "calibrationVector", "gamma") == 620.0
    with pytest.raises(ValueError):
        sources.annotation_lut(CAL_XML, 0, 0, "calibrationVector", "betaNought")


def test_noise_lut_reads_new_and_old_layouts_and_defaults_to_zero():
    new = b"<noise><noiseRangeVector><line>0</line><pixel>0 10</pixel><noiseRangeLut>100 200</noiseRangeLut></noiseRangeVector></noise>"
    old = b"<noise><noiseVector><line>0</line><pixel>0 10</pixel><noiseLut>40 40</noiseLut></noiseVector></noise>"
    assert sources.noise_lut(new, 0, 5) == 150.0
    assert sources.noise_lut(old, 0, 5) == 40.0
    assert sources.noise_lut(b"<noise/>", 0, 5) == 0.0


def test_radar_stats_calibrates_removes_noise_and_scores_vegetation():
    vv = np.full((3, 3), 500.0)          # DN^2 250000, gamma 500 -> 1.0 -> 0 dB
    vh = np.full((3, 3), 200.0)          # DN^2 40000 -> 0.16 -> -7.96 dB
    s = sources.radar_stats(vv, vh, 500.0, 500.0)
    assert (s["vv_db"], s["vh_db"], s["valid_px"]) == (0.0, -7.96, 9)
    assert s["rvi"] == round(4 * 0.16 / 1.16, 3)
    # Noise comes off before calibration.
    assert sources.radar_stats(vv, vh, 500.0, 500.0, noise_vh=15000.0)["vh_db"] == -10.0
    # Below the noise floor there is no VH, so no index rather than a made-up one.
    under = sources.radar_stats(vv, vh, 500.0, 500.0, noise_vh=50000.0)
    assert under["vh_db"] is None and under["rvi"] is None
    vh[0, 0] = 0                         # no-data pixel is left out
    assert sources.radar_stats(vv, vh, 500.0, 500.0)["valid_px"] == 8


# -------------------------------------------------- disease, or just stress?

def day(n):
    return datetime(2026, 10, 1, 5, 41, tzinfo=timezone.utc) + timedelta(days=n)


def test_canopy_change_measures_against_the_greenest_recent_pass():
    from src.ml import attribution as at
    assert at.canopy_change([(day(0), 0.6)]) is None
    c = at.canopy_change([(day(0), 0.70), (day(5), 0.75), (day(10), 0.64)])
    assert (c["ndvi_from"], c["delta"], c["trend"]) == (0.75, -0.11, "falling")
    # The peak 30 days back is out of the window: compare with the last pass.
    c = at.canopy_change([(day(0), 0.90), (day(30), 0.60), (day(33), 0.62)])
    assert (c["ndvi_from"], c["trend"]) == (0.60, "steady")
    assert at.canopy_change([(day(0), 0.5), (day(5), 0.6)])["trend"] == "growing"


@pytest.mark.parametrize("trend,high,rising,covered,state", [
    ("falling", 2, 0, 10, "check"),
    ("falling", 0, 2, 10, "check"),
    ("falling", 0, 1, 10, "stress"),
    ("falling", 0, 0, 3, "gap"),
    ("growing", 3, 0, 10, "watch"),
    ("steady", 0, 4, 10, "watch"),
    ("steady", 0, 1, 10, "clear"),
])
def test_verdict_needs_both_signals_to_point_at_disease(trend, high, rising, covered, state):
    from src.ml import attribution as at
    weather = {"days": 10, "covered": covered, "high": high, "rising": rising}
    assert at.verdict({"trend": trend}, weather)["state"] == state
    assert at.verdict(None, None)["state"] == "waiting"


def test_weather_between_counts_each_days_worst_disease():
    from src.ml import attribution as at
    d0 = day(0).date()
    levels = {d0: {"levels": {"a": "low", "b": "high"}},
              d0 + timedelta(days=1): {"levels": {"a": "rising", "b": "low"}},
              d0 + timedelta(days=9): {"levels": {"a": "high"}}}           # outside
    w = at.weather_between(levels, d0, d0 + timedelta(days=3))
    assert w == {"days": 4, "covered": 2, "high": 1, "rising": 1}


def test_ndvi_masks_cloud_and_applies_the_offset():
    red = np.full((3, 3), 1000)
    nir = np.full((3, 3), 4000)
    scl = np.array([[4, 4, 4], [4, 9, 9], [5, 5, 3]])     # 9 cloud, 3 shadow
    s = sources.ndvi_stats(red, nir, scl)
    assert (s["valid_px"], s["total_px"], s["ndvi"]) == (6, 9, 0.6)
    s = sources.ndvi_stats(red + 1000, nir + 1000, scl, offset=1000)
    assert s["ndvi"] == 0.6
    assert sources.ndvi_stats(red, nir, np.full((3, 3), 9))["ndvi"] is None


# ------------------------------------------------------------- the runner

def seed(db, days=16):
    random.seed(7)
    start = datetime.now(timezone.utc).replace(tzinfo=None, minute=0, second=0, microsecond=0) - timedelta(days=days)
    rows = []
    for m in range(0, days * 24 * 60, 5):
        ts = start + timedelta(minutes=m)
        lh = (ts.hour + ts.minute / 60.0 + 5.5) % 24
        air = 26.0 + 6.0 * math.cos((lh - 15) / 24 * 2 * math.pi)
        rh = max(35.0, min(99.0, 70.0 - 25.0 * math.cos((lh - 15) / 24 * 2 * math.pi)))
        dew = air - (100 - rh) / 5.0
        rows.append({"device_id": "box", "ts": ts, "air_temp_c": round(air, 2),
                     "humidity_pct": round(rh, 2), "dew_point_c": round(dew, 2),
                     "leaf_wetness": 100.0 if air - 1.0 <= dew else 0.0})
    db.readings.insert_many(rows)
    return start


@pytest.fixture
def fake(monkeypatch):
    db = mongomock.MongoClient()["agrisync"]
    monkeypatch.setattr(ddata, "_db", lambda: db)
    monkeypatch.setattr(uf, "db", lambda: db)
    start = seed(db)
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def fc(hours_ahead, temp, rh, dew):
        return [{"time": now - timedelta(hours=9) + timedelta(hours=i), "temp_c": temp,
                 "rh_pct": rh, "dew_c": dew, "precip_mm": 0.0} for i in range(96)]

    monkeypatch.setattr(sources, "forecast", lambda lat, lon, **kw: fc(96, 18.0, 98, 17.8))
    monkeypatch.setattr(sources, "ensemble", lambda lat, lon, **kw: [fc(96, 18.0, 98, 17.8),
                                                                     fc(96, 30.0, 40, 15.0)])

    def hourly_readings():
        return [doc for doc in db.readings.find({"ts": {"$gte": start}}) if doc["ts"].minute == 0]

    def sun(ts):
        lh = (ts.hour + 5.5) % 24
        return max(0.0, 700.0 * math.sin((lh - 6) / 12 * math.pi)) if 6 <= lh <= 18 else 0.0

    monkeypatch.setattr(sources, "previous_runs", lambda lat, lon, s, e, day=1: [
        {"time": doc["ts"].replace(tzinfo=timezone.utc), "temp_c": doc["air_temp_c"] - 0.8,
         "dew_c": doc["dew_point_c"] - 1.0, "rh_pct": doc["humidity_pct"] - 4, "cloud_pct": 30.0,
         "wind_ms": 2.0, "sw_wm2": sun(doc["ts"]), "precip_mm": 0.0} for doc in hourly_readings()])
    monkeypatch.setattr(sources, "era5_drivers", lambda lat, lon, s, e: [
        {"time": doc["ts"].replace(tzinfo=timezone.utc), "precip_mm": 0.0, "wind_ms": 2.0,
         "cloud_pct": 30.0, "sw_wm2": sun(doc["ts"])} for doc in hourly_readings()])

    def history(lat, lon, s, e):
        t0 = datetime.combine(s, datetime.min.time(), tzinfo=timezone.utc)
        out = []
        for i in range(24 * 75):
            ts = t0 + timedelta(hours=i)
            lh = (ts.hour + 5.5) % 24
            air = 26.0 + 6.0 * math.cos((lh - 15) / 24 * 2 * math.pi)
            out.append({"time": ts, "temp_c": air, "rh_pct": 80.0, "dew_c": air - 3.0 + (2.5 if lh < 6 else 0),
                        "precip_mm": 0.0, "wind_ms": 1.5, "cloud_pct": 20.0, "sw_wm2": sun(ts.replace(tzinfo=None))})
        return out
    monkeypatch.setattr(sources, "era5_history", history)
    monkeypatch.setattr(uf, "CLIMATE_YEARS", 2)

    def era(lat, lon, s, e):
        out = []
        for doc in db.readings.find({"ts": {"$gte": start}}):
            if doc["ts"].minute == 0:
                out.append({"time": doc["ts"].replace(tzinfo=timezone.utc),
                            "temp_c": doc["air_temp_c"] - 0.5, "rh_pct": doc["humidity_pct"],
                            "dew_c": doc["dew_point_c"], "precip_mm": 0.0})
        return out, {"grid_lat": 28.7, "grid_lon": 77.8, "elevation_m": 212}

    monkeypatch.setattr(sources, "era5_land", era)
    monkeypatch.setattr(sources, "era5_rain", lambda lat, lon, s, e: [
        {"time": r["time"], "rain_mm": 0.5} for r in era(lat, lon, s, e)[0]])
    monkeypatch.setattr(sources, "nasa_power", lambda lat, lon, s, e: [
        {"date": "2026-10-20", "t2m_c": 25.0, "rh_pct": 70, "solar_kwh_m2": 5.1}])
    # Sentinel-1: three passes, each found as two overlapping slices.
    monkeypatch.setattr(sources, "mpc_token", lambda: "tok")
    monkeypatch.setattr(sources, "s1_items", lambda lat, lon, s, e: [
        {"id": f"S1_{d}_{k}", "datetime": f"2026-10-{d:02d}T00:51:00Z", "orbit": "descending",
         "track": 136, "abs_orbit": 5000 + d, "platform": "sentinel-1d"}
        for d in (4, 9, 15) for k in ("a", "b")])

    def s1(item, lat, lon, radius, token):
        assert token == "tok"
        return {"vv_db": -8.0, "vh_db": -15.0, "ratio_db": -7.0, "rvi": 0.5 + item["abs_orbit"] % 10 / 100,
                "valid_px": 9, "total_px": 9, "scene": item["id"], "acquired": item["datetime"],
                "orbit": item["orbit"], "track": item["track"], "abs_orbit": item["abs_orbit"],
                "platform": item["platform"], "row": 1, "col": 1, "radius_m": radius}
    monkeypatch.setattr(sources, "s1_backscatter", s1)
    monkeypatch.setattr(sources, "s2_items", lambda lat, lon, s, e: [
        {"id": f"S2_{d}", "datetime": f"2026-10-{d:02d}T05:41:00Z", "cloud_cover": 1.0}
        for d in (5, 10, 20)])

    def ndvi(item, lat, lon, radius):
        cloudy = item["id"] == "S2_10"
        return {"valid_px": 1 if cloudy else 9, "total_px": 9, "ndvi": 0.71,
                "ndvi_median": 0.7, "scene": item["id"], "cloud_cover": 1.0,
                "acquired": item["datetime"], "radius_m": radius}
    monkeypatch.setattr(sources, "s2_ndvi", ndvi)
    return db


def test_runner_fills_every_feed_and_the_dashboard_reads_them(fake):
    assert uf.main(["x", "all"]) == 0
    status = fake.feeds.find_one({"_id": "status"})
    assert all("error" not in status[k] for k in uf.JOBS)

    f = ddata.forecast_risk()
    assert f["available"] and f["members"] == 2 and len(f["rows"]) == 4
    cell = f["rows"][0]["cells"][-1]
    assert cell["ens_n"] == 2 and cell["level"] in ("low", "rising", "high")
    assert len(f["chart"]["labels"]) == 72

    c = ddata.comparison()
    assert c["available"] and c["stats"]["temp"]["bias"] == 0.5
    assert c["stats"]["temp"]["r"] > 0.99

    assert c["rain"]["total"] > 0 and c["rain"]["wet_days"] >= 1       # 12 mm a full day
    assert c["sun"] == 5.1

    s = ddata.satellite()
    assert s["available"] and s["clear"] == 2 and s["passes"] == 3     # cloudy one dropped
    assert s["latest"]["ndvi"] == 0.71

    r = ddata.radar()
    assert r["available"] and r["passes"] == 3                         # slices merged
    assert len(r["chart"]["morning"]) == 3 and r["chart"]["evening"] == []
    assert [p["y"] for p in r["chart"]["trend"]] == [0.55, 0.55, 0.55]  # median of 0.54, 0.59, 0.55
    assert ddata.optical_gaps() == 2        # the 9th and 15th had no clear view near them

    chk = ddata.disease_or_stress()
    assert chk["available"] and chk["canopy"]["trend"] == "steady"
    assert chk["state"] in ("clear", "watch")

    a = ddata.anomalies()
    assert a["available"] and a["baseline_days"] >= 14 and a["flags"] == []


def test_centred_hours_straddle_the_stamp():
    db = mongomock.MongoClient()["x"]
    base = datetime(2026, 10, 1, 10, 0)
    for m, t in ((20, 20.0), (40, 22.0), (70, 24.0)):
        db.readings.insert_one({"ts": base + timedelta(minutes=m), "air_temp_c": t})
    since = datetime(2026, 10, 1, tzinfo=timezone.utc)
    h = (lambda hh: datetime(2026, 10, 1, hh, tzinfo=timezone.utc))
    floor = uf.station_hours(db, since)
    assert floor[h(10)]["temp"] == 21.0 and floor[h(11)]["temp"] == 24.0
    centred = uf.station_hours(db, since, centred=True)
    assert centred[h(10)]["temp"] == 20.0 and centred[h(11)]["temp"] == 23.0


def test_auto_skips_feeds_that_are_fresh(fake, monkeypatch):
    uf.main(["x", "all"])
    ran = []
    for name in uf.JOBS:
        monkeypatch.setitem(uf.JOBS, name, lambda d, n=name: ran.append(n))
    uf.main(["x", "auto"])
    hourly = [n for n, age in uf.MAX_AGE_H.items() if age == 0]
    assert "anomaly" in hourly and "forecast" not in hourly
    assert ran == hourly


def test_one_failing_feed_does_not_stop_the_others(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no internet")
    monkeypatch.setattr(sources, "ensemble", boom)
    uf.main(["x", "all"])
    status = fake.feeds.find_one({"_id": "status"})
    assert "no internet" in status["forecast"]["error"]
    assert "error" not in status["satellite"]
    assert ddata.satellite()["available"]


def test_page_shows_the_new_sections(fake):
    uf.main(["x", "all"])
    from dashboard.app import create_app
    body = create_app().test_client().get("/").data
    for text in (b'<span class="caps">Today\'s</span> <span class="ital">odds.</span>', b"c-odds",
                 b'id="season-strip"', b"c-ndvi", b"c-rvi",
                 b'<span class="caps">The field,</span> <span class="ital">from space.</span>', b"c-cmp-t",
                 b"Disease, or just stress?", b"NASA POWER", b"Already in the leaves?",
                 b'<span class="caps">The numbers,</span> <span class="ital">checked.</span>',
                 b"not a prediction"):
        assert text in body, text
    assert b"built-in method" not in body          # a dict method leaking into the page


def test_data_json_says_how_fresh_each_feed_is(fake, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret detail http://10.0.0.1/x")
    monkeypatch.setattr(sources, "s1_items", boom)
    uf.main(["x", "all"])
    from dashboard.app import create_app
    out = create_app().test_client().get("/data.json").get_json()
    f = out["feeds"]
    assert set(f) == set(ddata.FEED_NAMES)
    assert f["forecast"]["hours_since_ok"] < 0.1 and f["forecast"]["failing"] is False
    assert f["radar"] == {"hours_since_ok": None, "failing": True}
    assert b"secret detail" not in create_app().test_client().get("/data.json").data


def test_page_still_renders_before_any_feed_has_run(fake):
    from dashboard.app import create_app
    body = create_app().test_client().get("/").data
    assert b"The odds have not run yet." in body
    assert b"No satellite passes processed yet." in body
    assert b"The comparison has not run yet." in body
