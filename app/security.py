"""Auth/session security helpers: CSRF, login guard, current user, validation.

Design notes:
- CSRF uses a per-session random token, injected into every form and checked on
  every unsafe request. Constant-time comparison avoids timing leaks.
- ``login_required`` fails closed: no valid session -> redirect to sign-in.
- The post-login ``next`` target is validated to be a local path so the login
  form can't be abused as an open redirect (see security-standards).
"""

import functools
import hmac
import logging
import re
import secrets
from urllib.parse import urlparse

from flask import (
    abort,
    current_app,
    g,
    redirect,
    request,
    session,
    url_for,
)

from app import users

logger = logging.getLogger(__name__)

# Deliberately permissive but bounded email check: real validation is "can we
# send to it", which we don't do here. This rejects obviously malformed input
# and bounds length to avoid abuse. Allowlist-style structure over blocklist.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_EMAIL_LEN = 254
_MIN_PASSWORD_LEN = 8
_MAX_PASSWORD_LEN = 200

# Session keys.
_CSRF_KEY = "_csrf_token"
_USER_KEY = "user_id"

# Methods that mutate state and therefore require a CSRF token.
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


# --- CSRF -----------------------------------------------------------------


def csrf_token() -> str:
    """Return the session's CSRF token, minting one on first use."""
    if _CSRF_KEY not in session:
        session[_CSRF_KEY] = secrets.token_urlsafe(32)
    return session[_CSRF_KEY]


def _csrf_enabled() -> bool:
    """CSRF is on by default; tests can disable it via config."""
    return current_app.config.get("CSRF_ENABLED", True)


def verify_csrf() -> None:
    """Reject unsafe requests without a valid CSRF token. Registered globally.

    Runs as a ``before_request`` hook. Safe methods (GET/HEAD/OPTIONS) pass
    through. The OAuth callback is exempt here because it is a GET protected by
    its own ``state`` parameter, not a form post.
    """
    if not _csrf_enabled():
        return
    if request.method not in _UNSAFE_METHODS:
        return

    sent = request.form.get("csrf_token", "")
    expected = session.get(_CSRF_KEY, "")
    # compare_digest needs equal-length, non-empty inputs to be meaningful.
    if not expected or not hmac.compare_digest(sent, expected):
        logger.warning(
            "CSRF validation failed: path=%s remote=%s", request.path, request.remote_addr
        )
        abort(400, description="Invalid or missing CSRF token.")


# --- Current user / login guard -------------------------------------------


def load_current_user() -> None:
    """Populate ``g.user`` from the session before each request.

    Registered as a ``before_request`` hook. ``g.user`` is a ``sqlite3.Row`` or
    None; a stale session id (user deleted) is cleared so it can't linger.
    """
    user_id = session.get(_USER_KEY)
    g.user = users.get_by_id(user_id) if user_id is not None else None
    if user_id is not None and g.user is None:
        session.pop(_USER_KEY, None)


def login_user(user_id: int) -> None:
    """Establish an authenticated session for a user id.

    Rotates the session identifier semantics by clearing any prior CSRF token so
    a new one is minted post-login, mitigating session-fixation on the token.
    """
    session.clear()
    session[_USER_KEY] = user_id


def logout_user() -> None:
    """Tear down the authenticated session."""
    session.clear()


def login_required(view):
    """Decorator: require an authenticated session, else redirect to sign-in.

    Fails closed. Preserves the originally requested path as ``next`` so the
    user lands where they intended after signing in.
    """

    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.get("user") is None:
            logger.info("Unauthenticated access to %s; redirecting to sign-in", request.path)
            return redirect(url_for("auth.signin", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def safe_next_url(candidate: str | None) -> str | None:
    """Return ``candidate`` only if it is a safe, local, relative path.

    Blocks open-redirect attempts: no scheme, no host, must start with a single
    ``/`` (not ``//``).
    """
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme or parsed.netloc:
        return None
    if not candidate.startswith("/") or candidate.startswith("//"):
        return None
    return candidate


# --- Input validation -----------------------------------------------------


def validate_credentials(email: str, password: str) -> str | None:
    """Validate signup/login input. Return an error message, or None if valid.

    Bounds length on both fields (unbounded input is a DoS vector) and checks a
    minimal email shape and password length.
    """
    email = (email or "").strip()
    if not email or len(email) > _MAX_EMAIL_LEN or not _EMAIL_RE.match(email):
        return "Enter a valid email address."
    if not password or len(password) < _MIN_PASSWORD_LEN:
        return f"Password must be at least {_MIN_PASSWORD_LEN} characters."
    if len(password) > _MAX_PASSWORD_LEN:
        return "Password is too long."
    return None
