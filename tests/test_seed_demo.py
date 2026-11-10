"""Tests for the demo-account seeder (app.seed_demo) and its sweep-exclusion flag.

Covers three things the demo depends on:
  • the `demo` column migrates onto a pre-existing portfolio_items table;
  • the three item-tracking sweeps skip demo=1 items (so a seeded story is never
    scraped) while still processing real items;
  • seed()/—reset build the expected, idempotent, self-consistent dataset.
"""

import sqlite3
from datetime import date, timedelta

from app import (
    db,
    enqueue_item_tracking,
    item_rank,
    portfolio_perf,
    seed_demo,
    tracking,
)
from app.db import get_db
from app.users import create_local_user


def _insert(db_conn, uid, item_id, *, tracked, demo, brand="Tabasco", keyword="hot sauce"):
    """Insert a tracked/untracked item with one keyword; return its pk."""
    db_conn.execute(
        "INSERT INTO portfolio_items (user_id, brand, walmart_url, item_id, dedupe_key, "
        "title, title_status, tracked, demo) VALUES (?, ?, ?, ?, ?, ?, 'done', ?, ?)",
        (uid, brand, f"https://www.walmart.com/ip/{item_id}", item_id, item_id,
         f"{brand} {item_id}", tracked, demo),
    )
    pk = db_conn.execute(
        "SELECT id FROM portfolio_items WHERE item_id = ?", (item_id,)
    ).fetchone()["id"]
    db_conn.execute(
        "INSERT INTO portfolio_keywords (portfolio_item_id, user_id, slot, kind, keyword) "
        "VALUES (?, ?, 1, 'search', ?)",
        (pk, uid, keyword),
    )
    db_conn.commit()
    return pk


# ── Schema / migration ────────────────────────────────────────────────────────

def test_schema_has_demo_column_defaulting_zero(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("s1@example.com", "password123")
        pk = _insert(conn, uid, "111", tracked=1, demo=0)
        assert conn.execute(
            "SELECT demo FROM portfolio_items WHERE id = ?", (pk,)
        ).fetchone()["demo"] == 0


def test_migrate_adds_demo_to_preexisting_portfolio_items():
    """A droplet DB whose portfolio_items predates the demo column migrates cleanly."""
    conn = sqlite3.connect(":memory:")
    # Pre-demo shape: has tracked/title/title_status but no demo column.
    conn.executescript(
        """
        CREATE TABLE portfolio_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          brand TEXT, walmart_url TEXT NOT NULL, item_id TEXT, amazon_url TEXT,
          dedupe_key TEXT NOT NULL, title TEXT, title_status TEXT NOT NULL DEFAULT 'queued',
          tracked INTEGER NOT NULL DEFAULT 0,
          created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()

    db.ensure_schema(conn)  # must not raise "no such column: demo"

    cols = {r[1] for r in conn.execute("PRAGMA table_info(portfolio_items)")}
    assert "demo" in cols
    db.ensure_schema(conn)  # idempotent on the migrated DB


# ── Sweep exclusion ─────────────────────────────────────────────────────────

def test_sweeps_exclude_demo_items_but_keep_real_ones(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("s2@example.com", "password123")
        _insert(conn, uid, "real-1", tracked=1, demo=0)   # should be swept
        _insert(conn, uid, "demo-1", tracked=1, demo=1)   # must be skipped

        # Daily rank sweep: one (item, keyword) job — the real item only.
        assert item_rank.enqueue_rank_jobs(conn) == 1
        # Weekly facts sweep: one job — the real item only.
        assert tracking.enqueue_fact_jobs(conn) == 1
        # Weekly readiness re-score: one re-score — the real item only.
        assert enqueue_item_tracking.enqueue_weekly_readiness(conn) == 1


# ── Seed build ───────────────────────────────────────────────────────────────

def test_seed_builds_expected_story(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("s3@example.com", "password123")
        counts = seed_demo.seed(conn, uid, days=30)
        conn.commit()

        assert counts["showcase"] == 4
        assert counts["sandbox"] == 4
        assert counts["groups"] == 4
        assert counts["readings"] > 0 and counts["scores"] > 0

        # Showcase: tracked + demo-flagged; sandbox: neither.
        assert conn.execute(
            "SELECT COUNT(*) c FROM portfolio_items WHERE user_id = ? AND demo = 1 AND tracked = 1",
            (uid,)).fetchone()["c"] == 4
        assert conn.execute(
            "SELECT COUNT(*) c FROM portfolio_items WHERE user_id = ? AND demo = 0 AND tracked = 0",
            (uid,)).fetchone()["c"] == 4

        # Readiness scores carry the demo batch marker and are 'scored'.
        assert conn.execute(
            "SELECT COUNT(*) c FROM scored_items WHERE user_id = ? AND batch_id = ? "
            "AND status = 'scored'", (uid, seed_demo.DEMO_BATCH_ID)).fetchone()["c"] > 0

        # Dashboard defaults to the hero (first) brand's group.
        default_gid = conn.execute(
            "SELECT default_ci_group_id FROM users WHERE id = ?", (uid,)).fetchone()[0]
        hero = conn.execute(
            "SELECT id FROM ci_groups WHERE user_id = ? AND name = ?",
            (uid, seed_demo.SHOWCASE_BRANDS[0]["group"])).fetchone()["id"]
        assert default_gid == hero

        # The seeded items must NOT be swept (demo=1), and sandbox is untracked.
        assert item_rank.enqueue_rank_jobs(conn) == 0

        # Insights sees the 4 showcase items (tracked=1) with readings in-window.
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=30)).isoformat()
        rows = portfolio_perf.tracked_items_with_readings(conn, uid, start, end)
        assert len([r for r in rows if r["n"] > 0]) == 4

        # Seeded readings carry the organic/paid split (so the Organic/Paid block shows
        # without any backfill).
        assert conn.execute(
            "SELECT COUNT(*) c FROM portfolio_rank_readings r "
            "JOIN portfolio_items p ON p.id = r.portfolio_item_id "
            "WHERE p.user_id = ? AND r.item_sponsored_slots IS NOT NULL", (uid,)
        ).fetchone()["c"] > 0
        # The portfolio roll-up now reports a real organic/paid split for the brand.
        summ = portfolio_perf.portfolio_rank_summary(conn, uid, start, end)
        assert summ["organic_share"] is not None and summ["paid_share"] is not None
        # At least one showcase item's keywords differ in Share of Search (not just rank).
        showcase = conn.execute(
            "SELECT id FROM portfolio_items WHERE user_id = ? AND demo = 1", (uid,)
        ).fetchall()
        varied = any(
            len({kw["share"] for kw in portfolio_perf.item_keyword_readings(conn, uid, it["id"], start, end)
                 if kw["share"] is not None}) > 1
            for it in showcase
        )
        assert varied, "expected an item's keywords to differ in share of search"


def test_rank_reading_split_is_consistent_and_bounded():
    import random
    rng = random.Random(42)
    for _ in range(300):
        r = seed_demo._rank_reading("losing", 0.5, False, rng, kw_factor=1.0)
        isp, tsp = r["item_sponsored_slots"], r["total_sponsored_slots"]
        assert 0 <= isp <= r["item_slots"]
        assert isp <= tsp <= r["total_slots"]
        # A sponsored rank exists iff the item holds a paid slot (kept consistent).
        assert (r["sponsored_rank"] is not None) == (isp > 0)


def test_rank_reading_kw_factor_scales_share():
    import random

    def summed_share(factor):
        rng = random.Random(7)
        item = total = 0
        for i in range(60):
            r = seed_demo._rank_reading("leader", i / 59, False, rng, kw_factor=factor)
            item += r["item_slots"]
            total += r["total_slots"]
        return item / total

    # Same role + seed → identical rng draws; only the per-keyword factor moves share.
    assert summed_share(1.35) > summed_share(0.65)


def test_seed_reset_is_idempotent_and_spares_real_items(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("s4@example.com", "password123")
        # A real item the presenter might have added — reset must not touch it.
        real_pk = _insert(conn, uid, "999001", tracked=1, demo=0, brand="RealCo")

        first = seed_demo.seed(conn, uid, days=30)
        conn.commit()

        seed_demo._reset(conn, uid)
        second = seed_demo.seed(conn, uid, days=30)
        conn.commit()

        assert first == second  # deterministic re-seed yields identical counts
        # Exactly one set of demo content remains (no duplication).
        assert conn.execute(
            "SELECT COUNT(*) c FROM portfolio_items WHERE user_id = ? AND demo = 1",
            (uid,)).fetchone()["c"] == 4
        # The real item survived the reset.
        assert conn.execute(
            "SELECT COUNT(*) c FROM portfolio_items WHERE id = ?", (real_pk,)
        ).fetchone()["c"] == 1
