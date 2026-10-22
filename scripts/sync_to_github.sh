#!/usr/bin/env bash
# Push the Pi's current code to GitHub without leaking secrets.
#
# Put this in ~/agrisync/scripts/ on the Pi and run it from anywhere in the repo:
#     bash ~/agrisync/scripts/sync_to_github.sh
#
# It shows what changed, refuses to commit anything that looks like a secret,
# asks before it commits, then pushes. Readings stay in MongoDB on the Pi;
# only code and docs go to GitHub.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

echo "== Fetching GitHub"
git fetch origin

echo; echo "== Files changed on the Pi"
git status --short

echo; echo "== Commits on the Pi that GitHub does not have"
git log --oneline origin/main..HEAD || true
echo; echo "== Commits on GitHub that the Pi does not have"
git log --oneline HEAD..origin/main || true

# 1. Known leaks that must be gone from every tracked file before anything is pushed.
# Values that are ${...} references or obvious placeholders are fine.
LEAKS=$(git grep -nIE "WIFI_PASSWORD='[^'\"\$]{6,}'|#define[[:space:]]+HMAC_SECRET[[:space:]]+\"[0-9a-f]{32,}\"" \
  | grep -vE "WIFI_PASSWORD='(your|<)" || true)
if [ -n "$LEAKS" ]; then
  echo; echo "STOP: these tracked lines still hold a real password or key:"
  echo "$LEAKS" | cut -d: -f1,2
  echo "Replace them with placeholders, then run this again."
  exit 1
fi

# 2. Files that must never go to a public repo, whatever .gitignore says.
BLOCK='(^|/)\.env$|\.pem$|\.key$|(^|/)mongo-data/|(^|/)backups/|readings-backup-.*\.json$|\.sqlite3?$|\.db$|(^|/)exports/|\.bak(_[A-Za-z0-9]+|[0-9]*)$'
BAD=$(git status --porcelain --untracked-files=all | awk '{print $NF}' | grep -E "$BLOCK" || true)
if [ -n "$BAD" ]; then
  echo; echo "STOP: these must not go to a public repo (secrets, data or backup copies). Move them or add them to .gitignore:"
  echo "$BAD"
  exit 1
fi

git add -A

# 3. Scan what is about to be committed for secret-looking values.
#    Only quoted values or KEY=value lines count, so code like
#    secret = current_app.config[...] does not trip it.
ADDED=$(git diff --cached -U0 | grep -E '^\+' | grep -vE '^\+\+\+' || true)
SUSPECT=$( { printf '%s\n' "$ADDED" \
    | grep -iE "(password|passwd|secret|token|api_?key)[A-Za-z0-9_]*[\"']?[[:space:]]*[=:][[:space:]]*[\"'][A-Za-z0-9+/_.=-]{8,}[\"']" ;
  printf '%s\n' "$ADDED" \
    | grep -E "^\+[[:space:]]*(export[[:space:]]+)?[A-Z0-9_]*(PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY)[A-Z0-9_]*=[\"']?[A-Za-z0-9+/_.-]{8,}" ;
  printf '%s\n' "$ADDED" | grep -E "mongodb(\+srv)?://[^:/[:space:]]+:[^@[:space:]]+@" ; } \
  | grep -viE 'getenv|environ|sysenv|placeholder|change-me|your-|example|PUT_THE|generate-me|test|dummy|fake|sample|user:pass@' | sort -u || true)
HEX=$(printf '%s\n' "$ADDED" | grep -E '\b[0-9a-f]{40,}\b' || true)
if [ -n "$SUSPECT$HEX" ]; then
  echo; echo "STOP: these new lines look like real secrets:"
  printf '%s\n%s\n' "$SUSPECT" "$HEX" | sed '/^$/d' | cut -c1-120
  git reset -q
  echo "Nothing was committed. Remove them, then run this again."
  exit 1
fi

if git diff --cached --quiet; then
  echo; echo "Nothing new to commit."
else
  echo; git diff --cached --stat | tail -25
  read -r -p "Commit these and push to GitHub? [y/N] " ans
  if [ "$ans" != "y" ] && [ "$ans" != "Y" ]; then
    git reset -q; echo "Cancelled. Nothing was committed."; exit 0
  fi
  git commit -q -m "Sync from the Pi, $(date +%F)"
fi

if ! git pull --rebase origin main; then
  echo; echo "GitHub had changes that clash with the Pi's. Run: git rebase --abort"
  echo "Then send this output to Claude before trying again."
  exit 1
fi
git push origin HEAD:main
echo; echo "Done. GitHub now matches the Pi."
