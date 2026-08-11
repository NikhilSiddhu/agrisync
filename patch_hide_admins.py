#!/usr/bin/env python3
"""Keep admin accounts out of the visitor register and the request queue.

    cd ~/agrisync && python3 patch_hide_admins.py && sudo systemctl restart agrisync-api

WHAT IT DOES
    /admin/visitors and /admin/requests both render visitors.html with the same
    four calls. This replaces both with one helper that strips out anybody in
    ADMIN_EMAILS first.

FILTERED, NOT DELETED
    Your rows stay in MongoDB. They are real visits - 44 of them - and removing
    them would quietly corrupt every count and first-seen date that derives
    from the collection. The register is a view, so the right place to leave
    yourself out is the view.

    It also means turning this off later is a one-line change rather than an
    attempt to reconstruct data that no longer exists.

THE HEADLINE COUNT
    "5 people so far" has to drop to 3, or hiding the rows just makes the page
    contradict itself. The helper subtracts however many admins it actually
    removed rather than assuming a fixed number, so adding a third admin email
    needs no further edit.

ALSO FIXED
    visitors.html says "Email is verified by Cloudflare and never typed."
    Google verifies it now. That sentence is a claim about how you handle
    people's data, so it should not be stale.
"""
import ast
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
VIEWS = ROOT / "src" / "api" / "views.py"

if not VIEWS.is_file():
    sys.exit(f"cannot find {VIEWS}\nRun this from ~/agrisync.")

text = VIEWS.read_text()

if "_render_register" in text:
    sys.exit("already applied.")

RENDER_BLOCK = '''    return render_template("visitors.html",
                           visitors=visitors.listing(),
                           requests=visitors.requests_listing(),
                           total=visitors.count(),
                           identity=ident,
                           display_name=visitors.display_name(ident["email"]),
                           logout_url=_logout_url())'''

found = text.count(RENDER_BLOCK)
if found != 2:
    sys.exit(f"expected 2 copies of the render block, found {found}. "
             "Send me views.py and I will re-anchor.")

HELPER = '''def _not_admin(rows):
    """Drop anybody in ADMIN_EMAILS from a list of visitor or request rows.

    Compares lowercased, because an address typed into .env with different
    capitalisation is the same mailbox and would otherwise slip through.
    """
    out = []
    for r in rows or []:
        email = (r.get("email") or "").strip().lower()
        if email and access.is_admin({"email": email}):
            continue
        out.append(r)
    return out


def _render_register(ident):
    """The visitor register and request queue, with admins filtered out.

    Both admin routes render the same page, so they share this rather than
    keeping two copies of the same six arguments in step by hand.
    """
    all_visitors = visitors.listing()
    shown = _not_admin(all_visitors)

    # Subtract the ones actually removed. Hiding rows while leaving the
    # headline at 5 would just make the page argue with itself.
    hidden = len(all_visitors) - len(shown)
    total = max(0, visitors.count() - hidden)

    return render_template("visitors.html",
                           visitors=shown,
                           requests=_not_admin(visitors.requests_listing()),
                           total=total,
                           identity=ident,
                           display_name=visitors.display_name(ident["email"]),
                           logout_url=_logout_url())


'''

# Put the helpers above the first route that uses them.
anchor = '@bp.get("/admin/visitors")'
if anchor not in text:
    sys.exit("could not find the /admin/visitors route. Send me views.py.")

text = text.replace(RENDER_BLOCK, "    return _render_register(ident)")
text = text.replace(anchor, HELPER + anchor, 1)

try:
    ast.parse(text)
except SyntaxError as exc:
    sys.exit(f"result would not parse ({exc}). Nothing written.")

shutil.copy(VIEWS, VIEWS.with_suffix(".py.bak2"))
VIEWS.write_text(text)
print("  views.py — admins filtered from both tables, backup at views.py.bak2")

# ---------------------------------------------------------- stale copy ------

for tpl in (ROOT / "main" / "templates" / "visitors.html",
            ROOT / "src" / "templates" / "visitors.html",
            ROOT / "templates" / "visitors.html"):
    if not tpl.is_file():
        continue
    t = tpl.read_text()
    if "verified by Cloudflare" in t:
        shutil.copy(tpl, tpl.with_suffix(".html.bak"))
        tpl.write_text(t.replace("verified by Cloudflare", "verified by Google"))
        print(f"  {tpl.name} — 'verified by Cloudflare' corrected to Google")
    else:
        print(f"  {tpl.name} — no stale Cloudflare wording found")
    break
else:
    print("  could not find visitors.html - fix the Cloudflare wording by hand")

print()
print("  Now:  sudo systemctl restart agrisync-api")
print()
