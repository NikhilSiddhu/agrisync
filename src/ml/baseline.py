"""Learned healthy baseline: a trained model of this field when it is well.

The rules in physics_baseline say what weather favours disease. This learns
what THIS field normally looks like, from its own season, and flags when it
stops looking like that. No disease-labelled examples are needed: the model
is fitted on healthy days only (every day before the field log first records
symptoms), and anything far outside it is a departure worth a look.

Two parts, both refitted from scratch every day:

MICROCLIMATE
    The air in a crop canopy differs from the regional weather in a steady,
    learnable way: warmer by day where the sun heats the soil, more humid at
    night, more so as the canopy closes. A ridge regression learns the
    station's temperature and dew point from the ERA5-Land reanalysis for the
    same hour, the hour of day, and the canopy's greenness. The hours of the
    last week are then checked against it. A sustained departure means the
    field's own air changed: a sensor fault, irrigation, a collapsed canopy.
    ERA5-Land runs about five days behind, so this part looks back, not now.

CANOPY
    A healthy crop greens up along an S-curve. A logistic curve is fitted to
    the clear Sentinel-2 passes, and separately to the radar index. A pass
    well below its own season's curve is flagged.

Everything here is numpy. No model files: the fit is cheap and the season is
the training set.
"""
import numpy as np

Z_FLAG = 3.5            # an hour counts as a departure beyond this
MIN_RUN = 3             # and only in runs of at least this many hours
MIN_TRAIN_HOURS = 240   # ten days of overlap before the model says anything
MIN_R2 = 0.3            # below this a target is reported but never flagged from
CHECK_DAYS = 7
CANOPY_Z = 2.5


def robust_sigma(x):
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return float("nan")
    return float(1.4826 * np.median(np.abs(x - np.median(x))))


def ridge(X, y, lam=1.0):
    """Closed-form ridge. Column 0 is the intercept and is not penalised."""
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float)
    P = lam * np.eye(X.shape[1])
    P[0, 0] = 0.0
    return np.linalg.solve(X.T @ X + P, X.T @ y)


def design(era_t, era_dew, local_hour, ndvi):
    h = 2 * np.pi * np.asarray(local_hour, dtype=float) / 24.0
    era_t = np.asarray(era_t, dtype=float)
    era_dew = np.asarray(era_dew, dtype=float)
    ndvi = np.asarray(ndvi, dtype=float)
    return np.column_stack([
        np.ones_like(era_t), era_t, era_dew,
        np.sin(h), np.cos(h), np.sin(2 * h), np.cos(2 * h),
        ndvi, ndvi * np.sin(h), ndvi * np.cos(h),
    ])


def runs(mask, min_run=MIN_RUN):
    """Indices that sit in a run of at least min_run consecutive Trues."""
    out, start = set(), None
    mask = list(mask) + [False]
    for i, m in enumerate(mask):
        if m and start is None:
            start = i
        elif not m and start is not None:
            if i - start >= min_run:
                out.update(range(start, i))
            start = None
    return out


def microclimate(rows, check_from, healthy_until=None):
    """rows: dicts with time (UTC), local_hour, ndvi, st_temp, st_dew, era_temp, era_dew.

    Trains on rows before check_from (and before healthy_until, if set),
    checks rows from check_from on. Rows must be in time order.
    """
    train = [r for r in rows if r["time"] < check_from
             and (healthy_until is None or r["time"] < healthy_until)]
    check = [r for r in rows if r["time"] >= check_from]
    if len(train) < MIN_TRAIN_HOURS:
        return {"state": "learning", "trained_hours": len(train), "needs_hours": MIN_TRAIN_HOURS}

    def cols(rs):
        return design([r["era_temp"] for r in rs], [r["era_dew"] for r in rs],
                      [r["local_hour"] for r in rs], [r["ndvi"] for r in rs])

    Xt = cols(train)
    out = {"state": "normal", "trained_hours": len(train),
           "trained_days": len({r["time"].date() for r in train}),
           "checked_hours": len(check), "targets": {}, "flags": []}
    flagged = {}
    for target in ("st_temp", "st_dew"):
        y = np.array([r[target] for r in train])
        beta = ridge(Xt, y)
        fit = Xt @ beta
        resid = y - fit
        sigma = max(robust_sigma(resid), 0.25)
        ss_tot = float(((y - y.mean()) ** 2).sum())
        r2 = 1 - float((resid ** 2).sum()) / ss_tot if ss_tot > 0 else None
        usable = r2 is not None and r2 >= MIN_R2
        out["targets"][target] = {"r2": round(r2, 3) if r2 is not None else None,
                                  "sigma": round(sigma, 2), "used": usable,
                                  "plain_rmse": round(float(np.sqrt((resid ** 2).mean())), 2)}
        # A model that explains little of its target cannot say what is
        # unusual for it. Report it, but do not flag from it.
        if not check or not usable:
            continue
        Xc = cols(check)
        z = (np.array([r[target] for r in check]) - Xc @ beta) / sigma
        for i in runs(np.abs(z) > Z_FLAG):
            flagged.setdefault(i, []).append((target, float(z[i])))
    for i in sorted(flagged):
        r = check[i]
        for target, zval in flagged[i]:
            out["flags"].append({"time": r["time"], "variable": target,
                                 "z": round(zval, 1),
                                 "direction": "higher" if zval > 0 else "lower"})
    if out["flags"]:
        out["state"] = "departure"
    return out


def logistic(t, a, b, k, t0):
    return a + b / (1.0 + np.exp(-k * (np.asarray(t, dtype=float) - t0)))


def fit_logistic(t, y):
    """Least squares S-curve by grid over (k, t0), linear in (a, b)."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    best = None
    lo, hi = t.min(), t.max()
    for k in np.linspace(0.03, 0.35, 33):
        for t0 in np.linspace(lo - 10, hi + 10, 81):
            s = 1.0 / (1.0 + np.exp(-k * (t - t0)))
            A = np.column_stack([np.ones_like(s), s])
            coef, *_ = np.linalg.lstsq(A, y, rcond=None)
            a, b = coef
            if b < 0:            # a healthy season greens up; never fit a decline
                continue
            sse = float(((A @ coef - y) ** 2).sum())
            if best is None or sse < best[0]:
                best = (sse, a, b, k, t0)
    if best is None:
        return None
    _, a, b, k, t0 = best
    return {"a": float(a), "b": float(b), "k": float(k), "t0": float(t0)}


def canopy(days, values, min_points, sigma_floor, recent=1):
    """Fit the season's S-curve and judge the most recent pass(es) against it.

    The judged passes are left out of the fit, so a sudden fall cannot drag
    the curve down to meet it.
    """
    n = len(values)
    if n < min_points + recent:
        return {"state": "learning", "points": n, "needs_points": min_points + recent}
    t_fit, y_fit = days[:-recent], values[:-recent]
    p = fit_logistic(t_fit, y_fit)
    if p is None:
        return {"state": "learning", "points": n}
    resid = np.asarray(y_fit) - logistic(t_fit, **p)
    sigma = max(robust_sigma(resid), sigma_floor)
    expected = logistic(days[-recent:], **p)
    gap = float(np.median(np.asarray(values[-recent:]) - expected))
    z = gap / sigma
    return {"state": "departure" if z < -CANOPY_Z else "normal",
            "points": n, "expected": round(float(np.median(expected)), 3),
            "observed": round(float(np.median(values[-recent:])), 3),
            "z": round(z, 2), "sigma": round(sigma, 3),
            "curve": {k: round(v, 4) for k, v in p.items()}}


def overall(micro, ndvi, radar):
    parts = [x.get("state") for x in (micro, ndvi, radar) if x]
    if "departure" in parts:
        return "departure"
    if parts and all(s == "learning" for s in parts):
        return "learning"
    return "normal"


def ndvi_on(day_ord, passes):
    """Greenness on a day, linearly between clear passes, flat at the ends."""
    if not passes:
        return 0.0
    xs = [d for d, _ in passes]
    ys = [v for _, v in passes]
    return float(np.interp(day_ord, xs, ys))

