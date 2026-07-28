"""Device health watchdog.

Run every 15 minutes by agrisync-watchdog.timer, or by hand:

    source .venv/bin/activate
    python -m scripts.check_devices

Alerts only on STATE CHANGE, never repeatedly. A box that has been down for
three days sends one message, not 288. That is the difference between an
alert you act on and an alert you mute.
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from pymongo import MongoClient

from src.config import Config
from src.health_check import STATUS_OK, assess_all

STATE_FILE = Path(os.getenv("ALERT_STATE_FILE", Path.home() / ".agrisync-alert-state.json"))


def load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state):
    try:
        STATE_FILE.write_text(json.dumps(state, indent=2))
    except Exception as exc:
        print(f"warning: could not write state file: {exc}", file=sys.stderr)


def send_webhook(title, message):
    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        # Plain text body works with ntfy.sh. Slack and Discord want JSON,
        # so send both shapes and let the receiver pick.
        if "ntfy.sh" in url:
            requests.post(
                url,
                data=message.encode("utf-8"),
                headers={"Title": title, "Priority": "high", "Tags": "warning"},
                timeout=10,
            )
        else:
            requests.post(url, json={"text": f"{title}\n{message}"}, timeout=10)
        return True
    except Exception as exc:
        print(f"webhook failed: {exc}", file=sys.stderr)
        return False


def send_telegram(title, message):
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"{title}\n\n{message}"},
            timeout=10,
        )
        return True
    except Exception as exc:
        print(f"telegram failed: {exc}", file=sys.stderr)
        return False


def alert(title, message):
    sent = send_webhook(title, message)
    sent = send_telegram(title, message) or sent
    if not sent:
        print("(no alert channel configured - set ALERT_WEBHOOK_URL or "
              "TELEGRAM_BOT_TOKEN in .env)", file=sys.stderr)


def main():
    client = MongoClient(Config.MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client.get_default_database()

    try:
        devices = assess_all(db)
    except Exception as exc:
        print(f"FATAL: cannot reach the database: {exc}", file=sys.stderr)
        alert("AgriSync: database unreachable", str(exc))
        return 2

    if not devices:
        print("no devices have reported yet")
        return 0

    previous = load_state()
    current = {}
    changes = []

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"AgriSync device check  {stamp}")
    print("-" * 60)

    for dev in devices:
        did = dev["device_id"]
        status = dev["status"]
        current[did] = status

        marker = "OK " if status == STATUS_OK else "!! "
        print(f"{marker}{did:<22} {status:<10} "
              f"last seen {dev.get('minutes_since_last_reading', '?')} min ago")
        for problem in dev["problems"]:
            print(f"      - {problem}")

        was = previous.get(did)
        if was != status:
            changes.append((did, was, status, dev["problems"]))

    save_state(current)

    if not changes:
        print("\nno change since the last check")
        return 0

    lines = []
    for did, was, status, problems in changes:
        if status == STATUS_OK:
            lines.append(f"RECOVERED: {did} is reporting normally again")
        else:
            lines.append(f"{did}: {was or 'unknown'} -> {status}")
            lines.extend(f"  - {p}" for p in problems)

    body = "\n".join(lines)
    print("\n=== state change, alerting ===")
    print(body)

    worst = [c for c in changes if c[2] != STATUS_OK]
    title = "AgriSync: device problem" if worst else "AgriSync: recovered"
    alert(title, body)

    return 1 if worst else 0


if __name__ == "__main__":
    sys.exit(main())
