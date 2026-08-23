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
MONITORING_HOURS = (7, 15, 23)  # 7 AM, 3 PM, 11 PM CST


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
        })

    def _sort_key(r):
        # mine (0) before competitor (1) before other (2); then higher share first.
        rank = {"mine": 0, "competitor": 1}.get(r["type"], 2)
        return (rank, -r["total_share"])

    out.sort(key=_sort_key)
    return out


def share_of_shelf_trend(conn: sqlite3.Connection, group_id: int, period: str) -> dict:
    """Total-share-% per brand per date over the window (for the trend chart).

    Returns ``{"dates": [...], "brands": [{brand_id, brand_name, type, share: [...]}]}``
    where each share value is that brand's percentage of all slots on that date.
    """
    start, end = get_date_range(period)
    rows = conn.execute(
        "SELECT date, brand_id, SUM(total_count) AS total "
        "FROM ci_share_of_search WHERE group_id = ? AND date >= ? AND date <= ? "
        "GROUP BY date, brand_id ORDER BY date",
        (group_id, start, end),
    ).fetchall()

    dates = sorted({r["date"] for r in rows})
    # date -> grand total (for share %), and (brand_id, date) -> total.
    grand_by_date: dict = defaultdict(int)
    by_brand_date: dict = defaultdict(dict)
    seen_brand_ids: list = []
    for r in rows:
        grand_by_date[r["date"]] += r["total"]
        by_brand_date[r["brand_id"]][r["date"]] = r["total"]
        if r["brand_id"] not in seen_brand_ids:
            seen_brand_ids.append(r["brand_id"])

    brand_meta = _brand_meta(conn, group_id)
    brands_out = []
    # Group brands (in meta order) first, then Other (None) if present.
    ordered_ids = [bid for bid in brand_meta if bid in seen_brand_ids]
    if None in seen_brand_ids:
        ordered_ids.append(None)
    for brand_id in ordered_ids:
        meta = brand_meta.get(brand_id)
        series = [_pct(by_brand_date[brand_id].get(d, 0), grand_by_date[d]) for d in dates]
        brands_out.append({
            "brand_id": brand_id,
            "brand_name": meta["name"] if meta else "Other",
            "type": meta["type"] if meta else "other",
            "share": series,
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
    """Average best page-1 position per brand across the run's keywords.

    The headline "how am I doing overall" figure: reduce each brand to its best
    (lowest) slot per keyword, then average those bests into one number per brand
    (mine + competitors). The average is taken only over keywords the brand
    actually placed for — an absent keyword isn't counted as a penalty — so
    ``keyword_count`` is returned to show how many terms back the average.
    Ordered mine-first, then best (lowest) average first.
    """
    rows = conn.execute(
        # Inner query: each brand's best slot per keyword in this run.
        # Outer query: average those per-keyword bests into one figure per brand.
        # The JOIN to ci_brands drops the untracked "Other" bucket (brand_id NULL).
        "SELECT b.name AS brand_name, b.type AS brand_type, "
        "  AVG(per_kw.best) AS avg_best, COUNT(*) AS keyword_count "
        "FROM (SELECT brand_id, keyword_id, MIN(position) AS best "
        "      FROM ci_search_results WHERE group_id = ? AND run_id = ? "
        "      GROUP BY brand_id, keyword_id) per_kw "
        "JOIN ci_brands b ON b.id = per_kw.brand_id "
        "GROUP BY b.id "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, avg_best",
        (group_id, run_id),
    ).fetchall()
    return [{
        "brand_name": r["brand_name"],
        "type": r["brand_type"],
        "avg_position": round(r["avg_best"], 1) if r["avg_best"] is not None else None,
        "keyword_count": r["keyword_count"],
    } for r in rows]


def snapshot_rank_by_term(conn: sqlite3.Connection, group_id: int, run_id: int) -> list[dict]:
    """Per keyword, the best slot for *my* brands vs *competitor* brands in a run.

    Condenses the brand-level detail into a head-to-head: for each keyword, the
    single best (lowest) position across all my brands and across all competitor
    brands, each tagged with the slot type (organic/sponsored) that achieved it.
    Untracked-brand rows (brand_id NULL) are excluded — this compares tracked
    mine-vs-competitor standings only. ``*_position`` is None when that side held
    no slot for the keyword.
    """
    rows = conn.execute(
        "SELECT k.keyword AS keyword, "
        "  CASE b.type WHEN 'mine' THEN 'mine' ELSE 'competitor' END AS side, "
        "  sr.position AS position, sr.position_type AS position_type "
        "FROM ci_search_results sr "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "WHERE sr.group_id = ? AND sr.run_id = ?",
        (group_id, run_id),
    ).fetchall()

    # keyword -> side -> (best_position, type_at_best). Reduce to the min position
    # per side in Python (result sets are small — page-1 only, few keywords/brands).
    best: dict[str, dict[str, tuple[int, str]]] = {}
    order: list[str] = []  # preserve first-seen keyword order before the final sort
    for r in rows:
        kw = r["keyword"]
        if kw not in best:
            best[kw] = {}
            order.append(kw)
        held = best[kw].get(r["side"])
        if held is None or r["position"] < held[0]:
            best[kw][r["side"]] = (r["position"], r["position_type"])

    out = []
    for kw in sorted(order, key=str.lower):
        mine = best[kw].get("mine")
        comp = best[kw].get("competitor")
        out.append({
            "keyword": kw,
            "mine_position": mine[0] if mine else None,
            "mine_type": mine[1] if mine else None,
            "competitor_position": comp[0] if comp else None,
            "competitor_type": comp[1] if comp else None,
        })
    return out


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

    Brand-level and includes competitors so the user can compare against rivals.
    For each (brand, keyword) seen in the window: the daily best (minimum) position,
    the latest value, the prior-window best (for a delta), and the daily series for
    a sparkline. Attribution is by brand_id (set via name matching), so sponsored
    slots count. Ordered mine-first, then brand, then keyword.
    """
    start, end = get_date_range(period)
    p_start, p_end = get_prior_date_range(period)

    # (brand, keyword) pairs that actually appeared in the window.
    pairs = conn.execute(
        "SELECT DISTINCT b.id AS brand_id, b.name AS brand_name, b.type AS brand_type, "
        "  k.id AS keyword_id, k.keyword AS keyword "
        "FROM ci_search_results sr "
        "JOIN ci_brands b ON b.id = sr.brand_id "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "WHERE sr.group_id = ? AND sr.scraped_at >= ? AND sr.scraped_at <= ? "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, "
        "  b.name COLLATE NOCASE, k.keyword COLLATE NOCASE",
        (group_id, start, end),
    ).fetchall()

    out = []
    for pr in pairs:
        series = conn.execute(
            "SELECT scraped_at, MIN(position) AS pos FROM ci_search_results "
            "WHERE group_id = ? AND brand_id = ? AND keyword_id = ? "
            "AND scraped_at >= ? AND scraped_at <= ? "
            "GROUP BY scraped_at ORDER BY scraped_at",
            (group_id, pr["brand_id"], pr["keyword_id"], start, end),
        ).fetchall()
        if not series:
            continue

        dates = [r["scraped_at"] for r in series]
        positions = [r["pos"] for r in series]
        current = positions[-1]

        prior_row = conn.execute(
            "SELECT MIN(position) AS pos FROM ci_search_results "
            "WHERE group_id = ? AND brand_id = ? AND keyword_id = ? "
            "AND scraped_at >= ? AND scraped_at <= ?",
            (group_id, pr["brand_id"], pr["keyword_id"], p_start, p_end),
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
