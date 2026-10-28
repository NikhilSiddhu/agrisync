"""Disease alerts to the owner's phone.

An alert goes out when the odds engine (src/ml/odds.py) puts the next 48
hours at HIGH (1 in 4 or more) and at least half of the 51 weather runs
agree. One alert when it turns high, then at most one reminder a day while
it stays high. If the odds have not been computed (a fresh install), the
older per-disease rule below stands in: a disease at HIGH within 48 hours
with half the runs agreeing.

Only the models with published thresholds alert by default. The two with
estimated thresholds (downy mildew, black rot) would mostly cry wolf; set
ALERT_ESTIMATED=1 in .env to include them anyway.

Every message says the levels are uncalibrated, and once the field log has
enough visits it also says how often a "high" was followed by symptoms.
Delivery goes through src/notify.py (ntfy or Telegram, whichever .env sets).
"""
import os
from datetime import timedelta, timezone

from src import notify

LOOKAHEAD_H = 48
MIN_ENS_SHARE = 0.5
COOLDOWN = timedelta(hours=24)
IST = timedelta(hours=5, minutes=30)


def _aware(dt):
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def candidates(fc, include_estimated=None):
    """Diseases forecast HIGH within 48 h with at least half the runs agreeing."""
    if include_estimated is None:
        include_estimated = os.getenv("ALERT_ESTIMATED", "0") == "1"
    out = []
    for r in (fc or {}).get("rows") or []:
        if not r.get("sourced") and not include_estimated:
            continue
        for c in r.get("cells") or []:
            if c.get("lead_h", 999) > LOOKAHEAD_H or c.get("level") != "high":
                continue
            ens = c.get("ens") or {}
            n = ens.get("n") or 0
            if n and (ens.get("high") or 0) / n < MIN_ENS_SHARE:
                continue
            out.append({"key": r["key"], "name": r["name"], "sourced": r.get("sourced", False),
                        "at": _aware(c.get("at")), "lead_h": c["lead_h"],
                        "ens_high": ens.get("high"), "ens_n": n})
            break
    return out


ODDS_MAX_AGE = timedelta(hours=3)


def odds_fresh(odds, now):
    gen = _aware((odds or {}).get("generated_at"))
    return bool(odds and odds.get("windows")) and gen is not None and now - gen <= ODDS_MAX_AGE


def odds_candidate(odds):
    """The 48 h odds, if high with at least half the weather runs agreeing."""
    if not odds or not odds.get("windows"):
        return None
    w = next((x for x in odds["windows"] if x.get("hours") == LOOKAHEAD_H), None)
    members = (odds.get("inputs") or {}).get("members") or 0
    if not w or w.get("level") != "high":
        return None
    if members and (w.get("members_high") or 0) / members < MIN_ENS_SHARE:
        return None
    return {"key": "odds", "name": "Disease odds", "p": w["p"], "lo": w["lo"], "hi": w["hi"],
            "members_high": w.get("members_high"), "members": members,
            "top": odds.get("top_name"), "spell": odds.get("spell"),
            "checked": (odds.get("calibration") or {}).get("checked", False)}


def odds_message(c, now):
    pct = round(100 * c["p"])
    body = (f"Odds of infection in the next 48 h: {pct}% "
            f"(range {round(100 * c['lo'])}-{round(100 * c['hi'])}%).")
    sp = c.get("spell")
    if sp and sp.get("from"):
        a = (_aware(sp["from"]) + IST).strftime("%a %-I %p")
        b = (_aware(sp["to"]) + IST).strftime("%-I %p")
        t = f" at {sp['temp']:.0f} C" if sp.get("temp") is not None else ""
        body += f" Leaves wet {a} to {b}{t}."
    if c.get("top"):
        body += f" Most likely: {c['top']}."
    if c.get("members"):
        body += f" {c['members_high']} of {c['members']} weather runs put it high."
    if not c.get("checked"):
        body += " Not yet checked against this field: walk the crop before deciding to spray."
    return {"key": "odds", "title": "AgriSync: high odds of infection", "body": body, "at": now}


def message(c, cal, now):
    when = (c["at"] + IST).strftime("%a %d %b, %H:%M") if c.get("at") else f"within {c['lead_h']} h"
    agree = f" {c['ens_high']} of {c['ens_n']} weather runs agree." if c.get("ens_n") else ""
    body = f"{c['name']}: high pressure by {when} IST.{agree}"
    if not c.get("sourced"):
        body += " This model uses estimated thresholds."
    t = ((cal or {}).get("levels") or {}).get("high") or {}
    if (cal or {}).get("ready") and t.get("n"):
        body += (f" Past record: after a high, new symptoms followed {t['k']} of {t['n']} times "
                 f"(90% range {round(100 * t['lo'])} to {round(100 * t['hi'])}%).")
    else:
        body += " Uncalibrated: walk the field before you decide to spray."
    return {"key": c["key"], "title": "AgriSync: disease pressure turning high",
            "body": body, "at": now}


def evaluate(fc, state, cal, now, odds=None):
    """-> {"send": [messages], "state": updated alert state}"""
    state = dict(state or {})
    per = dict(state.get("per") or {})
    if odds_fresh(odds, now):
        oc = odds_candidate(odds)
        active = {"odds": oc} if oc else {}
    else:
        active = {c["key"]: c for c in candidates(fc)}
    send = []
    for key, c in active.items():
        prev = per.get(key) or {}
        last = _aware(prev.get("last_sent"))
        if not prev.get("active") or last is None or now - last >= COOLDOWN:
            send.append(odds_message(c, now) if key == "odds" else message(c, cal, now))
            per[key] = {"active": True, "last_sent": now}
        else:
            per[key] = dict(prev, active=True)
    for key, prev in list(per.items()):
        if key not in active and prev.get("active"):
            per[key] = dict(prev, active=False, cleared_at=now)
    state["per"] = per
    state["active"] = sorted(active)
    state["channel"] = channel_configured()
    return {"send": send, "state": state}


def channel_configured():
    return bool(os.getenv("ALERT_WEBHOOK_URL", "").strip()
                or (os.getenv("TELEGRAM_BOT_TOKEN", "").strip() and os.getenv("TELEGRAM_CHAT_ID", "").strip()))


def deliver(msg):
    return notify.push(msg["title"], msg["body"], priority="high", tags="seedling,warning")
