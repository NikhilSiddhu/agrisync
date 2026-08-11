#!/usr/bin/env python3
"""Second dashboard pass: hide the API host, fold the offers into the locks,
and add an admin strip that only exists for you.

Put this in ~/agrisync and run:

    cd ~/agrisync && python3 patch_dashboard_2.py

Run patch_dashboard.py first. This one edits what that one produced.


ON "IMPOSSIBLE TO SEE EVEN IF THEY INSPECT"
    That is achievable, and this does it - but it is worth being precise about
    which half does the work.

    Jinja runs on the Pi. `{% if is_admin %}` blocks are never rendered into
    the response for anybody else, so there is no hidden element, no
    display:none, and nothing in View Source or the DOM inspector. A visitor
    cannot unhide what was never sent. That part is real.

    What it is NOT is the protection. Anyone can type /photos/download-all into
    the address bar without ever having seen a button. The thing that stops
    them is _require_admin() on the server, which this does not touch because
    it is already there.

    So: the markup is genuinely invisible, and the endpoint is genuinely
    guarded, and those are two separate mechanisms. Relying on only the first
    would be security by obscurity. Relying on only the second would work but
    would show strangers buttons that 403 at them.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
TPL = ROOT / "dashboard" / "templates" / "index.html"

applied, skipped = [], []


def patch(label, anchor, new, *, marker):
    text = TPL.read_text()
    if marker in text:
        skipped.append(f"{label} — already applied")
        return
    if anchor not in text:
        skipped.append(f"{label} — ANCHOR NOT FOUND, tell me")
        return
    TPL.write_text(text.replace(anchor, new, 1))
    applied.append(label)


if not TPL.is_file():
    sys.exit(f"cannot find {TPL}\nRun this from ~/agrisync.")
if "lock-veil" not in TPL.read_text():
    sys.exit("run patch_dashboard.py first - this builds on it.")
shutil.copy(TPL, TPL.with_suffix(".html.bak2"))


# 1 ---------------------------------------------------- kill the API link ---
# The old block advertised api.agrisync.tech to every logged-out visitor. The
# hostname should not appear anywhere a stranger can read it: it is the admin
# surface, and naming it is a free hint about where to push. Replaced with the
# social links, which is what that space is actually for.

patch("footer CTA — remove the API link", marker="foot-cta-social",
      anchor='''    <div class="connect reveal only-out">
      <h3>Beyond <span class="serif">{{ deeper.max_days }} days.</span></h3>
      <p>{{ deeper.note }}</p>
      <a class="btn btn-greenglass" href="{{ deeper.api_url }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        Connect
      </a>
    </div>''',
      new='''    <div class="connect reveal foot-cta-social">
      <h3>Built in the <span class="serif">open.</span></h3>
      <p>A solar field station in Hapur, measuring cabbage microclimate at the
        leaf. Notes, failures and progress as it goes up.</p>
      <div style="display:flex;gap:.6rem;justify-content:center;flex-wrap:wrap">
        <a class="btn btn-greenglass"
           href="https://www.linkedin.com/in/nikhil-siddhu-411406366"
           target="_blank" rel="noopener">
          <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4.98 3.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5zM3 9h4v12H3zM9 9h3.8v1.7h.05c.53-1 1.83-2.05 3.76-2.05C20.4 8.65 21 11 21 14.2V21h-4v-6c0-1.43-.03-3.27-2-3.27-2 0-2.3 1.56-2.3 3.17V21H9z"/></svg>
          LinkedIn
        </a>
        <a class="btn btn-glass" href="https://x.com/" target="_blank" rel="noopener">
          <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M18.9 2H22l-7 8 8.2 12H16l-5-7.3L5.3 22H2l7.5-8.6L1.7 2H9l4.6 6.7zm-1.1 18h1.7L7.3 3.8H5.5z"/></svg>
          X
        </a>
      </div>
    </div>''')


# 2 ------------------------------------------- fold the offers into the lock --
# Two separate "request the CSV" and "see the images" buttons on a logged-out
# page ask a stranger to make a decision before they have any reason to care.
# The offer belongs inside the thing they are already looking at, as the reason
# to sign in - not as a competing call to action beside it.

patch("table lock — mention the CSV and photos", marker="lock-includes",
      anchor='''          <span class="lock-foot">Free. Your name and email, nothing else.</span>
        </div>
      </div>
    </div>
  </div>
</section>


<!-- ============================================ the canopy =============== -->''',
      new='''          <ul class="lock-includes">
            <li>The full table, every reading since the station went up</li>
            <li>The raw CSV, yours to download</li>
            <li>The canopy photographs, on request</li>
          </ul>
          <span class="lock-foot">Free. Your name and email, nothing else.</span>
        </div>
      </div>
    </div>
  </div>
</section>


<!-- ============================================ the canopy =============== -->''')


# 3 --------------------------------------------- signed-in action buttons ---

patch("signed-in actions", marker="member-actions",
      anchor='''    <div class="only-in" style="margin-top:1.1rem;text-align:center">
      <a class="btn btn-greenglass" href="/photos">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
        Open the gallery
      </a>
    </div>''',
      new='''    <div class="only-in member-actions">
      <a class="btn btn-greenglass" href="{{ links.photos }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 5h16v14H4z"/><path d="M4 15l4-4 3 3 3-3 6 5"/></svg>
        Canopy photographs
      </a>
      <a class="btn btn-glass" href="{{ links.csv }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 19h16"/></svg>
        Download the CSV
      </a>
    </div>

    {#- ------------------------------------------------------------------
        ADMIN ONLY.

        is_admin is computed on the Pi from a signed cookie. For every other
        visitor Jinja skips this block entirely, so none of it reaches the
        browser - not hidden, not styled away, simply absent from the
        response. There is nothing here for a stranger to find in View Source.

        The endpoints behind these links check admin status again on their own.
        That check, not this one, is what actually stops anybody reaching them.
        -------------------------------------------------------------------#}
    {% if is_admin %}
    <div class="admin-strip">
      <span class="admin-tag">Admin</span>
      <a href="{{ links.visitors }}">Visitor register</a>
      <a href="{{ links.requests }}">Pending requests</a>
      <a href="{{ links.zip }}">Download all images</a>
      <a href="{{ links.photos }}">Manage &amp; delete</a>
    </div>
    {% endif %}''')


# 4 ------------------------------------------------------------- styling ---

patch("styling for the new pieces", marker="lock-includes{",
      anchor="</head>", new='''
<style>
/* Bulleted proof inside the lock card. Three concrete things beat one vague
   "and more" - the reason to sign in has to be legible in two seconds. */
.lock-includes{
  list-style:none; margin:0 0 1.1rem; padding:0;
  text-align:left; display:inline-block;
}
.lock-includes li{
  position:relative; padding-left:1.35rem; margin:.34rem 0;
  font-size:.845rem; line-height:1.45; color:var(--ink-soft);
}
.lock-includes li::before{
  content:''; position:absolute; left:.15rem; top:.52em;
  width:5px; height:5px; border-radius:50%; background:var(--moss);
}

.member-actions{
  margin-top:1.2rem; display:flex; gap:.6rem;
  justify-content:center; flex-wrap:wrap;
}

/* Deliberately quiet. These are tools, not an achievement - and this strip is
   the only thing on the page that says the admin surface exists at all, so it
   should not shout. */
.admin-strip{
  margin:1.5rem auto 0; max-width:640px;
  display:flex; align-items:center; gap:.35rem 1.1rem;
  flex-wrap:wrap; justify-content:center;
  padding:.75rem 1rem;
  border:1px dashed var(--line); border-radius:12px;
  background:rgba(157,123,47,.045);
}
.admin-tag{
  font-size:.63rem; font-weight:600; letter-spacing:.15em;
  text-transform:uppercase; color:var(--amber);
}
.admin-strip a{
  font-size:.82rem; color:var(--ink-soft);
  text-decoration:none; border-bottom:1px solid transparent;
}
.admin-strip a:hover{ color:var(--ink); border-bottom-color:var(--moss); }
</style>
</head>''')


# 5 ------------------------------------------------- stale comment leak ----
# An HTML comment naming api.agrisync.tech. Comments are not stripped - they
# ship to the browser and sit in View Source. It was also out of date: the
# widget talks to ingest, not api.

patch("remove API hostname from a comment", marker="asks the ingest host",
      anchor="On load it asks api.agrisync.tech who you are.",
      new="On load it asks the ingest host who you are.")


print()
for a in applied:
    print(f"  applied   {a}")
for s in skipped:
    print(f"  skipped   {s}")
print()
if any("ANCHOR NOT FOUND" in s for s in skipped):
    print("  An anchor missed. Nothing half-written. Send me the file.")
else:
    print("  Now:  sudo systemctl restart agrisync-dashboard")
print()
print(f"  Backup: {TPL.name}.bak2")
print()
