"""Sign-in with Google, and the session cookie that follows.

Goes at:  ~/agrisync/src/auth_session.py

WHY GOOGLE AND NOT CLOUDFLARE ACCESS
    Access is a gate: you are either through it or you never reach the app. It
    cannot let someone browse anonymously AND recognise them if they happen to
    be signed in - which is exactly what a public dashboard with a "see more"
    prompt needs.

    Google also hands over a NAME and a VERIFIED EMAIL in one step, so there is
    no signup form, nothing to type, and nothing to lie in.

    Cloudflare Access stays on api.agrisync.tech for admin. Different job,
    different gate, and a stronger one is right for the admin surface.

WHY THE COOKIE LOOKS LIKE THIS
    domain   .agrisync.tech   one login covers the main site, the dashboard
                              and the API without anything clever
    HttpOnly                  JavaScript cannot read it, so an XSS bug on the
                              static site cannot steal a session
    Secure                    never sent over plain HTTP
    SameSite=Lax              blocks the obvious CSRF cases. Requests between
                              our own subdomains are same-SITE, so it still
                              travels where we need it to
    signed, not encrypted     the contents are not secret - an email and a
                              name the user already knows. What matters is
                              that they cannot be CHANGED, which the signature
                              guarantees

    Stateless by choice. No session table, no lookup per request. The trade is
    that you cannot revoke one session early; for a dashboard showing cabbage
    temperatures that is the right side of the trade.
"""
import os
from datetime import timedelta

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

try:
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token
    GOOGLE_AVAILABLE = True
except ImportError:                                   # pragma: no cover
    GOOGLE_AVAILABLE = False


COOKIE_NAME = "agri_session"

# Three weeks. Long enough that a returning visitor is not asked again, short
# enough that a shared or forgotten browser does not stay signed in forever.
SESSION_DAYS = 21
SESSION_MAX_AGE = SESSION_DAYS * 24 * 60 * 60

SALT = "agrisync-session-v1"


def client_id():
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()


def cookie_domain():
    """Empty means "this host only", which is what you want on localhost."""
    return os.getenv("SESSION_COOKIE_DOMAIN", ".agrisync.tech").strip()


def configured(app):
    return bool(client_id() and app.config.get("SECRET_KEY"))


def _serializer(app):
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt=SALT)


# --------------------------------------------------------------- google -----

def verify_google_token(credential):
    """Check a Google ID token and return {email, name, picture} or None.

    VERIFIED, not merely decoded. A JWT is readable by anyone; only the
    signature check proves Google issued it and that it was issued for OUR
    client. Skipping either would let anybody sign in as anybody.

    Returns None on any failure rather than raising - a bad token is a normal
    thing to receive on a public endpoint, not an exception.
    """
    if not GOOGLE_AVAILABLE or not credential or not client_id():
        return None

    try:
        info = google_id_token.verify_oauth2_token(
            credential, google_requests.Request(), client_id())
    except Exception:
        return None

    # verify_oauth2_token checks the signature, expiry and audience. The issuer
    # is worth checking too, and costs nothing.
    if info.get("iss") not in ("accounts.google.com",
                               "https://accounts.google.com"):
        return None

    # An unverified address on a Google account is rare but possible. Since the
    # whole point is a verified email, refuse it.
    if not info.get("email") or not info.get("email_verified"):
        return None

    return {
        "email": info["email"].strip().lower(),
        "name": (info.get("name") or "").strip()[:80],
        "picture": info.get("picture") or "",
    }


# -------------------------------------------------------------- session -----

def issue(app, response, email, name):
    """Sign a session and attach it to the response."""
    token = _serializer(app).dumps({"email": email, "name": name})
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=SESSION_MAX_AGE,
        domain=cookie_domain() or None,
        httponly=True,
        secure=True,
        samesite="Lax",
        path="/",
    )
    return response


def read(app, request):
    """Current session as {email, name}, or None.

    Expiry is enforced here by max_age rather than trusting the browser to
    drop the cookie. A cookie's expiry is a request; the signature's age is a
    fact.
    """
    raw = request.cookies.get(COOKIE_NAME)
    if not raw or not app.config.get("SECRET_KEY"):
        return None
    try:
        data = _serializer(app).loads(raw, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or not data.get("email"):
        return None
    return {"email": data["email"], "name": data.get("name") or ""}


def clear(response):
    response.set_cookie(
        COOKIE_NAME, "", expires=0,
        domain=cookie_domain() or None,
        httponly=True, secure=True, samesite="Lax", path="/",
    )
    return response


def initial(name, email=""):
    """One letter for the avatar. Falls back to the email if there is no name,
    and to a dot rather than showing an empty circle."""
    source = (name or email or "").strip()
    return source[0].upper() if source else "·"
