"""Portfolio-wide performance roll-ups for the Discovery lane.

The per-item capture pipeline (:mod:`app.item_rank`) stores one daily rank +
share-of-search reading per tracked item per keyword in ``portfolio_rank_readings``.
That module's readers are single-item, calendar-bucket views. The Discovery surfaces
— the Dashboard *Discoverability* card, the Performance *Search Rank* / *Share of
Search* tabs, and the Discovery opportunities lane — instead need **cross-item**
roll-ups over an arbitrary date window, the window being whatever Dashboard *period*
the user has selected (mapped to dates by :func:`app.ci_analysis.period_bounds`).

Separation of concerns: the route layer owns the period→window mapping and the
delta/trend loop (it calls these functions once per period, mirroring the Content
Readiness tab's ``_readiness_trend``); this module stays period-agnostic and simply
aggregates one ``[start, end]`` window.

Every read is ``user_id``-scoped and only ever counts tracked items
(``portfolio_items.tracked = 1``); all SQL is parameterized.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)


def _pct(item_slots: int | None, total_slots: int | None) -> int | None:
    """Share of search as a 0–100 percent, or ``None`` when the shelf was never
    measured. Shares sum the raw slot counts over the window (not a mean of daily
    percentages), matching :func:`app.item_rank._aggregate`."""
    return round(100 * item_slots / total_slots) if total_slots else None


def _brand_filter(brands: list[str] | None) -> tuple[str, list]:
    """Build an optional ``AND p.brand IN (...)`` fragment + its params.

    Discovery is scoped to the Dashboard's selected brand by passing that group's
    "mine" brand names; ``None``/empty means every tracked item (brand-agnostic).
    """
    if not brands:
        return "", []
    placeholders = ",".join("?" for _ in brands)
    return f" AND p.brand IN ({placeholders})", list(brands)


def portfolio_rank_summary(conn: sqlite3.Connection, user_id: int,
                           start: str, end: str,
                           brands: list[str] | None = None) -> dict:
    """Window roll-up of rank + share-of-search across the user's tracked items.

    Returns the Discoverability headline numbers for one ``[start, end]`` window:

    * ``avg_rank`` — mean page-1 position over every tracked item×keyword reading in
      the window (``None`` if nothing ranked); lower is better.
    * ``share`` — tracked slots ÷ all page-1 slots over the window, 0–100 percent
      (``None`` if the shelf was never measured).
    * ``items_total`` / ``items_ranked`` — tracked items with any reading / with at
      least one page-1 slot in the window.
    * ``keywords_total`` / ``keywords_no_rank`` — (item, keyword) pairs measured /
      never on page 1 in the window (feeds the card narrative + Discovery opportunity).
    * ``n`` — reading count (``0`` ⇒ nothing collected yet, a "gathering" state).
    """
    frag, bparams = _brand_filter(brands)
    # One grouped read per (item, keyword) pair; item- and portfolio-level figures
    # are derived in Python so share stays a sum-of-slots ratio, never an avg-of-avgs.
    rows = conn.execute(
        "SELECT r.portfolio_item_id AS item, "
        "       SUM(r.item_slots) AS item_slots, "
        "       SUM(r.total_slots) AS total_slots, "
        "       SUM(CASE WHEN r.rank IS NOT NULL THEN r.rank ELSE 0 END) AS rank_sum, "
        "       COUNT(r.rank) AS rank_n, "   # COUNT(col) ignores NULLs → page-1 hits
        "       COUNT(*) AS n "
        "FROM portfolio_rank_readings r "
        "JOIN portfolio_items p ON p.id = r.portfolio_item_id "
        "WHERE r.user_id = ? AND p.tracked = 1 "
        "  AND r.captured_at BETWEEN ? AND ?" + frag + " "
        "GROUP BY r.portfolio_item_id, r.keyword_id",
        [user_id, start, end] + bparams,
    ).fetchall()

    rank_sum = sum(r["rank_sum"] for r in rows)
    rank_n = sum(r["rank_n"] for r in rows)
    item_slots = sum(r["item_slots"] for r in rows)
    total_slots = sum(r["total_slots"] for r in rows)
    items_total = len({r["item"] for r in rows})
    items_ranked = len({r["item"] for r in rows if r["rank_n"]})
    keywords_total = len(rows)
    keywords_no_rank = sum(1 for r in rows if not r["rank_n"])

    summary = {
        "avg_rank": round(rank_sum / rank_n, 1) if rank_n else None,
        "share": _pct(item_slots, total_slots),
        "items_total": items_total,
        "items_ranked": items_ranked,
        "keywords_total": keywords_total,
        "keywords_no_rank": keywords_no_rank,
        "n": sum(r["n"] for r in rows),
    }
    logger.debug("Portfolio rank summary user_id=%s window=%s..%s brands=%s n=%s avg_rank=%s share=%s",
                 user_id, start, end, brands, summary["n"], summary["avg_rank"], summary["share"])
    return summary


def tracked_items_with_readings(conn: sqlite3.Connection, user_id: int,
                                start: str, end: str,
                                brands: list[str] | None = None) -> list[dict]:
    """One row per tracked item with its window rank + share (the all-items table).

    LEFT JOIN so a tracked item with no readings in the window still appears (the
    page shows it as "collecting" / "not ranked"), ordered newest-first like the
    Portfolio list. ``name`` prefers the backfilled title, falling back to the item #.
    """
    frag, bparams = _brand_filter(brands)
    rows = conn.execute(
        "SELECT p.id AS id, p.item_id AS item_id, p.title AS title, p.brand AS brand, "
        "       AVG(r.rank) AS avg_rank, "
        "       SUM(r.item_slots) AS item_slots, SUM(r.total_slots) AS total_slots, "
        "       COUNT(r.id) AS n "
        "FROM portfolio_items p "
        "LEFT JOIN portfolio_rank_readings r "
        "  ON r.portfolio_item_id = p.id AND r.user_id = p.user_id "
        "  AND r.captured_at BETWEEN ? AND ? "
        "WHERE p.user_id = ? AND p.tracked = 1" + frag + " "
        "GROUP BY p.id ORDER BY p.id DESC",
        [start, end, user_id] + bparams,
    ).fetchall()
    return [{
        "id": r["id"],
        "item_id": r["item_id"],
        "name": r["title"] or r["item_id"],
        "brand": r["brand"],
        "avg_rank": round(r["avg_rank"], 1) if r["avg_rank"] is not None else None,
        "share": _pct(r["item_slots"], r["total_slots"]),
        "n": r["n"],
    } for r in rows]


def item_window_summary(conn: sqlite3.Connection, user_id: int, item_pk: int,
                        start: str, end: str) -> dict:
    """One tracked item's window rank + share (the drill-down headline numbers).

    User-scoped (IDOR) via the ``portfolio_items`` join, so a forged/foreign item id
    yields an empty window (``n = 0``) rather than another user's data.
    """
    row = conn.execute(
        "SELECT AVG(r.rank) AS avg_rank, SUM(r.item_slots) AS item_slots, "
        "       SUM(r.total_slots) AS total_slots, COUNT(r.id) AS n "
        "FROM portfolio_rank_readings r "
        "JOIN portfolio_items p ON p.id = r.portfolio_item_id "
        "WHERE r.user_id = ? AND p.id = ? AND p.tracked = 1 "
        "  AND r.captured_at BETWEEN ? AND ?",
        (user_id, item_pk, start, end),
    ).fetchone()
    return {
        "avg_rank": round(row["avg_rank"], 1) if row["avg_rank"] is not None else None,
        "share": _pct(row["item_slots"], row["total_slots"]),
        "n": row["n"] or 0,
    }


def item_keyword_readings(conn: sqlite3.Connection, user_id: int, item_pk: int,
                          start: str, end: str) -> list[dict]:
    """Per-keyword rank + share for ONE tracked item over the window (drill table).

    User-scoped (IDOR) through the ``portfolio_items`` join. Lists the item's
    non-blank keyword slots in slot order; a slot with no readings in the window
    comes back with ``avg_rank``/``share`` = ``None`` ("collecting").
    """
    rows = conn.execute(
        "SELECT k.id AS keyword_id, k.slot AS slot, k.kind AS kind, k.keyword AS keyword, "
        "       AVG(r.rank) AS avg_rank, "
        "       SUM(r.item_slots) AS item_slots, SUM(r.total_slots) AS total_slots, "
        "       COUNT(r.id) AS n "
        "FROM portfolio_keywords k "
        "JOIN portfolio_items p ON p.id = k.portfolio_item_id "
        "LEFT JOIN portfolio_rank_readings r "
        "  ON r.keyword_id = k.id AND r.user_id = k.user_id "
        "  AND r.captured_at BETWEEN ? AND ? "
        "WHERE k.portfolio_item_id = ? AND p.user_id = ? "
        "  AND k.keyword IS NOT NULL AND TRIM(k.keyword) <> '' "
        "GROUP BY k.id ORDER BY k.slot",
        [start, end, item_pk, user_id],
    ).fetchall()
    return [{
        "keyword_id": r["keyword_id"],
        "slot": r["slot"],
        "kind": r["kind"],
        "keyword": r["keyword"],
        "avg_rank": round(r["avg_rank"], 1) if r["avg_rank"] is not None else None,
        "share": _pct(r["item_slots"], r["total_slots"]),
        "n": r["n"],
    } for r in rows]


def item_keyword_window_ranks(conn: sqlite3.Connection, user_id: int, item_pk: int,
                              start: str, end: str) -> dict[int, float | None]:
    """``{keyword_id: avg_rank}`` for one item's non-blank keywords over a window.

    The per-period building block for the drill's per-keyword rank trends: called
    once per trend period so each keyword gets its own series. User-scoped (IDOR)
    through the ``portfolio_items`` join, so a forged item id yields no rows.
    """
    rows = conn.execute(
        "SELECT k.id AS keyword_id, AVG(r.rank) AS avg_rank "
        "FROM portfolio_keywords k "
        "JOIN portfolio_items p ON p.id = k.portfolio_item_id "
        "LEFT JOIN portfolio_rank_readings r "
        "  ON r.keyword_id = k.id AND r.user_id = k.user_id "
        "  AND r.captured_at BETWEEN ? AND ? "
        "WHERE k.portfolio_item_id = ? AND p.user_id = ? AND p.tracked = 1 "
        "  AND k.keyword IS NOT NULL AND TRIM(k.keyword) <> '' "
        "GROUP BY k.id",
        [start, end, item_pk, user_id],
    ).fetchall()
    return {r["keyword_id"]: (round(r["avg_rank"], 1) if r["avg_rank"] is not None else None)
            for r in rows}
