"""Enqueue item-tracking work for every tracked portfolio item.

Invoked by systemd timers — see ``deploy/ecomm-copilot-track-*.timer``:
  • ``daily``  → a cheap commerce-facts fetch per tracked item (price / stock /
                 rating / reviews), via the ``portfolio_fact_jobs`` queue.
  • ``weekly`` → a full content-readiness re-score per tracked item, reusing the
                 existing scoring queue (``scored_items``) so the readiness trend
                 and all its plumbing come for free.

Like :mod:`app.enqueue_monitoring`, this only writes ``queued`` rows and stays
fast/display-less; the background worker does the slow browser work.

Usage: ``python -m app.enqueue_item_tracking <daily|weekly>``.
"""

import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

logger = logging.getLogger("tracking.enqueue_item_tracking")

VALID_MODES = ("daily", "weekly")


def _connect() -> sqlite3.Connection:
    """Open a standalone SQLite connection with the schema ensured."""
    from app import db  # imported after load_dotenv so DATABASE_URL is honored

    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def enqueue_weekly_readiness(conn: sqlite3.Connection) -> int:
    """Enqueue a readiness re-score for every tracked item; return the count.

    Reuses :func:`app.jobs.enqueue_items`, grouping each user's tracked items into
    one submission (one ``batch_id`` per user per weekly run) so the resulting
    scored_items rows behave exactly like a user-initiated scoring run.
    """
    from app import jobs

    by_user: dict[int, list[dict]] = {}
    for it in conn.execute(
        "SELECT user_id, item_id, walmart_url, brand FROM portfolio_items WHERE tracked = 1"
    ):
        if not it["walmart_url"]:
            continue
        by_user.setdefault(it["user_id"], []).append(
            {"url": it["walmart_url"], "item": it["item_id"], "brand": it["brand"]}
        )

    enqueued = 0
    for user_id, items in by_user.items():
        enqueued += len(jobs.enqueue_items(conn, user_id, items))
    logger.info("Weekly readiness: enqueued %d re-score(s) across %d user(s)",
                enqueued, len(by_user))
    return enqueued


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    argv = argv if argv is not None else sys.argv[1:]
    mode = (argv[0] if argv else "").strip().lower()
    if mode not in VALID_MODES:
        logger.error("Usage: python -m app.enqueue_item_tracking <%s>", "|".join(VALID_MODES))
        return 2

    load_dotenv()
    conn = _connect()
    try:
        if mode == "daily":
            from app import tracking
            tracking.enqueue_fact_jobs(conn)
        else:  # weekly
            enqueue_weekly_readiness(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
