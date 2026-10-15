"""Persistence for generated business reports: the file on disk + its DB row.

Report bytes live under ``MEDIA_DIR/reports/<user_id>/<token>.<ext>`` — outside
the git checkout (so a deploy ``git pull`` never wipes them), beside the SQLite
DB, sharing the exact media root the CI image cache resolves (so the web app and
the background worker always agree on the location). The ``generated_reports``
row stores the *relative* path; the download route maps it back to an absolute
path and re-checks ownership before serving.

Every path component is server-controlled and validated (``user_id`` is an int,
the token is a hex UUID, the extension is a fixed allowlist), so a crafted value
can never escape the media directory (path traversal — see security-standards).
This mirrors :mod:`app.seasonal.storage`; it's a separate module so the reports
media subtree and the row CRUD stay scoped to this feature.
"""

import logging
import os
import re
import sqlite3
import uuid

from app import ci_images

logger = logging.getLogger(__name__)

# File formats a report can be stored as. Doubles as the extension allowlist, so
# an unexpected ``fmt`` can never become part of a filesystem path.
FORMATS = ("pdf", "pptx")

_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_ID_RE = re.compile(r"^\d+$")


def _root() -> str:
    """Absolute reports media root, under the shared MEDIA_DIR."""
    # Reuse the CI cache's media-root resolution so the worker and web app resolve
    # the identical location without duplicating the DATABASE_URL/MEDIA_DIR logic.
    return os.path.join(ci_images._media_root(), "reports")


def _rel_path(user_id, token: str, fmt: str) -> str | None:
    """Relative report path (under MEDIA_DIR), or ``None`` for unsafe inputs.

    Guards every component: a non-numeric user id, a token that isn't a 32-char
    hex UUID, or a format outside :data:`FORMATS` yields ``None`` rather than a
    path, so a caller can never be tricked into reading/writing outside the tree.
    """
    if user_id is None or not _ID_RE.match(str(user_id)):
        return None
    if not token or not _TOKEN_RE.match(token):
        return None
    if fmt not in FORMATS:
        return None
    return os.path.join("reports", str(user_id), f"{token}.{fmt}")


def abspath(rel_path: str) -> str | None:
    """Map a stored relative path back to an absolute one, refusing escapes.

    The returned path is guaranteed to sit inside the reports media root; a
    ``rel_path`` containing ``..`` or an absolute segment (a tampered DB value or
    a bug) resolves outside the root and returns ``None`` instead.
    """
    if not rel_path:
        return None
    root = _root()
    candidate = os.path.normpath(os.path.join(ci_images._media_root(), rel_path))
    # Confinement check: the normalized path must still be within reports/.
    if os.path.commonpath([candidate, root]) != root:
        logger.warning("Refusing report path outside media root: %r", rel_path)
        return None
    return candidate


def _write_report_file(user_id: int, fmt: str, data: bytes) -> str:
    """Write ``data`` to a fresh report file; return its relative path.

    The file is written under a server-generated hex token so two reports never
    collide. Raises ``ValueError`` for an unsafe path (our own inputs, but never
    trust a path blindly) and re-raises an I/O error after cleaning up any
    half-written file so a later download can't read a truncated deck.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported report format: {fmt!r}")
    rel = _rel_path(user_id, uuid.uuid4().hex, fmt)
    if rel is None:
        raise ValueError("Could not build a safe report path")
    dest = abspath(rel)
    if dest is None:
        raise ValueError("Resolved report path escaped the media root")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        with open(dest, "wb") as fh:
            fh.write(data)
    except OSError:
        logger.exception("Failed writing report file user_id=%s rel=%s", user_id, rel)
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return rel


def save_report(conn: sqlite3.Connection, *, user_id: int, group_id: int | None,
                brand_label: str, period: str, period_label: str | None, fmt: str,
                data: bytes, filename: str, source: str = "on_demand",
                schedule_id: int | None = None) -> int:
    """Write the report bytes to disk and insert a ``done`` ``generated_reports`` row.

    The synchronous (on-demand) path: the file is written first, then recorded, so
    the row always points at bytes that exist. On-demand reports are born ``seen``
    (the user just downloaded them). Returns the new row id.
    """
    rel = _write_report_file(user_id, fmt, data)
    cur = conn.execute(
        "INSERT INTO generated_reports "
        "(user_id, group_id, brand_label, period, period_label, fmt, source, "
        " schedule_id, filename, rel_path, size_bytes, status, seen) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done', 1)",
        (user_id, group_id, brand_label, period, period_label, fmt, source,
         schedule_id, filename, rel, len(data)),
    )
    conn.commit()
    report_id = cur.lastrowid
    logger.info("Saved report id=%s user_id=%s fmt=%s bytes=%d source=%s",
                report_id, user_id, fmt, len(data), source)
    return report_id


# ── Scheduled (async) generation ─────────────────────────────────────────────
# A scheduled report is enqueued as a 'queued' row with no file yet; the worker
# claims it ('running'), builds the deck, then finishes it ('done') or fails it
# ('error'). rel_path is '' until the file exists. Mirrors the other worker queues
# (single-process, conditional-UPDATE claim — see app.image_jobs).

_PROCESSING = "running"


def enqueue_report(conn: sqlite3.Connection, *, user_id: int, group_id: int,
                   brand_label: str, period: str, period_label: str | None, fmt: str,
                   filename: str, schedule_id: int | None = None) -> int:
    """Insert a ``queued`` scheduled-report row (no file yet); return its id.

    The file is written later by :func:`finish_report` once the worker builds it.
    Scheduled reports start ``seen = 0`` so the rail shows a "new" badge until the
    user opens History.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported report format: {fmt!r}")
    cur = conn.execute(
        "INSERT INTO generated_reports "
        "(user_id, group_id, brand_label, period, period_label, fmt, source, "
        " schedule_id, filename, rel_path, status, seen) "
        "VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?, '', 'queued', 0)",
        (user_id, group_id, brand_label, period, period_label, fmt, schedule_id, filename),
    )
    conn.commit()
    logger.info("Enqueued scheduled report id=%s user_id=%s group_id=%s fmt=%s",
                cur.lastrowid, user_id, group_id, fmt)
    return cur.lastrowid


def claim_next_report(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest ``queued`` report (-> ``running``), or None.

    Conditional UPDATE so a restarted worker can't double-build a row; a lost race
    retries the next candidate. FIFO by id.
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM generated_reports WHERE status = 'queued' ORDER BY id LIMIT 1"
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE generated_reports SET status = ? WHERE id = ? AND status = 'queued'",
            (_PROCESSING, candidate["id"]),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute("SELECT * FROM generated_reports WHERE id = ?",
                                (candidate["id"],)).fetchone()
        # Lost the race; try the next queued row.


def finish_report(conn: sqlite3.Connection, report_id: int, data: bytes) -> None:
    """Write a claimed report's bytes to disk and mark it ``done``."""
    row = conn.execute("SELECT user_id, fmt FROM generated_reports WHERE id = ?",
                       (report_id,)).fetchone()
    if row is None:
        logger.warning("finish_report: row %s vanished", report_id)
        return
    rel = _write_report_file(row["user_id"], row["fmt"], data)
    conn.execute(
        "UPDATE generated_reports SET status = 'done', rel_path = ?, size_bytes = ?, "
        "error = NULL WHERE id = ?",
        (rel, len(data), report_id),
    )
    conn.commit()
    logger.info("Finished scheduled report id=%s bytes=%d", report_id, len(data))


def fail_report(conn: sqlite3.Connection, report_id: int, error: str) -> None:
    """Mark a claimed report ``error`` with a short message."""
    conn.execute(
        "UPDATE generated_reports SET status = 'error', error = ? WHERE id = ?",
        (error[:500], report_id),
    )
    conn.commit()
    logger.warning("Scheduled report id=%s failed: %s", report_id, error[:200])


def reclaim_orphaned_reports(conn: sqlite3.Connection) -> int:
    """Fail any report still ``running`` at worker startup (its builder died).

    Single-process worker, so a ``running`` row at startup can only be orphaned —
    the process that claimed it was killed (deploy restart / OOM) before finishing.
    Marking it ``error`` lets the next scheduled run (or a manual rebuild) replace
    it rather than leaving it stuck. Returns the count reclaimed.
    """
    updated = conn.execute(
        "UPDATE generated_reports SET status = 'error', "
        "error = 'Interrupted — the worker restarted while building this report.' "
        "WHERE status = ?",
        (_PROCESSING,),
    )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned report(s) on startup", updated.rowcount)
    return updated.rowcount


# ── "New" badge + retention ──────────────────────────────────────────────────

def count_unseen_scheduled(conn: sqlite3.Connection, user_id: int) -> int:
    """How many completed scheduled reports the user hasn't opened yet (rail badge)."""
    return conn.execute(
        "SELECT COUNT(*) FROM generated_reports "
        "WHERE user_id = ? AND source = 'scheduled' AND status = 'done' AND seen = 0",
        (user_id,),
    ).fetchone()[0]


def mark_scheduled_seen(conn: sqlite3.Connection, user_id: int) -> None:
    """Clear the "new" flag on a user's scheduled reports (called when they view History)."""
    conn.execute(
        "UPDATE generated_reports SET seen = 1 "
        "WHERE user_id = ? AND source = 'scheduled' AND seen = 0",
        (user_id,),
    )
    conn.commit()


def prune_old_reports(conn: sqlite3.Connection, days: int = 90) -> int:
    """Delete generated reports (row + file) older than ``days``; return the count.

    Retention housekeeping for the daily enqueue run, so the media tree and the
    History list don't grow without bound. Files are removed best-effort; a missing
    file is not an error.
    """
    cutoff_rows = conn.execute(
        "SELECT id, rel_path FROM generated_reports "
        "WHERE created_at < datetime('now', ?)",
        (f"-{int(days)} days",),
    ).fetchall()
    removed = 0
    for row in cutoff_rows:
        conn.execute("DELETE FROM generated_reports WHERE id = ?", (row["id"],))
        dest = abspath(row["rel_path"]) if row["rel_path"] else None
        if dest and os.path.isfile(dest):
            try:
                os.remove(dest)
            except OSError:
                logger.warning("Pruned report row %s but could not remove file %s",
                               row["id"], dest)
        removed += 1
    conn.commit()
    if removed:
        logger.info("Pruned %d report(s) older than %d days", removed, days)
    return removed


def get_report(conn: sqlite3.Connection, report_id: int, user_id: int) -> sqlite3.Row | None:
    """Fetch one report row, scoped to its owner (IDOR gate).

    Returns ``None`` when the id doesn't exist or belongs to another user — the
    route turns that into a 404, never revealing that the id exists.
    """
    return conn.execute(
        "SELECT * FROM generated_reports WHERE id = ? AND user_id = ?",
        (report_id, user_id),
    ).fetchone()


def list_reports(conn: sqlite3.Connection, user_id: int, limit: int = 100) -> list[sqlite3.Row]:
    """Recent reports for a user, newest first — the History tab's rows."""
    return conn.execute(
        "SELECT * FROM generated_reports WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


def delete_report(conn: sqlite3.Connection, report_id: int, user_id: int) -> bool:
    """Delete a report's row and its file. Ownership-scoped; returns whether a row went.

    The row is removed first (inside the user's scope), then the file — a missing
    file is not an error (it may have been cleaned up already). Returns ``False``
    when nothing matched (unknown id or another user's report).
    """
    row = get_report(conn, report_id, user_id)
    if row is None:
        return False
    conn.execute("DELETE FROM generated_reports WHERE id = ? AND user_id = ?",
                 (report_id, user_id))
    conn.commit()
    dest = abspath(row["rel_path"])
    if dest and os.path.isfile(dest):
        try:
            os.remove(dest)
        except OSError:
            # The row is already gone; a stray file is harmless and gets swept by
            # retention later. Log it so a disk-permission problem is visible.
            logger.warning("Deleted report row id=%s but could not remove file %s",
                           report_id, dest)
    logger.info("Deleted report id=%s user_id=%s", report_id, user_id)
    return True
