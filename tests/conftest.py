"""Shared pytest fixtures."""

import os

import pytest

from app import create_app


@pytest.fixture
def client():
    """A test client with a throwaway SECRET_KEY set for the app factory."""
    os.environ.setdefault("SECRET_KEY", "test-only-not-a-real-secret")
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
