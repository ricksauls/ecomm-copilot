"""Amazon product-page image extraction for Import Product Images.

Amazon's PDP isn't a single JSON blob like Walmart's ``__NEXT_DATA__`` — the gallery
images live in an inline ``colorImages`` script (``"hiRes"`` / ``"large"`` URLs) and
in ``data-a-dynamic-image`` attributes. :func:`extract_amazon_images` is a pure
parser over the rendered HTML (unit-testable with fixtures); :func:`fetch_amazon_images`
renders the page first (headed browser **through the residential proxy** — Amazon
needs it from a datacenter IP) and then parses.

Each image is returned as a ref dict ``{"full", "thumb", "width", "height"}``:
- ``full``  — the original max-resolution URL (Amazon's size token stripped).
- ``thumb`` — a lightweight ~300px rendition derived from the same base, hotlinked
  in the gallery so we never download a full image just to display it.
Dimensions are left ``None`` (not needed without upscaling in v1).
"""

import html as html_lib
import json
import logging
import re

logger = logging.getLogger(__name__)

# Amazon bakes the rendition size into the filename, e.g. ``71abc._AC_SL1500_.jpg``;
# stripping the ``._<tokens>_`` segment before the extension yields the original.
_SIZE_TOKEN_RE = re.compile(
    r"\._[A-Za-z0-9,_\-]+_(?=\.(?:jpg|jpeg|png|gif|webp)\b)", re.IGNORECASE)
_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp)(\?.*)?$", re.IGNORECASE)
_IMG_URL_RE = re.compile(r"^https://[^\s\"']+\.(?:jpg|jpeg|png|gif|webp)(?:\?.*)?$",
                         re.IGNORECASE)
# Image URLs inside inline JS, e.g. "hiRes":"https://m.media-amazon.com/...jpg".
_HIRES_RE = re.compile(r'"hiRes"\s*:\s*"([^"]+)"')
_LARGE_RE = re.compile(r'"large"\s*:\s*"([^"]+)"')
_DYNAMIC_RE = re.compile(r'data-a-dynamic-image="([^"]+)"')
_TITLE_RE = re.compile(r'id="productTitle"[^>]*>(.*?)</', re.IGNORECASE | re.DOTALL)
# The default thumbnail rendition we build from a base URL.
_THUMB_TOKEN = "._AC_SX300_"


def strip_size_token(url: str) -> str:
    """Return the original-resolution Amazon image URL (size token removed)."""
    return _SIZE_TOKEN_RE.sub("", url)


def _thumb_from(full_url: str) -> str:
    """Build a light ~300px rendition URL from a token-free Amazon image URL."""
    return _EXT_RE.sub(lambda m: f"{_THUMB_TOKEN}.{m.group(1)}{m.group(2) or ''}", full_url)


def _clean_url(raw: str) -> str | None:
    """Normalize a URL scraped from inline JS (unescape ``\\/``) and validate it."""
    url = raw.replace("\\/", "/").strip()
    return url if _IMG_URL_RE.match(url) else None


def _title(markup: str) -> str | None:
    m = _TITLE_RE.search(markup)
    if not m:
        return None
    return html_lib.unescape(re.sub(r"\s+", " ", m.group(1)).strip()) or None


def extract_amazon_images(markup: str) -> tuple[str | None, list[dict]]:
    """Parse an Amazon PDP's rendered HTML into ``(title, [image refs])``.

    Pure function (no network). Prefers the ``hiRes`` gallery URLs, falls back to
    ``large``, then to the largest ``data-a-dynamic-image`` entry (the main image).
    Order-preserving and de-duplicated by the original-resolution URL.
    """
    title = _title(markup)
    seen: set[str] = set()
    refs: list[dict] = []

    def _add(raw: str) -> None:
        url = _clean_url(raw)
        if not url:
            return
        full = strip_size_token(url)
        if full in seen:
            return
        seen.add(full)
        refs.append({"full": full, "thumb": _thumb_from(full), "width": None, "height": None})

    for m in _HIRES_RE.finditer(markup):
        _add(m.group(1))
    if not refs:
        for m in _LARGE_RE.finditer(markup):
            _add(m.group(1))
    if not refs:
        # Fallback: the landing image's dynamic-image map (HTML-entity-encoded JSON
        # of {url: [w, h]}); take the largest by pixel area.
        for m in _DYNAMIC_RE.finditer(markup):
            try:
                mapping = json.loads(html_lib.unescape(m.group(1)))
            except (ValueError, TypeError):
                continue
            if isinstance(mapping, dict) and mapping:
                largest = max(
                    mapping.items(),
                    key=lambda kv: (kv[1][0] * kv[1][1]) if isinstance(kv[1], list)
                    and len(kv[1]) == 2 else 0)
                _add(largest[0])

    return title, refs


def fetch_amazon_images(url: str, *, timeout_ms: int = 35000) -> tuple[str | None, list[dict]]:
    """Render an Amazon PDP (proxied) and extract its image refs + title.

    Raises :class:`app.fetch.FetchBlocked` / :class:`app.fetch.FetchError` — the
    caller (:mod:`app.imgimport.sources`) converts those to a user-facing message.
    """
    from app import fetch  # lazy: heavy Playwright import

    logger.info("Fetching Amazon images url=%s", url)
    markup = fetch.render_page_html(url, timeout_ms=timeout_ms, use_proxy=True)
    return extract_amazon_images(markup)
