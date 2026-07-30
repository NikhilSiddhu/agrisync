"""Serial bridge tests. No serial port, no network, no database."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.serial_bridge import PAYLOAD_PREFIX, forward, parse_payload

GOOD = ('[payload] {"device_id":"esp32-smoke-01","firmware":"1.3.0",'
        '"air_temp_c":25.2,"humidity_pct":47.6}')


class FakeResponse:
    def __init__(self, status, body=None, text=""):
        self.status_code = status
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, response=None, raise_exc=None):
        self.response = response
        self.raise_exc = raise_exc
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append((url, json, headers))
        if self.raise_exc:
            raise self.raise_exc
        return self.response


# --- parsing ---

def test_parses_a_real_payload_line():
    p = parse_payload(GOOD)
    assert p["device_id"] == "esp32-smoke-01"
    assert p["air_temp_c"] == 25.2


def test_ignores_boot_banner():
    assert parse_payload("=== AgriSync Board A  fw 1.3.0 ===") is None


def test_ignores_i2c_scan_output():
    assert parse_payload("[i2c] device at 0x76") is None


def test_ignores_mode_line():
    assert parse_payload("[mode] SERIAL ONLY - radio off") is None


def test_survives_truncated_json():
    """Connecting mid-transmission gives you half a line. Must not crash."""
    assert parse_payload('[payload] {"device_id":"esp32-smo') is None


def test_rejects_payload_without_device_id():
    assert parse_payload('[payload] {"air_temp_c":25.2}') is None


def test_rejects_non_object_payload():
    assert parse_payload('[payload] [1,2,3]') is None


def test_ignores_empty_line():
    assert parse_payload("") is None


# --- forwarding ---

def test_forward_success():
    s = FakeSession(FakeResponse(201, {"ts_source": "server", "quality_flags": []}))
    assert forward(parse_payload(GOOD), s) is True
    url, body, headers = s.calls[0]
    assert body["device_id"] == "esp32-smoke-01"
    assert "X-API-Key" in headers


def test_forward_reports_401_without_crashing():
    s = FakeSession(FakeResponse(401, text="unauthorized"))
    assert forward(parse_payload(GOOD), s) is False


def test_forward_survives_a_network_error():
    """The API restarting mid-run must not kill the bridge."""
    s = FakeSession(raise_exc=ConnectionError("refused"))
    assert forward(parse_payload(GOOD), s) is False


def test_forward_handles_500():
    s = FakeSession(FakeResponse(500, text="boom"))
    assert forward(parse_payload(GOOD), s) is False


def test_quality_flags_are_surfaced():
    s = FakeSession(FakeResponse(201, {"ts_source": "server",
                                       "quality_flags": ["i2c:all_zero"]}))
    assert forward(parse_payload(GOOD), s) is True
