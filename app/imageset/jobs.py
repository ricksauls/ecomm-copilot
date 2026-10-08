r"""Queue for image-set asset generation.

Mirrors :mod:`app.image_jobs`: single worker process, single phase. The web app
inserts one ``imageset_jobs`` row per asset (``queued``); the background worker
claims a row (``processing``), runs the asset's generation (scene → composite, or
a programmatic template), writes the outputs, and marks the row ``done`` (or
``error``). Every function takes an explicit ``sqlite3.Connection`` so it works in
a request and in the worker.

Status lifecycle::

    queued -> processing -> done
                        \-> error

``UNIQUE(asset_id)`` makes enqueue idempotent — one job per asset — so re-running
a set or a single asset never stacks duplicate provider calls.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

_PROCESSING = "processing"
_ORPHAN_MESSAGE = (
    "Interrupted — the worker restarted mid-generation (marked failed on startup). "
    "Try generating this asset again."
)


def enqueue_asset_job(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    project_id: int,
    asset_id: int,
    priority: int = 0,
) -> int | None:
    """Queue generation for one asset; return its job id. Idempotent per asset.

    A live (``queued``/``processing``) job for the asset is a no-op; a finished or
    failed job is reset to ``queued`` so the asset can be regenerated/retried.
    """
    existing = conn.execute(
        "SELECT id, status FROM imageset_jobs WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    if existing is not None:
        if existing["status"] in ("queued", _PROCESSING):
            return int(existing["id"])
        conn.execute(
            "UPDATE imageset_jobs SET status = 'queued', priority = ?, error = NULL, "
            "updated_at = datetime('now') WHERE id = ?",
            (priority, existing["id"]),
        )
        conn.commit()
        logger.info("Re-queued image-set job id=%s asset=%s", existing["id"], asset_id)
        return int(existing["id"])

    cur = conn.execute(
        "INSERT INTO imageset_jobs (user_id, project_id, asset_id, priority, status) "
        "VALUES (?, ?, ?, ?, 'queued')",
        (user_id, project_id, asset_id, priority),
    )
    conn.commit()
    logger.info(
        "Enqueued image-set job id=%s project=%s asset=%s user_id=%s",
        cur.lastrowid, project_id, asset_id, user_id,
    )
    return int(cur.lastrowid)


def has_claimable_jobs(conn: sqlite3.Connection) -> bool:
    """True if any image-set job is waiting for a worker (status ``queued``)."""
    return conn.execute(
        "SELECT 1 FROM imageset_jobs WHERE status = 'queued' LIMIT 1"
    ).fetchone() is not None


def claim_next_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest ``queued`` job (-> ``processing``), or None.

    Conditional UPDATE so concurrent threads can't grab the same row; higher
    ``priority`` drains first, ties FIFO by id.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM imageset_jobs WHERE status = 'queued' "
            "ORDER BY priority DESC, id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE imageset_jobs SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = 'queued'",
            (_PROCESSING, candidate["id"]),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM imageset_jobs WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def mark_done(conn: sqlite3.Connection, row_id: int) -> None:
    """Mark a job ``done`` (its asset's outputs are now written)."""
    conn.execute(
        "UPDATE imageset_jobs SET status = 'done', error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (row_id,),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection, row_id: int, message: str) -> None:
    """Mark a job ``error`` with a short, secret-free message."""
    conn.execute(
        "UPDATE imageset_jobs SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (message[:500], row_id),
    )
    conn.commit()


def get_job(conn: sqlite3.Connection, row_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one job owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM imageset_jobs WHERE id = ? AND user_id = ?",
        (row_id, user_id),
    ).fetchone()


def jobs_for_project(
    conn: sqlite3.Connection, project_id: int, user_id: int
) -> dict[int, sqlite3.Row]:
    """Map ``asset_id -> job row`` for a project, scoped to a user (IDOR guard).

    Backs the gallery: it annotates each asset with its job status so the UI can
    render queued / generating / ready / retry. Returns ``{}`` when none.
    """
    rows = conn.execute(
        "SELECT * FROM imageset_jobs WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchall()
    return {r["asset_id"]: r for r in rows}


def cancel_queued_for_project(conn: sqlite3.Connection, project_id: int, user_id: int) -> int:
    """Delete still-``queued`` jobs for a project; return the count (IDOR-scoped).

    A ``processing`` job is mid-provider-call and left to finish; done/error rows
    are untouched. Deleting frees the asset's slot so it can be re-queued cleanly.
    """
    cur = conn.execute(
        "DELETE FROM imageset_jobs WHERE project_id = ? AND user_id = ? AND status = 'queued'",
        (project_id, user_id),
    )
    conn.commit()
    if cur.rowcount:
        logger.info(
            "Cancelled %d queued image-set job(s) for project=%s user_id=%s",
            cur.rowcount, project_id, user_id,
        )
    return cur.rowcount


def reclaim_orphaned_jobs(conn: sqlite3.Connection) -> int:
    """Fail any job still ``processing`` — call on worker startup.

    Generation runs inside a single worker process, so a row still ``processing``
    at startup was orphaned by a mid-call restart and no survivor will finish it.
    Marking it ``error`` lets the user retry rather than see a stuck spinner.
    Relies on one worker process (like the other queues).
    """
    # Capture the orphaned assets before flipping the jobs so we can fail them too.
    orphan_asset_ids = [r[0] for r in conn.execute(
        "SELECT asset_id FROM imageset_jobs WHERE status = ?", (_PROCESSING,))]
    updated = conn.execute(
        "UPDATE imageset_jobs SET status = 'error', error = ?, "
        "updated_at = datetime('now') WHERE status = ?",
        (_ORPHAN_MESSAGE, _PROCESSING),
    )
    # Sync the asset to 'failed' so the gallery shows a failed card (with a Retry),
    # not a stuck "Generating…" spinner. Without this the asset stays 'generating'.
    for asset_id in orphan_asset_ids:
        conn.execute(
            "UPDATE imageset_assets SET status = 'failed', error = ?, "
            "updated_at = datetime('now') WHERE id = ? AND status NOT IN ('ready', 'failed')",
            (_ORPHAN_MESSAGE[:200], asset_id),
        )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned image-set job(s) on startup", updated.rowcount)
    return updated.rowcount
