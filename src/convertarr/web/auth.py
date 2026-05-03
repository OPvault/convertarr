"""Authentication primitives.

Two ways in:
  - Browser session cookie (signed by itsdangerous, set by /login)
  - `X-Api-Key` request header matching the api_key in `setting`

Both go through `require_auth`, which is plugged in as a router-level dependency
on every protected route. Public routes (login, setup, static, image proxy) live
on a separate, unauthenticated router.
"""
from __future__ import annotations

import hmac
import logging
import secrets

import bcrypt
from fastapi import HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import runtime_settings

VALID_AUTH_METHODS = ("none", "forms")

log = logging.getLogger(__name__)

SESSION_COOKIE = "convertarr_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 30  # 30 days
SESSION_SALT = "convertarr.session.v1"


# --- bcrypt wrappers ---

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# --- session cookie sign/verify ---

def _signer() -> URLSafeTimedSerializer:
    """Lazily-initialized signer keyed off the api_key (which itself is randomly
    seeded). If the api_key is regenerated, all existing sessions are invalidated."""
    secret = runtime_settings.get("api_key", None) or "convertarr-fallback-secret"
    return URLSafeTimedSerializer(secret, salt=SESSION_SALT)


def make_session_cookie(username: str) -> str:
    return _signer().dumps({"u": username})


def read_session_cookie(token: str) -> str | None:
    try:
        data = _signer().loads(token, max_age=SESSION_MAX_AGE)
    except BadSignature:
        return None
    if isinstance(data, dict):
        return data.get("u")
    return None


def set_session_cookie(response: Response, username: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(username),
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        # Don't set secure=True so it works on plain-HTTP LAN setups.
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


# --- request guard ---

class AuthRedirect(Exception):
    """Raised by `require_auth` when the request should be bounced to /login or
    /setup. Handled by an exception handler in main.py that emits a 303 with a
    proper Location header — Starlette doesn't redirect on HTTPException(3xx).
    """

    def __init__(self, location: str) -> None:
        self.location = location


def _is_browser_request(request: Request) -> bool:
    """Heuristic: treat anything that isn't an explicit JSON/AJAX/HTMX call as
    a browser navigation, so curl-with-no-Accept and `<a href>` clicks both
    redirect cleanly."""
    if request.headers.get("HX-Request") == "true":
        return False
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        return False
    return True


def require_auth(request: Request) -> str:
    """FastAPI dependency. Behavior depends on `auth_method` setting:

    - `none`   → no auth required, return "anonymous"
    - `forms`  → cookie-based with /login form; X-Api-Key still works for scripts

    Returns the authenticated username (or "api-key" / "anonymous"). Raises
    `AuthRedirect` for browser navigation, or HTTPException 401 for non-browser
    clients in `forms` mode.
    """
    method = runtime_settings.get("auth_method", "none")
    if method not in VALID_AUTH_METHODS:
        method = "none"

    if method == "none":
        return "anonymous"

    # API key always honoured — useful for scripts regardless of UI auth mode.
    header_key = request.headers.get("x-api-key")
    if header_key:
        configured = runtime_settings.get("api_key", "")
        if configured and hmac.compare_digest(header_key, configured):
            return "api-key"

    # No password set yet — bounce browsers to /setup, return 401 to clients.
    if not runtime_settings.get("auth_password_hash", ""):
        if _is_browser_request(request):
            raise AuthRedirect("/setup")
        raise HTTPException(status_code=401, detail="authentication required")

    # method == "forms"
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        username = read_session_cookie(token)
        if username:
            return username

    if _is_browser_request(request):
        next_url = request.url.path
        if request.url.query:
            next_url += f"?{request.url.query}"
        raise AuthRedirect(f"/login?next={next_url}")
    raise HTTPException(status_code=401, detail="authentication required")


def regenerate_api_key() -> str:
    """Returns the new key. Existing session cookies become invalid because
    the signer secret changes."""
    new_key = secrets.token_hex(32)
    runtime_settings.set("api_key", new_key)
    log.warning("API key regenerated; existing sessions invalidated")
    return new_key
