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
    """Best position of each my-product per keyword within a single run (no trend).

    Used by the One-Time Snapshot results.
    """
    rows = conn.execute(
        "SELECT k.keyword AS keyword, p.name AS product_name, b.name AS brand_name, "
        "  p.walmart_item_id AS item_id, MIN(sr.position) AS position "
        "FROM ci_search_results sr "
        "JOIN ci_keywords k ON k.id = sr.keyword_id "
        "JOIN ci_products p ON p.walmart_item_id = sr.item_id AND p.group_id = sr.group_id "
        "JOIN ci_brands b ON b.id = p.brand_id "
        "WHERE sr.group_id = ? AND sr.run_id = ? AND b.type = 'mine' "
        "GROUP BY k.id, p.id "
        "ORDER BY b.name COLLATE NOCASE, p.name COLLATE NOCASE, k.keyword COLLATE NOCASE",
        (group_id, run_id),
    ).fetchall()
    return [{
        "keyword": r["keyword"],
        "product_name": r["product_name"] or r["item_id"],
        "brand_name": r["brand_name"],
        "item_id": r["item_id"],
        "current_position": r["position"],
    } for r in rows]


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
    """Current best rank + trend per (my product, keyword) over the window.

    For every "mine" product and each keyword in the group, computes the daily
    best (minimum) overall position, the latest value, the prior-window best (for
    a delta), and the full daily series for a sparkline. Rows with no sightings in
    the window are omitted. Ordered by brand, then product, then keyword.
    """
    start, end = get_date_range(period)
    p_start, p_end = get_prior_date_range(period)

    products = conn.execute(
        "SELECT p.id, p.name, p.walmart_item_id, b.name AS brand_name, b.type AS brand_type "
        "FROM ci_products p JOIN ci_brands b ON b.id = p.brand_id "
        "WHERE p.group_id = ? AND p.active = 1 AND b.type = 'mine' "
        "ORDER BY b.name COLLATE NOCASE, p.name COLLATE NOCASE",
        (group_id,),
    ).fetchall()
    keywords = conn.execute(
        "SELECT id, keyword FROM ci_keywords WHERE group_id = ? AND active = 1 "
        "ORDER BY keyword COLLATE NOCASE",
        (group_id,),
    ).fetchall()

    out = []
    for p in products:
        for kw in keywords:
            series = conn.execute(
                "SELECT scraped_at, MIN(position) AS pos FROM ci_search_results "
                "WHERE group_id = ? AND keyword_id = ? AND item_id = ? "
                "AND scraped_at >= ? AND scraped_at <= ? "
                "GROUP BY scraped_at ORDER BY scraped_at",
                (group_id, kw["id"], p["walmart_item_id"], start, end),
            ).fetchall()
            if not series:
                continue  # product never surfaced for this keyword in the window

            dates = [r["scraped_at"] for r in series]
            positions = [r["pos"] for r in series]
            current = positions[-1]

            prior_row = conn.execute(
                "SELECT MIN(position) AS pos FROM ci_search_results "
                "WHERE group_id = ? AND keyword_id = ? AND item_id = ? "
                "AND scraped_at >= ? AND scraped_at <= ?",
                (group_id, kw["id"], p["walmart_item_id"], p_start, p_end),
            ).fetchone()
            prior = prior_row["pos"] if prior_row and prior_row["pos"] is not None else None
            # Lower position is better: positive delta = moved up.
            delta = (prior - current) if prior is not None else None

            out.append({
                "product_id": p["id"],
                "product_name": p["name"] or p["walmart_item_id"],
                "brand_name": p["brand_name"],
                "item_id": p["walmart_item_id"],
                "keyword": kw["keyword"],
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
