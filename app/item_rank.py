"""Daily search-rank + share-of-search capture for tracked portfolio items.

Each tracked item is scraped once a day for each of its (non-blank) keywords: we
pull the keyword's page-1 SERP, find where the item's own product places, and
record its rank plus how many of the page's placements are its own (``item_slots``
over ``total_slots`` = its share of search). Raw counts are stored per day; the
calendar-bucket readers below roll them up to the Weekly / Monthly / Quarterly
views (there is no daily view).

This is a dedicated pipeline (not the CI group machinery) keyed straight to
``portfolio_items`` — see the module docstrings in :mod:`app.db` for the three
tables. It reuses only stateless helpers: :func:`app.ci_scraper.scrape_keyword_cards`
for the SERP and :func:`app.pdp.item_number_from_url` for matching the item's own
cards. The queue mirrors the commerce-facts queue (claim/settle/reclaim) so it
self-heals on worker restart.

The daily enqueue is **gated off by default** (``ITEM_TRACKING_RANK_ENABLED``) so
the feature can ship without spending on scrapes until it's deliberately turned
on. Every request-reachable read is ``user_id``-scoped; all SQL is parameterized.
"""

import calendar
import logging
import os
import sqlite3
from datetime import date, timedelta

from app.pdp import item_number_from_url

logger = logging.getLogger(__name__)

# The grains the app presents (finest to coarsest). No daily view — daily readings
# are only ever rolled up into these calendar buckets.
GRAINS = ("week", "month", "quarter")

# How many trailing buckets a sparkline spans (including the selected one).
TREND_BUCKETS = 6

# Failure detail is capped before storage (mirrors the facts/report queues).
_MAX_MESSAGE_LEN = 500


def rank_capture_enabled() -> bool:
    """Whether the daily rank/share enqueue is turned on (env, default off).

    Kept as an env switch so deploying the code costs nothing until an operator
    sets ``ITEM_TRACKING_RANK_ENABLED`` truthy on the droplet — the daily scrape
    is the feature's dominant Oxylabs cost.
    """
    return os.environ.get("ITEM_TRACKING_RANK_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _norm_name(s: str | None) -> str:
    """Lowercase + strip non-alphanumerics for tolerant title matching.

    Mirrors :func:`app.ci_scraper._norm_name` so the sponsored-slot tie-back here
    behaves identically to the CI pipeline's.
    """
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def build_reading(cards: list[dict], walmart_item_id: str | None,
                  walmart_url: str | None = None) -> dict:
    """Reduce a keyword's page-1 cards to one item's rank + slot counts (pure).

    ``cards`` is the uniform SERP shape from :func:`app.ci_scraper.scrape_keyword_cards`
    (``{name, item_id, listing_type, product_url}``, in page order). A card is the
    tracked item's when its URL item number matches ``walmart_item_id``; a sponsored
    slot of the same product carries an opaque id but the same title, so — like the
    CI pipeline — we learn the item's title from its id-matched (organic) card and
    tie same-title sponsored slots back to it.

    Returns ``{rank, organic_rank, sponsored_rank, item_slots, total_slots}``:
    ``rank`` is the best (lowest) page-1 position of the item's placements (None
    when it isn't on page 1); ``total_slots`` is every placement on the page (the
    share-of-search denominator).
    """
    total = len(cards)
    target_id = (walmart_item_id or "").strip() or None

    def _card_id(card: dict) -> str | None:
        url = card.get("product_url") or ""
        return (item_number_from_url(url) if url else None) or (card.get("item_id") or None)

    def _matches_by_id(card: dict) -> bool:
        return target_id is not None and _card_id(card) == target_id

    # Pre-pass: learn the item's title from any id-matched card so a same-title
    # sponsored slot (opaque id) can be attributed back to it below.
    title_keys = {
        _norm_name(card.get("name")) for card in cards
        if _matches_by_id(card) and _norm_name(card.get("name"))
    }

    matched, organic, sponsored = [], [], []
    for position, card in enumerate(cards, start=1):
        is_ours = _matches_by_id(card)
        if not is_ours and title_keys and _norm_name(card.get("name")) in title_keys:
            is_ours = True
        if not is_ours:
            continue
        matched.append(position)
        if card.get("listing_type") == "sponsored":
            sponsored.append(position)
        else:
            organic.append(position)

    return {
        "rank": min(matched) if matched else None,
        "organic_rank": min(organic) if organic else None,
        "sponsored_rank": min(sponsored) if sponsored else None,
        "item_slots": len(matched),
        "total_slots": total,
    }


# ── Daily enqueue (request/cron side) ────────────────────────────────────────

def has_active_rank_job(conn: sqlite3.Connection, portfolio_item_id: int,
                        keyword_id: int) -> bool:
    """True if a rank job for this (item, keyword) is queued or in flight.

    The dedupe guard so a re-run of the daily sweep can't pile duplicate jobs on a
    keyword the worker hasn't drained yet.
    """
    return conn.execute(
        "SELECT 1 FROM portfolio_rank_jobs WHERE portfolio_item_id = ? AND keyword_id = ? "
        "AND status IN ('queued', 'fetching') LIMIT 1",
        (portfolio_item_id, keyword_id),
    ).fetchone() is not None


def enqueue_rank_jobs(conn: sqlite3.Connection) -> int:
    """Queue a daily rank job per (tracked item, non-blank keyword); return count.

    Skips items that aren't tracked, keywords left blank (those are deliberately
    not scraped — a cost brake), and any (item, keyword) already queued/in-flight.
    """
    candidates = conn.execute(
        "SELECT k.id AS keyword_id, k.keyword, p.id AS portfolio_item_id, p.user_id, "
        "       p.item_id AS walmart_item_id, p.walmart_url "
        "FROM portfolio_keywords k JOIN portfolio_items p ON p.id = k.portfolio_item_id "
        "WHERE p.tracked = 1 AND k.keyword IS NOT NULL AND TRIM(k.keyword) <> '' "
        "ORDER BY p.id, k.slot"
    ).fetchall()

    enqueued = 0
    for row in candidates:
        if has_active_rank_job(conn, row["portfolio_item_id"], row["keyword_id"]):
            continue
        conn.execute(
            "INSERT INTO portfolio_rank_jobs "
            "(portfolio_item_id, keyword_id, user_id, keyword, walmart_item_id, walmart_url) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (row["portfolio_item_id"], row["keyword_id"], row["user_id"],
             row["keyword"], row["walmart_item_id"], row["walmart_url"]),
        )
        enqueued += 1
    conn.commit()
    logger.info("Daily rank sweep: enqueued %d job(s) across tracked items", enqueued)
    return enqueued


# ── Worker-side claim / settle (keyed off the job row, not user-scoped) ───────

def has_claimable_rank_job(conn: sqlite3.Connection) -> bool:
    """True if any rank job is waiting (worker gate)."""
    return conn.execute(
        "SELECT 1 FROM portfolio_rank_jobs WHERE status = 'queued' LIMIT 1"
    ).fetchone() is not None


def claim_next_rank_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest queued rank job ('queued' -> 'fetching'), or None.

    Conditional UPDATE so a concurrent claimer can't grab the same row; ties FIFO
    by id. Mirrors the commerce-facts / title claim pattern.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM portfolio_rank_jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE portfolio_rank_jobs SET status = 'fetching' WHERE id = ? AND status = 'queued'",
            (candidate["id"],),
        )
        conn.commit()
        if updated.rowcount:
            return conn.execute(
                "SELECT * FROM portfolio_rank_jobs WHERE id = ?", (candidate["id"],)
            ).fetchone()


def save_reading(conn: sqlite3.Connection, job: sqlite3.Row, reading: dict,
                 captured_at: str) -> int:
    """Store one day's rank reading and settle the job to 'done'. Returns row id."""
    cur = conn.execute(
        "INSERT INTO portfolio_rank_readings "
        "(portfolio_item_id, keyword_id, user_id, walmart_item_id, captured_at, "
        " rank, organic_rank, sponsored_rank, item_slots, total_slots) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job["portfolio_item_id"], job["keyword_id"], job["user_id"],
         job["walmart_item_id"], captured_at, reading["rank"], reading["organic_rank"],
         reading["sponsored_rank"], reading["item_slots"], reading["total_slots"]),
    )
    conn.execute(
        "UPDATE portfolio_rank_jobs SET status = 'done', finished_at = datetime('now') "
        "WHERE id = ?",
        (job["id"],),
    )
    conn.commit()
    return int(cur.lastrowid)


def mark_rank_failed(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    """Mark a rank job failed ('error'). Not re-queued — the next day captures fresh."""
    conn.execute(
        "UPDATE portfolio_rank_jobs SET status = 'error', finished_at = datetime('now'), "
        "message = ? WHERE id = ?",
        ((message or "")[:_MAX_MESSAGE_LEN], job_id),
    )
    conn.commit()


def reclaim_orphaned_rank_jobs(conn: sqlite3.Connection) -> int:
    """At worker startup, fail any job stuck 'fetching' (a prior crash). Returns count.

    No requeue: a missed day is harmless — the next daily sweep captures fresh,
    just like the commerce-facts queue.
    """
    cur = conn.execute(
        "UPDATE portfolio_rank_jobs SET status = 'error', finished_at = datetime('now'), "
        "message = 'reclaimed: worker restart' WHERE status = 'fetching'"
    )
    conn.commit()
    if cur.rowcount:
        logger.warning("Reclaimed %d orphaned rank job(s) at startup", cur.rowcount)
    return cur.rowcount


# ── Calendar buckets + roll-up readers (Weekly / Monthly / Quarterly) ─────────
#
# Buckets are ABSOLUTE and user-selectable, not relative deltas:
#   • week    = fixed 7-day bins anchored at Jan 1 (week 1 = Jan 1–7, week 2 =
#               Jan 8–14, …); the final bin of the year is a short stub.
#   • month   = calendar month.
#   • quarter = calendar quarter (Q1 Jan–Mar, Q2 Apr–Jun, Q3 Jul–Sep, Q4 Oct–Dec).
# A bucket is identified by (grain, year, index): index is the week number, month
# (1–12), or quarter (1–4). Daily readings are aggregated into whichever bucket
# their captured_at date falls in; the app never shows a per-day figure.

def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


def weeks_in_year(year: int) -> int:
    """Number of 7-day bins covering the year (52 full + a 1–2 day stub = 53)."""
    return (_days_in_year(year) + 6) // 7


def week_bounds(year: int, n: int) -> tuple[str, str]:
    """(start, end) ISO dates for the n-th 7-day bin from Jan 1 (end clamped)."""
    jan1 = date(year, 1, 1)
    start = jan1 + timedelta(days=7 * (n - 1))
    end = jan1 + timedelta(days=min(7 * n, _days_in_year(year)) - 1)
    return start.isoformat(), end.isoformat()


def month_bounds(year: int, m: int) -> tuple[str, str]:
    """(first, last) ISO dates of calendar month m."""
    last = calendar.monthrange(year, m)[1]
    return date(year, m, 1).isoformat(), date(year, m, last).isoformat()


def quarter_bounds(year: int, q: int) -> tuple[str, str]:
    """(first, last) ISO dates of calendar quarter q (Q1 = Jan–Mar …)."""
    first_m = 3 * (q - 1) + 1
    last_m = first_m + 2
    last = calendar.monthrange(year, last_m)[1]
    return date(year, first_m, 1).isoformat(), date(year, last_m, last).isoformat()


def bucket_bounds(grain: str, year: int, index: int) -> tuple[str, str]:
    """Dispatch to the grain's bounds helper."""
    if grain == "week":
        return week_bounds(year, index)
    if grain == "month":
        return month_bounds(year, index)
    return quarter_bounds(year, index)


def bucket_of(grain: str, d: date) -> tuple[int, int]:
    """The (year, index) bucket a date falls in, for the given grain."""
    if grain == "week":
        return d.year, (d.timetuple().tm_yday - 1) // 7 + 1
    if grain == "month":
        return d.year, d.month
    return d.year, (d.month - 1) // 3 + 1


def _prev_bucket(grain: str, year: int, index: int) -> tuple[int, int]:
    """The bucket immediately before (year, index) at this grain."""
    if index > 1:
        return year, index - 1
    if grain == "week":
        return year - 1, weeks_in_year(year - 1)
    if grain == "month":
        return year - 1, 12
    return year - 1, 4


def bucket_label(grain: str, year: int, index: int) -> str:
    """Human label: 'Jan 1–7, 2026' / 'Jan 29 – Feb 4, 2026' / 'January 2026' / 'Q1 2026'."""
    if grain == "week":
        start, end = week_bounds(year, index)
        sd, ed = date.fromisoformat(start), date.fromisoformat(end)
        if sd.month == ed.month:
            return f"{sd:%b} {sd.day}–{ed.day}, {year}"
        return f"{sd:%b} {sd.day} – {ed:%b} {ed.day}, {year}"
    if grain == "month":
        return f"{date(year, index, 1):%B %Y}"
    return f"Q{index} {year}"


def _bucket_dict(grain: str, year: int, index: int) -> dict:
    start, end = bucket_bounds(grain, year, index)
    return {"grain": grain, "year": year, "index": index,
            "label": bucket_label(grain, year, index), "start": start, "end": end}


def available_buckets(conn: sqlite3.Connection, item_id: int, grain: str) -> list[dict]:
    """Buckets (newest first) that hold any reading for this item at this grain.

    Powers the grain's selector. Empty => the item has collected no data yet for
    this grain, which the page shows as a 'collecting data' state.
    """
    rows = conn.execute(
        "SELECT DISTINCT captured_at FROM portfolio_rank_readings WHERE portfolio_item_id = ?",
        (item_id,),
    ).fetchall()
    keys = {bucket_of(grain, date.fromisoformat(r["captured_at"][:10])) for r in rows}
    return [_bucket_dict(grain, y, i) for (y, i) in sorted(keys, reverse=True)]


def _aggregate(conn: sqlite3.Connection, item_id: int, keyword_id: int,
               start: str, end: str) -> tuple[float | None, float | None, int]:
    """(avg rank, share, reading count) for one keyword within [start, end].

    Rank averages the non-null daily positions; share sums the item's slots over
    all page slots across the window (not a mean of daily percentages).
    """
    rows = conn.execute(
        "SELECT rank, item_slots, total_slots FROM portfolio_rank_readings "
        "WHERE portfolio_item_id = ? AND keyword_id = ? AND captured_at BETWEEN ? AND ?",
        (item_id, keyword_id, start, end),
    ).fetchall()
    ranks = [r["rank"] for r in rows if r["rank"] is not None]
    avg_rank = (sum(ranks) / len(ranks)) if ranks else None
    item_slots = sum(r["item_slots"] for r in rows)
    total_slots = sum(r["total_slots"] for r in rows)
    share = (item_slots / total_slots) if total_slots else None
    return avg_rank, share, len(rows)


def keyword_rollup(conn: sqlite3.Connection, user_id: int, item_id: int, grain: str,
                   year: int, index: int) -> list[dict]:
    """Per-keyword rank + share for the selected bucket, with delta + trend series.

    User-scoped (IDOR). For each of the item's non-blank keywords it returns the
    selected bucket's average rank and share of search, the change versus the
    immediately-preceding bucket of the same grain, and the trailing
    :data:`TREND_BUCKETS`-bucket series (oldest→newest, nulls dropped) the route
    turns into a sparkline. ``n`` is the reading count in the selected bucket.
    """
    keywords = conn.execute(
        "SELECT id, slot, kind, keyword FROM portfolio_keywords "
        "WHERE portfolio_item_id = ? AND user_id = ? AND keyword IS NOT NULL "
        "AND TRIM(keyword) <> '' ORDER BY slot",
        (item_id, user_id),
    ).fetchall()

    # Trailing buckets, oldest→newest, ending at the selected one.
    span = [(year, index)]
    y, i = year, index
    for _ in range(TREND_BUCKETS - 1):
        y, i = _prev_bucket(grain, y, i)
        span.append((y, i))
    span.reverse()

    out = []
    for kw in keywords:
        rank_series, share_series = [], []
        n_selected = 0
        for pos, (by, bi) in enumerate(span):
            start, end = bucket_bounds(grain, by, bi)
            avg_rank, share, n = _aggregate(conn, item_id, kw["id"], start, end)
            rank_series.append(avg_rank)
            share_series.append(share)
            if pos == len(span) - 1:
                n_selected = n

        rank_current, rank_prev = rank_series[-1], rank_series[-2] if len(rank_series) >= 2 else None
        share_current, share_prev = share_series[-1], share_series[-2] if len(share_series) >= 2 else None
        out.append({
            "slot": kw["slot"],
            "kind": kw["kind"],
            "keyword": kw["keyword"],
            "rank_current": rank_current,
            "rank_delta": (rank_current - rank_prev)
                          if (rank_current is not None and rank_prev is not None) else None,
            "rank_series": [v for v in rank_series if v is not None],
            "share_current": share_current,
            "share_delta": (share_current - share_prev)
                           if (share_current is not None and share_prev is not None) else None,
            "share_series": [v for v in share_series if v is not None],
            "n": n_selected,
        })
    return out
