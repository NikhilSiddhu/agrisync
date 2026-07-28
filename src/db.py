"""Mongo connection.

Deliberately tolerant: if Mongo is unreachable the app still starts, so
/health answers and /health/db is the endpoint that tells you the truth.
An API that refuses to boot when the database is down is much harder to
debug from a field site.
"""
from pymongo import MongoClient, ASCENDING, DESCENDING

_client = None
_db = None


def init_db(app):
    global _client, _db
    _client = MongoClient(
        app.config["MONGO_URI"],
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
    )
    _db = _client.get_default_database()
    try:
        _db.readings.create_index([("device_id", ASCENDING), ("ts", DESCENDING)])
        _db.readings.create_index([("ts", DESCENDING)])
        app.logger.info("mongo indexes ready")
    except Exception as exc:  # Mongo down at boot - not fatal
        app.logger.warning("could not create indexes yet: %s", exc)


def get_db():
    if _db is None:
        raise RuntimeError("init_db() was never called")
    return _db


def ping():
    """Raises if Mongo is unreachable. Used by /health/db."""
    _client.admin.command("ping")
    return True
