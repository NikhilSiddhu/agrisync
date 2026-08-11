"""Sign-in endpoints, called from the static site and the dashboard.

Goes at:  ~/agrisync/src/api/auth.py

    POST /auth/google    {"credential": "<google id token>"}  -> sets cookie
    GET  /auth/me                                             -> who is this
    POST /auth/logout                                         -> clears cookie

ON CORS
    agrisync.tech is static on GitHub Pages and calls this API on a different
    subdomain, so the browser demands explicit permission. The allowed origins
    are listed, not wildcarded: `Access-Control-Allow-Origin: *` is forbidden
    alongside credentials, and rightly so - it would let any site on the
    internet make authenticated calls with your visitor's cookie.
"""
import os

from flask import Blueprint, current_app, jsonify, request

from src import auth_session, visitors

bp = Blueprint("auth", __name__, url_prefix="/auth")


def allowed_origins():
    """ALLOWED_ORIGINS in .env, comma separated. Sensible defaults."""
    raw = os.getenv("ALLOWED_ORIGINS", "").strip()
    if raw:
        return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]
    return [
        "https://agrisync.tech",
        "https://www.agrisync.tech",
        "https://dashboard.agrisync.tech",
        "https://api.agrisync.tech",
    ]


@bp.after_request
def cors(response):
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin in allowed_origins():
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "86400"
        # The response differs by Origin, so caches must not serve one
        # visitor's headers to another.
        response.headers["Vary"] = "Origin"
    return response


@bp.route("/google", methods=["POST", "OPTIONS"])
def google():
    if request.method == "OPTIONS":
        return ("", 204)                       # preflight, headers added above

    if not auth_session.configured(current_app):
        return jsonify({"error": "sign-in is not configured on the server"}), 503

    payload = request.get_json(silent=True) or {}
    who = auth_session.verify_google_token(payload.get("credential"))
    if not who:
        # Deliberately vague. Telling an attacker which check failed is free
        # help, and a real user can do nothing with the detail anyway.
        return jsonify({"error": "could not verify that sign-in"}), 401

    # Record them in the visitor register. Google gave us a verified email and
    # a real name, so there is no form to fill in - phone is asked for later,
    # only if they request the dataset or the images.
    try:
        visitors.record(who["email"], path="/auth/google")
        existing = visitors.get(who["email"]) or {}
        if who["name"] and not (existing.get("name") or "").strip():
            current_app.extensions.setdefault("_agri", {})
            # save_profile insists on a phone; the name alone goes in directly
            # so signing in never demands a number just to look at a chart.
            from src.db import get_db
            get_db().visitors.update_one(
                {"email": who["email"]},
                {"$set": {"name": who["name"], "name_source": "google"}})
    except Exception:
        current_app.logger.exception("could not record visitor on sign-in")

    resp = jsonify({
        "ok": True,
        "email": who["email"],
        "name": who["name"],
        "initial": auth_session.initial(who["name"], who["email"]),
        "is_admin": visitors.is_admin(who["email"]),
        "is_granted": visitors.is_granted(who["email"]),
    })
    return auth_session.issue(current_app, resp, who["email"], who["name"])


@bp.route("/me", methods=["GET", "OPTIONS"])
def me():
    if request.method == "OPTIONS":
        return ("", 204)

    session = auth_session.read(current_app, request)
    if not session:
        return jsonify({"signed_in": False})

    email = session["email"]
    return jsonify({
        "signed_in": True,
        "email": email,
        "name": session.get("name") or "",
        "initial": auth_session.initial(session.get("name"), email),
        "is_admin": visitors.is_admin(email),
        "is_granted": visitors.is_granted(email),
        "has_requested": visitors.has_requested(email),
    })


@bp.route("/logout", methods=["POST", "OPTIONS"])
def logout():
    if request.method == "OPTIONS":
        return ("", 204)
    return auth_session.clear(jsonify({"ok": True}))


@bp.route("/config", methods=["GET", "OPTIONS"])
def config():
    """The client ID, so the static site does not have it hard-coded in two
    places. It is not a secret - it ships in every page that uses Google
    sign-in, by design."""
    if request.method == "OPTIONS":
        return ("", 204)
    return jsonify({"client_id": auth_session.client_id()})
