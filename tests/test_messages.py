"""Tests for the in-app messaging model (app/messages.py).

Focus: thread/reply lifecycle, per-side unread accounting (including the
same-second case that a timestamp compare would miss), ownership (IDOR), input
validation, and close/reopen.
"""

import pytest

from app import messages
from app.db import get_db
from app.messages import MessageError
from app.users import create_local_user


def _two_users(db):
    return (create_local_user("owner@example.com", "password123"),
            create_local_user("admin@example.com", "password123"))


def test_create_thread_is_unread_for_admin_only(app):
    with app.app_context():
        db = get_db()
        uid, _ = _two_users(db)
        tid = messages.create_thread(db, uid, "CSV export?", "customization", "Please add it.")
        assert tid > 0
        assert messages.count_unread_for_admin(db) == 1   # a new user message
        assert messages.count_unread_for_user(db, uid) == 0  # author read their own
        msgs = messages.list_messages(db, tid)
        assert len(msgs) == 1 and msgs[0]["sender_role"] == "user"


def test_admin_reply_flips_unread_to_user_even_same_second(app):
    # The read marker is a message id, not a timestamp, so a reply created in the
    # same second as the thread still counts as unread for the user.
    with app.app_context():
        db = get_db()
        uid, admin = _two_users(db)
        tid = messages.create_thread(db, uid, "Q", "question", "Hi")
        messages.post_reply(db, tid, admin, "admin", "Here's an answer.")
        assert messages.count_unread_for_user(db, uid) == 1
        assert messages.count_unread_for_admin(db) == 0  # admin's own reply is read
        messages.mark_read(db, tid, "user")
        assert messages.count_unread_for_user(db, uid) == 0


def test_user_reply_flips_unread_back_to_admin(app):
    with app.app_context():
        db = get_db()
        uid, admin = _two_users(db)
        tid = messages.create_thread(db, uid, "Q", "question", "Hi")
        messages.mark_read(db, tid, "admin")               # admin catches up
        assert messages.count_unread_for_admin(db) == 0
        messages.post_reply(db, tid, uid, "user", "One more thing")
        assert messages.count_unread_for_admin(db) == 1


def test_user_reply_reopens_a_closed_thread(app):
    with app.app_context():
        db = get_db()
        uid, _ = _two_users(db)
        tid = messages.create_thread(db, uid, "Q", "issue", "Broken")
        messages.set_status(db, tid, "closed")
        assert messages.get_thread(db, tid)["status"] == "closed"
        messages.post_reply(db, tid, uid, "user", "Still broken")
        assert messages.get_thread(db, tid)["status"] == "open"


def test_get_thread_for_user_is_ownership_scoped(app):
    with app.app_context():
        db = get_db()
        owner, other = _two_users(db)
        tid = messages.create_thread(db, owner, "Private", "other", "secret")
        assert messages.get_thread_for_user(db, tid, owner) is not None
        assert messages.get_thread_for_user(db, tid, other) is None   # IDOR gate


def test_list_threads_scoped_and_flagged(app):
    with app.app_context():
        db = get_db()
        owner, other = _two_users(db)
        t1 = messages.create_thread(db, owner, "Mine", "question", "hi")
        messages.create_thread(db, other, "Theirs", "question", "hi")
        mine = messages.list_threads_for_user(db, owner)
        assert [t["id"] for t in mine] == [t1]
        # unread flag reflects an unseen admin reply.
        assert mine[0]["unread"] == 0
        messages.post_reply(db, t1, other, "admin", "reply")
        assert messages.list_threads_for_user(db, owner)[0]["unread"] == 1


def test_list_all_threads_orders_unread_first(app):
    with app.app_context():
        db = get_db()
        uid, admin = _two_users(db)
        read_t = messages.create_thread(db, uid, "Read one", "question", "a")
        messages.mark_read(db, read_t, "admin")            # no longer unread
        unread_t = messages.create_thread(db, uid, "Unread one", "question", "b")
        rows = messages.list_all_threads(db)
        assert rows[0]["id"] == unread_t and rows[0]["unread"] == 1
        assert all("user_email" in r.keys() for r in rows)


@pytest.mark.parametrize("subject,category,body", [
    ("", "question", "body"),               # empty subject
    ("subj", "question", ""),               # empty body
    ("subj", "bogus", "body"),              # category not allowlisted
])
def test_create_thread_validation(app, subject, category, body):
    with app.app_context():
        db = get_db()
        uid = create_local_user("v@example.com", "password123")
        with pytest.raises(MessageError):
            messages.create_thread(db, uid, subject, category, body)


# ── admin-initiated broadcast (admin composes TO users) ──────────────────────

def test_broadcast_creates_one_private_thread_per_recipient(app):
    with app.app_context():
        db = get_db()
        admin = create_local_user("admin@example.com", "password123")
        r1 = create_local_user("r1@example.com", "password123")
        r2 = create_local_user("r2@example.com", "password123")
        tids = messages.broadcast_from_admin(
            db, [r1, r2], admin, "Scheduled maintenance", "We'll be down Sunday.")
        assert len(tids) == 2
        # Each thread belongs to its recipient, opens with an admin message, and
        # carries the announcement category.
        owners = []
        for tid in tids:
            thread = messages.get_thread(db, tid)
            owners.append(thread["user_id"])
            assert thread["category"] == messages.CATEGORY_ANNOUNCEMENT
            msgs = messages.list_messages(db, tid)
            assert len(msgs) == 1 and msgs[0]["sender_role"] == "admin"
            assert msgs[0]["body"] == "We'll be down Sunday."
        assert set(owners) == {r1, r2}
        # Unread for each recipient, read for the admin side (they wrote it).
        assert messages.count_unread_for_user(db, r1) == 1
        assert messages.count_unread_for_user(db, r2) == 1
        assert messages.count_unread_for_admin(db) == 0


def test_broadcast_to_single_recipient(app):
    with app.app_context():
        db = get_db()
        admin = create_local_user("admin@example.com", "password123")
        r1 = create_local_user("r1@example.com", "password123")
        tids = messages.broadcast_from_admin(db, [r1], admin, "Hi", "Just you.")
        assert len(tids) == 1
        assert messages.count_unread_for_user(db, r1) == 1


def test_broadcast_dedupes_repeated_recipients(app):
    # A doubled id (e.g. a mangled form post) must not send twice.
    with app.app_context():
        db = get_db()
        admin = create_local_user("admin@example.com", "password123")
        r1 = create_local_user("r1@example.com", "password123")
        tids = messages.broadcast_from_admin(db, [r1, r1, r1], admin, "S", "B")
        assert len(tids) == 1


def test_recipient_reply_stays_private_to_their_thread(app):
    # The whole point of per-recipient fan-out: one recipient's reply reaches the
    # admin but never the other recipients' threads.
    with app.app_context():
        db = get_db()
        admin = create_local_user("admin@example.com", "password123")
        r1 = create_local_user("r1@example.com", "password123")
        r2 = create_local_user("r2@example.com", "password123")
        t1, t2 = messages.broadcast_from_admin(db, [r1, r2], admin, "S", "B")
        messages.mark_read(db, t1, "admin")
        messages.mark_read(db, t2, "admin")
        messages.post_reply(db, t1, r1, "user", "Thanks!")
        assert messages.count_unread_for_admin(db) == 1          # only r1's thread
        assert len(messages.list_messages(db, t2)) == 1          # r2 untouched


@pytest.mark.parametrize("recipients,subject,body", [
    ([], "subj", "body"),                   # no recipients
    ([1], "", "body"),                      # empty subject
    ([1], "subj", ""),                      # empty body
])
def test_broadcast_validation(app, recipients, subject, body):
    with app.app_context():
        db = get_db()
        admin = create_local_user("admin@example.com", "password123")
        with pytest.raises(MessageError):
            messages.broadcast_from_admin(db, recipients, admin, subject, body)


def test_broadcast_rejects_unknown_recipient(app):
    with app.app_context():
        db = get_db()
        admin = create_local_user("admin@example.com", "password123")
        r1 = create_local_user("r1@example.com", "password123")
        with pytest.raises(MessageError):
            messages.broadcast_from_admin(db, [r1, 999999], admin, "S", "B")
        # Nothing was written (one transaction, all-or-nothing).
        assert db.execute("SELECT COUNT(*) FROM message_threads").fetchone()[0] == 0
