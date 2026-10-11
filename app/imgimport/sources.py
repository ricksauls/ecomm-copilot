"""Classify a submitted product URL, resolve it to image URLs, and fetch bytes.

Phase 1 sources:
- **walmart**  — a Walmart product page; the existing headed-Chrome scraper yields
  the whole gallery (browser work, worker-only).
- **image**    — a direct image URL (a known image CDN or an image file extension),
  including Amazon image URLs, which are normalized to their native max resolution.
- **amazon_pdp** — an Amazon *product page*. Not supported in Phase 1 (reliable
  extraction needs a residential proxy — Phase 2); rejected with a clear message.
- **unknown**  — anything else; attempted as a direct image and rejected if the
  bytes aren't a decodable image.

Security (see security-standards): the download target is user-supplied, so every
fetch is SSRF-guarded — http/https only, and the host must resolve entirely to
public IPs (private/loopback/link-local/reserved/multicast are refused). Downloads
are size-capped and the bytes are verified to be a real image with Pillow rather
than trusting the server's Content-Type.
"""

import io
import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

WALMART = "walmart"
IMAGE = "image"
AMAZON_PDP = "amazon_pdp"
UNKNOWN = "unknown"

# Cap a single download so one hostile/huge URL can't exhaust memory/disk.
MAX_IMAGE_BYTES = 25 * 1024 * 1024
# Bound how many images we pull per product page (galleries past this are rare).
MAX_IMAGES_PER_ITEM = 15
_DOWNLOAD_TIMEOUT_S = 25
# A desktop UA — some CDNs 403 an empty/again UA.
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_IMAGE_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp)(?:$|\?)", re.IGNORECASE)
# Hosts whose paths serve raw images directly (so a query-only or extension-less
# path is still an image). Matched as a suffix of the hostname.
_IMAGE_HOST_SUFFIXES = (
    "media-amazon.com", "ssl-images-amazon.com", "images-amazon.com",
    "i5.walmartimages.com", "i5-walmartimages.com", "walmartimages.com",
)
_AMAZON_HOST_SUFFIXES = ("amazon.com", "amazon.co.uk", "amazon.ca", "amazon.de",
                         "amazon.fr", "amazon.es", "amazon.it", "amazon.co.jp",
                         "amazon.in", "amazon.com.mx", "amazon.com.au")
_AMAZON_PDP_PATH_RE = re.compile(r"/(dp|gp/product|gp/aw/d)/", re.IGNORECASE)
# Amazon bakes the rendition size into the filename, e.g.
# ``71abc._AC_SL1500_.jpg`` or ``...._SX300_SY300_.jpg``. Stripping the
# ``._<tokens>_`` segment before the extension yields the original max-res image.
_AMAZON_SIZE_TOKEN_RE = re.compile(
    r"\._[A-Za-z0-9,_\-]+_(?=\.(?:jpg|jpeg|png|gif|webp)\b)", re.IGNORECASE)

_PILLOW_FMT_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}


class SourceError(RuntimeError):
    """A user-facing, secret-free reason an item couldn't be acquired."""


class SourceNotSupported(SourceError):
    """A recognized but not-yet-implemented source (e.g. an Amazon product page)."""


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, suffixes) -> bool:
    return any(host == s or host.endswith("." + s) for s in suffixes)


def classify(url: str) -> str:
    """Bucket a URL into a source type (see module docstring)."""
    host = _host(url)
    path = urlparse(url).path if host else ""
    if not host:
        return UNKNOWN
    if _host_matches(host, _IMAGE_HOST_SUFFIXES) or _IMAGE_EXT_RE.search(url):
        return IMAGE
    if _host_matches(host, _AMAZON_HOST_SUFFIXES) and _AMAZON_PDP_PATH_RE.search(path):
        return AMAZON_PDP
    if host.endswith("walmart.com") and "/ip/" in path:
        return WALMART
    return UNKNOWN


def amazon_native_res(url: str) -> str:
    """Return the original-resolution Amazon image URL (size token stripped).

    No-op for non-Amazon image hosts and for URLs without a size token, so it's
    safe to call on any image URL.
    """
    if not _host_matches(_host(url), _IMAGE_HOST_SUFFIXES):
        return url
    return _AMAZON_SIZE_TOKEN_RE.sub("", url)


def is_safe_public_url(url: str) -> bool:
    """True only if ``url`` is http(s) and its host resolves entirely to public IPs.

    SSRF guard for the user-supplied download target: blocks private, loopback,
    link-local (incl. the cloud metadata endpoint 169.254.169.254), reserved,
    multicast and unspecified addresses. Note: this resolves DNS at check time, so
    a determined attacker could in theory rebind between this check and the fetch;
    acceptable for Phase 1 (public image CDNs), and a stricter pin-the-IP fetch is
    a later hardening.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    host = parsed.hostname
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        logger.info("Refusing unresolvable host for import URL: %r", host)
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            logger.warning("Refusing non-public IP %s for import host %r", ip, host)
            return False
    return True


def _probe_image(data: bytes) -> tuple[int, int, str]:
    """Return ``(width, height, ext)`` after verifying the bytes decode as an image.

    Trusting the server's Content-Type would let a non-image (or a disguised file)
    through, so the format is read from the bytes with Pillow. Raises
    :class:`SourceError` when the bytes aren't a supported image.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow is a runtime dep
        raise SourceError("Image processing is unavailable on the server.")
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            fmt = (im.format or "").upper()
            width, height = im.width, im.height
    except Exception:  # noqa: BLE001 - any decode failure means "not an image"
        raise SourceError("That URL didn't return a usable image.")
    ext = _PILLOW_FMT_EXT.get(fmt)
    if not ext:
        raise SourceError(f"Unsupported image format ({fmt or 'unknown'}).")
    return width, height, ext


def download_image(url: str) -> tuple[bytes, str, int, int]:
    """SSRF-safe download of one image. Returns ``(data, ext, width, height)``.

    Raises :class:`SourceError` (user-facing, secret-free) on a blocked URL, an
    oversize response, a transport error, or non-image bytes.
    """
    if not is_safe_public_url(url):
        raise SourceError("That URL is not an allowed public image address.")
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a runtime dep
        raise SourceError("The downloader is unavailable on the server.")
    try:
        resp = requests.get(url, timeout=_DOWNLOAD_TIMEOUT_S, stream=True,
                            headers={"User-Agent": _UA})
        resp.raise_for_status()
        chunks, total = [], 0
        for chunk in resp.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise SourceError("That image is larger than the 25 MB limit.")
            chunks.append(chunk)
    except SourceError:
        raise
    except Exception as e:  # noqa: BLE001 - requests raises a broad family
        logger.info("Import download failed url=%s: %s", url, e)
        raise SourceError("Couldn't download that image (the server didn't respond).")
    data = b"".join(chunks)
    width, height, ext = _probe_image(data)
    return data, ext, width, height


def resolve_image_urls(url: str, source_type: str) -> tuple[str | None, list[str]]:
    """Resolve a submitted URL to ``(label, [image URLs])`` to download.

    - ``walmart`` → scrape the gallery (browser; worker-only) + the product title.
    - ``image``   → the URL itself, normalized to native max-res.
    - ``unknown`` → attempted as a single direct image.
    - ``amazon_pdp`` → raises :class:`SourceNotSupported` (Phase 2 / needs a proxy).

    Raises :class:`SourceError`/:class:`SourceNotSupported` with a user-facing
    reason; the caller records it on the item.
    """
    if source_type == AMAZON_PDP:
        raise SourceNotSupported(
            "Amazon product-page import isn't available yet — paste the image URL "
            "directly for now (right-click the product image → Copy image address).")
    if source_type == WALMART:
        from app import fetch  # lazy: browser/Playwright import is heavy
        title, urls = fetch.fetch_all_image_urls(url)
        if not urls:
            raise SourceError("No images were found on that Walmart page.")
        return (title or None), urls[:MAX_IMAGES_PER_ITEM]
    # image / unknown: treat as a single direct image, native-res where applicable.
    return None, [amazon_native_res(url)]
