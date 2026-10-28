"""What is running, for the two sites and the logs.

RELEASE names the Pi's software (API, dashboard, feeds, odds engine) by the
day it was released. Change it with each push meant for the Pi. The commit is
read from the checkout itself, so the dashboard always says exactly which
code it runs, even between releases.

ODDS_ENGINE is the version of the disease-odds model (src/ml/odds.py and the
modules it calls). Raise it when the model's sums change, not for a fix that
leaves every number the same.

The box's firmware is not here: every reading says which firmware sent it,
so the sites show what Phytoplex is actually running.
"""
from functools import lru_cache
from pathlib import Path

RELEASE = "2026.10.28"
ODDS_ENGINE = "1.0"
# Written, tested on a computer, waiting for its bench test and the
# season-end flash (docs/FIRMWARE_1.6.0.md).
NEXT_FIRMWARE = "1.6.0"

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


def versions(firmware=None):
    """Everything the sites show, in one place."""
    return {"release": RELEASE, "commit": commit(), "odds_engine": ODDS_ENGINE,
            "firmware": firmware, "next_firmware": NEXT_FIRMWARE}
