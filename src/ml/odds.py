"""The odds engine: one number for the chance this crop picks up a disease.

THE QUESTION IT ANSWERS
    "What are the odds that the next 24 hours infect this crop?" One number,
    for all the diseases together, with a range, instead of a score per
    disease and per data source that the reader has to add up in their head.

HOW THE PIECES COMBINE (the disease triangle: weather x crop x spores)

    weather   The station's own hours, gaps filled from the outside feeds and
              shifted to match the field (fill_gaps). Ahead: all 51 runs of
              the ECMWF ensemble, each corrected to this field by the station's
              own record (src/ml/mos.py) and nudged by the station's error
              right now.
    wetness   Not the dew-point rule: an energy balance of the leaf, with
              cloud, wind and sunshine (src/ml/leafwet.py). The satellite's
              view of how closed the canopy is shades the leaf from sky and
              wind.
    disease   For each disease, the wet hours it needs at the temperature of
              the wet spell, from lab and field trials (src/ml/pathogens.py),
              with the generic infection model of Magarey et al. (2005).
    crop      How susceptible the crop is at its stage (days since
              transplanting), per disease.
    spores    A background level per disease and month, from the region's
              season; eight times higher once symptoms are logged on the farm
              (the farm is then its own source), and raised when the satellite
              check finds the canopy falling in disease weather.

    All of it runs as a Monte Carlo: each world is one weather run with one
    set of disease and leaf parameters drawn from their uncertainty. In each
    world the diseases add up to one infection hazard H and the odds of at
    least one infection are 1 - exp(-H). The headline is the average over
    worlds; the range is the 10th to 90th percentile across them.

WHAT THE NUMBER MEANS, AND WHAT IS NOT YET KNOWN
    "The odds that the next 24 hours start an infection you would later see
    as new spots." The scale is one stated assumption: with no disease on
    the farm, one ideal spell for one disease (wet long enough, at its best
    temperature, crop at its most susceptible) gives a 1 in 10 chance; with
    spots already on the farm, about 1 in 2. Over a month of such nights
    that compounds to near certainty, which is what an unsprayed crop in a
    humid north Indian autumn tends to show. Lab thresholds decide WHEN the
    odds rise; nothing yet decides how far on this farm. The field log does
    (posterior): every walk through the crop, healthy ones included, moves
    the scale towards what actually happened. Until 20 walks with at least
    3 outbreaks, the odds are labelled "not yet checked against this field".

WHAT IT CANNOT DO
    It cannot see spores. It cannot see a sprayed crop unless told. The
    leaf wetness is computed, not measured, except on past hours where the
    calibrated wetness sensor reported (drivers["wet_obs"]); ahead it is
    always computed.
"""
from datetime import date, datetime, timedelta, timezone
from math import exp, log

import numpy as np

from src.ml import leafwet
from src.ml import pathogens as pz

IST = timedelta(hours=5, minutes=30)

SPELL_P = 0.10               # one ideal spell, one disease, none on the farm yet
LAMBDA0 = -log(1.0 - SPELL_P)
ON_FARM = 8.0                # spores once symptoms are logged: about 1 in 2 a spell
ON_FARM_DAYS = 28
CANOPY_BOOST = {"check": 2.0}   # attribution: canopy fell in disease weather
PRIOR_SD = 0.8               # on log scale: a factor of about 2.2 either way
LEVELS = ((0.25, "high"), (0.10, "moderate"), (0.0, "low"))
WINDOWS_H = (24, 48, 72)
SPIN_UP_H = 72
MIN_JUDGED, MIN_POSITIVE = 20, 3


def level(p):
    if p is None:
        return None
    for cut, name in LEVELS:
        if p >= cut:
            return name
    return "low"


_FRACTIONS = ((1, 20), (1, 10), (1, 8), (1, 6), (1, 5), (1, 4), (1, 3), (2, 5), (1, 2),
              (3, 5), (2, 3), (3, 4), (4, 5), (9, 10))


def words(p):
    """0.27 -> 'about 1 in 4'."""
    if p is None:
        return None
    if p < 0.035:
        return "under 1 in 25"
    if p > 0.95:
        return "almost certain"
    a, b = min(_FRACTIONS, key=lambda f: abs(f[0] / f[1] - p))
    return f"about {a} in {b}"


# ------------------------------------------------------------ the weather

def local(h):
    return h + IST


def hour_offsets(local_hours, station, outside, min_n=5):
    """Median (station - outside) per local hour, falling back to the overall
    median, then 0. This is how a gap in the station record is filled: the
    outside value for that hour, shifted by the field's usual offset at that
    time of day."""
    lh = np.asarray(local_hours) % 24
    diff = np.asarray(station, dtype=float) - np.asarray(outside, dtype=float)
    ok = np.isfinite(diff)
    overall = float(np.median(diff[ok])) if ok.sum() >= min_n else 0.0
    out = np.full(24, overall)
    for h in range(24):
        m = ok & (lh == h)
        if m.sum() >= min_n:
            out[h] = float(np.median(diff[m]))
    return out


def merge(local_hours, station, outside, offsets):
    station = np.asarray(station, dtype=float)
    outside = np.asarray(outside, dtype=float)
    shifted = outside + offsets[np.asarray(local_hours) % 24]
    measured = np.isfinite(station)
    return np.where(measured, station, shifted), measured


# ------------------------------------------------------------ the diseases

def humid_flags(temp, rh):
    """Sclerotinia's rule: RH 80% or more at 7 C or more (SkleroPro)."""
    return (np.asarray(rh) >= 80.0) & (np.asarray(temp) >= 7.0)


def intensity(w, need, w_max, width=0.5):
    """0..1 infection intensity of a wet spell of w hours that needs `need`."""
    finite = np.isfinite(need)
    need_f = np.where(finite, need, 0.0)
    gate = 1.0 / (1.0 + np.exp(-np.clip((w - need_f) / width, -50, 50)))
    span = np.maximum(w_max - need_f, 2.0)
    sev = 0.5 + 0.5 * np.clip((w - need_f) / span, 0.0, 1.0)
    return np.where(finite & (w > 0), gate * sev, 0.0)


def increments(flags, temp, path, shift=0.0, w_min=None, w_max=None, dry=None, splash=None):
    """Hour-by-hour gain in infection intensity for one pathogen.

    flags: (runs, hours) bool, the pathogen's wet rule met; temp: (runs,
    hours). shift moves the cardinal temperatures (parameter uncertainty).
    One fully developed spell adds up to 1.0 over its hours. splash: (runs,
    hours) 0/1, hours with splashing rain, weighted by path.rain_boost.
    """
    n, hours = flags.shape
    shift = np.broadcast_to(np.asarray(shift, dtype=float), (n,))
    w_min = np.broadcast_to(np.asarray(path.w_min if w_min is None else w_min, dtype=float), (n,))
    w_max = np.broadcast_to(np.asarray(path.w_max if w_max is None else w_max, dtype=float), (n,))
    dry = np.broadcast_to(np.asarray(path.dry_break_h if dry is None else dry, dtype=float), (n,))
    end_after = np.maximum(np.round(dry), 1.0)
    W = np.zeros(n)
    sum_t = np.zeros(n)
    gap = np.zeros(n)
    e_prev = np.zeros(n)
    out = np.zeros((n, hours))
    for h in range(hours):
        f = flags[:, h]
        gap = np.where(f, 0.0, gap + 1.0)
        ended = (~f) & (gap >= end_after)
        W = np.where(ended, 0.0, W)
        sum_t = np.where(ended, 0.0, sum_t)
        e_prev = np.where(ended, 0.0, e_prev)
        W = W + f
        sum_t = sum_t + np.where(f, temp[:, h], 0.0)
        t_mean = np.where(W > 0, sum_t / np.maximum(W, 1.0), temp[:, h])
        need = path.w_needed(t_mean - shift, w_min, w_max)
        e = intensity(W, need, w_max)
        gain = np.maximum(e - e_prev, 0.0)
        if splash is not None and path.rain_boost:
            gain = gain * (1.0 + path.rain_boost * splash[:, h])
        out[:, h] = gain
        e_prev = np.maximum(e, e_prev)
        # A spell that has run its full length has done all one cohort of
        # spores can do; if it stays wet, the next cohort starts from zero.
        # (Without this, three rainy days would count as one infection.)
        done = f & (W >= w_max)
        W = np.where(done, 0.0, W)
        sum_t = np.where(done, 0.0, sum_t)
        e_prev = np.where(done, 0.0, e_prev)
    return out


# ------------------------------------------------------------ the crop and spores

def stage_weights(hours, transplanted):
    """Per pathogen, (hours,) susceptibility from crop stage. Unknown
    transplant date: the average over stages, said so in the output."""
    out = {}
    for p in pz.PATHOGENS:
        if transplanted is None:
            out[p.key] = np.full(len(hours), float(np.mean(list(p.stage.values()))))
            continue
        vals = []
        for h in hours:
            dat = (local(h).date() - transplanted).days
            vals.append(p.stage.get(pz.stage_of(dat), 1.0))
        out[p.key] = np.array(vals, dtype=float)
    return out


def background(hours):
    out = {}
    months = np.array([local(h).month for h in hours])
    for p in pz.PATHOGENS:
        lut = np.array([p.inoculum.get(m, 1.0) for m in range(1, 13)])
        out[p.key] = lut[months - 1]
    return out


def on_farm(hours, symptom_days):
    """Per group, (hours,) multiplier: ON_FARM for ON_FARM_DAYS after a logged
    symptom of that group, else 1."""
    out = {}
    days = np.array([local(h).date().toordinal() for h in hours])
    for g in pz.GROUPS:
        m = np.ones(len(hours))
        for d in symptom_days.get(g, ()):
            o = d.toordinal()
            m = np.where((days >= o) & (days < o + ON_FARM_DAYS), ON_FARM, m)
        out[g] = m
    return out


def multipliers(hours, ctx):
    """Susceptibility x spores, per pathogen, (hours,). Excludes the scale."""
    st = stage_weights(hours, ctx.get("transplanted"))
    bg = background(hours)
    farm = on_farm(hours, ctx.get("symptoms", {}))
    canopy = CANOPY_BOOST.get(ctx.get("canopy_state"), 1.0)
    out = {}
    for p in pz.PATHOGENS:
        m = st[p.key] * bg[p.key] * farm[p.group]
        if p.group in ("alternaria", "black_rot"):
            m = m * canopy
        out[p.key] = np.minimum(m, ON_FARM * 1.5)
    return out


# ------------------------------------------------------------ the Monte Carlo

def draw(n, rng):
    """Parameter sets for n worlds."""
    worlds = {"leaf": leafwet.sample_params(n, rng), "path": {}}
    for p in pz.PATHOGENS:
        w_min = p.w_min * np.exp(rng.normal(0.0, 0.25, n))
        w_max = np.maximum(p.w_max * np.exp(rng.normal(0.0, 0.2, n)), w_min + 2.0)
        worlds["path"][p.key] = {
            "shift": rng.normal(0.0, 1.0, n),
            "w_min": w_min, "w_max": w_max,
            "dry": p.dry_break_h * rng.uniform(0.5, 1.5, n),
            "spores": np.exp(rng.normal(0.0, 0.35, n)),
        }
    return worlds


def run(drivers, worlds, cover=None):
    """Simulate leaf wetness and infection for every world.

    drivers: dict of (runs, hours) or (hours,) arrays: temp, rh, wind, cloud,
    sw, rain. Returns {"wet": (runs, hours) bool, "inc": {key: (runs, hours)}}.

    Optional drivers["wet_obs"], (hours,) or (runs, hours): the leaf wetness
    sensor's verdict per hour, 1 wet, 0 dry, NaN where it did not report.
    Wherever it is finite it replaces the simulated wetness in every world.
    Callers pass NaN for forecast hours, so only observed hours change (see
    forecast). Sclerotinia keeps its own humid rule and does not use it.
    """
    leaf = worlds["leaf"]
    if cover:
        c = float(np.clip(cover, 0, 1))
        leaf = dict(leaf, sky_view=leaf["sky_view"] * (1 - 0.4 * c),
                    wind_factor=leaf["wind_factor"] * (1 - 0.5 * c))
    n = len(leaf["leaf_m"])
    temp = np.broadcast_to(np.atleast_2d(drivers["temp"]), (n, np.shape(drivers["temp"])[-1]))
    rh = np.broadcast_to(np.atleast_2d(drivers["rh"]), temp.shape)
    sim = leafwet.simulate(temp, rh, drivers.get("wind"), drivers.get("cloud"),
                           drivers.get("sw"), drivers.get("rain"), params=leaf)
    wet = observed_wet(sim["wet"], drivers.get("wet_obs"))
    rain = drivers.get("rain")
    splash = None
    if rain is not None:
        splash = (np.broadcast_to(np.nan_to_num(np.atleast_2d(rain)), temp.shape) >= 1.0).astype(float)
    humid = humid_flags(temp, rh)
    inc = {}
    for p in pz.PATHOGENS:
        pp = worlds["path"][p.key]
        flags = humid if p.wet_rule == "humid" else wet
        inc[p.key] = increments(flags, temp, p, pp["shift"], pp["w_min"], pp["w_max"],
                                pp["dry"], splash)
    return {"wet": wet, "inc": inc}


def observed_wet(sim_wet, wet_obs):
    """The simulated wetness, with the sensor's hours put in where it
    reported. sim_wet: (runs, hours) bool; wet_obs: None, (hours,) or (runs,
    hours) of 1/0/NaN. Returns sim_wet itself when nothing is finite."""
    if wet_obs is None:
        return sim_wet
    obs = np.broadcast_to(np.atleast_2d(np.asarray(wet_obs, dtype=float)), sim_wet.shape)
    seen = np.isfinite(obs)
    if not seen.any():
        return sim_wet
    return np.where(seen, np.nan_to_num(obs) >= 0.5, sim_wet)


def hazards(res, mult, worlds, scale, a, b):
    """Per pathogen (runs,) hazard over hour indices [a, b)."""
    out = {}
    for p in pz.PATHOGENS:
        w = res["inc"][p.key][:, a:b] * mult[p.key][None, a:b]
        out[p.key] = scale * worlds["path"][p.key]["spores"] * w.sum(axis=1)
    return out


def combine(haz):
    """-> per-world odds of at least one infection."""
    total = sum(haz.values())
    return 1.0 - np.exp(-total)


def summarise(p_world):
    return {"p": round(float(np.mean(p_world)), 3),
            "lo": round(float(np.percentile(p_world, 10)), 3),
            "hi": round(float(np.percentile(p_world, 90)), 3)}


# ------------------------------------------------------------ calibration

def posterior(pairs, prior_mu=log(LAMBDA0), prior_sd=PRIOR_SD):
    """Scale from field-log outcomes.

    pairs: [(exposure, outcome)] where exposure is the unscaled hazard the
    model put on the days that could have caused what a visit found, and
    outcome True (new symptoms) or False (nothing new). Grid posterior over
    log scale, normal prior. -> {"median", "lo", "hi", "n", "k"}.
    """
    grid = np.linspace(prior_mu - 4 * prior_sd, prior_mu + 4 * prior_sd, 241)
    logp = -0.5 * ((grid - prior_mu) / prior_sd) ** 2
    lam = np.exp(grid)
    k = 0
    for x, y in pairs:
        q = np.clip(1.0 - np.exp(-lam * max(float(x), 0.0)), 1e-9, 1 - 1e-9)
        logp += np.log(q) if y else np.log(1.0 - q)
        k += 1 if y else 0
    w = np.exp(logp - logp.max())
    w /= w.sum()
    cdf = np.cumsum(w)

    def q_at(f):
        return float(np.exp(grid[min(np.searchsorted(cdf, f), len(grid) - 1)]))
    return {"median": q_at(0.5), "lo": q_at(0.05), "hi": q_at(0.95),
            "n": len(pairs), "k": k,
            "ready": len(pairs) >= MIN_JUDGED and k >= MIN_POSITIVE}


def prior_only():
    return posterior([])


# ------------------------------------------------------------ the season

def daily_windows(hours):
    """{day: (a, b)} index ranges of the 24 h from noon IST the day before to
    noon IST on that day, so a night is never split."""
    out = {}
    for i, h in enumerate(hours):
        d = (local(h) + timedelta(hours=12)).date()
        a, b = out.get(d, (i, i))
        out[d] = (min(a, i), i + 1)
    return out


def season(hours, drivers, ctx, n=120, seed=7):
    """Simulate the whole season once. The scale is applied afterwards
    (season_rows), because the field log has to see the unscaled exposure
    before the scale can be estimated.

    hours: list of aware UTC datetimes; drivers: (hours,) arrays, gaps filled.
    """
    rng = np.random.default_rng(seed)
    worlds = draw(n, rng)
    res = run(drivers, worlds, ctx.get("cover"))
    win = daily_windows(hours)
    days = sorted(d for d, (a, b) in win.items() if b - a >= 18)
    mult = multipliers(hours, ctx)
    exposure = {g: {} for g in pz.GROUPS}
    for d in days:
        a, b = win[d]
        haz = hazards(res, mult, worlds, 1.0, a, b)
        for g, keys in pz.GROUPS.items():
            exposure[g][d] = float(np.mean(sum(haz[k] for k in keys)))
    return {"hours": hours, "win": win, "days": days, "exposure": exposure,
            "worlds": worlds, "res": res, "mult": mult,
            "measured": ctx.get("measured"), "temp": np.asarray(drivers["temp"], dtype=float)}


def season_rows(base, scale):
    """Daily odds, noon to noon, for every day of the season."""
    rows = []
    res, mult, worlds, win = base["res"], base["mult"], base["worlds"], base["win"]
    measured = base.get("measured")
    for d in base["days"]:
        a, b = win[d]
        haz = hazards(res, mult, worlds, scale, a, b)
        pw = combine(haz)
        wet_h = res["wet"][:, a:b].sum(axis=1)
        rows.append({
            "date": d.isoformat(), **summarise(pw),
            "wet_h": round(float(np.median(wet_h)), 1),
            "measured": round(float(np.mean(measured[a:b])), 2) if measured is not None else None,
            "groups": {g: round(float(np.mean(1.0 - np.exp(-sum(haz[k] for k in keys)))), 3)
                       for g, keys in pz.GROUPS.items()},
        })
    return rows


def incubating(base, scale, now, horizon_d=10, min_p=0.02):
    """What the last three weeks may still show, per disease group.

    Each day's expected infection odds are carried to the days its symptoms
    could appear (the pathogen's latent period). Per group: the odds that
    something from those days shows between now and `horizon_d` days ahead,
    and the window it would show in. -> [{group, name, p, show_from, show_to,
    infected_from, infected_to}]"""
    res, mult, worlds, win, temps = (base[k] for k in ("res", "mult", "worlds", "win", "temp"))
    today = local(now).date()
    until = today + timedelta(days=horizon_d)
    acc = {}
    for d in base["days"]:
        if (today - d).days > 21 or d > today:
            continue
        a, b = win[d]
        haz = hazards(res, mult, worlds, scale, a, b)
        t_mean = float(np.nanmean(temps[a:b]))
        for g, keys in pz.GROUPS.items():
            p = float(np.mean(1.0 - np.exp(-sum(haz[k] for k in keys))))
            if p < min_p:
                continue
            lo, hi = pz.latent_days(pz.BY_KEY[keys[0]], t_mean)
            start = d + timedelta(days=int(round(lo)))
            end = d + timedelta(days=int(round(hi)))
            if end < today or start > until:
                continue
            # Share of the latent window still ahead of us.
            span = max((end - start).days + 1, 1)
            ahead = (end - max(start, today)).days + 1
            q = p * min(max(ahead / span, 0.0), 1.0)
            e = acc.setdefault(g, {"none": 1.0, "from": None, "to": None, "inf": []})
            e["none"] *= (1.0 - q)
            e["from"] = min(e["from"] or max(start, today), max(start, today))
            e["to"] = max(e["to"] or end, end)
            e["inf"].append(d)
    out = []
    for g, e in acc.items():
        p = 1.0 - e["none"]
        if p < 0.05:
            continue
        out.append({"group": g, "name": pz.GROUP_NAMES[g], "p": round(p, 3),
                    "show_from": e["from"].isoformat(), "show_to": e["to"].isoformat(),
                    "infected_from": min(e["inf"]).isoformat(), "infected_to": max(e["inf"]).isoformat()})
    return sorted(out, key=lambda x: -x["p"])


def calibration_pairs(visits, exposure):
    """Field-log visits -> [(exposure, outcome)] for posterior().

    visits: [{date, outcomes: {group: True/False/None}}]. A visit is judged
    on the unscaled hazard over its incubation window."""
    pairs = []
    for v in visits:
        d = v["date"] if isinstance(v["date"], date) else date.fromisoformat(str(v["date"])[:10])
        for g, y in (v.get("outcomes") or {}).items():
            if y is None or g not in exposure:
                continue
            p = pz.BY_KEY[pz.GROUPS[g][0]]
            lo, hi = pz.latent_days(p, 22.0)
            days = [d - timedelta(days=k) for k in range(int(round(lo)), int(round(hi)) + 1)]
            have = [exposure[g][x] for x in days if x in exposure[g]]
            if len(have) < max(1, len(days) // 2):
                continue
            pairs.append((sum(have), bool(y)))
    return pairs


# ------------------------------------------------------------ the forecast

def forecast(past_hours, past, members, ctx, scale=None, n_param=20, seed=None):
    """Odds for the next 24, 48 and 72 hours.

    past_hours/past: the last SPIN_UP_H hours, (hours,) arrays, filled.
    past may carry "wet_obs" (sensor wetness, NaN where missing); it replaces
    the simulated wetness on those past hours only, never ahead.
    members: list of dicts with "hours" and (hours,) arrays for the coming
    hours, already corrected to the field. Every member is paired with
    n_param parameter draws.
    """
    scale = LAMBDA0 if scale is None else scale
    if not members:
        raise ValueError("no forecast members")
    fut_hours = members[0]["hours"]
    nm = len(members)
    n = nm * n_param
    rng = np.random.default_rng(seed)
    worlds = draw(n, rng)
    hp = len(past_hours)
    hours = list(past_hours) + list(fut_hours)

    def stack(key, default):
        rows = []
        for m in members:
            fut = np.asarray(m.get(key) if m.get(key) is not None else np.full(len(fut_hours), np.nan), dtype=float)
            rows.append(np.concatenate([np.asarray(past.get(key, np.full(hp, np.nan)), dtype=float), fut]))
        arr = np.repeat(np.array(rows), n_param, axis=0)
        return np.where(np.isfinite(arr), arr, default)
    drivers = {"temp": stack("temp", np.nan), "rh": stack("rh", 80.0), "wind": stack("wind", 1.5),
               "cloud": stack("cloud", 50.0), "sw": stack("sw", 0.0), "rain": stack("rain", 0.0)}
    # Forecast error the correction could not remove: an AR(1) wobble per world.
    sig_t = float(ctx.get("sigma_temp", 1.2))
    sig_d = float(ctx.get("sigma_dew", 1.6))
    noise_t = np.zeros((n, len(fut_hours)))
    noise_d = np.zeros((n, len(fut_hours)))
    phi = 0.85
    et = rng.normal(0, sig_t, n)
    ed = rng.normal(0, sig_d, n)
    for k in range(len(fut_hours)):
        if k:
            et = phi * et + np.sqrt(1 - phi ** 2) * rng.normal(0, sig_t, n)
            ed = phi * ed + np.sqrt(1 - phi ** 2) * rng.normal(0, sig_d, n)
        noise_t[:, k] = et
        noise_d[:, k] = ed
    t_f = drivers["temp"][:, hp:] + noise_t
    dew_f = np.minimum(leafwet.dew_from_rh(drivers["temp"][:, hp:], drivers["rh"][:, hp:]) + noise_d, t_f)
    drivers["temp"] = np.concatenate([drivers["temp"][:, :hp], t_f], axis=1)
    drivers["rh"] = np.concatenate([drivers["rh"][:, :hp], leafwet.rh_from_dew(t_f, dew_f)], axis=1)
    if not np.isfinite(drivers["temp"]).all():
        raise ValueError("temperature has gaps; fill them before forecasting")
    # The sensor's wet hours count for the spin-up only; ahead stays simulated.
    if past.get("wet_obs") is not None:
        drivers["wet_obs"] = np.concatenate([np.asarray(past["wet_obs"], dtype=float),
                                             np.full(len(fut_hours), np.nan)])

    res = run(drivers, worlds, ctx.get("cover"))
    mult = multipliers(hours, ctx)

    out = {"windows": [], "hourly": [], "members": nm, "worlds": n}
    for wh in WINDOWS_H:
        b = min(hp + wh, len(hours))
        haz = hazards(res, mult, worlds, scale, hp, b)
        pw = combine(haz)
        groups = {}
        for g, keys in pz.GROUPS.items():
            hg = sum(haz[k] for k in keys)
            groups[g] = round(float(np.mean(1.0 - np.exp(-hg))), 3)
        total = sum(float(np.mean(h)) for h in haz.values()) or 1.0
        share = {g: round(sum(float(np.mean(haz[k])) for k in keys) / total, 3)
                 for g, keys in pz.GROUPS.items()}
        wet_h = res["wet"][:, hp:b].sum(axis=1)
        by_member = pw.reshape(nm, n_param).mean(axis=1)
        out["windows"].append({
            "hours": wh, **summarise(pw), "level": None, "groups": groups, "share": share,
            "wet_h": round(float(np.median(wet_h)), 1),
            "wet_h_lo": round(float(np.percentile(wet_h, 10)), 1),
            "wet_h_hi": round(float(np.percentile(wet_h, 90)), 1),
            "members_high": int((by_member >= LEVELS[0][0]).sum()),
            # Hazard before the scale: what the climate comparison ranks, so
            # the comparison does not move when the field log moves the scale.
            "x": round(float(np.mean(sum(haz.values()))) / scale, 5),
        })
        out["windows"][-1]["level"] = level(out["windows"][-1]["p"])
    # Cumulative odds hour by hour, for the chart.
    cum = {k: np.cumsum(res["inc"][k][:, hp:] * mult[k][None, hp:], axis=1) * worlds["path"][k]["spores"][:, None] * scale
           for k in res["inc"]}
    total = sum(cum.values())
    pc = 1.0 - np.exp(-total)
    wet_share = res["wet"][:, hp:].mean(axis=0)
    for i, h in enumerate(fut_hours):
        out["hourly"].append({"time": h, "p": round(float(pc[:, i].mean()), 3),
                              "lo": round(float(np.percentile(pc[:, i], 10)), 3),
                              "hi": round(float(np.percentile(pc[:, i], 90)), 3),
                              "wet": round(float(wet_share[i]), 2)})
    # The likeliest wet spell in the first day, from the run-average wetness.
    out["spell"] = wet_spell(fut_hours[:24], wet_share[:24])
    out["spell_temp"] = None
    if out["spell"]:
        i0, i1 = out["spell"]["i0"], out["spell"]["i1"]
        out["spell_temp"] = round(float(np.median(drivers["temp"][:, hp + i0:hp + i1 + 1])), 1)
    return out


def wet_spell(hours, wet_share, cut=0.5):
    """Longest run of hours wet in at least half the worlds."""
    best, start = None, None
    for i, s in enumerate(list(wet_share) + [0.0]):
        if s >= cut and start is None:
            start = i
        elif s < cut and start is not None:
            if best is None or i - start > best[1] - best[0]:
                best = (start, i)
            start = None
    if not best:
        return None
    i0, i1 = best[0], best[1] - 1
    return {"i0": i0, "i1": i1, "from": hours[i0], "to": hours[i1] + timedelta(hours=1),
            "hours": i1 - i0 + 1}


# ------------------------------------------------------------ plain words

def drivers_text(fc, ctx, top_group):
    """Two or three short reasons, most important first."""
    out = []
    sp = fc.get("spell")
    if sp:
        a = local(sp["from"])
        b = local(sp["to"])
        t = fc.get("spell_temp")
        out.append(f"Leaves wet about {sp['hours']} h, {a:%-I %p} to {b:%-I %p}"
                   + (f", at {t:.0f} °C" if t is not None else ""))
    else:
        out.append("Leaves expected to stay dry")
    if top_group:
        why = {"alternaria": "suits Alternaria", "black_rot": "suits black rot",
               "downy_mildew": "suits downy mildew", "sclerotinia": "suits white mould"}[top_group]
        out.append(f"That weather {why} most")
    st = ctx.get("stage")
    if st:
        out.append(f"Crop stage: {st}")
    elif ctx.get("transplanted") is None:
        out.append("Crop stage not set, so all stages are weighed evenly")
    if ctx.get("symptoms"):
        g, d = max(((g, max(v)) for g, v in ctx["symptoms"].items() if v), key=lambda x: x[1])
        out.append(f"{pz.GROUP_NAMES[g]} logged on {d:%-d %b}: spores now on the farm")
    return out[:4]


def growing_conditions(temps_day, temps_night):
    """How the last week compares with cabbage's best: days 15-24 C, nights
    4-15 C (TAMU and general guidance). -> dict for the page."""
    def share(arr, lo, hi):
        a = np.asarray([x for x in arr if x is not None and np.isfinite(x)])
        return round(float(((a >= lo) & (a <= hi)).mean()), 2) if a.size else None
    return {"day_in_band": share(temps_day, 15, 24), "night_in_band": share(temps_night, 4, 15),
            "day_mean": round(float(np.nanmean(temps_day)), 1) if len(temps_day) else None,
            "night_mean": round(float(np.nanmean(temps_night)), 1) if len(temps_night) else None,
            "hot_hours": int(np.sum(np.asarray(temps_day, dtype=float) > 30)) if len(temps_day) else 0}
