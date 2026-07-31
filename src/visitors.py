"""A register of who has signed in.

Cloudflare verifies the email; it cannot tell us a name, because a one-time PIN
proves an address and nothing more. So the name is asked for once, on first
visit, and stored against that verified address.

WHAT IS STORED

  email        verified by Cloudflare, never typed by the user
  name         required, asked once
  phone        optional
  organisation optional
  purpose      optional
  first_seen, last_seen, visits

No IP addresses, no user agents, no tracking, no cookies of our own. Everything
beyond name is genuinely optional and the form says so. If anyone ever asks what
you hold about them, that list is the whole answer.
"""
from datetime import datetime, timezone

from pymongo import DESCENDING

from src.db import get_db


MAX_LEN = {"name": 80, "phone": 32, "organisation": 120, "purpose": 300}


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
    """True until they have given a name. Asked once, never again."""
    if not email:
        return False
    doc = get(email)
    return not (doc and (doc.get("name") or "").strip())


def save_profile(email, name, phone=None, organisation=None, purpose=None):
    """Name is required. Everything else is stored only if given.

    Empty optional fields are not written at all, rather than written as empty
    strings, so the register never implies we asked for something and got a
    blank rather than never holding it.
    """
    if not email:
        return False, "no verified email"

    name = (name or "").strip()[:MAX_LEN["name"]]
    if not name:
        return False, "name is required"

    fields = {"name": name}
    for key, value in (("phone", phone), ("organisation", organisation),
                       ("purpose", purpose)):
        value = (value or "").strip()[:MAX_LEN[key]]
        if value:
            fields[key] = value

    now = datetime.now(timezone.utc)
    get_db().visitors.update_one(
        {"email": email},
        {"$set": {**fields, "profile_saved_at": now, "last_seen": now},
         "$setOnInsert": {"email": email, "first_seen": now}},
        upsert=True,
    )
    return True, None


def display_name(email):
    doc = get(email)
    if doc and (doc.get("name") or "").strip():
        return doc["name"].strip()
    return (email or "").split("@")[0] if email else None


def listing(limit=500):
    rows = list(get_db().visitors.find({}, {"_id": 0})
                .sort("last_seen", DESCENDING).limit(limit))
    out = []
    for r in rows:
        for key in ("first_seen", "last_seen"):
            v = r.get(key)
            if hasattr(v, "strftime"):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=timezone.utc)
                r[key] = v.strftime("%d %b %Y, %H:%M")
        out.append(r)
    return out


def count():
    try:
        return get_db().visitors.estimated_document_count()
    except Exception:
        return 0


# ---------------------------------------------------------------- requests ---

def record_request(email, note=None):
    """Someone signed in has asked for the raw dataset.

    Returns (created, doc). created is False if they had already asked, so the
    caller can avoid pinging the phone twice for the same person.
    """
    from datetime import datetime, timezone
    if not email:
        return False, None

    now = datetime.now(timezone.utc)
    existing = get_db().data_requests.find_one({"email": email})
    if existing:
        get_db().data_requests.update_one(
            {"email": email},
            {"$set": {"last_asked": now}, "$inc": {"times_asked": 1}})
        return False, existing

    doc = {"email": email, "first_asked": now, "last_asked": now,
           "times_asked": 1, "note": (note or "")[:500], "granted": False}
    get_db().data_requests.insert_one(doc)
    return True, doc


def requests_listing(limit=200):
    from datetime import timezone
    rows = list(get_db().data_requests.find({}, {"_id": 0})
                .sort("last_asked", DESCENDING).limit(limit))
    for r in rows:
        for key in ("first_asked", "last_asked"):
            v = r.get(key)
            if hasattr(v, "strftime"):
                if v.tzinfo is None:
                    v = v.replace(tzinfo=timezone.utc)
                r[key] = v.strftime("%d %b %Y, %H:%M")
    return rows


def has_requested(email):
    if not email:
        return False
    return get_db().data_requests.count_documents({"email": email}) > 0


def set_granted(email, granted=True):
    """Approve or revoke someone's access to the raw dataset."""
    from datetime import datetime, timezone
    if not email:
        return False
    now = datetime.now(timezone.utc)
    res = get_db().data_requests.update_one(
        {"email": email},
        {"$set": {"granted": bool(granted),
                  "granted_at": now if granted else None}})
    return res.matched_count > 0


def is_granted(email):
    """Approved people download live data, not a stale snapshot — the export
    streams straight from the database, so there is nothing to keep in sync."""
    if not email:
        return False
    return get_db().data_requests.count_documents(
        {"email": email, "granted": True}) > 0
