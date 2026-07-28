"""AgriSync API entry point.

Dev:         python main/app.py
Production:  .venv/bin/gunicorn -w 1 -b 0.0.0.0:5000 "main.app:create_app()"

One worker only. Gunicorn forking multiple workers each holding their own
Mongo pool is pointless on a Pi handling a few requests a minute.
"""
import logging
import sys
from pathlib import Path

# Running `python main/app.py` puts main/ on the import path, not the repo
# root, so `import src.*` fails. Adding the repo root here means the command
# in the manual works as written. Gunicorn does not need this, but it is
# harmless there.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from flask import Flask, jsonify

from src.api.devices import bp as devices_bp
from src.api.health import bp as health_bp
from src.api.risk import bp as risk_bp
from src.api.sensors import bp as sensors_bp
from src.config import Config
from src.db import init_db


def create_app(config_object=Config):
    app = Flask(__name__)
    app.config.from_object(config_object)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    init_db(app)

    app.register_blueprint(health_bp)
    app.register_blueprint(devices_bp)
    app.register_blueprint(sensors_bp)
    app.register_blueprint(risk_bp)

    @app.get("/")
    def index():
        return jsonify({
            "service": "agrisync-api",
            "site": app.config["SITE_NAME"],
            "endpoints": [
                "GET  /health",
                "GET  /health/db",
                "POST /api/v1/sensors/readings   (needs X-API-Key header)",
                "GET  /api/v1/sensors/readings",
                "GET  /api/v1/risk/<device_id>",
                "GET  /api/v1/devices",
                "GET  /api/v1/devices/<device_id>",
            ],
        })

    if not app.config.get("DEVICE_API_KEY") or app.config["DEVICE_API_KEY"].startswith("change-me"):
        app.logger.warning(
            "DEVICE_API_KEY is not set in .env - every device POST will fail"
        )

    return app


if __name__ == "__main__":
    application = create_app()
    # host 0.0.0.0 is REQUIRED. On 127.0.0.1 the ESP32 cannot reach the Pi
    # over WiFi and you get a board that flashes fine and never reports.
    application.run(host="0.0.0.0", port=application.config["FLASK_PORT"], debug=True)
