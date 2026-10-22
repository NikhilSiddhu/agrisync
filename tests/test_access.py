"""Cloudflare Access identity and the visitor register.

The security-relevant assertion here is that a forged header gets you nothing.
"""
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import access, db as db_module, visitors

TEAM = "agrisynctech.cloudflareaccess.com"
AUD = "abc123def456"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("CF_ACCESS_TEAM_DOMAIN", "CF_ACCESS_AUD", "ADMIN_EMAILS"):
        monkeypatch.delenv(k, raising=False)
    access._jwk_client = None
    access._jwk_fetched_at = 0.0


class FakeRequest:
    def __init__(self, headers=None, cookies=None, path="/"):
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.path = path


# --- fails closed ---

def test_no_identity_when_unconfigured():
    assert access.configured() is False
    assert access.identity(FakeRequest({"Cf-Access-Jwt-Assertion": "anything"})) is None


def test_no_identity_without_a_token(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    assert access.identity(FakeRequest()) is None


def test_forged_token_is_rejected(monkeypatch):
    """The whole point. An unsigned JWT claiming to be someone must fail."""
    import jwt as pyjwt
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    forged = pyjwt.encode({"email": "attacker@example.com", "aud": AUD},
                          "not-cloudflares-key", algorithm="HS256")
    assert access.identity(FakeRequest({"Cf-Access-Jwt-Assertion": forged})) is None


def test_garbage_token_is_rejected(monkeypatch):
    monkeypatch.setenv("CF_ACCESS_TEAM_DOMAIN", TEAM)
    monkeypatch.setenv("CF_ACCESS_AUD", AUD)
    assert access.identity(FakeRequest({"Cf-Access-Jwt-Assertion": "not.a.jwt"})) is None


# --- admin gate ---

def test_nobody_is_admin_by_default(monkeypatch):
    assert access.is_admin({"email": "someone@example.com"}) is False


def test_admin_matches_the_env_list(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com, other@example.com")
    assert access.is_admin({"email": "me@example.com"}) is True
    assert access.is_admin({"email": "other@example.com"}) is True
    assert access.is_admin({"email": "stranger@example.com"}) is False


def test_admin_check_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "Me@Example.com")
    assert access.is_admin({"email": "me@example.com"}) is True


def test_anonymous_is_never_admin(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "me@example.com")
    assert access.is_admin(None) is False
    assert access.is_admin({"email": None}) is False


# --- the register ---

@pytest.fixture
def db(monkeypatch):
    fake = mongomock.MongoClient()["visreg"]
    monkeypatch.setattr(db_module, "_db", fake)
    return fake


def test_records_a_visitor(db):
    visitors.record("a@example.com", "/")
    rows = visitors.listing()
    assert len(rows) == 1
    assert rows[0]["email"] == "a@example.com"
    assert rows[0]["visits"] == 1


def test_repeat_visits_increment_not_duplicate(db):
    for _ in range(4):
        visitors.record("a@example.com", "/")
    rows = visitors.listing()
    assert len(rows) == 1
    assert rows[0]["visits"] == 4


def test_first_seen_is_preserved(db):
    visitors.record("a@example.com")
    first = db.visitors.find_one({"email": "a@example.com"})["first_seen"]
    time.sleep(0.01)
    visitors.record("a@example.com")
    assert db.visitors.find_one({"email": "a@example.com"})["first_seen"] == first


def test_empty_email_is_ignored(db):
    visitors.record(None)
    visitors.record("")
    assert visitors.count() == 0


def test_stores_nothing_beyond_the_minimum(db):
    """If you have to explain what you keep about people, keep it short."""
    visitors.record("a@example.com", "/")
    doc = db.visitors.find_one({"email": "a@example.com"})
    assert set(doc.keys()) <= {"_id", "email", "first_seen", "last_seen", "visits", "last_path"}
    for banned in ("ip", "ip_address", "user_agent", "referrer"):
        assert banned not in doc


# --- dataset requests ---

def test_records_a_request(db):
    created, _ = visitors.record_request("a@example.com", "for a paper")
    assert created is True
    rows = visitors.requests_listing()
    assert len(rows) == 1
    assert rows[0]["email"] == "a@example.com"
    assert rows[0]["note"] == "for a paper"


def test_second_request_does_not_notify_again(db):
    assert visitors.record_request("a@example.com")[0] is True
    assert visitors.record_request("a@example.com")[0] is False   # no second buzz
    rows = visitors.requests_listing()
    assert len(rows) == 1
    assert rows[0]["times_asked"] == 2


def test_has_requested(db):
    assert visitors.has_requested("a@example.com") is False
    visitors.record_request("a@example.com")
    assert visitors.has_requested("a@example.com") is True


def test_request_note_is_truncated(db):
    visitors.record_request("a@example.com", "x" * 5000)
    assert len(visitors.requests_listing()[0]["note"]) == 500


def test_anonymous_cannot_request(db):
    assert visitors.record_request(None)[0] is False
    assert visitors.requests_listing() == []


# --- profiles ---

def test_profile_is_needed_before_a_name_is_given(db):
    visitors.record("a@example.com")
    assert visitors.needs_profile("a@example.com") is True


def test_profile_no_longer_needed_after_saving(db):
    visitors.save_profile("a@example.com", name="Nikhil", phone="98765 43210")
    assert visitors.needs_profile("a@example.com") is False


def test_phone_is_required_too(db):
    ok, err = visitors.save_profile("a@example.com", name="Nikhil")
    assert ok is False and "phone" in err
    ok, err = visitors.save_profile("a@example.com", name="Nikhil", phone="12345")
    assert ok is False and "phone" in err
    assert visitors.needs_profile("a@example.com") is True


def test_name_is_required(db):
    ok, err = visitors.save_profile("a@example.com", name="   ")
    assert ok is False and "name" in err
    assert visitors.needs_profile("a@example.com") is True


def test_optional_fields_are_stored_when_given(db):
    visitors.save_profile("a@example.com", name="Nikhil", phone="+91 90000 00000",
                          organisation="AgriSync", purpose="research")
    doc = visitors.get("a@example.com")
    assert doc["phone"] == "9000000000"          # stored as the bare ten digits
    assert doc["organisation"] == "AgriSync"


def test_blank_optionals_are_not_written_at_all(db):
    """A missing key is honest. An empty string implies we asked and got nothing."""
    visitors.save_profile("a@example.com", name="Nikhil", phone="9876543210",
                          organisation="", purpose="  ")
    doc = visitors.get("a@example.com")
    assert "organisation" not in doc
    assert "purpose" not in doc


def test_fields_are_length_capped(db):
    visitors.save_profile("a@example.com", name="N" * 500, phone="9876543210",
                          organisation="O" * 500, purpose="P" * 5000)
    doc = visitors.get("a@example.com")
    assert len(doc["name"]) == 80
    assert doc["phone"] == "9876543210"
    assert len(doc["organisation"]) == 120
    assert len(doc["purpose"]) == 300


def test_display_name_is_blank_until_a_name_is_saved(db):
    """No guessing a name from the email address (changed in 787147f): the
    page asks for the profile instead."""
    visitors.record("someone@example.com")
    assert visitors.display_name("someone@example.com") == ""
    visitors.save_profile("someone@example.com", name="Real Name", phone="9876543210")
    assert visitors.display_name("someone@example.com") == "Real Name"


def test_profile_save_keeps_first_seen(db):
    visitors.record("a@example.com")
    first = visitors.get("a@example.com")["first_seen"]
    visitors.save_profile("a@example.com", name="Nikhil")
    assert visitors.get("a@example.com")["first_seen"] == first


def test_anonymous_cannot_save_a_profile(db):
    ok, _ = visitors.save_profile(None, name="Nobody")
    assert ok is False


# --- granting the dataset ---

def test_nobody_is_granted_by_default(db):
    visitors.record_request("a@example.com")
    assert visitors.is_granted("a@example.com") is False


def test_grant_and_revoke(db):
    visitors.record_request("a@example.com")
    assert visitors.set_granted("a@example.com", True) is True
    assert visitors.is_granted("a@example.com") is True
    visitors.set_granted("a@example.com", False)
    assert visitors.is_granted("a@example.com") is False


def test_cannot_grant_someone_who_never_asked(db):
    assert visitors.set_granted("stranger@example.com", True) is False
    assert visitors.is_granted("stranger@example.com") is False


def test_anonymous_is_never_granted(db):
    assert visitors.is_granted(None) is False
    assert visitors.is_granted("") is False
