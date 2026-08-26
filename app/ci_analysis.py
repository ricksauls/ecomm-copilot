"""Read-side aggregations for the Competitive Intelligence dashboard.

Turns the raw ``ci_search_results`` / ``ci_share_of_search`` rows into the two
headline views — **Search Ranking** and **Share of Digital Shelf** (organic vs
sponsored) — plus their trends over a rolling period window. Ported from the
reference wm-dot-com-competitive-intelligence ``analysis.py`` (SQLAlchemy -> raw
SQLite). All queries are parameterized and scoped to a single group; callers
verify group ownership first (see :func:`app.ci_config.owns_group`).

A note on "position": lower is better (position 1 is the top slot), so a rank
delta is ``prior - current`` — positive means the product moved *up*.
"""

import logging
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Rolling period windows (days), ending today. Mirrors the reference periods.
PERIOD_DAYS = {"wow": 7, "mom": 30, "qoq": 90, "yoy": 365}
DEFAULT_PERIOD = "wow"

# The three daily monitoring slots, in Central time (matches the systemd timers).
CST = ZoneInfo("America/Chicago")
UTC = ZoneInfo("UTC")
MONITORING_HOURS = (7, 15, 23)  # 7 AM, 3 PM, 11 PM CST

# Search Ranking answers "where does *my tracked item* stand", so it counts only
# the group's tracked products (by Walmart item id) — never a brand's untracked
# SKUs, which the share-of-shelf name matcher deliberately sweeps in and which
# would otherwise drag a brand's average down. (Share of Digital Shelf still counts
# every SKU; that is its correct, brand-level meaning.) A tracked item's sponsored
# slot carries an opaque id that can't be tied back to the item number, so only its
# organic placements match here — an acceptable, unavoidable limitation.
# Appended into a WHERE clause; contributes one bound parameter (the group id).
_TRACKED_ITEMS_FILTER = (
    "item_id IN (SELECT walmart_item_id FROM ci_products "
    "WHERE group_id = ? AND active = 1)"
)

# Shared display format for run/schedule times (e.g. "Mon Aug 24, 3:00 PM CST").
_TIME_FMT = "%a %b %-d, %-I:%M %p"


def format_run_time_cst(ts: str | None) -> str | None:
    """Format a stored UTC run timestamp as Central time (matches the schedule).

    Run timestamps are written by SQLite ``datetime('now')`` in UTC
    ("YYYY-MM-DD HH:MM:SS"). The monitoring screens otherwise show Central
    wall-clock (the 7/3/11 slots, the next run), so a bare UTC timestamp beside
    them reads as a different — and confusingly earlier — time. Returns the same
    shape as the next-run label; falls back to the raw string on a parse failure
    (better to show *something* than to drop the run time) and ``None`` when empty.
    """
    if not ts:
        return None
    try:
        naive = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        logger.warning("Unparseable run timestamp, showing raw value: %r", ts)
        return ts
    local = naive.replace(tzinfo=UTC).astimezone(CST)
    return local.strftime(_TIME_FMT) + " CST"


def next_monitoring_run(now: datetime | None = None) -> datetime:
    """Return the next 7 AM / 3 PM / 11 PM Central run strictly after ``now``.

    Timezone-aware (DST handled by zoneinfo) so the monitoring screens can show
    the same wall-clock time the systemd timers fire at.
    """
    now = now.astimezone(CST) if now else datetime.now(CST)
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for hour in MONITORING_HOURS:
            candidate = datetime(day.year, day.month, day.day, hour, 0, tzinfo=CST)
            if candidate > now:
                return candidate
    # Unreachable (tomorrow's 7 AM always qualifies), but keep the type total.
    return datetime(now.year, now.month, now.day, MONITORING_HOURS[0], 0, tzinfo=CST)


def get_date_range(period: str, today: date | None = None) -> tuple[str, str]:
    """Return (start, end) ISO dates for the current window (inclusive)."""
    today = today or date.today()
    days = PERIOD_DAYS.get(period, PERIOD_DAYS[DEFAULT_PERIOD])
    start = today - timedelta(days=days)
    return start.isoformat(), today.isoformat()


def get_prior_date_range(period: str, today: date | None = None) -> tuple[str, str]:
    """Return (start, end) ISO dates for the immediately preceding window."""
    today = today or date.today()
    days = PERIOD_DAYS.get(period, PERIOD_DAYS[DEFAULT_PERIOD])
    end = today - timedelta(days=days)
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _pct(part: int, whole: int) -> float:
    """Percentage of ``part`` in ``whole``, rounded to one decimal (0 if whole=0)."""
    return round(100.0 * part / whole, 1) if whole else 0.0


# ── Share of Digital Shelf ───────────────────────────────────────────────────────

def share_of_shelf_summary(conn: sqlite3.Connection, group_id: int, period: str) -> list[dict]:
    """Per-brand share of page-1 slots over the window, with a delta vs the prior window.

    Aggregates every keyword's rollup in the group. ``*_share`` are percentages of
    all slots recorded in the window (organic share is of organic slots, etc.).
    Rows are ordered mine-first, then by total share descending; the unmatched
    "Other" bucket (brand_id NULL) sorts last.
    """
    start, end = get_date_range(period)
    p_start, p_end = get_prior_date_range(period)

    cur = _sos_totals_by_brand(conn, group_id, start, end)
    prior = _sos_totals_by_brand(conn, group_id, p_start, p_end)

    grand = {k: sum(b[k] for b in cur.values()) for k in ("organic", "sponsored", "total")}
    grand_prior_total = sum(b["total"] for b in prior.values())

    # Daily total-share % per brand over the window, for the per-row trend
    # sparkline (mirrors the Search Ranking table's `positions`).
    dates, share_series = _daily_total_share_by_brand(conn, group_id, start, end)

    brand_meta = _brand_meta(conn, group_id)
    out = []
    for brand_id, c in cur.items():
        meta = brand_meta.get(brand_id)
        total_share = _pct(c["total"], grand["total"])
        prior_share = _pct(prior.get(brand_id, {}).get("total", 0), grand_prior_total)
        out.append({
            "brand_id": brand_id,
            "brand_name": meta["name"] if meta else "Other",
            "type": meta["type"] if meta else "other",
            "organic": c["organic"],
            "sponsored": c["sponsored"],
            "total": c["total"],
            "organic_share": _pct(c["organic"], grand["organic"]),
            "sponsored_share": _pct(c["sponsored"], grand["sponsored"]),
            "total_share": total_share,
            "total_share_prior": prior_share,
            "total_share_delta": round(total_share - prior_share, 1),
            # Daily total-share % across the window for the trend sparkline.
            "dates": dates,
            "shares": share_series.get(brand_id, []),
        })

    def _sort_key(r):
        # mine (0) before competitor (1) before other (2); then higher share first.
        rank = {"mine": 0, "competitor": 1}.get(r["type"], 2)
        return (rank, -r["total_share"])

    out.sort(key=_sort_key)
    return out


def _daily_total_share_by_brand(conn: sqlite3.Connection, group_id: int,
                                start: str, end: str) -> tuple[list[str], dict]:
    """Daily total-share-% series per brand over a window.

    Returns ``(dates, {brand_id: [share%, ...]})`` where each list is aligned to
    ``dates`` and a share is the brand's percentage of all page-1 slots on that
    date. Shared by the trend chart and the Share-of-Shelf table's per-row
    sparkline so the two never disagree.
    """
    rows = conn.execute(
        "SELECT date, brand_id, SUM(total_count) AS total "
        "FROM ci_share_of_search WHERE group_id = ? AND date >= ? AND date <= ? "
        "GROUP BY date, brand_id ORDER BY date",
        (group_id, start, end),
    ).fetchall()

    dates = sorted({r["date"] for r in rows})
    grand_by_date: dict = defaultdict(int)
    by_brand_date: dict = defaultdict(dict)
    for r in rows:
        grand_by_date[r["date"]] += r["total"]
        by_brand_date[r["brand_id"]][r["date"]] = r["total"]

    series_by_brand = {
        brand_id: [_pct(by_date.get(d, 0), grand_by_date[d]) for d in dates]
        for brand_id, by_date in by_brand_date.items()
    }
    return dates, series_by_brand


def share_of_shelf_trend(conn: sqlite3.Connection, group_id: int, period: str) -> dict:
    """Total-share-% per brand per date over the window (for the trend chart).

    Returns ``{"dates": [...], "brands": [{brand_id, brand_name, type, share: [...]}]}``
    where each share value is that brand's percentage of all slots on that date.
    """
    start, end = get_date_range(period)
    dates, series_by_brand = _daily_total_share_by_brand(conn, group_id, start, end)

    brand_meta = _brand_meta(conn, group_id)
    brands_out = []
    # Group brands (in meta order) first, then Other (None) if present.
    ordered_ids = [bid for bid in brand_meta if bid in series_by_brand]
    if None in series_by_brand:
        ordered_ids.append(None)
    for brand_id in ordered_ids:
        meta = brand_meta.get(brand_id)
        brands_out.append({
            "brand_id": brand_id,
            "brand_name": meta["name"] if meta else "Other",
            "type": meta["type"] if meta else "other",
            "share": series_by_brand[brand_id],
        })
    return {"dates": dates, "brands": brands_out}


def _sos_totals_by_brand(conn: sqlite3.Connection, group_id: int,
                         start: str, end: str) -> dict[int | None, dict]:
    """Sum organic/sponsored/total per brand over a date window."""
    rows = conn.execute(
        "SELECT brand_id, SUM(organic_count) AS o, SUM(sponsored_count) AS s, "
        "SUM(total_count) AS t FROM ci_share_of_search "
        "WHERE group_id = ? AND date >= ? AND date <= ? GROUP BY brand_id",
        (group_id, start, end),
    ).fetchall()
    return {
        r["brand_id"]: {"organic": r["o"] or 0, "sponsored": r["s"] or 0, "total": r["t"] or 0}
        for r in rows
    }


def snapshot_share_of_shelf(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Per-brand share of shelf for a single run (current-state, no deltas/trends).

    Used by the One-Time Snapshot results — one run, so trend/delta are meaningless.
    """
    rows = conn.execute(
        "SELECT brand_id, SUM(organic_count) AS o, SUM(sponsored_count) AS s, "
        "SUM(total_count) AS t FROM ci_share_of_search "
        "WHERE group_id = ? AND run_id = ? GROUP BY brand_id",
        (group_id, run_id),
    ).fetchall()
    counts = {r["brand_id"]: {"organic": r["o"] or 0, "sponsored": r["s"] or 0,
                              "total": r["t"] or 0} for r in rows}
    grand = {k: sum(b[k] for b in counts.values()) for k in ("organic", "sponsored", "total")}

    brand_meta = _brand_meta(conn, group_id)
    out = []
    for brand_id, c in counts.items():
        meta = brand_meta.get(brand_id)
        out.append({
            "brand_id": brand_id,
            "brand_name": meta["name"] if meta else "Other",
            "type": meta["type"] if meta else "other",
            "organic": c["organic"],
            "sponsored": c["sponsored"],
            "total": c["total"],
            "organic_share": _pct(c["organic"], grand["organic"]),
            "sponsored_share": _pct(c["sponsored"], grand["sponsored"]),
            "total_share": _pct(c["total"], grand["total"]),
        })
    out.sort(key=lambda r: ({"mine": 0, "competitor": 1}.get(r["type"], 2), -r["total_share"]))
    return out


def snapshot_share_by_keyword(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Per-keyword, per-brand share of shelf for a single run.

    Like :func:`snapshot_share_of_shelf` but broken out by keyword: shares are of
    that keyword's own page-1 slots (so each keyword's total shares sum to ~100%,
    including the untracked "Other" bucket). Ordered by keyword, then by total
    share descending within each keyword (largest shelf holder first).
    """
    rows = conn.execute(
        "SELECT k.keyword AS keyword, sos.brand_id AS brand_id, "
        "  SUM(sos.organic_count) AS o, SUM(sos.sponsored_count) AS s, "
        "  SUM(sos.total_count) AS t "
        "FROM ci_share_of_search sos "
        "JOIN ci_keywords k ON k.id = sos.keyword_id "
        "WHERE sos.group_id = ? AND sos.run_id = ? "
        "GROUP BY k.id, sos.brand_id",
        (group_id, run_id),
    ).fetchall()

    brand_meta = _brand_meta(conn, group_id)
    by_kw: dict = defaultdict(list)
    for r in rows:
        by_kw[r["keyword"]].append(r)

    out = []
    for kw in sorted(by_kw, key=str.lower):
        krows = by_kw[kw]
        # Per-keyword denominators so shares are of this keyword's own slots.
        grand = {key: sum((r[col] or 0) for r in krows)
                 for key, col in (("organic", "o"), ("sponsored", "s"), ("total", "t"))}
        entries = []
        for r in krows:
            meta = brand_meta.get(r["brand_id"])
            entries.append({
                "keyword": kw,
                "brand_name": meta["name"] if meta else "Other",
                "type": meta["type"] if meta else "other",
                "organic_share": _pct(r["o"] or 0, grand["organic"]),
                "sponsored_share": _pct(r["s"] or 0, grand["sponsored"]),
                "total_share": _pct(r["t"] or 0, grand["total"]),
            })
        entries.sort(key=lambda e: -e["total_share"])
        out.extend(entries)
    return out


def snapshot_rank(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Best page-1 position per brand per keyword within a single run (no trend).

    Brand-level (mine AND competitors) so the user can compare their standing to
    rivals. ``current_position`` is the best (lowest) slot the brand holds for the
    keyword across organic + sponsored; the split columns show each channel's best
    (None when the brand had no slot of that type). Attribution is by brand_id,
    which the scraper now sets via name matching, so sponsored slots are included.
    """
    rows = conn.execute(
        "SELECT b.name AS brand_name, b.type AS brand_type, k.keyword AS keyword, "
        "  MIN(sr.position) AS best, "
        "  MIN(CASE WHEN sr.position_type = 'organic' THEN sr.position END) AS organic_best, "
        "  MIN(CASE WHEN sr.position_type = 'sponsored' THEN sr.position END) AS sponsored_best "
        "FROM ci_search_results sr "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "WHERE sr.group_id = ? AND sr.run_id = ? "
        "GROUP BY b.id, k.id "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, "
        "  b.name COLLATE NOCASE, k.keyword COLLATE NOCASE",
        (group_id, run_id),
    ).fetchall()
    return [{
        "brand_name": r["brand_name"],
        "type": r["brand_type"],
        "keyword": r["keyword"],
        "current_position": r["best"],
        "organic_position": r["organic_best"],
        "sponsored_position": r["sponsored_best"],
    } for r in rows]


def snapshot_brand_avg_rank(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Average page-1 ranking per brand across the terms it appears on.

    The headline "how am I doing overall" figure. Two-stage average so it reconciles
    with the per-term table (:func:`snapshot_rank_by_keyword_brand`): first average
    the brand's *tracked* placements within each keyword, then average those
    per-keyword figures across every term the tracked item placed on. Terms with no
    page-1 slot are excluded from the math, not scored as a penalty; untracked SKUs
    are excluded too (see :data:`_TRACKED_ITEMS_FILTER`). Ordered mine-first, then
    best (lowest) average first.
    """
    rows = conn.execute(
        # Inner query: each brand's average tracked-item position within a keyword.
        # Outer query: average those per-keyword figures into one per brand, so
        # every term is weighted equally regardless of how many slots it held.
        # The JOIN to ci_brands drops the untracked "Other" bucket (brand_id NULL).
        "SELECT b.name AS brand_name, b.type AS brand_type, "
        "  AVG(per_kw.avg_pos) AS overall_avg "
        "FROM (SELECT brand_id, keyword_id, AVG(position) AS avg_pos "
        "      FROM ci_search_results WHERE group_id = ? AND run_id = ? AND " + _TRACKED_ITEMS_FILTER + " "
        "      GROUP BY brand_id, keyword_id) per_kw "
        "JOIN ci_brands b ON b.id = per_kw.brand_id "
        "GROUP BY b.id "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, overall_avg",
        (group_id, run_id, group_id),
    ).fetchall()
    return [{
        "brand_name": r["brand_name"],
        "type": r["brand_type"],
        "avg_position": round(r["overall_avg"], 1) if r["overall_avg"] is not None else None,
    } for r in rows]


def snapshot_page1_depth(conn: sqlite3.Connection, group_id: int, run_id: int) -> int:
    """Deepest page-1 slot captured in a run (the number of placements to draw).

    Sizes the "Overall Search Ranking" placement grid: rows are laid out to reach
    at least this far so a brand's average tile always lands on a real placement.
    Returns 0 when the run recorded nothing.
    """
    row = conn.execute(
        "SELECT MAX(position) FROM ci_search_results WHERE group_id = ? AND run_id = ?",
        (group_id, run_id),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _nearest_tile(avg_position: float) -> int:
    """Round an average rank to the placement tile it sits on (half-up, min 1)."""
    return max(1, int(avg_position + 0.5))


def build_rank_placement_map(avg_ranks: list[dict], depth: int, cols: int = 4) -> dict | None:
    """Lay each brand's *overall* average rank onto a page-1 placement grid.

    A pure transform (no DB) so the results page and its PDF render the exact same
    grid. ``avg_ranks`` is :func:`snapshot_brand_avg_rank`'s output; ``depth`` is
    :func:`snapshot_page1_depth`. Returns ``None`` when no brand has an average
    (nothing to plot), else::

        {"cols", "total", "rows", "marks"}

    where ``total`` is ``depth`` rounded up to a full row (so the last row isn't
    ragged) but always deep enough to show every brand, and ``marks`` maps a 1-based
    placement number to the list of brands whose average rounds onto it. The value
    is a *list* so tied brands share one tile (the split-tile case) rather than one
    silently overwriting another.
    """
    ranked = [r for r in avg_ranks if r.get("avg_position") is not None]
    if not ranked:
        return None

    tiles = [_nearest_tile(r["avg_position"]) for r in ranked]
    needed = max([depth, cols, *tiles])
    total = ((needed + cols - 1) // cols) * cols  # round up to whole rows

    marks: dict[int, list[dict]] = {}
    for r in ranked:
        pos = min(_nearest_tile(r["avg_position"]), total)
        marks.setdefault(pos, []).append({
            "brand_name": r["brand_name"],
            "type": r["type"],
            "avg_position": r["avg_position"],
        })
    return {"cols": cols, "total": total, "rows": total // cols, "marks": marks}


def snapshot_rank_by_keyword_brand(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Average page-1 ranking per brand per keyword within a run.

    One row per (keyword, brand) whose *tracked* product(s) placed on page 1;
    ``avg_ranking`` averages those tracked items' organic slots for the keyword (see
    :data:`_TRACKED_ITEMS_FILTER` for why untracked SKUs are excluded). Ordered by
    keyword, then average ranking ascending (best standing first).
    """
    rows = conn.execute(
        "SELECT k.keyword AS keyword, b.name AS brand_name, b.type AS brand_type, "
        "  AVG(sr.position) AS avg_pos "
        "FROM ci_search_results sr "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "WHERE sr.group_id = ? AND sr.run_id = ? AND sr." + _TRACKED_ITEMS_FILTER + " "
        "GROUP BY k.id, b.id "
        "ORDER BY k.keyword COLLATE NOCASE, avg_pos",
        (group_id, run_id, group_id),
    ).fetchall()
    return [{
        "keyword": r["keyword"],
        "brand_name": r["brand_name"],
        "type": r["brand_type"],
        "avg_ranking": round(r["avg_pos"], 1),
    } for r in rows]


# ── Monitoring trend series ──────────────────────────────────────────────────
# These power the trend sparklines the Daily Monitoring view adds on top of the
# snapshot layout. Each returns a dict keyed to match a snapshot row (brand names
# / keywords are unique within a group) whose value is ``{"dates": [...],
# "values": [...]}`` — one aligned point per day the entity was observed, in date
# order. The sparkline draws the values; the dates drive the hover tooltip.


def _empty_series() -> dict:
    return {"dates": [], "values": []}


def rank_trend_by_brand(conn: sqlite3.Connection, group_id: int, period: str) -> dict:
    """{brand_name: {dates, values}} of daily avg tracked-item position over the window.

    Trend for the "Overall Search Ranking" table. Per day, a brand's average
    tracked-item page-1 position (lower is better); untracked SKUs excluded.
    """
    start, end = get_date_range(period)
    rows = conn.execute(
        "SELECT sr.scraped_at AS d, b.name AS brand_name, AVG(sr.position) AS avg_pos "
        "FROM ci_search_results sr JOIN ci_brands b ON b.id = sr.brand_id "
        "WHERE sr.group_id = ? AND sr.scraped_at >= ? AND sr.scraped_at <= ? "
        "  AND sr." + _TRACKED_ITEMS_FILTER + " "
        "GROUP BY sr.scraped_at, b.id ORDER BY sr.scraped_at",
        (group_id, start, end, group_id),
    ).fetchall()
    out: dict = defaultdict(_empty_series)
    for r in rows:
        out[r["brand_name"]]["dates"].append(r["d"])
        out[r["brand_name"]]["values"].append(round(r["avg_pos"], 1))
    return dict(out)


def rank_trend_by_keyword_brand(conn: sqlite3.Connection, group_id: int, period: str) -> dict:
    """{(keyword, brand_name): {dates, values}} of daily avg position over the window.

    Trend for the per-keyword "Search Ranking" table (lower is better).
    """
    start, end = get_date_range(period)
    rows = conn.execute(
        "SELECT sr.scraped_at AS d, k.keyword AS kw, b.name AS brand_name, "
        "  AVG(sr.position) AS avg_pos "
        "FROM ci_search_results sr "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "WHERE sr.group_id = ? AND sr.scraped_at >= ? AND sr.scraped_at <= ? "
        "  AND sr." + _TRACKED_ITEMS_FILTER + " "
        "GROUP BY sr.scraped_at, k.id, b.id ORDER BY sr.scraped_at",
        (group_id, start, end, group_id),
    ).fetchall()
    out: dict = defaultdict(_empty_series)
    for r in rows:
        key = (r["kw"], r["brand_name"])
        out[key]["dates"].append(r["d"])
        out[key]["values"].append(round(r["avg_pos"], 1))
    return dict(out)


def share_trend_by_brand(conn: sqlite3.Connection, group_id: int, period: str) -> dict:
    """{brand_name: {dates, values}} of daily total-share % over the window (incl. "Other").

    Trend for the "Overall Share of Digital Shelf" table (higher is better).
    Reuses the daily share series that feeds the trend chart, keyed by name; every
    brand's series is aligned to the same window dates.
    """
    start, end = get_date_range(period)
    dates, by_brand_id = _daily_total_share_by_brand(conn, group_id, start, end)
    brand_meta = _brand_meta(conn, group_id)
    return {
        (brand_meta[bid]["name"] if bid in brand_meta else "Other"):
            {"dates": dates, "values": series}
        for bid, series in by_brand_id.items()
    }


def share_trend_by_keyword_brand(conn: sqlite3.Connection, group_id: int, period: str) -> dict:
    """{(keyword, brand_name): {dates, values}} of daily total-share % (incl. "Other").

    Trend for the per-keyword "Share of Digital Shelf" table. Each day's share is
    of that keyword's own page-1 slots that day (higher is better).
    """
    start, end = get_date_range(period)
    rows = conn.execute(
        "SELECT sos.date AS d, k.keyword AS kw, sos.brand_id AS bid, "
        "  SUM(sos.total_count) AS t "
        "FROM ci_share_of_search sos JOIN ci_keywords k ON k.id = sos.keyword_id "
        "WHERE sos.group_id = ? AND sos.date >= ? AND sos.date <= ? "
        "GROUP BY sos.date, k.id, sos.brand_id ORDER BY sos.date",
        (group_id, start, end),
    ).fetchall()

    brand_meta = _brand_meta(conn, group_id)
    # Per (date, keyword) grand total for the share denominator.
    grand: dict = defaultdict(int)
    for r in rows:
        grand[(r["d"], r["kw"])] += r["t"]

    out: dict = defaultdict(_empty_series)
    for r in rows:
        name = brand_meta[r["bid"]]["name"] if r["bid"] in brand_meta else "Other"
        key = (r["kw"], name)
        out[key]["dates"].append(r["d"])
        out[key]["values"].append(_pct(r["t"], grand[(r["d"], r["kw"])]))
    return dict(out)


def _brand_meta(conn: sqlite3.Connection, group_id: int) -> dict[int, dict]:
    """Return {brand_id: {name, type}} for a group, preserving mine-first order."""
    rows = conn.execute(
        "SELECT id, name, type FROM ci_brands WHERE group_id = ? "
        "ORDER BY CASE type WHEN 'mine' THEN 0 ELSE 1 END, name COLLATE NOCASE",
        (group_id,),
    ).fetchall()
    return {r["id"]: {"name": r["name"], "type": r["type"]} for r in rows}


# ── Search Ranking ───────────────────────────────────────────────────────────────

def rank_summary(conn: sqlite3.Connection, group_id: int, period: str) -> list[dict]:
    """Current best rank + trend per brand per keyword over the window.

    Brand-level and includes competitors so the user can compare against rivals, but
    counts only each brand's *tracked* products (see :data:`_TRACKED_ITEMS_FILTER`),
    so an untracked SKU can't stand in for the item the user follows. For each
    (brand, keyword) whose tracked item appeared in the window: the daily best
    (minimum) tracked-item position, the latest value, the prior-window best (for a
    delta), and the daily series for a sparkline. Ordered mine-first, then brand,
    then keyword.
    """
    start, end = get_date_range(period)
    p_start, p_end = get_prior_date_range(period)

    # (brand, keyword) pairs whose tracked item(s) actually appeared in the window.
    pairs = conn.execute(
        "SELECT DISTINCT b.id AS brand_id, b.name AS brand_name, b.type AS brand_type, "
        "  k.id AS keyword_id, k.keyword AS keyword "
        "FROM ci_search_results sr "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "WHERE sr.group_id = ? AND sr.scraped_at >= ? AND sr.scraped_at <= ? "
        "  AND sr." + _TRACKED_ITEMS_FILTER + " "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, "
        "  b.name COLLATE NOCASE, k.keyword COLLATE NOCASE",
        (group_id, start, end, group_id),
    ).fetchall()

    out = []
    for pr in pairs:
        series = conn.execute(
            "SELECT scraped_at, MIN(position) AS pos FROM ci_search_results "
            "WHERE group_id = ? AND brand_id = ? AND keyword_id = ? "
            "AND scraped_at >= ? AND scraped_at <= ? AND " + _TRACKED_ITEMS_FILTER + " "
            "GROUP BY scraped_at ORDER BY scraped_at",
            (group_id, pr["brand_id"], pr["keyword_id"], start, end, group_id),
        ).fetchall()
        if not series:
            continue

        dates = [r["scraped_at"] for r in series]
        positions = [r["pos"] for r in series]
        current = positions[-1]

        prior_row = conn.execute(
            "SELECT MIN(position) AS pos FROM ci_search_results "
            "WHERE group_id = ? AND brand_id = ? AND keyword_id = ? "
            "AND scraped_at >= ? AND scraped_at <= ? AND " + _TRACKED_ITEMS_FILTER,
            (group_id, pr["brand_id"], pr["keyword_id"], p_start, p_end, group_id),
        ).fetchone()
        prior = prior_row["pos"] if prior_row and prior_row["pos"] is not None else None
        # Lower position is better: positive delta = moved up.
        delta = (prior - current) if prior is not None else None

        out.append({
            "brand_name": pr["brand_name"],
            "type": pr["brand_type"],
            "keyword": pr["keyword"],
            "current_position": current,
            "prior_position": prior,
            "delta": delta,
            "dates": dates,
            "positions": positions,
        })
    return out


def rank_trend(conn: sqlite3.Connection, group_id: int, keyword_id: int, period: str) -> dict:
    """Daily best position for each of the group's my-products for one keyword.

    Shape mirrors the reference get_search_rank_trend, for a per-keyword chart.
    """
    start, end = get_date_range(period)
    products = conn.execute(
        "SELECT p.id, p.name, p.walmart_item_id FROM ci_products p "
        "JOIN ci_brands b ON b.id = p.brand_id "
        "WHERE p.group_id = ? AND p.active = 1 AND b.type = 'mine'",
        (group_id,),
    ).fetchall()

    series_out = []
    for p in products:
        rows = conn.execute(
            "SELECT scraped_at, MIN(position) AS pos FROM ci_search_results "
            "WHERE group_id = ? AND keyword_id = ? AND item_id = ? "
            "AND scraped_at >= ? AND scraped_at <= ? "
            "GROUP BY scraped_at ORDER BY scraped_at",
            (group_id, keyword_id, p["walmart_item_id"], start, end),
        ).fetchall()
        series_out.append({
            "product_id": p["id"],
            "product_name": p["name"] or p["walmart_item_id"],
            "dates": [r["scraped_at"] for r in rows],
            "positions": [r["pos"] for r in rows],
        })
    return {"products": series_out}
