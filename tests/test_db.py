"""Tests for schema setup — the worker/web share of db.ensure_schema."""

import sqlite3

from app import db


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_ensure_schema_creates_all_tables_on_a_bare_connection():
    # A fresh in-memory DB (as the worker gets when it starts ahead of the web
    # app) must end up with every table, so claim queries never hit "no such
    # table". This is the regression guard for the copy_items deploy race.
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    tables = _tables(conn)
    for expected in ("users", "scored_items", "keyword_cache", "copy_items"):
        assert expected in tables


def test_ensure_schema_is_idempotent():
    # Runs on every worker start and web startup, so calling it repeatedly on an
    # already-initialized DB must be a no-op, not an error.
    conn = sqlite3.connect(":memory:")
    db.ensure_schema(conn)
    db.ensure_schema(conn)  # must not raise
    assert "copy_items" in _tables(conn)


def test_migrate_adds_priority_to_preexisting_image_jobs():
    """Regression: a droplet DB whose image_jobs predates the priority column must
    migrate cleanly. ensure_schema runs _SCHEMA (which must NOT index a column that
    doesn't exist yet) before _migrate adds the column + its claim-order index.
    This guards the deploy outage where the priority index lived in _SCHEMA."""
    conn = sqlite3.connect(":memory:")
    # Old image_jobs: no priority column (as it existed before this feature).
    conn.executescript(
        """
        CREATE TABLE image_jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, scored_item_id INTEGER,
          slot TEXT, operation TEXT, source_url TEXT, ext TEXT DEFAULT 'jpg',
          status TEXT DEFAULT 'queued', error TEXT,
          created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
          UNIQUE(scored_item_id, slot)
        );
        """
    )
    conn.commit()

    db.ensure_schema(conn)  # must not raise "no such column: priority"

    cols = {r[1] for r in conn.execute("PRAGMA table_info(image_jobs)")}
    assert "priority" in cols
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='image_jobs'"
    )}
    assert "idx_image_jobs_claim" in idx
    db.ensure_schema(conn)  # idempotent on the migrated DB
