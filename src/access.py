"""Identity from Cloudflare Access.

Cloudflare puts a signed JWT on every authenticated request, in the
Cf-Access-Jwt-Assertion header. It contains the email of whoever passed the
one-time PIN. That means the app knows who is visiting without ever handling
a password, sending an email, or storing a credential.

WHY THE SIGNATURE IS VERIFIED, NOT JUST DECODED

A header is only trustworthy if nothing else can set it. Today the API binds
to 0.0.0.0, so anything on your LAN could send a forged Cf-Access-Jwt-Assertion
and be recorded as any email it liked. Verifying against Cloudflare's public
keys removes that entirely, and keeps holding once the box is on 4G and the
threat model widens.

If PyJWT is missing or the team domain is unset, identity is simply unavailable
rather than silently trusted. Failing closed is the only safe direction here.
"""
import os
import time

import requests

try:
    import jwt as pyjwt
    from jwt import PyJWKClient
    JWT_AVAILABLE = True
except ImportError:                                   # pragma: no cover
    JWT_AVAILABLE = False

_jwk_client = None
_jwk_fetched_at = 0.0
JWK_TTL_S = 3600


def _team_domain():
    return os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip().rstrip("/")


def _audience():
    return os.getenv("CF_ACCESS_AUD", "").strip()


def configured():
    return bool(JWT_AVAILABLE and _team_domain() and _audience())


def _client():
    """Cloudflare's signing keys, cached for an hour.

    Fetching these on every request would add a round trip to the internet
    to every page load, and would break the page entirely when the link is
    down - which is exactly when you least want that.
    """
    global _jwk_client, _jwk_fetched_at
    now = time.time()
    if _jwk_client is None or (now - _jwk_fetched_at) > JWK_TTL_S:
        url = f"https://{_team_domain()}/cdn-cgi/access/certs"
        _jwk_client = PyJWKClient(url, cache_keys=True)
        _jwk_fetched_at = now
    return _jwk_client


def identity(request):
    """The verified visitor, or None.

    None means: not configured, no token, or a token that failed verification.
    Callers must treat all three the same way - as anonymous.
    """
    if not configured():
        return None

    token = (request.headers.get("Cf-Access-Jwt-Assertion")
             or request.cookies.get("CF_Authorization"))
    if not token:
        return None

    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = pyjwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=_audience(),
            issuer=f"https://{_team_domain()}",
        )
    except Exception:
        # Expired, forged, wrong audience, keys unreachable. All anonymous.
        return None

    email = (claims.get("email") or "").strip().lower()
    if not email:
        # Service tokens authenticate with no email. That is the ESP32, and it
        # is not a visitor to record.
        return {"email": None, "service_token": True,
                "sub": claims.get("sub"), "common_name": claims.get("common_name")}

    return {"email": email, "service_token": False,
            "sub": claims.get("sub"), "issued_at": claims.get("iat")}


def is_admin(ident):
    """Admins are listed in .env. Empty list means nobody, not everybody."""
    if not ident or not ident.get("email"):
        return False
    allowed = [e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()]
    return ident["email"] in allowed
