"""Tests for the shared residential-proxy config helper."""

from app import proxy


def test_unset_is_none(monkeypatch):
    monkeypatch.delenv("WALMART_PROXY_SERVER", raising=False)
    assert proxy.proxy_from_env() is None
    monkeypatch.setenv("WALMART_PROXY_SERVER", "   ")
    assert proxy.proxy_from_env() is None


def test_server_only(monkeypatch):
    monkeypatch.setenv("WALMART_PROXY_SERVER", "http://pr.oxylabs.io:7777")
    monkeypatch.delenv("WALMART_PROXY_USERNAME", raising=False)
    assert proxy.proxy_from_env() == {"server": "http://pr.oxylabs.io:7777"}


def test_with_credentials(monkeypatch):
    monkeypatch.setenv("WALMART_PROXY_SERVER", "http://pr.oxylabs.io:7777")
    monkeypatch.setenv("WALMART_PROXY_USERNAME", "user")
    monkeypatch.setenv("WALMART_PROXY_PASSWORD", "secret")
    assert proxy.proxy_from_env() == {
        "server": "http://pr.oxylabs.io:7777", "username": "user", "password": "secret"}
