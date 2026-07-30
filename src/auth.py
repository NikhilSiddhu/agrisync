"""Authentication for the API.

Two separate keys, because writing and reading are different risks:

  DEVICE_API_KEY   the ESP32 sends this to POST readings. Protects against
                   someone injecting fake data, which would poison the
                   dataset silently and permanently. This is the one that
                   actually matters.

  READ_API_KEY     optional. When set, GET endpoints require it too.
                   Leave it empty on a trusted LAN; set it before you expose
                   anything through a tunnel.

/health and /health/db are deliberately left open. Monitoring has to work
without credentials, and they leak nothing beyond "the service is up".

NO LOOPBACK BYPASS. It is tempting to exempt 127.0.0.1 so your own curl
commands stay short, but cloudflared connects to the app FROM localhost -
so a loopback exemption would silently disable auth for the entire internet
the moment you put a tunnel in front.
"""
import hmac
from functools import wraps

from flask import current_app, jsonify, request

PLACEHOLDER = "change-me-before-you-flash-anything"


def _valid(sent, expected):
    return bool(expected) and hmac.compare_digest(sent or "", expected)


def require_device_key(fn):
    """Write access. Always required, never optional."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("DEVICE_API_KEY", "")
        if not expected or expected == PLACEHOLDER:
            return jsonify({
                "error": "server misconfigured",
                "detail": "DEVICE_API_KEY is not set in .env",
            }), 500

        if not _valid(request.headers.get("X-API-Key"), expected):
            current_app.logger.warning(
                "rejected POST from %s", request.headers.get(
                    "CF-Connecting-IP", request.remote_addr)
            )
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


def require_read_key(fn):
    """Read access. Enforced only when READ_API_KEY is set in .env."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("READ_API_KEY", "")
        if not expected:
            return fn(*args, **kwargs)   # open by choice, not by accident

        sent = request.headers.get("X-Read-Key") or request.args.get("key")
        if not _valid(sent, expected):
            return jsonify({
                "error": "unauthorized",
                "detail": "send X-Read-Key, or ?key= for a browser",
            }), 401
        return fn(*args, **kwargs)

    return wrapper
