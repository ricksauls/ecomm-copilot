"""Per-item tracking keywords for portfolio products.

Every tracked portfolio item carries three keywords that the daily search sweep
(:mod:`app.item_rank`) scrapes for rank + share of search:

1. **brand** — the item's brand (e.g. ``Tabasco``).
2. **brand_product_size** — brand + product name + size (e.g. ``Tabasco Original
   Hot Sauce 12 oz``).
3. **category** — the category / shopper intent (e.g. ``Hot Sauce``).

They're auto-generated as *best-effort defaults* when an item is added (brand is
known immediately; product/size/category are filled by the descriptors backfill
once the PDP is fetched — see :func:`app.portfolio.save_descriptors`). The user
can then edit any of them; an edited row (``user_edited = 1``) is never
overwritten by the backfill.

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

# The three fixed slots, in display order: (slot number, kind label).
SLOTS = ((1, "brand"), (2, "brand_product_size"), (3, "category"))


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
    """Seed the three keyword rows for an item if they don't exist yet.

    Called at add-time (brand only, usually) and safe to call again — the
    ``UNIQUE(portfolio_item_id, slot)`` constraint means ``INSERT OR IGNORE`` never
    duplicates or clobbers an existing (possibly user-edited) row. Later refinement
    of KW2/KW3 from the fetched PDP goes through :func:`apply_descriptors`.
    """
    keywords = {
        1: _clean(brand),
        2: _compose_product_keyword(brand, title, size),
        3: _clean(category),
    }
    for slot, kind in SLOTS:
        conn.execute(
            "INSERT OR IGNORE INTO portfolio_keywords "
            "(portfolio_item_id, user_id, slot, kind, keyword) VALUES (?, ?, ?, ?, ?)",
            (portfolio_item_id, user_id, slot, kind, keywords[slot]),
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


def list_keywords(conn: sqlite3.Connection, user_id: int,
                  portfolio_item_id: int) -> list[sqlite3.Row]:
    """The item's three keyword rows (slot order), scoped to the signed-in user."""
    return conn.execute(
        "SELECT id, slot, kind, keyword, user_edited FROM portfolio_keywords "
        "WHERE portfolio_item_id = ? AND user_id = ? ORDER BY slot",
        (portfolio_item_id, user_id),
    ).fetchall()


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
        if slot not in (1, 2, 3):
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
