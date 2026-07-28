"""Shared-secret auth for the ESP32 boards.

The device sends its key in the X-API-Key header. Compared with
hmac.compare_digest so the comparison is constant time.
"""
import hmac
from functools import wraps

from flask import current_app, jsonify, request


def require_device_key(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        expected = current_app.config.get("DEVICE_API_KEY", "")
        if not expected or expected == "change-me-before-you-flash-anything":
            return jsonify({
                "error": "server misconfigured",
                "detail": "DEVICE_API_KEY is not set in .env",
            }), 500

        sent = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(sent, expected):
            return jsonify({"error": "unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper
