"""Every screen that accepts a CSV upload offers a matching downloadable template.

These guard that each template route returns a well-formed CSV attachment with the
header and example rows that screen's parser expects.
"""

import pytest


# (route, expected filename, a header substring, an example-URL substring)
_TEMPLATES = [
    ("/app/portfolio/template.csv", "portfolio-template.csv", b"Walmart Item URL", b"walmart.com/ip/"),
    ("/app/pdp-scoring/template.csv", "content-scoring-template.csv", b"Walmart Item URL", b"walmart.com/ip/"),
    ("/app/pdp-copy/template.csv", "product-copy-template.csv", b"Walmart Item URL", b"walmart.com/ip/"),
    ("/app/import-images/template.csv", "acquire-images-template.csv", b"Product or Image URL", b"media-amazon.com"),
    ("/app/seasonal/template.csv", "seasonal-products-template.csv", b"Product URL", b"amazon.com/dp/"),
]


@pytest.mark.parametrize("route, filename, header, example", _TEMPLATES)
def test_csv_template_downloads(client, auth, route, filename, header, example):
    auth.register(email="tmpl@example.com", password="password123")
    resp = client.get(route)
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert filename in resp.headers["Content-Disposition"]
    assert header in resp.data
    assert example in resp.data


@pytest.mark.parametrize("route", [t[0] for t in _TEMPLATES])
def test_csv_template_requires_login(client, route):
    # Template routes are behind login_required like the screens they belong to.
    resp = client.get(route)
    assert resp.status_code in (302, 401)
