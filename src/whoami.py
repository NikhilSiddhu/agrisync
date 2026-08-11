"""One place that answers "who is making this request".

Goes at:  ~/agrisync/src/whoami.py

WHY THIS EXISTS
    The project is moving from Cloudflare Access to Google sign-in. Two auth
    systems briefly coexist, and every page that needs an identity should not
    have to know about both.

    Google session first, Cloudflare Access second. That order matters: once
    Access is removed the second branch simply never fires, and this file can
    be reduced to one line without touching anything that calls it.

WHY NOT JUST SWITCH EVERYTHING AT ONCE
    Removing Access before the app can read Google sessions leaves /photos and
    the admin tools with nothing in front of them. This lets both work, so you
    can prove the new path before dismantling the old one.
"""
from flask import current_app, request as flask_request

from src import access, auth_session


def current(request=None):
    """Returns {"email", "name", "source"} or None.

    source is "google" or "cloudflare", which is worth having in logs while
    both are live - it tells you whether anyone is still coming through the
    old door.
    """
    req = request or flask_request

    session = auth_session.read(current_app, req)
    if session and session.get("email"):
        return {"email": session["email"],
                "name": session.get("name") or "",
                "source": "google"}

    # Legacy path. Delete this block once the Access application is gone.
    try:
        ident = access.identity(req)
    except Exception:
        ident = None
    if ident and ident.get("email"):
        return {"email": ident["email"], "name": "", "source": "cloudflare"}

    return None


def email(request=None):
    """Just the address, or None. Most callers only want this."""
    who = current(request)
    return who["email"] if who else None


def configured():
    """True if at least one way of identifying someone is set up.

    Used to tell "nobody is signed in" apart from "this server cannot tell who
    anyone is", which are different problems and deserve different pages.
    """
    if auth_session.client_id() and current_app.config.get("SECRET_KEY"):
        return True
    try:
        return access.configured()
    except Exception:
        return False
