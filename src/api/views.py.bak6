"""Human-facing pages on the API hostname.

api.agrisync.tech sits behind Cloudflare Access, so everything here is already
gated. The read key still applies as a second layer, which is why the download
links carry ?key= — a browser session cannot add a header.

Content negotiation on /: a browser gets the page, anything asking for JSON
gets the endpoint index that was there before, so nothing machine-facing breaks.
"""
from flask import Blueprint, Response, current_app, jsonify, render_template, request

from src import access, history, notify, visitors
from src.auth import require_read_key

# Identity now comes from the Google session, with Cloudflare Access
# as a fallback while both exist. See src/whoami.py.
from src import whoami

bp = Blueprint("views", __name__)

ENDPOINTS = [
    "GET  /health",
    "GET  /health/db",
    "POST /api/v1/sensors/readings   (needs X-API-Key header)",
    "GET  /api/v1/sensors/readings",
    "GET  /api/v1/risk/<device_id>",
    "GET  /api/v1/devices",
    "GET  /api/v1/devices/<device_id>",
    "GET  /export/readings.csv       (full dataset, streamed)",
]


def _logout_url():
    """Cloudflare's own logout. Clears the Access session, not just ours —
    there is no session of ours to clear."""
    team = (current_app.config.get("CF_ACCESS_TEAM_DOMAIN") or "").strip().rstrip("/")
    return f"https://{team}/cdn-cgi/access/logout" if team else None


def _who():
    """Verified visitor, recorded on the way past. Never fatal."""
    ident = whoami.current(request)
    if ident and ident.get("email"):
        try:
            visitors.record(ident["email"], request.path)
        except Exception as exc:
            current_app.logger.warning("could not record visitor: %s", exc)
    return ident


def _wants_json():
    best = request.accept_mimetypes.best_match(["text/html", "application/json"])
    return best == "application/json" or request.args.get("format") == "json"


@bp.get("/")
def index():
    if _wants_json():
        return jsonify({
            "service": "agrisync-api",
            "site": current_app.config["SITE_NAME"],
            "endpoints": ENDPOINTS,
        })

    ident = _who()
    try:
        ctx = {
            "identity": ident,
            "is_admin": access.is_admin(ident),
            "profile": visitors.get(ident["email"]) if ident and ident.get("email") else None,
            "needs_profile": visitors.needs_profile(ident["email"]) if ident and ident.get("email") else False,
            "display_name": visitors.display_name(ident["email"]) if ident and ident.get("email") else None,
            "logout_url": _logout_url(),
            "already_requested": visitors.has_requested(ident["email"]) if ident and ident.get("email") else False,
            "can_download": access.is_admin(ident) or (visitors.is_granted(ident["email"]) if ident and ident.get("email") else False),
            "span": history.span(),
            "series": history.series(),
            "daily": history.daily()[:30],
            "endpoints": ENDPOINTS,
            "read_key": current_app.config.get("READ_API_KEY", ""),
            "site": current_app.config["SITE_NAME"],
            "db_ok": True,
        }
    except Exception as exc:
        current_app.logger.warning("history view degraded: %s", exc)
        ctx = {"identity": ident, "is_admin": access.is_admin(ident),
               "profile": None, "needs_profile": False, "display_name": None,
               "logout_url": _logout_url(), "already_requested": False,
               "can_download": False,
               "span": {"empty": True, "total": 0}, "series": {}, "daily": [],
               "endpoints": ENDPOINTS, "read_key": "",
               "site": current_app.config["SITE_NAME"], "db_ok": False}
    return render_template("history.html", **ctx)


@bp.get("/export/readings.csv")
def export_csv():
    """The raw dataset. Admins only.

    Signing in gets you the charts and the daily table. It does not get you a
    season of raw work as a single file — that is a conversation, not a button.
    Everyone else gets the request flow below.
    """
    ident = whoami.current(request)
    email = (ident or {}).get("email")
    if not (access.is_admin(ident) or visitors.is_granted(email)):
        if not access.configured():
            return jsonify({"error": "identity unavailable",
                            "detail": "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are unset"}), 503
        return jsonify({"error": "not permitted",
                        "detail": "request the dataset from the history page"}), 403

    return Response(
        history.stream_csv(),
        mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="agrisync_readings.csv"',
                 "Cache-Control": "no-store"},
    )


def _not_admin(rows):
    """Drop anybody in ADMIN_EMAILS from a list of visitor or request rows.

    Compares lowercased, because an address typed into .env with different
    capitalisation is the same mailbox and would otherwise slip through.
    """
    out = []
    for r in rows or []:
        email = (r.get("email") or "").strip().lower()
        if email and access.is_admin({"email": email}):
            continue
        out.append(r)
    return out


def _render_register(ident):
    """The visitor register and request queue, with admins filtered out.

    Both admin routes render the same page, so they share this rather than
    keeping two copies of the same six arguments in step by hand.
    """
    all_visitors = visitors.listing()
    shown = _not_admin(all_visitors)

    # Subtract the ones actually removed. Hiding rows while leaving the
    # headline at 5 would just make the page argue with itself.
    hidden = len(all_visitors) - len(shown)
    total = max(0, visitors.count() - hidden)

    return render_template("visitors.html",
                           visitors=shown,
                           requests=_not_admin(visitors.requests_listing()),
                           total=total,
                           identity=ident,
                           display_name=visitors.display_name(ident["email"]),
                           logout_url=_logout_url())


@bp.get("/admin/visitors")
def admin_visitors():
    """Who has signed in. Only for the emails listed in ADMIN_EMAILS.

    Access already decides who reaches this hostname at all. This is the
    second gate: being able to sign in does not make you an administrator.
    """
    ident = whoami.current(request)
    if not access.is_admin(ident):
        if not access.configured():
            return jsonify({
                "error": "identity unavailable",
                "detail": "CF_ACCESS_TEAM_DOMAIN and CF_ACCESS_AUD are not set in .env",
            }), 503
        return jsonify({"error": "forbidden",
                        "detail": "your email is not in ADMIN_EMAILS"}), 403

    return _render_register(ident)


@bp.post("/request-data")
def request_data():
    """Ask for the raw dataset. Records who asked and pings the owner.

    Deliberately not an email form. Cloudflare has already verified the address,
    so there is nothing to type and nothing to spoof.
    """
    ident = whoami.current(request)
    if not ident or not ident.get("email"):
        if not access.configured():
            return jsonify({"error": "identity unavailable"}), 503
        return jsonify({"error": "sign in first"}), 401

    data = request.get_json(silent=True) or request.form

    # The details come with the request, because this is the moment there is a
    # reason to ask. The email is never taken from the form — Cloudflare
    # verified it and the browser only ever displayed it.
    ok, err = visitors.save_profile(
        ident["email"],
        name=data.get("name"),
        phone=data.get("phone"),
        organisation=data.get("organisation"),
        purpose=data.get("purpose"),
    )
    if not ok:
        return jsonify({"error": err}), 400

    note = (data.get("purpose") or "").strip()

    try:
        created, _ = visitors.record_request(ident["email"], note)
    except Exception as exc:
        current_app.logger.warning("could not record request: %s", exc)
        return jsonify({"error": "could not record the request"}), 503

    # Only ping the phone the first time. A refresh should not buzz you again.
    if created:
        who = visitors.get(ident["email"]) or {}
        lines = [f"{who.get('name') or ident['email']} asked for the raw data.",
                 ident["email"]]
        if who.get("organisation"):
            lines.append(who["organisation"])
        if who.get("phone"):
            lines.append(who["phone"])
        if note:
            lines.append(f"\n{note[:200]}")
        notify.push("AgriSync: dataset requested", "\n".join(lines),
                    priority="default", tags="inbox_tray")

    return jsonify({"status": "recorded", "new": created,
                    "email": ident["email"]}), 200


@bp.get("/admin/requests")
def admin_requests():
    ident = whoami.current(request)
    if not access.is_admin(ident):
        if not access.configured():
            return jsonify({"error": "identity unavailable"}), 503
        return jsonify({"error": "forbidden"}), 403
    return _render_register(ident)


@bp.post("/profile")
def save_profile():
    """Name once, on first visit. Cloudflare proved the address; it cannot
    tell us what to call someone."""
    ident = whoami.current(request)
    if not ident or not ident.get("email"):
        if not access.configured():
            return jsonify({"error": "identity unavailable"}), 503
        return jsonify({"error": "sign in first"}), 401

    data = request.get_json(silent=True) or request.form
    ok, err = visitors.save_profile(
        ident["email"],
        name=data.get("name"),
        phone=data.get("phone"),
        organisation=data.get("organisation"),
        purpose=data.get("purpose"),
    )
    if not ok:
        return jsonify({"error": err}), 400
    return jsonify({"status": "saved",
                    "name": visitors.display_name(ident["email"])}), 200


@bp.post("/admin/requests/<path:email>/<action>")
def admin_grant(email, action):
    """Approve or revoke a dataset request.

    Approved people get a live download, not a snapshot — the export streams
    from the database, so what they fetch is always current and there is
    nothing to regenerate nightly.
    """
    ident = whoami.current(request)
    if not access.is_admin(ident):
        if not access.configured():
            return jsonify({"error": "identity unavailable"}), 503
        return jsonify({"error": "forbidden"}), 403

    if action not in ("approve", "revoke"):
        return jsonify({"error": "unknown action"}), 400

    ok = visitors.set_granted(email, action == "approve")
    if not ok:
        return jsonify({"error": "no such request"}), 404
    return jsonify({"status": action + "d", "email": email}), 200
