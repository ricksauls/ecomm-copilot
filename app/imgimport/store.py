"""Data layer for Import Product Images: runs, items, assets + the item queue.

A *run* groups one submission; an *item* is one submitted URL; an *asset* is one
acquired image. The item's ``status`` column doubles as the worker queue
(``queued -> processing -> done|error``), claimed with a conditional UPDATE so the
single worker can't double-process. Every function takes an explicit
``sqlite3.Connection`` so it works in a web request and in the worker.

All reads are scoped by ``user_id`` (IDOR guard — see security-standards).
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

STATUS_PROCESSING = "processing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

ITEM_QUEUED = "queued"
ITEM_PROCESSING = "processing"
ITEM_DONE = "done"
ITEM_ERROR = "error"

_ORPHAN_MESSAGE = (
    "Interrupted — the worker restarted while acquiring this item (marked failed on "
    "startup). Submit it again.")


# --- runs -------------------------------------------------------------------

def create_run(conn: sqlite3.Connection, user_id: int) -> int:
    """Create a run (status ``processing``) and return its id."""
    cur = conn.execute(
        "INSERT INTO import_runs (user_id, status) VALUES (?, ?)",
        (user_id, STATUS_PROCESSING),
    )
    conn.commit()
    logger.info("Created import run id=%s user_id=%s", cur.lastrowid, user_id)
    return int(cur.lastrowid)


def get_run(conn: sqlite3.Connection, run_id: int, user_id: int) -> sqlite3.Row | None:
    """Return a run owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM import_runs WHERE id = ? AND user_id = ?", (run_id, user_id)
    ).fetchone()


def set_run_status(conn: sqlite3.Connection, run_id: int, status: str) -> None:
    conn.execute(
        "UPDATE import_runs SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, run_id),
    )
    conn.commit()


def recompute_run_status(conn: sqlite3.Connection, run_id: int) -> str:
    """Roll the run's status up from its items; return the new status.

    ``processing`` while any item is still queued/processing; once every item is
    terminal, ``ready`` if the run produced at least one ready asset, else
    ``failed`` (every item errored or yielded nothing).
    """
    pending = conn.execute(
        "SELECT 1 FROM import_items WHERE run_id = ? AND status IN (?, ?) LIMIT 1",
        (run_id, ITEM_QUEUED, ITEM_PROCESSING),
    ).fetchone()
    if pending is not None:
        status = STATUS_PROCESSING
    else:
        ready = conn.execute(
            "SELECT 1 FROM import_assets WHERE run_id = ? AND status = ? LIMIT 1",
            (run_id, STATUS_READY),
        ).fetchone()
        status = STATUS_READY if ready is not None else STATUS_FAILED
    set_run_status(conn, run_id, status)
    return status


# --- items ------------------------------------------------------------------

def add_item(conn: sqlite3.Connection, *, run_id: int, user_id: int,
             source_url: str, source_type: str) -> int:
    """Add one submitted URL to a run (status ``queued``) and return its id."""
    cur = conn.execute(
        "INSERT INTO import_items (run_id, user_id, source_url, source_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, user_id, source_url, source_type, ITEM_QUEUED),
    )
    conn.commit()
    return int(cur.lastrowid)


def items_for_run(conn: sqlite3.Connection, run_id: int, user_id: int) -> list[sqlite3.Row]:
    """All items for a run, in submission order (IDOR-scoped)."""
    return conn.execute(
        "SELECT * FROM import_items WHERE run_id = ? AND user_id = ? ORDER BY id",
        (run_id, user_id),
    ).fetchall()


def set_item_status(conn: sqlite3.Connection, item_id: int, status: str, *,
                    error: str | None = None, label: str | None = None) -> None:
    """Update an item's status (+ optional short error / product label)."""
    conn.execute(
        "UPDATE import_items SET status = ?, error = ?, "
        "label = COALESCE(?, label), updated_at = datetime('now') WHERE id = ?",
        (status, error[:500] if error else None, label, item_id),
    )
    conn.commit()


def has_claimable_item(conn: sqlite3.Connection) -> bool:
    """True if any import item is waiting for the worker (status ``queued``)."""
    return conn.execute(
        "SELECT 1 FROM import_items WHERE status = ? LIMIT 1", (ITEM_QUEUED,)
    ).fetchone() is not None


def claim_next_item(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest ``queued`` item (-> ``processing``), or None.

    Conditional UPDATE so a concurrent claimer can't grab the same row; ties FIFO
    by id. Mirrors the image-set fetch/plan claim pattern.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM import_items WHERE status = ? ORDER BY id LIMIT 1",
            (ITEM_QUEUED,),
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE import_items SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (ITEM_PROCESSING, candidate["id"], ITEM_QUEUED),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM import_items WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def reclaim_orphaned_items(conn: sqlite3.Connection) -> int:
    """Fail any item left ``processing`` by a crashed worker — call on startup.

    Acquisition runs in the single worker process, so a row still ``processing`` at
    startup was orphaned by a mid-flight restart and nothing will finish it. Mark it
    ``error`` (so the gallery shows a clear failure, not a stuck spinner), fail any
    half-written assets, and roll each affected run's status up.
    """
    orphans = [r[0] for r in conn.execute(
        "SELECT id FROM import_items WHERE status = ?", (ITEM_PROCESSING,))]
    if not orphans:
        return 0
    run_ids = set()
    for item_id in orphans:
        row = conn.execute("SELECT run_id FROM import_items WHERE id = ?", (item_id,)).fetchone()
        if row:
            run_ids.add(row["run_id"])
        conn.execute(
            "UPDATE import_items SET status = ?, error = ?, updated_at = datetime('now') "
            "WHERE id = ?", (ITEM_ERROR, _ORPHAN_MESSAGE, item_id))
        conn.execute(
            "UPDATE import_assets SET status = ?, error = ? "
            "WHERE item_id = ? AND status NOT IN (?, ?)",
            (STATUS_FAILED, _ORPHAN_MESSAGE[:200], item_id, STATUS_READY, STATUS_FAILED))
    conn.commit()
    for run_id in run_ids:
        recompute_run_status(conn, run_id)
    logger.warning("Reclaimed %d orphaned import item(s) on startup", len(orphans))
    return len(orphans)


# --- assets -----------------------------------------------------------------

def create_asset(conn: sqlite3.Connection, *, item_id: int, run_id: int,
                 user_id: int, source_image_url: str) -> int:
    """Create a pending asset row (status ``processing``) and return its id.

    Inserted before the file is written so the row id can name the stored file;
    :func:`finalize_asset` or :func:`fail_asset` then resolves it.
    """
    cur = conn.execute(
        "INSERT INTO import_assets (item_id, run_id, user_id, source_image_url, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (item_id, run_id, user_id, source_image_url, STATUS_PROCESSING),
    )
    conn.commit()
    return int(cur.lastrowid)


def finalize_asset(conn: sqlite3.Connection, asset_id: int, *, stored_path: str,
                   ext: str, width: int, height: int, upscaled: bool) -> None:
    """Mark an asset ``ready`` with its stored file + dimensions."""
    conn.execute(
        "UPDATE import_assets SET stored_path = ?, ext = ?, width = ?, height = ?, "
        "upscaled = ?, status = ?, error = NULL WHERE id = ?",
        (stored_path, ext, width, height, 1 if upscaled else 0, STATUS_READY, asset_id),
    )
    conn.commit()


def fail_asset(conn: sqlite3.Connection, asset_id: int, error: str) -> None:
    """Mark an asset ``failed`` with a short, secret-free reason."""
    conn.execute(
        "UPDATE import_assets SET status = ?, error = ? WHERE id = ?",
        (STATUS_FAILED, error[:500], asset_id),
    )
    conn.commit()


def assets_for_run(conn: sqlite3.Connection, run_id: int, user_id: int) -> list[sqlite3.Row]:
    """All assets for a run (IDOR-scoped), grouped by item then creation order."""
    return conn.execute(
        "SELECT * FROM import_assets WHERE run_id = ? AND user_id = ? ORDER BY item_id, id",
        (run_id, user_id),
    ).fetchall()


def ready_assets_for_run(conn: sqlite3.Connection, run_id: int, user_id: int) -> list[sqlite3.Row]:
    """Ready (downloadable) assets for a run — backs the ZIP (IDOR-scoped)."""
    return conn.execute(
        "SELECT * FROM import_assets WHERE run_id = ? AND user_id = ? AND status = ? "
        "ORDER BY item_id, id",
        (run_id, user_id, STATUS_READY),
    ).fetchall()


def get_asset(conn: sqlite3.Connection, asset_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one asset owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM import_assets WHERE id = ? AND user_id = ?", (asset_id, user_id)
    ).fetchone()
