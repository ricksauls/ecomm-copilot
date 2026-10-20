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

from app import proxy as webproxy
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


# Walmart embeds its server state in a <script id="__NEXT_DATA__"> JSON blob.
# When the page HTML comes from the managed scraping API (see app.scraper_api)
# instead of a live browser, we pull that blob out of the markup here.
_NEXT_DATA_RE = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)


def _next_data_from_html(html_text: str) -> dict:
    """Extract Walmart's ``__NEXT_DATA__`` ``initialData.data`` from page HTML.

    Produces the identical dict the browser path returns (``{"product": …,
    "idml": …}``), so every caller parses it the same way regardless of whether
    the HTML came from the Oxylabs API or a live browser. Raises
    :class:`FetchError` when the blob is absent (e.g. an error/block page) or
    malformed.
    """
    import json

    match = _NEXT_DATA_RE.search(html_text or "")
    if not match:
        raise FetchError("__NEXT_DATA__ not found in the page HTML")
    try:
        blob = json.loads(match.group(1))
        return blob["props"]["pageProps"]["initialData"]["data"]
    except (ValueError, KeyError, TypeError) as e:
        raise FetchError(f"__NEXT_DATA__ present but unparsable: {e}") from e


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


def _extract_category(product: dict) -> str:
    """Best-effort leaf category / shopper intent from the product node.

    Used to seed the item's category keyword (KW3). Walmart's __NEXT_DATA__ is not
    consistent here, so we try the known shapes in order and return the most
    specific (leaf) category name, or '' when none is exposed — in which case KW3
    is left blank for the user to fill (the defaults are explicitly best-effort).

      • ``product.category.path`` — a breadcrumb list of ``{name}``; last = leaf.
      • ``product.categoryPathName`` / ``product.productType`` — a ``"A/B/C"`` or
        plain string; last path segment = leaf.
    """
    category = product.get("category")
    if isinstance(category, dict):
        path = category.get("path")
        if isinstance(path, list) and path:
            leaf = path[-1]
            name = leaf.get("name") if isinstance(leaf, dict) else leaf
            if isinstance(name, str) and name.strip():
                return name.strip()
    elif isinstance(category, str) and category.strip():
        return category.strip().split("/")[-1].strip()

    for key in ("categoryPathName", "productType"):
        value = product.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().split("/")[-1].strip()
    return ""


# Spec names (lowercased, substring-matched) that carry a product's size / pack,
# in priority order — the first one present with a value wins. Ordered so an
# explicit net content/weight beats a generic "size", and a multipack count is a
# last resort.
_SIZE_SPEC_HINTS = (
    "net content", "net weight", "total capacity", "capacity",
    "container size", "size", "volume", "count per pack", "count",
)


def _extract_size_from_specs(specs: list[dict] | None) -> str:
    """Best-effort product size from the idml spec rows (see _extract_idml_specs).

    Scans the ``{name, value}`` rows for the first size-ish attribute with a
    value (e.g. ``Net Content → "12 oz"``). Returns '' when none is found — the
    size is simply dropped from KW2 rather than guessed.
    """
    if not specs:
        return ""
    for hint in _SIZE_SPEC_HINTS:
        for row in specs:
            name = (row.get("name") or "").strip().lower()
            value = (row.get("value") or "").strip()
            if value and hint in name:
                return value
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


def _measure_image_dims(urls: list[str], *, limit: int = 12, timeout: int = 10) -> list[dict]:
    """Measure each image's largest edge (px), in gallery order.

    Returns one ``{"url": str, "px": int}`` per image (up to ``limit``); px is the
    largest edge, or 0 when that image couldn't be measured. Image dimensions
    aren't in __NEXT_DATA__, so we fetch the bytes and read the size with Pillow
    (as the WM scraper does). Best-effort *per image*: a single download/parse
    failure yields px 0 for that image (no resolution credit, and it isn't flagged
    as sub-spec) without affecting the others. Capped at ``limit`` to bound
    per-item time — Walmart galleries past that are rare.
    """
    try:
        import io

        import requests
        from PIL import Image
    except ImportError:  # pragma: no cover - Pillow/requests are runtime deps
        return [{"url": u, "px": 0} for u in urls[:limit]]

    dims: list[dict] = []
    for url in urls[:limit]:
        px = 0
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            with Image.open(io.BytesIO(resp.content)) as img:
                px = max(img.width, img.height)
        except Exception as e:  # noqa: BLE001 - best-effort measurement
            logger.debug("Could not measure image %s: %s", url[:80], e)
        dims.append({"url": url, "px": px})
    return dims


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


def _to_float(value) -> float | None:
    """Coerce a JSON number/string to float, or None when it isn't numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    """Coerce a JSON number/string to int, or None when it isn't an integer."""
    f = _to_float(value)
    return int(f) if f is not None else None


def _extract_price(product: dict) -> tuple[float | None, str | None]:
    """Read current price + display string from the product node.

    Walmart nests the live price under ``priceInfo.currentPrice`` (``price`` is the
    number, ``priceString`` the formatted "$3.98"). Out-of-stock items may omit the
    block entirely, so both fall back to None.
    """
    current = (product.get("priceInfo") or {}).get("currentPrice") or {}
    price_string = current.get("priceString")
    return (
        _to_float(current.get("price")),
        price_string.strip() if isinstance(price_string, str) and price_string.strip() else None,
    )


def _extract_rating(product: dict) -> float | None:
    """Average star rating — on the product node directly or under ``rating``."""
    direct = _to_float(product.get("averageRating"))
    if direct is not None:
        return direct
    return _to_float((product.get("rating") or {}).get("averageRating"))


def _extract_review_count(product: dict) -> int | None:
    """Review count — on the product node directly or under ``rating``."""
    direct = _to_int(product.get("numberOfReviews"))
    if direct is not None:
        return direct
    return _to_int((product.get("rating") or {}).get("numberOfReviews"))


def _extract_in_stock(product: dict) -> bool | None:
    """Whether the item is purchasable.

    Prefers the explicit ``availabilityStatus`` string ("IN_STOCK"/"OUT_OF_STOCK"),
    falling back to the boolean ``canAddToCart``. None when neither is present.
    """
    status = product.get("availabilityStatus")
    if isinstance(status, str) and status.strip():
        return status.strip().upper() == "IN_STOCK"
    cart = product.get("canAddToCart")
    if isinstance(cart, bool):
        return cart
    return None


def parse_product(product: dict, *, url: str = "", item_id: str | None = None,
                   max_image_px: int = 0,
                   image_dims: list[dict] | None = None,
                   spec_pairs: list[dict] | None = None,
                   bullets: list[str] | None = None,
                   main_image_white_bg: bool | None = None,
                   main_image_url: str | None = None) -> PdpRecord:
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
        image_dims=image_dims or [],
        has_video=_detect_video(product),
        bullets=bullets if bullets is not None else _extract_bullets(product),
        description=_extract_description(product),
        attributes_present=attrs,
        attributes_measured=attrs_measured,
        main_image_white_bg=main_image_white_bg,
        main_image_url=main_image_url,
    )


def _load_pdp_data(url: str, timeout_ms: int, *, use_proxy: bool = False) -> dict:
    """Drive a real browser to a Walmart PDP and return ``initialData.data``.

    Centralizes the anti-bot browser setup and ``__NEXT_DATA__`` extraction shared
    by :func:`fetch_pdp` (full scoring parse) and :func:`fetch_main_image_url`
    (image discovery only). Raises :class:`FetchBlocked` on bot detection and
    :class:`FetchError` on any other failure. Slow (seconds) and headed — must run
    in a background job, never inline in a request.

    ``use_proxy`` routes the browser through the residential proxy
    (:func:`app.proxy.proxy_from_env`) when one is configured — opt-in because it's
    metered per-GB. Scoring/CI-image-cache leave it off (they work direct); Import
    Product Images turns it on so Walmart's bot detection sees a residential IP.

    When a managed scraping API is configured (:func:`app.scraper_api.scraper_enabled`)
    the API supplies the rendered HTML instead and the browser below is skipped —
    this is the live path on the droplet, where Walmart blocks our own browser.
    ``use_proxy`` is then moot (the API handles unblocking).
    """
    # Preferred on the droplet: the managed API returns the rendered HTML and we
    # parse the same __NEXT_DATA__ blob out of it. Imported here (not at module
    # top) to avoid a circular import — scraper_api imports this module's errors.
    from app import scraper_api

    if scraper_api.scraper_enabled():
        return _next_data_from_html(scraper_api.fetch_html(url))

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise FetchError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`playwright install chromium`."
        ) from e

    import json

    proxy = webproxy.proxy_from_env() if use_proxy else None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                **({"proxy": proxy} if proxy else {}),
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


def render_page_html(url: str, *, timeout_ms: int = 35000, use_proxy: bool = False) -> str:
    """Drive a real browser to ``url`` and return its rendered HTML (``content()``).

    For non-Walmart pages (e.g. Amazon) that parse their own DOM rather than
    ``__NEXT_DATA__``. Same anti-bot browser setup + block detection as
    :func:`_load_pdp_data` (kept parallel rather than shared to avoid refactoring the
    working Walmart path). ``use_proxy`` routes through the residential proxy when
    configured. Raises :class:`FetchBlocked` / :class:`FetchError`. Headed + slow —
    background jobs only.

    When a managed scraping API is configured
    (:func:`app.scraper_api.scraper_enabled`) it renders and returns the HTML
    instead, skipping the browser below.
    """
    from app import scraper_api

    if scraper_api.scraper_enabled():
        return scraper_api.fetch_html(url)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise FetchError("Playwright is not installed.") from e

    proxy = webproxy.proxy_from_env() if use_proxy else None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                **({"proxy": proxy} if proxy else {}),
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

                return page.content()
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
    image_dims = _measure_image_dims(image_urls)
    max_px = max((d["px"] for d in image_dims), default=0)
    white_bg = _detect_main_white_background(image_urls[0]) if image_urls else None
    return parse_product(
        product, url=url, item_id=item_id, max_image_px=max_px, image_dims=image_dims,
        spec_pairs=spec_pairs, bullets=bullets, main_image_white_bg=white_bg,
        main_image_url=image_urls[0] if image_urls else None,
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


def fetch_title(url: str, item_id: str | None = None, *, timeout_ms: int = 35000) -> str:
    """Return a Walmart product's title (name), or '' when the page yields none.

    The leanest cousin of :func:`fetch_pdp`, used to backfill portfolio item names
    so the managed-items pick-lists are searchable by product title. Only reads the
    product name — no scoring parse, no image work. Same browser/bot-detection path
    (headed, background-only): raises :class:`FetchBlocked` / :class:`FetchError`.
    """
    logger.info("Fetching product title url=%s item_id=%s", url, item_id)
    data = _load_pdp_data(url, timeout_ms)
    return ((data.get("product") or {}).get("name") or "").strip()


def fetch_item_facts(url: str, item_id: str | None = None, *,
                     timeout_ms: int = 35000) -> dict:
    """Lean cousin of :func:`fetch_pdp` for the DAILY item-tracking snapshot.

    Loads the PDP once and reads only the signals that actually move day to day —
    price, stock, rating, review count — plus a couple of free content facts (image
    count, video, title). It deliberately skips the slow image-byte downloads
    :func:`fetch_pdp` does for scoring, so it's cheap enough to run on every tracked
    item daily. Same headed-browser / bot-detection path: raises
    :class:`FetchBlocked` / :class:`FetchError`; background-only.

    Returns a dict keyed for :func:`app.tracking.save_facts` (``price``,
    ``price_string``, ``in_stock``, ``rating``, ``review_count``, ``image_count``,
    ``has_video``, ``title``); any field the page doesn't expose comes back None.
    """
    logger.info("Fetching item facts url=%s item_id=%s", url, item_id)
    data = _load_pdp_data(url, timeout_ms)
    product = data.get("product") or {}
    price, price_string = _extract_price(product)
    return {
        "price": price,
        "price_string": price_string,
        "in_stock": _extract_in_stock(product),
        "rating": _extract_rating(product),
        "review_count": _extract_review_count(product),
        "image_count": _extract_image_count(product),
        "has_video": _detect_video(product),
        "title": (product.get("name") or "").strip() or None,
    }


def fetch_item_descriptors(url: str, item_id: str | None = None, *,
                           timeout_ms: int = 35000) -> dict:
    """Lean cousin of :func:`fetch_pdp` that backfills an item's tracking keywords.

    Loads the PDP once and reads the few descriptors needed to generate the
    best-effort keyword defaults (see :mod:`app.item_keywords`): the product
    ``title`` and ``brand``, the ``size`` (from the idml spec rows), and the leaf
    ``category``. No scoring parse, no image work. Same headed-browser /
    bot-detection path: raises :class:`FetchBlocked` / :class:`FetchError`;
    background-only.

    Returns ``{title, brand, size, category}`` with '' for any field the page
    doesn't expose (title/brand also drive the existing name backfill).
    """
    logger.info("Fetching item descriptors url=%s item_id=%s", url, item_id)
    data = _load_pdp_data(url, timeout_ms)
    product = data.get("product") or {}
    specs = _extract_idml_specs(data.get("idml"))
    return {
        "title": (product.get("name") or "").strip(),
        "brand": _extract_brand(product),
        "size": _extract_size_from_specs(specs),
        "category": _extract_category(product),
    }


def fetch_all_image_urls(url: str, *, timeout_ms: int = 35000,
                         use_proxy: bool = False) -> tuple[str, list[str]]:
    """Return ``(product_title, [all gallery image URLs])`` for a Walmart PDP.

    A lean cousin of :func:`fetch_pdp` for the Import Product Images feature: it
    needs the product's whole image gallery (not just the main image) plus a title
    to label the run, but none of the scoring parse. Same browser/bot-detection
    path (headed, background-only). ``use_proxy`` routes through the residential
    proxy (Import turns this on). Title is empty and the list may be empty when the
    page yields no data; the caller treats an empty list as "no images found".
    """
    logger.info("Fetching all image URLs url=%s proxy=%s", url, use_proxy)
    data = _load_pdp_data(url, timeout_ms, use_proxy=use_proxy)
    product = data.get("product") or {}
    title = (product.get("name") or "").strip()
    return title, _image_urls(product)
