"""SQLite access for the app.

A single connection is opened per request and stashed on Flask's ``g``, then
closed on teardown. The schema is created idempotently at startup by
``init_db``. Keep all SQL parameterized (``?`` placeholders) — never build query
strings with f-strings or ``%`` formatting (see security-standards).
"""

import logging
import os
import sqlite3

from flask import Flask, g

logger = logging.getLogger(__name__)

# Schema is intentionally minimal for the core-auth pass: local accounts and
# accounts created via SSO both live in one table. ``password_hash`` is NULL for
# SSO-only accounts (they have no local password). Emails are stored lowercased
# and uniqueness is enforced by the DB, not just the app.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT    NOT NULL UNIQUE,
    password_hash  TEXT,
    auth_provider  TEXT    NOT NULL DEFAULT 'local',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Login tracking: last_login_at powers the admin's "users table", and
    -- prev_login_at (the login before the current one) is the reference for the
    -- "new users since your last login" notification.
    last_login_at  TEXT,
    prev_login_at  TEXT
);

-- One row per submitted PDP URL. The web app inserts rows as 'queued'; the
-- background worker claims them, fetches + scores, and writes the result back.
CREATE TABLE IF NOT EXISTS scored_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    item_id      TEXT,
    url          TEXT    NOT NULL,
    title        TEXT,    -- product name, filled in by the worker once fetched
    status       TEXT    NOT NULL DEFAULT 'queued',  -- queued|scoring|scored|blocked|error
    overall      INTEGER,
    result_json  TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scored_items_status ON scored_items(status);
CREATE INDEX IF NOT EXISTS idx_scored_items_user ON scored_items(user_id, id);
"""


def get_db() -> sqlite3.Connection:
    """Return the request-scoped SQLite connection, opening it on first use.

    Rows come back as ``sqlite3.Row`` so callers can use column names.
    Foreign-key enforcement is turned on per connection (SQLite defaults it off).
    """
    if "db" not in g:
        database = g.get("_database_path") or _database_path()
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(exc: BaseException | None = None) -> None:
    """Close the request connection if one was opened. Registered as teardown."""
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _database_path() -> str:
    """Resolve the SQLite file path from app config / DATABASE_URL.

    Set at app startup into ``app.config['DATABASE']``. This helper is the
    fallback used when a connection is requested outside that config (kept in
    sync by ``init_app``).
    """
    return os.environ.get("DATABASE_URL") or "app.db"


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent, additive schema migrations for pre-existing databases.

    ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, and SQLite has
    no ``ADD COLUMN IF NOT EXISTS``, so we check the live columns and add any that
    are missing. Additive only — safe to run on every startup/deploy.
    """
    # PRAGMA rows are (cid, name, type, notnull, dflt, pk); name is index 1.
    item_cols = {row[1] for row in conn.execute("PRAGMA table_info(scored_items)")}
    if "title" not in item_cols:
        conn.execute("ALTER TABLE scored_items ADD COLUMN title TEXT")
        logger.info("Migrated scored_items: added 'title' column")

    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for col in ("last_login_at", "prev_login_at"):
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            logger.info("Migrated users: added '%s' column", col)


def init_db(app: Flask) -> None:
    """Create the schema if absent and lock down the DB file's permissions.

    Idempotent: safe to call on every startup, including on each deploy. The
    file is chmod 600 so only the service user can read it — the SQLite file
    holds password hashes and must never be world-readable (security-standards).
    """
    database = app.config["DATABASE"]
    conn = sqlite3.connect(database)
    try:
        conn.executescript(_SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()

    # Best-effort permission tightening; log rather than crash if the platform
    # doesn't support it (e.g. an in-memory or unusual path).
    try:
        if database != ":memory:" and os.path.exists(database):
            os.chmod(database, 0o600)
    except OSError as e:
        logger.warning("Could not chmod the SQLite file %s: %s", database, e)

    logger.info("Database ready at %s", database)


def init_app(app: Flask) -> None:
    """Wire DB lifecycle into the app: resolve the path, register teardown.

    Called from the application factory. Reads ``DATABASE_URL`` (a filesystem
    path) into config, defaulting to a local ``app.db`` for development.
    """
    app.config.setdefault("DATABASE", os.environ.get("DATABASE_URL") or "app.db")
    app.teardown_appcontext(close_db)
    init_db(app)
