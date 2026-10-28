"""Admin data for agrisync.tech's admin drawer.

    GET  /admin/api/overview                       visitors, data requests, field log
    POST /admin/api/requests/<email>/<approve|revoke>

The static site on GitHub Pages cannot render server templates, so it asks
for JSON and draws the panel itself. Same two gates as the dashboard's admin
pages: a signed session cookie, and an email listed in ADMIN_EMAILS. CORS is
the same listed-origins policy as /auth (never a wildcard), because these
calls carry the visitor's cookie.
"""
from flask import Blueprint, jsonify, request

from src import access, visitors, whoami
from src.api.auth import allowed_origins
from src.db import get_db

bp = Blueprint("admin_api", __name__, url_prefix="/admin/api")


@bp.after_request
def cors(response):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin in allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Vary"] = "Origin"
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _admin_or_error():
    ident = whoami.current(request)
    if not access.is_admin(ident):
        return None, (jsonify({"error": "Only the station's admins can see this."}), 403)
    return ident, None


def _iso(v):
    return v.isoformat() if hasattr(v, "isoformat") else v


@bp.route("/overview", methods=["GET", "OPTIONS"])
def overview():
    if request.method == "OPTIONS":
        return ("", 204)
    ident, err = _admin_or_error()
    if err:
        return err
    admins = visitors.admin_emails()
    people = [v for v in visitors.listing(300) if (v.get("email") or "").lower() not in admins]
    reqs = [r for r in visitors.requests_listing(200) if (r.get("email") or "").lower() not in admins]
    try:
        log_count = get_db().field_log.count_documents({})
        last = get_db().field_log.find_one({}, {"date": 1, "_id": 0}, sort=[("date", -1)])
    except Exception:
        log_count, last = 0, None
    keep_v = ("email", "name", "phone", "organisation", "visits", "first_seen", "last_seen")
    keep_r = ("email", "name", "phone", "organisation", "purpose", "last_asked", "granted", "wants_label")
    return jsonify({
        "total": len(people),
        "visitors": [{k: _iso(v.get(k)) for k in keep_v} for v in people],
        "requests": [{k: _iso(r.get(k)) for k in keep_r} for r in reqs],
        "field_log": {"count": log_count, "last": (last or {}).get("date")},
    })


@bp.route("/requests/<path:email>/<action>", methods=["POST", "OPTIONS"])
def grant(email, action):
    if request.method == "OPTIONS":
        return ("", 204)
    ident, err = _admin_or_error()
    if err:
        return err
    if action not in ("approve", "revoke"):
        return jsonify({"error": "unknown action"}), 400
    if not visitors.set_granted(email, action == "approve"):
        return jsonify({"error": "no such request"}), 404
    return jsonify({"status": action + "d", "email": email})
