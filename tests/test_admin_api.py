"""The admin JSON that agrisync.tech's admin drawer reads."""
import sys
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as db_module


@pytest.fixture
def setup(monkeypatch):
    fake = mongomock.MongoClient()["agrisync_test"]
    monkeypatch.setattr(db_module, "_db", fake)
    monkeypatch.setattr(db_module, "init_db", lambda app: None)
    from main.app import create_app
    from src.api import admin_api

    class C:
        MONGO_URI = "mongodb://fake"
        DEVICE_API_KEY = "k"
        TESTING = True

    app = create_app(C)
    monkeypatch.setattr(db_module, "_db", fake)
    state = {"admin": True}
    monkeypatch.setattr(admin_api.whoami, "current", lambda req=None: {"email": "owner@example.com"})
    monkeypatch.setattr(admin_api.access, "is_admin", lambda ident: state["admin"])
    monkeypatch.setenv("ADMIN_EMAILS", "owner@example.com")
    now = datetime(2026, 10, 22, 8, tzinfo=timezone.utc)
    fake.visitors.insert_many([
        {"email": "owner@example.com", "name": "Owner", "visits": 9, "first_seen": now, "last_seen": now},
        {"email": "asha@example.com", "name": "Asha", "phone": "9876543210", "visits": 2, "first_seen": now, "last_seen": now}])
    fake.data_requests.insert_one({"email": "asha@example.com", "first_asked": now, "last_asked": now,
                                   "times_asked": 1, "note": "thesis", "wants": ["dataset"], "granted": False})
    fake.field_log.insert_one({"date": "2026-10-20", "disease": "none"})
    return app.test_client(), fake, state


def test_admin_gets_the_overview_with_credentialed_cors(setup):
    client, _, _ = setup
    r = client.get("/admin/api/overview", headers={"Origin": "https://agrisync.tech"})
    assert r.status_code == 200
    assert r.headers["Access-Control-Allow-Origin"] == "https://agrisync.tech"
    assert r.headers["Access-Control-Allow-Credentials"] == "true"
    assert "no-store" in r.headers["Cache-Control"]
    d = r.get_json()
    assert d["total"] == 1 and d["visitors"][0]["email"] == "asha@example.com"   # the owner is left out
    assert d["requests"][0]["purpose"] == "thesis" and d["requests"][0]["granted"] is False
    assert d["field_log"] == {"count": 1, "last": "2026-10-20"}


def test_strangers_and_other_sites_get_nothing(setup):
    client, _, state = setup
    r = client.get("/admin/api/overview", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    state["admin"] = False
    r = client.get("/admin/api/overview", headers={"Origin": "https://agrisync.tech"})
    assert r.status_code == 403 and "visitors" not in r.get_json()


def test_admin_can_approve_and_revoke(setup):
    client, fake, _ = setup
    assert client.post("/admin/api/requests/asha@example.com/approve").status_code == 200
    assert fake.data_requests.find_one({"email": "asha@example.com"})["granted"] is True
    assert client.post("/admin/api/requests/asha@example.com/revoke").get_json()["status"] == "revoked"
    assert client.post("/admin/api/requests/asha@example.com/delete").status_code == 400
    assert client.post("/admin/api/requests/nobody@example.com/approve").status_code == 404
