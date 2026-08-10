"""HMAC request signing for device ingest.

Goes at:  ~/agrisync/src/hmac_auth.py

WHY THIS EXISTS
    The A7670C cannot complete a TLS handshake with Cloudflare's edge, so the
    field box posts over plain HTTP. An API key sent in a header over plain
    HTTP is visible to anyone on the path, and once they have it they can post
    whatever they like. That is precisely the failure src/auth.py calls out as
    the one that matters: fake data poisons the dataset silently.

    HMAC fixes that. The device proves it knows a shared secret WITHOUT ever
    sending the secret. Watching the traffic gains an attacker nothing they
    can reuse.

WHAT IT DOES AND DOES NOT GIVE YOU
    Gives:      nobody can forge or tamper with a reading
    Gives:      replays older than MAX_SKEW_SECONDS are rejected
    Does NOT:   hide the reading values from someone watching the network

    Sensor values from a field are not secret. Their authenticity is what
    matters, and that is what this protects.

THE SCHEME
    header:   X-Sig: <unix_timestamp>.<hex_hmac_sha256>
    signed:   "<unix_timestamp>." + <raw request body bytes>
    key:      DEVICE_HMAC_SECRET, shared between the device and this server
"""

import hashlib
import hmac
import time
from functools import wraps

from flask import current_app, jsonify, request

# How far the device's clock may differ from ours. The device stamps this at
# SEND time, not sample time, so buffered readings from days ago still pass -
# their age lives in sampled_at inside the body, not here.
MAX_SKEW_SECONDS = 900          # 15 minutes

PLACEHOLDER = "generate-me-with-openssl-rand-hex-32"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    """The canonical string is the timestamp, a dot, then the raw body."""
    msg = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def verify_signature() -> tuple[bool, str]:
    """Returns (ok, reason). Reason is for logs, never for the response body -
    telling an attacker exactly which check failed is free help."""
    secret = current_app.config.get("DEVICE_HMAC_SECRET", "")
    if not secret or secret == PLACEHOLDER:
        return False, "DEVICE_HMAC_SECRET not set in .env"

    header = request.headers.get("X-Sig", "")
    if "." not in header:
        return False, "no X-Sig header"

    timestamp, sig = header.split(".", 1)

    try:
        ts = int(timestamp)
    except ValueError:
        return False, "timestamp not an integer"

    skew = abs(int(time.time()) - ts)
    if skew > MAX_SKEW_SECONDS:
        return False, f"clock skew {skew}s exceeds {MAX_SKEW_SECONDS}s"

    expected = _sign(secret, timestamp, request.get_data())

    # compare_digest, not ==. A plain comparison leaks how much of the
    # signature was correct through how long it took to fail.
    if not hmac.compare_digest(sig, expected):
        return False, "signature mismatch"

    return True, "ok"


def require_device_auth(fn):
    """Accepts EITHER a valid HMAC signature OR the old X-API-Key.

    Keeping both means the USB-tethered and WiFi paths carry on working while
    the cellular path moves to signing. Drop the API key branch once every
    device signs.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ok, reason = verify_signature()
        if ok:
            return fn(*args, **kwargs)

        # fall back to the old scheme
        expected_key = current_app.config.get("DEVICE_API_KEY", "")
        sent_key = request.headers.get("X-API-Key")
        if expected_key and hmac.compare_digest(sent_key or "", expected_key):
            return fn(*args, **kwargs)

        current_app.logger.warning(
            "rejected POST from %s: %s",
            request.headers.get("CF-Connecting-IP", request.remote_addr),
            reason,
        )
        return jsonify({"error": "unauthorized"}), 401

    return wrapper


def require_hmac_only(fn):
    """Signature required, no API key fallback. Use once every device signs."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        ok, reason = verify_signature()
        if not ok:
            current_app.logger.warning(
                "rejected POST from %s: %s",
                request.headers.get("CF-Connecting-IP", request.remote_addr),
                reason,
            )
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper
