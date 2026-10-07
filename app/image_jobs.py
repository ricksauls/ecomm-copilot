r"""Queue for AI image fixes (upscale / white-background) requested from scoring.

Parallel to :mod:`app.copy_jobs`, but single-phase: the worker claims a queued
row, calls the upscaling provider (:mod:`app.image_enhance`), caches the output
under ``MEDIA_DIR/enhanced/`` (:func:`app.ci_images.save_enhanced_image`), and
marks the row ``done``. Rows live in ``image_jobs``. As with the other queues,
every function takes an explicit ``sqlite3.Connection`` so it works both inside a
Flask request and in the worker process.

Status lifecycle (see the ``image_jobs`` schema in :mod:`app.db`)::

    queued -> processing -> done
                        \-> error

Each row is one *output slot* of one scored item — ``'img{N}'`` for gallery image
N, ``'whitebg'`` for the main-image white-background fix — matching the cache key
in :mod:`app.ci_images`. ``UNIQUE(scored_item_id, slot)`` makes enqueue
idempotent: a slot already cached, or with a live job, is never duplicated.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# The in-flight status a worker holds a row in while calling the provider; a row
# left here at startup was stranded by a mid-call worker restart (see reclaim).
_PROCESSING = "processing"
_ORPHAN_MESSAGE = (
    "Interrupted — the worker restarted mid-enhancement (marked failed on "
    "startup). Try the fix again."
)


def enqueue_image_job(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    scored_item_id: int,
    slot: str,
    operation: str,
    source_url: str,
    ext: str = "jpg",
    priority: int = 0,
) -> int | None:
    """Queue an image fix for one (scored_item, slot); return its id, or None.

    Idempotent on the ``(scored_item_id, slot)`` unique key:
    - a ``queued`` or ``processing`` row for that slot already exists → no-op
      (return its id), so a double click doesn't stack duplicate provider calls;
    - a prior ``error`` (or ``done``) row is reset to ``queued`` so the user can
      retry / re-run a slot;
    - otherwise a fresh ``queued`` row is inserted.

    ``priority`` orders the claim queue (higher drains first); batch runs pass a
    higher value for main-image white-background fixes so the hard Walmart gate is
    delivered before gallery upscales if a run is interrupted.

    ``source_url`` must come from our own stored scrape (resolved by the caller
    from the scored item's result), never arbitrary request input — the provider
    fetches it directly (SSRF guard). Callers should skip enqueue entirely when
    the output is already cached (see :func:`app.ci_images.has_enhanced_image`).
    """
    existing = conn.execute(
        "SELECT id, status FROM image_jobs WHERE scored_item_id = ? AND slot = ?",
        (scored_item_id, slot),
    ).fetchone()
    if existing is not None:
        if existing["status"] in ("queued", _PROCESSING):
            # A live job already covers this slot — don't duplicate provider work.
            return int(existing["id"])
        # A finished/failed row: reset it to run again (retry or re-enhance).
        conn.execute(
            "UPDATE image_jobs SET status = 'queued', operation = ?, source_url = ?, "
            "ext = ?, priority = ?, error = NULL, updated_at = datetime('now') WHERE id = ?",
            (operation, source_url, ext, priority, existing["id"]),
        )
        conn.commit()
        logger.info(
            "Re-queued image job id=%s item=%s slot=%s op=%s user_id=%s",
            existing["id"], scored_item_id, slot, operation, user_id,
        )
        return int(existing["id"])

    cur = conn.execute(
        "INSERT INTO image_jobs (user_id, scored_item_id, slot, operation, source_url, ext, priority, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'queued')",
        (user_id, scored_item_id, slot, operation, source_url, ext, priority),
    )
    conn.commit()
    logger.info(
        "Enqueued image job id=%s item=%s slot=%s op=%s user_id=%s",
        cur.lastrowid, scored_item_id, slot, operation, user_id,
    )
    return int(cur.lastrowid)


def has_claimable_image_jobs(conn: sqlite3.Connection) -> bool:
    """True if any image job is waiting for a worker (status ``queued``).

    Lets the worker gate its image-fix thread pool — spin it up only when there's
    work rather than claim-probing on every idle poll.
    """
    return conn.execute(
        "SELECT 1 FROM image_jobs WHERE status = 'queued' LIMIT 1"
    ).fetchone() is not None


def claim_next_image_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest ``queued`` image job (-> ``processing``).

    Uses a conditional UPDATE so concurrent worker threads can't grab the same
    row; a lost race retries the next candidate. Returns the claimed row, or None
    when there's no work. Higher ``priority`` drains first (main-image white-bg
    ahead of gallery upscales); ties fall back to FIFO by id.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM image_jobs WHERE status = 'queued' "
            "ORDER BY priority DESC, id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE image_jobs SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (_PROCESSING, candidate["id"]),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM image_jobs WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def mark_image_done(conn: sqlite3.Connection, row_id: int) -> None:
    """Mark an image job ``done`` (its output is now cached)."""
    conn.execute(
        "UPDATE image_jobs SET status = 'done', error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (row_id,),
    )
    conn.commit()


def mark_image_failed(conn: sqlite3.Connection, row_id: int, message: str) -> None:
    """Mark an image job ``error`` with a short, secret-free message."""
    conn.execute(
        "UPDATE image_jobs SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (message[:500], row_id),
    )
    conn.commit()


def get_image_job(conn: sqlite3.Connection, row_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one image job owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM image_jobs WHERE id = ? AND user_id = ?",
        (row_id, user_id),
    ).fetchone()


def jobs_for_items(
    conn: sqlite3.Connection, scored_item_ids: list[int], user_id: int
) -> dict[tuple[int, str], sqlite3.Row]:
    """Map ``(scored_item_id, slot) -> row`` for the given items, scoped to a user.

    Backs the results page: it annotates each flagged image / white-bg slot with
    its current job status so the detail panel can render button / spinner /
    inline-image / retry. Scoped to ``user_id`` so one user never sees another's
    jobs (IDOR guard). Returns ``{}`` for an empty id list.
    """
    if not scored_item_ids:
        return {}
    placeholders = ",".join("?" for _ in scored_item_ids)
    rows = conn.execute(
        f"SELECT * FROM image_jobs WHERE user_id = ? AND scored_item_id IN ({placeholders})",
        (user_id, *scored_item_ids),
    ).fetchall()
    return {(r["scored_item_id"], r["slot"]): r for r in rows}


def cancel_queued_for_items(
    conn: sqlite3.Connection, scored_item_ids: list[int], user_id: int
) -> int:
    """Delete still-``queued`` image jobs for the given items; return the count.

    Backs the batch "Stop" action. Only ``queued`` rows are removed — a job already
    ``processing`` is mid-provider-call and can't be refunded, so it's left to
    finish; ``done``/``error`` rows are untouched (their result/retry still stands).
    Deleting rather than marking a terminal state keeps the ``(scored_item_id,
    slot)`` slot free, so the user can re-queue it cleanly later. Scoped to
    ``user_id`` (IDOR guard). No-op (returns 0) for an empty id list.
    """
    if not scored_item_ids:
        return 0
    placeholders = ",".join("?" for _ in scored_item_ids)
    cur = conn.execute(
        f"DELETE FROM image_jobs WHERE user_id = ? AND status = 'queued' "
        f"AND scored_item_id IN ({placeholders})",
        (user_id, *scored_item_ids),
    )
    conn.commit()
    if cur.rowcount:
        logger.info(
            "Cancelled %d queued image job(s) for %d item(s) user_id=%s",
            cur.rowcount, len(scored_item_ids), user_id,
        )
    return cur.rowcount


def requeue_failed_for_items(
    conn: sqlite3.Connection, scored_item_ids: list[int], user_id: int
) -> int:
    """Reset ``error`` image jobs for the given items back to ``queued``; return count.

    Backs the batch "Retry failed" action — re-runs the stored operation against
    the stored (SSRF-safe) source URL at the same priority. Only ``error`` rows are
    touched; a ``done`` row stays cached (no re-spend) and an in-flight row is left
    alone. Scoped to ``user_id`` (IDOR guard). No-op for an empty id list.
    """
    if not scored_item_ids:
        return 0
    placeholders = ",".join("?" for _ in scored_item_ids)
    cur = conn.execute(
        f"UPDATE image_jobs SET status = 'queued', error = NULL, "
        f"updated_at = datetime('now') WHERE user_id = ? AND status = 'error' "
        f"AND scored_item_id IN ({placeholders})",
        (user_id, *scored_item_ids),
    )
    conn.commit()
    if cur.rowcount:
        logger.info(
            "Re-queued %d failed image job(s) for %d item(s) user_id=%s",
            cur.rowcount, len(scored_item_ids), user_id,
        )
    return cur.rowcount


def reclaim_orphaned_image_jobs(conn: sqlite3.Connection) -> int:
    """Fail any job still ``processing`` — call on worker startup.

    Image fixes run concurrently but inside a *single* worker process (a thread
    pool — see ``worker.drain_image_jobs``), so a row still ``processing`` at
    startup can only be orphaned: the process that claimed it died mid-call (a
    deploy restart or OOM kill) and no survivor will finish it. Left alone it
    shows "Enhancing…" forever; marking it ``error`` lets the user retry. Returns
    the count reclaimed.

    NB: like the copy/scoring queues, this relies on one worker *process*. Fanning
    out to multiple processes would need a per-claim lease.
    """
    updated = conn.execute(
        "UPDATE image_jobs SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE status = ?",
        (_ORPHAN_MESSAGE, _PROCESSING),
    )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned image job(s) on startup", updated.rowcount)
    return updated.rowcount
