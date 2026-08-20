"""User data access: create and look up accounts.

Passwords are hashed with Werkzeug's ``generate_password_hash`` (scrypt by
default) — never stored or logged in plaintext, and never compared with ``==``.
All queries are parameterized (see security-standards).
"""

import logging
import sqlite3

from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db

logger = logging.getLogger(__name__)


class EmailAlreadyRegistered(Exception):
    """Raised when creating an account with an email that already exists."""


def normalize_email(email: str) -> str:
    """Lowercase and trim an email so lookups and uniqueness are consistent."""
    return email.strip().lower()


def get_by_email(email: str) -> sqlite3.Row | None:
    """Return the user row for an email, or None. Email is normalized first."""
    db = get_db()
    return db.execute(
        "SELECT * FROM users WHERE email = ?",
        (normalize_email(email),),
    ).fetchone()


def get_by_id(user_id: int) -> sqlite3.Row | None:
    """Return the user row for an id, or None."""
    db = get_db()
    return db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


def create_local_user(email: str, password: str) -> int:
    """Create an email+password account and return its new id.

    Raises :class:`EmailAlreadyRegistered` if the email is taken. The password
    is hashed before it touches the database; the plaintext is never persisted
    or logged.
    """
    email = normalize_email(email)
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO users (email, password_hash, auth_provider) VALUES (?, ?, 'local')",
            (email, generate_password_hash(password)),
        )
        db.commit()
    except sqlite3.IntegrityError as e:
        # UNIQUE constraint on email — surface a domain error the caller can
        # turn into a friendly "already registered" message.
        raise EmailAlreadyRegistered(email) from e
    logger.info("New local account registered: user_id=%s", cursor.lastrowid)
    return int(cursor.lastrowid)


def get_or_create_sso_user(email: str, provider: str) -> int:
    """Return the id of the account for an SSO email, creating one if needed.

    SSO accounts have no local password (``password_hash`` stays NULL), so they
    can only be signed into via the same provider.
    """
    existing = get_by_email(email)
    if existing is not None:
        return int(existing["id"])
    db = get_db()
    cursor = db.execute(
        "INSERT INTO users (email, password_hash, auth_provider) VALUES (?, NULL, ?)",
        (normalize_email(email), provider),
    )
    db.commit()
    logger.info("New SSO account created: user_id=%s provider=%s", cursor.lastrowid, provider)
    return int(cursor.lastrowid)


def verify_password(user: sqlite3.Row, password: str) -> bool:
    """Check a plaintext password against a user's stored hash.

    Returns False (fail closed) for SSO-only accounts that have no local
    password hash, so an empty/absent hash can never authenticate.
    """
    stored = user["password_hash"]
    if not stored:
        return False
    return check_password_hash(stored, password)
