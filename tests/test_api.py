"""Tests you can run before touching any hardware.

    pytest -q

These use an in-memory fake Mongo, so they prove the API logic without
needing the database running. They do NOT prove your Pi is set up right -
that is what /health/db and the Stage A checkpoint are for.
"""
import mongomock
import pytest

from src import db as db_module
from src.ml.physics_baseline import DISEASES
from src.ml.wetness import dew_point_c, leaf_wetness_index

TEST_KEY = "test-key-not-a-real-secret"


@pytest.fixture
def app(monkeypatch):
    fake_client = mongomock.MongoClient()
    fake_db = fake_client["agrisync_test"]
    monkeypatch.setattr(db_module, "_client", fake_client)
    monkeypatch.setattr(db_module, "_db", fake_db)
    monkeypatch.setattr(db_module, "init_db", lambda app: None)

    from main.app import create_app

    class TestConfig:
        MONGO_URI = "mongodb://fake"
        DEVICE_API_KEY = TEST_KEY
        SITE_NAME = "Hapur"
        SITE_LAT = 28.7170
        SITE_LON = 77.7740
        SITE_TZ = "Asia/Kolkata"
        FLASK_PORT = 5000
        TESTING = True

    application = create_app(TestConfig)
    monkeypatch.setattr(db_module, "_db", fake_db)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# --- dew point and wetness math ---

def test_dew_point_known_value():
    # 25 C at 80 % RH is about 21.3 C dew point
    assert dew_point_c(25.0, 80.0) == pytest.approx(21.3, abs=0.2)


def test_dew_point_saturated_air():
    # At 100 % RH the dew point equals the air temperature
    assert dew_point_c(20.0, 100.0) == pytest.approx(20.0, abs=0.05)


def test_leaf_fully_wet_below_dew_point():
    assert leaf_wetness_index(19.0, 21.3) == 100.0


def test_leaf_fully_dry_two_degrees_above():
    assert leaf_wetness_index(23.3, 21.3) == 0.0


def test_leaf_half_wet_one_degree_above():
    assert leaf_wetness_index(22.3, 21.3) == pytest.approx(50.0, abs=0.1)


# --- disease model shape ---

def test_pressure_zero_when_too_dry():
    model = DISEASES["alternaria_brassicae"]
    assert model.pressure(18.0, 3.0) == 0.0  # below the 4 h minimum


def test_pressure_maxes_at_optimum():
    model = DISEASES["alternaria_brassicae"]
    assert model.pressure(18.0, 12.0) == pytest.approx(1.0, abs=0.01)


def test_pressure_zero_outside_temperature_range():
    model = DISEASES["alternaria_brassicae"]
    assert model.pressure(2.0, 20.0) == 0.0
    assert model.pressure(35.0, 20.0) == 0.0


def test_warm_strain_prefers_warmer_weather():
    cool = DISEASES["alternaria_brassicae"]
    warm = DISEASES["alternaria_brassicicola"]
    assert cool.pressure(18.0, 14.0) > warm.pressure(18.0, 14.0)
    assert warm.pressure(29.0, 14.0) > cool.pressure(29.0, 14.0)


# --- API ---

def test_index_lists_endpoints(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "agrisync-api" in resp.get_json()["service"]


def test_health_is_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_post_without_key_is_rejected(client):
    resp = client.post("/api/v1/sensors/readings", json={"device_id": "x"})
    assert resp.status_code == 401


def test_post_with_wrong_key_is_rejected(client):
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "x"},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401


def test_post_without_device_id_is_rejected(client):
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"air_temp_c": 25},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.status_code == 400


def test_post_stores_and_derives_wetness(client):
    resp = client.post(
        "/api/v1/sensors/readings",
        json={
            "device_id": "esp32-smoke-01",
            "air_temp_c": 25.0,
            "humidity_pct": 80.0,
            "leaf_temp_c": 21.0,
        },
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.status_code == 201

    rows = client.get("/api/v1/sensors/readings").get_json()
    assert rows["count"] == 1
    row = rows["readings"][0]
    assert row["dew_point_c"] == pytest.approx(21.3, abs=0.2)
    assert row["leaf_wetness"] == 100.0  # leaf is below dew point
    assert row["leaf_wetness_source"] == "proxy"


def test_post_rejects_non_numeric(client):
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "x", "air_temp_c": "hot"},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.status_code == 400


def test_readings_filter_by_device(client):
    for dev in ("box-a", "box-b"):
        client.post(
            "/api/v1/sensors/readings",
            json={"device_id": dev, "air_temp_c": 20.0, "humidity_pct": 70.0},
            headers={"X-API-Key": TEST_KEY},
        )
    rows = client.get("/api/v1/sensors/readings?device_id=box-a").get_json()
    assert rows["count"] == 1
    assert rows["readings"][0]["device_id"] == "box-a"


# --- validation and quality flags ---

def test_clean_reading_has_no_flags():
    from src.validation import check
    assert check({
        "air_temp_c": 25.0, "humidity_pct": 80.0,
        "pressure_hpa": 1002.0, "leaf_temp_c": 21.0, "dew_point_c": 21.3,
    }) == []


def test_dead_i2c_bus_is_detected():
    from src.validation import check
    flags = check({"air_temp_c": 0.0, "humidity_pct": 0.0, "pressure_hpa": 0.0})
    assert "i2c:all_zero" in flags


def test_impossible_dew_point_is_flagged():
    from src.validation import check
    assert "dew_point:above_air_temp" in check({"air_temp_c": 20.0, "dew_point_c": 25.0})


def test_out_of_range_is_flagged():
    from src.validation import check
    assert "air_temp_c:out_of_range" in check({"air_temp_c": 900.0})


def test_suspect_reading_is_stored_not_rejected(client):
    """Critical: a broken sensor must not put the board in a retry loop."""
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "esp32-smoke-01", "air_temp_c": 0.0,
              "humidity_pct": 0.0, "pressure_hpa": 0.0},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.status_code == 201
    assert "i2c:all_zero" in resp.get_json()["quality_flags"]


def test_clean_filter_excludes_suspect_rows(client):
    client.post("/api/v1/sensors/readings",
                json={"device_id": "d", "air_temp_c": 0.0, "humidity_pct": 0.0,
                      "pressure_hpa": 0.0},
                headers={"X-API-Key": TEST_KEY})
    client.post("/api/v1/sensors/readings",
                json={"device_id": "d", "air_temp_c": 24.0, "humidity_pct": 70.0,
                      "pressure_hpa": 1001.0},
                headers={"X-API-Key": TEST_KEY})
    assert client.get("/api/v1/sensors/readings").get_json()["count"] == 2
    assert client.get("/api/v1/sensors/readings?clean=1").get_json()["count"] == 1


# --- buffered readings keep their real timestamps ---

def test_device_timestamp_is_honoured(client):
    import time
    two_hours_ago = time.time() - 7200
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "d", "sampled_at": two_hours_ago,
              "air_temp_c": 22.0, "humidity_pct": 75.0},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.status_code == 201
    assert resp.get_json()["ts_source"] == "device"
    row = client.get("/api/v1/sensors/readings").get_json()["readings"][0]
    assert row["backfilled"] is True
    assert row["delay_seconds"] > 7000


def test_device_clock_in_the_future_is_rejected(client):
    import time
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "d", "sampled_at": time.time() + 86400,
              "air_temp_c": 22.0},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.get_json()["ts_source"] == "server_device_clock_ahead"


def test_unsynced_device_clock_falls_back_to_server(client):
    """A board that never reached NTP reports something near 1970."""
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "d", "sampled_at": 100.0, "air_temp_c": 22.0},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.get_json()["ts_source"] == "server_device_clock_behind"


def test_live_reading_is_not_marked_backfilled(client):
    client.post("/api/v1/sensors/readings",
                json={"device_id": "d", "air_temp_c": 22.0, "humidity_pct": 70.0},
                headers={"X-API-Key": TEST_KEY})
    row = client.get("/api/v1/sensors/readings").get_json()["readings"][0]
    assert "backfilled" not in row


# --- device health ---

def test_missing_device_reports_missing(client):
    body = client.get("/api/v1/devices/never-existed").get_json()
    assert body["status"] == "missing"


def test_healthy_device_reports_ok(client):
    client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "esp32-smoke-01", "air_temp_c": 24.0,
              "humidity_pct": 70.0, "pressure_hpa": 1001.0, "leaf_temp_c": 23.0,
              "sensors": {"bme280": True, "mlx90614": True, "ads1115": True}},
        headers={"X-API-Key": TEST_KEY},
    )
    body = client.get("/api/v1/devices/esp32-smoke-01").get_json()
    assert body["status"] == "ok"
    assert body["problems"] == []


def test_dropped_sensor_reports_degraded(client):
    client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "esp32-smoke-01", "air_temp_c": 24.0,
              "humidity_pct": 70.0, "pressure_hpa": 1001.0,
              "sensors": {"bme280": True, "mlx90614": False, "ads1115": True}},
        headers={"X-API-Key": TEST_KEY},
    )
    body = client.get("/api/v1/devices/esp32-smoke-01").get_json()
    assert body["status"] == "degraded"
    assert any("mlx90614" in p for p in body["problems"])


def test_flat_battery_reports_degraded(client):
    client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "esp32-smoke-01", "air_temp_c": 24.0,
              "humidity_pct": 70.0, "pressure_hpa": 1001.0, "leaf_temp_c": 23.0,
              "battery_v": 11.1,
              "sensors": {"bme280": True, "mlx90614": True, "ads1115": True}},
        headers={"X-API-Key": TEST_KEY},
    )
    body = client.get("/api/v1/devices/esp32-smoke-01").get_json()
    assert body["status"] == "degraded"
    assert any("battery" in p.lower() for p in body["problems"])


def test_device_list_flags_attention(client):
    client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "esp32-smoke-01", "air_temp_c": 24.0,
              "humidity_pct": 70.0,
              "sensors": {"bme280": True, "mlx90614": False, "ads1115": True}},
        headers={"X-API-Key": TEST_KEY},
    )
    resp = client.get("/api/v1/devices")
    assert resp.status_code == 207
    assert resp.get_json()["overall"] == "attention"


# --- emissivity / sky model ---

def test_sky_is_much_colder_than_air_when_dry():
    from src.ml.leaf_temp import sky_temperature_c
    assert sky_temperature_c(25.0, 40.0) < 15.0


def test_humid_sky_is_warmer_than_dry_sky():
    from src.ml.leaf_temp import sky_temperature_c
    assert sky_temperature_c(25.0, 95.0) > sky_temperature_c(25.0, 30.0)


def test_correction_raises_leaf_temp_against_a_cold_sky():
    from src.ml.leaf_temp import correct_leaf_temp
    assert correct_leaf_temp(18.0, -15.0) > 18.0


def test_no_correction_when_emissivity_is_one():
    from src.ml.leaf_temp import correct_leaf_temp
    assert correct_leaf_temp(18.0, -15.0, emissivity=1.0) == 18.0


def test_server_corrects_raw_leaf_temp(client):
    resp = client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "d", "air_temp_c": 18.0, "humidity_pct": 95.0,
              "leaf_temp_raw_c": 17.0, "mlx_ambient_c": 18.2},
        headers={"X-API-Key": TEST_KEY},
    )
    assert resp.status_code == 201
    row = client.get("/api/v1/sensors/readings").get_json()["readings"][0]
    assert row["leaf_temp_method"] == "sky_model"
    assert row["leaf_temp_raw_c"] == 17.0          # raw is preserved
    assert row["leaf_temp_c"] > row["leaf_temp_raw_c"]


def test_raw_is_always_kept_so_correction_can_be_redone(client):
    """The one thing that must never be lost."""
    client.post(
        "/api/v1/sensors/readings",
        json={"device_id": "d", "air_temp_c": 20.0, "humidity_pct": 80.0,
              "leaf_temp_raw_c": 19.0},
        headers={"X-API-Key": TEST_KEY},
    )
    row = client.get("/api/v1/sensors/readings").get_json()["readings"][0]
    assert "leaf_temp_raw_c" in row
