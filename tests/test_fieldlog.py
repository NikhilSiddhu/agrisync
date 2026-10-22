"""Field log page: admin only, validates input, stores one row per visit."""
import sys
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
    from src.api import fieldlog

    class C:
        MONGO_URI = "mongodb://fake"
        DEVICE_API_KEY = "k"
        TESTING = True

    app = create_app(C)
    monkeypatch.setattr(db_module, "_db", fake)
    state = {"admin": True}
    monkeypatch.setattr(fieldlog.whoami, "current", lambda req=None: {"email": "owner@example.com"})
    monkeypatch.setattr(fieldlog.access, "is_admin", lambda ident: state["admin"])
    return app.test_client(), fake, state


GOOD = {"date": "2026-10-21", "stage": "vegetative", "disease": "alternaria",
        "severity": "2", "plants_checked": "20", "plants_affected": "3", "notes": "north edge"}


def test_admin_can_add_and_see_a_row(setup):
    client, fake, _ = setup
    r = client.post("/admin/field-log", data=GOOD)
    assert r.status_code == 303 and r.headers["Location"].endswith("saved=1")
    row = fake.field_log.find_one()
    assert (row["disease"], row["plants_affected"], row["severity"]) == ("alternaria", 3, 2)
    assert row["recorded_by"] == "owner@example.com"
    page = client.get("/admin/field-log").data
    assert b"Alternaria leaf spot" in page and b"north edge" in page


def test_nothing_seen_stores_zero_damage(setup):
    client, fake, _ = setup
    client.post("/admin/field-log", data=dict(GOOD, disease="none"))
    row = fake.field_log.find_one()
    assert (row["plants_affected"], row["severity"]) == (0, 0)


@pytest.mark.parametrize("change,err", [
    ({"plants_affected": "30"}, "affected"),
    ({"date": "2099-01-01"}, "future"),
    ({"date": "21/10/2026"}, "date"),
    ({"severity": "9"}, "values"),
    ({"disease": "dragon"}, "values"),
])
def test_bad_input_is_refused_and_nothing_saved(setup, change, err):
    client, fake, _ = setup
    r = client.post("/admin/field-log", data=dict(GOOD, **change))
    assert r.status_code == 303 and r.headers["Location"].endswith("error=" + err)
    assert fake.field_log.count_documents({}) == 0


def test_strangers_are_turned_away(setup):
    client, fake, state = setup
    state["admin"] = False
    assert client.get("/admin/field-log").status_code == 403
    assert client.post("/admin/field-log", data=GOOD).status_code == 403
    assert fake.field_log.count_documents({}) == 0
