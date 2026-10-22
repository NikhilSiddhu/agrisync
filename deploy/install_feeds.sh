#!/usr/bin/env bash
# Install or update the outside feeds on the Pi. Safe to run again.
#
#     cd ~/agrisync && git pull && bash deploy/install_feeds.sh LAT LON
#
# LAT LON is the middle of the field, in decimal degrees. It is only needed
# the first time; it goes into .env and never into this repository, which is
# public. Later runs need no arguments.
#
# Installs the Python packages, installs the hourly timer, fills every feed
# once so the dashboard has something to show, and restarts the two web apps.
set -euo pipefail
cd "$(dirname "$0")/.."
APP=$(pwd)
U=$(id -un)
[ -f .env ] || { echo "STOP: run this from the agrisync folder, the one with .env in it."; exit 1; }
[ -x .venv/bin/python ] || { echo "STOP: no .venv here. Run rebuild_station.sh first."; exit 1; }

echo "== 1/4 Field position"
if grep -q '^FIELD_LAT=' .env; then
  echo "already set: $(grep -E '^FIELD_(LAT|LON)=' .env | tr '\n' ' ')"
else
  LAT="${1:-}"; LON="${2:-}"
  if ! [[ "$LAT" =~ ^-?[0-9]{1,2}\.[0-9]+$ && "$LON" =~ ^-?[0-9]{1,3}\.[0-9]+$ ]]; then
    echo "STOP: give the field position once, like:  bash deploy/install_feeds.sh 28.70 77.81"
    exit 1
  fi
  printf '\n# The field itself (satellite pixels are 10 m). Kept out of git.\nFIELD_LAT=%s\nFIELD_LON=%s\nFIELD_RADIUS_M=15\nSEASON_START=2026-08-01\n' "$LAT" "$LON" >> .env
  echo "added FIELD_LAT=$LAT FIELD_LON=$LON to .env"
fi

echo "== 2/4 Python packages"
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -c "import rasterio; print('rasterio', rasterio.__version__)"

echo "== 3/4 Hourly timer"
for f in agrisync-feeds.service agrisync-feeds.timer; do
  sed -e "s|^User=pi$|User=$U|" -e "s|/home/pi/AgriSync|$APP|g" "deploy/$f" \
    | sudo tee "/etc/systemd/system/$f" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now agrisync-feeds.timer

echo "== 4/4 First fill (5 to 10 minutes: about 45 satellite scenes to read)"
sudo systemctl start agrisync-feeds.service || true
{ sudo journalctl -u agrisync-feeds --since "-35 min" --no-pager -o cat \
    | grep -E "^[0-9]{2}:[0-9]{2}:[0-9]{2} " | tail -n 20; } || true
sudo systemctl restart agrisync-dashboard agrisync-api
echo
echo "Done. The feeds refresh every hour on their own."
