#!/usr/bin/env python3
"""Add the members' view to the AgriSync dashboard.

Put this file in ~/agrisync and run:

    cd ~/agrisync && python3 patch_dashboard.py

It edits:
    dashboard/data.py                 member-aware clamp, table(), span()
    dashboard/templates/index.html    the lock styling and three new sections


WHY A SCRIPT RATHER THAN A LIST OF EDITS
    Both files are long and every edit lands in the middle of one. Hand-patching
    this project has cost a duplicated blueprint registration, a duplicated
    import, and one overwritten #include so far. A script matches on content
    instead of line numbers, refuses to apply a change that is already there,
    and writes a .bak before touching anything.

    Running it twice is safe. It prints what it skipped and why.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
DATA = ROOT / "dashboard" / "data.py"
TPL = ROOT / "dashboard" / "templates" / "index.html"

applied, skipped = [], []


def patch(path, label, anchor, new, *, marker):
    """Replace `anchor` with `new`, unless `marker` is already in the file."""
    text = path.read_text()
    if marker in text:
        skipped.append(f"{label} — already applied")
        return
    if anchor not in text:
        skipped.append(f"{label} — ANCHOR NOT FOUND, needs doing by hand")
        return
    path.write_text(text.replace(anchor, new, 1))
    applied.append(label)


def append(path, label, body, *, marker):
    text = path.read_text()
    if marker in text:
        skipped.append(f"{label} — already applied")
        return
    path.write_text(text + body)
    applied.append(label)


for f in (DATA, TPL):
    if not f.is_file():
        sys.exit(f"cannot find {f}\nRun this from ~/agrisync, not from /tmp.")
    shutil.copy(f, f.with_suffix(f.suffix + ".bak"))


# =========================================================== data.py ========

patch(DATA, "data.py — member-aware clamp", marker="MAX_MEMBER_HOURS",
      anchor='''def clamp(hours):
    """Never return more than the public window, whatever was asked for."""
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return MAX_PUBLIC_HOURS
    return max(1, min(hours, MAX_PUBLIC_HOURS))''',
      new='''# What a signed-in visitor may ask for. Two years is far past "everything" for
# a station that went up this season, so in practice it is no limit at all. It
# exists so that a crafted ?hours=999999999 cannot ask Mongo for a scan that
# never finishes.
MAX_MEMBER_HOURS = 2 * 365 * 24

DEFAULT_HOURS = MAX_PUBLIC_HOURS

# Offered in the range picker. The member ones are drawn locked rather than
# hidden: a range you can see but cannot yet use is an invitation, and one you
# cannot see at all is nothing.
RANGES = [
    {"hours": 24,    "label": "24 hours", "member": False},
    {"hours": 72,    "label": "3 days",   "member": False},
    {"hours": 168,   "label": "7 days",   "member": False},
    {"hours": 720,   "label": "30 days",  "member": True},
    {"hours": 2160,  "label": "90 days",  "member": True},
    {"hours": 17520, "label": "Everything", "member": True},
]


def clamp(hours, member=False):
    """How far back this visitor may look.

    The ceiling is decided here, from a signed cookie, and never from anything
    the browser can set. A stranger who edits ?hours= in the address bar gets
    the public window back rather than more data.
    """
    ceiling = MAX_MEMBER_HOURS if member else MAX_PUBLIC_HOURS
    try:
        hours = int(hours)
    except (TypeError, ValueError):
        return min(DEFAULT_HOURS, ceiling)
    return max(1, min(hours, ceiling))''')


append(DATA, "data.py — table() and span()", marker="def table(", body='''

# ============================================================= members ======

# One page of readings is plenty on screen and keeps the HTML small. For bulk,
# the CSV export is the right tool and it already exists.
TABLE_LIMIT = 500

# What a stranger sees above the fade. Real readings, just not many - the top
# of a real table, cut off. Not invented rows: fake numbers on a page whose
# whole claim is "these are real measurements" would be a bad trade for a
# slightly fuller-looking screenshot.
TABLE_PUBLIC = 5


def table(hours=MAX_PUBLIC_HOURS, limit=TABLE_LIMIT):
    """Recent readings, newest first.

    Suspect rows are excluded exactly as the charts exclude them. A reading of
    179 degrees is a sensor glitch, not data, and putting it in a tidy table
    lends it a credibility it has not earned.
    """
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = _db().readings.find(
        {"ts": {"$gte": since}, "quality_flags": {"$exists": False}},
        {"_id": 0, "ts": 1, "air_temp_c": 1, "humidity_pct": 1,
         "dew_point_c": 1, "leaf_wetness": 1, "pressure_hpa": 1},
    ).sort("ts", -1).limit(int(limit))

    out = []
    for r in rows:
        ts = r.get("ts")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append({
            "when": ts.astimezone().strftime("%d %b, %H:%M") if ts else "\\u2014",
            "air": r.get("air_temp_c"),
            "hum": r.get("humidity_pct"),
            "dew": r.get("dew_point_c"),
            "wet": r.get("leaf_wetness"),
            "pres": r.get("pressure_hpa"),
        })
    return out


def span():
    """How much record exists, for the copy on the locked panels.

    "Another 23 days of this" is a reason to sign in. "Sign in for more" is
    not, because it never says how much more and the honest answer might be
    none. So the number is read from the database rather than asserted.
    """
    try:
        first = _db().readings.find_one({}, {"ts": 1, "_id": 0},
                                        sort=[("ts", 1)])
        total = _db().readings.estimated_document_count()
    except Exception:
        return {"available": False}

    if not first or not first.get("ts"):
        return {"available": False}

    ts = first["ts"]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    days = max(1, (datetime.now(timezone.utc) - ts).days)
    return {
        "available": True,
        "total_days": days,
        # What signing in actually buys. While the station is younger than the
        # public window this is 0, and the template says something else rather
        # than promising data that does not exist yet.
        "extra_days": max(0, days - MAX_PUBLIC_HOURS // 24),
        "readings": total,
        "since": ts.astimezone().strftime("%d %B %Y"),
    }


def photo_count():
    """How many canopy images exist, for the locked images panel."""
    import os as _os
    from pathlib import Path as _Path
    d = _Path(_os.getenv("PHOTO_DIR", "data/canopy")).expanduser()
    if not d.is_dir():
        return 0
    return sum(1 for p in d.iterdir()
               if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png"))
''')


# ====================================================== index.html ==========

LOCK_CSS = '''
<style>
/* ==================================================== members' view =======

   Two rules govern everything below.

   ONE. Locked never means empty. An empty box with "sign in" on it reads as a
   door slammed shut, and the honest reaction to a slammed door is to leave.
   So every locked panel renders its real shape - a table with real headers, a
   grid with the real photo count, a range picker with the real ranges - and
   then withdraws the detail behind a soft fade. You can see that something
   substantial is there. That is the difference between a tease and a refusal.

   TWO. The fade is decoration, not security. The server does not send the
   locked numbers to an anonymous browser, so there is nothing behind the blur
   to recover with devtools. If these rules were deleted entirely the page
   would show skeleton bars, not data.

   Default state is LOCKED. If the auth script is blocked or the network dies
   the page fails closed, and nobody watches the full record flash up for
   200ms before being hidden.
   ======================================================================== */

.lockable{ position:relative; }

.lock-body{
  transition:filter .55s cubic-bezier(.2,.7,.3,1), opacity .55s ease;
}
body:not(.ag-in) .lock-body{
  filter:blur(7px) saturate(.72);
  opacity:.5;
  pointer-events:none;
  user-select:none;
}

/* The veil sits over the locked body. It is a gradient rather than a flat
   wash so the top of the panel stays legible and only the depth is taken
   away - you can read the first rows and watch them dissolve, which is the
   part that makes someone want the rest. */
.lock-veil{
  position:absolute; inset:0;
  display:flex; align-items:flex-end; justify-content:center;
  padding:1.5rem;
  border-radius:var(--r);
  background:linear-gradient(180deg,
    rgba(244,242,236,0) 0%,
    rgba(244,242,236,.55) 42%,
    rgba(244,242,236,.93) 78%,
    rgba(244,242,236,.99) 100%);
  z-index:3;
}
body.ag-in .lock-veil{ display:none; }

.lock-card{
  width:100%; max-width:430px; text-align:center;
  padding:1.6rem 1.5rem 1.5rem;
  background:rgba(251,250,246,.86);
  backdrop-filter:blur(14px) saturate(1.15);
  -webkit-backdrop-filter:blur(14px) saturate(1.15);
  border:1px solid rgba(228,225,214,.9);
  border-radius:calc(var(--r) - 2px);
  box-shadow:0 1px 2px rgba(30,40,25,.05), 0 18px 46px rgba(30,40,25,.13);
}

/* A thin warm hairline along the top edge. It signals "there is more here"
   without the red-and-padlock vocabulary of an error state. */
.lock-card::before{
  content:''; display:block;
  height:2px; width:44px; margin:0 auto 1.05rem;
  border-radius:2px;
  background:linear-gradient(90deg,var(--moss),var(--amber));
}

.lock-eyebrow{
  display:inline-flex; align-items:center; gap:.4rem;
  font-size:.7rem; font-weight:600; letter-spacing:.14em;
  text-transform:uppercase; color:var(--moss);
  margin-bottom:.6rem;
}
.lock-eyebrow svg{ width:11px; height:11px; }

.lock-card h4{
  margin:0 0 .45rem;
  font-size:1.3rem; font-weight:500; line-height:1.25; color:var(--ink);
}
.lock-card h4 em{ font-style:italic; font-family:Georgia,'Times New Roman',serif; }
.lock-card p{
  margin:0 0 1.15rem;
  font-size:.9rem; line-height:1.55; color:var(--ink-soft);
}
.lock-foot{
  display:block; margin-top:.7rem;
  font-size:.72rem; color:var(--ink-soft); opacity:.85;
}

/* ---- skeleton rows -------------------------------------------------------
   Under the fade sit grey bars, never numbers. Fabricated readings on a page
   whose entire claim is "these are real measurements" would be a poor trade
   for a fuller-looking screenshot. A skeleton says "rows continue" and lies
   about nothing. */
.skel-row{
  display:grid; grid-template-columns:1.3fr 1fr 1fr 1fr 1fr;
  gap:1rem; padding:.72rem .9rem;
  border-top:1px solid var(--line);
}
.skel-row span{
  height:9px; border-radius:5px;
  background:linear-gradient(90deg,#e8e5db,#efece3,#e8e5db);
  background-size:200% 100%;
  animation:skel 2.4s ease-in-out infinite;
}
.skel-row span:nth-child(2){ width:62%; }
.skel-row span:nth-child(3){ width:48%; }
.skel-row span:nth-child(4){ width:70%; }
.skel-row span:nth-child(5){ width:40%; }
@keyframes skel{ 0%,100%{background-position:0% 0} 50%{background-position:-140% 0} }
@media (prefers-reduced-motion:reduce){ .skel-row span{ animation:none; } }

/* ---- readings table ---------------------------------------------------- */
.rt-wrap{ overflow:hidden; border-radius:var(--r); }
body.ag-in .rt-wrap{ max-height:560px; overflow-y:auto; }
.rt{ width:100%; border-collapse:collapse; font-size:.87rem; }
.rt thead th{
  position:sticky; top:0; z-index:1;
  background:var(--card);
  text-align:left; font-weight:600;
  font-size:.68rem; letter-spacing:.13em; text-transform:uppercase;
  color:var(--ink-soft);
  padding:.85rem .9rem;
  border-bottom:1px solid var(--line);
}
.rt tbody td{
  padding:.72rem .9rem;
  border-top:1px solid var(--line);
  color:var(--ink);
  font-variant-numeric:tabular-nums;
}
.rt tbody tr:hover td{ background:rgba(92,115,64,.045); }
.rt .t{ color:var(--ink-soft); white-space:nowrap; }
.rt .muted{ color:var(--ink-soft); opacity:.5; }

/* ---- images grid ------------------------------------------------------- */
.img-grid{
  display:grid; gap:.6rem;
  grid-template-columns:repeat(auto-fill,minmax(132px,1fr));
}
.img-cell{
  aspect-ratio:4/3; border-radius:10px;
  background:linear-gradient(135deg,#dfe3d4,#eceadf 55%,#d9ded0);
  border:1px solid var(--line);
}
.img-cell:nth-child(3n){ background:linear-gradient(135deg,#e3e6d8,#f0eee5 50%,#dde2d2); }
.img-cell:nth-child(4n){ background:linear-gradient(160deg,#dae0cf,#eceadf); }

/* ---- range picker ------------------------------------------------------ */
.range .locked{
  position:relative;
  opacity:.5;
  padding-right:1.55rem;
  cursor:pointer;
}
.range .locked::after{
  content:''; position:absolute; right:.6rem; top:50%;
  width:9px; height:11px; margin-top:-5.5px;
  background:currentColor;
  -webkit-mask:var(--lockmask) center/contain no-repeat;
  mask:var(--lockmask) center/contain no-repeat;
}
body.ag-in .range .locked{ opacity:1; padding-right:.95rem; cursor:auto; }
body.ag-in .range .locked::after{ display:none; }
:root{
  --lockmask:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cpath d='M6 10V8a6 6 0 0 1 12 0v2h1a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1zm2 0h8V8a4 4 0 0 0-8 0z'/%3E%3C/svg%3E");
}

/* ---- signed-in only / signed-out only ---------------------------------- */
.only-in{ display:none; }
body.ag-in .only-in{ display:revert; }
body.ag-in .only-out{ display:none; }

/* A signed-in visitor gets a quiet confirmation rather than a banner. They
   asked to see more, they can see more; announcing it twice is noise. */
.member-chip{
  display:inline-flex; align-items:center; gap:.45rem;
  padding:.3rem .72rem;
  font-size:.72rem; letter-spacing:.06em; color:var(--moss);
  background:rgba(92,115,64,.09);
  border:1px solid rgba(92,115,64,.2);
  border-radius:999px;
}
.member-chip::before{
  content:''; width:5px; height:5px; border-radius:50%;
  background:var(--moss);
}
</style>
'''

patch(TPL, "index.html — lock styling", marker="members' view",
      anchor="</head>", new=LOCK_CSS + "</head>")


RANGE_NEW = '''    <div class="range reveal">
      {% for r in ranges %}
        {% if r.member and not member %}
      {#- Still an <a> with a real href, so it looks and reads like the others.
          data-ag-signin makes the widget swallow the click and open the modal
          instead. Without that the link would still be harmless - the server
          clamps the range back down for a stranger - but landing on the same
          seven days with no explanation feels broken rather than gated. -#}
      <a href="?hours={{ r.hours }}" class="locked" data-ag-signin>{{ r.label }}</a>
        {% else %}
      <a href="?hours={{ r.hours }}" class="{{ 'on' if hours == r.hours }}">{{ r.label }}</a>
        {% endif %}
      {% endfor %}
      {% if member %}<span class="member-chip">Full access</span>{% endif %}
    </div>
'''

patch(TPL, "index.html — range picker", marker="{% for r in ranges %}",
      anchor='''    <div class="range reveal">
      <a href="?hours=24"  class="{{ 'on' if hours == 24 }}">24 hours</a>
      <a href="?hours=72"  class="{{ 'on' if hours == 72 }}">3 days</a>
      <a href="?hours=168" class="{{ 'on' if hours == 168 }}">7 days</a>
      <a href="{{ deeper.api_url }}" rel="nofollow">Full history →</a>
    </div>
''', new=RANGE_NEW)


NEW_SECTIONS = '''
<!-- ============================================ every reading ============ -->
<section class="section">
  <div class="container">
    <div class="section-head reveal">
      <span class="eyebrow">The record</span>
      <h2>Every reading, <span class="serif">as it landed.</span></h2>
      <p class="lede">
        {%- if member and span.available %}
          {{ "{:,}".format(span.readings) }} readings since {{ span.since }}.
        {%- else %}
          One row per minute, straight from the station.
        {%- endif -%}
      </p>
    </div>

    <div class="card lockable reveal" style="padding:0;overflow:hidden">
      <div class="lock-body">
        <div class="rt-wrap">
          <table class="rt">
            <thead>
              <tr><th>Time</th><th>Air</th><th>Humidity</th>
                  <th>Dew point</th><th>Leaf</th></tr>
            </thead>
            <tbody>
            {% for r in table %}
              <tr>
                <td class="t">{{ r.when }}</td>
                <td>{% if r.air is not none %}{{ r.air|round(1) }} °C{% else %}<span class="muted">—</span>{% endif %}</td>
                <td>{% if r.hum is not none %}{{ r.hum|round(0)|int }} %{% else %}<span class="muted">—</span>{% endif %}</td>
                <td>{% if r.dew is not none %}{{ r.dew|round(1) }} °C{% else %}<span class="muted">—</span>{% endif %}</td>
                <td>{% if r.wet is not none %}{{ r.wet|round(0)|int }}{% else %}<span class="muted">—</span>{% endif %}</td>
              </tr>
            {% endfor %}
            </tbody>
          </table>

          <!-- Only drawn for a stranger, and only ever grey bars. -->
          <div class="only-out">
            {% for _ in range(7) %}
            <div class="skel-row"><span></span><span></span><span></span><span></span><span></span></div>
            {% endfor %}
          </div>
        </div>
      </div>

      <div class="lock-veil">
        <div class="lock-card">
          <span class="lock-eyebrow">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 10V8a6 6 0 0 1 12 0v2h1a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1zm2 0h8V8a4 4 0 0 0-8 0z"/></svg>
            Members
          </span>
          <h4>
            {%- if span.available and span.extra_days > 0 -%}
              Another <em>{{ span.extra_days }} days</em> of this.
            {%- else -%}
              The <em>whole table.</em>
            {%- endif -%}
          </h4>
          <p>
            {%- if span.available -%}
              Every one of the {{ "{:,}".format(span.readings) }} readings taken
              since {{ span.since }}, sortable, alongside the canopy photographs.
            {%- else -%}
              The complete record, alongside the canopy photographs.
            {%- endif -%}
          </p>
          <button class="btn btn-greenglass" data-ag-signin type="button">
            Sign in with Google
          </button>
          <span class="lock-foot">Free. Your name and email, nothing else.</span>
        </div>
      </div>
    </div>
  </div>
</section>


<!-- ============================================ the canopy =============== -->
<section class="section">
  <div class="container">
    <div class="section-head reveal">
      <span class="eyebrow">From the canopy</span>
      <h2>What the camera <span class="serif">sees.</span></h2>
      <p class="lede">A photograph of the same leaves, every fifteen minutes,
        through the season.</p>
    </div>

    <div class="card lockable reveal">
      <div class="lock-body">
        <div class="img-grid">
          {% for _ in range(12) %}<div class="img-cell"></div>{% endfor %}
        </div>
      </div>

      <div class="lock-veil">
        <div class="lock-card">
          <span class="lock-eyebrow">
            <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 10V8a6 6 0 0 1 12 0v2h1a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V11a1 1 0 0 1 1-1zm2 0h8V8a4 4 0 0 0-8 0z"/></svg>
            Members
          </span>
          <h4>The field, <em>photographed.</em></h4>
          <p>Sign in to ask for access to the canopy images. Requests are
            approved by hand, usually the same day.</p>
          <button class="btn btn-greenglass" data-ag-signin type="button">
            Sign in with Google
          </button>
          <span class="lock-foot">Free. Your name and email, nothing else.</span>
        </div>
      </div>
    </div>

    <div class="only-in" style="margin-top:1.1rem;text-align:center">
      <a class="btn btn-greenglass" href="/photos">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        Open the gallery
      </a>
    </div>
  </div>
</section>
'''

patch(TPL, "index.html — readings table and canopy sections",
      marker="Every reading, <span class=\"serif\">as it landed.",
      anchor='''<section class="section">
  <div class="container">
    <div class="connect reveal">
      <h3>Beyond <span class="serif">{{ deeper.max_days }} days.</span></h3>''',
      new=NEW_SECTIONS + '''
<section class="section">
  <div class="container">
    <div class="connect reveal only-out">
      <h3>Beyond <span class="serif">{{ deeper.max_days }} days.</span></h3>''')


# ------------------------------------------------------------------ report --

print()
for a in applied:
    print(f"  applied   {a}")
for s in skipped:
    print(f"  skipped   {s}")
print()
if any("ANCHOR NOT FOUND" in s for s in skipped):
    print("  An anchor did not match, so that one change was not made. Nothing")
    print("  is half-written - each change is all-or-nothing. Send me the file")
    print("  and I will re-anchor it.")
else:
    print("  Now:  sudo systemctl restart agrisync-dashboard")
print()
print("  Backups: dashboard/data.py.bak  dashboard/templates/index.html.bak")
print()
