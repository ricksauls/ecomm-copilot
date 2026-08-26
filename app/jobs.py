"""Queue and result store for PDP scoring jobs.

The web app enqueues submitted URLs as ``queued`` rows; a separate background
worker claims them one at a time (browser work is serial and slow), scores them,
and writes the result back. Functions take an explicit ``sqlite3.Connection`` so
they work both inside a Flask request (``app.db.get_db()``) and in the worker
process (its own connection).
"""

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)


def enqueue_items(conn: sqlite3.Connection, user_id: int, items: list[dict]) -> list[int]:
    """Insert queued rows for ``items`` (each ``{"url", "item", "brand"?}``); return ids.

    ``brand`` is the optional brand the user typed at intake; it's stored up front
    so the dashboard's brand count reflects the submission immediately (and even
    when a later fetch is blocked). The worker fills it in from the PDP only when
    the user left it blank — see :func:`save_result`.
    """
    ids: list[int] = []
    for it in items:
        cur = conn.execute(
            "INSERT INTO scored_items (user_id, item_id, url, brand, status) "
            "VALUES (?, ?, ?, ?, 'queued')",
            (user_id, it.get("item"), it["url"], it.get("brand")),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    logger.info("Enqueued %d PDP scoring item(s) for user_id=%s", len(ids), user_id)
    return ids


def get_items(conn: sqlite3.Connection, ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """Return the rows for ``ids`` that belong to ``user_id`` (authorization).

    Filtering by user_id here enforces that one user can't read another's jobs
    via guessed ids (IDOR protection).
    """
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM scored_items WHERE id IN ({placeholders}) AND user_id = ? ORDER BY id",
        (*ids, user_id),
    ).fetchall()
    return rows


# Discovered keyword sets go stale slowly (search demand drifts over weeks), so
# a week-long cache is a good balance of freshness and reuse.
KEYWORD_CACHE_MAX_AGE_DAYS = 7


def get_cached_keywords(conn: sqlite3.Connection, key: str,
                        max_age_days: int = KEYWORD_CACHE_MAX_AGE_DAYS) -> list[str] | None:
    """Return a cached keyword set for ``key`` if present and fresh, else None."""
    if not key:
        return None
    row = conn.execute(
        "SELECT keywords FROM keyword_cache WHERE cache_key = ? "
        "AND created_at > datetime('now', ?)",
        (key, f"-{int(max_age_days)} days"),
    ).fetchone()
    return json.loads(row["keywords"]) if row else None


def put_cached_keywords(conn: sqlite3.Connection, key: str, keywords: list[str]) -> None:
    """Store (or refresh) a discovered keyword set. No-op for an empty key."""
    if not key:
        return
    conn.execute(
        "INSERT INTO keyword_cache (cache_key, keywords, created_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(cache_key) DO UPDATE SET "
        "keywords = excluded.keywords, created_at = excluded.created_at",
        (key, json.dumps(keywords)),
    )
    conn.commit()


def count_items(conn: sqlite3.Connection) -> int:
    """Total number of scored_items rows (all statuses) — an activity count."""
    return int(conn.execute("SELECT COUNT(*) FROM scored_items").fetchone()[0])


def count_scored_products(conn: sqlite3.Connection, user_id: int,
                          since: str | None = None) -> int:
    """Distinct products a user has scored (dashboard "PDP's scored" KPI).

    Keyed on the Walmart item id so re-scoring the same product counts once; rows
    without an item id are excluded. ``since`` (an ISO date) restricts to rows
    created on/after that date, for the "this month" figure.
    """
    sql = ("SELECT COUNT(DISTINCT item_id) FROM scored_items "
           "WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''")
    params: list = [user_id]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    return int(conn.execute(sql, params).fetchone()[0])


def count_managed_products(conn: sqlite3.Connection, user_id: int,
                           since: str | None = None) -> int:
    """Distinct products a user has worked on — the dashboard "Products managed" KPI.

    A product counts once if the user has scored it OR created copy content for it
    (creative/image sets will union in here once that feature exists). Keyed on the
    Walmart item id, so the same product across scoring + copy is counted once;
    rows without an item id (a failed parse) are excluded. ``since`` (an ISO date)
    restricts to rows created on/after that date, for the "this month" figure.
    """
    date_clause = " AND created_at >= ?" if since else ""
    params: list = [user_id]
    if since:
        params.append(since)
    # Same clause + params for each side of the UNION.
    row = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT item_id FROM scored_items "
        "    WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''" + date_clause +
        "  UNION "
        "  SELECT item_id FROM copy_items "
        "    WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''" + date_clause +
        ")",
        params + params,
    ).fetchone()
    return int(row[0])


def count_managed_brands(conn: sqlite3.Connection, user_id: int,
                         since: str | None = None) -> int:
    """Distinct brands a user has worked on — the dashboard "brands" subtitle figure.

    A brand counts once across everything the user has scored OR created copy for
    (creative/image sets union in here once that feature exists). Compared
    case-insensitively (``LOWER(TRIM(...))``) so "Tabasco" and "TABASCO" — e.g. a
    user-typed label vs the PDP's spelling — don't double-count; NULL/blank brands
    are excluded. ``since`` (an ISO date) restricts to rows created on/after that
    date, mirroring the product counts.
    """
    date_clause = " AND created_at >= ?" if since else ""
    params: list = [user_id]
    if since:
        params.append(since)
    # Same brand filter + params for each side of the UNION.
    brand_filter = " AND brand IS NOT NULL AND TRIM(brand) != ''"
    row = conn.execute(
        "SELECT COUNT(DISTINCT LOWER(TRIM(brand))) FROM ("
        "  SELECT brand FROM scored_items WHERE user_id = ?" + brand_filter + date_clause +
        "  UNION ALL "
        "  SELECT brand FROM copy_items WHERE user_id = ?" + brand_filter + date_clause +
        ")",
        params + params,
    ).fetchone()
    return int(row[0])


def list_scored_activity(conn: sqlite3.Connection, user_id: int, since: str | None = None,
                         limit: int = 500) -> list[sqlite3.Row]:
    """Completed scores for ``user_id`` — the dashboard "scored" table + its View All.

    Only ``scored`` rows (a finished, scored item — not queued/blocked/errored),
    newest first. ``since`` (an ISO date) restricts to the current month for the
    dashboard; omit it for the all-time View All screen. Carries the columns the
    activity table shows: item id (for the cached thumbnail), title, brand, score,
    and when it ran. Capped at ``limit`` so the query stays bounded.
    """
    sql = ("SELECT id, item_id, url, title, brand, overall, created_at "
           "FROM scored_items WHERE user_id = ? AND status = 'scored'")
    params: list = [user_id]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_items(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Return recent items across all users (with submitter email) for admin.

    Newest first, capped at ``limit`` so the admin view stays bounded.
    """
    return conn.execute(
        "SELECT si.id, si.item_id, si.url, si.title, si.status, si.overall, "
        "si.created_at, si.updated_at, u.email AS user_email "
        "FROM scored_items si JOIN users u ON u.id = si.user_id "
        "ORDER BY si.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest queued row, marking it ``scoring``.

    Uses a conditional UPDATE so two workers can't grab the same row; if the
    claim loses the race it retries the next candidate.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM scored_items WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE scored_items SET status = 'scoring', updated_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (candidate["id"],),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM scored_items WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def save_result(conn: sqlite3.Connection, row_id: int, overall: int, result: dict,
                title: str | None = None, brand: str | None = None) -> None:
    """Store a completed score (and the fetched product title) and mark it scored.

    ``title`` is captured so the results view can label each item by product name
    rather than URL alone; it's unknown at enqueue time and filled in here.

    ``brand`` is the brand read from the PDP. It only fills the column when the
    user didn't already provide one at intake (``COALESCE`` over the existing,
    non-empty value) so a deliberate user label is never overwritten by the
    scraped value.
    """
    conn.execute(
        "UPDATE scored_items SET status = 'scored', overall = ?, result_json = ?, "
        "title = ?, brand = COALESCE(NULLIF(brand, ''), ?), "
        "error = NULL, updated_at = datetime('now') WHERE id = ?",
        (overall, json.dumps(result), title, brand, row_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, row_id: int, status: str, message: str) -> None:
    """Mark a row ``blocked`` or ``error`` with a short message."""
    conn.execute(
        "UPDATE scored_items SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (status, message[:500], row_id),
    )
    conn.commit()


# Message shown to the user for an item stranded by a mid-fetch worker restart.
_ORPHAN_MESSAGE = "Interrupted — the worker restarted mid-fetch (marked failed on startup). Re-run this item."


def reclaim_orphaned_items(conn: sqlite3.Connection) -> int:
    """Fail any item still marked ``scoring`` — call once on worker startup.

    With a single worker (see HANDOFF §6), a ``scoring`` row at startup can only be
    orphaned: the worker that claimed it died mid-fetch (a deploy restart or an OOM
    kill when headed Chrome spiked memory), so no process will ever finish it. Left
    alone it sits ``scoring`` forever — the results view flashes it as in-progress
    indefinitely and the user has no signal to act. Marking it ``error`` surfaces the
    failure so the user can re-run it. Returns the count reclaimed.

    NB: this assumes one worker. A worker pool (the §6 future) would need a heartbeat
    or age threshold so a restart can't fail a peer's in-flight item.
    """
    updated = conn.execute(
        "UPDATE scored_items SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE status = 'scoring'",
        (_ORPHAN_MESSAGE,),
    )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned scoring item(s) on startup", updated.rowcount)
    return updated.rowcount
