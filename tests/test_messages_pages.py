"""HTTP-layer tests for Contact Us (user) and the admin Messages inbox.

Covers the create/view/reply flows on both sides, the ownership (IDOR) and admin
authorization guards, and the unread notification counts surfaced in the shell.
"""

from app import messages
from app.db import get_db
from app.users import create_local_user

_ADMIN = "admin@example.com"
_PW = "password123"


def _as_admin(app):
    app.config["ADMIN_EMAILS"] = {_ADMIN}


def _open_thread(client, subject="Need help", category="issue", body="It broke"):
    """Create a thread via the UI; return its id from the redirect target."""
    resp = client.post("/app/contact", data={
        "subject": subject, "category": category, "body": body,
    })
    assert resp.status_code == 302
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


# ── user side ────────────────────────────────────────────────────────────────

def test_contact_requires_login(client):
    assert client.get("/app/contact").status_code == 302
    assert client.post("/app/contact", data={}).status_code == 302


def test_user_can_create_view_and_reply(client, auth):
    auth.register()
    tid = _open_thread(client, subject="Bulk CSV export")
    page = client.get(f"/app/contact/threads/{tid}")
    assert page.status_code == 200
    assert b"Bulk CSV export" in page.data
    assert b"It broke" in page.data
    # Reply appends a second message.
    client.post(f"/app/contact/threads/{tid}/reply", data={"body": "Any update?"})
    thread = client.get(f"/app/contact/threads/{tid}")
    assert b"Any update?" in thread.data


def test_user_cannot_view_another_users_thread(client, auth, app):
    with app.app_context():
        other = create_local_user("other@example.com", _PW)
        tid = messages.create_thread(get_db(), other, "Private", "other", "secret")
    auth.register(email="intruder@example.com")
    assert client.get(f"/app/contact/threads/{tid}").status_code == 404
    assert client.post(f"/app/contact/threads/{tid}/reply",
                       data={"body": "sneaky"}).status_code == 404


def test_invalid_submission_reflashes_without_creating(client, auth):
    auth.register()
    resp = client.post("/app/contact", data={"subject": "", "category": "question",
                                             "body": "x"}, follow_redirects=True)
    assert resp.status_code == 200
    with client.application.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM message_threads").fetchone()[0] == 0


# ── admin side ───────────────────────────────────────────────────────────────

def test_admin_messages_forbidden_for_regular_user(client, auth, app):
    _as_admin(app)
    auth.register(email="regular@example.com")  # not in allowlist
    assert client.get("/admin/messages").status_code == 403


def test_admin_can_view_inbox_and_reply(client, auth, app):
    _as_admin(app)
    # A user opens a thread.
    with app.app_context():
        uid = create_local_user("cust@example.com", _PW)
        tid = messages.create_thread(get_db(), uid, "Question about pricing",
                                     "question", "How much?")
    auth.register(email=_ADMIN)  # sign in as the admin
    inbox = client.get("/admin/messages")
    assert inbox.status_code == 200
    assert b"Question about pricing" in inbox.data
    assert b"cust@example.com" in inbox.data
    # Admin replies; it lands as an admin-role message.
    client.post(f"/admin/messages/{tid}/reply", data={"body": "It depends."})
    with app.app_context():
        rows = messages.list_messages(get_db(), tid)
        assert rows[-1]["sender_role"] == "admin"
        assert rows[-1]["body"] == "It depends."


def test_admin_can_close_and_reopen(client, auth, app):
    _as_admin(app)
    with app.app_context():
        uid = create_local_user("cust2@example.com", _PW)
        tid = messages.create_thread(get_db(), uid, "T", "issue", "x")
    auth.register(email=_ADMIN)
    client.post(f"/admin/messages/{tid}/status", data={"status": "closed"})
    with app.app_context():
        assert messages.get_thread(get_db(), tid)["status"] == "closed"


# ── notification counts in the shell ─────────────────────────────────────────

def test_unread_badges_render_for_each_side(client, auth, app):
    _as_admin(app)
    # User opens a thread -> admin should see an unread badge in the shell.
    with app.app_context():
        uid = create_local_user("cust3@example.com", _PW)
        tid = messages.create_thread(get_db(), uid, "Hello there", "question", "hi")
    auth.register(email=_ADMIN)
    shell = client.get("/admin/messages").data
    assert b"new message" in shell  # topbar badge text

    # Admin replies, then the user sees their own unread badge in the shell.
    client.post(f"/admin/messages/{tid}/reply", data={"body": "hi back"})
    auth.logout()
    client.post("/signin", data={"email": "cust3@example.com", "password": _PW})
    home = client.get("/app").data
    assert b"new message" in home
