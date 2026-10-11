"""Tests for Import Product Images source handling: classify, native-res,
SSRF guard, SSRF-safe download, and URL resolution."""

import io

import pytest
from PIL import Image

from app.imgimport import sources


def _png_bytes(w=64, h=48) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResp:
    def __init__(self, data: bytes, status_ok=True):
        self._data = data
        self._ok = status_ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("HTTP 500")

    def iter_content(self, chunk_size):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


# --- classify ---------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.walmart.com/ip/10294528", sources.WALMART),
    ("https://www.walmart.com/ip/Tabasco/10294528", sources.WALMART),
    ("https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg", sources.IMAGE),
    ("https://example.com/photo.JPG", sources.IMAGE),
    ("https://i5.walmartimages.com/seo/x", sources.IMAGE),
    ("https://www.amazon.com/dp/B07XYZ", sources.AMAZON_PDP),
    ("https://www.amazon.com/gp/product/B07XYZ", sources.AMAZON_PDP),
    ("https://example.com/some/page", sources.UNKNOWN),
    ("not a url", sources.UNKNOWN),
])
def test_classify(url, expected):
    assert sources.classify(url) == expected


# --- amazon native resolution ----------------------------------------------

def test_amazon_native_res_strips_size_token():
    url = "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg"
    assert sources.amazon_native_res(url) == "https://m.media-amazon.com/images/I/71abc.jpg"


def test_amazon_native_res_strips_multi_token():
    url = "https://images-na.ssl-images-amazon.com/images/I/61x._SX300_SY300_.jpg"
    assert sources.amazon_native_res(url) == (
        "https://images-na.ssl-images-amazon.com/images/I/61x.jpg")


def test_amazon_native_res_noop_without_token_or_non_amazon():
    assert sources.amazon_native_res("https://m.media-amazon.com/images/I/71abc.jpg") == (
        "https://m.media-amazon.com/images/I/71abc.jpg")
    # A non-Amazon host is left untouched even if it has a token-like name.
    other = "https://cdn.example.com/71abc._AC_SL1500_.jpg"
    assert sources.amazon_native_res(other) == other


# --- SSRF guard -------------------------------------------------------------

def test_is_safe_public_url_rejects_non_http():
    assert sources.is_safe_public_url("ftp://example.com/x.jpg") is False
    assert sources.is_safe_public_url("file:///etc/passwd") is False


def test_is_safe_public_url_rejects_private_and_metadata(monkeypatch):
    def fake_getaddrinfo(host, *a, **k):
        mapping = {
            "metadata": "169.254.169.254",   # cloud metadata (link-local)
            "internal": "10.0.0.5",          # private
            "loop": "127.0.0.1",             # loopback
        }
        return [(2, 1, 6, "", (mapping.get(host, "8.8.8.8"), 0))]
    monkeypatch.setattr(sources.socket, "getaddrinfo", fake_getaddrinfo)
    assert sources.is_safe_public_url("http://metadata/latest") is False
    assert sources.is_safe_public_url("http://internal/x.jpg") is False
    assert sources.is_safe_public_url("http://loop/x.jpg") is False
    assert sources.is_safe_public_url("https://public-host/x.jpg") is True


def test_is_safe_public_url_rejects_unresolvable(monkeypatch):
    import socket as real_socket

    def boom(host, *a, **k):
        raise real_socket.gaierror("no such host")
    monkeypatch.setattr(sources.socket, "getaddrinfo", boom)
    assert sources.is_safe_public_url("https://nope.invalid/x.jpg") is False


# --- download ---------------------------------------------------------------

def test_download_image_happy_path(monkeypatch):
    monkeypatch.setattr(sources, "is_safe_public_url", lambda u: True)
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(_png_bytes(80, 60)))
    data, ext, w, h = sources.download_image("https://example.com/x.png")
    assert ext == "png" and w == 80 and h == 60 and len(data) > 0


def test_download_image_blocks_unsafe_url(monkeypatch):
    monkeypatch.setattr(sources, "is_safe_public_url", lambda u: False)
    with pytest.raises(sources.SourceError):
        sources.download_image("http://169.254.169.254/x.jpg")


def test_download_image_rejects_oversize(monkeypatch):
    monkeypatch.setattr(sources, "is_safe_public_url", lambda u: True)
    monkeypatch.setattr(sources, "MAX_IMAGE_BYTES", 100)
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(b"x" * 500))
    with pytest.raises(sources.SourceError):
        sources.download_image("https://example.com/big.png")


def test_download_image_rejects_non_image(monkeypatch):
    monkeypatch.setattr(sources, "is_safe_public_url", lambda u: True)
    monkeypatch.setattr("requests.get", lambda *a, **k: _FakeResp(b"<html>not an image</html>"))
    with pytest.raises(sources.SourceError):
        sources.download_image("https://example.com/notimage")


# --- resolve ----------------------------------------------------------------

def test_resolve_image_applies_native_res():
    # A direct Amazon image URL: full = native max-res; thumb = the given URL.
    label, refs = sources.resolve_images(
        "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg", sources.IMAGE)
    assert label is None and len(refs) == 1
    assert refs[0]["full"] == "https://m.media-amazon.com/images/I/71abc.jpg"
    assert refs[0]["thumb"] == "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg"


def test_resolve_walmart_uses_fetch_with_proxy(monkeypatch):
    seen = {}

    def fake(url, **k):
        seen.update(k)
        return ("Tabasco Sauce", ["https://i5.walmartimages.com/a.jpg",
                                  "https://i5.walmartimages.com/b.jpg"])
    monkeypatch.setattr("app.fetch.fetch_all_image_urls", fake)
    label, refs = sources.resolve_images("https://www.walmart.com/ip/10294528", sources.WALMART)
    assert label == "Tabasco Sauce" and len(refs) == 2
    assert seen.get("use_proxy") is True  # Import routes Walmart through the proxy
    assert all(r["full"].startswith("https://i5.walmartimages.com/") for r in refs)
    assert "odnHeight" in refs[0]["thumb"]  # a lighter gallery rendition


def test_resolve_walmart_empty_raises(monkeypatch):
    monkeypatch.setattr("app.fetch.fetch_all_image_urls", lambda url, **k: ("", []))
    with pytest.raises(sources.SourceError):
        sources.resolve_images("https://www.walmart.com/ip/10294528", sources.WALMART)


def test_resolve_walmart_bot_block_becomes_clean_sourceerror(monkeypatch):
    # A bot-detection block from the scraper must surface as a friendly SourceError
    # (not a raw "Unexpected error"), pointing at the direct-image-URL workaround.
    from app import fetch

    def blocked(url, **k):
        raise fetch.FetchBlocked("Bot detection blocked the request")
    monkeypatch.setattr("app.fetch.fetch_all_image_urls", blocked)
    with pytest.raises(sources.SourceError) as exc:
        sources.resolve_images("https://www.walmart.com/ip/10294528", sources.WALMART)
    msg = str(exc.value).lower()
    assert "blocked" in msg and "image url" in msg


def test_resolve_walmart_fetch_error_becomes_sourceerror(monkeypatch):
    from app import fetch

    def failing(url, **k):
        raise fetch.FetchError("page read failed")
    monkeypatch.setattr("app.fetch.fetch_all_image_urls", failing)
    with pytest.raises(sources.SourceError):
        sources.resolve_images("https://www.walmart.com/ip/10294528", sources.WALMART)


def test_resolve_amazon_pdp_uses_extractor(monkeypatch):
    refs = [{"full": "https://m.media-amazon.com/images/I/71x.jpg",
             "thumb": "https://m.media-amazon.com/images/I/71x._AC_SX300_.jpg",
             "width": None, "height": None}]
    monkeypatch.setattr("app.imgimport.amazon.fetch_amazon_images",
                        lambda url, **k: ("Deep Woods", refs))
    label, got = sources.resolve_images("https://www.amazon.com/dp/B07XYZ", sources.AMAZON_PDP)
    assert label == "Deep Woods" and got == refs


def test_resolve_amazon_pdp_block_becomes_clean_sourceerror(monkeypatch):
    from app import fetch

    def blocked(url, **k):
        raise fetch.FetchBlocked("blocked")
    monkeypatch.setattr("app.imgimport.amazon.fetch_amazon_images", blocked)
    with pytest.raises(sources.SourceError) as exc:
        sources.resolve_images("https://www.amazon.com/dp/B07XYZ", sources.AMAZON_PDP)
    assert "amazon" in str(exc.value).lower()
