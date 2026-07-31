"""Full-history view. Uncapped by design, so the tests check the cap is
genuinely absent here while still present on the public service."""
import math
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db as db_module

TEST_KEY = "hist-write-key"
READ_KEY = "hist-read-key"


def seed(db, days=30):
    random.seed(5)
    start = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    rows = []
    for m in range(0, days * 24 * 60, 5):        # every 5 min keeps the test quick
        ts = start + timedelta(minutes=m)
        lh = (ts.hour + ts.minute / 60.0 + 5.5) % 24
        air = 27.0 + 6.5 * math.cos((lh - 15) / 24 * 2 * math.pi)
        rh = max(30.0, min(99.0, 62.0 - 30.0 * math.cos((lh - 15) / 24 * 2 * math.pi)))
        leaf = air - 3.0 if (lh < 6.5 or lh > 20.5) else air + 1.6
        dew = air - (100 - rh) / 5.0
        doc = {"device_id": "esp32-smoke-01", "ts": ts,
               "air_temp_c": round(air, 2), "humidity_pct": round(rh, 2),
               "pressure_hpa": 975.0, "dew_point_c": round(dew, 2),
               "leaf_temp_c": round(leaf, 2),
               "leaf_wetness": 100.0 if leaf <= dew else 0.0}
        if random.random() < 0.01:
            doc["quality_flags"] = ["i2c:all_zero"]
        rows.append(doc)
    db.readings.insert_many(rows)


@pytest.fixture
def app(monkeypatch):
    fake_client = mongomock.MongoClient()
    fake_db = fake_client["agrisync_hist"]
    monkeypatch.setattr(db_module, "_client", fake_client)
    monkeypatch.setattr(db_module, "_db", fake_db)
    monkeypatch.setattr(db_module, "init_db", lambda app: None)
    seed(fake_db)

    from main.app import create_app

    class C:
        MONGO_URI = "mongodb://fake"; DEVICE_API_KEY = TEST_KEY; READ_API_KEY = READ_KEY
        BIND_HOST = "127.0.0.1"; SITE_NAME = "Hapur"; SITE_LAT = 28.717; SITE_LON = 77.774
        SITE_TZ = "Asia/Kolkata"; FLASK_PORT = 5000; TESTING = True

    application = create_app(C)
    monkeypatch.setattr(db_module, "_db", fake_db)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# --- content negotiation: machines must not break ---

def test_browser_gets_the_page(client):
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert b"Full history" in r.data


def test_json_client_still_gets_the_endpoint_index(client):
    r = client.get("/", headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.get_json()["service"] == "agrisync-api"


def test_format_json_override(client):
    assert client.get("/?format=json").get_json()["service"] == "agrisync-api"


# --- uncapped, unlike the public dashboard ---

def test_history_covers_the_whole_dataset(client):
    """30 days of seed data. The public service would have shown 7."""
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    assert "30.0 days" in body or "29.9 days" in body or "30 days" in body


def test_reports_totals(client):
    from src import history
    with client.application.app_context():
        s = history.span()
    assert s["empty"] is False
    assert s["total"] > 8000
    assert s["days"] > 25


def test_daily_table_is_newest_first(client):
    from src import history
    with client.application.app_context():
        rows = history.daily()
    assert len(rows) > 25
    assert rows[0]["readings"] > 0


def test_suspect_rows_counted_not_hidden(client):
    from src import history
    with client.application.app_context():
        s = history.span()
    assert s["suspect"] > 0


# --- CSV export ---

def test_csv_is_refused_without_identity(client, monkeypatch):
    """Signing in gets you the charts. It does not get you the raw season."""
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    assert client.get("/export/readings.csv").status_code == 403


def test_csv_read_key_alone_is_not_enough(client, monkeypatch):
    """The old ?key= route must not still open it."""
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    assert client.get(f"/export/readings.csv?key={READ_KEY}").status_code == 403


def test_csv_says_so_when_identity_is_unconfigured(client, monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    assert client.get("/export/readings.csv").status_code == 503


def test_csv_is_streamed_not_buffered(client):
    """A season is ~250k rows. Building that in memory on a Pi is an OOM."""
    from src import history
    with client.application.app_context():
        gen = history.stream_csv()
        first = next(gen)
    assert first.startswith("ts,device_id,")   # yielded before the query finished


def test_quality_flags_survive_the_csv(client):
    """Checked at the generator, since the route is now admin gated."""
    from src import history
    with client.application.app_context():
        text = "".join(history.stream_csv())
    assert "i2c:all_zero" in text


# --- the page itself ---

def test_page_offers_the_public_view(client):
    body = client.get("/", headers={"Accept": "text/html"}).data
    assert b"dashboard.agrisync.tech" in body


def test_page_is_behind_access(client):
    """The chip was replaced by the profile menu; the footer still states it."""
    assert b"Behind Cloudflare Access" in client.get("/", headers={"Accept": "text/html"}).data


def test_page_is_not_indexable(client):
    """Behind Access, but belt and braces."""
    assert b"noindex" in client.get("/", headers={"Accept": "text/html"}).data


def test_uses_the_same_design_tokens(client):
    body = client.get("/", headers={"Accept": "text/html"}).data
    for token in (b"--paper:#f4f2ec", b"--green:#3f5e39", b"Instrument+Serif"):
        assert token in body


def test_survives_an_empty_database(monkeypatch):
    fake = mongomock.MongoClient()
    monkeypatch.setattr(db_module, "_client", fake)
    monkeypatch.setattr(db_module, "_db", fake["blank"])
    monkeypatch.setattr(db_module, "init_db", lambda app: None)
    from main.app import create_app

    class C:
        MONGO_URI = "mongodb://fake"; DEVICE_API_KEY = "k"; READ_API_KEY = ""
        BIND_HOST = "127.0.0.1"; SITE_NAME = "Hapur"; SITE_LAT = 28.7; SITE_LON = 77.7
        SITE_TZ = "Asia/Kolkata"; FLASK_PORT = 5000; TESTING = True

    app = create_app(C)
    monkeypatch.setattr(db_module, "_db", fake["blank"])
    r = app.test_client().get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert b"No readings yet" in r.data


# --- the admin page, when identity is unavailable ---

def test_admin_page_refuses_when_access_is_unconfigured(client, monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    r = client.get("/admin/visitors")
    assert r.status_code == 503
    assert "identity unavailable" in r.get_json()["error"]


def test_admin_page_refuses_a_non_admin(client, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    r = client.get("/admin/visitors")     # no valid token
    assert r.status_code == 403


def test_history_page_renders_without_identity(client):
    """Identity is optional. The page must not depend on it."""
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert b"Full history" in r.data


def test_visitor_link_is_not_in_the_nav(client):
    """Admin plumbing does not belong in a page meant to look finished."""
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    head, _, tail = body.partition("</header>")
    assert "/admin/visitors" not in head


def test_visitor_link_is_hidden_from_non_admins(client):
    """No session in tests, so nobody is an admin and the link must be absent."""
    assert b"/admin/visitors" not in client.get("/", headers={"Accept": "text/html"}).data



# --- the request flow ---

def test_request_needs_a_signed_in_identity(client, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    assert client.post("/request-data", json={}).status_code == 401


def test_request_reports_when_identity_is_unconfigured(client, monkeypatch):
    monkeypatch.delenv("CF_ACCESS_TEAM_DOMAIN", raising=False)
    monkeypatch.delenv("CF_ACCESS_AUD", raising=False)
    assert client.post("/request-data", json={}).status_code == 503


def test_page_offers_a_request_not_a_download_to_non_admins(client):
    body = client.get("/", headers={"Accept": "text/html"}).data
    assert b"Request the dataset" in body
    assert b"Download CSV" not in body


def test_admin_requests_page_is_gated(client, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    assert client.get("/admin/requests").status_code == 403


def test_profile_endpoint_needs_identity(client, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    assert client.post("/profile", json={"name": "X"}).status_code == 401


def test_logout_link_uses_cloudflare(client, monkeypatch):
    """There is no session of ours to clear — only Cloudflare's."""
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    from main.app import create_app
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    assert "cdn-cgi/access/logout" in body or "logout" not in body


# --- profile gate and the profile menu ---

def test_no_logout_shown_when_not_signed_in(client):
    """Nothing to sign out of, so no button.

    Style and script blocks are stripped first: the stylesheet names these
    classes whether or not anything uses them, and the script carries a comment
    explaining the sign-out behaviour. Neither is rendered markup.
    """
    import re
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    rendered = re.sub(r"<(style|script)\b.*?</\1>", "", body, flags=re.S)
    assert 'id="signout"' not in rendered
    assert 'class="btn btn-glass pm-trigger"' not in rendered
    assert "Sign out" not in rendered


def test_request_form_shows_the_verified_email_disabled(client):
    """The address is never typed. It comes from the sign-in and is fixed."""
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    assert "disabled" in body
    assert "Verified when you signed in" in body


def test_no_second_gate_after_login(client):
    """Cloudflare's login is the only gate. The data is visible immediately."""
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    assert 'class="gate"' not in body
    assert "What should we" not in body


def test_no_optional_wording_on_the_form(client):
    """The star marks required. Labelling the rest 'Optional' is noise."""
    body = client.get("/", headers={"Accept": "text/html"}).data.decode()
    assert 'placeholder="Optional"' not in body
    assert ">Optional<" not in body


def test_public_view_link_always_present(client):
    assert b"dashboard.agrisync.tech" in client.get("/", headers={"Accept": "text/html"}).data


def test_grant_endpoint_is_admin_only(client, monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", "agrisynctech.cloudflareaccess.com")
    monkeypatch.setenv("CF_ACCESS_AUD", "aud123")
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    assert client.post("/admin/requests/x@y.com/approve").status_code == 403
