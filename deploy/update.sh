#!/usr/bin/env bash
# Bring the Pi up to date with GitHub, check it works, and go back if not.
#
#     cd ~/agrisync && bash deploy/update.sh
#
# What it does, in order:
#   1. Stops if a tracked file was changed by hand here, so nothing is lost.
#   2. Shows the commits it is about to take.
#   3. Backs up the database (scripts/backup_mongo.sh), if it can.
#   4. Fast-forwards to origin/main. Never a merge, never a forced reset.
#   5. Installs the Python packages, checks both apps import, and refreshes
#      the hourly feeds timer (deploy/install_feeds.sh).
#   6. Restarts agrisync-api and agrisync-dashboard and asks each one if it
#      is healthy, for up to a minute.
#   7. If either is not, goes back to the commit it started from, restarts
#      again and says so. Your .env, data and backups are never touched.
#
# Options:  --no-backup   skip step 3
#           --no-feeds    skip deploy/install_feeds.sh (the 5 to 10 minute fill)
#
# Nothing here talks to the field box. Phytoplex keeps posting throughout;
# the minute the API restarts, its readings wait in the box's queue and
# arrive a few minutes late.
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP=1; FEEDS=1
for a in "$@"; do
  case "$a" in
    --no-backup) BACKUP=0 ;;
    --no-feeds)  FEEDS=0 ;;
    *) echo "unknown option $a"; exit 2 ;;
  esac
done

say()  { printf '\n== %s\n' "$*"; }
stop() { printf '\nSTOP: %s\n' "$*"; exit 1; }

[ -f .env ] || stop "run this from the agrisync folder, the one with .env in it."
[ -x .venv/bin/python ] || stop "no .venv here."
git diff --quiet && git diff --cached --quiet \
  || stop "files here were changed by hand (git status shows them). Commit or move them first."

BEFORE=$(git rev-parse HEAD)

say "1/6 What is new on GitHub"
git fetch --quiet origin main
if [ "$(git rev-parse origin/main)" = "$BEFORE" ]; then
  echo "Already up to date at $(git log -1 --format='%h %s')."
  exit 0
fi
git merge-base --is-ancestor "$BEFORE" origin/main \
  || stop "this Pi has commits GitHub does not. Nothing changed; ask before going further."
git log --oneline "$BEFORE..origin/main"

if [ "$BACKUP" = 1 ]; then
  say "2/6 Database backup"
  if bash scripts/backup_mongo.sh; then :; else
    echo "Backup did not run (see above). The update does not change stored data, so carrying on."
  fi
fi

say "3/6 Taking the new code"
git merge --ff-only --quiet origin/main
echo "now at $(git log -1 --format='%h %s')"

rollback() {
  say "Going back to $(git log -1 --format='%h' "$BEFORE")"
  git reset --keep "$BEFORE"
  .venv/bin/pip install -q -r requirements.txt || true
  sudo systemctl restart agrisync-api agrisync-dashboard
  stop "$1  The Pi is back on the code it had before. Nothing else changed."
}

say "4/6 Python packages and a load check"
.venv/bin/pip install -q -r requirements.txt || rollback "pip could not install requirements.txt."
.venv/bin/python - <<'PY' || rollback "the new code does not load."
from main.app import create_app as api
from dashboard.app import create_app as dash
api(); dash()
print("both apps load")
PY

if [ "$FEEDS" = 1 ]; then
  say "5/6 Feeds timer and first fill"
  bash deploy/install_feeds.sh || echo "install_feeds.sh reported a problem (see above); the apps are still checked next."
fi

say "6/6 Restart and health"
sudo systemctl restart agrisync-api agrisync-dashboard
ok_api=0; ok_dash=0
for _ in $(seq 1 30); do
  sleep 2
  curl -fsS -m 3 http://127.0.0.1:5000/health/db >/dev/null 2>&1 && ok_api=1
  curl -fsS -m 5 -o /dev/null http://127.0.0.1:8080/ 2>/dev/null && ok_dash=1
  [ "$ok_api" = 1 ] && [ "$ok_dash" = 1 ] && break
done
[ "$ok_api" = 1 ]  || rollback "the API did not answer /health/db within a minute."
[ "$ok_dash" = 1 ] || rollback "the dashboard did not answer within a minute."

echo "API and dashboard healthy on $(git log -1 --format='%h %s')."
# agrisync.tech reads this one URL for its live numbers and the versions strip.
if curl -fsS -m 8 http://127.0.0.1:8080/summary.json -o /tmp/agrisync-summary.json 2>/dev/null; then
  .venv/bin/python - <<'PY' || true
import json
v = json.load(open("/tmp/agrisync-summary.json")).get("versions") or {}
print("Running: Phytoplex firmware %s, Pi software %s (%s), odds engine %s."
      % (v.get("firmware") or "unknown", v.get("release"), v.get("commit") or "no commit", v.get("odds_engine")))
PY
else
  echo "Note: /summary.json did not answer, so agrisync.tech keeps showing its built-in numbers until it does."
fi
echo
echo "Check from anywhere:  https://dashboard.agrisync.tech"
echo "The box's state:      curl -s http://127.0.0.1:5000/api/v1/devices | python3 -m json.tool"
