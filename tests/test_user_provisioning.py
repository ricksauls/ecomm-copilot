"""Admin-provisioned accounts + the forced first-login password change.

Covers: an admin creating a local account with a temporary password and role;
the gate that forces such an account to reset its password before reaching any
other page; and that the reset clears the flag and invalidates the temp
password. Self-service signup is unaffected (regression).
"""

from app import users
from app.db import get_db
from app.users import create_local_user

_ADMIN = "admin@example.com"
_PW = "password123"


def _as_admin(app):
    app.config["ADMIN_EMAILS"] = {_ADMIN}


def _admin_login(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)


# ── admin provisioning ───────────────────────────────────────────────────────

def test_admin_provisions_user_with_role_and_temp_password(client, auth, app):
    _admin_login(client, auth, app)
    resp = client.post("/admin/users", data={
        "email": "newhire@acme.com", "password": "temp-pass-123", "role": "user",
    })
    assert resp.status_code == 302
    with app.app_context():
        row = users.get_by_email("newhire@acme.com")
        assert row is not None
        assert row["role"] == "user"
        assert row["auth_provider"] == "local"
        assert row["must_change_password"] == 1          # forced reset pending
    # The list shows the pending indicator.
    page = client.get("/admin/users").data
    assert b"newhire@acme.com" in page
    assert b"Password pending" in page


def test_admin_can_provision_an_admin(client, auth, app):
    _admin_login(client, auth, app)
    client.post("/admin/users", data={
        "email": "boss@acme.com", "password": "temp-pass-123", "role": "admin",
    })
    with app.app_context():
        assert users.get_by_email("boss@acme.com")["role"] == "admin"


def test_provision_rejects_duplicate_email(client, auth, app):
    _admin_login(client, auth, app)
    with app.app_context():
        create_local_user("taken@acme.com", _PW)
    resp = client.post("/admin/users", data={
        "email": "taken@acme.com", "password": "temp-pass-123", "role": "user",
    }, follow_redirects=True)
    assert b"already registered" in resp.data
    with app.app_context():
        cnt = get_db().execute(
            "SELECT COUNT(*) FROM users WHERE email = ?", ("taken@acme.com",)
        ).fetchone()[0]
        assert cnt == 1


def test_provision_rejects_weak_password(client, auth, app):
    _admin_login(client, auth, app)
    resp = client.post("/admin/users", data={
        "email": "weak@acme.com", "password": "short", "role": "user",
    }, follow_redirects=True)
    assert b"at least 8 characters" in resp.data
    with app.app_context():
        assert users.get_by_email("weak@acme.com") is None


def test_provision_rejects_bad_role(client, auth, app):
    _admin_login(client, auth, app)
    resp = client.post("/admin/users", data={
        "email": "roley@acme.com", "password": "temp-pass-123", "role": "superuser",
    }, follow_redirects=True)
    assert b"valid role" in resp.data
    with app.app_context():
        assert users.get_by_email("roley@acme.com") is None


def test_provision_forbidden_for_non_admin(client, auth, app):
    _as_admin(app)
    auth.register(email="regular@example.com", password=_PW)  # not an admin
    assert client.post("/admin/users", data={
        "email": "x@acme.com", "password": "temp-pass-123", "role": "user",
    }).status_code == 403


# ── forced first-login password change ───────────────────────────────────────

def _provision(client, auth, app, email, temp="temp-pass-123", role="user"):
    """Admin provisions `email`, then signs back out so `email` can sign in."""
    _admin_login(client, auth, app)
    client.post("/admin/users", data={"email": email, "password": temp, "role": role})
    auth.logout()


def test_provisioned_user_is_forced_to_change_password(client, auth, app):
    _provision(client, auth, app, "forced@acme.com")
    auth.login(email="forced@acme.com", password="temp-pass-123")
    # Any app page bounces to the change-password screen while the flag is set.
    resp = client.get("/app")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/account/password")
    # The change-password page itself is reachable.
    assert client.get("/account/password").status_code == 200


def test_setting_password_clears_the_gate_and_invalidates_temp(client, auth, app):
    _provision(client, auth, app, "reset@acme.com")
    auth.login(email="reset@acme.com", password="temp-pass-123")
    resp = client.post("/account/password", data={
        "password": "my-own-secret-9", "confirm": "my-own-secret-9",
    })
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/app")
    with app.app_context():
        assert users.get_by_email("reset@acme.com")["must_change_password"] == 0
    # The app is now reachable (gate released).
    assert client.get("/app").status_code == 200
    # The old temporary password no longer works; the new one does.
    auth.logout()
    assert auth.login(email="reset@acme.com", password="temp-pass-123").status_code == 401
    assert auth.login(email="reset@acme.com",
                      password="my-own-secret-9").status_code == 302


def test_change_password_rejects_mismatch_and_keeps_flag(client, auth, app):
    _provision(client, auth, app, "mismatch@acme.com")
    auth.login(email="mismatch@acme.com", password="temp-pass-123")
    resp = client.post("/account/password", data={
        "password": "my-own-secret-9", "confirm": "different-secret-9",
    }, follow_redirects=True)
    assert b"don&#39;t match" in resp.data or b"don't match" in resp.data
    with app.app_context():
        assert users.get_by_email("mismatch@acme.com")["must_change_password"] == 1


def test_forced_user_can_still_sign_out(client, auth, app):
    _provision(client, auth, app, "leave@acme.com")
    auth.login(email="leave@acme.com", password="temp-pass-123")
    # Sign-out is exempt from the gate so a user is never trapped.
    assert auth.logout().status_code == 302


def test_change_password_requires_login(client):
    assert client.get("/account/password").status_code == 302
    assert client.post("/account/password", data={}).status_code == 302


# ── regressions: self-service accounts are not gated ─────────────────────────

def test_self_service_signup_is_not_forced_to_change(client, auth, app):
    auth.register(email="selfserve@acme.com", password=_PW)
    with app.app_context():
        assert users.get_by_email("selfserve@acme.com")["must_change_password"] == 0
    assert client.get("/app").status_code == 200  # no gate


def test_normal_user_can_voluntarily_change_password(client, auth, app):
    auth.register(email="voluntary@acme.com", password=_PW)
    resp = client.post("/account/password", data={
        "password": "brand-new-pass-1", "confirm": "brand-new-pass-1",
    })
    assert resp.status_code == 302
    auth.logout()
    assert auth.login(email="voluntary@acme.com",
                      password="brand-new-pass-1").status_code == 302
