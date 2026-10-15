"""Tests for the Oxylabs managed-scraping client (app.scraper_api).

No network is hit: requests.post/get are monkeypatched with a fake response, and
time.sleep is stubbed so retry backoff doesn't slow the suite — mirroring the
style in test_image_enhance.py.
"""

import pytest

from app import scraper_api
from app.fetch import FetchBlocked, FetchError

_HTML = (
    '<html><body><script id="__NEXT_DATA__" type="application/json">'
    '{"props":{"pageProps":{"initialData":{"data":{"product":{"name":"x"}}}}}}'
    "</script></body></html>"
)


class _FakeResp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Start every test with no Oxylabs credentials set."""
    for var in (
        "OXYLABS_SCRAPER_USERNAME", "OXYLABS_SCRAPER_PASSWORD", "OXYLABS_SCRAPER_URL",
        "OXYLABS_UNBLOCKER_USERNAME", "OXYLABS_UNBLOCKER_PASSWORD", "OXYLABS_UNBLOCKER_SERVER",
    ):
        monkeypatch.delenv(var, raising=False)
    # Never actually sleep during backoff.
    monkeypatch.setattr(scraper_api.time, "sleep", lambda *_: None)


def _set_scraper(monkeypatch):
    monkeypatch.setenv("OXYLABS_SCRAPER_USERNAME", "u")
    monkeypatch.setenv("OXYLABS_SCRAPER_PASSWORD", "p")


def _set_unblocker(monkeypatch):
    monkeypatch.setenv("OXYLABS_UNBLOCKER_USERNAME", "uu")
    monkeypatch.setenv("OXYLABS_UNBLOCKER_PASSWORD", "pp")


def test_scraper_enabled_gating(monkeypatch):
    assert scraper_api.scraper_enabled() is False
    _set_scraper(monkeypatch)
    assert scraper_api.scraper_enabled() is True


def test_scraper_enabled_with_only_unblocker(monkeypatch):
    _set_unblocker(monkeypatch)
    assert scraper_api.scraper_enabled() is True


def test_fetch_html_raises_when_unconfigured():
    with pytest.raises(FetchError):
        scraper_api.fetch_html("https://www.walmart.com/ip/1")


def test_fetch_html_scraper_api_success(monkeypatch):
    _set_scraper(monkeypatch)
    calls = {}

    def fake_post(url, json=None, auth=None, timeout=None, **kw):
        calls["url"] = url
        calls["json"] = json
        calls["auth"] = auth
        return _FakeResp(200, json_data={"results": [{"content": _HTML}]})

    monkeypatch.setattr("requests.post", fake_post)
    out = scraper_api.fetch_html("https://www.walmart.com/ip/123")

    assert out == _HTML
    assert calls["json"] == {
        "source": "universal",
        "url": "https://www.walmart.com/ip/123",
        "render": "html",
    }
    assert calls["auth"] == ("u", "p")


def test_fetch_html_falls_back_to_unblocker(monkeypatch):
    """Scraper API 500s on every retry → the Unblocker serves the HTML."""
    _set_scraper(monkeypatch)
    _set_unblocker(monkeypatch)

    def fake_post(url, json=None, auth=None, timeout=None, **kw):
        return _FakeResp(500, text="upstream error")

    def fake_get(url, proxies=None, headers=None, timeout=None, verify=None, **kw):
        assert verify is False  # Unblocker requires skipping TLS verification
        assert headers == {"x-oxylabs-render": "html"}
        return _FakeResp(200, text=_HTML)

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)

    assert scraper_api.fetch_html("https://www.walmart.com/ip/123") == _HTML


def test_fetch_html_block_raises_fetchblocked(monkeypatch):
    _set_scraper(monkeypatch)
    block_html = "<html><body>Robot or human? Activate and hold the button</body></html>"

    def fake_post(url, json=None, auth=None, timeout=None, **kw):
        return _FakeResp(200, json_data={"results": [{"content": block_html}]})

    monkeypatch.setattr("requests.post", fake_post)
    with pytest.raises(FetchBlocked):
        scraper_api.fetch_html("https://www.walmart.com/ip/123")


def test_fetch_html_all_providers_fail_raises_fetcherror(monkeypatch):
    _set_scraper(monkeypatch)

    def fake_post(url, json=None, auth=None, timeout=None, **kw):
        return _FakeResp(500, text="boom")

    monkeypatch.setattr("requests.post", fake_post)
    with pytest.raises(FetchError):
        scraper_api.fetch_html("https://www.walmart.com/ip/123")


# --- walmart_search (Phase 2) -------------------------------------------------

def _search_resp(items):
    """A fake realtime response wrapping walmart_search parsed content."""
    return _FakeResp(200, json_data={"results": [{"content": {"results": items}}]})


def test_fetch_walmart_search_requires_scraper_creds(monkeypatch):
    # Unblocker-only config does not enable the parsed-search path.
    _set_unblocker(monkeypatch)
    with pytest.raises(FetchError):
        scraper_api.fetch_walmart_search("hot sauce")


def test_fetch_walmart_search_returns_results_sorted_by_pos(monkeypatch):
    _set_scraper(monkeypatch)
    items = [
        {"general": {"title": "C", "pos": 3}},
        {"general": {"title": "A", "pos": 1}},
        {"general": {"title": "B", "pos": 2}},
    ]
    captured = {}

    def fake_post(url, json=None, auth=None, timeout=None, **kw):
        captured["json"] = json
        return _search_resp(items)

    monkeypatch.setattr("requests.post", fake_post)
    out = scraper_api.fetch_walmart_search("hot sauce")
    assert [r["general"]["title"] for r in out] == ["A", "B", "C"]
    assert captured["json"] == {"source": "walmart_search", "query": "hot sauce", "parse": True}


def test_fetch_walmart_search_non_200_raises(monkeypatch):
    _set_scraper(monkeypatch)
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(500, text="boom"))
    with pytest.raises(FetchError):
        scraper_api.fetch_walmart_search("hot sauce")


def test_search_enabled_gating(monkeypatch):
    assert scraper_api.search_enabled() is False
    _set_unblocker(monkeypatch)
    assert scraper_api.search_enabled() is False  # unblocker alone doesn't count
    _set_scraper(monkeypatch)
    assert scraper_api.search_enabled() is True
