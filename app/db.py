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
    brand        TEXT,    -- brand, from the user at intake and/or the PDP on fetch
    batch_id     TEXT,    -- groups items submitted together in one run (row-click reopens the run)
    status       TEXT    NOT NULL DEFAULT 'queued',  -- queued|scoring|scored|blocked|error
    overall      INTEGER,
    result_json  TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scored_items_status ON scored_items(status);
CREATE INDEX IF NOT EXISTS idx_scored_items_user ON scored_items(user_id, id);

-- Discovered keyword sets, keyed by an item's derived seeds so same-category
-- items reuse one discovery instead of re-mining competitors each time. Shared
-- across worker processes; entries expire (staleness handled by the reader).
CREATE TABLE IF NOT EXISTS keyword_cache (
    cache_key   TEXT PRIMARY KEY,
    keywords    TEXT NOT NULL,  -- JSON array of keyword strings
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per submitted PDP URL for the Copy Content Creation feature. Distinct
-- from scored_items because the lifecycle is two-phase: the worker first fetches
-- the CURRENT copy (Title/Description/Key Features), then — on the user's "Create
-- new copy content" action, or immediately when auto_generate is set (the flow
-- that starts from the scoring screen) — generates NEW copy with the AI. Both the
-- current and generated copy (and each one's rule-based score) are stored so the
-- results screen can show them side by side.
CREATE TABLE IF NOT EXISTS copy_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id            INTEGER NOT NULL REFERENCES users(id),
    item_id            TEXT,
    url                TEXT    NOT NULL,
    -- queued -> fetching -> fetched -> gen_queued -> generating -> done
    -- (plus blocked|error). "fetched" is the resting state after the current
    -- copy is retrieved; the user (or auto_generate) advances it to gen_queued.
    status             TEXT    NOT NULL DEFAULT 'queued',
    -- 1 => generate immediately after the fetch, without waiting for a second
    -- click. Set when the batch originates from the scoring screen.
    auto_generate      INTEGER NOT NULL DEFAULT 0,
    title              TEXT,           -- product name, filled by the worker on fetch
    brand              TEXT,           -- brand, from the user at intake and/or the PDP on fetch
    batch_id           TEXT,           -- groups items submitted together in one run (row-click reopens the run)
    current_json       TEXT,           -- JSON: {title, bullets[], description, score}
    new_json           TEXT,           -- JSON: {title, bullets[], description, score}
    current_overall    INTEGER,        -- rule-based score of the current copy
    projected_overall  INTEGER,        -- rule-based score of the generated copy
    keywords_json      TEXT,           -- target keyword set resolved at fetch, reused at generation
    error              TEXT,
    created_at         TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_copy_items_status ON copy_items(status);
CREATE INDEX IF NOT EXISTS idx_copy_items_user ON copy_items(user_id, id);

-- ── Competitive Intelligence ────────────────────────────────────────────────
-- Search Ranking + Share of Digital Shelf tracking. A user sets up one or more
-- "groups" (e.g. a hot-sauce line, a cookie line); within a group they define
-- Brands (their own vs competitors), the Products under each brand, and the
-- Keywords to track. A "run" scrapes page-1 Walmart search results for every
-- active keyword in a group and records each card's position + organic/sponsored
-- type, then rolls those up into per-brand share-of-search. Runs are either
-- one-time (user-triggered) or monitoring (3x/day scheduled). Model ported from
-- the reference wm-dot-com-competitive-intelligence project (SQLAlchemy -> raw
-- SQLite). Every top-level row carries user_id for ownership/IDOR checks, and
-- child rows cascade-delete with their group.

CREATE TABLE IF NOT EXISTS ci_groups (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL REFERENCES users(id),
    name                TEXT    NOT NULL,
    description         TEXT,
    -- 'snapshot' (one-time, current-state) or 'monitoring' (scheduled 3x/day
    -- over time). A group belongs to exactly one mode; the two setup menus each
    -- manage their own.
    mode                TEXT    NOT NULL DEFAULT 'snapshot',
    -- 1 => include this group in the scheduled 3x/day monitoring sweep.
    monitoring_enabled  INTEGER NOT NULL DEFAULT 0,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ci_groups_user ON ci_groups(user_id, id);

CREATE TABLE IF NOT EXISTS ci_brands (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    type        TEXT    NOT NULL DEFAULT 'competitor',  -- mine|competitor
    tracked     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ci_brands_group ON ci_brands(group_id);

CREATE TABLE IF NOT EXISTS ci_products (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id          INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    brand_id          INTEGER NOT NULL REFERENCES ci_brands(id) ON DELETE CASCADE,
    name              TEXT,
    walmart_item_id   TEXT    NOT NULL,
    walmart_url       TEXT    NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ci_products_group ON ci_products(group_id);
CREATE INDEX IF NOT EXISTS idx_ci_products_item ON ci_products(walmart_item_id);

CREATE TABLE IF NOT EXISTS ci_keywords (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    keyword     TEXT    NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ci_keywords_group ON ci_keywords(group_id);

-- One row per scrape sweep of a group. The web app / scheduler inserts 'queued'
-- rows; the worker claims one, marks it 'running', scrapes every active keyword,
-- then marks it 'done' (or 'error'). slot identifies which monitoring window a
-- scheduled run belongs to (NULL for one-time runs).
CREATE TABLE IF NOT EXISTS ci_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id     INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    run_type     TEXT    NOT NULL DEFAULT 'one_time',   -- one_time|monitoring
    slot         TEXT,                                   -- morning|afternoon|night|NULL
    status       TEXT    NOT NULL DEFAULT 'queued',      -- queued|running|done|error
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ci_runs_status ON ci_runs(status);
CREATE INDEX IF NOT EXISTS idx_ci_runs_group ON ci_runs(group_id, id);

-- Raw page-1 search results: one row per card per keyword per run. brand_id is
-- set when the card's item_id/URL matches a tracked product, else NULL ("other").
CREATE TABLE IF NOT EXISTS ci_search_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES ci_runs(id) ON DELETE CASCADE,
    group_id       INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    keyword_id     INTEGER NOT NULL REFERENCES ci_keywords(id) ON DELETE CASCADE,
    scraped_at     TEXT    NOT NULL,                     -- date (YYYY-MM-DD)
    position       INTEGER NOT NULL,                     -- overall slot on page 1
    position_type  TEXT    NOT NULL,                     -- organic|sponsored
    item_id        TEXT,
    brand_id       INTEGER REFERENCES ci_brands(id) ON DELETE SET NULL,
    is_new_sku     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ci_results_group_kw_date
    ON ci_search_results(group_id, keyword_id, scraped_at);
CREATE INDEX IF NOT EXISTS idx_ci_results_run ON ci_search_results(run_id);

-- Per-brand share-of-search rollup, one row per brand per keyword per run.
CREATE TABLE IF NOT EXISTS ci_share_of_search (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           INTEGER NOT NULL REFERENCES ci_runs(id) ON DELETE CASCADE,
    group_id         INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    keyword_id       INTEGER NOT NULL REFERENCES ci_keywords(id) ON DELETE CASCADE,
    date             TEXT    NOT NULL,                   -- date (YYYY-MM-DD)
    slot             TEXT,                               -- monitoring window or NULL
    brand_id         INTEGER REFERENCES ci_brands(id) ON DELETE SET NULL,
    organic_count    INTEGER NOT NULL DEFAULT 0,
    sponsored_count  INTEGER NOT NULL DEFAULT 0,
    total_count      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_ci_sos_group_brand_date
    ON ci_share_of_search(group_id, brand_id, date);
CREATE INDEX IF NOT EXISTS idx_ci_sos_run ON ci_share_of_search(run_id);

-- In-app "Contact Us" messaging. A thread is one topic a user raised; messages
-- are the back-and-forth within it between the user and the admin team. Read
-- state is tracked per side (the two admins share one inbox) so each side's
-- notification badge counts threads it hasn't caught up on.
CREATE TABLE IF NOT EXISTS message_threads (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    subject                TEXT    NOT NULL,
    category               TEXT    NOT NULL DEFAULT 'question',  -- question|issue|customization|other
    status                 TEXT    NOT NULL DEFAULT 'open',      -- open|closed
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    last_message_at        TEXT    NOT NULL DEFAULT (datetime('now')),
    -- Read state is the id of the newest message each side has seen. Message ids
    -- are monotonic, so this has none of the same-second tie problems a timestamp
    -- comparison does: a thread is unread for a side when a message from the other
    -- side has an id greater than this marker.
    user_last_read_msg_id  INTEGER NOT NULL DEFAULT 0,
    admin_last_read_msg_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_message_threads_user ON message_threads(user_id, id);
CREATE INDEX IF NOT EXISTS idx_message_threads_activity ON message_threads(last_message_at);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id    INTEGER NOT NULL REFERENCES message_threads(id) ON DELETE CASCADE,
    -- Sender kept even if the user is later deleted (SET NULL) so history reads;
    -- sender_role is the source of truth for which side sent it.
    sender_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    sender_role  TEXT    NOT NULL,   -- user|admin
    body         TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, id);
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
    # Brand: the product's brand, captured from the user at intake and/or from the
    # Walmart PDP during fetch. Nullable — existing rows stay NULL until re-run, so
    # the dashboard brand count starts low and grows (an accepted trade-off).
    if "brand" not in item_cols:
        conn.execute("ALTER TABLE scored_items ADD COLUMN brand TEXT")
        logger.info("Migrated scored_items: added 'brand' column")
    # batch_id groups the items submitted together in one run, so a dashboard row
    # click can reopen the whole run's results. Nullable — rows scored before this
    # column existed stay NULL and open on their own (a one-item run).
    if "batch_id" not in item_cols:
        conn.execute("ALTER TABLE scored_items ADD COLUMN batch_id TEXT")
        logger.info("Migrated scored_items: added 'batch_id' column")

    copy_cols = {row[1] for row in conn.execute("PRAGMA table_info(copy_items)")}
    if copy_cols and "brand" not in copy_cols:
        conn.execute("ALTER TABLE copy_items ADD COLUMN brand TEXT")
        logger.info("Migrated copy_items: added 'brand' column")
    if copy_cols and "batch_id" not in copy_cols:
        conn.execute("ALTER TABLE copy_items ADD COLUMN batch_id TEXT")
        logger.info("Migrated copy_items: added 'batch_id' column")

    user_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for col in ("last_login_at", "prev_login_at"):
        if col not in user_cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
            logger.info("Migrated users: added '%s' column", col)

    # ci_groups.mode distinguishes one-time snapshot groups from monitoring
    # groups (added when the CI feature split into separate setup menus).
    ci_group_cols = {row[1] for row in conn.execute("PRAGMA table_info(ci_groups)")}
    if ci_group_cols and "mode" not in ci_group_cols:
        conn.execute("ALTER TABLE ci_groups ADD COLUMN mode TEXT NOT NULL DEFAULT 'snapshot'")
        logger.info("Migrated ci_groups: added 'mode' column")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the schema if absent and apply additive migrations. Idempotent.

    Shared by the web app (:func:`init_db`) and the background worker
    (``worker.connect``) so each guarantees its own tables exist rather than
    depending on the other having initialized the DB first. Without this the
    worker can restart ahead of the web app on a deploy that adds a table and
    crash on the missing table until the web app catches up (a real race we hit
    when ``copy_items`` was added).
    """
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()


def init_db(app: Flask) -> None:
    """Create the schema if absent and lock down the DB file's permissions.

    Idempotent: safe to call on every startup, including on each deploy. The
    file is chmod 600 so only the service user can read it — the SQLite file
    holds password hashes and must never be world-readable (security-standards).
    """
    database = app.config["DATABASE"]
    conn = sqlite3.connect(database)
    try:
        ensure_schema(conn)
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
