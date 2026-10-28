"""Correct the weather forecast to this field, from the field's own record.

A global forecast describes a grid box tens of kilometres wide at 2 m. The
station sits in the crop, about a metre up, among irrigated cabbage. Its
nights run cooler and damper than the grid's, and that is exactly when dew
forms. Weather services handle this with Model Output Statistics (MOS):
learn, from past pairs of forecast and measurement, how a site differs from
the model, and correct new forecasts the same way. This does that for one
field, for air temperature and dew point.

THE CORRECTION
    error = station - forecast, modelled as a ridge regression on:
        time of day        two harmonics of local hour
        the forecast value high values tend to be over-forecast, low under
        cloud cover        clear nights are where the field departs most
        wind               calm nights too
    Training pairs are forecasts made a day ahead (24-47 h lead), so the
    correction is tuned for the lead the odds care most about.

    Two candidates compete, each trained on the 30 days before the newest
    quarter of the pairs, which is held back to score them: the regression
    above, and a plain median offset per local hour. The one with the lower
    error on those held-back hours wins, and is used only if it beats the
    raw forecast by at least 5%; otherwise the raw forecast stands. The
    winner is then refitted on the latest 30 days. Either way the scores are
    kept, so the page can say how much it helped.

    Why 30 days and not the whole season: the field's offset changes with
    the season. On this station's first season, training on everything
    since August made the October dew point WORSE than no correction
    (2.9 C error to 3.3 C), because the monsoon offset no longer held;
    training on the last 30 days cut it to 1.8-2.1 C.

NOWCAST
    For the first hours ahead, the station's current error is better than
    any statistics: if it is 2 C colder than forecast right now, the next
    hour will be too. That error is carried forward and fades with a
    six-hour time constant (blend_now).
"""
from math import pi

import numpy as np

MIN_PAIRS = 240           # ten days of hours before a correction is trusted
HOLDOUT = 0.25
MIN_GAIN = 0.05
RIDGE = 2.0
TAU_H = 6.0
WINDOW_D = 30


def design(local_hour, fc, cloud, wind, fc_mean=0.0):
    h = np.asarray(local_hour, dtype=float)
    a = 2 * pi * h / 24.0
    cl = np.nan_to_num(np.asarray(cloud, dtype=float), nan=50.0) / 100.0
    wd = np.nan_to_num(np.asarray(wind, dtype=float), nan=2.0)
    return np.column_stack([
        np.ones_like(h), np.sin(a), np.cos(a), np.sin(2 * a), np.cos(2 * a),
        np.asarray(fc, dtype=float) - fc_mean, cl, np.minimum(wd, 8.0),
    ])


def _ridge(X, y, lam):
    pen = lam * np.eye(X.shape[1])
    pen[0, 0] = 0.0                       # the intercept is not shrunk
    return np.linalg.solve(X.T @ X + pen, X.T @ y)


def _offsets(lh, err, min_n=3):
    lh = np.asarray(lh).astype(int) % 24
    overall = float(np.median(err)) if err.size else 0.0
    out = np.full(24, overall)
    for h in range(24):
        m = lh == h
        if m.sum() >= min_n:
            out[h] = float(np.median(err[m]))
    return out


def _train(kind, lh, fc, obs, cl, wd, lam):
    if kind == "offsets":
        return {"kind": "offsets", "offsets": [round(float(v), 3) for v in _offsets(lh, obs - fc)]}
    mean = float(np.mean(fc))
    beta = _ridge(design(lh, fc, cl, wd, mean), obs - fc, lam)
    return {"kind": "ridge", "coef": [round(float(b), 5) for b in beta], "fc_mean": round(mean, 3)}


def _predict(model, lh, fc, cl, wd):
    if model["kind"] == "offsets":
        return np.asarray(model["offsets"])[np.asarray(lh).astype(int) % 24]
    return design(lh, fc, cl, wd, model["fc_mean"]) @ np.asarray(model["coef"])


def fit(local_hour, fc, obs, cloud, wind, times_d=None, lam=RIDGE):
    """-> model dict. Arrays are aligned hours, oldest first; times_d is each
    hour's time in days (any origin), for the 30-day windows. Without it,
    every pair counts as recent."""
    fc = np.asarray(fc, dtype=float)
    obs = np.asarray(obs, dtype=float)
    lh, cl, wd = (np.asarray(v, dtype=float) for v in (local_hour, cloud, wind))
    td = np.zeros_like(fc) if times_d is None else np.asarray(times_d, dtype=float)
    ok = np.isfinite(fc) & np.isfinite(obs)
    idx = np.flatnonzero(ok)
    n = len(idx)
    out = {"n": int(n), "use": False}
    if n < MIN_PAIRS:
        out["state"] = "learning"
        return out
    cut = idx[int(n * (1 - HOLDOUT))]
    te = idx[idx >= cut]
    tr = idx[(idx < cut) & (td >= td[cut] - WINDOW_D)] if times_d is not None else idx[idx < cut]
    if len(tr) < MIN_PAIRS // 2:
        tr = idx[idx < cut]
    raw = float(np.sqrt(np.mean((obs[te] - fc[te]) ** 2)))
    scores = {}
    for kind in ("ridge", "offsets"):
        m = _train(kind, lh[tr], fc[tr], obs[tr], cl[tr], wd[tr], lam)
        pred = _predict(m, lh[te], fc[te], cl[te], wd[te])
        scores[kind] = float(np.sqrt(np.mean((obs[te] - fc[te] - pred) ** 2)))
    best = min(scores, key=scores.get)
    use = scores[best] <= raw * (1 - MIN_GAIN)
    recent = idx[td[idx] >= td[idx[-1]] - WINDOW_D] if times_d is not None else idx
    if len(recent) < MIN_PAIRS // 2:
        recent = idx
    final = _train(best, lh[recent], fc[recent], obs[recent], cl[recent], wd[recent], lam)
    resid = obs[recent] - fc[recent] - (_predict(final, lh[recent], fc[recent], cl[recent], wd[recent]) if use else 0.0)
    out.update(final)
    out.update({
        "state": "on" if use else "off", "use": bool(use),
        "holdout_hours": int(len(te)), "rmse_raw": round(raw, 3),
        "rmse_corrected": round(scores[best], 3),
        "scores": {k: round(v, 3) for k, v in scores.items()},
        "bias_raw": round(float(np.mean(obs[te] - fc[te])), 3),
        "trained_hours": int(len(recent)),
        "sigma": round(float(np.std(resid)), 3),
    })
    return out


def apply(model, local_hour, fc, cloud, wind):
    """Corrected forecast; the raw one when there is no usable model."""
    fc = np.asarray(fc, dtype=float)
    if not model or not model.get("use"):
        return fc
    shape = fc.shape
    pred = _predict(model, np.broadcast_to(local_hour, shape).ravel(), fc.ravel(),
                    np.broadcast_to(cloud, shape).ravel(), np.broadcast_to(wind, shape).ravel())
    return fc + np.asarray(pred).reshape(shape)


def blend_now(series, lead_h, error_now, tau=TAU_H):
    """Carry the station's current forecast error into the first hours."""
    if error_now is None or not np.isfinite(error_now):
        return series
    lead = np.asarray(lead_h, dtype=float)
    return series + error_now * np.exp(-np.clip(lead, 0, None) / tau)
