"""Shared pytest fixtures.

Each test gets a fresh app backed by a throwaway SQLite file, CSRF disabled
(re-enabled explicitly in the CSRF test), and non-Secure cookies so the test
client's plain-HTTP requests keep their session.
"""

import os
import tempfile

import pytest

from app import create_app


@pytest.fixture
def app():
    """An app instance on a temporary database, torn down after the test."""
    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.environ["SECRET_KEY"] = "test-only-not-a-real-secret"
    os.environ["DATABASE_URL"] = db_path
    os.environ["SESSION_COOKIE_SECURE"] = "false"

    application = create_app()
    application.config["TESTING"] = True
    # Disable CSRF by default so form-post tests stay terse; the CSRF test flips
    # this back on to prove enforcement.
    application.config["CSRF_ENABLED"] = False

    yield application

    os.close(db_fd)
    os.unlink(db_path)


@pytest.fixture
def client(app):
    """A test client for the temporary app."""
    return app.test_client()


class AuthActions:
    """Convenience wrapper for the auth flows in tests."""

    def __init__(self, client):
        self._client = client

    def register(self, email="user@example.com", password="password123", **kw):
        return self._client.post(
            "/signup", data={"email": email, "password": password}, **kw
        )

    def login(self, email="user@example.com", password="password123", **kw):
        return self._client.post(
            "/signin", data={"email": email, "password": password}, **kw
        )

    def logout(self, **kw):
        return self._client.post("/signout", **kw)


@pytest.fixture
def auth(client):
    return AuthActions(client)
