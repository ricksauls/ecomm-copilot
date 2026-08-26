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

# Reporting periods for the Daily Monitoring view. Each aggregates over the last
# *completed* calendar period and compares to the one before (week-over-week, …).
# Weeks run Monday–Sunday; months/quarters/years are calendar.
PERIODS = ("wow", "mom", "qoq", "yoy")
PERIOD_LABELS = {"wow": "Week", "mom": "Month", "qoq": "Quarter", "yoy": "Year"}
DEFAULT_PERIOD = "wow"
# How many completed periods the per-row trend sparkline plots (one point each).
TREND_PERIODS = 6

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


def _pct(part: int, whole: int) -> float:
    """Percentage of ``part`` in ``whole``, rounded to one decimal (0 if whole=0)."""
    return round(100.0 * part / whole, 1) if whole else 0.0


# ── Share of Digital Shelf ───────────────────────────────────────────────────────

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


def _shape_share_rows(counts: dict[int | None, dict], brand_meta: dict) -> list[dict]:
    """Turn ``{brand_id: {organic,sponsored,total}}`` into sorted share-of-shelf rows.

    Denominators are the grand totals across all brands, so each share is of the
    whole page-1 shelf. A ``None`` brand_id (a placement attributed to no tracked
    brand) becomes the "Other" bucket. Ordered mine-first, then competitors, then
    Other, and within a tier by total share descending. Shared by every share
    function (tracked-only and brand-level, snapshot and range) so their shaping
    can't drift.
    """
    grand = {k: sum(b[k] for b in counts.values()) for k in ("organic", "sponsored", "total")}
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


def _brand_level_counts(conn: sqlite3.Connection, where: str, params: tuple) -> dict[int | None, dict]:
    """Count each brand's organic/sponsored/total page-1 cards straight from
    ``ci_search_results`` (so a brand's *untracked* SKUs count too, unlike the
    tracked-only ``ci_share_of_search`` rollup). ``where`` is the row filter after
    ``WHERE`` and ``params`` its bound values.
    """
    rows = conn.execute(
        "SELECT brand_id, "
        "  SUM(CASE WHEN position_type = 'organic' THEN 1 ELSE 0 END) AS o, "
        "  SUM(CASE WHEN position_type = 'sponsored' THEN 1 ELSE 0 END) AS s, "
        "  COUNT(*) AS t "
        "FROM ci_search_results WHERE " + where + " GROUP BY brand_id",
        params,
    ).fetchall()
    return {r["brand_id"]: {"organic": r["o"] or 0, "sponsored": r["s"] or 0, "total": r["t"] or 0}
            for r in rows}


def snapshot_share_of_shelf(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Per-brand share of shelf for a single run (current-state, no deltas/trends).

    Tracked-item-only (reads the ``ci_share_of_search`` rollup): a brand's share is
    its tracked SKUs' slots ÷ all placements. Used by the One-Time Snapshot results —
    one run, so trend/delta are meaningless. See :func:`snapshot_brand_share_of_shelf`
    for the whole-brand counterpart.
    """
    rows = conn.execute(
        "SELECT brand_id, SUM(organic_count) AS o, SUM(sponsored_count) AS s, "
        "SUM(total_count) AS t FROM ci_share_of_search "
        "WHERE group_id = ? AND run_id = ? GROUP BY brand_id",
        (group_id, run_id),
    ).fetchall()
    counts = {r["brand_id"]: {"organic": r["o"] or 0, "sponsored": r["s"] or 0,
                              "total": r["t"] or 0} for r in rows}
    return _shape_share_rows(counts, _brand_meta(conn, group_id))


def snapshot_brand_share_of_shelf(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Brand-level share of shelf for a single run: *all* of a brand's page-1 cards
    (tracked + untracked) ÷ total placements.

    The whole-brand counterpart to :func:`snapshot_share_of_shelf` (which counts
    only tracked SKUs). Reads ``ci_search_results`` directly — every card already
    carries a ``brand_id`` (untracked SKUs are brand-attributed by name in the
    scraper), and a card matched to no tracked brand falls into the "Other" bucket,
    so the denominator stays the whole page-1 shelf.
    """
    counts = _brand_level_counts(conn, "group_id = ? AND run_id = ?", (group_id, run_id))
    return _shape_share_rows(counts, _brand_meta(conn, group_id))


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


def _brand_meta(conn: sqlite3.Connection, group_id: int) -> dict[int, dict]:
    """Return {brand_id: {name, type}} for a group, preserving mine-first order."""
    rows = conn.execute(
        "SELECT id, name, type FROM ci_brands WHERE group_id = ? "
        "ORDER BY CASE type WHEN 'mine' THEN 0 ELSE 1 END, name COLLATE NOCASE",
        (group_id,),
    ).fetchall()
    return {r["id"]: {"name": r["name"], "type": r["type"]} for r in rows}


# ── Calendar reporting periods (Daily Monitoring) ────────────────────────────
# The monitoring view aggregates each results table over the last *completed*
# calendar period and compares it to the one before it. A period's button stays
# off until its most recent completed period actually holds scraped data.

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """Shift a (year, month) pair by ``delta`` months (month is 1-12)."""
    total = year * 12 + (month - 1) + delta
    return total // 12, total % 12 + 1


def _month_end(year: int, month: int) -> date:
    """Last calendar day of a given year/month."""
    ny, nm = _add_months(year, month, 1)
    return date(ny, nm, 1) - timedelta(days=1)


def period_bounds(period: str, index: int = 0, today: date | None = None) -> tuple[str, str]:
    """(start, end) ISO dates for a *completed* calendar period.

    ``index`` counts back from the most recently completed period: 0 is the last
    completed period, 1 the one before it, and so on. Weeks are Monday–Sunday;
    months, quarters and years are calendar (never the in-progress current one).
    """
    today = today or date.today()
    if period == "wow":
        this_monday = today - timedelta(days=today.weekday())
        end = this_monday - timedelta(days=1 + 7 * index)  # Sunday of the target week
        start = end - timedelta(days=6)
    elif period == "mom":
        y, m = _add_months(today.year, today.month, -(index + 1))
        start, end = date(y, m, 1), _month_end(y, m)
    elif period == "qoq":
        q_first_month = ((today.month - 1) // 3) * 3 + 1
        y, m = _add_months(today.year, q_first_month, -3 * (index + 1))
        ey, em = _add_months(y, m, 2)
        start, end = date(y, m, 1), _month_end(ey, em)
    elif period == "yoy":
        y = today.year - 1 - index
        start, end = date(y, 1, 1), date(y, 12, 31)
    else:
        raise ValueError(f"Unknown period {period!r}")
    return start.isoformat(), end.isoformat()


def period_label(period: str, index: int = 0, today: date | None = None) -> str:
    """Human label for a completed period ('Aug 17–23', 'Jul 2026', 'Q2 2026', '2025')."""
    s = date.fromisoformat(period_bounds(period, index, today)[0])
    e = date.fromisoformat(period_bounds(period, index, today)[1])
    if period == "wow":
        if s.month == e.month:
            return f"{_MONTHS[s.month - 1]} {s.day}–{e.day}"
        return f"{_MONTHS[s.month - 1]} {s.day}–{_MONTHS[e.month - 1]} {e.day}"
    if period == "mom":
        return f"{_MONTHS[s.month - 1]} {s.year}"
    if period == "qoq":
        return f"Q{(s.month - 1) // 3 + 1} {s.year}"
    return str(s.year)  # yoy


def period_has_data(conn: sqlite3.Connection, group_id: int, period: str,
                    index: int = 0, today: date | None = None) -> bool:
    """Whether the given completed period holds any scraped data for the group."""
    start, end = period_bounds(period, index, today)
    for table, col in (("ci_search_results", "scraped_at"), ("ci_share_of_search", "date")):
        row = conn.execute(
            f"SELECT 1 FROM {table} WHERE group_id = ? AND {col} >= ? AND {col} <= ? LIMIT 1",
            (group_id, start, end),
        ).fetchone()
        if row:
            return True
    return False


def available_periods(conn: sqlite3.Connection, group_id: int,
                      today: date | None = None) -> list[str]:
    """Period keys whose most recent completed period has data (the enabled buttons)."""
    return [p for p in PERIODS if period_has_data(conn, group_id, p, 0, today)]


# ── Period-range aggregations (mirror the run-scoped snapshot_* by date) ──────

def _avg_rank_by_brand_range(conn, group_id, start, end) -> list[dict]:
    """Two-stage average tracked-item page-1 position per brand over a date range."""
    rows = conn.execute(
        "SELECT b.name AS brand_name, b.type AS brand_type, AVG(per_kw.avg_pos) AS overall_avg "
        "FROM (SELECT brand_id, keyword_id, AVG(position) AS avg_pos FROM ci_search_results "
        "      WHERE group_id = ? AND scraped_at >= ? AND scraped_at <= ? AND " + _TRACKED_ITEMS_FILTER + " "
        "      GROUP BY brand_id, keyword_id) per_kw "
        "JOIN ci_brands b ON b.id = per_kw.brand_id GROUP BY b.id "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, overall_avg",
        (group_id, start, end, group_id),
    ).fetchall()
    return [{"brand_name": r["brand_name"], "type": r["brand_type"],
             "avg_position": round(r["overall_avg"], 1) if r["overall_avg"] is not None else None}
            for r in rows]


def _rank_by_keyword_brand_range(conn, group_id, start, end) -> list[dict]:
    """Average tracked-item page-1 position per (keyword, brand) over a date range."""
    rows = conn.execute(
        "SELECT k.keyword AS keyword, b.name AS brand_name, b.type AS brand_type, "
        "  AVG(sr.position) AS avg_pos "
        "FROM ci_search_results sr JOIN ci_keywords k ON k.id = sr.keyword_id "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "WHERE sr.group_id = ? AND sr.scraped_at >= ? AND sr.scraped_at <= ? AND sr." + _TRACKED_ITEMS_FILTER + " "
        "GROUP BY k.id, b.id ORDER BY k.keyword COLLATE NOCASE, avg_pos",
        (group_id, start, end, group_id),
    ).fetchall()
    return [{"keyword": r["keyword"], "brand_name": r["brand_name"], "type": r["brand_type"],
             "avg_ranking": round(r["avg_pos"], 1)} for r in rows]


def _share_of_shelf_range(conn, group_id, start, end) -> list[dict]:
    """Per-brand tracked-item-only share of shelf over a date range."""
    counts = _sos_totals_by_brand(conn, group_id, start, end)
    return _shape_share_rows(counts, _brand_meta(conn, group_id))


def _brand_share_of_shelf_range(conn, group_id, start, end) -> list[dict]:
    """Brand-level share of shelf (all of a brand's page-1 cards) over a date range."""
    counts = _brand_level_counts(
        conn, "group_id = ? AND scraped_at >= ? AND scraped_at <= ?", (group_id, start, end))
    return _shape_share_rows(counts, _brand_meta(conn, group_id))


def _share_by_keyword_range(conn, group_id, start, end) -> list[dict]:
    """Per-keyword, per-brand share of that keyword's own slots over a date range."""
    rows = conn.execute(
        "SELECT k.keyword AS keyword, sos.brand_id AS brand_id, "
        "  SUM(sos.organic_count) AS o, SUM(sos.sponsored_count) AS s, SUM(sos.total_count) AS t "
        "FROM ci_share_of_search sos JOIN ci_keywords k ON k.id = sos.keyword_id "
        "WHERE sos.group_id = ? AND sos.date >= ? AND sos.date <= ? GROUP BY k.id, sos.brand_id",
        (group_id, start, end),
    ).fetchall()
    brand_meta = _brand_meta(conn, group_id)
    by_kw: dict = defaultdict(list)
    for r in rows:
        by_kw[r["keyword"]].append(r)
    out = []
    for kw in sorted(by_kw, key=str.lower):
        krows = by_kw[kw]
        grand = {key: sum((r[col] or 0) for r in krows)
                 for key, col in (("organic", "o"), ("sponsored", "s"), ("total", "t"))}
        entries = [{
            "keyword": kw,
            "brand_name": (brand_meta.get(r["brand_id"]) or {}).get("name", "Other"),
            "type": (brand_meta.get(r["brand_id"]) or {}).get("type", "other"),
            "organic_share": _pct(r["o"] or 0, grand["organic"]),
            "sponsored_share": _pct(r["s"] or 0, grand["sponsored"]),
            "total_share": _pct(r["t"] or 0, grand["total"]),
        } for r in krows]
        entries.sort(key=lambda e: -e["total_share"])
        out.extend(entries)
    return out


def _page1_depth_range(conn, group_id, start, end) -> int:
    """Deepest page-1 slot recorded over a date range (sizes the placement grid)."""
    row = conn.execute(
        "SELECT MAX(position) FROM ci_search_results "
        "WHERE group_id = ? AND scraped_at >= ? AND scraped_at <= ?",
        (group_id, start, end),
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ── Period-over-period assemblers (current + delta + trend) ──────────────────

def _period_trend(conn, group_id, period, today, *, key_fn, val_fn, range_fn) -> dict:
    """Per-entity {dates(labels), values} across the last ``TREND_PERIODS`` periods.

    One aggregated point per completed period (oldest→newest), skipping periods
    where the entity had no value. ``dates`` carry the period labels the tooltip
    shows; ``values`` are what the sparkline draws.
    """
    series: dict = defaultdict(lambda: {"dates": [], "values": []})
    for i in range(TREND_PERIODS - 1, -1, -1):
        start, end = period_bounds(period, i, today)
        label = period_label(period, i, today)
        for r in range_fn(conn, group_id, start, end):
            v = val_fn(r)
            if v is None:
                continue
            series[key_fn(r)]["dates"].append(label)
            series[key_fn(r)]["values"].append(v)
    return series


def monitoring_avg_rank(conn, group_id, period, today=None) -> list[dict]:
    """Overall Search Ranking for the last completed period, with delta + trend.

    Delta is prior-average minus current-average, so a positive delta means the
    brand's average ranking *improved* (moved up).
    """
    cur = _avg_rank_by_brand_range(conn, group_id, *period_bounds(period, 0, today))
    prior = {r["brand_name"]: r for r in _avg_rank_by_brand_range(conn, group_id, *period_bounds(period, 1, today))}
    trend = _period_trend(conn, group_id, period, today, key_fn=lambda r: r["brand_name"],
                          val_fn=lambda r: r["avg_position"], range_fn=_avg_rank_by_brand_range)
    out = []
    for r in cur:
        p = prior.get(r["brand_name"])
        delta = (round(p["avg_position"] - r["avg_position"], 1)
                 if p and p["avg_position"] is not None and r["avg_position"] is not None else None)
        t = trend.get(r["brand_name"], {"dates": [], "values": []})
        out.append({**r, "delta": delta, "trend": t["values"], "trend_dates": t["dates"]})
    return out


def monitoring_rank_by_keyword(conn, group_id, period, today=None) -> list[dict]:
    """Per-keyword Search Ranking for the last completed period, with delta + trend."""
    cur = _rank_by_keyword_brand_range(conn, group_id, *period_bounds(period, 0, today))
    prior = {(r["keyword"], r["brand_name"]): r
             for r in _rank_by_keyword_brand_range(conn, group_id, *period_bounds(period, 1, today))}
    trend = _period_trend(conn, group_id, period, today,
                          key_fn=lambda r: (r["keyword"], r["brand_name"]),
                          val_fn=lambda r: r["avg_ranking"], range_fn=_rank_by_keyword_brand_range)
    out = []
    for r in cur:
        key = (r["keyword"], r["brand_name"])
        p = prior.get(key)
        delta = round(p["avg_ranking"] - r["avg_ranking"], 1) if p else None
        t = trend.get(key, {"dates": [], "values": []})
        out.append({**r, "delta": delta, "trend": t["values"], "trend_dates": t["dates"]})
    return out


def _assemble_share_over_period(conn, group_id, period, today, range_fn) -> list[dict]:
    """Per-brand share for the last completed period with delta + trend, over any
    share ``range_fn`` (tracked-only or brand-level). Delta is current minus prior
    total-share (percentage points; positive = gained).
    """
    cur = range_fn(conn, group_id, *period_bounds(period, 0, today))
    prior = {r["brand_name"]: r for r in range_fn(conn, group_id, *period_bounds(period, 1, today))}
    trend = _period_trend(conn, group_id, period, today, key_fn=lambda r: r["brand_name"],
                          val_fn=lambda r: r["total_share"], range_fn=range_fn)
    out = []
    for r in cur:
        p = prior.get(r["brand_name"])
        delta = round(r["total_share"] - p["total_share"], 1) if p else None
        t = trend.get(r["brand_name"], {"dates": [], "values": []})
        out.append({**r, "delta": delta, "trend": t["values"], "trend_dates": t["dates"]})
    return out


def monitoring_share_of_shelf(conn, group_id, period, today=None) -> list[dict]:
    """Overall (tracked-item-only) Share of Digital Shelf for the last completed
    period, with delta + trend."""
    return _assemble_share_over_period(conn, group_id, period, today, _share_of_shelf_range)


def monitoring_brand_share_of_shelf(conn, group_id, period, today=None) -> list[dict]:
    """Brand-level Share of Digital Shelf (all of a brand's cards) for the last
    completed period, with delta + trend. Whole-brand counterpart to
    :func:`monitoring_share_of_shelf`."""
    return _assemble_share_over_period(conn, group_id, period, today, _brand_share_of_shelf_range)


def monitoring_share_by_keyword(conn, group_id, period, today=None) -> list[dict]:
    """Per-keyword Share of Digital Shelf for the last completed period, with delta + trend."""
    cur = _share_by_keyword_range(conn, group_id, *period_bounds(period, 0, today))
    prior = {(r["keyword"], r["brand_name"]): r
             for r in _share_by_keyword_range(conn, group_id, *period_bounds(period, 1, today))}
    trend = _period_trend(conn, group_id, period, today,
                          key_fn=lambda r: (r["keyword"], r["brand_name"]),
                          val_fn=lambda r: r["total_share"], range_fn=_share_by_keyword_range)
    out = []
    for r in cur:
        key = (r["keyword"], r["brand_name"])
        p = prior.get(key)
        delta = round(r["total_share"] - p["total_share"], 1) if p else None
        t = trend.get(key, {"dates": [], "values": []})
        out.append({**r, "delta": delta, "trend": t["values"], "trend_dates": t["dates"]})
    return out


def monitoring_placement_map(conn, group_id, period, avg_ranks, today=None) -> dict | None:
    """Placement grid for the period's overall average ranking (page + PDF share it)."""
    start, end = period_bounds(period, 0, today)
    return build_rank_placement_map(avg_ranks, _page1_depth_range(conn, group_id, start, end))
