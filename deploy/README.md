# Deploying AgriSync on the Pi

Three units. Install them once and the box looks after itself.

## 1. The API

```bash
sudo cp deploy/agrisync-api.service /etc/systemd/system/
sudo nano /etc/systemd/system/agrisync-api.service
```

Change `User=`, `Group=`, and every `/home/pi/AgriSync` path if your username is
not `pi`. Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now agrisync-api
systemctl status agrisync-api
```

`enable` means it starts on boot. `--now` means it starts immediately. Check it:

```bash
curl localhost:5000/health
journalctl -u agrisync-api -f      # live logs, Ctrl-C to stop
```

**Test the reboot.** Do it now, on the bench, not after the box is on a pole:

```bash
sudo reboot
# wait, ssh back in
curl localhost:5000/health
```

If that answers without you starting anything, the API survives power cuts.

## 2. The health watchdog

```bash
sudo cp deploy/agrisync-watchdog.service deploy/agrisync-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agrisync-watchdog.timer
systemctl list-timers agrisync-watchdog
```

Runs every 15 minutes. Checks whether each device is still reporting and whether
any sensor has dropped off the bus, and alerts you if the state changes.

Configure alerts in `.env`. Both are optional and both are free:

```
ALERT_STALE_MINUTES=30
ALERT_WEBHOOK_URL=https://ntfy.sh/your-unique-topic-name
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

`ntfy.sh` is the quickest: pick any unguessable topic name, install the ntfy app on
your phone, subscribe to that topic. No signup, no card.

Test it by hand before you rely on it:

```bash
source .venv/bin/activate
python -m scripts.check_devices
```

## 3. The nightly backup

```bash
chmod +x scripts/backup_mongo.sh
crontab -e
```

Add:

```
0 2 * * * /home/pi/AgriSync/scripts/backup_mongo.sh >> /home/pi/agrisync-backup.log 2>&1
```

## Set the clock properly

Every reading is timestamped, so the dataset is only as good as the Pi knowing the
time. Check it:

```bash
timedatectl
```

You want `System clock synchronized: yes` and the right timezone. If not:

```bash
sudo timedatectl set-timezone Asia/Kolkata
sudo timedatectl set-ntp true
```

**Fit the RTC battery.** The Pi 5 has a battery connector for its real-time clock.
Without a cell, a Pi that boots with the network down has no idea what time it is,
and every reading it stamps is wrong. Once fitted, check with:

```bash
sudo hwclock -r
```

## Handy commands

| What | Command |
|---|---|
| Restart the API after a `git pull` | `sudo systemctl restart agrisync-api` |
| Live logs | `journalctl -u agrisync-api -f` |
| Did the watchdog run | `journalctl -u agrisync-watchdog -n 50` |
| Is Mongo up | `docker ps` |
| Device health right now | `curl localhost:5000/api/v1/devices` |
