"""Tests for the admin screens: authorization, nav, counts, notification.

Authorization is the important part — the admin routes must fail closed for
non-admins and the unauthenticated, regardless of whether the nav is shown.
"""

from app.db import get_db
from app.users import count_created_since, create_local_user, record_login

_ADMIN = "admin@example.com"
_PW = "password123"


def _as_admin(app):
    """Make ADMIN the only admin for this test app."""
    app.config["ADMIN_EMAILS"] = {_ADMIN}


def test_admin_routes_forbidden_for_regular_user(client, auth, app):
    _as_admin(app)
    auth.register(email="regular@example.com", password=_PW)  # not in the allowlist
    assert client.get("/admin/users").status_code == 403
    assert client.get("/admin/items").status_code == 403


def test_admin_routes_redirect_when_unauthenticated(client, app):
    _as_admin(app)
    resp = client.get("/admin/users")
    assert resp.status_code == 302
    assert "/signin" in resp.headers["Location"]


def test_admin_can_view_users_and_items(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    users_resp = client.get("/admin/users")
    assert users_resp.status_code == 200
    assert _ADMIN.encode() in users_resp.data
    assert client.get("/admin/items").status_code == 200


def test_admin_nav_visibility(client, auth, app):
    # Admin sees the Admin nav links; a regular user does not.
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    assert b"/admin/users" in client.get("/app").data

    auth.logout()
    auth.register(email="regular@example.com", password=_PW)
    assert b"/admin/users" not in client.get("/app").data


def test_count_created_since_is_bounded_and_excludes_self(app):
    with app.app_context():
        db = get_db()
        admin_id = create_local_user(_ADMIN, _PW)
        db.execute(
            "INSERT INTO users (email, created_at) VALUES ('old@x.com', '2026-01-01 00:00:00')"
        )
        db.execute(
            "INSERT INTO users (email, created_at) VALUES ('new@x.com', '2026-06-01 00:00:00')"
        )
        db.commit()
        # Reference mid-window: only new@x.com is "new".
        assert count_created_since("2026-03-01 00:00:00", admin_id) == 1
        # No reference (admin's first-ever login) counts nothing.
        assert count_created_since(None, admin_id) == 0


def test_record_login_rolls_last_into_prev(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user(_ADMIN, _PW)
        record_login(uid)
        first = db.execute(
            "SELECT last_login_at, prev_login_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
        assert first["last_login_at"] is not None
        assert first["prev_login_at"] is None  # first login has no prior

        # Force a distinct previous value, then log in again.
        db.execute(
            "UPDATE users SET last_login_at = '2026-01-01 00:00:00' WHERE id = ?", (uid,)
        )
        db.commit()
        record_login(uid)
        second = db.execute(
            "SELECT prev_login_at FROM users WHERE id = ?", (uid,)
        ).fetchone()
        assert second["prev_login_at"] == "2026-01-01 00:00:00"


def test_new_user_notification_renders_for_admin(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)  # logs the admin in
    with app.app_context():
        db = get_db()
        # Give the admin a reference window, and a user who signed up inside it.
        db.execute(
            "UPDATE users SET prev_login_at = '2026-01-01 00:00:00' WHERE email = ?",
            (_ADMIN,),
        )
        db.execute(
            "INSERT INTO users (email, created_at) VALUES ('newbie@x.com', '2026-06-01 00:00:00')"
        )
        db.commit()
    # The shell (topbar) should now show the new-user badge.
    resp = client.get("/app")
    assert b"1 new user" in resp.data
