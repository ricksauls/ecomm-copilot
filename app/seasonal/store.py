"""Data layer for Seasonal Content Campaigns.

A *campaign* pairs a season/theme with planning dates; a *product* is one URL added
to it; an *image* is one acquired gallery image (and, once selected + generated, its
seasonal treatment). Two worker queues drive the async work:

- **ACQUIRE** — ``seasonal_products.status`` doubles as the queue
  (``queued -> processing -> done|error``), claimed with a conditional UPDATE so the
  single worker can't double-process (mirrors :mod:`app.imgimport.store`).
- **GENERATE** — a dedicated ``seasonal_jobs`` table, one row per selected image
  (see :mod:`app.seasonal.jobs`); this module owns the image rows those jobs write.

Every function takes an explicit ``sqlite3.Connection`` so it works in a web request
and in the worker, and every read is scoped by ``user_id`` (IDOR guard — see
security-standards).
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Campaign statuses (history "committed run" filter gates on READY).
CAMPAIGN_DRAFT = "draft"
CAMPAIGN_READY = "ready"

# Product (ACQUIRE queue) statuses.
PRODUCT_QUEUED = "queued"
PRODUCT_PROCESSING = "processing"
PRODUCT_DONE = "done"
PRODUCT_ERROR = "error"

# Image generation statuses.
GEN_ACQUIRED = "acquired"      # resolved into the gallery, not yet generated
GEN_QUEUED = "queued"
GEN_GENERATING = "generating"
GEN_READY = "ready"
GEN_FAILED = "failed"

_PRODUCT_ORPHAN_MESSAGE = (
    "Interrupted — the worker restarted while acquiring this product (marked failed "
    "on startup). Add it again.")


# --- campaigns --------------------------------------------------------------

def create_campaign(conn: sqlite3.Connection, *, user_id: int, name: str, theme: str,
                    custom_prompt: str = "", launch_date: str | None = None,
                    expiration_date: str | None = None,
                    reminder_date: str | None = None) -> int:
    """Create a draft campaign and return its id."""
    cur = conn.execute(
        "INSERT INTO seasonal_campaigns "
        "(user_id, name, theme, custom_prompt, launch_date, expiration_date, "
        " reminder_date, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, name, theme, custom_prompt or "", launch_date, expiration_date,
         reminder_date, CAMPAIGN_DRAFT),
    )
    conn.commit()
    logger.info("Created seasonal campaign id=%s theme=%s user_id=%s",
                cur.lastrowid, theme, user_id)
    return int(cur.lastrowid)


def get_campaign(conn: sqlite3.Connection, campaign_id: int, user_id: int) -> sqlite3.Row | None:
    """Return a campaign owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM seasonal_campaigns WHERE id = ? AND user_id = ?",
        (campaign_id, user_id),
    ).fetchone()


def list_campaigns(conn: sqlite3.Connection, user_id: int, *, limit: int = 500,
                   ready_only: bool = False) -> list[sqlite3.Row]:
    """Campaigns for ``user_id``, newest first — backs the home list and history.

    Each row is a one-query summary (correlated subqueries, no N+1): ``product_count``
    (submitted products), ``image_count`` (ready, kept seasonal images), and
    ``thumb_path`` (the first ready image's thumbnail, for a history card). With
    ``ready_only`` the list is restricted to campaigns that produced at least one
    ready image (the history "committed run" filter). IDOR-scoped by ``user_id``.
    """
    sql = (
        "SELECT c.*, "
        "  (SELECT COUNT(*) FROM seasonal_products p WHERE p.campaign_id = c.id) "
        "    AS product_count, "
        "  (SELECT COUNT(*) FROM seasonal_images i "
        "     WHERE i.campaign_id = c.id AND i.gen_status = ? AND i.kept = 1) "
        "    AS image_count, "
        "  (SELECT i.thumb_path FROM seasonal_images i "
        "     WHERE i.campaign_id = c.id AND i.gen_status = ? AND i.kept = 1 "
        "     ORDER BY i.id LIMIT 1) AS thumb_path "
        "FROM seasonal_campaigns c WHERE c.user_id = ?"
    )
    params: list = [GEN_READY, GEN_READY, user_id]
    if ready_only:
        sql += " AND c.status = ?"
        params.append(CAMPAIGN_READY)
    sql += " ORDER BY c.created_at DESC, c.id DESC LIMIT ?"
    params.append(max(1, min(limit, 500)))
    return conn.execute(sql, params).fetchall()


def update_campaign(conn: sqlite3.Connection, campaign_id: int, user_id: int, *,
                    name: str | None = None, launch_date: str | None = None,
                    expiration_date: str | None = None, reminder_date: str | None = None,
                    clear_dates: bool = False) -> None:
    """Update a campaign's name / planning dates (IDOR-scoped).

    Only non-None fields are written. ``clear_dates`` lets a date be explicitly
    set back to NULL (passing ``None`` alone leaves it unchanged), so the edit form
    can both set and clear dates.
    """
    sets, params = [], []
    if name is not None:
        sets.append("name = ?")
        params.append(name)
    for col, val in (("launch_date", launch_date), ("expiration_date", expiration_date),
                     ("reminder_date", reminder_date)):
        if val is not None or clear_dates:
            sets.append(f"{col} = ?")
            params.append(val)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.extend([campaign_id, user_id])
    conn.execute(
        f"UPDATE seasonal_campaigns SET {', '.join(sets)} WHERE id = ? AND user_id = ?",
        params,
    )
    conn.commit()


def recompute_campaign_status(conn: sqlite3.Connection, campaign_id: int) -> str:
    """Roll a campaign's status up: READY once it has a ready image, else DRAFT."""
    ready = conn.execute(
        "SELECT 1 FROM seasonal_images WHERE campaign_id = ? AND gen_status = ? LIMIT 1",
        (campaign_id, GEN_READY),
    ).fetchone()
    status = CAMPAIGN_READY if ready is not None else CAMPAIGN_DRAFT
    conn.execute(
        "UPDATE seasonal_campaigns SET status = ?, updated_at = datetime('now') WHERE id = ?",
        (status, campaign_id),
    )
    conn.commit()
    return status


def delete_campaign(conn: sqlite3.Connection, campaign_id: int, user_id: int) -> bool:
    """Delete a campaign (children cascade). Returns True if a row was removed.

    IDOR-scoped: the WHERE clause includes ``user_id``, so a user can only delete
    their own campaign. On-disk artifacts under MEDIA_DIR/seasonal/<id>/ are left
    for a later sweep (the DB rows that referenced them are gone).
    """
    cur = conn.execute(
        "DELETE FROM seasonal_campaigns WHERE id = ? AND user_id = ?",
        (campaign_id, user_id),
    )
    conn.commit()
    if cur.rowcount:
        logger.info("Deleted seasonal campaign id=%s user_id=%s", campaign_id, user_id)
    return cur.rowcount > 0


def has_pending_work(conn: sqlite3.Connection, campaign_id: int) -> bool:
    """True while any product is still acquiring or any image is still generating.

    Backs the detail page's poller: it keeps polling until both phases are quiet.
    """
    product = conn.execute(
        "SELECT 1 FROM seasonal_products WHERE campaign_id = ? AND status IN (?, ?) LIMIT 1",
        (campaign_id, PRODUCT_QUEUED, PRODUCT_PROCESSING),
    ).fetchone()
    if product is not None:
        return True
    image = conn.execute(
        "SELECT 1 FROM seasonal_images WHERE campaign_id = ? AND gen_status IN (?, ?) LIMIT 1",
        (campaign_id, GEN_QUEUED, GEN_GENERATING),
    ).fetchone()
    return image is not None


# --- products (ACQUIRE queue) -----------------------------------------------

def add_product(conn: sqlite3.Connection, *, campaign_id: int, user_id: int,
                source_url: str, source_type: str) -> int:
    """Add one product URL to a campaign (status ``queued``) and return its id."""
    cur = conn.execute(
        "INSERT INTO seasonal_products (campaign_id, user_id, source_url, source_type, status) "
        "VALUES (?, ?, ?, ?, ?)",
        (campaign_id, user_id, source_url, source_type, PRODUCT_QUEUED),
    )
    conn.commit()
    return int(cur.lastrowid)


def products_for_campaign(conn: sqlite3.Connection, campaign_id: int,
                          user_id: int) -> list[sqlite3.Row]:
    """All products for a campaign, in submission order (IDOR-scoped)."""
    return conn.execute(
        "SELECT * FROM seasonal_products WHERE campaign_id = ? AND user_id = ? ORDER BY id",
        (campaign_id, user_id),
    ).fetchall()


def get_product(conn: sqlite3.Connection, product_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one product owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM seasonal_products WHERE id = ? AND user_id = ?",
        (product_id, user_id),
    ).fetchone()


def set_product_status(conn: sqlite3.Connection, product_id: int, status: str, *,
                       error: str | None = None, label: str | None = None) -> None:
    """Update a product's acquire status (+ optional short error / product label)."""
    conn.execute(
        "UPDATE seasonal_products SET status = ?, error = ?, "
        "label = COALESCE(?, label), updated_at = datetime('now') WHERE id = ?",
        (status, error[:500] if error else None, label, product_id),
    )
    conn.commit()


def has_claimable_product(conn: sqlite3.Connection) -> bool:
    """True if any product is waiting for the acquire worker (status ``queued``)."""
    return conn.execute(
        "SELECT 1 FROM seasonal_products WHERE status = ? LIMIT 1", (PRODUCT_QUEUED,)
    ).fetchone() is not None


def claim_next_product(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest ``queued`` product (-> ``processing``), or None.

    Conditional UPDATE so a concurrent claimer can't grab the same row; ties FIFO
    by id (mirrors the import item claim).
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM seasonal_products WHERE status = ? ORDER BY id LIMIT 1",
            (PRODUCT_QUEUED,),
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE seasonal_products SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (PRODUCT_PROCESSING, candidate["id"], PRODUCT_QUEUED),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM seasonal_products WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued row.


def reclaim_orphaned_products(conn: sqlite3.Connection) -> int:
    """Fail any product left ``processing`` by a crashed worker — call on startup.

    Acquisition runs in the single worker process, so a row still ``processing`` at
    startup was orphaned by a mid-flight restart and nothing will finish it. Mark it
    ``error`` so the gallery shows a clear failure, not a stuck spinner.
    """
    cur = conn.execute(
        "UPDATE seasonal_products SET status = ?, error = ?, updated_at = datetime('now') "
        "WHERE status = ?",
        (PRODUCT_ERROR, _PRODUCT_ORPHAN_MESSAGE, PRODUCT_PROCESSING),
    )
    conn.commit()
    if cur.rowcount:
        logger.warning("Reclaimed %d orphaned seasonal product(s) on startup", cur.rowcount)
    return cur.rowcount


# --- images (gallery + GENERATE outputs) ------------------------------------

def add_acquired_image(conn: sqlite3.Connection, *, product_id: int, campaign_id: int,
                       user_id: int, full_url: str, thumb_url: str | None,
                       width: int | None = None, height: int | None = None) -> int:
    """Record one resolved gallery image (``acquired``) and return its id.

    No bytes are downloaded at acquire time: the gallery hotlinks ``thumb_url`` and
    the full-resolution ``full_url`` is fetched only when the image is generated.
    """
    cur = conn.execute(
        "INSERT INTO seasonal_images "
        "(product_id, campaign_id, user_id, full_url, thumb_url, width, height, gen_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (product_id, campaign_id, user_id, full_url, thumb_url, width, height, GEN_ACQUIRED),
    )
    conn.commit()
    return int(cur.lastrowid)


def images_for_campaign(conn: sqlite3.Connection, campaign_id: int,
                        user_id: int) -> list[sqlite3.Row]:
    """All images for a campaign (IDOR-scoped), grouped by product then order."""
    return conn.execute(
        "SELECT * FROM seasonal_images WHERE campaign_id = ? AND user_id = ? "
        "ORDER BY product_id, id",
        (campaign_id, user_id),
    ).fetchall()


def get_image(conn: sqlite3.Connection, image_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one image owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM seasonal_images WHERE id = ? AND user_id = ?",
        (image_id, user_id),
    ).fetchone()


def ready_images_for_campaign(conn: sqlite3.Connection, campaign_id: int,
                              user_id: int) -> list[sqlite3.Row]:
    """Ready, kept seasonal images for a campaign — backs the ZIP (IDOR-scoped)."""
    return conn.execute(
        "SELECT * FROM seasonal_images WHERE campaign_id = ? AND user_id = ? "
        "AND gen_status = ? AND kept = 1 ORDER BY product_id, id",
        (campaign_id, user_id, GEN_READY),
    ).fetchall()


def set_selection(conn: sqlite3.Connection, campaign_id: int, user_id: int,
                  selected_ids: set[int]) -> int:
    """Set exactly which images in a campaign are selected; return the selected count.

    Images whose ids are in ``selected_ids`` get ``selected = 1``; all others in the
    campaign get ``selected = 0``. IDOR-scoped so a user can only touch their own
    campaign's images. The caller enqueues the selected ones for generation.
    """
    rows = conn.execute(
        "SELECT id FROM seasonal_images WHERE campaign_id = ? AND user_id = ?",
        (campaign_id, user_id),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE seasonal_images SET selected = ?, updated_at = datetime('now') WHERE id = ?",
            (1 if row["id"] in selected_ids else 0, row["id"]),
        )
    conn.commit()
    return sum(1 for row in rows if row["id"] in selected_ids)


def selected_images(conn: sqlite3.Connection, campaign_id: int,
                    user_id: int) -> list[sqlite3.Row]:
    """The campaign's currently-selected images (IDOR-scoped)."""
    return conn.execute(
        "SELECT * FROM seasonal_images WHERE campaign_id = ? AND user_id = ? AND selected = 1 "
        "ORDER BY product_id, id",
        (campaign_id, user_id),
    ).fetchall()


def mark_image_queued(conn: sqlite3.Connection, image_id: int) -> None:
    """Move an image into the generation queue (``gen_status = 'queued'``), clearing error."""
    conn.execute(
        "UPDATE seasonal_images SET gen_status = ?, error = NULL, updated_at = datetime('now') "
        "WHERE id = ?",
        (GEN_QUEUED, image_id),
    )
    conn.commit()


def update_image(conn: sqlite3.Connection, image_id: int, **fields) -> None:
    """Update an image's generation columns (status/paths/error). Mirrors imageset.

    Accepts ``gen_status``, ``original_path``, ``cutout_path``, ``scene_path``,
    ``final_path``, ``thumb_path``, ``error`` — only recognized columns are written,
    so a caller can never inject arbitrary SQL via a field name.
    """
    allowed = {"gen_status", "original_path", "cutout_path", "scene_path",
               "final_path", "thumb_path", "error"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"update_image: unknown field {key!r}")
        sets.append(f"{key} = ?")
        params.append(value)
    if not sets:
        return
    sets.append("updated_at = datetime('now')")
    params.append(image_id)
    conn.execute(f"UPDATE seasonal_images SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def set_image_kept(conn: sqlite3.Connection, image_id: int, user_id: int, kept: bool) -> None:
    """Keep or discard a generated image from the set (IDOR-scoped)."""
    conn.execute(
        "UPDATE seasonal_images SET kept = ?, updated_at = datetime('now') "
        "WHERE id = ? AND user_id = ?",
        (1 if kept else 0, image_id, user_id),
    )
    conn.commit()


def dashboard_reminders(conn: sqlite3.Connection, user_id: int, today: str, *,
                        soon_days: int = 14, limit: int = 12) -> list[dict]:
    """Campaigns with an upcoming/overdue planning date — backs the dashboard widget.

    Returns at most ``limit`` dicts ``{id, name, kind, date, days}`` where ``kind``
    is one of ``overdue`` (expiration already passed), ``reminder`` (reminder date
    due today or earlier), ``expiring_soon`` or ``launching_soon`` (within
    ``soon_days``). At most one reminder per campaign, by urgency (overdue → reminder
    → expiring_soon → launching_soon), most urgent first. ``today`` is an ISO date
    string. IDOR-scoped by ``user_id``; dates are optional, so a campaign with none
    in range simply doesn't appear.
    """
    from datetime import date

    try:
        today_d = date.fromisoformat(today)
    except ValueError:
        return []

    rows = conn.execute(
        "SELECT id, name, launch_date, expiration_date, reminder_date "
        "FROM seasonal_campaigns WHERE user_id = ? AND ("
        "  launch_date IS NOT NULL OR expiration_date IS NOT NULL "
        "  OR reminder_date IS NOT NULL)",
        (user_id,),
    ).fetchall()

    def _days(iso: str | None) -> int | None:
        """Whole days from today to an ISO date (negative = in the past), or None."""
        if not iso:
            return None
        try:
            return (date.fromisoformat(iso) - today_d).days
        except ValueError:
            return None

    reminders: list[dict] = []
    for r in rows:
        exp, rem, launch = _days(r["expiration_date"]), _days(r["reminder_date"]), _days(r["launch_date"])
        # One reminder per campaign, chosen by urgency.
        if exp is not None and exp < 0:
            kind, d, when = "overdue", exp, r["expiration_date"]
        elif rem is not None and rem <= 0:
            kind, d, when = "reminder", rem, r["reminder_date"]
        elif exp is not None and 0 <= exp <= soon_days:
            kind, d, when = "expiring_soon", exp, r["expiration_date"]
        elif launch is not None and 0 <= launch <= soon_days:
            kind, d, when = "launching_soon", launch, r["launch_date"]
        else:
            continue
        reminders.append({"id": r["id"], "name": r["name"], "kind": kind,
                          "date": when, "days": d})

    # Most urgent first: overdue (most negative days) before due-today before soonest.
    _order = {"overdue": 0, "reminder": 1, "expiring_soon": 2, "launching_soon": 3}
    reminders.sort(key=lambda x: (_order[x["kind"]], x["days"]))
    return reminders[:limit]


def image_index_in_product(conn: sqlite3.Connection, product_id: int, image_id: int) -> int:
    """1-based position of an image among its product's images (by id order).

    Used to number a product's downloaded files (``…-1.png``, ``…-2.png``).
    """
    return int(conn.execute(
        "SELECT COUNT(*) FROM seasonal_images WHERE product_id = ? AND id <= ?",
        (product_id, image_id),
    ).fetchone()[0])
