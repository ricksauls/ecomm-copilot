"""Portfolio Management: the user's curated catalog of managed products.

Items come from a CSV upload (columns ``Brand``, ``Walmart Item URL``,
``Amazon ASIN URL (Optional)``) or the single-item add form, and are then offered
as a filterable pick-list on the Content Scoring and Product Copy intakes — the
first way the user chooses which items to run, ahead of manual URL entry.

Everything here validates at the boundary: URLs are scheme-checked and
length-bounded (reusing :mod:`app.pdp`), the brand is length-capped, the CSV is
read under a byte cap and row cap, and every query is parameterized. De-duping is
per user on the Walmart item number (falling back to the normalized URL) so
re-importing a list skips products already in the portfolio — enforced by a
UNIQUE index, not just app code (see security-standards).
"""

import csv
import io
import logging
import sqlite3

from werkzeug.datastructures import FileStorage

from app import pdp

logger = logging.getLogger(__name__)

# Second belt-and-braces cap on an uploaded CSV, independent of the app-wide
# MAX_CONTENT_LENGTH (mirrors app.pdp.MAX_CSV_BYTES).
MAX_CSV_BYTES = 2 * 1024 * 1024
# Cap rows accepted from one import so an oversized file can't bloat the table.
MAX_IMPORT_ROWS = 1000

# Header matching is lenient: we lowercase + strip each column name and look for
# these substrings, so "Walmart Item URL", "walmart url", and "Walmart_URL" all
# map to the same field and a stray header row never sinks the import.
_BRAND_HINTS = ("brand",)
_WALMART_HINTS = ("walmart",)
_AMAZON_HINTS = ("amazon", "asin")

# Column headers written to the downloadable template. Kept here so the template
# route and any documentation share one source of truth.
TEMPLATE_HEADERS = ("Brand", "Walmart Item URL", "Amazon ASIN URL (Optional)")


def _dedupe_key(walmart_url: str, item_id: str | None) -> str:
    """The per-user uniqueness key for a managed product.

    The Walmart item number is the stable identity when the URL carries one
    (different slugs/campaign params still resolve to the same product); when it
    doesn't, fall back to the lowercased URL so the row is still de-duplicated
    against an identical paste.
    """
    return item_id if item_id else walmart_url.strip().lower()


def _clean_optional_url(raw: str | None) -> str | None:
    """Validate an optional URL (e.g. the Amazon link), returning None when blank.

    Reuses the scheme/length allowlist from :func:`app.pdp.validate_item_url`, so
    ``javascript:``/``data:`` and oversized values are rejected rather than stored.
    """
    if not raw or not raw.strip():
        return None
    return pdp.validate_item_url(raw)


def parse_csv(file: FileStorage) -> tuple[list[dict], int]:
    """Parse an uploaded portfolio CSV into clean rows.

    Returns ``(rows, invalid_count)`` where each row is
    ``{"brand", "walmart_url", "amazon_url"}`` (amazon may be None; brand is
    required) and ``invalid_count`` is the number of non-empty data rows dropped
    for lacking a usable Walmart URL **or a brand** — surfaced to the user so a
    malformed file isn't silently half-imported. Reads at most ``MAX_CSV_BYTES``
    and ``MAX_IMPORT_ROWS`` rows.
    """
    raw = file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        logger.warning("Portfolio CSV exceeded %d bytes; truncating", MAX_CSV_BYTES)
        raw = raw[:MAX_CSV_BYTES]
    # Decode leniently — a stray non-UTF-8 byte shouldn't sink the whole file.
    text = raw.decode("utf-8", errors="replace")

    rows: list[dict] = []
    invalid = 0
    try:
        all_rows = list(csv.reader(io.StringIO(text)))
        first = next((r for r in all_rows if any(c.strip() for c in r)), None)
        if first is None:
            return rows, 0

        # Decide header vs headerless by content, not by name alone: a real header
        # is made of labels, so it contains no valid URL. If the first non-empty
        # row already holds a URL, there's no header — parse every row positionally
        # (brand, walmart, amazon) so that first row's data isn't lost.
        first_has_url = any(pdp.validate_item_url(c) for c in first)
        if first_has_url:
            brand_i, walmart_i, amazon_i = 0, 1, 2
            candidate_rows = all_rows
        else:
            brand_i, walmart_i, amazon_i = _map_columns(first)
            if walmart_i is None:
                # A header with no recognizable Walmart column — fall back to
                # positional for the remaining rows rather than dropping them all.
                brand_i, walmart_i, amazon_i = 0, 1, 2
            candidate_rows = all_rows[all_rows.index(first) + 1:]

        for cells in candidate_rows:
            if len(rows) >= MAX_IMPORT_ROWS:
                logger.warning("Portfolio CSV hit the %d-row cap; ignoring the rest", MAX_IMPORT_ROWS)
                break
            if not any(c.strip() for c in cells):
                continue  # blank line — not an invalid row, just noise
            walmart_raw = cells[walmart_i] if walmart_i < len(cells) else ""
            walmart_url = pdp.validate_item_url(walmart_raw)
            if walmart_url is None:
                invalid += 1
                continue
            # Brand is required on every managed item — a row without one is
            # dropped (counted invalid), not imported with a blank brand.
            brand = pdp.clean_brand(cells[brand_i]) if brand_i is not None and brand_i < len(cells) else None
            if brand is None:
                invalid += 1
                continue
            amazon_url = _clean_optional_url(cells[amazon_i]) if amazon_i is not None and amazon_i < len(cells) else None
            rows.append({"brand": brand, "walmart_url": walmart_url, "amazon_url": amazon_url})
    except csv.Error as e:
        # Malformed CSV: keep whatever parsed before the error rather than failing
        # the whole upload.
        logger.warning("Portfolio CSV parse error after %d rows: %s", len(rows), e)

    return rows, invalid


def _map_columns(header: list[str]) -> tuple[int | None, int | None, int | None]:
    """Map the CSV header to (brand, walmart, amazon) column indexes by name.

    Case/space-insensitive substring match so common header spellings all work.
    Any index may be None when that column is absent.
    """
    brand_i = walmart_i = amazon_i = None
    for idx, name in enumerate(header):
        key = name.strip().lower()
        if walmart_i is None and any(h in key for h in _WALMART_HINTS):
            walmart_i = idx
        elif amazon_i is None and any(h in key for h in _AMAZON_HINTS):
            amazon_i = idx
        elif brand_i is None and any(h in key for h in _BRAND_HINTS):
            brand_i = idx
    return brand_i, walmart_i, amazon_i


def import_rows(conn: sqlite3.Connection, user_id: int, rows: list[dict]) -> dict:
    """Insert parsed rows, skipping any product already in the user's portfolio.

    Returns ``{"added": int, "skipped": int}``. Skipping is enforced by the
    per-user UNIQUE index on ``dedupe_key`` via ``INSERT OR IGNORE``, so a row
    that collides with an existing item (or a duplicate within this same upload)
    is counted as skipped rather than erroring the batch.
    """
    added = 0
    for row in rows:
        walmart_url = row["walmart_url"]
        item_id = pdp.item_number_from_url(walmart_url)
        key = _dedupe_key(walmart_url, item_id)
        cur = conn.execute(
            "INSERT OR IGNORE INTO portfolio_items "
            "(user_id, brand, walmart_url, item_id, amazon_url, dedupe_key) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, row.get("brand"), walmart_url, item_id, row.get("amazon_url"), key),
        )
        added += cur.rowcount  # 1 when inserted, 0 when the UNIQUE index skipped it
    conn.commit()
    skipped = len(rows) - added
    logger.info("Portfolio import: %d added, %d skipped, user_id=%s", added, skipped, user_id)
    return {"added": added, "skipped": skipped}


def add_item(conn: sqlite3.Connection, user_id: int, brand: str | None,
             walmart_url_raw: str, amazon_url_raw: str | None) -> tuple[bool, str | None]:
    """Add a single managed product from the inline form.

    Returns ``(ok, error)``. ``ok`` is False with a user-facing message when the
    brand is blank or the Walmart URL is invalid; a product already in the
    portfolio returns ``(False, <already-exists message>)`` so the UI can say so
    plainly. Brand is required on every managed item.
    """
    cleaned_brand = pdp.clean_brand(brand)
    if cleaned_brand is None:
        return False, "Enter a brand for this item."
    walmart_url = pdp.validate_item_url(walmart_url_raw)
    if walmart_url is None:
        return False, "Enter a valid Walmart item URL (http/https)."
    item_id = pdp.item_number_from_url(walmart_url)
    key = _dedupe_key(walmart_url, item_id)
    cur = conn.execute(
        "INSERT OR IGNORE INTO portfolio_items "
        "(user_id, brand, walmart_url, item_id, amazon_url, dedupe_key) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, cleaned_brand, walmart_url, item_id,
         _clean_optional_url(amazon_url_raw), key),
    )
    conn.commit()
    if cur.rowcount == 0:
        return False, "That product is already in your portfolio."
    logger.info("Portfolio: added one item, user_id=%s", user_id)
    return True, None


def delete_item(conn: sqlite3.Connection, user_id: int, pk: int) -> bool:
    """Delete one portfolio item the user owns. Returns True when a row was removed.

    Scoped to ``user_id`` so one user can't delete another's item (IDOR guard).
    """
    cur = conn.execute(
        "DELETE FROM portfolio_items WHERE id = ? AND user_id = ?", (pk, user_id)
    )
    conn.commit()
    removed = cur.rowcount > 0
    if removed:
        logger.info("Portfolio: deleted item id=%s, user_id=%s", pk, user_id)
    return removed


def list_items(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """All of the user's managed products, newest first.

    Carries the columns the screen and the scoring/copy pick-lists need: the
    Walmart URL (the value submitted to a run), the parsed item number and brand
    (both filterable), and the optional Amazon link.
    """
    return conn.execute(
        "SELECT id, brand, walmart_url, item_id, amazon_url, created_at "
        "FROM portfolio_items WHERE user_id = ? ORDER BY id DESC",
        (user_id,),
    ).fetchall()


def distinct_brands(conn: sqlite3.Connection, user_id: int) -> list[str]:
    """Distinct non-blank brands in the user's portfolio, for the filter dropdown.

    Sorted case-insensitively; the stored casing is returned for display.
    """
    rows = conn.execute(
        "SELECT DISTINCT brand FROM portfolio_items "
        "WHERE user_id = ? AND brand IS NOT NULL AND TRIM(brand) != '' "
        "ORDER BY LOWER(brand)",
        (user_id,),
    ).fetchall()
    return [r["brand"] for r in rows]
