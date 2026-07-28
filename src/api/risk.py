"""Disease risk endpoint.

Returns, per disease: current pressure, the peak over the next 12 h and
72 h from forecast, and measured_current computed from the node's own
readings once it has reported enough of them.

Always carries calibrated: false. See src/ml/physics_baseline.py for why.
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, current_app, jsonify

from src.db import get_db
from src.ml.physics_baseline import DISEASES, evaluate_all, wet_hours_from_readings
from src.ml.wetness import dew_point_c, leaf_wetness_index
from src.weather.forecast import fetch_forecast

bp = Blueprint("risk", __name__, url_prefix="/api/v1/risk")

MEASURED_WINDOW_HOURS = 24


@bp.get("/<device_id>")
def risk(device_id):
    # --- measured side: what the box actually saw ---
    since = datetime.now(timezone.utc) - timedelta(hours=MEASURED_WINDOW_HOURS)
    db_error = None
    readings = []
    try:
        # quality_flags absent means the row passed every plausibility check.
        # A failing sensor must never quietly reach a disease number.
        readings = list(
            get_db().readings.find({
                "device_id": device_id,
                "ts": {"$gte": since},
                "quality_flags": {"$exists": False},
            }).sort("ts", 1)
        )
    except Exception as exc:
        # Mongo down should degrade to forecast-only, not throw a 500.
        db_error = str(exc)
        current_app.logger.warning("risk: mongo unreachable: %s", exc)

    measured_wet_hours, measured_temp = wet_hours_from_readings(readings)
    measured = None
    if measured_temp is not None:
        measured = evaluate_all(measured_temp, measured_wet_hours)

    # --- forecast side ---
    cfg = current_app.config
    forecast = fetch_forecast(cfg["SITE_LAT"], cfg["SITE_LON"], cfg["SITE_TZ"])

    if forecast is None and measured is None:
        return jsonify({
            "error": "no signal",
            "detail": (
                "live weather is unreachable and this device has not reported "
                "enough readings to compute measured wetness yet"
            ),
            "db_error": db_error,
        }), 503

    peaks = {}
    if forecast:
        peaks = _forecast_peaks(forecast)

    return jsonify({
        "device_id": device_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "calibrated": False,
        "measured": {
            "db_error": db_error,
            "window_hours": MEASURED_WINDOW_HOURS,
            "readings_used": len(readings),
            "wet_hours": round(measured_wet_hours, 2),
            "mean_temp_during_wet_c": measured_temp,
            "current": measured,
        },
        "forecast": peaks,
        "disclaimer": (
            "Physics floor only. Leaf wetness is a dew-point proxy, not a "
            "measured signal. Placeholder parameters are in use for some "
            "diseases. Do not make spray decisions from this alone."
        ),
    })


def _forecast_peaks(forecast):
    """Roll the forecast forward, accumulating wet hours, and take the peak."""
    out = {}
    for horizon in (12, 72):
        window = forecast[:horizon]
        wet_hours = 0.0
        peak = {k: 0.0 for k in DISEASES}

        for row in window:
            leaf_proxy = leaf_wetness_index(
                row["temp_c"], row.get("dew_point_c")
                if row.get("dew_point_c") is not None
                else dew_point_c(row["temp_c"], row["humidity_pct"])
            )
            raining = (row.get("precip_mm") or 0) > 0.2
            if leaf_proxy >= 50.0 or raining:
                wet_hours += 1.0
            else:
                wet_hours = max(0.0, wet_hours - 1.0)  # drying resets slowly

            for key, model in DISEASES.items():
                peak[key] = max(peak[key], model.pressure(row["temp_c"], wet_hours))

        out[f"peak_{horizon}h"] = {
            key: {
                "name": DISEASES[key].name,
                "pressure": round(value, 4),
                "parameters_sourced": DISEASES[key].sourced,
            }
            for key, value in peak.items()
        }
    return out
