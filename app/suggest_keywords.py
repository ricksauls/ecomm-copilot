"""Admin one-off: (re)generate AI tracking keywords for existing portfolio items.

The portfolio keyword model moved from three mechanical slots (brand /
brand+product+size / category) to five AI-chosen shopper search terms (see
:mod:`app.keyword_ai`). New items get AI keywords automatically from the
descriptors backfill, but items that predate the change keep their old keywords
(and only three slots) until this runs.

For each matched item it calls OpenAI with the item's **already-stored** brand and
title (no Walmart re-scrape — so this costs no Oxylabs requests, only the cheap
chat calls) and writes the result into the item's slots via
:func:`app.item_keywords.set_ai_keywords`, which:
  • ensures all five slot rows exist (adds slots 4-5 to a three-slot item),
  • fills slot 1..N from the suggested terms and blanks the rest,
  • never overwrites a keyword the user has edited (``user_edited = 1``).

An item whose AI call fails is left exactly as-is and logged, so a transient error
never wipes good keywords. Idempotent: re-running just re-asks and re-writes the
non-edited slots.

Usage:
  python -m app.suggest_keywords            # all TRACKED items (the common case)
  python -m app.suggest_keywords --all      # every portfolio item, tracked or not
  python -m app.suggest_keywords PREFIX     # items whose brand/title/item# starts
                                            # with PREFIX (e.g. a demo set)

Requires OPENAI_API_KEY and KEYWORD_AI_ENABLED (on by default) in the environment.
"""

import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

from app import item_keywords, keyword_ai

logger = logging.getLogger("tracking.suggest_keywords")


def _connect() -> sqlite3.Connection:
    """Open a standalone SQLite connection with the schema ensured."""
    from app import db  # imported after load_dotenv so DATABASE_URL is honored

    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def _select_items(conn: sqlite3.Connection, *, all_items: bool,
                  prefix: str | None) -> list[sqlite3.Row]:
    """Pick the items to regenerate: tracked-only (default), all, or by prefix."""
    if prefix:
        like = f"{prefix}%"
        return conn.execute(
            "SELECT id, user_id, brand, title FROM portfolio_items "
            "WHERE brand LIKE ? OR title LIKE ? OR item_id LIKE ? ORDER BY id",
            (like, like, like),
        ).fetchall()
    if all_items:
        return conn.execute(
            "SELECT id, user_id, brand, title FROM portfolio_items ORDER BY id"
        ).fetchall()
    return conn.execute(
        "SELECT id, user_id, brand, title FROM portfolio_items WHERE tracked = 1 ORDER BY id"
    ).fetchall()


def regenerate(conn: sqlite3.Connection, items: list[sqlite3.Row]) -> dict:
    """Ask OpenAI for keywords per item and write them. Returns a small summary.

    Uses stored brand + title only (no size/category — those aren't persisted on
    the item row), which is enough for a good suggestion and avoids any re-scrape.
    """
    updated = failed = skipped = 0
    for row in items:
        try:
            terms = keyword_ai.suggest_keywords(row["brand"], row["title"], None, None)
        except keyword_ai.KeywordAIError as e:
            # Leave this item's keywords untouched; the message never carries the key.
            logger.warning("Skipped item id=%s — AI keywords unavailable: %s", row["id"], e)
            # "disabled"/"no brand or title" are deliberate skips, not hard failures.
            if "disabled" in str(e).lower() or "no brand or title" in str(e).lower():
                skipped += 1
            else:
                failed += 1
            continue
        item_keywords.set_ai_keywords(conn, row["id"], terms, user_id=row["user_id"])
        updated += 1
        logger.info("Regenerated keywords for item id=%s (%d term(s))", row["id"], len(terms))
    return {"updated": updated, "failed": failed, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    argv = argv if argv is not None else sys.argv[1:]
    all_items = "--all" in argv
    prefix = next((a.strip() for a in argv if not a.startswith("--") and a.strip()), None)

    load_dotenv()
    if not keyword_ai.ai_keywords_enabled():
        logger.error("KEYWORD_AI_ENABLED is off — nothing to do. Enable it to regenerate.")
        return 2

    conn = _connect()
    try:
        items = _select_items(conn, all_items=all_items, prefix=prefix)
        if not items:
            logger.info("No matching portfolio items.")
            return 0
        logger.info("Regenerating AI keywords for %d item(s)%s", len(items),
                    f" (prefix {prefix!r})" if prefix else (" (all)" if all_items else " (tracked)"))
        summary = regenerate(conn, items)
    finally:
        conn.close()
    logger.info("Done — %d updated, %d failed, %d skipped",
                summary["updated"], summary["failed"], summary["skipped"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
