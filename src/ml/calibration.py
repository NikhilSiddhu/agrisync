"""Calibrated uncertainty: how often the levels came true, from the field log.

The infection models say low, rising or high. Calibration asks the only
question that matters about that: when the model said high, how often did
symptoms actually follow? The answer comes from one place, the field log,
so every walk through the field, including the healthy ones, is a data point.

HOW A FIELD VISIT BECOMES A DATA POINT
    Each visit day and each disease group gives at most one pair:

    outcome   True  new symptoms of that disease: first seen, or more severe
                    than at the previous visit
              False the walk found nothing of that disease
              None  already infected and not worse (not a new event), or
                    no visit; left out
    exposure  the worst level the station scored for that disease over the
              incubation window before the visit. A lesion seen today was
              set off by weather days ago, so the model is judged on those
              days, not on today. Windows are assumptions from the general
              literature, stated here so they can be argued with:

                  Alternaria leaf spot   2 to 7 days
                  downy mildew           3 to 8 days
                  black rot              7 to 21 days
                  white mould            4 to 10 days

              A window needs station data on at least half its days.

    A row with "nothing seen" makes the visit a negative for every disease.
    A row naming one disease makes it a negative for the others, on the
    reasoning that a walk which found Alternaria would have reported black
    rot too.

WHAT COMES OUT
    For each level: visits n, visits with new symptoms k, the rate k/n and a
    90% Wilson interval. That interval is the "stated error rate": with five
    visits it is wide, and saying so is the point. The table is called ready
    only with at least 20 judged visits and 3 with new symptoms; before that
    the dashboard shows it as collecting.

One field gives few outbreaks a season. This will stay wide for a long time.
Pooling more fields is what would narrow it.
"""
from datetime import date, timedelta
from math import sqrt

LEVELS = ("low", "rising", "high")
RANK = {"low": 0, "rising": 1, "high": 2}
GROUPS = {
    "alternaria": ("alternaria_brassicae", "alternaria_brassicicola"),
    "downy_mildew": ("downy_mildew",),
    "black_rot": ("black_rot",),
    "sclerotinia": ("sclerotinia",),
}
GROUP_NAMES = {"alternaria": "Alternaria leaf spot", "downy_mildew": "Downy mildew",
               "black_rot": "Black rot", "sclerotinia": "White mould"}
INCUBATION_D = {"alternaria": (2, 7), "downy_mildew": (3, 8), "black_rot": (7, 21),
                "sclerotinia": (4, 10)}
MIN_JUDGED, MIN_POSITIVE = 20, 3
Z90 = 1.6449


def wilson(k, n, z=Z90):
    """Wilson score interval for k successes in n trials."""
    if n == 0:
        return None, None
    p = k / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, centre - half), 3), round(min(1.0, centre + half), 3)


def _day(v):
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def visits(rows):
    """Field log rows -> {date: {"healthy": bool, "seen": {group: severity}}}"""
    out = {}
    for r in rows:
        d = _day(r["date"])
        v = out.setdefault(d, {"healthy": False, "seen": {}})
        disease = r.get("disease")
        sev = int(r.get("severity") or 0)
        if disease == "none":
            v["healthy"] = True
        elif disease in GROUPS and sev >= 1 and int(r.get("plants_affected") or 0) >= 1:
            v["seen"][disease] = max(sev, v["seen"].get(disease, 0))
    return out


def outcomes(by_day):
    """-> {(date, group): True/False/None}"""
    out = {}
    last = {g: 0 for g in GROUPS}
    for d in sorted(by_day):
        v = by_day[d]
        for g in GROUPS:
            sev = v["seen"].get(g, 0)
            if sev:
                out[(d, g)] = True if sev > last[g] else None
            else:
                out[(d, g)] = False
            last[g] = sev
    return out


def exposure(levels_by_day, d, group):
    """Worst level of the group's models over the window before day d."""
    lo, hi = INCUBATION_D[group]
    days = [d - timedelta(days=k) for k in range(lo, hi + 1)]
    seen = [levels_by_day[x]["levels"] for x in days if x in levels_by_day]
    if len(seen) * 2 < len(days):
        return None
    worst = -1
    for lv in seen:
        for key in GROUPS[group]:
            worst = max(worst, RANK.get(lv.get(key), -1))
    return LEVELS[worst] if worst >= 0 else None


def _table(pairs):
    t = {}
    for lv in LEVELS:
        n = sum(1 for level, _ in pairs if level == lv)
        k = sum(1 for level, hit in pairs if level == lv and hit)
        lo, hi = wilson(k, n)
        t[lv] = {"n": n, "k": k, "rate": round(k / n, 3) if n else None, "lo": lo, "hi": hi}
    return t


def calibrate(rows, levels_by_day):
    """rows: field_log documents. levels_by_day: {date: {"levels": {model: level}}}"""
    by_day = visits(rows)
    oc = outcomes(by_day)
    pairs, per_group = [], {g: [] for g in GROUPS}
    unscored = 0
    for (d, g), hit in sorted(oc.items()):
        if hit is None:
            continue
        lv = exposure(levels_by_day, d, g)
        if lv is None:
            unscored += 1
            continue
        pairs.append((lv, hit))
        per_group[g].append((lv, hit))
    positives = sum(1 for _, hit in pairs if hit)
    table = _table(pairs)
    ready = len(pairs) >= MIN_JUDGED and positives >= MIN_POSITIVE
    result = {
        "visits": len(by_day),
        "judged": len(pairs),
        "positives": positives,
        "unscored": unscored,
        "ready": ready,
        "needs": {"judged": max(0, MIN_JUDGED - len(pairs)),
                  "positives": max(0, MIN_POSITIVE - positives)},
        "levels": table,
        "groups": {g: {"name": GROUP_NAMES[g], "judged": len(p),
                       "positives": sum(1 for _, h in p if h), "levels": _table(p)}
                   for g, p in per_group.items()},
        "incubation_days": {g: list(w) for g, w in INCUBATION_D.items()},
    }
    # As a warning: of the visits that did show new symptoms, how many had a
    # "high" before them, and of those that did not, how many had a false one.
    pos = [lv for lv, hit in pairs if hit]
    neg = [lv for lv, hit in pairs if not hit]
    result["warning"] = {
        "caught": sum(1 for lv in pos if lv == "high"), "outbreaks": len(pos),
        "false_alarms": sum(1 for lv in neg if lv == "high"), "quiet_visits": len(neg),
    }
    return result
