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
    -- Full PdpRecord captured at scoring time (JSON), so the "Create new copy
    -- content" cross-link can reuse the already-fetched content instead of
    -- re-fetching the PDP. NULL for items scored before this column, or still
    -- queued/blocked — those fall back to a fresh fetch on the copy path.
    record_json  TEXT,
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

-- One row per AI image-fix (upscale / white-background) requested from the
-- scoring results page. The web app inserts rows as 'queued'; the background
-- worker claims them, calls the upscaling provider, and caches the output under
-- MEDIA_DIR/enhanced/ (see app.ci_images.enhanced_image_path). The lifecycle is
-- single-phase (queued -> processing -> done|error); the DB row only tracks
-- status — the cached file keyed by (scored_item_id, slot) is the artifact.
-- UNIQUE(scored_item_id, slot) makes re-enqueue idempotent: one job per output
-- slot, matching the one cache file per slot.
CREATE TABLE IF NOT EXISTS image_jobs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    scored_item_id INTEGER NOT NULL REFERENCES scored_items(id),
    slot           TEXT    NOT NULL,  -- 'img{N}' (gallery image N) | 'whitebg' (main image fix)
    operation      TEXT    NOT NULL,  -- 'upscale' | 'white_bg' (app.image_enhance.OPERATIONS)
    source_url     TEXT    NOT NULL,  -- resolved from our own stored scrape (SSRF-safe)
    ext            TEXT    NOT NULL DEFAULT 'jpg',
    status         TEXT    NOT NULL DEFAULT 'queued',  -- queued|processing|done|error
    priority       INTEGER NOT NULL DEFAULT 0,  -- higher drains first (main-image white-bg ahead of gallery upscales)
    error          TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(scored_item_id, slot)
);
CREATE INDEX IF NOT EXISTS idx_image_jobs_status ON image_jobs(status);
-- NB: the claim-order index idx_image_jobs_claim references the `priority`
-- column, which is added by _migrate on pre-existing DBs. It is therefore
-- created in _migrate (after the column is guaranteed to exist), NOT here —
-- this script runs before _migrate, so an index on `priority` here would fail
-- on an already-created image_jobs table that predates the column.
CREATE INDEX IF NOT EXISTS idx_image_jobs_item ON image_jobs(scored_item_id);

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

-- Brand ad sightings on the search page. One row per ad unit seen on a keyword's
-- page-1 for a run: a headline (Sponsored Brand Ad / "Brand Amplifier") or a
-- sponsored video ad, attributed to a tracked brand by the ad's own brand name.
-- Count over a period = number of these rows (one per keyword-appearance);
-- image_path is the captured creative (latest wins for a given brand+type).
CREATE TABLE IF NOT EXISTS ci_ad_units (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES ci_runs(id) ON DELETE CASCADE,
    group_id    INTEGER NOT NULL REFERENCES ci_groups(id) ON DELETE CASCADE,
    keyword_id  INTEGER NOT NULL REFERENCES ci_keywords(id) ON DELETE CASCADE,
    scraped_at  TEXT    NOT NULL,                     -- date (YYYY-MM-DD)
    ad_type     TEXT    NOT NULL,                     -- headline|video
    brand_id    INTEGER REFERENCES ci_brands(id) ON DELETE SET NULL,
    brand_text  TEXT,                                 -- the ad's own brand label, as seen
    image_path  TEXT                                  -- captured creative (relative to MEDIA_DIR)
);
CREATE INDEX IF NOT EXISTS idx_ci_ad_units_group_brand_date
    ON ci_ad_units(group_id, brand_id, scraped_at);
CREATE INDEX IF NOT EXISTS idx_ci_ad_units_run ON ci_ad_units(run_id);

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

-- ── PDP Image Set Creation ──────────────────────────────────────────────────
-- Port of the marketplace-creative-studio pipeline (Next.js/Postgres -> Flask/
-- SQLite). From one approved product (facts entered by the user + an uploaded
-- photo) the pipeline produces a set of marketplace creative assets: AI-generated
-- scenes composited with the real product cutout and programmatic copy. Every top
-- -level row carries user_id for ownership/IDOR checks; children cascade-delete
-- with their project. Image bytes live on disk under MEDIA_DIR/imageset/, never in
-- the DB — rows store only paths + status (same split as the enhanced-image cache).

-- One row per product the user is building an image set for. `status` tracks the
-- flow: draft -> cutout_pending -> cutout_approved -> planning -> generating ->
-- ready (plus failed). JSON columns hold the structured facts the planner/
-- compositor read (features live in their own table for round-robin assignment).
CREATE TABLE IF NOT EXISTS imageset_projects (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                INTEGER NOT NULL REFERENCES users(id),
    name                   TEXT    NOT NULL,
    brand                  TEXT    NOT NULL DEFAULT '',
    category               TEXT    NOT NULL DEFAULT '',
    description            TEXT    NOT NULL DEFAULT '',
    target_audience        TEXT    NOT NULL DEFAULT '',
    directions             TEXT    NOT NULL DEFAULT '',
    caution                TEXT    NOT NULL DEFAULT '',
    intended_environments  TEXT,          -- JSON array of strings
    brand_colors           TEXT,          -- JSON array of hex strings
    dimensions_json        TEXT,          -- JSON: {width,height,depth,unit,weight}
    status                 TEXT    NOT NULL DEFAULT 'draft',
    original_path          TEXT,          -- uploaded product photo (rel to MEDIA_DIR)
    cutout_path            TEXT,          -- transparent-PNG cutout (rel to MEDIA_DIR)
    logo_path              TEXT,          -- optional brand logo (rel to MEDIA_DIR)
    cutout_approved_at     TEXT,          -- set when the human approves the cutout
    plan_json              TEXT,          -- the validated CreativePlan (JSON)
    batch_id               TEXT,          -- groups a generation run for history
    source_url             TEXT,          -- Walmart PDP URL when prefilled from a product
    selected_types         TEXT,          -- JSON array of asset types the user chose to generate (NULL = all implemented)
    error                  TEXT,
    created_at             TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at             TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_imageset_projects_user ON imageset_projects(user_id, id);
CREATE INDEX IF NOT EXISTS idx_imageset_projects_status ON imageset_projects(status);

-- Approved product features the planner may reference. `feature_key` is the
-- stable id the plan assigns against (e.g. 'f1'); the planner can only reference
-- these, so it can never introduce a feature (hence a factual claim) that isn't
-- real. Cascade-deletes with the project.
CREATE TABLE IF NOT EXISTS imageset_features (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   INTEGER NOT NULL REFERENCES imageset_projects(id) ON DELETE CASCADE,
    feature_key  TEXT    NOT NULL,   -- stable id referenced by the plan ('f1'…)
    title        TEXT    NOT NULL,
    description  TEXT    NOT NULL DEFAULT '',
    feature_type TEXT    NOT NULL DEFAULT 'benefit',
    icon         TEXT    NOT NULL DEFAULT '',
    position     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(project_id, feature_key)
);
CREATE INDEX IF NOT EXISTS idx_imageset_features_project ON imageset_features(project_id);

-- One row per planned/generated asset (8 per set). The DB row tracks status +
-- output paths; the image bytes are files under MEDIA_DIR/imageset/. Status:
-- draft -> queued -> generating -> compositing -> reviewing -> ready
-- (plus needs_attention|failed). Cascade-deletes with the project.
CREATE TABLE IF NOT EXISTS imageset_assets (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER NOT NULL REFERENCES imageset_projects(id) ON DELETE CASCADE,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    asset_type           TEXT    NOT NULL,  -- LIFESTYLE|FEATURE_CALLOUT|PRODUCT_IN_USE|SIZE_COMPARISON|INFOGRAPHIC
    variation_number     INTEGER NOT NULL DEFAULT 1,
    title                TEXT    NOT NULL DEFAULT '',
    layout_style         TEXT    NOT NULL DEFAULT 'product-left',
    scene_description    TEXT    NOT NULL DEFAULT '',
    usage_scenario       TEXT    NOT NULL DEFAULT '',
    assigned_feature_ids TEXT,           -- JSON array of feature_key values
    generation_instructions TEXT NOT NULL DEFAULT '',
    status               TEXT    NOT NULL DEFAULT 'draft',
    final_path           TEXT,           -- composited 2000px PNG (rel to MEDIA_DIR)
    thumb_path           TEXT,           -- thumbnail (rel to MEDIA_DIR)
    scene_path           TEXT,           -- raw AI scene before compositing (rel to MEDIA_DIR)
    review_json          TEXT,           -- automated-review findings (JSON)
    kept                 INTEGER NOT NULL DEFAULT 1,  -- 1=keep in the set, 0=discarded by the user
    error                TEXT,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_imageset_assets_project ON imageset_assets(project_id, id);
CREATE INDEX IF NOT EXISTS idx_imageset_assets_user ON imageset_assets(user_id, id);

-- Worker queue for asset generation, mirroring image_jobs (single process, single
-- phase: queued -> processing -> done|error). One job per asset — UNIQUE(asset_id)
-- makes re-enqueue idempotent. `priority` lets programmatic (free, fast) assets or
-- reruns be ordered ahead of AI ones if desired.
CREATE TABLE IF NOT EXISTS imageset_jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    project_id  INTEGER NOT NULL REFERENCES imageset_projects(id) ON DELETE CASCADE,
    asset_id    INTEGER NOT NULL REFERENCES imageset_assets(id) ON DELETE CASCADE,
    status      TEXT    NOT NULL DEFAULT 'queued',  -- queued|processing|done|error
    priority    INTEGER NOT NULL DEFAULT 0,          -- higher drains first
    error       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(asset_id)
);
CREATE INDEX IF NOT EXISTS idx_imageset_jobs_claim ON imageset_jobs(status, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_imageset_jobs_project ON imageset_jobs(project_id);
"""


# How long a connection waits on a competing writer's lock before raising
# "database is locked". The scoring worker now writes from several threads at
# once (save_result / mark_failed) while the web app reads and writes; those
# writes are sub-millisecond, so 5s is ample headroom and keeps either side from
# erroring under contention.
_BUSY_TIMEOUT_MS = 5000


def tune_connection(conn: sqlite3.Connection) -> None:
    """Apply the pragmas every connection needs: FK enforcement, a busy timeout,
    and WAL journaling.

    WAL lets readers proceed while a writer holds the lock — essential now that
    the scoring worker writes concurrently from a thread pool while the web app
    serves reads. WAL is a persistent, database-level mode (setting it on any one
    connection sticks for the file), but we apply it on every connection so a
    fresh database (tests, a newly provisioned droplet) gets it regardless of who
    connects first. Best-effort on WAL: a platform that can't enable it falls back
    to the default journal rather than failing the connection.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    try:
        conn.execute("PRAGMA journal_mode = WAL")
    except sqlite3.OperationalError as e:
        logger.warning("Could not enable WAL journaling: %s", e)


def get_db() -> sqlite3.Connection:
    """Return the request-scoped SQLite connection, opening it on first use.

    Rows come back as ``sqlite3.Row`` so callers can use column names. Shared
    connection pragmas (FK enforcement, busy timeout, WAL) come from
    :func:`tune_connection`.
    """
    if "db" not in g:
        database = g.get("_database_path") or _database_path()
        conn = sqlite3.connect(database)
        conn.row_factory = sqlite3.Row
        tune_connection(conn)
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
    # record_json stores the full PdpRecord captured at scoring time so the copy
    # cross-link can skip re-fetching. Nullable — rows scored before this column
    # stay NULL and fall back to a fresh fetch on the copy path.
    if "record_json" not in item_cols:
        conn.execute("ALTER TABLE scored_items ADD COLUMN record_json TEXT")
        logger.info("Migrated scored_items: added 'record_json' column")

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

    # image_jobs.priority orders the claim queue so main-image white-background
    # fixes (the hard Walmart gate) drain ahead of gallery upscales — an
    # interrupted batch then delivers the compliance wins first. Existing rows
    # default to 0 (plain FIFO), unchanged.
    image_cols = {row[1] for row in conn.execute("PRAGMA table_info(image_jobs)")}
    if image_cols and "priority" not in image_cols:
        conn.execute("ALTER TABLE image_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        logger.info("Migrated image_jobs: added 'priority' column")
    # Create the claim-order index here (not in _SCHEMA): it references `priority`,
    # which only exists after the ALTER above on pre-existing DBs. IF NOT EXISTS +
    # running after the column is guaranteed makes this idempotent for both fresh
    # and migrated databases. Guarded on the table existing at all.
    if image_cols:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_image_jobs_claim "
            "ON image_jobs(status, priority DESC, id)"
        )

    # imageset_projects.source_url holds the Walmart PDP URL when a project was
    # prefilled from a product. Added after the table itself shipped, so existing
    # (prod) rows need the ALTER; fresh DBs get it from _SCHEMA. Guarded on the
    # table existing at all (older DBs predate the whole feature).
    imgset_cols = {row[1] for row in conn.execute("PRAGMA table_info(imageset_projects)")}
    if imgset_cols and "source_url" not in imgset_cols:
        conn.execute("ALTER TABLE imageset_projects ADD COLUMN source_url TEXT")
        logger.info("Migrated imageset_projects: added 'source_url' column")
    if imgset_cols and "selected_types" not in imgset_cols:
        conn.execute("ALTER TABLE imageset_projects ADD COLUMN selected_types TEXT")
        logger.info("Migrated imageset_projects: added 'selected_types' column")
    imgset_asset_cols = {row[1] for row in conn.execute("PRAGMA table_info(imageset_assets)")}
    if imgset_asset_cols and "kept" not in imgset_asset_cols:
        conn.execute("ALTER TABLE imageset_assets ADD COLUMN kept INTEGER NOT NULL DEFAULT 1")
        logger.info("Migrated imageset_assets: added 'kept' column")


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
        # Tune first so WAL is established on the file at startup, before the
        # worker threads and web requests start contending for it.
        tune_connection(conn)
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
