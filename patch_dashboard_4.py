#!/usr/bin/env python3
"""Fourth pass: pluralisation, and a contradiction about the MLX90614.

    cd ~/agrisync && python3 patch_dashboard_4.py && sudo systemctl restart agrisync-api agrisync-dashboard


1. "The last 1 days."  /  "1 readings"

   Small, but this is the kind of thing a reader registers as sloppiness
   without being able to say why. On a page whose whole argument is
   carefulness with measurement, it costs more than it looks.

2. THE MLX CONTRADICTION

   The disease panel now says the infrared sensor "never worked reliably and
   was removed", and then data.py appends "leaf wetness needs the MLX90614,
   which is not fitted yet" directly underneath.

   Two different stories in one paragraph: removed after failing, versus not
   fitted yet. The second also reads as an unfinished build rather than a
   decision taken for a reason. The panel already explains the situation, so
   the reason string just needs to stop repeating a version of it that is no
   longer true.
"""
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
DATA = ROOT / "dashboard" / "data.py"
TPL = ROOT / "dashboard" / "templates" / "index.html"

applied, skipped = [], []

for f in (DATA, TPL):
    if not f.is_file():
        sys.exit(f"cannot find {f}\nRun this from ~/agrisync.")


# ---------------------------------------------------------- data.py --------

t = DATA.read_text()
before = t

OLD_REASON = '"reason": "leaf wetness needs the MLX90614, which is not fitted yet"'
NEW_REASON = ('"reason": "leaf wetness is estimated from air temperature and '
              'dew point"')

if NEW_REASON in t:
    skipped.append("data.py — MLX reason (already applied)")
elif OLD_REASON not in t:
    skipped.append("data.py — MLX reason (ANCHOR NOT FOUND)")
else:
    t = t.replace(OLD_REASON, NEW_REASON)
    applied.append("data.py — MLX reason no longer contradicts the panel")

if t != before:
    try:
        ast.parse(t)
    except SyntaxError as exc:
        sys.exit(f"data.py would not parse ({exc}). Nothing written.")
    shutil.copy(DATA, DATA.with_suffix(".py.bak4"))
    DATA.write_text(t)


# ------------------------------------------------------- index.html --------

h = TPL.read_text()
before = h

PAIRS = [
    # "The last 1 days." -> "The last 24 hours."
    ("headline range",
     '<h2>The last <span class="serif">{{ (hours/24)|round|int }} days.</span></h2>',
     '<h2>The last <span class="serif">{% if hours < 48 %}{{ hours }} hours{% elif hours >= 8760 %}everything{% else %}{{ (hours/24)|round|int }} days{% endif %}.</span></h2>'),

    # "1 readings" -> "1 reading"
    ("readings count",
     '<p class="lede">{{ stats.readings or 0 }} readings{% if stats.coverage_pct %} · {{ stats.coverage_pct }}% coverage{% endif %}</p>',
     '<p class="lede">{{ stats.readings or 0 }} reading{{ \'\' if stats.readings == 1 else \'s\' }}{% if stats.coverage_pct %} · {{ stats.coverage_pct }}% coverage{% endif %}</p>'),

    # "Every one of the 1 readings taken since" -> reads properly at any count
    ("lock card count",
     '''              Every one of the {{ "{:,}".format(span.readings) }} readings taken
              since {{ span.since }}, sortable, alongside the canopy photographs.''',
     '''              {% if span.readings == 1 %}The record so far, and everything
              that follows it{% else %}Every one of the
              {{ "{:,}".format(span.readings) }} readings taken since
              {{ span.since }}{% endif %}, sortable, alongside the canopy
              photographs.'''),
]

for label, a, b in PAIRS:
    if b in h:
        skipped.append(f"index.html — {label} (already applied)")
    elif a not in h:
        skipped.append(f"index.html — {label} (ANCHOR NOT FOUND)")
    else:
        h = h.replace(a, b, 1)
        applied.append(f"index.html — {label}")

if h != before:
    shutil.copy(TPL, TPL.with_suffix(".html.bak4"))
    TPL.write_text(h)


print()
for a in applied:
    print(f"  applied   {a}")
for s in skipped:
    print(f"  skipped   {s}")
print()
if any("ANCHOR NOT FOUND" in s for s in skipped):
    print("  An anchor missed. Send me the file.")
else:
    print("  Now:  sudo systemctl restart agrisync-api agrisync-dashboard")
print()
