"""Enqueue scheduled business reports that are due today, and prune old ones.

Invoked once a day by a systemd timer — see ``deploy/ecomm-copilot-reports.timer``.
Like :mod:`app.enqueue_monitoring` / :mod:`app.enqueue_item_tracking`, it only
writes ``queued`` rows (one ``generated_reports`` row per schedule × format) and
stays fast/display-less; the background worker builds the actual decks.

It also runs retention: generated reports older than ``REPORT_RETENTION_DAYS``
(default 90) are deleted (row + file) so the History list and the media tree don't
grow without bound.

Usage: ``python -m app.enqueue_reports`` (no arguments).
"""

import logging
import os
import re
import sqlite3
from datetime import date

from dotenv import load_dotenv

logger = logging.getLogger("reports.enqueue_reports")

_DEFAULT_RETENTION_DAYS = 90


def _connect() -> sqlite3.Connection:
    """Open a standalone SQLite connection with the schema ensured."""
    from app import db  # imported after load_dotenv so DATABASE_URL is honored

    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def _slug(name: str) -> str:
    """A filesystem-safe brand slug for the download filename."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "report").lower()).strip("-") or "report"


def enqueue_due(conn: sqlite3.Connection, today=None) -> int:
    """Enqueue every schedule due today (one queued row per format). Return the count.

    For each due schedule: derive the reporting period from its cadence (weekly →
    last week, monthly → last month), denormalize the brand + period labels onto the
    queued rows (so History stays meaningful even if the group is later renamed),
    and stamp ``last_run_on`` so a second timer firing today doesn't duplicate work.
    """
    from app import ci_analysis, report_schedules, report_store

    due = report_schedules.due_schedules(conn, today)
    enqueued = 0
    for s in due:
        period = report_schedules.period_for_cadence(s["cadence"])
        period_label = ci_analysis.period_label(period)
        generated_date = (today or date.today()).isoformat()
        for fmt in report_schedules.parse_formats(s["formats"]):
            filename = f"discotech-{_slug(s['brand_name'])}-{period}-{generated_date}.{fmt}"
            report_store.enqueue_report(
                conn, user_id=s["user_id"], group_id=s["group_id"],
                brand_label=s["brand_name"], period=period, period_label=period_label,
                fmt=fmt, filename=filename, schedule_id=s["id"])
            enqueued += 1
        report_schedules.mark_ran(conn, s["id"], today)
    logger.info("Scheduled reports: enqueued %d deck(s) across %d due schedule(s)",
                enqueued, len(due))
    return enqueued


def prune(conn: sqlite3.Connection) -> int:
    """Delete generated reports past the retention window. Return the count removed."""
    from app import report_store

    try:
        days = int(os.environ.get("REPORT_RETENTION_DAYS", _DEFAULT_RETENTION_DAYS))
    except ValueError:
        logger.warning("Invalid REPORT_RETENTION_DAYS; using default %d", _DEFAULT_RETENTION_DAYS)
        days = _DEFAULT_RETENTION_DAYS
    return report_store.prune_old_reports(conn, days)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    load_dotenv()
    conn = _connect()
    try:
        enqueue_due(conn)
        prune(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
