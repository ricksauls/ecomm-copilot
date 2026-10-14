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
import uuid

logger = logging.getLogger(__name__)


def enqueue_items(conn: sqlite3.Connection, user_id: int, items: list[dict]) -> list[int]:
    """Insert queued rows for ``items`` (each ``{"url", "item", "brand"?}``); return ids.

    ``brand`` is the optional brand the user typed at intake; it's stored up front
    so the dashboard's brand count reflects the submission immediately (and even
    when a later fetch is blocked). The worker fills it in from the PDP only when
    the user left it blank — see :func:`save_result`.

    All items in one submission share a generated ``batch_id`` so a later
    dashboard row click can reopen the whole run's results (see
    :func:`batch_ids_for_item`).
    """
    batch_id = uuid.uuid4().hex
    ids: list[int] = []
    for it in items:
        cur = conn.execute(
            "INSERT INTO scored_items (user_id, item_id, url, brand, batch_id, status) "
            "VALUES (?, ?, ?, ?, ?, 'queued')",
            (user_id, it.get("item"), it["url"], it.get("brand"), batch_id),
        )
        ids.append(int(cur.lastrowid))
    conn.commit()
    logger.info("Enqueued %d PDP scoring item(s) for user_id=%s batch=%s",
                len(ids), user_id, batch_id)
    return ids


def batch_ids_for_item(conn: sqlite3.Connection, row_id: int, user_id: int) -> list[int]:
    """All scored_items ids in the same run as ``row_id`` (owned by ``user_id``).

    Backs the dashboard/View All row click: open the whole run the clicked item
    belongs to. Items submitted together share a ``batch_id``, so we return every
    sibling (ordered). Rows scored before batch tracking have no ``batch_id`` and
    open on their own. Returns ``[]`` when the item isn't found or isn't owned by
    the user, so the caller can 404 (IDOR guard).
    """
    row = conn.execute(
        "SELECT batch_id FROM scored_items WHERE id = ? AND user_id = ?",
        (row_id, user_id),
    ).fetchone()
    if row is None:
        return []
    if not row["batch_id"]:
        return [row_id]
    siblings = conn.execute(
        "SELECT id FROM scored_items WHERE batch_id = ? AND user_id = ? ORDER BY id",
        (row["batch_id"], user_id),
    ).fetchall()
    return [r["id"] for r in siblings]


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


# The "strong" content-health band boundary: a product scoring below this has at
# least one dimension worth fixing. Mirrors the strong≥80 cut in the scoring
# results summary (``_SCORE_BANDS`` in app/routes/pages.py); kept here as a plain
# constant so this aggregate stays importable without pulling in the routes module.
READINESS_STRONG_MIN = 80

# Each scoring dimension maps to the workflow that fixes it: the imagery dimension
# is addressed in the image tools, the text dimensions in Product Copy. The
# dashboard Opportunities list uses this split to point each gap at the right
# workflow. Keys match the ``dimensions[].key`` values in a scored result_json.
IMAGE_DIMENSIONS = frozenset({"imagery"})
COPY_DIMENSIONS = frozenset({"title", "key_features", "description"})


def readiness_breakdown(conn: sqlite3.Connection, user_id: int,
                        brands: list[str]) -> dict:
    """Per-workflow content gaps for a set of brands — feeds the Opportunities list.

    Reads each product's *latest* scored result (one row per item, newest first),
    parses its dimension scores, and tallies how many products have an **image gap**
    (the imagery dimension below :data:`READINESS_STRONG_MIN`) versus a **copy gap**
    (any text dimension below it). A product can count in both buckets. Only
    dimensions marked ``available`` are judged — an unmeasured dimension (e.g. images
    that couldn't be fetched) is not counted as a gap.

    Scoping to ``brands`` (the selected CI group's own brand names) keeps the
    Opportunities list on the same brand as the three cards; an empty list
    short-circuits (no brand selected / matched).

    Args:
        conn: Open SQLite connection (request or worker).
        user_id: Owner of the scored items.
        brands: Brand names to include; matched case/space-insensitively.

    Returns:
        ``{scored_count, image_gap_count, copy_gap_count, image_items, copy_items}``.
        The ``*_items`` lists carry ``{item_id, title}`` for the gapped products
        (capped at :data:`_OPP_ITEMS_CAP`), newest-scored first, for later drill-down.
    """
    wanted = [b.strip().lower() for b in brands if b and b.strip()]
    if not wanted:
        return {"scored_count": 0, "image_gap_count": 0, "copy_gap_count": 0,
                "image_items": [], "copy_items": []}

    placeholders = ",".join("?" for _ in wanted)
    # One row per product at its most recent scored state (same window-function
    # latest-per-item rule as readiness_score), carrying the dimension detail.
    rows = conn.execute(
        "SELECT item_id, title, result_json FROM ("
        "  SELECT item_id, title, result_json, ROW_NUMBER() OVER ("
        "    PARTITION BY item_id ORDER BY created_at DESC, id DESC) AS rn "
        "  FROM scored_items "
        "  WHERE user_id = ? AND item_id IS NOT NULL AND item_id != '' "
        "    AND overall IS NOT NULL AND result_json IS NOT NULL AND result_json != '' "
        "    AND LOWER(TRIM(brand)) IN (" + placeholders + ")"
        ") WHERE rn = 1 ORDER BY item_id",
        [user_id, *wanted],
    ).fetchall()

    scored_count = 0
    image_items: list[dict] = []
    copy_items: list[dict] = []
    for r in rows:
        try:
            dims = json.loads(r["result_json"]).get("dimensions", [])
        except (ValueError, TypeError):
            # A malformed result shouldn't sink the whole briefing — skip it and
            # keep going, but leave a breadcrumb so a bad write is traceable.
            logger.warning("Skipping unparseable scored result user_id=%s item_id=%s",
                           user_id, r["item_id"])
            continue
        scored_count += 1
        # Capture the weak dimensions' scores too, so the Opportunities page can show
        # how far below target each product is (the dashboard list uses only counts).
        image_score = None
        copy_weak: list[dict] = []
        for d in dims:
            if not d.get("available", True) or d.get("score") is None:
                continue
            if d["score"] >= READINESS_STRONG_MIN:
                continue
            if d.get("key") in IMAGE_DIMENSIONS:
                image_score = d["score"]  # imagery is the sole image dimension
            elif d.get("key") in COPY_DIMENSIONS:
                copy_weak.append({"label": d.get("label") or d.get("key"), "score": d["score"]})
        item = {"item_id": r["item_id"], "title": r["title"]}
        if image_score is not None:
            image_items.append({**item, "score": image_score})
        if copy_weak:
            # Worst dimension first, so the row leads with the biggest problem.
            copy_weak.sort(key=lambda w: w["score"])
            copy_items.append({**item, "weak": copy_weak})

    logger.debug("Readiness breakdown user_id=%s brands=%s → scored=%d image_gap=%d copy_gap=%d",
                 user_id, wanted, scored_count, len(image_items), len(copy_items))
    return {
        "scored_count": scored_count,
        "image_gap_count": len(image_items),
        "copy_gap_count": len(copy_items),
        "image_items": image_items[:_OPP_ITEMS_CAP],
        "copy_items": copy_items[:_OPP_ITEMS_CAP],
    }


# Upper bound on the example products carried per content gap (the row shows a
# count; the list is for a future per-product drill-down). Keeps the payload small.
_OPP_ITEMS_CAP = 20


def readiness_score(conn: sqlite3.Connection, user_id: int, brands: list[str],
                    as_of: str | None = None) -> dict:
    """Content Readiness for a set of brands — the dashboard's "Conversion" card.

    Readiness is the average of each product's *latest* overall score (0-100), so
    a product counts once at its current state no matter how many times it was
    re-scored. Scoping to ``brands`` (the CI group's own brand names) ties the card
    to the same brand context as the Discoverability/Competition cards; an empty
    ``brands`` list short-circuits to an empty result (no brand selected / matched).

    ``as_of`` (an ISO datetime) computes readiness as it stood at a past moment —
    the latest score *on or before* that date — which is how the card derives its
    period-over-period delta and trend without any extra history table.

    Args:
        conn: Open SQLite connection (request or worker).
        user_id: Owner of the scored items.
        brands: Brand names to include; matched case/space-insensitively.
        as_of: Optional ISO datetime upper bound on ``created_at``.

    Returns:
        ``{avg, scored_count, needs_attention_count}`` — ``avg`` is the rounded
        mean readiness (``None`` when no products qualify), ``scored_count`` the
        number of distinct products averaged, and ``needs_attention_count`` those
        scoring below :data:`READINESS_STRONG_MIN`.
    """
    # Normalize brand names to match the LOWER(TRIM(...)) comparison in SQL. An
    # empty set means "no brand in scope", which can't match anything — return
    # early rather than build a degenerate ``IN ()`` clause.
    wanted = [b.strip().lower() for b in brands if b and b.strip()]
    if not wanted:
        return {"avg": None, "scored_count": 0, "needs_attention_count": 0}

    placeholders = ",".join("?" for _ in wanted)
    params: list = [user_id, *wanted]
    as_of_clause = ""
    if as_of:
        as_of_clause = " AND created_at <= ?"
        params.append(as_of)

    # One row per product: its most recent scored overall (newest created_at, with
    # id as the tiebreak when two scores share a timestamp). Window function keeps
    # this correct even when re-scores land in the same second.
    rows = conn.execute(
        "SELECT overall FROM ("
        "  SELECT overall, ROW_NUMBER() OVER ("
        "    PARTITION BY item_id ORDER BY created_at DESC, id DESC) AS rn "
        "  FROM scored_items "
        "  WHERE user_id = ? AND item_id IS NOT NULL AND item_id != '' "
        "    AND overall IS NOT NULL "
        "    AND LOWER(TRIM(brand)) IN (" + placeholders + ")" + as_of_clause +
        ") WHERE rn = 1",
        params,
    ).fetchall()

    if not rows:
        return {"avg": None, "scored_count": 0, "needs_attention_count": 0}

    scores = [r["overall"] for r in rows]
    needs_attention = sum(1 for s in scores if s < READINESS_STRONG_MIN)
    avg = round(sum(scores) / len(scores))
    logger.debug("Readiness user_id=%s brands=%s as_of=%s → avg=%s n=%d needs=%d",
                 user_id, wanted, as_of, avg, len(scores), needs_attention)
    return {"avg": avg, "scored_count": len(scores), "needs_attention_count": needs_attention}


def readiness_items(conn: sqlite3.Connection, user_id: int, brands: list[str]) -> list[dict]:
    """Every scored product for a set of brands at its latest overall score.

    Feeds the Content Readiness tab's product table (all products, not just the
    gapped ones). ``{item_id, title, score}`` per product, worst-first so the ones
    needing work surface at the top. Same latest-per-item rule as
    :func:`readiness_score`; an empty brand scope short-circuits.
    """
    wanted = [b.strip().lower() for b in brands if b and b.strip()]
    if not wanted:
        return []
    placeholders = ",".join("?" for _ in wanted)
    rows = conn.execute(
        "SELECT item_id, title, overall FROM ("
        "  SELECT item_id, title, overall, ROW_NUMBER() OVER ("
        "    PARTITION BY item_id ORDER BY created_at DESC, id DESC) AS rn "
        "  FROM scored_items "
        "  WHERE user_id = ? AND item_id IS NOT NULL AND item_id != '' "
        "    AND overall IS NOT NULL AND LOWER(TRIM(brand)) IN (" + placeholders + ")"
        ") WHERE rn = 1 ORDER BY overall ASC, item_id",
        [user_id, *wanted],
    ).fetchall()
    return [{"item_id": r["item_id"], "title": r["title"], "score": r["overall"]} for r in rows]


def readiness_item(conn: sqlite3.Connection, user_id: int, item_id: str,
                   as_of: str | None = None) -> dict | None:
    """One product's latest scored result, for the drilled Content Readiness view.

    Returns ``{title, overall, dimensions}`` (each dimension ``{label, score,
    available}``) for the newest score on or before ``as_of`` (when given, so a
    per-item score trend can read past periods), or ``None`` when the product has no
    score. Scoped to the owner; the caller has already checked the item belongs to
    the selected brand.
    """
    params: list = [user_id, item_id]
    clause = ""
    if as_of:
        clause = " AND created_at <= ?"
        params.append(as_of)
    row = conn.execute(
        "SELECT title, overall, result_json FROM scored_items "
        "WHERE user_id = ? AND item_id = ? AND overall IS NOT NULL" + clause +
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    if row is None:
        return None
    try:
        dims = json.loads(row["result_json"] or "{}").get("dimensions", [])
    except (ValueError, TypeError):
        # A malformed result shouldn't blank the whole view — fall back to no dims.
        logger.warning("Unparseable scored result user_id=%s item_id=%s", user_id, item_id)
        dims = []
    dimensions = [{"label": d.get("label") or d.get("key"), "score": d.get("score"),
                   "available": d.get("available", True)} for d in dims]
    return {"title": row["title"], "overall": row["overall"], "dimensions": dimensions}


def count_managed_products(conn: sqlite3.Connection, user_id: int,
                           since: str | None = None) -> int:
    """Distinct products a user has worked on — the dashboard "Products managed" KPI.

    A product counts once if the user has scored it OR created copy content for it
    OR added it to their managed portfolio (creative/image sets will union in here
    once that feature exists). Keyed on the Walmart item id, so the same product
    across scoring + copy + portfolio is counted once; rows without an item id (a
    failed parse) are excluded. ``since`` (an ISO date) restricts to rows created
    on/after that date, for the "this month" figure.
    """
    date_clause = " AND created_at >= ?" if since else ""
    params: list = [user_id]
    if since:
        params.append(since)
    # Same clause + params for each arm of the UNION (scored / copy / portfolio).
    row = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT item_id FROM scored_items "
        "    WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''" + date_clause +
        "  UNION "
        "  SELECT item_id FROM copy_items "
        "    WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''" + date_clause +
        "  UNION "
        "  SELECT item_id FROM portfolio_items "
        "    WHERE user_id = ? AND item_id IS NOT NULL AND item_id != ''" + date_clause +
        ")",
        params + params + params,
    ).fetchone()
    return int(row[0])


def count_managed_brands(conn: sqlite3.Connection, user_id: int,
                         since: str | None = None) -> int:
    """Distinct brands a user has worked on — the dashboard "brands" subtitle figure.

    A brand counts once across everything the user has scored OR created copy for
    OR added to their managed portfolio (creative/image sets union in here once
    that feature exists). Compared case-insensitively (``LOWER(TRIM(...))``) so
    "Tabasco" and "TABASCO" — e.g. a user-typed label vs the PDP's spelling —
    don't double-count; NULL/blank brands are excluded. ``since`` (an ISO date)
    restricts to rows created on/after that date, mirroring the product counts.
    """
    date_clause = " AND created_at >= ?" if since else ""
    params: list = [user_id]
    if since:
        params.append(since)
    # Same brand filter + params for each arm of the UNION (scored / copy / portfolio).
    brand_filter = " AND brand IS NOT NULL AND TRIM(brand) != ''"
    row = conn.execute(
        "SELECT COUNT(DISTINCT LOWER(TRIM(brand))) FROM ("
        "  SELECT brand FROM scored_items WHERE user_id = ?" + brand_filter + date_clause +
        "  UNION ALL "
        "  SELECT brand FROM copy_items WHERE user_id = ?" + brand_filter + date_clause +
        "  UNION ALL "
        "  SELECT brand FROM portfolio_items WHERE user_id = ?" + brand_filter + date_clause +
        ")",
        params + params + params,
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
    sql = ("SELECT id, item_id, url, title, brand, overall, created_at, batch_id "
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


def has_queued_items(conn: sqlite3.Connection) -> bool:
    """True if any scoring item is waiting to be claimed.

    Lets the worker gate its thread pool: spin threads up only when there's work,
    rather than churning a pool of claimers on every idle poll.
    """
    return conn.execute(
        "SELECT 1 FROM scored_items WHERE status = 'queued' LIMIT 1"
    ).fetchone() is not None


def claim_next(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest queued row, marking it ``scoring``.

    Uses a conditional UPDATE so two concurrent claimers can't grab the same row
    (the scoring worker runs a pool of them); if the claim loses the race it
    retries the next candidate.
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
                title: str | None = None, brand: str | None = None,
                record: dict | None = None) -> None:
    """Store a completed score (and the fetched product title) and mark it scored.

    ``title`` is captured so the results view can label each item by product name
    rather than URL alone; it's unknown at enqueue time and filled in here.

    ``brand`` is the brand read from the PDP. It only fills the column when the
    user didn't already provide one at intake (``COALESCE`` over the existing,
    non-empty value) so a deliberate user label is never overwritten by the
    scraped value.

    ``record`` is the full PdpRecord (as a dict) captured during this fetch. We
    persist it so the "Create new copy content" cross-link can reuse the current
    copy instead of re-fetching the PDP. ``None`` leaves the column untouched
    (e.g. a caller that doesn't carry the record).
    """
    conn.execute(
        "UPDATE scored_items SET status = 'scored', overall = ?, result_json = ?, "
        "title = ?, brand = COALESCE(NULLIF(brand, ''), ?), record_json = ?, "
        "error = NULL, updated_at = datetime('now') WHERE id = ?",
        (overall, json.dumps(result), title, brand,
         json.dumps(record) if record is not None else None, row_id),
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

    Scoring runs concurrently, but inside a *single* worker process (a thread
    pool — see ``worker.drain_scoring``). So at startup every ``scoring`` row is
    necessarily orphaned: the one process that owned those in-flight items died
    mid-fetch (a deploy restart or an OOM kill when headed Chrome spiked memory),
    and no survivor will finish them. Left alone a row sits ``scoring`` forever —
    the results view flashes it as in-progress indefinitely with no signal to act.
    Marking it ``error`` surfaces the failure so the user can re-run it. Returns
    the count reclaimed.

    NB: this correctness relies on one worker *process*. Fanning scoring out to
    multiple processes/hosts would need a per-claim lease (a ``claimed_at`` + age
    threshold) so one process restarting can't fail a peer's live item.
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
