"""Smoke tests for the three built pages.

These assert the routes render and return the expected surface, not pixel
fidelity. They exist so CI catches template/route breakage on every push.
"""


def test_landing_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Verbatim hero copy from the design.
    assert b"Your eCommerce Team, Amplified." in resp.data


def test_signin_renders(client):
    resp = client.get("/signin")
    assert resp.status_code == 200
    assert b"Welcome back" in resp.data


def test_dashboard_renders(client):
    resp = client.get("/app")
    assert resp.status_code == 200
    assert b"Products losing ground" in resp.data
    # The worst-row gap should be present.
    assert b"\xe2\x88\x9217" in resp.data  # U+2212 17


def test_unknown_route_404(client):
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert b"This page isn't here." in resp.data


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in resp.headers
