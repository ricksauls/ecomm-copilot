"""Classify a submitted product URL, resolve it to image URLs, and fetch bytes.

Sources:
- **walmart**  — a Walmart product page; the headed-Chrome scraper (through the
  residential proxy) yields the whole gallery (browser work, worker-only).
- **amazon_pdp** — an Amazon product page; rendered through the proxy and parsed
  for its hi-res gallery (see :mod:`app.imgimport.amazon`).
- **image**    — a direct image URL (a known image CDN or an image file extension),
  including Amazon image URLs, normalized to their native max resolution.
- **unknown**  — anything else; attempted as a direct image and rejected if the
  bytes aren't a decodable image.

Resolution returns image *URLs* only (no bytes); the full-resolution file is fetched
later, on Download. Security (see security-standards): the download target is
user-supplied, so :func:`download_image` is SSRF-guarded — http/https only, and the
host must resolve entirely to public IPs (private/loopback/link-local/reserved/
multicast are refused) — size-capped, and the bytes are verified to be a real image
with Pillow rather than trusting the server's Content-Type.
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
# (Amazon size-token stripping lives in app.imgimport.amazon.strip_size_token.)

_PILLOW_FMT_EXT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}


class SourceError(RuntimeError):
    """A user-facing, secret-free reason an item couldn't be acquired."""


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
    from app.imgimport import amazon  # local import keeps sources→amazon one-way
    if not _host_matches(_host(url), _IMAGE_HOST_SUFFIXES):
        return url
    return amazon.strip_size_token(url)


def _walmart_thumb(url: str) -> str:
    """A lighter Walmart image URL for the gallery (i5 honors odn size params)."""
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}odnHeight=300&odnWidth=300&odnBg=FFFFFF"


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


def resolve_images(url: str, source_type: str) -> tuple[str | None, list[dict]]:
    """Resolve a submitted URL to ``(label, [image refs])`` — no bytes downloaded.

    Each ref is ``{"full", "thumb", "width", "height"}``: ``full`` is the
    highest-resolution URL (fetched only on Download), ``thumb`` a lighter URL the
    gallery hotlinks. By source:
    - ``walmart``    → scrape the gallery (proxied browser; worker-only) + title.
    - ``amazon_pdp`` → scrape the Amazon PDP (proxied) and extract hi-res + thumbs.
    - ``image``      → the URL itself (full = native max-res; thumb = as given).
    - ``unknown``    → treated as a single direct image.

    Raises :class:`SourceError` with a user-facing reason; the caller records it on
    the item. Page scrapes that get bot-blocked are translated to a friendly message.
    """
    if source_type == WALMART:
        from app import fetch  # lazy: browser/Playwright import is heavy
        try:
            title, urls = fetch.fetch_all_image_urls(url, use_proxy=True)
        except fetch.FetchBlocked:
            raise SourceError(
                "Walmart blocked the request (bot detection). Try again in a moment, "
                "or paste the product image URL directly (right-click the product "
                "image → Copy image address).")
        except fetch.FetchError:
            raise SourceError(
                "Couldn't read that Walmart page. Try again, or paste the product "
                "image URL directly (right-click the product image → Copy image address).")
        if not urls:
            raise SourceError("No images were found on that Walmart page.")
        refs = [{"full": u, "thumb": _walmart_thumb(u), "width": None, "height": None}
                for u in urls[:MAX_IMAGES_PER_ITEM]]
        return (title or None), refs

    if source_type == AMAZON_PDP:
        from app import fetch
        from app.imgimport import amazon
        try:
            title, refs = amazon.fetch_amazon_images(url)
        except fetch.FetchBlocked:
            raise SourceError(
                "Amazon blocked the request (bot detection). Try again in a moment, "
                "or paste the product image URL directly (right-click the product "
                "image → Copy image address).")
        except fetch.FetchError:
            raise SourceError(
                "Couldn't read that Amazon page. Try again, or paste the product "
                "image URL directly (right-click the product image → Copy image address).")
        if not refs:
            raise SourceError("No images were found on that Amazon page.")
        return (title or None), refs[:MAX_IMAGES_PER_ITEM]

    # image / unknown: a single direct image — full = native max-res, thumb = as given.
    return None, [{"full": amazon_native_res(url), "thumb": url, "width": None, "height": None}]
