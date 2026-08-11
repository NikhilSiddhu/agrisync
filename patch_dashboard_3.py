#!/usr/bin/env python3
"""Third dashboard pass: fix the member range bug, the coverage maths, the
empty disease panel, and a placeholder link.

    cd ~/agrisync && python3 patch_dashboard_3.py && sudo systemctl restart agrisync-dashboard


1. THE MEMBER RANGE BUG  (this one actually breaks the feature)

   series(), stats() and wet_hours() each call clamp(hours) with no member
   flag, so each one re-clamps to the PUBLIC ceiling. app.py resolves 30 days
   correctly, passes 720 in, and every function quietly cuts it back to 168.

   A signed-in visitor picks "Everything", the page says Everything, and the
   charts show seven days. Worse than an error, because nothing looks wrong.

   Introduced when clamp() gained the member parameter - the callers were
   updated and these internal calls were not. Fixed by clamping against the
   absolute ceiling instead: still refuses a crafted ?hours=999999999, no
   longer overrides a decision the caller already made correctly.


2. COVERAGE

   expected = hours * 60 assumes a reading every minute. The station sends
   every five, so the denominator was 5x too big and the percentage was
   nonsense regardless of how well the station was running.

   The gap in the data is real - 118 readings on 11 Aug, 63 on 5 Aug, nothing
   between - and this does not paper over that. It only makes the number mean
   what it says.


3. THE EMPTY DISEASE PANEL

   "Conditions, scored." followed by "no wet hours recorded" reads as a broken
   feature. It is a design limitation: wetness is inferred from dew point and
   it has not been wet. Saying so converts an apparent bug into a stated
   constraint, which is the difference between looking careless and looking
   careful.


4. A PLACEHOLDER I LEFT

   The X button points at https://x.com/ with no handle.
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


# ------------------------------------------------------------- data.py -----

text = DATA.read_text()
before = text

if "member=True)  # caller already decided" in text:
    skipped.append("data.py — member range fix (already applied)")
else:
    n = text.count("    hours = clamp(hours)\n")
    if n != 3:
        skipped.append(f"data.py — member range fix (expected 3 sites, found {n})")
    else:
        text = text.replace(
            "    hours = clamp(hours)\n",
            "    # Absolute ceiling only. app.py has already applied the public or\n"
            "    # member limit from a signed cookie; re-applying the public one here\n"
            "    # would silently undo it and serve a member seven days.\n"
            "    hours = clamp(hours, member=True)  # caller already decided the window\n",
        )
        applied.append("data.py — member range fix (3 call sites)")

if "SAMPLE_MINUTES" in text:
    skipped.append("data.py — coverage maths (already applied)")
elif "    expected = hours * 60  # one reading a minute\n" not in text:
    skipped.append("data.py — coverage maths (ANCHOR NOT FOUND)")
else:
    text = text.replace(
        "    expected = hours * 60  # one reading a minute\n",
        "    # How often the station actually reports. Board A posts every five\n"
        "    # minutes, so assuming one a minute made the denominator five times\n"
        "    # too large and the percentage meaningless. Override with\n"
        "    # SAMPLE_MINUTES in .env if the firmware interval changes.\n"
        "    expected = hours * 60 / SAMPLE_MINUTES\n",
    )
    # Module-level constant, next to the other env-driven ones.
    text = text.replace(
        'STALE_MINUTES = int(os.getenv("DASH_STALE_MINUTES", "30"))',
        'STALE_MINUTES = int(os.getenv("DASH_STALE_MINUTES", "30"))\n'
        'SAMPLE_MINUTES = max(1, int(os.getenv("SAMPLE_MINUTES", "5")))',
        1,
    )
    applied.append("data.py — coverage maths")

if text != before:
    try:
        ast.parse(text)
    except SyntaxError as exc:
        sys.exit(f"data.py would not parse ({exc}). Nothing written.")
    shutil.copy(DATA, DATA.with_suffix(".py.bak3"))
    DATA.write_text(text)


# --------------------------------------------------------- index.html ------

t = TPL.read_text()
before = t

OLD_EMPTY = '''        <p style="text-align:center;color:var(--ink-soft);font-size:.95rem">
          {{ risk.reason if risk.reason else 'Waiting for leaf wetness data.' }}</p>'''

NEW_EMPTY = '''        {#- An empty panel under a heading that promises scores reads as a
            broken feature. It is not: the models need wet hours, wetness is
            inferred from dew point, and it has not been wet. Saying that is
            the difference between looking careless and looking careful. -#}
        <div class="risk-empty">
          <p class="risk-empty-h">Nothing to score yet.</p>
          <p class="risk-empty-p">
            Both infection models need leaf wetness, and wetness is inferred
            from air temperature and dew point rather than measured directly.
            The leaf has not reached its dew point recently, so there are no
            wet hours to score against.
            {% if risk.reason %}<br><span class="risk-empty-r">{{ risk.reason }}</span>{% endif %}
          </p>
          <p class="risk-empty-p">
            <a href="https://agrisync.tech/#engine">How the scoring works &rarr;</a>
          </p>
        </div>'''

if "risk-empty" in t:
    skipped.append("index.html — disease panel copy (already applied)")
elif OLD_EMPTY not in t:
    skipped.append("index.html — disease panel copy (ANCHOR NOT FOUND)")
else:
    t = t.replace(OLD_EMPTY, NEW_EMPTY, 1)
    t = t.replace("</head>", '''<style>
.risk-empty{ max-width:520px; margin:0 auto; text-align:center; }
.risk-empty-h{
  margin:0 0 .5rem; font-size:1.05rem; font-weight:500; color:var(--ink);
}
.risk-empty-p{
  margin:0 0 .6rem; font-size:.9rem; line-height:1.6; color:var(--ink-soft);
}
.risk-empty-r{ font-size:.82rem; opacity:.75; }
.risk-empty a{ color:var(--moss); text-decoration:none;
  border-bottom:1px solid rgba(92,115,64,.35); }
.risk-empty a:hover{ border-bottom-color:var(--moss); }
</style>
</head>''', 1)
    applied.append("index.html — disease panel copy")

if 'href="https://x.com/"' in t:
    t = t.replace('href="https://x.com/"', 'href="https://x.com/nikhilsiddhu_"')
    applied.append("index.html — real X handle")
else:
    skipped.append("index.html — X handle (already fixed or not found)")

if t != before:
    shutil.copy(TPL, TPL.with_suffix(".html.bak3"))
    TPL.write_text(t)


print()
for a in applied:
    print(f"  applied   {a}")
for s in skipped:
    print(f"  skipped   {s}")
print()
if any("ANCHOR NOT FOUND" in s for s in skipped):
    print("  An anchor missed. Send me the file.")
else:
    print("  Now:  sudo systemctl restart agrisync-dashboard")
print()
