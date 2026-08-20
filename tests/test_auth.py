"""Tests for sign-up, sign-in, sign-out, the login guard, and CSRF."""


def test_signup_creates_account_and_logs_in(client, auth):
    resp = auth.register()
    # Success redirects to the dashboard, now reachable because we're logged in.
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app")
    assert client.get("/app").status_code == 200


def test_signup_rejects_short_password(client, auth):
    resp = auth.register(password="short")
    assert resp.status_code == 400
    assert b"at least 8 characters" in resp.data.lower()
    # No session established on a rejected sign-up.
    assert client.get("/app").status_code == 302


def test_signup_duplicate_email_is_rejected(client, auth):
    auth.register(email="dupe@example.com")
    auth.logout()
    resp = auth.register(email="dupe@example.com")
    assert resp.status_code == 400
    assert b"already registered" in resp.data.lower()


def test_login_success(client, auth):
    auth.register(email="log@example.com", password="password123")
    auth.logout()
    resp = auth.login(email="log@example.com", password="password123")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app")


def test_login_wrong_password_is_generic(client, auth):
    auth.register(email="wp@example.com", password="password123")
    auth.logout()
    resp = auth.login(email="wp@example.com", password="wrongpassword")
    assert resp.status_code == 401
    assert b"incorrect" in resp.data.lower()


def test_login_unknown_email_is_generic(client, auth):
    resp = auth.login(email="nobody@example.com", password="password123")
    assert resp.status_code == 401
    assert b"incorrect" in resp.data.lower()


def test_dashboard_requires_login(client):
    resp = client.get("/app")
    assert resp.status_code == 302
    # Redirects to sign-in, preserving where we were headed.
    assert "/signin" in resp.headers["Location"]
    assert "next=/app" in resp.headers["Location"]


def test_logout_ends_session(client, auth):
    auth.register()
    assert client.get("/app").status_code == 200
    resp = auth.logout()
    assert resp.status_code == 302
    # After logout the guard kicks back in.
    assert client.get("/app").status_code == 302


def test_password_is_hashed_not_plaintext(app, client, auth):
    auth.register(email="hash@example.com", password="password123")
    with app.app_context():
        from app.db import get_db

        row = get_db().execute(
            "SELECT password_hash FROM users WHERE email = ?", ("hash@example.com",)
        ).fetchone()
    assert row is not None
    assert row["password_hash"]
    assert "password123" not in row["password_hash"]


def test_csrf_enforced_when_enabled(client):
    # Flip CSRF back on and post without a token: must be rejected.
    client.application.config["CSRF_ENABLED"] = True
    resp = client.post("/signup", data={"email": "x@example.com", "password": "password123"})
    assert resp.status_code == 400


def test_google_sso_route_404_when_unconfigured(client):
    # No GOOGLE_CLIENT_ID/SECRET in tests, so SSO is disabled.
    assert client.get("/auth/google").status_code == 404
