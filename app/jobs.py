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
    """Insert queued rows for ``items`` (each ``{"url", "item"}``); return ids."""
    ids: list[int] = []
    for it in items:
        cur = conn.execute(
            "INSERT INTO scored_items (user_id, item_id, url, status) VALUES (?, ?, ?, 'queued')",
            (user_id, it.get("item"), it["url"]),
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
                title: str | None = None) -> None:
    """Store a completed score (and the fetched product title) and mark it scored.

    ``title`` is captured so the results view can label each item by product name
    rather than URL alone; it's unknown at enqueue time and filled in here.
    """
    conn.execute(
        "UPDATE scored_items SET status = 'scored', overall = ?, result_json = ?, "
        "title = ?, error = NULL, updated_at = datetime('now') WHERE id = ?",
        (overall, json.dumps(result), title, row_id),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, row_id: int, status: str, message: str) -> None:
    """Mark a row ``blocked`` or ``error`` with a short message."""
    conn.execute(
        "UPDATE scored_items SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (status, message[:500], row_id),
    )
    conn.commit()
