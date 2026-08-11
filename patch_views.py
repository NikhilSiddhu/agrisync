#!/usr/bin/env python3
"""Point views.py at whoami instead of Cloudflare Access.

    cd ~/agrisync && python3 patch_views.py

THE BUG
    views.py calls access.identity(request) in seven places. That reads the
    Cf-Access-Jwt-Assertion header, which Cloudflare Access injects.

    Traffic no longer goes through an Access application - it arrives on
    dashboard.agrisync.tech, where there is no Access policy and therefore no
    header. So identity() returns nothing, every admin route decides you are a
    stranger, and /admin/visitors answers "your email is not in ADMIN_EMAILS"
    while /auth/me cheerfully reports is_admin: true. Both are correct; they
    were just asking different systems.

THE FIX
    whoami.current() returns {"email", "name", "source"} or None - the same
    shape access.identity() returned, so nothing downstream needs changing. It
    tries the Google session first and falls back to Access, which means this
    keeps working either way and will keep working after Access is deleted.

WHY A SCRIPT
    Seven identical replacements plus an import. Doing that by hand is how a
    line ends up in the wrong place. Backs up first, refuses to run twice, and
    checks the result still parses before saving.
"""
import ast
import shutil
import sys
from pathlib import Path

TARGET = Path.cwd() / "src" / "api" / "views.py"

if not TARGET.is_file():
    sys.exit(f"cannot find {TARGET}\nRun this from ~/agrisync.")

original = TARGET.read_text()

if "whoami.current(request)" in original:
    sys.exit("already applied - views.py is using whoami.")

count = original.count("access.identity(request)")
if not count:
    sys.exit("found no access.identity(request) calls. Send me views.py.")

text = original.replace("access.identity(request)", "whoami.current(request)")

# The import goes immediately above the blueprint, which is after every other
# import in this file. Anchoring there rather than guessing a line number is
# what stops this landing in the middle of an existing statement.
anchor = 'bp = Blueprint("views", __name__)'
if anchor not in text:
    sys.exit("could not find the Blueprint line. Send me views.py.")

if "from src import whoami" not in text:
    text = text.replace(
        anchor,
        "# Identity now comes from the Google session, with Cloudflare Access\n"
        "# as a fallback while both exist. See src/whoami.py.\n"
        "from src import whoami\n\n" + anchor,
        1,
    )

try:
    ast.parse(text)
except SyntaxError as exc:
    sys.exit(f"result would not parse ({exc}). Nothing written.")

shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))
TARGET.write_text(text)

print()
print(f"  replaced {count} access.identity(request) calls with whoami.current(request)")
print("  added the whoami import")
print("  syntax checked, backup at src/api/views.py.bak")
print()
print("  Now:  sudo systemctl restart agrisync-api")
print()
