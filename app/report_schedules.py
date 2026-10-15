"""CRUD + due-date logic for scheduled (cadenced) business reports.

A schedule is a user's standing request to build a brand's deck every week or
every month, delivered to the Reports → History list. The daily enqueue job
(:mod:`app.enqueue_reports`, a systemd timer) asks :func:`due_schedules` which
ones fire today and enqueues the work; the worker builds it. This module owns the
``report_schedules`` table and the (pure, testable) cadence math; it writes no
files and talks to no queue.

Cadence drives both the firing day and the reported period: **weekly** fires on a
weekday and reports the last completed week (``wow``); **monthly** fires on a
day-of-month (1-28, capped so every month has it) and reports the last completed
month (``mom``). All SQL is parameterized; every write is ownership-scoped.
"""

import logging
import sqlite3
from datetime import date, timedelta

from app.report_store import FORMATS

logger = logging.getLogger(__name__)

CADENCES = ("weekly", "monthly")
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Day-of-month is capped at 28 so a schedule never silently skips a short month.
MAX_DAY_OF_MONTH = 28


class ScheduleError(ValueError):
    """Raised for an invalid schedule config (surfaced to the user as a 400)."""


def period_for_cadence(cadence: str) -> str:
    """The reporting period a cadence summarizes: weekly→last week, monthly→last month."""
    return "wow" if cadence == "weekly" else "mom"


def parse_formats(raw) -> list[str]:
    """Normalize a formats value (list or comma string) to a validated subset.

    Keeps declaration order of :data:`app.report_store.FORMATS`, dedupes, and drops
    anything unknown. Returns ``[]`` when nothing valid is present (the caller
    rejects that).
    """
    if isinstance(raw, str):
        requested = {f.strip().lower() for f in raw.split(",")}
    else:
        requested = {str(f).strip().lower() for f in (raw or [])}
    return [f for f in FORMATS if f in requested]


def _validate(cadence: str, day_of_week, day_of_month, formats: list[str]) -> None:
    """Validate a schedule's config; raise :class:`ScheduleError` on any bad field."""
    if cadence not in CADENCES:
        raise ScheduleError("Choose a weekly or monthly cadence.")
    if not formats:
        raise ScheduleError("Pick at least one format (PDF or PowerPoint).")
    if cadence == "weekly":
        if day_of_week is None or not (0 <= day_of_week <= 6):
            raise ScheduleError("Pick a day of the week.")
    else:  # monthly
        if day_of_month is None or not (1 <= day_of_month <= MAX_DAY_OF_MONTH):
            raise ScheduleError(f"Pick a day of the month (1-{MAX_DAY_OF_MONTH}).")


def create_schedule(conn: sqlite3.Connection, *, user_id: int, group_id: int, cadence: str,
                    day_of_week=None, day_of_month=None, formats=("pdf",)) -> int:
    """Insert a schedule after validating its config. Returns the new id.

    The caller has already verified the user owns ``group_id`` (IDOR). The unused
    day column for the chosen cadence is stored NULL.
    """
    fmts = parse_formats(formats)
    _validate(cadence, day_of_week, day_of_month, fmts)
    dow = day_of_week if cadence == "weekly" else None
    dom = day_of_month if cadence == "monthly" else None
    cur = conn.execute(
        "INSERT INTO report_schedules "
        "(user_id, group_id, cadence, day_of_week, day_of_month, formats, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (user_id, group_id, cadence, dow, dom, ",".join(fmts)),
    )
    conn.commit()
    logger.info("Created report schedule id=%s user_id=%s group_id=%s cadence=%s",
                cur.lastrowid, user_id, group_id, cadence)
    return cur.lastrowid


def update_schedule(conn: sqlite3.Connection, schedule_id: int, user_id: int, *,
                    group_id: int, cadence: str, day_of_week=None, day_of_month=None,
                    formats=("pdf",)) -> bool:
    """Update a schedule the user owns. Returns whether a row changed."""
    fmts = parse_formats(formats)
    _validate(cadence, day_of_week, day_of_month, fmts)
    dow = day_of_week if cadence == "weekly" else None
    dom = day_of_month if cadence == "monthly" else None
    updated = conn.execute(
        "UPDATE report_schedules SET group_id = ?, cadence = ?, day_of_week = ?, "
        "day_of_month = ?, formats = ?, updated_at = datetime('now') "
        "WHERE id = ? AND user_id = ?",
        (group_id, cadence, dow, dom, ",".join(fmts), schedule_id, user_id),
    )
    conn.commit()
    return updated.rowcount == 1


def get_schedule(conn: sqlite3.Connection, schedule_id: int, user_id: int) -> sqlite3.Row | None:
    """One schedule, scoped to its owner (IDOR gate)."""
    return conn.execute(
        "SELECT * FROM report_schedules WHERE id = ? AND user_id = ?",
        (schedule_id, user_id),
    ).fetchone()


def list_schedules(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """A user's schedules with the brand name joined, newest first."""
    return conn.execute(
        "SELECT s.*, g.name AS brand_name FROM report_schedules s "
        "JOIN ci_groups g ON g.id = s.group_id "
        "WHERE s.user_id = ? ORDER BY s.id DESC",
        (user_id,),
    ).fetchall()


def set_enabled(conn: sqlite3.Connection, schedule_id: int, user_id: int, enabled: bool) -> bool:
    """Pause/resume a schedule the user owns. Returns whether a row changed."""
    updated = conn.execute(
        "UPDATE report_schedules SET enabled = ?, updated_at = datetime('now') "
        "WHERE id = ? AND user_id = ?",
        (1 if enabled else 0, schedule_id, user_id),
    )
    conn.commit()
    return updated.rowcount == 1


def delete_schedule(conn: sqlite3.Connection, schedule_id: int, user_id: int) -> bool:
    """Delete a schedule the user owns. Returns whether a row went."""
    deleted = conn.execute(
        "DELETE FROM report_schedules WHERE id = ? AND user_id = ?",
        (schedule_id, user_id),
    )
    conn.commit()
    return deleted.rowcount == 1


def is_due(schedule: sqlite3.Row, today: date) -> bool:
    """Whether a schedule fires on ``today`` (enabled + cadence day matches).

    Dedupe (already ran today) is handled in SQL by :func:`due_schedules`; this is
    the pure calendar predicate so it's easy to unit-test.
    """
    if not schedule["enabled"]:
        return False
    if schedule["cadence"] == "weekly":
        return schedule["day_of_week"] == today.weekday()
    return schedule["day_of_month"] == today.day


def due_schedules(conn: sqlite3.Connection, today: date | None = None) -> list[sqlite3.Row]:
    """Enabled schedules that fire today and haven't been enqueued yet today.

    ``last_run_on`` dedupes within the day, so a timer that fires or retries more
    than once never double-enqueues a schedule. Joins the brand name for the
    enqueue log/filenames.
    """
    today = today or date.today()
    iso = today.isoformat()
    rows = conn.execute(
        "SELECT s.*, g.name AS brand_name FROM report_schedules s "
        "JOIN ci_groups g ON g.id = s.group_id "
        "WHERE s.enabled = 1 AND (s.last_run_on IS NULL OR s.last_run_on != ?)",
        (iso,),
    ).fetchall()
    return [r for r in rows if is_due(r, today)]


def mark_ran(conn: sqlite3.Connection, schedule_id: int, today: date | None = None) -> None:
    """Stamp a schedule's ``last_run_on`` so it isn't re-enqueued today."""
    conn.execute(
        "UPDATE report_schedules SET last_run_on = ? WHERE id = ?",
        ((today or date.today()).isoformat(), schedule_id),
    )
    conn.commit()


def describe(schedule) -> str:
    """Human cadence description, e.g. 'Weekly · Monday' or 'Monthly · day 15'."""
    if schedule["cadence"] == "weekly":
        dow = schedule["day_of_week"]
        name = WEEKDAY_NAMES[dow] if dow is not None and 0 <= dow <= 6 else "?"
        return f"Weekly · {name}"
    return f"Monthly · day {schedule['day_of_month']}"


def next_run_date(schedule, today: date | None = None) -> date:
    """The next calendar date this schedule will fire on or after ``today``."""
    today = today or date.today()
    if schedule["cadence"] == "weekly":
        delta = (schedule["day_of_week"] - today.weekday()) % 7
        return today + timedelta(days=delta)
    # Monthly: this month's day if it's today or later, else next month's.
    dom = schedule["day_of_month"]
    if today.day <= dom:
        return today.replace(day=dom)
    year, month = (today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1)
    return date(year, month, dom)
