"""In-app "Contact Us" messaging between users and the admin team.

A *thread* is one topic a user raised (a question, an issue, a customization
request); *messages* are the back-and-forth within it. The two admins share one
inbox, so read state is tracked per **side** — ``user_last_read_at`` and
``admin_last_read_at`` on the thread — which drives each side's unread badge.

Every function takes an explicit ``sqlite3.Connection`` and parameterizes all SQL
(never build query strings from input — see security-standards). User-facing reads
and mutations are scoped by ``user_id`` and verify thread ownership before
returning or writing anything — that ownership check is the IDOR boundary for the
whole feature; admin-side functions are unscoped and rely on ``admin_required`` at
the route.
"""

import logging
import sqlite3

logger = logging.getLogger(__name__)

# Triage categories offered on the contact form (allowlist — anything else is
# rejected rather than stored).
CATEGORIES = ("question", "issue", "customization", "other")
_CATEGORY_LABELS = {
    "question": "Question",
    "issue": "Issue",
    "customization": "Customization",
    "other": "Other",
}

MAX_SUBJECT_LEN = 200
MAX_BODY_LEN = 5000

# Sides of a conversation. The role decides which read-state column a view/reply
# touches and which messages count as "unread" for a given viewer.
ROLE_USER = "user"
ROLE_ADMIN = "admin"


class MessageError(ValueError):
    """A message mutation was rejected (bad input or not authorized)."""


def category_label(category: str) -> str:
    """Human label for a stored category slug (falls back to the slug)."""
    return _CATEGORY_LABELS.get(category, category)


def _clean(text: str | None, *, max_len: int, field: str) -> str:
    """Trim and length-check a required free-text field."""
    value = (text or "").strip()
    if not value:
        raise MessageError(f"{field} is required.")
    if len(value) > max_len:
        raise MessageError(f"{field} must be {max_len} characters or fewer.")
    return value


# ── create / reply ───────────────────────────────────────────────────────────

def create_thread(conn: sqlite3.Connection, user_id: int, subject: str,
                  category: str, body: str) -> int:
    """Open a new thread with its first (user) message; return the thread id."""
    subject = _clean(subject, max_len=MAX_SUBJECT_LEN, field="Subject")
    body = _clean(body, max_len=MAX_BODY_LEN, field="Message")
    if category not in CATEGORIES:
        raise MessageError("Pick a valid category.")

    cur = conn.execute(
        "INSERT INTO message_threads (user_id, subject, category) VALUES (?, ?, ?)",
        (user_id, subject, category),
    )
    thread_id = int(cur.lastrowid)
    msg = conn.execute(
        "INSERT INTO messages (thread_id, sender_id, sender_role, body) VALUES (?, ?, ?, ?)",
        (thread_id, user_id, ROLE_USER, body),
    )
    # The author has, by definition, read their own opening message.
    conn.execute(
        "UPDATE message_threads SET user_last_read_msg_id = ? WHERE id = ?",
        (int(msg.lastrowid), thread_id),
    )
    conn.commit()
    logger.info("Message thread opened id=%s user_id=%s category=%s", thread_id, user_id, category)
    return thread_id


def post_reply(conn: sqlite3.Connection, thread_id: int, sender_id: int,
               role: str, body: str) -> int:
    """Append a reply and bump the thread's activity. Returns the message id.

    Marks the thread read for the *sender's* side (they've clearly seen it) and
    reopens a closed thread on a user reply so a follow-up isn't lost.
    """
    body = _clean(body, max_len=MAX_BODY_LEN, field="Message")
    if role not in (ROLE_USER, ROLE_ADMIN):
        raise MessageError("Unknown sender role.")

    cur = conn.execute(
        "INSERT INTO messages (thread_id, sender_id, sender_role, body) VALUES (?, ?, ?, ?)",
        (thread_id, sender_id, role, body),
    )
    msg_id = int(cur.lastrowid)
    read_col = "user_last_read_msg_id" if role == ROLE_USER else "admin_last_read_msg_id"
    reopen = ", status = 'open'" if role == ROLE_USER else ""
    conn.execute(
        f"UPDATE message_threads SET last_message_at = datetime('now'), "
        f"{read_col} = ?{reopen} WHERE id = ?",
        (msg_id, thread_id),
    )
    conn.commit()
    logger.info("Message posted thread_id=%s role=%s sender_id=%s", thread_id, role, sender_id)
    return msg_id


def set_status(conn: sqlite3.Connection, thread_id: int, status: str) -> None:
    """Admin: close or reopen a thread."""
    if status not in ("open", "closed"):
        raise MessageError("Invalid status.")
    conn.execute("UPDATE message_threads SET status = ? WHERE id = ?", (status, thread_id))
    conn.commit()
    logger.info("Message thread id=%s status=%s", thread_id, status)


def mark_read(conn: sqlite3.Connection, thread_id: int, role: str) -> None:
    """Advance a side's read marker to the newest message (it opened the thread)."""
    col = "user_last_read_msg_id" if role == ROLE_USER else "admin_last_read_msg_id"
    conn.execute(
        f"UPDATE message_threads SET {col} = "
        "COALESCE((SELECT MAX(id) FROM messages WHERE thread_id = ?), 0) WHERE id = ?",
        (thread_id, thread_id),
    )
    conn.commit()


# ── reads ────────────────────────────────────────────────────────────────────

# A thread is "unread" for a side when a message *from the other side* has an id
# greater than that side's last-read marker. Ids are monotonic, so this is exact
# even for messages created in the same second (which a timestamp compare misses).
_UNREAD_FOR_ADMIN = (
    "EXISTS (SELECT 1 FROM messages m WHERE m.thread_id = t.id "
    "        AND m.sender_role = 'user' AND m.id > t.admin_last_read_msg_id)"
)
_UNREAD_FOR_USER = (
    "EXISTS (SELECT 1 FROM messages m WHERE m.thread_id = t.id "
    "        AND m.sender_role = 'admin' AND m.id > t.user_last_read_msg_id)"
)


def get_thread_for_user(conn: sqlite3.Connection, thread_id: int,
                        user_id: int) -> sqlite3.Row | None:
    """Return the thread only if it belongs to ``user_id`` (the IDOR gate)."""
    return conn.execute(
        "SELECT * FROM message_threads WHERE id = ? AND user_id = ?",
        (thread_id, user_id),
    ).fetchone()


def get_thread(conn: sqlite3.Connection, thread_id: int) -> sqlite3.Row | None:
    """Return a thread joined to its owner's email (admin view; unscoped)."""
    return conn.execute(
        "SELECT t.*, u.email AS user_email FROM message_threads t "
        "JOIN users u ON u.id = t.user_id WHERE t.id = ?",
        (thread_id,),
    ).fetchone()


def list_messages(conn: sqlite3.Connection, thread_id: int) -> list[sqlite3.Row]:
    """All messages in a thread, oldest first, with the sender's email."""
    return conn.execute(
        "SELECT m.*, u.email AS sender_email FROM messages m "
        "LEFT JOIN users u ON u.id = m.sender_id "
        "WHERE m.thread_id = ? ORDER BY m.id",
        (thread_id,),
    ).fetchall()


def list_threads_for_user(conn: sqlite3.Connection, user_id: int) -> list[sqlite3.Row]:
    """The user's own threads, most-recently-active first, each with an unread flag."""
    return conn.execute(
        f"SELECT t.*, {_UNREAD_FOR_USER} AS unread "
        "FROM message_threads t WHERE t.user_id = ? "
        "ORDER BY t.last_message_at DESC, t.id DESC",
        (user_id,),
    ).fetchall()


def list_all_threads(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Admin inbox: every thread + owner email + unread flag, unread/newest first."""
    return conn.execute(
        f"SELECT t.*, u.email AS user_email, {_UNREAD_FOR_ADMIN} AS unread "
        "FROM message_threads t JOIN users u ON u.id = t.user_id "
        "ORDER BY unread DESC, t.last_message_at DESC, t.id DESC"
    ).fetchall()


def count_unread_for_user(conn: sqlite3.Connection, user_id: int) -> int:
    """How many of the user's threads have unseen admin replies (badge count)."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM message_threads t "
        f"WHERE t.user_id = ? AND {_UNREAD_FOR_USER}",
        (user_id,),
    ).fetchone()
    return int(row[0])


def count_unread_for_admin(conn: sqlite3.Connection) -> int:
    """How many threads have unseen user messages (shared admin badge count)."""
    row = conn.execute(
        f"SELECT COUNT(*) FROM message_threads t WHERE {_UNREAD_FOR_ADMIN}"
    ).fetchone()
    return int(row[0])
