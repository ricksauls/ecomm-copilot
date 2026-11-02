"""Per-item tracking keywords for portfolio products.

Every tracked portfolio item carries up to five keywords that the daily search
sweep (:mod:`app.item_rank`) scrapes for rank + share of search. The keywords are
the real shopper **search terms** the item is measured against; they're AI-chosen
(see :mod:`app.keyword_ai`) from the product's brand/title/size/category by the
descriptors backfill (:func:`app.portfolio.save_descriptors`).

Until the backfill runs — or if AI selection is disabled or errors — the slots
fall back to best-effort mechanical defaults, which is why slots 1-3 still carry
the historical ``kind`` tags:

1. **brand** — the item's brand (e.g. ``Tabasco``).
2. **brand_product_size** — brand + product name + size (e.g. ``Tabasco Original
   Hot Sauce 12 oz``).
3. **category** — the category / shopper intent (e.g. ``Hot Sauce``).
4-5. **search** — extra slots, blank in the mechanical fallback, filled by AI.

The ``kind`` column is now vestigial (the UI no longer labels chips by kind); it
is kept only so the fallback can compose sensible slot-1-3 defaults. The user can
edit any slot; an edited row (``user_edited = 1``) is never overwritten by the
backfill or the AI step.

This module owns the ``portfolio_keywords`` table only. Every request-reachable
read/write is scoped by ``user_id`` (the IDOR boundary); all SQL is
parameterized. Functions take an explicit ``sqlite3.Connection`` so they work in a
Flask request (:func:`app.db.get_db`) and in the worker/enqueuer processes alike.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# One keyword can't exceed this many characters — a belt-and-braces cap matching
# the single-line text inputs in the editor, so a pasted blob can't bloat a row or
# the SERP query. Blank is allowed and stored as NULL ("don't scrape this slot").
MAX_KEYWORD_LEN = 120

# The five slots, in display order: (slot number, kind tag). Slots 1-3 keep the
# historical kinds so the mechanical fallback can compose them; 4-5 are generic
# "search" slots the AI step fills (blank in the fallback). `kind` is no longer
# shown in the UI — it only steers the fallback composition below.
SLOTS = ((1, "brand"), (2, "brand_product_size"), (3, "category"),
         (4, "search"), (5, "search"))

# The set of valid slot numbers, derived from SLOTS so adding/removing a slot is a
# one-line change here. Used to reject a forged slot in `set_keywords`.
VALID_SLOTS = frozenset(slot for slot, _ in SLOTS)


def _clean(keyword: str | None) -> str | None:
    """Normalize a keyword for storage: strip, cap length, blank -> None."""
    if keyword is None:
        return None
    cleaned = keyword.strip()[:MAX_KEYWORD_LEN].strip()
    return cleaned or None


def _compose_product_keyword(brand: str | None, title: str | None,
                             size: str | None) -> str | None:
    """Best-effort "brand + product + size" keyword (KW2).

    Avoids repeating the brand when the fetched title already leads with it (a
    Walmart title is often ``"<Brand> <Product>"``), and appends the size only
    when it isn't already present in the title. Returns None when there's nothing
    usable to build from.
    """
    brand = (brand or "").strip()
    title = (title or "").strip()
    size = (size or "").strip()

    parts: list[str] = []
    if title:
        # Don't double the brand if the title already starts with it.
        if brand and not title.lower().startswith(brand.lower()):
            parts.append(brand)
        parts.append(title)
    elif brand:
        parts.append(brand)

    base = " ".join(parts)
    if size and size.lower() not in base.lower():
        parts.append(size)

    return _clean(" ".join(parts))


def generate_defaults(conn: sqlite3.Connection, user_id: int, portfolio_item_id: int,
                      brand: str | None, *, title: str | None = None,
                      size: str | None = None, category: str | None = None) -> None:
    """Seed the five keyword rows for an item if they don't exist yet.

    Called at add-time (brand only, usually) and safe to call again — the
    ``UNIQUE(portfolio_item_id, slot)`` constraint means ``INSERT OR IGNORE`` never
    duplicates or clobbers an existing (possibly user-edited) row. These are only
    the mechanical fallback values; the descriptors backfill upgrades them to
    AI-chosen terms via :func:`set_ai_keywords` (falling back to
    :func:`apply_descriptors` when AI is off/unavailable).
    """
    # Mechanical fallback values per slot; slots 4-5 start blank (AI fills them).
    keywords = {
        1: _clean(brand),
        2: _compose_product_keyword(brand, title, size),
        3: _clean(category),
    }
    for slot, kind in SLOTS:
        conn.execute(
            "INSERT OR IGNORE INTO portfolio_keywords "
            "(portfolio_item_id, user_id, slot, kind, keyword) VALUES (?, ?, ?, ?, ?)",
            (portfolio_item_id, user_id, slot, kind, keywords.get(slot)),
        )
    conn.commit()
    logger.info("Seeded tracking keywords for portfolio_item_id=%s user_id=%s",
                portfolio_item_id, user_id)


def apply_descriptors(conn: sqlite3.Connection, portfolio_item_id: int, *,
                      brand: str | None, title: str | None,
                      size: str | None, category: str | None) -> None:
    """Fill KW2/KW3 from the fetched PDP, without clobbering user edits.

    Recomputes the "brand + product + size" keyword (slot 2) and sets the category
    keyword (slot 3), updating ONLY rows the user hasn't edited (``user_edited =
    0``). A blank computed value leaves the row as-is (keeps any earlier default).
    Called by the descriptors backfill worker once the product title/specs are
    known. Not user-facing, so it isn't user-scoped — the worker keys off the
    item row it already claimed.
    """
    kw2 = _compose_product_keyword(brand, title, size)
    if kw2:
        conn.execute(
            "UPDATE portfolio_keywords SET keyword = ?, updated_at = datetime('now') "
            "WHERE portfolio_item_id = ? AND slot = 2 AND user_edited = 0",
            (kw2, portfolio_item_id),
        )
    kw3 = _clean(category)
    if kw3:
        conn.execute(
            "UPDATE portfolio_keywords SET keyword = ?, updated_at = datetime('now') "
            "WHERE portfolio_item_id = ? AND slot = 3 AND user_edited = 0",
            (kw3, portfolio_item_id),
        )
    conn.commit()


def set_ai_keywords(conn: sqlite3.Connection, portfolio_item_id: int,
                    terms: list[str], *, user_id: int) -> int:
    """Write AI-chosen keywords into the item's slots, preserving user edits.

    ``terms`` is the ordered list from :func:`app.keyword_ai.suggest_keywords`
    (already cleaned/capped). Each term fills the next slot (1, 2, 3, …); any slot
    beyond the supplied terms is blanked (so a stale mechanical default doesn't
    linger and get scraped). Only rows the user hasn't edited (``user_edited = 0``)
    are touched — a hand-edited keyword is never overwritten.

    Ensures all :data:`SLOTS` rows exist first (``INSERT OR IGNORE`` needs the
    ``user_id`` for any missing slot — e.g. slots 4-5 on an item that predates the
    five-slot model), then updates the keyword text. Worker/command context (keyed
    off a row the caller already owns), so the UPDATE is scoped by
    ``portfolio_item_id`` like :func:`apply_descriptors`. Returns the number of
    slots written (filled + blanked). All writes are parameterized.
    """
    # Make sure every slot row exists so the UPDATEs below have something to hit.
    for slot, kind in SLOTS:
        conn.execute(
            "INSERT OR IGNORE INTO portfolio_keywords "
            "(portfolio_item_id, user_id, slot, kind, keyword) VALUES (?, ?, ?, ?, NULL)",
            (portfolio_item_id, user_id, slot, kind),
        )

    written = 0
    for slot, _ in SLOTS:
        # terms[0] -> slot 1, etc.; slots past the supplied terms are blanked.
        value = _clean(terms[slot - 1]) if slot - 1 < len(terms) else None
        cur = conn.execute(
            "UPDATE portfolio_keywords "
            "SET keyword = ?, updated_at = datetime('now') "
            "WHERE portfolio_item_id = ? AND slot = ? AND user_edited = 0",
            (value, portfolio_item_id, slot),
        )
        written += cur.rowcount
    conn.commit()
    logger.info("Applied %d AI keyword(s) to portfolio_item_id=%s (slots written=%d)",
                len([t for t in terms if t]), portfolio_item_id, written)
    return written


def list_keywords(conn: sqlite3.Connection, user_id: int,
                  portfolio_item_id: int) -> list[sqlite3.Row]:
    """The item's keyword rows (slot order), scoped to the signed-in user."""
    return conn.execute(
        "SELECT id, slot, kind, keyword, user_edited FROM portfolio_keywords "
        "WHERE portfolio_item_id = ? AND user_id = ? ORDER BY slot",
        (portfolio_item_id, user_id),
    ).fetchall()


def list_keywords_by_item(
    conn: sqlite3.Connection, user_id: int, portfolio_item_ids: list[int]
) -> dict[int, list[sqlite3.Row]]:
    """Keywords for several of the user's items at once, grouped by item id.

    Used by the Portfolio list to render each tracked item's keyword chips in one
    query instead of an N+1 loop of :func:`list_keywords`. User-scoped (the
    ``user_id`` predicate is the same IDOR gate), and the ``IN`` list is built from
    integer ids the caller already owns, so it is parameterized, not interpolated.
    Returns ``{portfolio_item_id: [rows in slot order]}``; items with no keywords
    are simply absent from the map.
    """
    if not portfolio_item_ids:
        return {}
    placeholders = ",".join("?" * len(portfolio_item_ids))
    rows = conn.execute(
        "SELECT id, portfolio_item_id, slot, kind, keyword, user_edited "
        "FROM portfolio_keywords "
        f"WHERE user_id = ? AND portfolio_item_id IN ({placeholders}) "
        "ORDER BY portfolio_item_id, slot",
        (user_id, *portfolio_item_ids),
    ).fetchall()
    by_item: dict[int, list[sqlite3.Row]] = {}
    for row in rows:
        by_item.setdefault(row["portfolio_item_id"], []).append(row)
    return by_item


def set_keywords(conn: sqlite3.Connection, user_id: int, portfolio_item_id: int,
                 values: dict[int, str | None]) -> bool:
    """Apply user edits to one item's keywords. Returns False on a foreign id.

    ``values`` maps slot (1|2|3) -> new keyword text (blank clears the slot, which
    means "don't scrape it"). Ownership is checked first so a forged/foreign
    ``portfolio_item_id`` can't touch another user's rows (the IDOR gate → 404).
    Each edited row is flagged ``user_edited = 1`` so the descriptors backfill
    stops overwriting it. All writes are parameterized and user-scoped.
    """
    owns = conn.execute(
        "SELECT 1 FROM portfolio_items WHERE id = ? AND user_id = ?",
        (portfolio_item_id, user_id),
    ).fetchone()
    if owns is None:
        logger.warning("Rejected keyword edit for foreign/absent portfolio_item_id=%s user_id=%s",
                       portfolio_item_id, user_id)
        return False

    for slot, raw in values.items():
        if slot not in VALID_SLOTS:
            continue
        conn.execute(
            "UPDATE portfolio_keywords "
            "SET keyword = ?, user_edited = 1, updated_at = datetime('now') "
            "WHERE portfolio_item_id = ? AND user_id = ? AND slot = ?",
            (_clean(raw), portfolio_item_id, user_id, slot),
        )
    conn.commit()
    logger.info("Updated tracking keywords for portfolio_item_id=%s user_id=%s slots=%s",
                portfolio_item_id, user_id, sorted(values))
    return True
