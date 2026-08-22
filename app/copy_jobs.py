r"""Queue and result store for PDP Copy Content Creation jobs.

Parallel to :mod:`app.jobs` (the scoring queue) but for a two-phase lifecycle:
the worker first fetches an item's CURRENT copy, then generates NEW copy. Rows
live in ``copy_items``. As with :mod:`app.jobs`, every function takes an explicit
``sqlite3.Connection`` so it works both inside a Flask request and in the worker
process.

Status lifecycle (see the ``copy_items`` schema in :mod:`app.db`):

    queued -> fetching -> fetched -> gen_queued -> generating -> done
                                \-> (blocked | error)

``fetched`` is the resting point after the current copy is retrieved. The user's
"Create new copy content" action (or ``auto_generate`` on rows that came from the
scoring screen) advances a row to ``gen_queued``.
"""

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

# Statuses the worker can claim, and what each transitions to when claimed.
_CLAIMABLE = {"queued": "fetching", "gen_queued": "generating"}


def enqueue_copy_items(
    conn: sqlite3.Connection,
    user_id: int,
    items: list[dict],
    *,
    auto_generate: bool = False,
) -> list[int]:
    """Insert queued copy rows for ``items`` (each ``{"url", "item"}``); return ids.

    ``auto_generate`` marks the batch to generate new copy immediately after the
    fetch (the flow that starts from the scoring screen), rather than resting at
    ``fetched`` until the user clicks "Create new copy content".
    """
    ids: list[int] = []
    for it in items:
        cur = conn.execute(
            "INSERT INTO copy_items (user_id, item_id, url, status, auto_generate) "
            "VALUES (?, ?, ?, 'queued', ?)",
            (user_id, it.get("item"), it["url"], 1 if auto_generate else 0),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    logger.info(
        "Enqueued %d copy item(s) for user_id=%s (auto_generate=%s)",
        len(ids), user_id, auto_generate,
    )
    return ids


def get_copy_items(conn: sqlite3.Connection, ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """Return the copy rows for ``ids`` that belong to ``user_id`` (IDOR guard)."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT * FROM copy_items WHERE id IN ({placeholders}) AND user_id = ? ORDER BY id",
        (*ids, user_id),
    ).fetchall()


def claim_next_copy(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest claimable copy row.

    Claims a ``queued`` row (advancing it to ``fetching``) or a ``gen_queued`` row
    (advancing it to ``generating``), oldest first. Uses a conditional UPDATE so
    two workers can't grab the same row; if the claim loses the race it retries
    the next candidate. Returns the claimed row, or None when there's no work.
    """
    while True:
        candidate = conn.execute(
            "SELECT id, status FROM copy_items WHERE status IN ('queued', 'gen_queued') "
            "ORDER BY id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        old_status = candidate["status"]
        new_status = _CLAIMABLE[old_status]
        updated = conn.execute(
            "UPDATE copy_items SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (new_status, candidate["id"], old_status),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM copy_items WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next claimable row.


def save_current_copy(
    conn: sqlite3.Connection,
    row_id: int,
    *,
    title: str | None,
    current: dict,
    current_overall: int,
    keywords: list[str] | None,
    next_status: str,
) -> None:
    """Store the fetched current copy (and its score) and set the next status.

    ``next_status`` is ``'gen_queued'`` for an auto-generate row (the worker will
    immediately pick it back up to generate) or ``'fetched'`` otherwise (it rests
    until the user requests generation). The resolved keyword set is persisted so
    the generation phase reuses it without re-discovering.
    """
    conn.execute(
        "UPDATE copy_items SET status = ?, title = ?, current_json = ?, "
        "current_overall = ?, keywords_json = ?, error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (
            next_status,
            title,
            json.dumps(current),
            current_overall,
            json.dumps(keywords or []),
            row_id,
        ),
    )
    conn.commit()


def save_generated_copy(
    conn: sqlite3.Connection, row_id: int, *, new: dict, projected_overall: int
) -> None:
    """Store the generated copy (and its projected score) and mark the row done."""
    conn.execute(
        "UPDATE copy_items SET status = 'done', new_json = ?, projected_overall = ?, "
        "error = NULL, updated_at = datetime('now') WHERE id = ?",
        (json.dumps(new), projected_overall, row_id),
    )
    conn.commit()


def request_generation(conn: sqlite3.Connection, ids: list[int], user_id: int) -> int:
    """Advance the user's ``fetched`` rows in ``ids`` to ``gen_queued``.

    Backs the "Create new copy content" button. Scoped to ``user_id`` (IDOR
    guard) and only touches rows that are actually ``fetched``, so a double click
    or a stale form can't disturb rows mid-flight. Returns the number advanced.
    """
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    updated = conn.execute(
        f"UPDATE copy_items SET status = 'gen_queued', updated_at = datetime('now') "
        f"WHERE id IN ({placeholders}) AND user_id = ? AND status = 'fetched'",
        (*ids, user_id),
    )
    conn.commit()
    logger.info("Requested generation for %d copy item(s), user_id=%s", updated.rowcount, user_id)
    return updated.rowcount


def mark_copy_failed(conn: sqlite3.Connection, row_id: int, status: str, message: str) -> None:
    """Mark a copy row ``blocked`` or ``error`` with a short message."""
    conn.execute(
        "UPDATE copy_items SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (status, message[:500], row_id),
    )
    conn.commit()
