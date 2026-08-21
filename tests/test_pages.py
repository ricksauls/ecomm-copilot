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


def test_dashboard_renders_when_authenticated(client, auth):
    # Registering logs the user in, so the guarded dashboard is reachable.
    auth.register()
    resp = client.get("/app")
    assert resp.status_code == 200
    assert b"Products losing ground" in resp.data
    # The worst-row gap should be present.
    assert b"\xe2\x88\x9217" in resp.data  # U+2212 17


def test_pdp_scoring_page_renders(client, auth):
    # The guarded intake page renders with its heading, and no longer shows the
    # removed "Content Scoring" eyebrow or the topbar search / client-view UI.
    auth.register()
    resp = client.get("/app/pdp-scoring")
    assert resp.status_code == 200
    assert b"Score PDPs (Product Detail Pages)" in resp.data
    # The eyebrow line is gone (the rail nav item keeps its own label).
    assert b"(Product Detail Page) Content Scoring" not in resp.data
    assert b"Search products, brands" not in resp.data
    assert b"Client view" not in resp.data
    # "What we score" band reflects the paused dimensions: 4 cards, no Attributes
    # card, no video mention.
    assert resp.data.count(b'class="dim-name"') == 4
    assert b"four dimensions" in resp.data
    assert b">Attributes<" not in resp.data
    assert b"video" not in resp.data


def test_unknown_route_404(client):
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert b"This page isn't here." in resp.data


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in resp.headers
