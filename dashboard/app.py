"""Public AgriSync dashboard.

Runs as its OWN service on port 8080, separate from the API on 5000.

That separation is a security boundary, not tidiness. This hostname has no
Cloudflare Access in front of it, so anyone on the internet can reach it. If it
shared a process with the API, a visitor could request an API path on this
hostname and walk straight past the Access policy protecting the other one.

This process:
  - only ever reads from MongoDB
  - only ever returns HTML and chart data
  - caps every query at 7 days
  - holds no keys and exposes no device credentials

Run:
    .venv/bin/gunicorn -w 2 -b 127.0.0.1:8080 "dashboard.app:create_app()"
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify, render_template, request

from dashboard import data


def create_app():
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 8 * 1024

    @app.after_request
    def headers(resp):
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
        resp.headers["Referrer-Policy"] = "no-referrer"
        # Public page, changes once a minute. Let the edge cache it briefly so a
        # burst of visitors does not hammer a Raspberry Pi.
        resp.headers.setdefault("Cache-Control", "public, max-age=60")
        resp.headers.pop("Server", None)
        return resp

    @app.get("/")
    def index():
        hours = data.clamp(request.args.get("hours", data.MAX_PUBLIC_HOURS))
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
        except Exception as exc:
            app.logger.warning("dashboard degraded: %s", exc)
            payload = {
                "site": data.site(), "latest": None, "stats": {}, "series": {},
                "wet": {"available": False}, "risk": {"available": False},
                "deeper": data.deeper_access(), "hours": hours, "db_ok": False,
            }
        return render_template("index.html", **payload)

    @app.get("/data.json")
    def data_json():
        """Same numbers, for the page to refresh itself without a reload."""
        hours = data.clamp(request.args.get("hours", data.MAX_PUBLIC_HOURS))
        try:
            return jsonify({
                "latest": data.latest(),
                "stats": data.stats(hours),
                "series": data.series(hours),
                "wet": data.wet_hours(hours),
                "risk": data.risk(),
                "hours": hours,
            })
        except Exception as exc:
            return jsonify({"error": "data unavailable", "detail": str(exc)[:120]}), 503

    @app.get("/health")
    def health():
        return jsonify({"service": "agrisync-dashboard",
                        "status": "ok" if data.healthy() else "degraded"})

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.getenv("DASH_PORT", "8080")))
