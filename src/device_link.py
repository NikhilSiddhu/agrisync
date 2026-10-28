"""What the Pi tells the box, in the reply to each reading (firmware 1.6.0).

The box cannot be reached in the field, so from 1.6.0 it takes its settings
from the server instead: every reply to a reading can carry a short, signed
config string, and the box applies it if the signature checks out.

    cfg      "v=1;i=300;b=12"                     settings, key=value;...
    cfg_ts   1793170000                           when the Pi signed it
    cfg_sig  hex HMAC-SHA256(secret, f"{cfg_ts}.{cfg}")

A plain string, not JSON, because the box verifies the exact bytes it
received: re-serialising JSON in C would never reproduce them reliably.
Signed with the same DEVICE_HMAC_SECRET the box signs its readings with, so
nobody on the plain-HTTP path can change how the box behaves. The box also
refuses a cfg_ts more than 15 minutes from its own clock, or older than the
last one it applied.

Keys the box understands:

    v     config version, for the logs
    i     seconds between readings (60 to 1800)
    b     readings per queued batch (1 to 12; 1 sends them one at a time)
    ota   a firmware version to install, with
    osz   its size in bytes and
    osha  its SHA-256, hex

The interval follows the field. Wet nights are when the disease models need
the finest record of when the leaves wetted and dried, so readings come
faster then; a low battery slows them down, whatever else is going on. The
box enforces its own battery floor too, so a flat battery never depends on
this file being right.
"""
import hashlib
import hmac
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CONFIG_VERSION = 1

# Firmware that reads the reply. Older boards only look at the status code.
FIRST_LINKED_FIRMWARE = (1, 6, 0)

MIN_INTERVAL_S, MAX_INTERVAL_S = 60, 1800
MAX_BATCH = 12

# A version string the box can carry in NVS and compare: digits and dots.
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


def _int_env(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def parse_version(v):
    """'1.6.0' -> (1, 6, 0); anything else -> None."""
    if not isinstance(v, str) or not VERSION_RE.match(v.strip()):
        return None
    return tuple(int(x) for x in v.strip().split("."))


def is_linked(firmware):
    """Does this firmware read the reply? (1.6.0 and later.)"""
    v = parse_version(firmware or "")
    return v is not None and v >= FIRST_LINKED_FIRMWARE


# ---------------------------------------------------------------- interval

def desired_interval(latest, now=None):
    """Seconds between readings for the box, from its own latest reading.

    BOX_INTERVAL_S       the usual interval (default 300, five minutes)
    BOX_WET_INTERVAL_S   on a wet night (default 150); set equal to the usual
                         interval to switch the boost off
    A wet night: humidity at 90% or more, or the leaf counted wet, between
    6 pm and 10 am in India. A battery under 11.9 V slows the box to ten
    minutes, under 11.6 V to fifteen.
    """
    base = _int_env("BOX_INTERVAL_S", 300)
    wet_ivl = _int_env("BOX_WET_INTERVAL_S", 150)
    latest = latest or {}
    now = now or datetime.now(timezone.utc)
    ivl = base

    hour_ist = (now + timedelta(hours=5, minutes=30)).hour
    night = hour_ist >= 18 or hour_ist < 10
    rh = latest.get("humidity_pct")
    wet = latest.get("leaf_wetness")
    if night and ((rh is not None and rh >= 90) or (wet is not None and wet >= 50)):
        ivl = min(ivl, wet_ivl)

    batt = latest.get("battery_v")
    if batt is not None and batt > 0:
        if batt < 11.6:
            ivl = max(ivl, 900)
        elif batt < 11.9:
            ivl = max(ivl, 600)

    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, int(ivl)))


# ---------------------------------------------------------------- firmware

def firmware_dir():
    return Path(os.getenv("FIRMWARE_DIR", str(Path(__file__).resolve().parent.parent / "data" / "firmware")))


def manifest():
    """The firmware on offer, or None. Written by scripts/publish_firmware.py."""
    path = firmware_dir() / "manifest.json"
    try:
        m = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(m, dict) or parse_version(m.get("version")) is None:
        return None
    if not isinstance(m.get("size"), int) or not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", ""))):
        return None
    if not (firmware_dir() / m.get("file", "")).is_file():
        return None
    return m


def firmware_path(version):
    """The published binary for this version, if it is the one on offer."""
    m = manifest()
    if not m or m["version"] != version:
        return None
    return firmware_dir() / m["file"]


MAX_OTA_ATTEMPTS = 3


def ota_offer(db, device_id, firmware):
    """The update to offer this box, or None.

    Offered when OTA_ENABLED=1, a manifest is published, it is newer than what
    the box runs, and the box has not reported failing it three times.
    """
    if os.getenv("OTA_ENABLED", "0") != "1":
        return None
    m = manifest()
    running = parse_version(firmware or "")
    if not m or running is None or parse_version(m["version"]) <= running:
        return None
    state = (db.feeds.find_one({"_id": f"ota:{device_id}"}) or {}) if db is not None else {}
    if (state.get("fails") or {}).get(m["version"].replace(".", "_"), 0) >= MAX_OTA_ATTEMPTS:
        return None
    return m


def note_ota_state(db, device_id, state, now=None):
    """Remember what the box said about an update: 'ok:1.6.1', 'fail:sha:1.6.1',
    'rolled_back:1.6.1'. Failures count against that version."""
    if db is None or not state:
        return
    state = str(state)[:48]
    now = now or datetime.now(timezone.utc)
    update = {"$set": {"last": state, "at": now}}
    parts = state.split(":")
    if parts[0] in ("fail", "rolled_back") and parse_version(parts[-1]):
        update["$inc"] = {f"fails.{parts[-1].replace('.', '_')}": 1}
    db.feeds.update_one({"_id": f"ota:{device_id}"}, update, upsert=True)


# ---------------------------------------------------------------- signing

def sign(secret, ts, cfg):
    msg = f"{int(ts)}.{cfg}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def build_cfg(latest, offer=None, now=None):
    parts = [f"v={CONFIG_VERSION}", f"i={desired_interval(latest, now)}",
             f"b={max(1, min(MAX_BATCH, _int_env('BOX_BATCH', MAX_BATCH)))}"]
    if offer:
        parts += [f"ota={offer['version']}", f"osz={offer['size']}", f"osha={offer['sha256']}"]
    return ";".join(parts)


def reply_fields(secret, latest, db=None, now=None):
    """The signed config to add to a reply, or {} when it cannot be signed or
    the box would not read it."""
    if not secret or not latest or not is_linked(latest.get("firmware")):
        return {}
    offer = ota_offer(db, latest.get("device_id"), latest.get("firmware"))
    cfg = build_cfg(latest, offer, now)
    ts = int(time.time()) if now is None else int(now.timestamp())
    return {"cfg": cfg, "cfg_ts": ts, "cfg_sig": sign(secret, ts, cfg)}
