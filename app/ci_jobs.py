"""Run queue and result store for Competitive Intelligence scrapes.

A *run* is one scrape sweep of a group's active keywords. The web app (one-time
runs) and the scheduler (monitoring runs) insert ``queued`` rows; the background
worker claims one at a time, scrapes every keyword, and writes search results +
per-brand share-of-search rollups. Mirrors the queue idiom in :mod:`app.jobs`
(atomic conditional-UPDATE claim so two workers can't grab the same run) so the
whole app stays consistent.

Functions take an explicit ``sqlite3.Connection`` to work in both the request and
worker processes. All SQL is parameterized (security-standards).
"""

import logging
import sqlite3
from collections import defaultdict

logger = logging.getLogger(__name__)


# ── enqueue / claim / finish ─────────────────────────────────────────────────────

def enqueue_run(conn: sqlite3.Connection, group_id: int, run_type: str = "one_time",
                slot: str | None = None) -> int:
    """Insert a queued run for ``group_id``; return its id.

    ``run_type`` is ``one_time`` (user-triggered) or ``monitoring`` (scheduled);
    ``slot`` names the monitoring window (morning/afternoon/night) or is None.
    """
    cur = conn.execute(
        "INSERT INTO ci_runs (group_id, run_type, slot, status) VALUES (?, ?, ?, 'queued')",
        (group_id, run_type, slot),
    )
    conn.commit()
    run_id = int(cur.lastrowid)
    logger.info("CI run enqueued id=%s group_id=%s type=%s slot=%s",
                run_id, group_id, run_type, slot)
    return run_id


def has_active_run(conn: sqlite3.Connection, group_id: int) -> bool:
    """True if the group already has a queued/running run (avoid piling up dupes)."""
    row = conn.execute(
        "SELECT 1 FROM ci_runs WHERE group_id = ? AND status IN ('queued', 'running') LIMIT 1",
        (group_id,),
    ).fetchone()
    return row is not None


def claim_next_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest queued run, marking it ``running``.

    Conditional UPDATE guards against two workers claiming the same run; on a lost
    race it retries the next candidate. Mirrors :func:`app.jobs.claim_next`.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM ci_runs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE ci_runs SET status = 'running', started_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (candidate["id"],),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM ci_runs WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def finish_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Mark a run ``done``."""
    conn.execute(
        "UPDATE ci_runs SET status = 'done', finished_at = datetime('now') WHERE id = ?",
        (run_id,),
    )
    conn.commit()
    logger.info("CI run finished id=%s", run_id)


def fail_run(conn: sqlite3.Connection, run_id: int, message: str) -> None:
    """Mark a run ``error`` with a short message."""
    conn.execute(
        "UPDATE ci_runs SET status = 'error', error = ?, finished_at = datetime('now') "
        "WHERE id = ?",
        (message[:500], run_id),
    )
    conn.commit()
    logger.warning("CI run failed id=%s: %s", run_id, message[:200])


def get_run(conn: sqlite3.Connection, run_id: int, user_id: int) -> sqlite3.Row | None:
    """Return a run joined to its group, only if ``user_id`` owns the group (IDOR)."""
    return conn.execute(
        "SELECT r.* FROM ci_runs r JOIN ci_groups g ON g.id = r.group_id "
        "WHERE r.id = ? AND g.user_id = ?",
        (run_id, user_id),
    ).fetchone()


def latest_run(conn: sqlite3.Connection, group_id: int) -> sqlite3.Row | None:
    """Return the most recent run for a group (any status), or None."""
    return conn.execute(
        "SELECT * FROM ci_runs WHERE group_id = ? ORDER BY id DESC LIMIT 1",
        (group_id,),
    ).fetchone()


# ── result writers ───────────────────────────────────────────────────────────────

def write_search_results(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """Persist a batch of per-card search-result rows.

    Each dict has: run_id, group_id, keyword_id, scraped_at, position,
    position_type, item_id, brand_id, is_new_sku.
    """
    for row in rows:
        conn.execute(
            "INSERT INTO ci_search_results "
            "(run_id, group_id, keyword_id, scraped_at, position, position_type, "
            " item_id, brand_id, is_new_sku) "
            "VALUES (:run_id, :group_id, :keyword_id, :scraped_at, :position, "
            ":position_type, :item_id, :brand_id, :is_new_sku)",
            row,
        )
    conn.commit()


def write_share_of_search(conn: sqlite3.Connection, run_id: int, group_id: int,
                          keyword_id: int, scrape_date: str, slot: str | None,
                          results: list[dict]) -> None:
    """Compute and persist the per-brand share-of-search rollup for one keyword.

    Groups the page-1 results by brand_id, counting organic vs sponsored slots.
    Cards that matched no tracked product fall under a single NULL brand row
    ("other"). Rollup logic ported from the reference daily.write_share_of_search.
    """
    counts: dict = defaultdict(lambda: {"organic": 0, "sponsored": 0})
    for r in results:
        bucket = counts[r.get("brand_id")]
        if r["position_type"] == "organic":
            bucket["organic"] += 1
        else:
            bucket["sponsored"] += 1

    for brand_id, c in counts.items():
        total = c["organic"] + c["sponsored"]
        conn.execute(
            "INSERT INTO ci_share_of_search "
            "(run_id, group_id, keyword_id, date, slot, brand_id, "
            " organic_count, sponsored_count, total_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, group_id, keyword_id, scrape_date, slot, brand_id,
             c["organic"], c["sponsored"], total),
        )
    conn.commit()


# ── admin helpers (mirror jobs.list_items / count_items) ─────────────────────────

def seen_item_ids_by_brand(conn: sqlite3.Connection, group_id: int) -> dict[int, set]:
    """Return {brand_id: set(item_ids)} ever recorded for a group's brands.

    Used by the scraper to flag brand-new competitor SKUs: any item id not in this
    set on a run is "new". Preloading from prior runs makes the flag mean "not
    seen before", so re-appearances aren't re-flagged.
    """
    rows = conn.execute(
        "SELECT DISTINCT brand_id, item_id FROM ci_search_results "
        "WHERE group_id = ? AND brand_id IS NOT NULL AND item_id IS NOT NULL",
        (group_id,),
    ).fetchall()
    seen: dict[int, set] = {}
    for r in rows:
        seen.setdefault(r["brand_id"], set()).add(r["item_id"])
    return seen


def count_runs(conn: sqlite3.Connection) -> int:
    """Total number of CI runs across all users (an activity count for admin)."""
    return int(conn.execute("SELECT COUNT(*) FROM ci_runs").fetchone()[0])


def list_runs(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Recent runs across all users with group name + submitter email, newest first."""
    return conn.execute(
        "SELECT r.id, r.run_type, r.slot, r.status, r.started_at, r.finished_at, "
        "  r.created_at, g.name AS group_name, u.email AS user_email "
        "FROM ci_runs r JOIN ci_groups g ON g.id = r.group_id "
        "JOIN users u ON u.id = g.user_id "
        "ORDER BY r.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
