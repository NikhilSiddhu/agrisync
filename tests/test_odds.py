"""The odds engine: leaf wetness, infection, the Monte Carlo, calibration,
the field correction, and the runner end to end. No network."""
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import alerts
from src.ml import leafwet, mos, odds
from src.ml import pathogens as pz

H = 12


def night(t=20.0, rh=95.0, wind=0.5, cloud=0.0, sw=0.0, rain=0.0, hours=H):
    return leafwet.simulate(np.full(hours, t), np.full(hours, rh), np.full(hours, wind),
                            np.full(hours, cloud), np.full(hours, sw), np.full(hours, rain))


# ---------------------------------------------------------------- leaf wetness

def test_clear_still_humid_night_wets_the_leaf_and_a_cloudy_breezy_one_does_not():
    assert night()["wet"].sum() >= 10
    assert night(wind=4.0, cloud=100.0)["wet"].sum() == 0


def test_dew_needs_less_humidity_under_a_clear_sky_than_the_dew_point_rule_says():
    # 85% at 20 C is 2.6 C above the dew point: the rule calls it dry.
    assert not leafwet.proxy_wet(20.0, float(leafwet.dew_from_rh(20.0, 85.0)))
    assert night(rh=85.0)["wet"].sum() > 0          # radiative cooling wets it anyway
    assert night(rh=80.0)["wet"].sum() == 0


def test_more_cloud_means_less_dew():
    water = [night(cloud=c)["water"][0].max() for c in (0, 40, 80)]
    assert water[0] > water[1] > water[2]


def test_sun_dries_and_rain_wets():
    assert night(t=30.0, rh=70.0, wind=2.0, cloud=10.0, sw=600.0)["wet"].sum() == 0
    r = night(t=22.0, rh=95.0, wind=2.0, cloud=100.0, rain=2.0)
    assert r["wet"].all() and (r["source"] == 2).all()


def test_dew_and_humidity_convert_both_ways():
    d = leafwet.dew_from_rh(25.0, 70.0)
    assert abs(float(leafwet.rh_from_dew(25.0, d)) - 70.0) < 0.5


def test_clear_sky_sun_is_zero_at_night_and_strong_at_noon():
    noon = datetime(2026, 10, 22, 6, 30, tzinfo=timezone.utc)       # about noon IST
    midnight = datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)
    sw = leafwet.clear_sky_sw([noon, midnight], 28.7, 77.8)
    assert sw[0] > 600 and sw[1] == 0


# ---------------------------------------------------------------- pathogens

def test_temperature_response_peaks_at_the_optimum():
    p = pz.BY_KEY["alternaria_brassicae"]
    assert float(p.f_temp(p.t_opt)) == pytest.approx(1.0)
    assert float(p.f_temp(p.t_min)) == 0.0 and float(p.f_temp(p.t_max)) == 0.0
    assert float(p.w_needed(p.t_opt)) == pytest.approx(p.w_min)
    assert np.isinf(float(p.w_needed(p.t_max + 1)))


def test_every_pathogen_says_where_its_numbers_came_from():
    for p in pz.PATHOGENS:
        assert p.sources and p.t_min < p.t_opt < p.t_max and p.w_min < p.w_max
        assert set(p.stage) == {s[0] for s in pz.STAGES}
        assert set(p.inoculum) == set(range(1, 13))
    assert set(pz.GROUP_NAMES) == set(pz.GROUPS)


def _spell(wet_hours, temp, total=48, start=2):
    flags = np.zeros((1, total), dtype=bool)
    flags[0, start:start + wet_hours] = True
    return flags, np.full((1, total), float(temp))


def test_a_long_spell_at_the_optimum_infects_and_a_short_one_does_not():
    p = pz.BY_KEY["alternaria_brassicae"]
    full = odds.increments(*_spell(int(p.w_max), p.t_opt), p).sum()
    short = odds.increments(*_spell(2, p.t_opt), p).sum()
    assert full == pytest.approx(1.0, abs=0.02)
    assert short < 0.05


def test_too_cold_or_too_hot_does_not_infect():
    p = pz.BY_KEY["alternaria_brassicae"]
    assert odds.increments(*_spell(14, 3.0), p).sum() == 0
    assert odds.increments(*_spell(14, 33.0), p).sum() == 0


def test_short_dry_breaks_are_bridged_long_ones_end_the_spell():
    p = pz.BY_KEY["alternaria_brassicae"]           # dry break 3 h
    flags = np.zeros((1, 30), dtype=bool)
    flags[0, 0:3] = True
    flags[0, 4:7] = True                            # 1 h dry in the middle
    temp = np.full((1, 30), p.t_opt)
    bridged = odds.increments(flags, temp, p).sum()
    flags2 = np.zeros((1, 30), dtype=bool)
    flags2[0, 0:3] = True
    flags2[0, 10:13] = True                         # 7 h dry
    broken = odds.increments(flags2, temp, p).sum()
    assert bridged > 0.4 and broken < 0.15 and bridged > 3 * broken


def test_days_of_rain_count_as_more_than_one_infection():
    p = pz.BY_KEY["alternaria_brassicae"]
    one = odds.increments(*_spell(14, p.t_opt, total=80), p).sum()
    three_days = odds.increments(*_spell(72, p.t_opt, total=80), p).sum()
    assert three_days > 2.5 * one


def test_white_mould_needs_23_unbroken_humid_hours():
    p = pz.BY_KEY["sclerotinia"]
    assert odds.increments(*_spell(20, 15.0), p).sum() < 0.01
    assert odds.increments(*_spell(30, 15.0), p).sum() > 0.4
    assert bool(odds.humid_flags(10.0, 85.0)) and not bool(odds.humid_flags(5.0, 95.0))


# ---------------------------------------------------------------- odds maths

def test_words_and_levels():
    assert odds.words(0.27) == "about 1 in 4"
    assert odds.words(0.5) == "about 1 in 2"
    assert odds.words(0.01) == "under 1 in 25"
    assert odds.level(0.3) == "high" and odds.level(0.12) == "moderate" and odds.level(0.02) == "low"


def test_one_ideal_spell_with_no_disease_on_the_farm_is_about_one_in_ten():
    assert 1 - math.exp(-odds.LAMBDA0) == pytest.approx(0.10, abs=1e-6)
    assert 1 - math.exp(-odds.LAMBDA0 * odds.ON_FARM) == pytest.approx(0.57, abs=0.01)


def test_posterior_starts_at_the_prior_and_follows_the_field():
    prior = odds.posterior([])
    assert prior["median"] == pytest.approx(odds.LAMBDA0, rel=0.05) and not prior["ready"]
    quiet = odds.posterior([(5.0, False)] * 15)
    busy = odds.posterior([(0.5, True)] * 15)
    assert quiet["median"] < prior["median"] < busy["median"]
    mixed = odds.posterior([(2.0, False)] * 18 + [(2.0, True)] * 4)
    assert mixed["ready"] and mixed["hi"] / mixed["lo"] < prior["hi"] / prior["lo"]


def _hours(n, start=datetime(2026, 10, 1, 0, tzinfo=timezone.utc)):
    return [start + timedelta(hours=i) for i in range(n)]


def _drivers(n, wet_nights=True):
    hrs = _hours(n)
    lh = np.array([(h + odds.IST).hour for h in hrs])
    temp = 24.0 + 6.0 * np.cos((lh - 15) / 24 * 2 * np.pi)
    rh = np.where((lh >= 21) | (lh < 7), 97.0 if wet_nights else 60.0, 55.0)
    sw = np.array([max(0.0, 700 * math.sin((h - 6) / 12 * math.pi)) if 6 <= h <= 18 else 0.0 for h in lh])
    return hrs, {"temp": temp, "rh": rh, "wind": np.full(n, 0.8), "cloud": np.full(n, 10.0),
                 "sw": sw, "rain": np.zeros(n)}


def test_season_odds_are_near_zero_when_dry_and_rise_with_wet_nights():
    ctx = {"transplanted": date(2026, 8, 20), "symptoms": {}, "cover": 0.5}
    hrs, dry = _drivers(24 * 10, wet_nights=False)
    base = odds.season(hrs, dry, ctx, n=40)
    assert max(r["p"] for r in odds.season_rows(base, odds.LAMBDA0)) < 0.02
    hrs, wet = _drivers(24 * 10, wet_nights=True)
    base = odds.season(hrs, wet, ctx, n=40)
    rows = odds.season_rows(base, odds.LAMBDA0)
    assert min(r["p"] for r in rows[1:]) > 0.05
    assert all(r["lo"] <= r["p"] <= r["hi"] for r in rows)


def test_symptoms_logged_on_the_farm_raise_the_odds():
    hrs, wet = _drivers(24 * 6)
    plain = odds.season_rows(odds.season(hrs, wet, {"symptoms": {}}, n=30), odds.LAMBDA0)
    logged = odds.season_rows(odds.season(hrs, wet, {"symptoms": {"alternaria": [date(2026, 10, 1)]}}, n=30),
                              odds.LAMBDA0)
    assert logged[-1]["groups"]["alternaria"] > 2 * plain[-1]["groups"]["alternaria"]


def test_incubating_says_when_spots_could_show():
    hrs, wet = _drivers(24 * 8)
    base = odds.season(hrs, wet, {"transplanted": date(2026, 8, 20), "symptoms": {}}, n=30)
    now = hrs[-1]
    inc = odds.incubating(base, odds.LAMBDA0, now)
    alt = next(e for e in inc if e["group"] == "alternaria")
    assert alt["p"] > 0.1
    assert date.fromisoformat(alt["show_from"]) >= (now + odds.IST).date()


def _member(hours, rh_night):
    lh = np.array([(h + odds.IST).hour for h in hours])
    temp = 24.0 + 6.0 * np.cos((lh - 15) / 24 * 2 * np.pi)
    return {"hours": hours, "temp": temp, "rh": np.where((lh >= 21) | (lh < 7), rh_night, 55.0),
            "wind": np.full(len(hours), 0.8), "cloud": np.full(len(hours), 10.0),
            "sw": np.zeros(len(hours)), "rain": np.zeros(len(hours))}


def test_forecast_odds_grow_with_the_window_and_with_wetter_runs():
    hrs, past = _drivers(odds.SPIN_UP_H)
    fut = [hrs[-1] + timedelta(hours=1 + i) for i in range(72)]
    ctx = {"transplanted": date(2026, 8, 20), "symptoms": {}}
    wet = odds.forecast(hrs, past, [_member(fut, 98.0)] * 3, ctx, n_param=10, seed=1)
    dry = odds.forecast(hrs, past, [_member(fut, 60.0)] * 3, ctx, n_param=10, seed=1)
    ps = [w["p"] for w in wet["windows"]]
    assert ps == sorted(ps) and ps[0] > 0.05
    assert dry["windows"][0]["p"] < wet["windows"][0]["p"] / 3
    again = odds.forecast(hrs, past, [_member(fut, 98.0)] * 3, ctx, n_param=10, seed=1)
    assert again["windows"] == wet["windows"]                       # seeded: repeatable
    assert len(wet["hourly"]) == 72 and wet["spell"]["hours"] >= 6
    assert odds.drivers_text(wet, dict(ctx, stage="head forming"), "alternaria")[0].startswith("Leaves wet")


# ---------------------------------------------------------------- measured wetness (Step 41a)

def _night_mask(hrs):
    lh = np.array([(h + odds.IST).hour for h in hrs])
    return (lh >= 21) | (lh < 7)


def test_a_wet_night_from_the_sensor_raises_the_24h_odds():
    # Same weather both times, dry enough that the leaf model keeps it dry.
    ctx = {"transplanted": date(2026, 8, 20), "symptoms": {}}
    hrs, weather = _drivers(24 * 4, wet_nights=False)
    night = _night_mask(hrs)
    wet = odds.season_rows(odds.season(hrs, dict(weather, wet_obs=np.where(night, 1.0, 0.0)), ctx, n=40),
                           odds.LAMBDA0)
    dry = odds.season_rows(odds.season(hrs, dict(weather, wet_obs=np.zeros(len(hrs))), ctx, n=40),
                           odds.LAMBDA0)
    assert [r["date"] for r in wet] == [r["date"] for r in dry]
    for w, d in zip(wet, dry):
        assert d["p"] < 0.02 and w["p"] > 0.05 and w["p"] > 3 * d["p"]
        assert w["wet_h"] == 10.0 and d["wet_h"] == 0.0     # 21:00-07:00 local, from the sensor

    # The next 24 h: a sensor-wet spell still running at "now" carries into
    # the forecast, where the leaf model wets the dawn hours of each run.
    hrs, past = _drivers(odds.SPIN_UP_H, wet_nights=False)
    fut = [hrs[-1] + timedelta(hours=1 + i) for i in range(72)]
    members = [_member(fut, 98.0)] * 3
    obs_wet = np.full(len(hrs), np.nan)
    obs_wet[-3:] = 1.0
    obs_dry = np.where(np.isfinite(obs_wet), 0.0, np.nan)
    a = odds.forecast(hrs, dict(past, wet_obs=obs_wet), members, ctx, n_param=10, seed=1)
    b = odds.forecast(hrs, dict(past, wet_obs=obs_dry), members, ctx, n_param=10, seed=1)
    assert a["windows"][0]["p"] > b["windows"][0]["p"] + 0.01
    # Ahead stays simulated: the hour-by-hour wetness of the runs is untouched.
    assert [h["wet"] for h in a["hourly"]] == [h["wet"] for h in b["hourly"]]


def test_all_missing_sensor_hours_change_nothing():
    ctx = {"transplanted": date(2026, 8, 20), "symptoms": {}, "cover": 0.5}
    hrs, wet = _drivers(24 * 4)
    plain = odds.season_rows(odds.season(hrs, wet, ctx, n=30), odds.LAMBDA0)
    nan = odds.season_rows(odds.season(hrs, dict(wet, wet_obs=np.full(len(hrs), np.nan)), ctx, n=30),
                           odds.LAMBDA0)
    assert nan == plain

    hrs, past = _drivers(odds.SPIN_UP_H)
    fut = [hrs[-1] + timedelta(hours=1 + i) for i in range(72)]
    members = [_member(fut, 98.0)] * 3
    a = odds.forecast(hrs, past, members, ctx, n_param=10, seed=2)
    b = odds.forecast(hrs, dict(past, wet_obs=np.full(len(hrs), np.nan)), members, ctx, n_param=10, seed=2)
    assert a == b


def test_partial_sensor_coverage_overrides_only_the_hours_it_saw():
    hrs, weather = _drivers(48)
    worlds = odds.draw(25, np.random.default_rng(5))
    sim = odds.run(weather, worlds, 0.5)
    obs = np.full(len(hrs), np.nan)
    obs[5:9] = 1.0          # sensor says wet
    obs[30:34] = 0.0        # sensor says dry
    res = odds.run(dict(weather, wet_obs=obs), worlds, 0.5)
    seen = np.isfinite(obs)
    assert res["wet"].shape == sim["wet"].shape
    assert res["wet"][:, 5:9].all() and not res["wet"][:, 30:34].any()
    assert np.array_equal(res["wet"][:, ~seen], sim["wet"][:, ~seen])
    # White mould keeps its humid rule whatever the sensor says.
    assert np.array_equal(res["inc"]["sclerotinia"], sim["inc"]["sclerotinia"])
    assert odds.observed_wet(sim["wet"], None) is sim["wet"]


def test_a_sensor_hour_is_wet_from_half_its_readings():
    from src.ml import wetness
    assert wetness.sensor_hour_wet(0, 0) is None
    assert wetness.sensor_hour_wet(1, 2) == 1.0          # exactly half counts as wet
    assert wetness.sensor_hour_wet(5, 12) == 0.0
    assert wetness.sensor_hour_wet(12, 12) == 1.0


def _sensor_readings():
    import mongomock
    db = mongomock.MongoClient()["x"]
    base = datetime(2026, 10, 1, 18, 0)                  # naive UTC, as pymongo returns it
    hour = {0: (True, True, False, False),               # exactly half wet -> wet
            1: (True, False, False, False),              # a quarter -> dry
            2: (None,) * 4}                              # no sensor value -> missing
    for h, flags in hour.items():
        for k, f in enumerate(flags):
            doc = {"ts": base + timedelta(hours=h, minutes=15 * k), "air_temp_c": 18.0,
                   "humidity_pct": 95.0, "dew_point_c": 17.2, "leaf_wetness": 100.0}
            if f is not None:
                doc["leaf_wet_sensor"] = f
            db.readings.insert_one(doc)
    # One lone sensor reading in hour 3 still counts; hour 4 has nothing.
    db.readings.insert_one({"ts": base + timedelta(hours=3, minutes=10), "air_temp_c": 18.0,
                            "leaf_wetness": 0.0, "leaf_wet_sensor": True})
    # A flagged reading never votes (it would make hour 1 exactly half wet).
    db.readings.insert_one({"ts": base + timedelta(hours=1, minutes=50), "leaf_wet_sensor": True,
                            "quality_flags": ["air_temp_range"]})
    return db, base.replace(tzinfo=timezone.utc)


def test_station_hours_score_the_sensor_by_half_its_readings():
    from scripts import update_feeds as uf
    db, t0 = _sensor_readings()
    st = uf.station_hours(db, t0, t0 + timedelta(hours=5))
    h = (lambda k: t0 + timedelta(hours=k))
    assert st[h(0)]["wet_obs"] == 1.0
    assert st[h(1)]["wet_obs"] == 0.0
    assert st[h(2)]["wet_obs"] is None
    assert st[h(3)]["wet_obs"] == 1.0
    assert h(4) not in st
    # The proxy keeps its own meaning, beside the sensor.
    assert st[h(1)]["wet"] == 1.0 and st[h(3)]["wet"] == 0.0


def test_odds_inputs_pass_the_sensor_as_wet_obs():
    from scripts import update_feeds as uf
    db, t0 = _sensor_readings()
    inp = uf.odds_inputs(db, t0, t0 + timedelta(hours=5), 28.7, 77.8)
    obs = inp["drivers"]["wet_obs"]
    assert obs.shape == (len(inp["hours"]),) == (5,)
    assert np.array_equal(obs, np.array([1.0, 0.0, np.nan, 1.0, np.nan]), equal_nan=True)


# ---------------------------------------------------------------- field correction

def test_correction_learns_a_cool_damp_night_and_is_used():
    rng = np.random.default_rng(3)
    n = 24 * 40
    lh = np.arange(n) % 24
    fc = 25 + 5 * np.cos((lh - 15) / 24 * 2 * np.pi) + rng.normal(0, 0.3, n)
    bias = np.where((lh >= 21) | (lh < 6), -2.5, 0.5)
    obs = fc + bias + rng.normal(0, 0.4, n)
    days = np.arange(n) / 24.0
    m = mos.fit(lh, fc, obs, np.full(n, 20.0), np.full(n, 2.0), days)
    assert m["use"] and m["rmse_corrected"] < 0.5 * m["rmse_raw"]
    out = mos.apply(m, lh[:24], fc[:24], np.full(24, 20.0), np.full(24, 2.0))
    assert abs(float(np.mean(out - fc[:24] - bias[:24]))) < 0.3


def test_correction_stays_off_when_there_is_nothing_to_learn():
    rng = np.random.default_rng(4)
    n = 24 * 30
    lh = np.arange(n) % 24
    fc = rng.normal(25, 3, n)
    obs = fc + rng.normal(0, 1.0, n)
    m = mos.fit(lh, fc, obs, np.full(n, 20.0), np.full(n, 2.0), np.arange(n) / 24.0)
    assert not m["use"]
    assert np.array_equal(mos.apply(m, lh, fc, 0, 0), fc)
    assert mos.fit(lh[:50], fc[:50], obs[:50], 0, 0)["state"] == "learning"


def test_nowcast_fades():
    out = mos.blend_now(np.zeros(4), np.array([0, 6, 12, 48]), 2.0)
    assert out[0] == pytest.approx(2.0) and out[1] == pytest.approx(2 * math.exp(-1))
    assert out[3] < 0.01


# ---------------------------------------------------------------- alerts

def _odds_doc(p=0.4, high=40, members=51, age_h=0.5, now=datetime(2026, 10, 22, 6, tzinfo=timezone.utc)):
    return {"generated_at": now - timedelta(hours=age_h),
            "windows": [{"hours": 24, "p": p, "lo": p / 2, "hi": p * 1.5, "level": odds.level(p), "members_high": high},
                        {"hours": 48, "p": p, "lo": p / 2, "hi": p * 1.5, "level": odds.level(p), "members_high": high}],
            "inputs": {"members": members}, "top_name": "Alternaria leaf spot",
            "spell": {"from": now, "to": now + timedelta(hours=10), "temp": 20.0},
            "calibration": {"checked": False}}


def test_alerts_follow_the_odds_when_they_are_fresh():
    now = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
    r = alerts.evaluate(None, {}, None, now, _odds_doc(now=now))
    assert len(r["send"]) == 1 and "48 h: 40%" in r["send"][0]["body"]
    assert "walk the crop" in r["send"][0]["body"]
    assert alerts.evaluate(None, {}, None, now, _odds_doc(high=10, now=now))["send"] == []
    assert alerts.evaluate(None, {}, None, now, _odds_doc(p=0.12, now=now))["send"] == []
    again = alerts.evaluate(None, r["state"], None, now + timedelta(hours=2), _odds_doc(now=now + timedelta(hours=2)))
    assert again["send"] == []                                      # cooldown


def test_stale_odds_fall_back_to_the_old_rule():
    now = datetime(2026, 10, 22, 6, tzinfo=timezone.utc)
    r = alerts.evaluate({"rows": []}, {}, None, now, _odds_doc(age_h=6, now=now))
    assert r["send"] == [] and r["state"]["active"] == []


# ---------------------------------------------------------------- end to end

@pytest.fixture
def fake(monkeypatch):
    from tests import test_feeds
    return test_feeds.fake.__wrapped__(monkeypatch)


def test_odds_job_end_to_end(fake):
    from dashboard import data as ddata
    from scripts import update_feeds as uf
    assert uf.main(["x", "all"]) == 0
    status = fake.feeds.find_one({"_id": "status"})
    assert all("error" not in status[k] for k in uf.JOBS), {k: status[k].get("error") for k in uf.JOBS}
    doc = fake.feeds.find_one({"_id": "odds"})
    assert 0 <= doc["now"]["p"] <= 1 and doc["now"]["lo"] <= doc["now"]["p"] <= doc["now"]["hi"]
    assert [w["hours"] for w in doc["windows"]] == [24, 48, 72]
    assert doc["inputs"]["members"] == 2 and doc["calibration"]["checked"] is False
    assert fake.feeds.find_one({"_id": "mos"})["pairs"] > 200
    assert fake.feeds.find_one({"_id": "climate"})["years"] == 2
    s = ddata.summary()
    assert s["odds"]["words"] and "'lat'" not in str(s) and "28.71" not in str(s) and "77.77" not in str(s)
    o = ddata.odds()
    assert o["available"] and len(o["chart"]["p"]) == 72 and o["season"]["points"]
