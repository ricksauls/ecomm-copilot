r"""Queue for the seasonal GENERATE phase.

Mirrors :mod:`app.imageset.jobs`: single worker process, single phase. The web app
inserts one ``seasonal_jobs`` row per selected image (``queued``); the background
worker claims a row (``processing``), runs the seasonal pipeline (download → cutout
→ AI backdrop → composite), writes the outputs, and marks the row ``done`` (or
``error``). Every function takes an explicit ``sqlite3.Connection`` so it works in a
request and in the worker.

Status lifecycle::

    queued -> processing -> done
                        \-> error

``UNIQUE(image_id)`` makes enqueue idempotent — one job per image — so re-running a
campaign or a single image never stacks duplicate provider calls.
"""

import logging
import sqlite3

from app.seasonal import store

logger = logging.getLogger(__name__)

_PROCESSING = "processing"
_ORPHAN_MESSAGE = (
    "Interrupted — the worker restarted mid-generation (marked failed on startup). "
    "Try generating this image again.")


def enqueue_image_job(conn: sqlite3.Connection, *, user_id: int, campaign_id: int,
                      image_id: int, priority: int = 0) -> int | None:
    """Queue generation for one image; return its job id. Idempotent per image.

    A live (``queued``/``processing``) job is a no-op; a finished or failed job is
    reset to ``queued`` so the image can be regenerated/retried. Also moves the
    image row into ``queued`` so the gallery shows it as pending.
    """
    store.mark_image_queued(conn, image_id)
    existing = conn.execute(
        "SELECT id, status FROM seasonal_jobs WHERE image_id = ?", (image_id,)
    ).fetchone()
    if existing is not None:
        if existing["status"] in (store.GEN_QUEUED, _PROCESSING):
            return int(existing["id"])
        conn.execute(
            "UPDATE seasonal_jobs SET status = 'queued', priority = ?, error = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (priority, existing["id"]),
        )
        conn.commit()
        logger.info("Re-queued seasonal job id=%s image=%s", existing["id"], image_id)
        return int(existing["id"])

    cur = conn.execute(
        "INSERT INTO seasonal_jobs (user_id, campaign_id, image_id, priority, status) "
        "VALUES (?, ?, ?, ?, 'queued')",
        (user_id, campaign_id, image_id, priority),
    )
    conn.commit()
    logger.info("Enqueued seasonal job id=%s campaign=%s image=%s user_id=%s",
                cur.lastrowid, campaign_id, image_id, user_id)
    return int(cur.lastrowid)


def has_claimable_jobs(conn: sqlite3.Connection) -> bool:
    """True if any seasonal job is waiting for a worker (status ``queued``)."""
    return conn.execute(
        "SELECT 1 FROM seasonal_jobs WHERE status = 'queued' LIMIT 1"
    ).fetchone() is not None


def claim_next_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest ``queued`` job (-> ``processing``), or None.

    Conditional UPDATE so concurrent threads can't grab the same row; higher
    ``priority`` drains first, ties FIFO by id.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM seasonal_jobs WHERE status = 'queued' "
            "ORDER BY priority DESC, id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE seasonal_jobs SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (_PROCESSING, candidate["id"]),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM seasonal_jobs WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def mark_done(conn: sqlite3.Connection, row_id: int) -> None:
    """Mark a job ``done`` (its image's outputs are now written)."""
    conn.execute(
        "UPDATE seasonal_jobs SET status = 'done', error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (row_id,),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, row_id: int, message: str) -> None:
    """Mark a job ``error`` with a short, secret-free message."""
    conn.execute(
        "UPDATE seasonal_jobs SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (message[:500], row_id),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, row_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one job owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM seasonal_jobs WHERE id = ? AND user_id = ?",
        (row_id, user_id),
    ).fetchone()


def reclaim_orphaned_jobs(conn: sqlite3.Connection) -> int:
    """Fail any job still ``processing`` — call on worker startup.

    Generation runs inside a single worker process, so a row still ``processing`` at
    startup was orphaned by a mid-call restart and no survivor will finish it.
    Marking it ``error`` (and syncing its image to ``failed``) lets the user retry
    rather than see a stuck spinner. Relies on one worker process (like the other
    queues).
    """
    orphan_image_ids = [r[0] for r in conn.execute(
        "SELECT image_id FROM seasonal_jobs WHERE status = ?", (_PROCESSING,))]
    updated = conn.execute(
        "UPDATE seasonal_jobs SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE status = ?",
        (_ORPHAN_MESSAGE, _PROCESSING),
    )
    for image_id in orphan_image_ids:
        conn.execute(
            "UPDATE seasonal_images SET gen_status = ?, error = ?, "
            "updated_at = datetime('now') WHERE id = ? AND gen_status NOT IN (?, ?)",
            (store.GEN_FAILED, _ORPHAN_MESSAGE[:200], image_id, store.GEN_READY,
             store.GEN_FAILED),
        )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned seasonal job(s) on startup", updated.rowcount)
    return updated.rowcount
