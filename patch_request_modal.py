#!/usr/bin/env python3
"""Turn the access request into a popup, for both the photos and the CSV.

    cd ~/agrisync && python3 patch_request_modal.py
    # then update the tunnel (see the note printed at the end)
    sudo systemctl restart agrisync-api agrisync-dashboard


WHAT CHANGES
    Before: clicking "Canopy photographs" while unapproved threw you onto a
    separate page with a bare form on it. Different layout, different header,
    and you had to find your way back.

    After: the button opens a modal in the same visual language as the sign-in
    popup. Ask, get told you are on the list, carry on reading. Nothing
    navigates.


THE THREE STATES

    Not asked yet   a short form - a number and a sentence, both optional,
                    because Google already gave us the name and the verified
                    email and asking twice for what we have is just friction

    Just asked      "You're on the list" with what happens next. This is the
                    moment someone wonders whether the click worked, so it
                    gets a real confirmation rather than a silent redirect

    Already asked   "Already on the list", with the date. Stops the second
                    click feeling like the first one failed


THE URL FALLBACK
    Typing /photos or /export/readings.csv directly used to hit a JSON 403 -
    a raw error blob with no way forward, which is what you saw.

    Now the server sends them back to the dashboard with ?request=photos or
    ?request=data, and the modal opens on arrival in whichever of the three
    states fits. Someone who has already asked lands on "already on the list"
    rather than being invited to ask again.


ONE THING TO WATCH
    /request-data does not match the tunnel's current path rule, so the CSV
    request would 404 until the ingress regex is updated. The command is
    printed at the end - it will not work without it.
"""
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
TPL = ROOT / "dashboard" / "templates" / "index.html"
PHOTOS = ROOT / "src" / "api" / "photos.py"
VIEWS = ROOT / "src" / "api" / "views.py"

for f in (TPL, PHOTOS, VIEWS):
    if not f.is_file():
        sys.exit(f"cannot find {f}\nRun this from ~/agrisync.")

applied, skipped = [], []


# ==================================================== the modal =============

MODAL = r'''
<!-- ============================================ access request modal ===== -->
<div class="rq-back" id="rqBack" aria-hidden="true" role="dialog" aria-modal="true">
  <div class="rq-card">
    <button class="rq-x" id="rqClose" aria-label="Close">&times;</button>
    <div id="rqBody"></div>
  </div>
</div>

<style>
/* Same vocabulary as the sign-in popup on purpose. A visitor should not be
   able to tell that one of these is the auth widget and the other is ours. */
.rq-back{
  position:fixed; inset:0; z-index:9999;
  display:none; align-items:center; justify-content:center;
  padding:1.25rem;
  background:rgba(32,39,31,.5);
  backdrop-filter:blur(6px); -webkit-backdrop-filter:blur(6px);
}
.rq-back.open{ display:flex; }
.rq-card{
  position:relative; width:100%; max-width:432px;
  padding:2rem 1.9rem 1.75rem;
  background:var(--card,#fbfaf6);
  border:1px solid var(--line,#e4e1d6);
  border-radius:20px;
  box-shadow:0 30px 70px rgba(30,40,25,.3);
  animation:rqIn .22s cubic-bezier(.2,.8,.3,1);
}
@keyframes rqIn{ from{ opacity:0; transform:translateY(10px) scale(.985) } }
@media (prefers-reduced-motion:reduce){ .rq-card{ animation:none } }

.rq-x{
  position:absolute; top:.85rem; right:1rem;
  background:none; border:0; cursor:pointer;
  font-size:1.5rem; line-height:1; color:var(--ink-soft,#5f655a);
  padding:.15rem .4rem; border-radius:8px;
}
.rq-x:hover{ background:rgba(32,39,31,.06); color:var(--ink,#20271f); }

.rq-eyebrow{
  display:block; margin-bottom:.55rem;
  font-size:.68rem; font-weight:600; letter-spacing:.15em;
  text-transform:uppercase; color:var(--moss,#5c7340);
}
.rq-card h3{
  margin:0 0 .5rem; font-size:1.35rem; font-weight:500;
  line-height:1.25; color:var(--ink,#20271f);
}
.rq-card h3 em{ font-style:italic; font-family:Georgia,'Times New Roman',serif; }
.rq-card p{
  margin:0 0 1.15rem; font-size:.9rem; line-height:1.6;
  color:var(--ink-soft,#5f655a);
}
.rq-who{
  display:inline-block; margin-bottom:1.15rem; padding:.32rem .7rem;
  font-size:.8rem; color:var(--ink,#20271f);
  background:rgba(92,115,64,.08);
  border:1px solid rgba(92,115,64,.18); border-radius:999px;
}
.rq-l{
  display:block; margin-bottom:.35rem;
  font-size:.78rem; color:var(--ink-soft,#5f655a);
}
.rq-in, .rq-ta{
  width:100%; box-sizing:border-box; margin-bottom:.9rem;
  padding:.62rem .8rem;
  font:inherit; font-size:.88rem; color:var(--ink,#20271f);
  background:#fff;
  border:1px solid var(--line,#e4e1d6); border-radius:11px;
}
.rq-ta{ min-height:74px; resize:vertical; }
.rq-in:focus, .rq-ta:focus{
  outline:none; border-color:var(--moss,#5c7340);
  box-shadow:0 0 0 3px rgba(92,115,64,.14);
}
.rq-go{
  width:100%; padding:.72rem 1rem;
  font:inherit; font-size:.92rem; font-weight:500; letter-spacing:.02em;
  color:#f4f2ec; background:var(--green,#3f5e39);
  border:0; border-radius:999px; cursor:pointer;
}
.rq-go:hover{ background:var(--green-deep,#26331f); }
.rq-go[disabled]{ opacity:.55; cursor:default; }
.rq-note{ margin:.75rem 0 0; font-size:.75rem; text-align:center; opacity:.85; }
.rq-err{ color:var(--rust,#96483c); font-size:.83rem; margin:.4rem 0 0; }

.rq-tick{
  width:46px; height:46px; margin:0 auto 1.05rem;
  display:flex; align-items:center; justify-content:center;
  border-radius:50%; background:rgba(92,115,64,.13);
  color:var(--moss,#5c7340); font-size:1.35rem;
}
.rq-mid{ text-align:center; }
.rq-mid p{ margin-bottom:1.35rem; }
</style>

<script>
(function () {
  var back = document.getElementById('rqBack');
  var body = document.getElementById('rqBody');
  if (!back) return;

  var API = 'https://ingest.agrisync.tech';
  var me  = { signed_in:false };
  var scope = 'photos';

  // The auth widget already asks who you are on every page load, so listening
  // to it beats a second identical request.
  document.addEventListener('ag:auth', function (e) { me = e.detail || me; });

  var COPY = {
    photos: { title:'The canopy <em>photographs.</em>',
              blurb:'A photograph of the same leaves every fifteen minutes. Access is granted by hand, usually the same day.',
              url:'/photos', post:'/photos/request' },
    data:   { title:'The raw <em>dataset.</em>',
              blurb:'Every reading since the station went up, as CSV, streamed live rather than a stale snapshot. Granted by hand, usually the same day.',
              url:'/export/readings.csv', post:'/request-data' }
  };

  function esc(s){ return String(s||'').replace(/[&<>"']/g, function(c){
    return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  function open(s){ scope = s; back.classList.add('open');
    back.setAttribute('aria-hidden','false'); draw(); }
  function close(){ back.classList.remove('open');
    back.setAttribute('aria-hidden','true');
    // Drop ?request= so a refresh does not reopen it.
    if (/[?&]request=/.test(location.search))
      history.replaceState({}, '', location.pathname);
  }

  document.getElementById('rqClose').addEventListener('click', close);
  back.addEventListener('click', function(e){ if (e.target === back) close(); });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && back.classList.contains('open')) close(); });

  function draw(){
    var c = COPY[scope];

    if (!me.signed_in) {
      body.innerHTML =
        '<span class="rq-eyebrow">Members</span>' +
        '<h3>' + c.title + '</h3>' +
        '<p>' + c.blurb + ' Sign in first so I know who is asking.</p>' +
        '<button class="rq-go" data-ag-signin type="button">Sign in with Google</button>' +
        '<p class="rq-note">Free. Your name and email, nothing else.</p>';
      return;
    }

    if (me.is_granted) { location.href = c.url; return; }

    if (me.has_requested) {
      body.innerHTML =
        '<div class="rq-mid">' +
        '<div class="rq-tick">&#10003;</div>' +
        '<h3>Already on the <em>list.</em></h3>' +
        '<p>Your request is in and waiting on me. Nothing more to do — I approve these by hand, usually the same day, and you will not need to ask again.</p>' +
        '<button class="rq-go" id="rqOk" type="button">Close</button></div>';
      document.getElementById('rqOk').addEventListener('click', close);
      return;
    }

    body.innerHTML =
      '<span class="rq-eyebrow">Request access</span>' +
      '<h3>' + c.title + '</h3>' +
      '<p>' + c.blurb + '</p>' +
      '<span class="rq-who">' + esc(me.email) + '</span>' +
      '<label class="rq-l" for="rqPhone">Phone, if you would not mind (optional)</label>' +
      '<input class="rq-in" id="rqPhone" type="tel" inputmode="numeric" placeholder="10 digits" autocomplete="tel">' +
      '<label class="rq-l" for="rqWhy">What are you hoping to use it for? (optional)</label>' +
      '<textarea class="rq-ta" id="rqWhy" placeholder="A sentence is plenty."></textarea>' +
      '<button class="rq-go" id="rqSend" type="button">Add me to the list</button>' +
      '<p class="rq-err" id="rqErr" hidden></p>' +
      '<p class="rq-note">Your name and verified email came from Google. Nothing else is stored unless you type it here.</p>';

    document.getElementById('rqSend').addEventListener('click', send);
  }

  function send(){
    var btn = document.getElementById('rqSend');
    var err = document.getElementById('rqErr');
    var phone = (document.getElementById('rqPhone').value || '').trim();
    var why   = (document.getElementById('rqWhy').value || '').trim();

    btn.disabled = true; btn.textContent = 'Sending…'; err.hidden = true;

    var fd = new FormData();
    if (phone) fd.append('phone', phone);
    if (why)   { fd.append('purpose', why); fd.append('note', why); }
    if (me.name) fd.append('name', me.name);

    fetch(API + COPY[scope].post, {
      method:'POST', credentials:'include', body:fd
    }).then(function(r){
      // /photos/request answers with a redirect, /request-data with JSON.
      // Either is a success; only a 4xx is not.
      if (r.status >= 400) return r.json().catch(function(){ return {}; })
        .then(function(j){ throw new Error(j.error || 'That did not go through.'); });
      me.has_requested = true;
      body.innerHTML =
        '<div class="rq-mid">' +
        '<div class="rq-tick">&#10003;</div>' +
        '<h3>You are on the <em>list.</em></h3>' +
        '<p>I review these by hand, usually the same day. Once it is approved this opens for you automatically — no second request, no email to watch for.</p>' +
        '<button class="rq-go" id="rqOk" type="button">Back to the data</button></div>';
      document.getElementById('rqOk').addEventListener('click', close);
    }).catch(function(e){
      btn.disabled = false; btn.textContent = 'Add me to the list';
      err.textContent = e.message || 'That did not go through.';
      err.hidden = false;
    });
  }

  // Buttons open the modal instead of navigating, unless already approved.
  document.addEventListener('click', function(e){
    var a = e.target.closest && e.target.closest('[data-ag-request]');
    if (!a) return;
    if (me.signed_in && me.is_granted) return;   // let the link work
    e.preventDefault();
    open(a.getAttribute('data-ag-request') || 'photos');
  });

  // Arrived from a URL the server turned away.
  function fromUrl(){
    var m = /[?&]request=(photos|data)/.exec(location.search);
    if (m) open(m[1]);
  }
  document.addEventListener('ag:auth', fromUrl, { once:true });
  setTimeout(fromUrl, 2500);   // in case the auth call never resolves
})();
</script>
'''

t = TPL.read_text()
if "rq-back" in t:
    skipped.append("index.html — modal (already applied)")
elif "</body>" not in t:
    skipped.append("index.html — modal (no </body> found)")
else:
    t = t.replace("</body>", MODAL + "\n</body>", 1)
    applied.append("index.html — request modal")

OLD_BTNS = '''      <a class="btn btn-greenglass" href="{{ links.photos }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 5h16v14H4z"/><path d="M4 15l4-4 3 3 3-3 6 5"/></svg>
        Canopy photographs
      </a>
      <a class="btn btn-glass" href="{{ links.csv }}">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 19h16"/></svg>
        Download the CSV
      </a>'''

NEW_BTNS = '''      {#- Still real links with real hrefs, so they work without JavaScript
          and the server decides either way. data-ag-request makes the script
          intercept the click and open the modal when access has not been
          granted yet. -#}
      <a class="btn btn-greenglass" href="{{ links.photos }}" data-ag-request="photos">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 5h16v14H4z"/><path d="M4 15l4-4 3 3 3-3 6 5"/></svg>
        Canopy photographs
      </a>
      <a class="btn btn-glass" href="{{ links.csv }}" data-ag-request="data">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v12m0 0l-4-4m4 4l4-4M4 19h16"/></svg>
        Download the CSV
      </a>'''

# Marker must be the ATTRIBUTE WITH ITS VALUE, not the bare name - the modal
# script above contains getAttribute('data-ag-request'), so a looser check
# matches its own JavaScript and skips the edit that matters.
if 'data-ag-request="photos"' in t:
    skipped.append("index.html — button hooks (already applied)")
elif OLD_BTNS not in t:
    skipped.append("index.html — button hooks (ANCHOR NOT FOUND)")
else:
    t = t.replace(OLD_BTNS, NEW_BTNS, 1)
    applied.append("index.html — button hooks")

if "rq-back" in t or "data-ag-request" in t:
    shutil.copy(TPL, TPL.with_suffix(".html.bak6"))
    TPL.write_text(t)


# =========================================== server-side fallbacks =========

DASH = "https://dashboard.agrisync.tech"

p = PHOTOS.read_text()
if 'request=photos' in p:
    skipped.append("photos.py — URL fallback (already applied)")
else:
    old = '''    if visitors.has_requested(email):
        return render_template("photos.html", state="pending", email=email)

    return render_template("photos.html", state="request", email=email)'''
    new = f'''    # Not approved. Send them back to the dashboard rather than showing a
    # bare form on a page with no header and no way back. The modal there
    # opens in the right state - "already on the list" if they have asked,
    # the form if they have not.
    return redirect("{DASH}/?request=photos")'''
    if old not in p:
        skipped.append("photos.py — URL fallback (ANCHOR NOT FOUND)")
    else:
        p = p.replace(old, new, 1)
        try:
            ast.parse(p)
        except SyntaxError as exc:
            sys.exit(f"photos.py would not parse ({exc}). Nothing written.")
        shutil.copy(PHOTOS, PHOTOS.with_suffix(".py.bak6"))
        PHOTOS.write_text(p)
        applied.append("photos.py — URL fallback")

v = VIEWS.read_text()
if 'request=data' in v:
    skipped.append("views.py — CSV fallback (already applied)")
else:
    old = '''        return jsonify({"error": "not permitted",
                        "detail": "request the dataset from the history page"}), 403'''
    new = f'''        # A browser gets sent back to the dashboard with the modal open. A
        # script gets the 403 it can actually handle - redirecting an API
        # client to an HTML page is how you get a parser reporting success
        # while reading a sign-in screen.
        if "text/html" in (request.headers.get("Accept") or ""):
            return redirect("{DASH}/?request=data")
        return jsonify({{"error": "not permitted",
                        "detail": "request the dataset from the dashboard"}}), 403'''
    if old not in v:
        skipped.append("views.py — CSV fallback (ANCHOR NOT FOUND)")
    else:
        v = v.replace(old, new, 1)
        if "redirect" not in v.split("bp = Blueprint")[0]:
            v = v.replace("bp = Blueprint(\"views\", __name__)",
                          "from flask import redirect  # noqa: E402\n\n"
                          "bp = Blueprint(\"views\", __name__)", 1)
        try:
            ast.parse(v)
        except SyntaxError as exc:
            sys.exit(f"views.py would not parse ({exc}). Nothing written.")
        shutil.copy(VIEWS, VIEWS.with_suffix(".py.bak6"))
        VIEWS.write_text(v)
        applied.append("views.py — CSV fallback")


print()
for a in applied:
    print(f"  applied   {a}")
for s in skipped:
    print(f"  skipped   {s}")
print()
print("  THE TUNNEL STILL NEEDS UPDATING - /request-data does not match the")
print("  current path rule and the CSV request will 404 without this:")
print()
print("    sudo sed -i 's|admin\\|auth\\|export|admin\\\\|auth\\\\|export\\\\|request-data|' /etc/cloudflared/config.yml")
print()
print("  Safer to edit it by hand. The line should read:")
print("    path: ^/(photos|api|admin|auth|export|request-data)(/.*)?$")
print()
print("  Then:  sudo cloudflared tunnel ingress validate")
print("         sudo systemctl restart cloudflared agrisync-api agrisync-dashboard")
print()
