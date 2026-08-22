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
