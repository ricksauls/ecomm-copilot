"""Fetch and parse a Walmart PDP into a :class:`~app.scoring.PdpRecord`.

Reuses the approach proven in the WM share-of-voice scraper: a real Chromium
(Playwright) loads the product page and we read the server-rendered
``__NEXT_DATA__`` JSON blob (``props.pageProps.initialData.data.product``). A
direct ``requests`` fetch is blocked by Walmart's bot defense, so a real browser
is required.

The parsing (:func:`parse_product`) is pure and unit-tested. The fetching
(:func:`fetch_pdp`) drives a browser and imports Playwright lazily, so importing
this module never requires Playwright to be installed — the app runs fine
without it, and the scoring feature degrades to a clear error.
"""

import html
import logging
import re

from app.scoring import PdpRecord

logger = logging.getLogger(__name__)

# Walmart video module type names seen in contentLayout.modules (from the WM
# scraper; extend as new ones surface).
_VIDEO_MODULES = {"Video", "ProductVideo", "MediaGalleryBtf", "video"}

# Bot-block markers Walmart shows instead of the product page.
_BLOCK_MARKERS = (
    "captcha",
    "access denied",
    "unusual traffic",
    "verify you are human",
    "blocked",
    "robot",
)


class FetchError(Exception):
    """PDP could not be fetched or parsed."""


class FetchBlocked(FetchError):
    """Walmart's bot defense blocked the request."""


def _extract_bullets(product: dict) -> list[str]:
    """Pull key-feature bullets from the known __NEXT_DATA__ locations."""
    kf = product.get("keyFeatures")
    if isinstance(kf, list):
        return [b for b in kf if isinstance(b, str) and b.strip()]
    return []


def _extract_brand(product: dict) -> str:
    """Read the product's brand from the __NEXT_DATA__ product node.

    Walmart exposes it as ``product.brand`` — usually a plain string, but some
    payloads nest it as ``{"name": ...}``, so both shapes are handled. Returns an
    empty string when absent (the item genuinely has no brand set, or the page
    didn't render one), which the job store treats as "no brand captured".
    """
    brand = product.get("brand")
    if isinstance(brand, str):
        return brand.strip()
    if isinstance(brand, dict):
        name = brand.get("name")
        if isinstance(name, str):
            return name.strip()
    return ""


def _extract_description(product: dict) -> str:
    """Prefer the long description, fall back to the short one."""
    for key in ("longDescription", "shortDescription"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_image_count(product: dict) -> int:
    """Count images from imageInfo.allImages."""
    images = product.get("imageInfo", {}).get("allImages", [])
    return len([img for img in images if isinstance(img, dict) and img.get("url")])


def _detect_video(product: dict) -> bool:
    """Video present if a known video module or a product-level video media exists."""
    layout = product.get("contentLayout") or {}
    module_types = {m.get("type", "") for m in layout.get("modules", []) if isinstance(m, dict)}
    if _VIDEO_MODULES & module_types:
        return True
    media = product.get("media") or {}
    return bool(media.get("videos"))


def _image_urls(product: dict) -> list[str]:
    """Return the image URLs from imageInfo.allImages."""
    images = product.get("imageInfo", {}).get("allImages", [])
    return [img["url"] for img in images if isinstance(img, dict) and img.get("url")]


def _measure_max_image_px(urls: list[str], *, limit: int = 8, timeout: int = 12) -> int:
    """Return the largest image edge (px) across the first ``limit`` images.

    Image dimensions aren't in __NEXT_DATA__, so we fetch the bytes and read the
    size with Pillow (as the WM scraper does). Best-effort: any download/parse
    failure is skipped, and 0 is returned if nothing could be measured, which
    simply means the resolution points aren't earned.
    """
    try:
        import io

        import requests
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow/requests are runtime deps
        return 0

    max_px = 0
    for url in urls[:limit]:
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            with Image.open(io.BytesIO(resp.content)) as img:
                max_px = max(max_px, img.width, img.height)
        except Exception as e:  # noqa: BLE001 - best-effort measurement
            logger.debug("Could not measure image %s: %s", url[:80], e)
    return max_px


def _is_white_background(img, *, sample: int = 160, border_frac: float = 0.08,
                        white: int = 245, min_ratio: float = 0.90) -> bool:
    """Whether an image's border is predominantly near-white.

    Walmart's main image must be the product on a pure white background. The
    product is centered, so the background is at the edges — we sample the outer
    border band and pass when nearly all of it is near-white. The image is
    downsized first (the background is uniform, so fine detail isn't needed),
    which keeps the pixel scan fast, and any transparency is flattened onto white
    (Walmart treats transparent as white). Pure and unit-tested.
    """
    from PIL import Image

    # Flatten alpha onto white so transparent PNGs read as white-background.
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        base = Image.new("RGB", rgba.size, (255, 255, 255))
        base.paste(rgba, mask=rgba.split()[-1])
        img = base
    else:
        img = img.convert("RGB")

    img = img.resize((sample, sample))
    pixels = img.load()
    band = max(1, int(sample * border_frac))
    total = near = 0
    for y in range(sample):
        for x in range(sample):
            # Skip the interior (where the product sits); score only the border.
            if band <= x < sample - band and band <= y < sample - band:
                continue
            r, g, b = pixels[x, y]
            total += 1
            if r >= white and g >= white and b >= white:
                near += 1
    return total > 0 and (near / total) >= min_ratio


def _detect_main_white_background(url: str, *, timeout: int = 12) -> bool | None:
    """Fetch the main image and report whether it's on a white background.

    Best-effort: returns ``None`` when the image can't be fetched or opened, so a
    hiccup leaves imagery scored without this signal rather than failing.
    """
    try:
        import io

        import requests
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow/requests are runtime deps
        return None
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        with Image.open(io.BytesIO(resp.content)) as img:
            return _is_white_background(img)
    except Exception as e:  # noqa: BLE001 - best-effort
        logger.debug("Could not check white background for %s: %s", url[:80], e)
        return None


def _extract_attribute_count(product: dict) -> int:
    """Count populated specs embedded on the ``product`` node itself.

    This is a fallback for the rare pages that hang a ``specifications`` field
    directly on the product (and for the unit-test fixtures). The full attribute
    set actually lives on a sibling node — see :func:`_extract_idml_specs`, which
    the fetch path prefers — so this typically returns 0 on live pages.
    """
    specs = product.get("specifications")
    if isinstance(specs, list):
        return len([s for s in specs if isinstance(s, dict) and s.get("value")])
    if isinstance(specs, dict):
        return len([v for v in specs.values() if v])
    return 0


def _count_spec_pairs(pairs: list[dict]) -> int:
    """Count spec rows that carry a non-empty value.

    A row with a name but a blank value is an *unfilled* attribute — it doesn't
    count toward completeness, matching how :func:`_extract_attribute_count`
    treats empty JSON spec values.
    """
    return len(
        [p for p in pairs if isinstance(p, dict) and str(p.get("value", "")).strip()]
    )


def _extract_idml_specs(idml: dict | None) -> list[dict] | None:
    """Pull the flat name/value spec rows from the ``idml`` node of __NEXT_DATA__.

    Walmart keeps the full attribute set under ``data.idml.specifications`` — a
    sibling of ``data.product``, not a field on the product. The on-page spec
    *table* is behind an A/B-gated collapsible (``enableSpecificationsTable`` is
    frequently off, and when off the rows aren't even rendered in the DOM), but
    this JSON is always server-rendered, so it's the reliable source.

    Returns the list of ``{name, value}`` rows, or ``None`` when the ``idml``
    node is absent — an *unknown* the caller treats as "unmeasured" rather than a
    false zero. An empty list means the node was present but carried no specs,
    which is a real zero.
    """
    if not isinstance(idml, dict):
        return None

    # Preferred shape: a flat list of {"name", "value"} dicts.
    specs = idml.get("specifications")
    if isinstance(specs, list):
        return [s for s in specs if isinstance(s, dict) and s.get("name")]

    # Fallback shape: grouped specificationsV2, where each entry pairs a
    # displayName with a list of attributeValue strings.
    v2 = idml.get("specificationsV2")
    if isinstance(v2, list):
        pairs: list[dict] = []
        for group in v2:
            if not isinstance(group, dict):
                continue
            for spec in group.get("specificationGroup") or []:
                if not isinstance(spec, dict):
                    continue
                name = spec.get("displayName")
                values = spec.get("attributeValue") or []
                if name:
                    pairs.append(
                        {"name": name, "value": ", ".join(str(v) for v in values)}
                    )
        return pairs

    return None


# Match each <li>…</li> in an HTML fragment, then strip any inner markup.
_LI_RE = re.compile(r"<li[^>]*>(.*?)</li>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def _html_list_items(markup: str) -> list[str]:
    """Extract clean text for each ``<li>`` in an HTML fragment.

    Walmart's ``idml.longDescription`` is an HTML ``<ul>`` of benefit bullets (the
    "Key item features" shown on the PDP). Pull each item's text, drop inner
    tags, unescape entities, normalize non-breaking spaces, and collapse
    whitespace; empty items are discarded.
    """
    items: list[str] = []
    for raw in _LI_RE.findall(markup or ""):
        text = html.unescape(_TAG_RE.sub("", raw)).replace("\xa0", " ")
        text = " ".join(text.split())
        if text:
            items.append(text)
    return items


def _extract_idml_bullets(idml: dict | None) -> list[str]:
    """Pull key-feature bullets from the ``idml`` node of __NEXT_DATA__.

    The product node's ``keyFeatures`` is empty on many live pages, but the same
    benefit bullets are present as an HTML ``<ul>`` in ``idml.longDescription``.
    Returns an empty list when no bulleted list is found (the item genuinely has
    no key features, or they're plain prose rather than a list).
    """
    if not isinstance(idml, dict):
        return []
    return _html_list_items(idml.get("longDescription") or "")


def parse_product(product: dict, *, url: str = "", item_id: str | None = None,
                   max_image_px: int = 0,
                   spec_pairs: list[dict] | None = None,
                   bullets: list[str] | None = None,
                   main_image_white_bg: bool | None = None) -> PdpRecord:
    """Map a Walmart ``__NEXT_DATA__`` product object to a :class:`PdpRecord`.

    ``max_image_px`` is passed in because image dimensions aren't in the JSON —
    they're measured separately by fetching the image bytes (as the WM scraper
    does with Pillow). Left at 0 (unknown) it simply doesn't earn resolution
    points.

    ``spec_pairs`` carries the resolved Specifications rows (from the ``idml``
    node — see :func:`_extract_idml_specs`). When provided it is authoritative:
    reaching that node — even if it turns out empty — counts as a real
    measurement and the attributes dimension is scored. When ``None`` (the node
    was absent, or the caller is a unit test) we fall back to any specs embedded
    on the product JSON, and treat attributes as unmeasured unless the JSON
    actually carried some.

    ``bullets`` likewise lets the caller supply resolved key-feature bullets
    (e.g. from ``idml.longDescription`` when the product node's ``keyFeatures`` is
    empty — see :func:`_extract_idml_bullets`). ``None`` falls back to the
    product node's own bullets.
    """
    if spec_pairs is not None:
        attrs = _count_spec_pairs(spec_pairs)
        attrs_measured = True
    else:
        attrs = _extract_attribute_count(product)
        attrs_measured = attrs > 0
    return PdpRecord(
        url=url,
        item_id=item_id,
        title=(product.get("name") or "").strip(),
        brand=_extract_brand(product),
        image_count=_extract_image_count(product),
        max_image_px=max_image_px,
        has_video=_detect_video(product),
        bullets=bullets if bullets is not None else _extract_bullets(product),
        description=_extract_description(product),
        attributes_present=attrs,
        attributes_measured=attrs_measured,
        main_image_white_bg=main_image_white_bg,
    )


def _load_pdp_data(url: str, timeout_ms: int) -> dict:
    """Drive a real browser to a Walmart PDP and return ``initialData.data``.

    Centralizes the anti-bot browser setup and ``__NEXT_DATA__`` extraction shared
    by :func:`fetch_pdp` (full scoring parse) and :func:`fetch_main_image_url`
    (image discovery only). Raises :class:`FetchBlocked` on bot detection and
    :class:`FetchError` on any other failure. Slow (seconds) and headed — must run
    in a background job, never inline in a request.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise FetchError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`playwright install chromium`."
        ) from e

    import json

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="America/Chicago",
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(3500)

                body = (page.inner_text("body") or "").lower()
                if any(marker in body for marker in _BLOCK_MARKERS):
                    raise FetchBlocked(f"Bot detection blocked the request for {url}")

                nd_raw = page.evaluate(
                    "() => document.getElementById('__NEXT_DATA__')?.textContent"
                )
                if not nd_raw:
                    raise FetchError("__NEXT_DATA__ not found on the page")

                return json.loads(nd_raw)["props"]["pageProps"]["initialData"]["data"]
            finally:
                browser.close()
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"Failed to fetch {url}: {e}") from e


def fetch_pdp(url: str, item_id: str | None = None, *, timeout_ms: int = 35000) -> PdpRecord:
    """Load a Walmart PDP in a real browser and parse it into a PdpRecord.

    Raises :class:`FetchBlocked` if bot detection triggers, or :class:`FetchError`
    on any other failure. Slow (seconds per item) — must run in a background job,
    never inline in a request handler.
    """
    logger.info("Fetching PDP url=%s item_id=%s", url, item_id)
    data = _load_pdp_data(url, timeout_ms)
    product = data["product"]
    idml = data.get("idml")
    # Specs and key-feature bullets both live on the sibling ``idml`` node. Prefer
    # the product node's keyFeatures when present, else fall back to
    # idml.longDescription's bulleted list.
    spec_pairs = _extract_idml_specs(idml)
    bullets = _extract_bullets(product) or _extract_idml_bullets(idml)

    # Image dimensions and the main-image background live outside __NEXT_DATA__;
    # both are read from the image bytes (the main image is the first).
    image_urls = _image_urls(product)
    max_px = _measure_max_image_px(image_urls)
    white_bg = _detect_main_white_background(image_urls[0]) if image_urls else None
    return parse_product(
        product, url=url, item_id=item_id, max_image_px=max_px,
        spec_pairs=spec_pairs, bullets=bullets, main_image_white_bg=white_bg,
    )


def fetch_main_image_url(url: str, item_id: str | None = None, *,
                         timeout_ms: int = 35000) -> str | None:
    """Return a Walmart product's main image URL (the first image), or ``None``.

    A lean cousin of :func:`fetch_pdp` used by the CI image cache: it only needs
    the image URL, so it skips the scoring parse and the extra image-byte
    downloads. Same browser/bot-detection path (headed, background-only).
    """
    logger.info("Fetching main image URL url=%s item_id=%s", url, item_id)
    data = _load_pdp_data(url, timeout_ms)
    urls = _image_urls(data.get("product") or {})
    return urls[0] if urls else None
