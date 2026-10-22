"""Public AgriSync dashboard, with a members' view for signed-in visitors.

Goes at:  ~/agrisync/dashboard/app.py   (replaces the existing file)

Runs as its OWN service on port 8080, separate from the API on 5000.

That separation is a security boundary, not tidiness. This hostname has no
Cloudflare Access in front of it, so anyone on the internet can reach it. If it
shared a process with the API, a visitor could request an API path on this
hostname and walk straight past the Access policy protecting the other one.

This process:
  - only ever reads from MongoDB
  - only ever returns HTML and chart data
  - holds no keys and exposes no device credentials


ONE URL, TWO VIEWS
    A stranger gets seven days. A signed-in visitor gets everything.

    Same address either way, which is the whole point. A bookmark keeps
    working. A link you paste into an email opens for whoever receives it, at
    whatever depth they are entitled to. The alternative - sending signed-in
    people to a different hostname - breaks every link the moment it is shared
    with someone in the other state.


THE GATE IS HERE, NOT IN THE TEMPLATE
    The blur in the stylesheet is decoration. It makes a locked section look
    inviting instead of forbidding, and that is all it does. Anyone can open
    devtools and delete a CSS rule.

    What actually protects the data is this file: an anonymous request is
    served a payload that DOES NOT CONTAIN the locked numbers. There is
    nothing behind the blur to find, because it was never sent.

    So every widening of scope below is guarded by `member`, and `member` comes
    from a signed cookie that a browser cannot forge.

Run:
    .venv/bin/gunicorn -w 2 -b 127.0.0.1:8080 "dashboard.app:create_app()"
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, render_template, request

from dashboard import data


def _viewer(app):
    """Who is asking, or None.

    Imported lazily and defensively. The dashboard's job is to render cabbage
    temperatures for the public; if the auth stack is broken or half-installed
    that should downgrade the page to its anonymous view, not take the whole
    site down. Failing closed here means a stranger sees exactly what a
    stranger is meant to see.
    """
    try:
        from src import whoami
        return whoami.current(request)
    except Exception:
        app.logger.debug("auth unavailable, serving public view", exc_info=True)
        return None


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024

    # Needed to READ the session cookie the API issued. Both processes sign
    # with the same key, so a login on either is recognised by both.
    app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "")

    # visitors.is_granted() reads Mongo, and src/db.py holds its connection in
    # a module-level global that init_db() sets. Without this call every
    # approval check raises inside this process.
    #
    # Failure here is not fatal on purpose: the dashboard's own reader has a
    # separate connection, so charts and readings keep working even if this
    # one cannot be established. Only the photo-approval check degrades.
    app.config["MONGO_URI"] = os.getenv(
        "MONGO_URI", "mongodb://127.0.0.1:27017/agrisync")
    try:
        from src.db import init_db
        init_db(app)
    except Exception:
        app.logger.warning("no mongo connection for approval checks",
                           exc_info=True)

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers.pop("Server", None)
        return resp

    @app.get("/")
    def index():
        who = _viewer(app)
        member = who is not None

        # A signed-in page is personal and must never be handed to the next
        # visitor by a cache. The public one is identical for everybody and can
        # sit at the edge for a minute.
        cache = ("private, no-store" if member
                 else "public, max-age=60")

        requested = request.args.get("hours", data.DEFAULT_HOURS)
        hours = data.clamp(requested, member=member)

        try:
            payload = {
                "site": data.site(),
                "latest": data.latest(),
                "stats": data.stats(hours),
                "series": data.series(hours),
                "wet": data.wet_hours(hours),
                "risk": data.risk(),
                "deeper": data.deeper_access(),
                "hours": hours,
                "db_ok": True,
            }
            # Outside feeds, refreshed by scripts/update_feeds.py. Each one
            # degrades to {"available": False} on its own.
            payload.update({
                "forecast": data.forecast_risk(),
                "compare": data.comparison(),
                "sat": data.satellite(),
                "radar": data.radar(),
                "optical_gaps": data.optical_gaps(),
                "check": data.disease_or_stress(),
                "anom": data.anomalies(),
                "fieldlog": data.field_log_summary(),
            })
        except Exception as exc:
            app.logger.warning("dashboard degraded: %s", exc)
            payload = {
                "site": data.site(), "latest": None, "stats": {}, "series": {},
                "wet": {"available": False}, "risk": {"available": False},
                "deeper": data.deeper_access(), "hours": hours, "db_ok": False,
                "forecast": {"available": False}, "compare": {"available": False},
                "sat": {"available": False}, "anom": {"available": False},
                "radar": {"available": False}, "optical_gaps": None,
                "check": {"available": False},
                "fieldlog": {"count": 0, "last": None},
            }

        # ---- members only -------------------------------------------------
        # Everything below is added ONLY for a signed-in viewer. An anonymous
        # response does not carry these keys at all, so the teaser has nothing
        # underneath it.
        payload["member"] = member
        payload["viewer"] = who or {}
        payload["is_admin"] = False
        payload["ranges"] = data.RANGES
        payload["span"] = data.span()

        # Every link is a PATH, never a hostname. Written as
        # "https://api.agrisync.tech/photos" these would tell every visitor
        # where the admin surface lives; written as "/photos" the browser stays
        # on dashboard.agrisync.tech and the tunnel forwards the path to the
        # API process behind the scenes. Nothing in the page names the API
        # host, so nothing in the page invites anyone to go poking at it.
        payload["links"] = {
            "photos": "/photos",
            "csv": "/export/readings.csv",
            "zip": "/photos/download-all",
            "visitors": "/admin/visitors",
            "requests": "/admin/requests",
        }

        # A stranger gets the top five rows - real readings, just the start of
        # the table, with the rest fading out under skeleton bars. Five rather
        # than zero because an empty table proves nothing, and rather than a
        # hundred because the point is to show the shape, not give it away.
        try:
            payload["table"] = data.table(
                hours, limit=data.TABLE_LIMIT if member else data.TABLE_PUBLIC)
        except Exception:
            app.logger.warning("readings table unavailable", exc_info=True)
            payload["table"] = []

        # Signed in is not the same as approved for images. Thumbnails are
        # served from /photos/img/, which 403s for anyone not granted - so
        # asking the browser to load them for a member who has not been
        # approved would just fill the section with broken images.
        # is_granted decides the ARCHIVE. The four newest are shown to any
        # signed-in visitor - enough to see what the camera looks at, not
        # enough to be the dataset.
        payload["can_see_photos"] = False   # approved: gallery, downloads
        payload["has_requested"] = False    # asked, still waiting
        payload["thumbs"] = []
        payload["photo_total"] = 0

        if member:
            try:
                from src import visitors
                email = who["email"]
                payload["is_admin"] = visitors.is_admin(email)
                payload["can_see_photos"] = visitors.is_granted(email)
                payload["has_requested"] = visitors.has_requested(email)
                payload["photo_total"] = data.photo_count()
            except Exception:
                app.logger.warning("photo access check failed", exc_info=True)

        resp = app.make_response(render_template("index.html", **payload))
        resp.headers["Cache-Control"] = cache
        return resp

    @app.get("/data.json")
    def data_json():
        """Same numbers, for the page to refresh itself without a reload."""
        member = _viewer(app) is not None
        hours = data.clamp(request.args.get("hours", data.DEFAULT_HOURS),
                           member=member)
        try:
            out = {
                "latest": data.latest(),
                "stats": data.stats(hours),
                "series": data.series(hours),
                "wet": data.wet_hours(hours),
                "risk": data.risk(),
                "hours": hours,
                "member": member,
            }
            if member:
                out["table"] = data.table(hours)
            resp = jsonify(out)
            resp.headers["Cache-Control"] = ("private, no-store" if member
                                             else "public, max-age=60")
            return resp
        except Exception as exc:
            return jsonify({"error": "data unavailable",
                            "detail": str(exc)[:120]}), 503

    # agrisync.tech is static on GitHub Pages and reads this to show live
    # readings. Listed origins, not a wildcard - this endpoint carries no
    # credentials, but a wildcard is a habit worth not forming.
    @app.after_request
    def data_cors(resp):
        if request.path == "/data.json":
            origin = (request.headers.get("Origin") or "").rstrip("/")
            if origin in ("https://agrisync.tech", "https://www.agrisync.tech"):
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Vary"] = "Origin"
        return resp

    @app.get("/health")
    def health():
        return jsonify({"service": "agrisync-dashboard",
                        "status": "ok" if data.healthy() else "degraded"})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("DASH_PORT", "8080")))
