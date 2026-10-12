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

def add_ready_asset(conn: sqlite3.Connection, *, item_id: int, run_id: int, user_id: int,
                    full_url: str, thumb_url: str | None,
                    width: int | None = None, height: int | None = None) -> int:
    """Record one resolved image (status ``ready``) and return its id.

    No bytes are downloaded at acquire time (the no-pre-download model): the gallery
    hotlinks ``thumb_url`` and the full-resolution ``full_url`` is fetched only when
    the user downloads. ``source_image_url`` stores the full-res URL.
    """
    cur = conn.execute(
        "INSERT INTO import_assets "
        "(item_id, run_id, user_id, source_image_url, thumb_url, width, height, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, run_id, user_id, full_url, thumb_url, width, height, STATUS_READY),
    )
    conn.commit()
    return int(cur.lastrowid)


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


def get_item(conn: sqlite3.Connection, item_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one item owned by ``user_id`` (IDOR guard), or None.

    Backs download filenames, which key off the item's source URL + label.
    """
    return conn.execute(
        "SELECT * FROM import_items WHERE id = ? AND user_id = ?", (item_id, user_id)
    ).fetchone()


def asset_index_in_item(conn: sqlite3.Connection, item_id: int, asset_id: int) -> int:
    """1-based position of an asset among its item's assets (by id order).

    Used to number a product's downloaded files (``…-1.jpg``, ``…-2.jpg``).
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM import_assets WHERE item_id = ? AND id <= ?",
        (item_id, asset_id),
    ).fetchone()[0])


# --- activity / history -----------------------------------------------------

# Labels can't contain this separator (it's not a title character), so the caller
# can split a run's GROUP_CONCAT'd product labels back apart without ambiguity.
_LABEL_SEP = "||"


def list_acquire_activity(
    conn: sqlite3.Connection, user_id: int, since: str | None = None, limit: int = 500
) -> list[sqlite3.Row]:
    """Finished acquire runs for ``user_id`` — backs the "Acquired Images" history.

    A run counts as finished once it reached ``ready``: ``recompute_run_status``
    sets that only when the run actually produced at least one ready asset. That
    mirrors the "committed run" filter the other content histories use (image sets
    gate on ``cutout_approved_at``, scoring/copy on a saved result), so an
    abandoned or wholly-failed run never shows. Still-``processing`` and
    ``failed`` runs are excluded; newest run first.

    Each row is a one-query summary (correlated subqueries, no N+1): ``image_count``
    (ready assets), ``product_count`` (submitted items), ``source_types`` (the
    distinct ``import_items.source_type`` values), ``thumb_url`` (the earliest
    ready asset's hotlinked thumbnail, full-res as fallback), and ``labels`` (the
    product titles joined on :data:`_LABEL_SEP`). ``since`` (an ISO date) restricts
    to a month for a dashboard table; omit it for the all-time history. Capped at
    ``limit`` so the query stays bounded (IDOR-scoped by ``user_id``).
    """
    sql = (
        "SELECT r.id, r.created_at AS run_at, "
        "  (SELECT COUNT(*) FROM import_assets a "
        "     WHERE a.run_id = r.id AND a.status = ?) AS image_count, "
        "  (SELECT COUNT(*) FROM import_items i WHERE i.run_id = r.id) AS product_count, "
        "  (SELECT GROUP_CONCAT(DISTINCT i.source_type) FROM import_items i "
        "     WHERE i.run_id = r.id) AS source_types, "
        "  (SELECT COALESCE(a.thumb_url, a.source_image_url) FROM import_assets a "
        "     WHERE a.run_id = r.id AND a.status = ? ORDER BY a.id LIMIT 1) AS thumb_url, "
        "  (SELECT GROUP_CONCAT(i.label, ?) FROM import_items i "
        "     WHERE i.run_id = r.id AND i.label IS NOT NULL AND TRIM(i.label) != '') AS labels "
        "FROM import_runs r "
        "WHERE r.user_id = ? AND r.status = ?"
    )
    params: list = [STATUS_READY, STATUS_READY, _LABEL_SEP, user_id, STATUS_READY]
    if since:
        sql += " AND r.created_at >= ?"
        params.append(since)
    sql += " ORDER BY r.created_at DESC, r.id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return conn.execute(sql, params).fetchall()
