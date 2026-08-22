"""Enqueue a monitoring CI run for every opted-in group.

Invoked by the systemd timers (3x/day: morning/afternoon/night, CST) — see
``deploy/ecomm-copilot-ci-*.timer``. It only writes ``queued`` rows to the run
queue; the background worker does the actual (slow, browser) scraping, so this
script stays fast and needs no display.

Usage: ``python -m app.enqueue_monitoring <slot>`` where slot is one of
morning|afternoon|night.
"""

import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

logger = logging.getLogger("ci.enqueue_monitoring")

VALID_SLOTS = ("morning", "afternoon", "night")


def _connect() -> sqlite3.Connection:
    """Open a standalone SQLite connection with the schema ensured."""
    from app import db  # imported after load_dotenv so DATABASE_URL is set

    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def enqueue_all(conn: sqlite3.Connection, slot: str) -> int:
    """Enqueue a monitoring run for each monitoring-enabled group; return the count.

    Skips a group that already has a queued/running run so a backed-up worker
    doesn't accumulate duplicate sweeps.
    """
    from app import ci_config, ci_jobs

    groups = ci_config.groups_with_monitoring(conn)
    enqueued = 0
    for g in groups:
        if ci_jobs.has_active_run(conn, g["id"]):
            logger.info("Skipping group id=%s (%s) — a run is already active",
                        g["id"], g["name"])
            continue
        ci_jobs.enqueue_run(conn, g["id"], run_type="monitoring", slot=slot)
        enqueued += 1
    logger.info("Monitoring slot=%s: enqueued %d of %d eligible group(s)",
                slot, enqueued, len(groups))
    return enqueued


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    argv = argv if argv is not None else sys.argv[1:]
    slot = (argv[0] if argv else "").strip().lower()
    if slot not in VALID_SLOTS:
        logger.error("Usage: python -m app.enqueue_monitoring <%s>", "|".join(VALID_SLOTS))
        return 2

    load_dotenv()
    conn = _connect()
    try:
        enqueue_all(conn, slot)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
