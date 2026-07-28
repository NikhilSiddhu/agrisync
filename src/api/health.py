from flask import Blueprint, jsonify

from src.db import ping

bp = Blueprint("health", __name__)


@bp.get("/health")
def health():
    """Is the Flask process alive? Says nothing about the database."""
    return jsonify({"status": "ok", "service": "agrisync-api"})


@bp.get("/health/db")
def health_db():
    """Can Flask actually reach Mongo? This is the one that matters."""
    try:
        ping()
        return jsonify({"status": "ok", "db": "reachable"})
    except Exception as exc:
        return jsonify({"status": "error", "db": "unreachable", "detail": str(exc)}), 503
