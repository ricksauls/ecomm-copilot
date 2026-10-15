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


def save_report(conn: sqlite3.Connection, *, user_id: int, group_id: int | None,
                brand_label: str, period: str, period_label: str | None, fmt: str,
                data: bytes, filename: str, source: str = "on_demand",
                schedule_id: int | None = None) -> int:
    """Write the report bytes to disk and insert its ``generated_reports`` row.

    Returns the new row id. The file is written first (under a server-generated
    hex token, so two reports never collide) and only then recorded, so a row
    always points at bytes that exist. Raises on an unknown ``fmt`` or an I/O
    failure — the caller surfaces a safe error; a half-written file is cleaned up.
    """
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported report format: {fmt!r}")

    token = uuid.uuid4().hex
    rel = _rel_path(user_id, token, fmt)
    if rel is None:  # defensive: our own inputs, but never trust a path blindly
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
        # Don't leave a truncated file behind to confuse a later download.
        try:
            os.remove(dest)
        except OSError:
            pass
        raise

    cur = conn.execute(
        "INSERT INTO generated_reports "
        "(user_id, group_id, brand_label, period, period_label, fmt, source, "
        " schedule_id, filename, rel_path, size_bytes, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'done')",
        (user_id, group_id, brand_label, period, period_label, fmt, source,
         schedule_id, filename, rel, len(data)),
    )
    conn.commit()
    report_id = cur.lastrowid
    logger.info("Saved report id=%s user_id=%s fmt=%s bytes=%d source=%s",
                report_id, user_id, fmt, len(data), source)
    return report_id


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
