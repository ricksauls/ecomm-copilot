"""Tests for the admin screens: authorization, nav, counts, notification.

Authorization is the important part — the admin routes must fail closed for
non-admins and the unauthenticated, regardless of whether the nav is shown.
"""

from app import ci_config, ci_jobs, copy_jobs, jobs
from app.db import get_db
from app.users import (
    count_created_since,
    create_local_user,
    get_by_email,
    get_by_id,
    record_login,
)

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
    assert client.get("/admin/copy").status_code == 403
    assert client.get("/admin/ci-snapshots").status_code == 403
    assert client.get("/admin/ci-monitoring").status_code == 403


def test_admin_routes_redirect_when_unauthenticated(client, app):
    _as_admin(app)
    for path in ("/admin/users", "/admin/items", "/admin/copy",
                 "/admin/ci-snapshots", "/admin/ci-monitoring"):
        resp = client.get(path)
        assert resp.status_code == 302
        assert "/signin" in resp.headers["Location"]


def test_admin_can_view_users_and_items(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    users_resp = client.get("/admin/users")
    assert users_resp.status_code == 200
    assert _ADMIN.encode() in users_resp.data
    assert client.get("/admin/items").status_code == 200
    assert client.get("/admin/copy").status_code == 200


def test_admin_copy_lists_items_with_scores(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        uid = get_by_email(_ADMIN)["id"]
        ids = copy_jobs.enqueue_copy_items(
            db, uid, [{"url": "https://www.walmart.com/ip/7", "item": "7"}]
        )
        copy_jobs.save_current_copy(
            db, ids[0], title="Zesty Widget",
            current={"record": {"url": "u"}}, current_overall=61, keywords=[],
            next_status="fetched",
        )
        copy_jobs.save_generated_copy(
            db, ids[0], new={"title": "N", "bullets": ["b"], "description": "d"},
            projected_overall=90,
        )
    resp = client.get("/admin/copy")
    assert resp.status_code == 200
    assert b"Zesty Widget" in resp.data  # product name shown
    assert b"90" in resp.data            # projected score shown
    assert _ADMIN.encode() in resp.data  # submitter email shown


def test_admin_ci_snapshots_and_monitoring_screens(client, auth, app):
    # The two CI admin screens list snapshot runs and monitoring schedules across
    # users, each scoped to its own group mode.
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        uid = get_by_email(_ADMIN)["id"]
        snap = ci_config.create_group(db, uid, "Snap Group", mode="snapshot")
        rid = ci_jobs.enqueue_run(db, snap, "one_time")
        ci_jobs.finish_run(db, rid)
        mon = ci_config.create_group(db, uid, "Mon Group", mode="monitoring")
        ci_config.set_monitoring(db, mon, uid, True)

    snaps = client.get("/admin/ci-snapshots")
    assert snaps.status_code == 200
    assert b"Snap Group" in snaps.data
    assert b"Mon Group" not in snaps.data          # monitoring group not listed here
    assert _ADMIN.encode() in snaps.data

    mons = client.get("/admin/ci-monitoring")
    assert mons.status_code == 200
    assert b"Mon Group" in mons.data
    assert b"Snap Group" not in mons.data          # snapshot group not listed here
    assert b"On" in mons.data                       # sweep enabled
    assert b"CST" in mons.data                       # next-run label present


def test_admin_nav_visibility(client, auth, app):
    # Admin sees the Admin nav links (incl. the new copy screen); a regular user
    # does not.
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    dash = client.get("/app").data
    assert b"/admin/users" in dash
    assert b"/admin/copy" in dash
    assert b"/admin/ci-snapshots" in dash
    assert b"/admin/ci-monitoring" in dash

    auth.logout()
    auth.register(email="regular@example.com", password=_PW)
    regular = client.get("/app").data
    assert b"/admin/copy" not in regular
    assert b"/admin/ci-snapshots" not in regular


def test_admin_can_delete_user_and_their_items(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        target = create_local_user("victim@example.com", _PW)
        jobs.enqueue_items(
            get_db(), target, [{"url": "https://www.walmart.com/ip/9", "item": "9"}]
        )
    resp = client.post(f"/admin/users/{target}/delete")
    assert resp.status_code == 302
    with app.app_context():
        db = get_db()
        assert get_by_id(target) is None
        assert db.execute(
            "SELECT COUNT(*) FROM scored_items WHERE user_id = ?", (target,)
        ).fetchone()[0] == 0


def test_admin_cannot_delete_own_account(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        me = get_by_email(_ADMIN)["id"]
    resp = client.post(f"/admin/users/{me}/delete")
    assert resp.status_code == 400
    with app.app_context():
        assert get_by_email(_ADMIN) is not None  # still there


def test_delete_forbidden_for_non_admin(client, auth, app):
    _as_admin(app)
    auth.register(email="regular@example.com", password=_PW)
    with app.app_context():
        victim = create_local_user("v@example.com", _PW)
    resp = client.post(f"/admin/users/{victim}/delete")
    assert resp.status_code == 403
    with app.app_context():
        assert get_by_id(victim) is not None  # untouched


def test_delete_button_shown_for_others_only(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        create_local_user("other@example.com", _PW)
    resp = client.get("/admin/users")
    # Two users (admin + other), but only the non-self row gets a Delete button.
    assert resp.data.count(b"btn-delete") == 1


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


def test_notification_falls_back_to_created_at_on_first_login(client, auth, app):
    # First tracked login (prev_login_at NULL) should still surface signups that
    # happened after the admin's own account was created.
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        db.execute(
            "UPDATE users SET prev_login_at = NULL, created_at = '2026-01-01 00:00:00' "
            "WHERE email = ?", (_ADMIN,),
        )
        db.execute(
            "INSERT INTO users (email, created_at) VALUES ('late@x.com', '2026-06-01 00:00:00')"
        )
        db.commit()
    assert b"1 new user" in client.get("/app").data


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
