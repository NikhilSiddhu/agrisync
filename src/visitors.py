"""A register of who has signed in.

Goes at:  ~/agrisync/src/visitors.py

Cloudflare verifies the email; it cannot tell us a name, because a one-time PIN
proves an address and nothing more. So name and phone are asked for once, on
first visit, and stored against that verified address.

WHAT IS STORED

  email        verified by Cloudflare, never typed by the user
  name         required, asked once
  phone        required, asked once, format-checked
  organisation optional
  purpose      optional
  first_seen, last_seen, visits

No IP addresses, no user agents, no tracking, no cookies of our own. Everything
beyond name and phone is genuinely optional and the form says so. If anyone
ever asks what you hold about them, that list is the whole answer.

ON "VERIFYING" THE PHONE NUMBER

  We check the format: ten digits, starting 6-9, which is every Indian mobile.
  That catches typos and casual nonsense.

  It does NOT prove the person owns that number. Nothing does, short of texting
  them a code, which needs a paid SMS gateway. Anyone telling you a form field
  is "verified" without one is describing a format check.

  The email IS genuinely verified, because Cloudflare made them prove it before
  your app ever saw the request. That is the identity you can actually rely on.
"""
import os
import re
from datetime import datetime, timezone

from pymongo import DESCENDING

from src.db import get_db


MAX_LEN = {"name": 80, "phone": 32, "organisation": 120, "purpose": 300}

# Indian mobile numbers: ten digits, first is 6-9.
PHONE_RE = re.compile(r"^[6-9]\d{9}$")

# What someone can ask for. One approval covers everything - a person you trust
# with the raw readings is a person you trust with pictures of cabbages - but
# the register records which things they actually asked about, so you can see
# why they came.
SCOPES = ("dataset", "photos")


# ------------------------------------------------------------------- admins --

def admin_emails():
    """ADMIN_EMAILS in .env, comma separated."""
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin(email):
    return bool(email) and email.strip().lower() in admin_emails()


# ------------------------------------------------------------------- phone ---

def clean_phone(raw):
    """Returns a ten digit number, or None if it is not plausible.

    Tolerates the ways people actually type numbers: +91, leading 0, spaces,
    dashes, brackets. Rejecting a valid number because someone wrote it with
    spaces is a bad reason to lose a request.
    """
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits if PHONE_RE.match(digits) else None


# ---------------------------------------------------------------- visitors ---

def record(email, path=None):
    """Upsert one visitor. Cheap enough to call on every page view."""
    if not email:
        return
    now = datetime.now(timezone.utc)
    get_db().visitors.update_one(
        {"email": email},
        {"$setOnInsert": {"email": email, "first_seen": now},
         "$set": {"last_seen": now, "last_path": path},
         "$inc": {"visits": 1}},
        upsert=True,
    )


def get(email):
    if not email:
        return None
    return get_db().visitors.find_one({"email": email}, {"_id": 0})


def needs_profile(email):
    """True until they have given both a name and a usable phone number.

    Existing visitors who only gave a name will be asked once for the number.
    That is deliberate: a register with half the contact details is not much
    of a register.
    """
    if not email:
        return False
    doc = get(email)
    if not doc:
        return True
    has_name = bool((doc.get("name") or "").strip())
    has_phone = bool(clean_phone(doc.get("phone")))
    return not (has_name and has_phone)


def save_profile(email, name, phone=None, organisation=None, purpose=None):
    """Name and phone are required. Everything else is stored only if given.

    Empty optional fields are not written at all, rather than written as empty
    strings, so the register never implies we asked for something and got a
    blank rather than never holding it.
    """
    if not email:
        return False, "no verified email"

    name = (name or "").strip()[:MAX_LEN["name"]]
    if not name:
        return False, "name is required"

    number = clean_phone(phone)
    if not number:
        return False, ("phone number is required, and should be a 10 digit "
                       "Indian mobile")

    fields = {"name": name, "phone": number}
    for key, value in (("organisation", organisation), ("purpose", purpose)):
        value = (value or "").strip()[:MAX_LEN[key]]
        if value:
            fields[key] = value

    now = datetime.now(timezone.utc)
    get_db().visitors.update_one(
        {"email": email},
        {"$set": {**fields, "profile_saved_at": now},
         "$setOnInsert": {"email": email, "first_seen": now}},
        upsert=True,
    )
    return True, "saved"


def display_name(email):
    doc = get(email)
    return (doc or {}).get("name") or ""


def listing(limit=500):
    rows = list(get_db().visitors.find({}, {"_id": 0})
                .sort("last_seen", DESCENDING).limit(limit))
    for r in rows:
        for key in ("first_seen", "last_seen"):
            v = r.get(key)
            if hasattr(v, "strftime"):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=timezone.utc)
                r[key] = v.strftime("%d %b %Y, %H:%M")
    return rows


def count():
    try:
        return get_db().visitors.estimated_document_count()
    except Exception:
        return 0


# ---------------------------------------------------------------- requests ---

def record_request(email, note=None, scope="dataset"):
    """Someone signed in has asked for access.

    scope says what they asked for - "dataset" or "photos". Both are tracked on
    one record in a `wants` list, because one approval covers everything and
    two parallel approval queues would be two things to forget about. You still
    see which they asked for, and in what order.

    Returns (created, doc). created is False if they had already asked, so the
    caller can avoid pinging your phone twice for the same person.
    """
    if not email:
        return False, None
    if scope not in SCOPES:
        scope = "dataset"

    now = datetime.now(timezone.utc)
    existing = get_db().data_requests.find_one({"email": email})

    if existing:
        already = scope in (existing.get("wants") or [])
        get_db().data_requests.update_one(
            {"email": email},
            {"$set": {"last_asked": now},
             "$inc": {"times_asked": 1},
             "$addToSet": {"wants": scope}})
        # Treat a new scope as worth telling you about, even from someone who
        # has asked before - "they now also want the photos" is news.
        return (not already), existing

    doc = {"email": email, "first_asked": now, "last_asked": now,
           "times_asked": 1, "note": (note or "")[:500],
           "wants": [scope], "granted": False}
    get_db().data_requests.insert_one(doc)
    return True, doc


def requests_listing(limit=200):
    rows = list(get_db().data_requests.find({}, {"_id": 0})
                .sort("last_asked", DESCENDING).limit(limit))
    for r in rows:
        for key in ("first_asked", "last_asked"):
            v = r.get(key)
            if hasattr(v, "strftime"):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=timezone.utc)
                r[key] = v.strftime("%d %b %Y, %H:%M")

        # Older records predate scopes. Show them as what they were.
        if not r.get("wants"):
            r["wants"] = ["dataset"]
        r["wants_label"] = " + ".join(r["wants"])

        # Pull the name and phone through so the approval screen shows a
        # person rather than an address.
        who = get(r.get("email")) or {}
        r["name"] = who.get("name") or ""
        r["phone"] = who.get("phone") or ""
        r["organisation"] = who.get("organisation") or ""
        r["purpose"] = who.get("purpose") or r.get("note") or ""
    return rows


def has_requested(email, scope=None):
    if not email:
        return False
    query = {"email": email}
    if scope:
        query["wants"] = scope
    return get_db().data_requests.count_documents(query) > 0


def set_granted(email, granted=True):
    """Approve or revoke access. One switch, covers dataset and photos."""
    if not email:
        return False
    now = datetime.now(timezone.utc)
    res = get_db().data_requests.update_one(
        {"email": email},
        {"$set": {"granted": bool(granted),
                  "granted_at": now if granted else None}})
    return res.matched_count > 0


def is_granted(email):
    """Approved people download live data, not a stale snapshot - the export
    streams straight from the database, so there is nothing to keep in sync.

    Admins are granted implicitly. Making the owner of the box queue up to
    approve himself is the kind of thing that seems rigorous and is just
    friction.
    """
    if not email:
        return False
    if is_admin(email):
        return True
    return get_db().data_requests.count_documents(
        {"email": email, "granted": True}) > 0
