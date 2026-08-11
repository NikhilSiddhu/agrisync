#!/usr/bin/env python3
"""Fifth pass: make the locked panels read as layers instead of merged boxes.

    cd ~/agrisync && python3 patch_dashboard_5.py && sudo systemctl restart agrisync-dashboard

THE PROBLEM
    The lock card is supposed to sit OVER visible blurred content. Seeing
    something substantial dissolve behind it is the entire mechanism - that is
    what makes a gate feel like an invitation rather than a wall.

    The skeleton bars were #e8e5db on a #fbfaf6 panel. Four percent of
    contrast, then blurred 7px and dropped to 50% opacity. Nothing survives
    that. So the card floats on blank cream and reads as one box merged into
    another with no relationship between them.

    It looked right when I mocked it because the mock had five rows of real
    data behind the card. The database has one.

THE FIX
    Three changes, all about making the layer beneath legible:

    - Skeleton and placeholder tones go from a 4% contrast step to about 12%.
      Still obviously not data, still grey bars, but they read through blur.
    - Blur drops 7px to 5px and opacity rises .5 to .62. Enough to make text
      unreadable, not so much that shape is lost. The security is server side
      anyway - the blur only has to say "there is more", not hide anything.
    - The card gets a real border and a deeper shadow so it sits ABOVE rather
      than merging into the panel it covers.

    Also fewer skeleton rows and image cells: shorter panels mean less empty
    space around the card while the record is still small.
"""
import shutil
import sys
from pathlib import Path

TPL = Path.cwd() / "dashboard" / "templates" / "index.html"
if not TPL.is_file():
    sys.exit(f"cannot find {TPL}\nRun this from ~/agrisync.")
if "lock-veil" not in TPL.read_text():
    sys.exit("run the earlier dashboard patches first.")

t = TPL.read_text()
before = t
applied, skipped = [], []


def sub(label, a, b, *, marker=None):
    global t
    if (marker or b) in t:
        skipped.append(f"{label} (already applied)")
        return
    if a not in t:
        skipped.append(f"{label} (ANCHOR NOT FOUND)")
        return
    t = t.replace(a, b, 1)
    applied.append(label)


# 1 -- the blur itself
sub("blur strength",
    """body:not(.ag-in) .lock-body{
  filter:blur(7px) saturate(.72);
  opacity:.5;""",
    """body:not(.ag-in) .lock-body{
  /* 5px, not 7. Text is unreadable either way - the server never sent the
     locked rows, so the blur is not what protects them. It only has to say
     "there is something here", and at 7px it stopped saying even that. */
  filter:blur(5px) saturate(.8);
  opacity:.62;""")

# 2 -- skeleton bars that survive the blur
sub("skeleton contrast",
    """.skel-row span{
  height:9px; border-radius:5px;
  background:linear-gradient(90deg,#e8e5db,#efece3,#e8e5db);""",
    """.skel-row span{
  /* Was #e8e5db on a #fbfaf6 panel - a 4% step, invisible once blurred.
     These need to read as "rows continue" through 5px of blur. */
  height:11px; border-radius:6px;
  background:linear-gradient(90deg,#dcd8c9,#e6e2d6,#dcd8c9);""")

# 3 -- placeholder image cells
sub("image cell contrast",
    """.img-cell{
  aspect-ratio:4/3; border-radius:10px;
  background:linear-gradient(135deg,#dfe3d4,#eceadf 55%,#d9ded0);
  border:1px solid var(--line);
}""",
    """.img-cell{
  aspect-ratio:4/3; border-radius:10px;
  background:linear-gradient(135deg,#ccd3bd,#dde0cf 55%,#c6cfb6);
  border:1px solid #c2cbb2;
}""")

# 4 -- the card as a distinct layer
sub("card elevation",
    """  background:rgba(251,250,246,.86);
  backdrop-filter:blur(14px) saturate(1.15);
  -webkit-backdrop-filter:blur(14px) saturate(1.15);
  border:1px solid rgba(228,225,214,.9);
  border-radius:calc(var(--r) - 2px);
  box-shadow:0 1px 2px rgba(30,40,25,.05), 0 18px 46px rgba(30,40,25,.13);""",
    """  /* Nearly opaque, with a defined edge and a deep shadow. The card has to
     read as a separate sheet lying ON the panel. At 86% with a faint border
     it blended into the surface it covers, which is what made two boxes look
     like one merged box. */
  background:rgba(252,251,248,.965);
  backdrop-filter:blur(16px) saturate(1.1);
  -webkit-backdrop-filter:blur(16px) saturate(1.1);
  border:1px solid rgba(196,192,176,.85);
  border-radius:calc(var(--r) - 2px);
  box-shadow:0 2px 4px rgba(30,40,25,.06),
             0 10px 20px rgba(30,40,25,.09),
             0 28px 60px rgba(30,40,25,.16);""")

# 5 -- lighter veil, so content above the card stays visible
sub("veil gradient",
    """  background:linear-gradient(180deg,
    rgba(244,242,236,0) 0%,
    rgba(244,242,236,.55) 42%,
    rgba(244,242,236,.93) 78%,
    rgba(244,242,236,.99) 100%);""",
    """  /* Starts later and stays lighter at the top, so the rows above the card
     are still visible. The old ramp washed the panel out by 42% down, which
     removed the very thing the card was supposed to be sitting on. */
  background:linear-gradient(180deg,
    rgba(244,242,236,0) 0%,
    rgba(244,242,236,.12) 30%,
    rgba(244,242,236,.62) 62%,
    rgba(244,242,236,.92) 100%);""")

# 6 -- shorter panels while the record is small
sub("fewer skeleton rows",
    "            {% for _ in range(7) %}\n",
    "            {% for _ in range(5) %}\n")

sub("fewer image cells",
    "{% for _ in range(12) %}<div class=\"img-cell\"></div>{% endfor %}",
    "{% for _ in range(8) %}<div class=\"img-cell\"></div>{% endfor %}")


if t != before:
    shutil.copy(TPL, TPL.with_suffix(".html.bak5"))
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
