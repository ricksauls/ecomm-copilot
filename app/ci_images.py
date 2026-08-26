"""Local cache for tracked-product main images (Competitive Intelligence).

Walmart product photos are hotlink-fragile and can't be embedded in the PDF, so
the worker downloads each tracked product's main image once and stores a small
JPEG here; the results page serves it from a same-origin ``/media`` route (the
strict CSP allows ``img-src 'self'``) and the PDF embeds the bytes.

Design notes:
- Keyed by the Walmart **item number** (all digits). The id is validated before
  it touches the filesystem so a crafted value can't escape the cache directory
  (path traversal — see security-standards).
- The cache lives *outside* the repo (next to the SQLite DB by default), so a
  ``git pull`` deploy never wipes it and images survive across releases.
- Every operation is best-effort and logged: a download or decode failure leaves
  the product imageless rather than failing the run or the page.
"""

import io
import logging
import os
import re

logger = logging.getLogger(__name__)

# Walmart item numbers are digits only; anchoring the whole string keeps a value
# like "../../etc" out of the filename we build from it.
_ITEM_ID_RE = re.compile(r"^\d+$")
# Thumbnails are shown small on the page and in the PDF — cap the long edge so the
# cache stays tiny and the PDF embeds quickly.
_MAX_EDGE_PX = 400
_JPEG_QUALITY = 85
_DOWNLOAD_TIMEOUT_S = 12


def _media_root() -> str:
    """Root media directory. Honors ``MEDIA_DIR``, else sits beside the DB file.

    Defaulting next to ``DATABASE_URL`` means the web app and the worker resolve
    the same location without extra config, and it lives outside the git checkout.
    """
    explicit = os.environ.get("MEDIA_DIR")
    if explicit:
        return explicit
    db_path = os.environ.get("DATABASE_URL") or "app.db"
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), "media")


def _product_dir() -> str:
    return os.path.join(_media_root(), "ci_products")


def product_image_path(item_id: str) -> str | None:
    """Absolute cache path for a product image, or ``None`` for an invalid id.

    Rejecting anything but digits is the path-traversal guard — callers can pass a
    raw item id straight through.
    """
    if item_id is None or not _ITEM_ID_RE.match(str(item_id)):
        return None
    return os.path.join(_product_dir(), f"{item_id}.jpg")


def has_product_image(item_id: str) -> bool:
    """Whether a cached image already exists for this item (skip re-fetching)."""
    path = product_image_path(item_id)
    return bool(path and os.path.isfile(path))


def save_product_image(item_id: str, data: bytes) -> bool:
    """Downscale and store image ``data`` as a JPEG in the cache. Best-effort.

    Transparency is flattened onto white (Walmart mains are white-bg) so a PNG
    with an alpha channel doesn't come out with a black background.
    """
    path = product_image_path(item_id)
    if not path:
        logger.warning("Refusing to cache image for invalid item_id=%r", item_id)
        return False
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a runtime dep
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with Image.open(io.BytesIO(data)) as img:
            if img.mode in ("RGBA", "LA", "P"):
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.split()[-1])
                img = flat
            else:
                img = img.convert("RGB")
            img.thumbnail((_MAX_EDGE_PX, _MAX_EDGE_PX))
            img.save(path, format="JPEG", quality=_JPEG_QUALITY)
        logger.info("Cached CI product image item_id=%s src_bytes=%d", item_id, len(data))
        return True
    except Exception:  # noqa: BLE001 - best-effort; a decode failure isn't fatal
        logger.exception("Failed to cache CI product image item_id=%s", item_id)
        return False


def cache_product_image_from_url(item_id: str, source_url: str) -> bool:
    """Download a product image and cache it. Best-effort; returns success.

    Walmart's image CDN (unlike its PDPs) serves plain HTTP requests, so no
    browser is needed for the download itself — only for discovering the URL.
    """
    if not source_url:
        return False
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a runtime dep
        return False
    try:
        resp = requests.get(source_url, timeout=_DOWNLOAD_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:  # noqa: BLE001 - network hiccup shouldn't fail the run
        logger.warning("Could not download CI product image item_id=%s url=%s: %s",
                       item_id, (source_url or "")[:80], e)
        return False
    return save_product_image(item_id, resp.content)


# ── Brand ad creatives (headline + sponsored video screenshots) ──────────────────

# Ad creatives are wider banners than product mains, so allow a larger long edge so
# the "Sponsored by <brand>" text stays legible on the page and in the PDF.
_AD_MAX_EDGE_PX = 900
_AD_TYPES = ("headline", "video")


def _ad_dir() -> str:
    return os.path.join(_media_root(), "ci_ads")


def ad_image_relpath(run_id: int, keyword_id: int, ad_type: str) -> str | None:
    """Relative cache path (under MEDIA_DIR) for an ad creative, or ``None`` if the
    inputs are unsafe. All parts are server-controlled — integer ids and a fixed
    ad-type enum — so a validated triple can't escape the cache directory.
    """
    if ad_type not in _AD_TYPES:
        return None
    try:
        run_id, keyword_id = int(run_id), int(keyword_id)
    except (TypeError, ValueError):
        return None
    return os.path.join("ci_ads", f"{run_id}_{keyword_id}_{ad_type}.jpg")


def ad_image_abspath(run_id: int, keyword_id: int, ad_type: str) -> str | None:
    """Absolute path for an ad creative (for the PDF to embed / the route to serve)."""
    rel = ad_image_relpath(run_id, keyword_id, ad_type)
    return os.path.join(_media_root(), rel) if rel else None


def save_ad_image(run_id: int, keyword_id: int, ad_type: str, data: bytes) -> str | None:
    """Downscale and store an ad-creative screenshot as a JPEG. Best-effort.

    Returns the relative path stored on the ci_ad_units row (so the page can serve
    it same-origin and the PDF can embed it), or ``None`` on any failure — a missed
    creative just means that ad shows without a thumbnail, never a failed run.
    """
    rel = ad_image_relpath(run_id, keyword_id, ad_type)
    if not rel or not data:
        logger.warning("Refusing to cache ad image for run=%s kw=%s type=%r",
                       run_id, keyword_id, ad_type)
        return None
    path = os.path.join(_media_root(), rel)
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a runtime dep
        return None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with Image.open(io.BytesIO(data)) as img:
            img = img.convert("RGB")  # screenshots are opaque PNGs
            img.thumbnail((_AD_MAX_EDGE_PX, _AD_MAX_EDGE_PX))
            img.save(path, format="JPEG", quality=_JPEG_QUALITY)
        logger.info("Cached CI ad image run=%s kw=%s type=%s src_bytes=%d",
                    run_id, keyword_id, ad_type, len(data))
        return rel
    except Exception:  # noqa: BLE001 - best-effort; a decode failure isn't fatal
        logger.exception("Failed to cache CI ad image run=%s kw=%s type=%s",
                         run_id, keyword_id, ad_type)
        return None
