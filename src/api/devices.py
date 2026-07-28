"""Device health endpoints.

    GET /api/v1/devices              every device the database has seen
    GET /api/v1/devices/<id>         one device in detail

Read only, no auth. Bookmark it on your phone.
"""
from flask import Blueprint, jsonify, request

from src.db import get_db
from src.health_check import SEVERITY, STATUS_OK, assess_all, assess_device

bp = Blueprint("devices", __name__, url_prefix="/api/v1/devices")


@bp.get("")
@bp.get("/")
def list_devices():
    try:
        window = min(int(request.args.get("window_hours", 6)), 168)
    except ValueError:
        window = 6

    try:
        devices = assess_all(get_db(), window)
    except Exception as exc:
        return jsonify({"error": "database unreachable", "detail": str(exc)}), 503

    worst = max((SEVERITY[d["status"]] for d in devices), default=0)
    return jsonify({
        "overall": "ok" if worst == 0 else "attention",
        "device_count": len(devices),
        "devices": devices,
    }), (200 if worst == 0 else 207)


@bp.get("/<device_id>")
def device_detail(device_id):
    try:
        window = min(int(request.args.get("window_hours", 6)), 168)
    except ValueError:
        window = 6

    try:
        result = assess_device(get_db(), device_id, window)
    except Exception as exc:
        return jsonify({"error": "database unreachable", "detail": str(exc)}), 503

    return jsonify(result), (200 if result["status"] == STATUS_OK else 207)
