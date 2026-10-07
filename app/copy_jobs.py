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
import uuid

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
    """Insert queued copy rows for ``items`` (each ``{"url", "item", "brand"?}``); return ids.

    ``auto_generate`` marks the batch to generate new copy immediately after the
    fetch (the flow that starts from the scoring screen), rather than resting at
    ``fetched`` until the user clicks "Create new copy content".

    ``brand`` is the optional brand the user typed at intake (or carried over from
    a scored item on the cross-link path); stored up front so the dashboard's
    brand count reflects the submission immediately. The worker fills it from the
    PDP only when the user left it blank — see :func:`save_current_copy`.
    """
    batch_id = uuid.uuid4().hex  # groups this submission so a row click reopens the run
    ids: list[int] = []
    for it in items:
        cur = conn.execute(
            "INSERT INTO copy_items (user_id, item_id, url, brand, batch_id, status, auto_generate) "
            "VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (user_id, it.get("item"), it["url"], it.get("brand"), batch_id,
             1 if auto_generate else 0),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    logger.info(
        "Enqueued %d copy item(s) for user_id=%s (auto_generate=%s) batch=%s",
        len(ids), user_id, auto_generate, batch_id,
    )
    return ids


def create_prefetched_copy_items(
    conn: sqlite3.Connection, user_id: int, items: list[dict]
) -> list[int]:
    """Create copy rows that already carry their current copy, from a prior score.

    Each item: ``{url, item_id?, brand?, title?, current (dict incl. 'record'),
    current_overall?, keywords?}``. Rows are inserted straight into ``gen_queued``
    (``auto_generate=1``) with ``current_json`` populated, so the worker's
    generation phase picks them up directly — no headed-Chrome re-fetch of a PDP
    we already fetched to score it. ``current`` must include the full ``record``
    (the stored PdpRecord) the generation phase rebuilds to project the new score.
    """
    batch_id = uuid.uuid4().hex
    ids: list[int] = []
    for it in items:
        cur = conn.execute(
            "INSERT INTO copy_items (user_id, item_id, url, brand, batch_id, status, "
            "auto_generate, title, current_json, current_overall, keywords_json) "
            "VALUES (?, ?, ?, ?, ?, 'gen_queued', 1, ?, ?, ?, ?)",
            (
                user_id, it.get("item_id"), it["url"], it.get("brand"), batch_id,
                it.get("title"), json.dumps(it["current"]), it.get("current_overall"),
                json.dumps(it.get("keywords") or []),
            ),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    logger.info(
        "Created %d pre-fetched copy item(s) for user_id=%s batch=%s (reused scored content)",
        len(ids), user_id, batch_id,
    )
    return ids


def batch_ids_for_copy_item(conn: sqlite3.Connection, row_id: int, user_id: int) -> list[int]:
    """All copy_items ids in the same run as ``row_id`` (owned by ``user_id``).

    Copy counterpart of :func:`app.jobs.batch_ids_for_item`: a dashboard/View All
    row click reopens the whole run the clicked item belongs to. Returns ``[]``
    when the item isn't found or owned (IDOR guard); a row without a ``batch_id``
    (pre-batch-tracking) opens on its own.
    """
    row = conn.execute(
        "SELECT batch_id FROM copy_items WHERE id = ? AND user_id = ?",
        (row_id, user_id),
    ).fetchone()
    if row is None:
        return []
    if not row["batch_id"]:
        return [row_id]
    siblings = conn.execute(
        "SELECT id FROM copy_items WHERE batch_id = ? AND user_id = ? ORDER BY id",
        (row["batch_id"], user_id),
    ).fetchall()
    return [r["id"] for r in siblings]


def get_copy_items(conn: sqlite3.Connection, ids: list[int], user_id: int) -> list[sqlite3.Row]:
    """Return the copy rows for ``ids`` that belong to ``user_id`` (IDOR guard)."""
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return conn.execute(
        f"SELECT * FROM copy_items WHERE id IN ({placeholders}) AND user_id = ? ORDER BY id",
        (*ids, user_id),
    ).fetchall()


# Copy-status buckets used when matching a scored item to its existing copy: a
# "done" or in-flight copy means a batch rewrite should skip the item (don't
# re-spend); a copy whose only attempts failed is eligible to re-run.
_DONE_STATUSES = ("done",)
_INFLIGHT_STATUSES = ("queued", "fetching", "fetched", "gen_queued", "generating")
_FAILED_STATUSES = ("blocked", "error")


def copy_states_for_items(
    conn: sqlite3.Connection, user_id: int, keys
) -> dict[str, dict]:
    """Summarize each scored item's existing copy, keyed by its item id (or URL).

    ``keys`` is an iterable of ``(item_id, url)`` pairs for the scored items on the
    results page. Copy lives in its own ``copy_items`` rows (no FK to scored_items),
    so a scored item is matched to its copy by **item id when present, else URL** —
    the same identity ``count_copy_products`` keys on. Returns ``{key -> summary}``
    where ``key`` is the item id (or URL) and ``summary`` carries both the dedup
    signals (``has_done`` / ``has_inflight`` / ``has_failed`` — so a batch rewrite
    skips items already covered or in flight) and the latest attempt's display
    fields (``latest_status`` / ``latest_projected`` / ``latest_current`` /
    ``latest_copy_item_id`` — for the per-row copy badge). Scoped to ``user_id``
    (IDOR guard); ``{}`` for empty input.

    A copy row is indexed under BOTH its item id and its URL, so a scored item
    finds it by either identity even if one side lacks the item number.
    """
    item_ids = sorted({k[0] for k in keys if k[0]})
    urls = sorted({k[1] for k in keys if k[1]})
    if not item_ids and not urls:
        return {}
    clauses: list[str] = []
    params: list = [user_id]
    if item_ids:
        clauses.append(f"item_id IN ({','.join('?' for _ in item_ids)})")
        params += item_ids
    if urls:
        clauses.append(f"url IN ({','.join('?' for _ in urls)})")
        params += urls
    rows = conn.execute(
        "SELECT id, item_id, url, status, current_overall, projected_overall "
        f"FROM copy_items WHERE user_id = ? AND ({' OR '.join(clauses)}) ORDER BY id",
        params,
    ).fetchall()
    states: dict[str, dict] = {}

    def _entry(key: str) -> dict:
        return states.setdefault(key, {
            "has_done": False, "has_inflight": False, "has_failed": False,
            "latest_status": None, "latest_projected": None,
            "latest_current": None, "latest_copy_item_id": None,
        })

    for r in rows:
        status = r["status"]
        for key in (r["item_id"], r["url"]):
            if not key:
                continue
            e = _entry(key)
            if status in _DONE_STATUSES:
                e["has_done"] = True
            elif status in _INFLIGHT_STATUSES:
                e["has_inflight"] = True
            elif status in _FAILED_STATUSES:
                e["has_failed"] = True
            # Rows are ordered by id asc, so the last write wins = latest attempt.
            e["latest_status"] = status
            e["latest_projected"] = r["projected_overall"]
            e["latest_current"] = r["current_overall"]
            e["latest_copy_item_id"] = r["id"]
    return states


def copy_item_ids_for_items(
    conn: sqlite3.Connection, user_id: int, keys
) -> list[int]:
    """Latest copy_item id per product whose item id or URL matches ``keys``.

    Backs the "View copy results" cross-link from the scoring page. A product can
    have copy created more than once (each creation is its own ``copy_items`` row,
    i.e. a version), so this returns only the **most recent** copy per product —
    the link should land on the latest version, not every past one. Products are
    keyed by item id when present, else URL (same matching as
    :func:`copy_states_for_items`); recency is by row id (monotonic). Ids are
    returned ascending. ``[]`` for empty input.
    """
    item_ids = sorted({k[0] for k in keys if k[0]})
    urls = sorted({k[1] for k in keys if k[1]})
    if not item_ids and not urls:
        return []
    clauses: list[str] = []
    params: list = [user_id]
    if item_ids:
        clauses.append(f"item_id IN ({','.join('?' for _ in item_ids)})")
        params += item_ids
    if urls:
        clauses.append(f"url IN ({','.join('?' for _ in urls)})")
        params += urls
    rows = conn.execute(
        f"SELECT id, item_id, url FROM copy_items WHERE user_id = ? "
        f"AND ({' OR '.join(clauses)}) ORDER BY id",
        params,
    ).fetchall()
    # Ascending id order → the last row seen for each product is its latest copy.
    latest: dict = {}
    for r in rows:
        latest[r["item_id"] or r["url"]] = r["id"]
    return sorted(latest.values())


def has_claimable_items(conn: sqlite3.Connection) -> bool:
    """True if any copy row is waiting for a worker (queued or gen_queued).

    Lets the worker gate its copy thread pool — spin it up only when there's work
    rather than claim-probing on every idle poll. (A ``fetched`` row resting for
    the user's "Create new copy content" click is not claimable and not counted.)
    """
    return conn.execute(
        "SELECT 1 FROM copy_items WHERE status IN ('queued', 'gen_queued') LIMIT 1"
    ).fetchone() is not None


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
    brand: str | None = None,
) -> None:
    """Store the fetched current copy (and its score) and set the next status.

    ``next_status`` is ``'gen_queued'`` for an auto-generate row (the worker will
    immediately pick it back up to generate) or ``'fetched'`` otherwise (it rests
    until the user requests generation). The resolved keyword set is persisted so
    the generation phase reuses it without re-discovering.

    ``brand`` is the brand read from the PDP; like the scoring queue, it only fills
    the column when the user didn't provide one at intake (``COALESCE`` over the
    existing non-empty value), so a deliberate user label is never overwritten.
    """
    conn.execute(
        "UPDATE copy_items SET status = ?, title = ?, current_json = ?, "
        "current_overall = ?, keywords_json = ?, brand = COALESCE(NULLIF(brand, ''), ?), "
        "error = NULL, updated_at = datetime('now') WHERE id = ?",
        (
            next_status,
            title,
            json.dumps(current),
            current_overall,
            json.dumps(keywords or []),
            brand,
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


def count_copy_items(conn: sqlite3.Connection) -> int:
    """Total number of copy_items rows (all statuses) — an activity count."""
    return int(conn.execute("SELECT COUNT(*) FROM copy_items").fetchone()[0])


def count_copy_products(conn: sqlite3.Connection, user_id: int,
                        since: str | None = None) -> int:
    """Distinct products a user has created copy for (dashboard "PDP's copy created").

    Keyed on the Walmart item id so re-running the same product counts once; rows
    without an item id are excluded. ``since`` (an ISO date) restricts to rows
    created on/after that date, for the "this month" figure.
    """
    sql = ("SELECT COUNT(DISTINCT item_id) FROM copy_items "
           "WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''")
    params: list = [user_id]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    return int(conn.execute(sql, params).fetchone()[0])


def list_copy_activity(conn: sqlite3.Connection, user_id: int, since: str | None = None,
                       limit: int = 500) -> list[sqlite3.Row]:
    """Copy created for ``user_id`` — the dashboard "copy" table + its View All.

    Only ``done`` rows (new copy was generated — not merely fetched or in flight),
    newest first. ``since`` (an ISO date) restricts to the current month for the
    dashboard; omit it for the all-time View All screen. Carries the columns the
    activity table shows: item id (for the cached thumbnail), title, brand, and
    when it ran. Capped at ``limit`` so the query stays bounded.
    """
    sql = ("SELECT id, item_id, url, title, brand, created_at "
           "FROM copy_items WHERE user_id = ? AND status = 'done'")
    params: list = [user_id]
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def list_copy_items(conn: sqlite3.Connection, limit: int = 200) -> list[sqlite3.Row]:
    """Return recent copy items across all users (with submitter email) for admin.

    Newest first, capped at ``limit`` so the admin view stays bounded. Carries both
    the current and projected scores so the admin table can show the copy lift.
    """
    return conn.execute(
        "SELECT ci.id, ci.item_id, ci.url, ci.title, ci.status, ci.current_overall, "
        "ci.projected_overall, ci.created_at, ci.updated_at, u.email AS user_email "
        "FROM copy_items ci JOIN users u ON u.id = ci.user_id "
        "ORDER BY ci.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def mark_copy_failed(conn: sqlite3.Connection, row_id: int, status: str, message: str) -> None:
    """Mark a copy row ``blocked`` or ``error`` with a short message."""
    conn.execute(
        "UPDATE copy_items SET status = ?, error = ?, updated_at = datetime('now') WHERE id = ?",
        (status, message[:500], row_id),
    )
    conn.commit()


# The two in-flight statuses a worker holds a copy row in while doing work; a row
# left in either at startup was stranded by a mid-flight worker restart.
_ORPHAN_STATUSES = ("fetching", "generating")
_ORPHAN_MESSAGE = "Interrupted — the worker restarted mid-run (marked failed on startup). Re-run this item."


def reclaim_orphaned_copy_items(conn: sqlite3.Connection) -> int:
    """Fail any copy row still ``fetching`` or ``generating`` — call on worker startup.

    Copy runs concurrently, but inside a *single* worker process (a thread pool —
    see ``worker.drain_copy``). So a row in an in-flight status at startup can only
    be orphaned: the one process that claimed it died mid-fetch or mid-generation
    (a deploy restart or an OOM kill), and no survivor will finish it. Left alone it
    sits in-progress forever — the results view flashes it indefinitely with no
    signal to act. Marking it ``error`` surfaces the failure so the user can re-run
    it. Returns the count reclaimed.

    NB: this correctness relies on one worker *process*. Fanning copy out to
    multiple processes/hosts would need a per-claim lease (a ``claimed_at`` + age
    threshold) so one process restarting can't fail a peer's live row.
    """
    placeholders = ",".join("?" for _ in _ORPHAN_STATUSES)
    updated = conn.execute(
        f"UPDATE copy_items SET status = 'error', error = ?, "
        f"updated_at = datetime('now') WHERE status IN ({placeholders})",
        (_ORPHAN_MESSAGE, *_ORPHAN_STATUSES),
    )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned copy item(s) on startup", updated.rowcount)
    return updated.rowcount
