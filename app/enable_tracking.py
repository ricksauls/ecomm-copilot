"""Admin one-off: turn tracking on for managed items whose name begins with a prefix.

Used to scope the initial tracking rollout — e.g. enable only the demo products
(brand or title starting with "DEMO") without hand-toggling each one in the UI.
Idempotent: re-running it just re-sets the same rows. Matching items get their
keyword rows seeded too (so they're ready to scrape) if they don't have them yet.

Usage: ``python -m app.enable_tracking [PREFIX]``  (PREFIX defaults to "DEMO").

Only sets the tracked flag — no scraping happens until the daily rank sweep is
enabled (ITEM_TRACKING_RANK_ENABLED) and the item has non-blank keywords.
"""

import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

logger = logging.getLogger("tracking.enable_tracking")

DEFAULT_PREFIX = "DEMO"


def _connect() -> sqlite3.Connection:
    """Open a standalone SQLite connection with the schema ensured."""
    from app import db  # imported after load_dotenv so DATABASE_URL is honored

    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def enable_by_prefix(conn: sqlite3.Connection, prefix: str) -> int:
    """Set tracked=1 for items whose brand OR title starts with ``prefix``.

    Case-insensitive (SQLite ``LIKE`` is case-insensitive for ASCII). Seeds the 3
    keyword rows for each matched item that lacks them, so newly-tracked items are
    immediately ready for the daily sweep. Returns the number of items enabled.
    """
    from app import item_keywords

    like = f"{prefix}%"
    rows = conn.execute(
        "SELECT id, user_id, brand, title FROM portfolio_items "
        "WHERE brand LIKE ? OR title LIKE ?",
        (like, like),
    ).fetchall()

    for row in rows:
        conn.execute(
            "UPDATE portfolio_items SET tracked = 1, updated_at = datetime('now') WHERE id = ?",
            (row["id"],),
        )
        # Seed keywords if this item somehow has none (idempotent; INSERT OR IGNORE).
        item_keywords.generate_defaults(conn, row["user_id"], row["id"], row["brand"],
                                        title=row["title"])
    conn.commit()
    logger.info("Enabled tracking for %d item(s) matching prefix %r", len(rows), prefix)
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    argv = argv if argv is not None else sys.argv[1:]
    prefix = (argv[0] if argv else DEFAULT_PREFIX).strip()
    if not prefix:
        logger.error("Usage: python -m app.enable_tracking [PREFIX]")
        return 2

    load_dotenv()
    conn = _connect()
    try:
        count = enable_by_prefix(conn, prefix)
    finally:
        conn.close()
    logger.info("Done — %d item(s) now tracked (prefix %r)", count, prefix)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
