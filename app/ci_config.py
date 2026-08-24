"""Config CRUD for Competitive Intelligence groups, brands, products, keywords.

A user owns one or more *groups*; each group holds *brands* (their own vs
competitors), the *products* under each brand, and the *keywords* to track. Every
mutation is scoped by ``user_id`` and verifies the target group belongs to the
caller before touching any child row — this is the IDOR boundary for the whole
feature, so callers can pass user-supplied ids freely.

Functions take an explicit ``sqlite3.Connection`` so they work both inside a
Flask request (``app.db.get_db()``) and in the worker/scheduler process. All SQL
is parameterized (never build query strings from input — see security-standards).
"""

import logging
import sqlite3

from app import pdp

logger = logging.getLogger(__name__)

# Bounds. Unbounded text/counts are a DoS and storage-abuse vector, so cap both
# field lengths and how many children a single group/user can hold.
MAX_NAME_LEN = 255
MAX_DESC_LEN = 1000
MAX_KEYWORD_LEN = 255
MAX_GROUPS_PER_USER = 50
MAX_BRANDS_PER_GROUP = 100
MAX_PRODUCTS_PER_GROUP = 500
MAX_KEYWORDS_PER_GROUP = 200

BRAND_TYPES = ("mine", "competitor")
# A group is either a one-time snapshot or an ongoing monitoring group.
GROUP_MODES = ("snapshot", "monitoring")


class ConfigError(ValueError):
    """A config mutation was rejected (bad input, limit hit, or not authorized)."""


# ── small helpers ──────────────────────────────────────────────────────────────

def _clean_text(raw: str | None, *, max_len: int, field: str, required: bool = True) -> str | None:
    """Trim and length-check a free-text field, raising ConfigError on violation."""
    value = (raw or "").strip()
    if not value:
        if required:
            raise ConfigError(f"{field} is required.")
        return None
    if len(value) > max_len:
        raise ConfigError(f"{field} must be {max_len} characters or fewer.")
    return value


def owns_group(conn: sqlite3.Connection, group_id: int, user_id: int) -> bool:
    """Return True iff ``group_id`` exists and belongs to ``user_id``."""
    row = conn.execute(
        "SELECT 1 FROM ci_groups WHERE id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()
    return row is not None


def _require_group(conn: sqlite3.Connection, group_id: int, user_id: int) -> None:
    """Raise ConfigError unless ``user_id`` owns ``group_id`` (the IDOR gate)."""
    if not owns_group(conn, group_id, user_id):
        # Deliberately vague — don't reveal whether the group exists for someone else.
        raise ConfigError("Group not found.")


def _count(conn: sqlite3.Connection, table: str, group_id: int) -> int:
    # `table` is a hard-coded literal at every call site, never user input.
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE group_id = ?", (group_id,)
    ).fetchone()[0])


# ── groups ──────────────────────────────────────────────────────────────────────

def create_group(conn: sqlite3.Connection, user_id: int, name: str,
                 description: str | None = None, mode: str = "snapshot") -> int:
    """Create a group for ``user_id`` in the given mode; return its id."""
    name = _clean_text(name, max_len=MAX_NAME_LEN, field="Group name")
    description = _clean_text(description, max_len=MAX_DESC_LEN, field="Description", required=False)
    if mode not in GROUP_MODES:
        raise ConfigError("Group mode must be 'snapshot' or 'monitoring'.")

    existing = conn.execute(
        "SELECT COUNT(*) FROM ci_groups WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if existing >= MAX_GROUPS_PER_USER:
        raise ConfigError(f"You can have at most {MAX_GROUPS_PER_USER} groups.")

    cur = conn.execute(
        "INSERT INTO ci_groups (user_id, name, description, mode) VALUES (?, ?, ?, ?)",
        (user_id, name, description, mode),
    )
    conn.commit()
    group_id = int(cur.lastrowid)
    logger.info("CI group created id=%s user_id=%s mode=%s", group_id, user_id, mode)
    return group_id


def list_groups(conn: sqlite3.Connection, user_id: int,
                mode: str | None = None) -> list[sqlite3.Row]:
    """Return the user's groups (newest first) with child counts for the list view.

    ``mode`` optionally restricts to 'snapshot' or 'monitoring' groups (the two
    setup menus each show their own).
    """
    sql = (
        "SELECT g.*, "
        "  (SELECT COUNT(*) FROM ci_brands   b WHERE b.group_id = g.id) AS brand_count, "
        "  (SELECT COUNT(*) FROM ci_products p WHERE p.group_id = g.id) AS product_count, "
        "  (SELECT COUNT(*) FROM ci_keywords k WHERE k.group_id = g.id) AS keyword_count "
        "FROM ci_groups g WHERE g.user_id = ?"
    )
    params: list = [user_id]
    if mode is not None:
        sql += " AND g.mode = ?"
        params.append(mode)
    sql += " ORDER BY g.id DESC"
    return conn.execute(sql, params).fetchall()


def get_group(conn: sqlite3.Connection, group_id: int, user_id: int) -> sqlite3.Row | None:
    """Return the group row if owned by ``user_id``, else None."""
    return conn.execute(
        "SELECT * FROM ci_groups WHERE id = ? AND user_id = ?",
        (group_id, user_id),
    ).fetchone()


def update_group(conn: sqlite3.Connection, group_id: int, user_id: int, *,
                 name: str | None = None, description: str | None = None) -> None:
    """Rename / re-describe a group the caller owns."""
    _require_group(conn, group_id, user_id)
    name = _clean_text(name, max_len=MAX_NAME_LEN, field="Group name")
    description = _clean_text(description, max_len=MAX_DESC_LEN, field="Description", required=False)
    conn.execute(
        "UPDATE ci_groups SET name = ?, description = ? WHERE id = ? AND user_id = ?",
        (name, description, group_id, user_id),
    )
    conn.commit()


def set_monitoring(conn: sqlite3.Connection, group_id: int, user_id: int, enabled: bool) -> None:
    """Toggle whether the scheduled 3x/day sweep includes this group."""
    _require_group(conn, group_id, user_id)
    conn.execute(
        "UPDATE ci_groups SET monitoring_enabled = ? WHERE id = ? AND user_id = ?",
        (1 if enabled else 0, group_id, user_id),
    )
    conn.commit()
    logger.info("CI group id=%s monitoring_enabled=%s (user_id=%s)", group_id, bool(enabled), user_id)


def clone_group_as_monitoring(conn: sqlite3.Connection, group_id: int, user_id: int) -> int:
    """Copy a snapshot group into a new monitoring group and return its id.

    Powers "Schedule for monitoring" on the snapshot card: a user who likes a
    one-time competitive read can promote that exact set to ongoing tracking. The
    source group is left untouched; a fresh ``mode='monitoring'`` group is created
    with monitoring already enabled (so the scheduled sweep picks it up) and every
    brand, product, and keyword duplicated. Run history is *not* copied — the new
    group starts its own baseline.

    All inserts share one transaction so a failure can't leave a half-populated
    group behind (the sweep would otherwise scrape an incomplete competitive set).
    """
    src = get_group(conn, group_id, user_id)
    if src is None:
        # Vague on purpose — don't reveal another user's group ids (IDOR gate).
        raise ConfigError("Group not found.")
    # Reuse the per-user cap check so cloning can't bypass the limit create_group enforces.
    existing = conn.execute(
        "SELECT COUNT(*) FROM ci_groups WHERE user_id = ?", (user_id,)
    ).fetchone()[0]
    if existing >= MAX_GROUPS_PER_USER:
        raise ConfigError(f"You can have at most {MAX_GROUPS_PER_USER} groups.")

    try:
        cur = conn.execute(
            "INSERT INTO ci_groups (user_id, name, description, mode, monitoring_enabled) "
            "VALUES (?, ?, ?, 'monitoring', 1)",
            (user_id, src["name"], src["description"]),
        )
        new_group_id = int(cur.lastrowid)

        # Copy brands first, keeping an old->new id map so products re-point correctly.
        brand_id_map: dict[int, int] = {}
        for b in conn.execute("SELECT * FROM ci_brands WHERE group_id = ?", (group_id,)):
            bc = conn.execute(
                "INSERT INTO ci_brands (group_id, name, type, tracked) VALUES (?, ?, ?, ?)",
                (new_group_id, b["name"], b["type"], b["tracked"]),
            )
            brand_id_map[b["id"]] = int(bc.lastrowid)

        # Products carry already-validated item id + URL — copy verbatim under the
        # remapped brand, preserving each row's active flag.
        for p in conn.execute("SELECT * FROM ci_products WHERE group_id = ?", (group_id,)):
            conn.execute(
                "INSERT INTO ci_products "
                "(group_id, brand_id, name, walmart_item_id, walmart_url, active) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (new_group_id, brand_id_map[p["brand_id"]], p["name"],
                 p["walmart_item_id"], p["walmart_url"], p["active"]),
            )

        for k in conn.execute("SELECT * FROM ci_keywords WHERE group_id = ?", (group_id,)):
            conn.execute(
                "INSERT INTO ci_keywords (group_id, keyword, active) VALUES (?, ?, ?)",
                (new_group_id, k["keyword"], k["active"]),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()  # keep the clone all-or-nothing
        logger.exception("CI clone-to-monitoring failed src_group_id=%s user_id=%s",
                         group_id, user_id)
        raise

    logger.info("CI group cloned to monitoring src=%s new=%s user_id=%s",
                group_id, new_group_id, user_id)
    return new_group_id


def delete_group(conn: sqlite3.Connection, group_id: int, user_id: int) -> None:
    """Delete a group and (via ON DELETE CASCADE) all its children."""
    _require_group(conn, group_id, user_id)
    conn.execute("DELETE FROM ci_groups WHERE id = ? AND user_id = ?", (group_id, user_id))
    conn.commit()
    logger.info("CI group deleted id=%s user_id=%s", group_id, user_id)


# ── brands ──────────────────────────────────────────────────────────────────────

def add_brand(conn: sqlite3.Connection, group_id: int, user_id: int, name: str,
              brand_type: str) -> int:
    """Add a brand to a group; return its id. ``brand_type`` is mine|competitor."""
    _require_group(conn, group_id, user_id)
    name = _clean_text(name, max_len=MAX_NAME_LEN, field="Brand name")
    if brand_type not in BRAND_TYPES:
        raise ConfigError("Brand type must be 'mine' or 'competitor'.")
    if _count(conn, "ci_brands", group_id) >= MAX_BRANDS_PER_GROUP:
        raise ConfigError(f"A group can have at most {MAX_BRANDS_PER_GROUP} brands.")
    cur = conn.execute(
        "INSERT INTO ci_brands (group_id, name, type) VALUES (?, ?, ?)",
        (group_id, name, brand_type),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_brands(conn: sqlite3.Connection, group_id: int, user_id: int) -> list[sqlite3.Row]:
    """Return the group's brands with a product count, ordered mine-first then name."""
    _require_group(conn, group_id, user_id)
    return conn.execute(
        "SELECT b.*, (SELECT COUNT(*) FROM ci_products p WHERE p.brand_id = b.id) AS product_count "
        "FROM ci_brands b WHERE b.group_id = ? "
        "ORDER BY CASE b.type WHEN 'mine' THEN 0 ELSE 1 END, b.name COLLATE NOCASE",
        (group_id,),
    ).fetchall()


def delete_brand(conn: sqlite3.Connection, brand_id: int, user_id: int) -> None:
    """Delete a brand (and its products) if the caller owns the parent group."""
    row = conn.execute(
        "SELECT b.id FROM ci_brands b JOIN ci_groups g ON g.id = b.group_id "
        "WHERE b.id = ? AND g.user_id = ?",
        (brand_id, user_id),
    ).fetchone()
    if row is None:
        raise ConfigError("Brand not found.")
    conn.execute("DELETE FROM ci_brands WHERE id = ?", (brand_id,))
    conn.commit()


# ── products ────────────────────────────────────────────────────────────────────

def add_product(conn: sqlite3.Connection, group_id: int, brand_id: int, user_id: int,
                url: str, name: str | None = None) -> int:
    """Add a product under a brand. Validates the Walmart URL and derives item id.

    Reuses the PDP intake validators (:func:`pdp.validate_item_url`,
    :func:`pdp.item_number_from_url`) so URL handling matches the rest of the app.
    """
    _require_group(conn, group_id, user_id)
    # Brand must belong to the same group (prevents cross-group stitching).
    brand = conn.execute(
        "SELECT id FROM ci_brands WHERE id = ? AND group_id = ?", (brand_id, group_id)
    ).fetchone()
    if brand is None:
        raise ConfigError("Brand not found in this group.")

    clean_url = pdp.validate_item_url(url)
    if not clean_url:
        raise ConfigError("Enter a valid http(s) product URL.")
    item_id = pdp.item_number_from_url(clean_url)
    if not item_id:
        raise ConfigError("Could not find a Walmart item number in that URL.")

    name = _clean_text(name, max_len=MAX_NAME_LEN, field="Product name", required=False)
    if _count(conn, "ci_products", group_id) >= MAX_PRODUCTS_PER_GROUP:
        raise ConfigError(f"A group can have at most {MAX_PRODUCTS_PER_GROUP} products.")

    cur = conn.execute(
        "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url) "
        "VALUES (?, ?, ?, ?, ?)",
        (group_id, brand_id, name, item_id, clean_url),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_products(conn: sqlite3.Connection, group_id: int, user_id: int) -> list[sqlite3.Row]:
    """Return the group's products joined to their brand (for the config table)."""
    _require_group(conn, group_id, user_id)
    return conn.execute(
        "SELECT p.*, b.name AS brand_name, b.type AS brand_type "
        "FROM ci_products p JOIN ci_brands b ON b.id = p.brand_id "
        "WHERE p.group_id = ? "
        "ORDER BY b.name COLLATE NOCASE, p.id DESC",
        (group_id,),
    ).fetchall()


def delete_product(conn: sqlite3.Connection, product_id: int, user_id: int) -> None:
    """Delete a product if the caller owns the parent group."""
    row = conn.execute(
        "SELECT p.id FROM ci_products p JOIN ci_groups g ON g.id = p.group_id "
        "WHERE p.id = ? AND g.user_id = ?",
        (product_id, user_id),
    ).fetchone()
    if row is None:
        raise ConfigError("Product not found.")
    conn.execute("DELETE FROM ci_products WHERE id = ?", (product_id,))
    conn.commit()


# ── keywords ────────────────────────────────────────────────────────────────────

def add_keyword(conn: sqlite3.Connection, group_id: int, user_id: int, keyword: str) -> int:
    """Add a search keyword to a group; return its id. Duplicates are rejected."""
    _require_group(conn, group_id, user_id)
    keyword = _clean_text(keyword, max_len=MAX_KEYWORD_LEN, field="Keyword")
    if _count(conn, "ci_keywords", group_id) >= MAX_KEYWORDS_PER_GROUP:
        raise ConfigError(f"A group can have at most {MAX_KEYWORDS_PER_GROUP} keywords.")
    # Case-insensitive de-dupe within the group so the same term isn't scraped twice.
    dupe = conn.execute(
        "SELECT 1 FROM ci_keywords WHERE group_id = ? AND keyword = ? COLLATE NOCASE",
        (group_id, keyword),
    ).fetchone()
    if dupe is not None:
        raise ConfigError("That keyword is already tracked in this group.")
    cur = conn.execute(
        "INSERT INTO ci_keywords (group_id, keyword) VALUES (?, ?)",
        (group_id, keyword),
    )
    conn.commit()
    return int(cur.lastrowid)


def list_keywords(conn: sqlite3.Connection, group_id: int, user_id: int) -> list[sqlite3.Row]:
    """Return the group's keywords, alphabetical."""
    _require_group(conn, group_id, user_id)
    return conn.execute(
        "SELECT * FROM ci_keywords WHERE group_id = ? ORDER BY keyword COLLATE NOCASE",
        (group_id,),
    ).fetchall()


def delete_keyword(conn: sqlite3.Connection, keyword_id: int, user_id: int) -> None:
    """Delete a keyword if the caller owns the parent group."""
    row = conn.execute(
        "SELECT k.id FROM ci_keywords k JOIN ci_groups g ON g.id = k.group_id "
        "WHERE k.id = ? AND g.user_id = ?",
        (keyword_id, user_id),
    ).fetchone()
    if row is None:
        raise ConfigError("Keyword not found.")
    conn.execute("DELETE FROM ci_keywords WHERE id = ?", (keyword_id,))
    conn.commit()


# ── config bundle for the scraper/worker ────────────────────────────────────────

def load_group_config(conn: sqlite3.Connection, group_id: int) -> tuple[list, dict, dict]:
    """Load a group's scrape inputs (no user filter — the run already owns the group).

    Mirrors the reference daily.load_config: returns
        keywords  — list of active ci_keywords rows
        item_map  — {walmart_item_id: product row} for brand matching
        brand_map — {brand_id: brand row}
    """
    keywords = conn.execute(
        "SELECT * FROM ci_keywords WHERE group_id = ? AND active = 1 ORDER BY id",
        (group_id,),
    ).fetchall()
    products = conn.execute(
        "SELECT * FROM ci_products WHERE group_id = ? AND active = 1", (group_id,)
    ).fetchall()
    brands = conn.execute("SELECT * FROM ci_brands WHERE group_id = ?", (group_id,)).fetchall()

    item_map = {p["walmart_item_id"]: p for p in products}
    brand_map = {b["id"]: b for b in brands}
    return keywords, item_map, brand_map


def groups_with_monitoring(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return active monitoring groups opted into the sweep (used by the scheduler)."""
    return conn.execute(
        "SELECT * FROM ci_groups "
        "WHERE monitoring_enabled = 1 AND active = 1 AND mode = 'monitoring' ORDER BY id"
    ).fetchall()
