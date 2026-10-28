"""What is running, named the way the sites show it.

Each part has a name and a version, like a product line:

    Phytoplex 1.4    the firmware in the field box, read from its readings
    Phytoplex 1.6    the next firmware, written and waiting to be flashed
    Rootstock 1.0    the Pi's software: API, dashboard, feeds, alerts
    Sporecast 1.0    the disease odds engine (src/ml/odds.py and what it calls)

No dates anywhere on the sites. Raise ROOTSTOCK with each push meant for the
Pi (1.0, 1.1, ... 2.0 for a big change). Raise SPORECAST when the model's
sums change, not for a fix that leaves every number the same. The box's
version is never typed here: every reading says which firmware sent it.

The commit is read from the checkout and kept in /summary.json and the
update log for checking, but not shown on the sites.
"""
from functools import lru_cache
from pathlib import Path

BOX = "Phytoplex"
NEXT_FIRMWARE = "1.6.0"     # written, tested on a computer; flashed at season end
ROOTSTOCK = "1.0"
SPORECAST = "1.0"

_ROOT = Path(__file__).resolve().parent.parent


@lru_cache(maxsize=1)
def commit():
    """Short hash of the checked-out commit, or None. Reads .git directly:
    no git binary, no subprocess, safe to call from a request."""
    git = _ROOT / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if head.startswith("ref: "):
            ref = head[5:]
            p = git / ref
            if p.is_file():
                return p.read_text().strip()[:7]
            packed = git / "packed-refs"
            if packed.is_file():
                for line in packed.read_text().splitlines():
                    if line.endswith(" " + ref):
                        return line.split(" ", 1)[0][:7]
            return None
        return head[:7] or None
    except OSError:
        return None


def short(v):
    """'1.4.0' -> '1.4', '1.6.1' -> '1.6.1'. A trailing .0 patch is noise."""
    if not v:
        return None
    parts = str(v).split(".")
    if len(parts) == 3 and parts[2] == "0":
        parts = parts[:2]
    return ".".join(parts)


def versions(firmware=None):
    """Everything the sites show, in one place. `names` is the text on the
    page; the rest is for scripts and the update log."""
    box = short(firmware)
    nxt = short(NEXT_FIRMWARE)
    return {
        "names": {
            "box": f"{BOX} {box}" if box else None,
            "next_box": f"{BOX} {nxt}" if nxt and nxt != box else None,
            "pi": f"Rootstock {ROOTSTOCK}",
            "odds": f"Sporecast {SPORECAST}",
        },
        "firmware": firmware, "next_firmware": NEXT_FIRMWARE,
        "rootstock": ROOTSTOCK, "sporecast": SPORECAST, "commit": commit(),
    }
