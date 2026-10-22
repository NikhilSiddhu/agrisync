"""Disease, or just stress?

A drop in canopy greenness alone could be disease, drought, hunger, a
harvest, or haze the cloud mask missed. This check only points at disease
when two independent things line up: the satellite saw the canopy fall, and
the station's weather sat in the infection window over the same days.

It is a fixed rule, not a trained model, and it has not been checked against
the field log yet. Its job is to say where to look, not what is there.

    canopy   latest clear Sentinel-2 pass against the greenest clear pass in
             the three weeks before it (or the previous pass)
    weather  the infection level the station scored for each day in between,
             each day scored noon to noon so a night's wet spell stays whole
"""

FALL = -0.08        # NDVI drop that counts as a fall. A clear 3x3 pixel mean
GROW = 0.05         # moves by a few hundredths pass to pass on its own.
LOOKBACK_D = 21
RANK = {"low": 0, "rising": 1, "high": 2}


def canopy_change(clear, lookback_days=LOOKBACK_D):
    """clear: [(datetime, ndvi)] oldest first, clear passes only."""
    if len(clear) < 2:
        return None
    t1, v1 = clear[-1]
    prior = [(t, v) for t, v in clear[:-1] if 0 < (t1 - t).total_seconds() <= lookback_days * 86400]
    t0, v0 = max(prior, key=lambda x: x[1]) if prior else clear[-2]
    delta = round(v1 - v0, 3)
    trend = "falling" if delta <= FALL else "growing" if delta >= GROW else "steady"
    return {"from": t0, "to": t1, "ndvi_from": v0, "ndvi_to": v1,
            "delta": delta, "trend": trend}


def weather_between(levels_by_day, start_day, end_day):
    """levels_by_day: {date: {"levels": {disease: level}}}. Counts the days
    whose worst disease reached each level."""
    worst = {}
    for day, v in levels_by_day.items():
        if start_day <= day <= end_day and v.get("levels"):
            worst[day] = max(v["levels"].values(), key=lambda lv: RANK.get(lv, -1))
    return {"days": (end_day - start_day).days + 1, "covered": len(worst),
            "high": sum(1 for lv in worst.values() if lv == "high"),
            "rising": sum(1 for lv in worst.values() if lv == "rising")}


def verdict(canopy, weather):
    if canopy is None:
        return {"state": "waiting"}
    fell = canopy["trend"] == "falling"
    favoured = weather["high"] >= 1 or weather["rising"] >= 2
    thin = weather["covered"] < 0.5 * weather["days"]
    if fell and favoured:
        state = "check"
    elif fell and thin:
        state = "gap"
    elif fell:
        state = "stress"
    elif favoured:
        state = "watch"
    else:
        state = "clear"
    return {"state": state, "thin_weather": thin}
