"""Individual item tracking for portfolio products.

A portfolio item the user flags as *tracked* is watched over time: cheap commerce
facts (price / stock / rating / reviews) captured **daily** and a full
content-readiness re-score **weekly**. This module owns the tracked flag, the
daily-facts queue (``portfolio_fact_jobs``), and the dated snapshots
(``portfolio_item_facts``). The weekly readiness re-score reuses the existing
scoring queue (:mod:`app.jobs`), so there's no separate store for it here.

Every read/write that a request can reach is scoped by ``user_id`` so one user
can't touch another's items (the IDOR boundary); the worker-side claim/save
helpers run in the background process and key off the job row instead. All SQL is
parameterized.

Functions take an explicit ``sqlite3.Connection`` so they work inside a Flask
request (``app.db.get_db()``) and in the worker/enqueuer processes alike.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)


# ── tracked flag (request-side, user-scoped) ─────────────────────────────────────

def set_tracked(conn: sqlite3.Connection, user_id: int, portfolio_item_id: int,
                tracked: bool) -> bool:
    """Turn tracking on/off for one of the user's portfolio items.

    Returns True if a row was updated (the item exists and belongs to the user),
    False otherwise — the caller uses that to 404 a forged/foreign id (IDOR gate).
    """
    cur = conn.execute(
        "UPDATE portfolio_items SET tracked = ?, updated_at = datetime('now') "
        "WHERE id = ? AND user_id = ?",
        (1 if tracked else 0, portfolio_item_id, user_id),
    )
    conn.commit()
    if cur.rowcount:
        logger.info("Portfolio item id=%s tracked=%s (user_id=%s)",
                    portfolio_item_id, bool(tracked), user_id)
    return cur.rowcount == 1


def list_tracked(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """Return the user's tracked portfolio items (newest first)."""
    return conn.execute(
        "SELECT * FROM portfolio_items WHERE user_id = ? AND tracked = 1 "
        "ORDER BY id DESC",
        (user_id,),
    ).fetchall()


def count_tracked(conn: sqlite3.Connection, user_id: int) -> int:
    """Count the user's tracked items (for the Portfolio header / dashboard)."""
    return int(conn.execute(
        "SELECT COUNT(*) FROM portfolio_items WHERE user_id = ? AND tracked = 1",
        (user_id,),
    ).fetchone()[0])


# ── daily-facts queue (enqueuer + worker) ────────────────────────────────────────

def has_active_fact_job(conn: sqlite3.Connection, portfolio_item_id: int) -> bool:
    """True if a facts job for this item is already queued or in flight.

    Guards the daily enqueuer against piling up duplicate fetches when the worker
    is backed up (same idea as the CI sweep's ``has_active_run``).
    """
    return conn.execute(
        "SELECT 1 FROM portfolio_fact_jobs "
        "WHERE portfolio_item_id = ? AND status IN ('queued', 'fetching') LIMIT 1",
        (portfolio_item_id,),
    ).fetchone() is not None


def enqueue_fact_jobs(conn: sqlite3.Connection) -> int:
    """Queue a daily facts fetch for every tracked item without one in flight.

    Runs in the daily enqueuer process across ALL users (not request-scoped).
    Returns the number of jobs enqueued. Items whose URL is missing are skipped
    (nothing to fetch).
    """
    items = conn.execute(
        "SELECT id, user_id, item_id, walmart_url FROM portfolio_items WHERE tracked = 1"
    ).fetchall()
    enqueued = 0
    for it in items:
        if not it["walmart_url"]:
            continue
        if has_active_fact_job(conn, it["id"]):
            continue
        conn.execute(
            "INSERT INTO portfolio_fact_jobs "
            "(portfolio_item_id, user_id, walmart_item_id, walmart_url, status) "
            "VALUES (?, ?, ?, ?, 'queued')",
            (it["id"], it["user_id"], it["item_id"], it["walmart_url"]),
        )
        enqueued += 1
    conn.commit()
    logger.info("Daily facts: enqueued %d of %d tracked item(s)", enqueued, len(items))
    return enqueued


def has_claimable_fact_job(conn: sqlite3.Connection) -> bool:
    """True if any facts job is waiting — lets the worker gate this drain."""
    return conn.execute(
        "SELECT 1 FROM portfolio_fact_jobs WHERE status = 'queued' LIMIT 1"
    ).fetchone() is not None


def claim_next_fact_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest queued facts job, marking it ``fetching``.

    Conditional UPDATE so a claim can't double-grab a row; returns None when the
    queue is empty. (The facts drain is processed one-at-a-time, but the atomic
    claim keeps it safe regardless.)
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM portfolio_fact_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE portfolio_fact_jobs SET status = 'fetching' "
            "WHERE id = ? AND status = 'queued'",
            (candidate["id"],),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM portfolio_fact_jobs WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def save_facts(conn: sqlite3.Connection, job: sqlite3.Row, facts: dict) -> int:
    """Write a dated snapshot from a completed facts fetch and close the job.

    ``facts`` carries the fields :func:`app.fetch.fetch_item_facts` returns
    (price, price_string, in_stock, rating, review_count, image_count, has_video,
    title); any missing key stores NULL. Booleans persist as 0/1. Returns the new
    snapshot row id.
    """
    cur = conn.execute(
        "INSERT INTO portfolio_item_facts "
        "(portfolio_item_id, user_id, walmart_item_id, price, price_string, "
        " in_stock, rating, review_count, image_count, has_video, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job["portfolio_item_id"], job["user_id"], job["walmart_item_id"],
            facts.get("price"), facts.get("price_string"),
            _as_int_bool(facts.get("in_stock")),
            facts.get("rating"), facts.get("review_count"),
            facts.get("image_count"), _as_int_bool(facts.get("has_video")),
            facts.get("title"),
        ),
    )
    conn.execute(
        "UPDATE portfolio_fact_jobs SET status = 'done', finished_at = datetime('now'), "
        "message = NULL WHERE id = ?",
        (job["id"],),
    )
    conn.commit()
    logger.info("Saved facts snapshot item=%s job=%s", job["portfolio_item_id"], job["id"])
    return int(cur.lastrowid)


def mark_fact_failed(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    """Mark a facts job ``error`` with a short reason (blocked, fetch failure)."""
    conn.execute(
        "UPDATE portfolio_fact_jobs SET status = 'error', finished_at = datetime('now'), "
        "message = ? WHERE id = ?",
        (message[:500], job_id),
    )
    conn.commit()


_ORPHAN_MESSAGE = "Interrupted — the worker restarted mid-fetch (marked failed on startup)."


def reclaim_orphaned_fact_jobs(conn: sqlite3.Connection) -> int:
    """Fail any facts job stuck ``fetching`` — call once on worker startup.

    The facts drain runs in the single worker process, so at startup every
    ``fetching`` row is orphaned (the process that owned it died mid-fetch). A
    missed daily snapshot is harmless — the next day's run captures a fresh one —
    so we don't requeue; we just clear the stuck state. Returns the count.
    """
    cur = conn.execute(
        "UPDATE portfolio_fact_jobs SET status = 'error', finished_at = datetime('now'), "
        "message = ? WHERE status = 'fetching'",
        (_ORPHAN_MESSAGE,),
    )
    conn.commit()
    if cur.rowcount:
        logger.warning("Reclaimed %d orphaned portfolio facts job(s)", cur.rowcount)
    return cur.rowcount


# ── facts history (read-side, user-scoped) — used by the trend UI ────────────────

def list_facts(conn: sqlite3.Connection, user_id: int, portfolio_item_id: int,
               limit: int | None = None) -> list[sqlite3.Row]:
    """Return a tracked item's fact snapshots oldest-first (for a trend chart).

    User-scoped: a snapshot is returned only when the item belongs to the caller.
    ``limit`` caps to the most recent N (still returned oldest-first).
    """
    if limit is not None:
        rows = conn.execute(
            "SELECT * FROM portfolio_item_facts "
            "WHERE portfolio_item_id = ? AND user_id = ? "
            "ORDER BY captured_at DESC, id DESC LIMIT ?",
            (portfolio_item_id, user_id, limit),
        ).fetchall()
        return list(reversed(rows))
    return conn.execute(
        "SELECT * FROM portfolio_item_facts "
        "WHERE portfolio_item_id = ? AND user_id = ? "
        "ORDER BY captured_at, id",
        (portfolio_item_id, user_id),
    ).fetchall()


def latest_facts(conn: sqlite3.Connection, user_id: int,
                 portfolio_item_id: int) -> sqlite3.Row | None:
    """Return the most recent fact snapshot for a user's item, or None."""
    return conn.execute(
        "SELECT * FROM portfolio_item_facts "
        "WHERE portfolio_item_id = ? AND user_id = ? "
        "ORDER BY captured_at DESC, id DESC LIMIT 1",
        (portfolio_item_id, user_id),
    ).fetchone()


def _as_int_bool(value) -> int | None:
    """Normalize a tri-state boolean fact to 1/0/NULL for storage."""
    if value is None:
        return None
    return 1 if value else 0
