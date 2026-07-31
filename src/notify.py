"""One place to push a message to the owner's phone.

Shared by the device watchdog and the data-request flow so there is a single
place to change if the channel ever moves. Never raises - a notification
failing must not take down the request that triggered it.
"""
import os

import requests


def push(title, message, priority="default", tags=""):
    """Returns True if any channel accepted it."""
    sent = False

    url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if url:
        try:
            if "ntfy.sh" in url:
                requests.post(url, data=message.encode("utf-8"),
                              headers={"Title": title, "Priority": priority, "Tags": tags},
                              timeout=8)
            else:
                requests.post(url, json={"text": f"{title}\n{message}"}, timeout=8)
            sent = True
        except Exception:
            pass

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": f"{title}\n\n{message}"}, timeout=8)
            sent = True
        except Exception:
            pass

    return sent
