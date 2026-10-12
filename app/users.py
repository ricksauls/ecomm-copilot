"""User data access: create and look up accounts.

Passwords are hashed with Werkzeug's ``generate_password_hash`` (scrypt by
default) — never stored or logged in plaintext, and never compared with ``==``.
All queries are parameterized (see security-standards).
"""

import logging
import sqlite3

from flask import current_app
from werkzeug.security import check_password_hash, generate_password_hash

from app.db import get_db

logger = logging.getLogger(__name__)

# Authorization roles. Admins reach the admin screens; everyone else is a plain
# 'user' with full non-admin app access. Kept as constants so the role string
# lives in exactly one place (schema CHECK, set_role validation, is_admin).
ROLE_ADMIN = "admin"
ROLE_USER = "user"
VALID_ROLES = frozenset({ROLE_ADMIN, ROLE_USER})


class EmailAlreadyRegistered(Exception):
    """Raised when creating an account with an email that already exists."""


def normalize_email(email: str) -> str:
    """Lowercase and trim an email so lookups and uniqueness are consistent."""
    return email.strip().lower()


def _role_for_new_email(email: str) -> str:
    """Role to assign a newly created account: admin if its email is allowlisted.

    The ADMIN_EMAILS allowlist grants admin at *account creation* as well as at
    startup, so an allowlisted email becomes admin the moment it signs up (no
    restart needed) — while a later UI demotion still sticks, because this only
    fires on insert, never on every authorization check.
    """
    allowed = current_app.config.get("ADMIN_EMAILS") or set()
    return ROLE_ADMIN if normalize_email(email) in allowed else ROLE_USER


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
    role = _role_for_new_email(email)
    db = get_db()
    try:
        cursor = db.execute(
            "INSERT INTO users (email, password_hash, auth_provider, role) "
            "VALUES (?, ?, 'local', ?)",
            (email, generate_password_hash(password), role),
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
    role = _role_for_new_email(email)
    db = get_db()
    cursor = db.execute(
        "INSERT INTO users (email, password_hash, auth_provider, role) VALUES (?, NULL, ?, ?)",
        (normalize_email(email), provider, role),
    )
    db.commit()
    logger.info("New SSO account created: user_id=%s provider=%s", cursor.lastrowid, provider)
    return int(cursor.lastrowid)


def record_login(user_id: int) -> None:
    """Stamp a successful login: roll ``last_login_at`` into ``prev_login_at``.

    Called on every sign-in path. ``prev_login_at`` (the time of the login before
    this one) becomes the reference for the admin's "new users since your last
    login" notification; ``last_login_at`` is shown in the admin users table.
    """
    db = get_db()
    db.execute(
        "UPDATE users SET prev_login_at = last_login_at, "
        "last_login_at = datetime('now') WHERE id = ?",
        (user_id,),
    )
    db.commit()


def delete_user(user_id: int) -> None:
    """Delete a user and their scored items. The caller must authorize (admin).

    ``scored_items.user_id`` is a NOT NULL foreign key, so the user's items are
    removed first (in the same transaction) to satisfy the constraint — deleting
    a user takes their scoring history with them.
    """
    db = get_db()
    db.execute("DELETE FROM scored_items WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    logger.info("Deleted user_id=%s and their scored items", user_id)


def list_users() -> list[sqlite3.Row]:
    """Return all users, newest first, for the admin users table."""
    db = get_db()
    return db.execute(
        "SELECT id, email, auth_provider, created_at, last_login_at, role "
        "FROM users ORDER BY id DESC"
    ).fetchall()


def set_role(user_id: int, role: str) -> None:
    """Set a user's authorization role. The caller must authorize (admin).

    Validates against :data:`VALID_ROLES` so a bad value can never reach the
    database (defense in depth alongside the column's CHECK constraint). Guarding
    against removing the last admin is the route's responsibility — this is the
    low-level setter.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"Invalid role: {role!r}")
    db = get_db()
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    db.commit()
    logger.info("Set role of user_id=%s to %s", user_id, role)


def count_admins() -> int:
    """Number of users with the admin role — backs the last-admin lockout guard."""
    return int(get_db().execute(
        "SELECT COUNT(*) FROM users WHERE role = ?", (ROLE_ADMIN,)
    ).fetchone()[0])


def apply_admin_allowlist(conn: sqlite3.Connection, emails) -> int:
    """Promote every existing user whose email is in ``emails`` to admin.

    Runs at startup (break-glass bootstrap) with a raw connection, so it takes
    ``conn`` explicitly rather than using the request-scoped :func:`get_db`.
    Idempotent — only rows not already admin are touched — and a no-op when the
    allowlist is empty. ``emails`` are expected pre-normalized (lowercased), as
    the app factory stores them. Returns the number of users promoted.
    """
    emails = {e for e in emails if e}
    if not emails:
        return 0
    # One placeholder per email; values are bound, so this is not string-built SQL.
    placeholders = ",".join("?" for _ in emails)
    cur = conn.execute(
        f"UPDATE users SET role = ? "
        f"WHERE LOWER(TRIM(email)) IN ({placeholders}) AND role != ?",
        (ROLE_ADMIN, *sorted(emails), ROLE_ADMIN),
    )
    conn.commit()
    if cur.rowcount:
        logger.info("Admin allowlist: promoted %d existing user(s) to admin", cur.rowcount)
    return int(cur.rowcount)


def count_users() -> int:
    """Total number of registered users."""
    return int(get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0])


def count_created_since(reference: str | None, exclude_id: int) -> int:
    """Count users created strictly after ``reference`` (a datetime string).

    Excludes ``exclude_id`` (the admin themselves). ``reference`` is the admin's
    previous-login time; when it's None (their first-ever login) nothing is
    counted as new — a fresh admin shouldn't see the whole roster flagged.
    """
    if reference is None:
        return 0
    row = get_db().execute(
        "SELECT COUNT(*) FROM users WHERE created_at > ? AND id != ?",
        (reference, exclude_id),
    ).fetchone()
    return int(row[0])


def verify_password(user: sqlite3.Row, password: str) -> bool:
    """Check a plaintext password against a user's stored hash.

    Returns False (fail closed) for SSO-only accounts that have no local
    password hash, so an empty/absent hash can never authenticate.
    """
    stored = user["password_hash"]
    if not stored:
        return False
    return check_password_hash(stored, password)
