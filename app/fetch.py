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

import logging

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


def _extract_attribute_count(product: dict) -> int:
    """Count populated specification/attribute values from ``__NEXT_DATA__``.

    Walmart exposes these under a few shapes; count non-empty values wherever we
    find them. On most live pages the spec table isn't in ``__NEXT_DATA__`` at
    all (it lazy-loads into the DOM — see :func:`_extract_spec_pairs`), so this
    typically returns 0 and is only a fallback for the categories that do embed
    specs in the JSON (and for the unit-test fixtures).
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


# Browser-side scrape of the rendered Specifications table into {name, value}
# rows. Walmart obfuscates class names and doesn't ship the table in
# __NEXT_DATA__, so we anchor on the heading text and the table's row/cell
# structure rather than any CSS class. Returns ``null`` when the section can't
# be located at all — the caller keeps attributes *unmeasured* in that case,
# which is distinct from an empty ``[]`` (section rendered, but no rows).
_SPEC_SCRAPE_JS = """
() => {
  const headings = Array.from(document.querySelectorAll('h2, h3'));
  const heading = headings.find(
    (h) => /^(specifications|product details)$/i.test((h.textContent || '').trim())
  );
  if (!heading) return null;

  // Prefer a table inside the heading's section; fall back to the nearest
  // table that follows the heading in document order.
  const container = heading.closest('section') || heading.parentElement;
  let table = container ? container.querySelector('table') : null;
  if (!table) {
    table = Array.from(document.querySelectorAll('table')).find(
      (t) => heading.compareDocumentPosition(t) & Node.DOCUMENT_POSITION_FOLLOWING
    );
  }
  if (!table) return [];

  const pairs = [];
  for (const row of Array.from(table.querySelectorAll('tr'))) {
    const cells = Array.from(row.querySelectorAll('th, td'));
    if (cells.length >= 2) {
      const name = (cells[0].textContent || '').trim();
      const value = (cells[1].textContent || '').trim();
      if (name) pairs.push({ name, value });
    }
  }
  return pairs;
}
"""


def _expand_specifications(page) -> None:
    """Best-effort: click a "See more" control so the full spec list renders.

    Some Walmart layouts truncate the spec table behind an expander. This is
    layout-specific and can't be validated against live Walmart from the Mac, so
    every failure is swallowed — a missing or unclicked expander just means we
    read whatever rows are already present.
    """
    for name in ("See more", "View more", "Show more"):
        try:
            button = page.get_by_role("button", name=name, exact=False)
            if button.count() and button.first.is_visible():
                button.first.click(timeout=1500)
                page.wait_for_timeout(500)
                return
        except Exception as e:  # noqa: BLE001 - expander is optional
            logger.debug("Spec expander '%s' not clickable: %s", name, e)


def _extract_spec_pairs(page, *, settle_ms: int = 1200) -> list[dict] | None:
    """Scrape the lazy-loaded Specifications table from the rendered DOM.

    Walmart doesn't ship the spec table in ``__NEXT_DATA__``; it renders it in
    the DOM and lazy-loads it on scroll. We scroll the section into view to
    trigger the load, expand any "See more" control, then read the name/value
    rows via :data:`_SPEC_SCRAPE_JS`.

    Returns a list of ``{name, value}`` dicts when the section is found (possibly
    empty), or ``None`` when it can't be located at all — the caller keeps the
    attributes dimension *unmeasured* in that case rather than scoring a false
    zero. Best-effort throughout: any browser error is logged and downgraded to
    ``None`` so a scrape failure never aborts the whole fetch.
    """
    try:
        # Scroll the spec heading into view (or the page bottom) to trip the
        # lazy load, then let the network/render settle.
        page.evaluate(
            """() => {
                const h = Array.from(document.querySelectorAll('h2, h3')).find(
                    (e) => /^(specifications|product details)$/i.test(
                        (e.textContent || '').trim()
                    )
                );
                (h || document.body).scrollIntoView({block: h ? 'center' : 'end'});
            }"""
        )
        page.wait_for_timeout(settle_ms)
        _expand_specifications(page)
        pairs = page.evaluate(_SPEC_SCRAPE_JS)
    except Exception as e:  # noqa: BLE001 - best-effort; never fail the fetch
        logger.warning("Spec DOM extraction failed: %s", e)
        return None

    if pairs is None:
        logger.info("Specifications section not found in DOM")
        return None
    logger.info("Extracted %d spec pairs from DOM", len(pairs))
    return pairs


def parse_product(product: dict, *, url: str = "", item_id: str | None = None,
                   max_image_px: int = 0,
                   spec_pairs: list[dict] | None = None) -> PdpRecord:
    """Map a Walmart ``__NEXT_DATA__`` product object to a :class:`PdpRecord`.

    ``max_image_px`` is passed in because image dimensions aren't in the JSON —
    they're measured separately by fetching the image bytes (as the WM scraper
    does with Pillow). Left at 0 (unknown) it simply doesn't earn resolution
    points.

    ``spec_pairs`` carries the DOM-scraped Specifications rows (see
    :func:`_extract_spec_pairs`). When provided it is authoritative: Walmart
    lazy-loads the spec table into the DOM and usually omits it from
    ``__NEXT_DATA__``, so reaching that section — even if it turns out empty —
    counts as a real measurement and the attributes dimension is scored. When
    ``None`` (e.g. the section wasn't found, or the caller is a unit test) we
    fall back to any specs embedded in the product JSON, and treat attributes as
    unmeasured unless the JSON actually carried some.
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
        image_count=_extract_image_count(product),
        max_image_px=max_image_px,
        has_video=_detect_video(product),
        bullets=_extract_bullets(product),
        description=_extract_description(product),
        attributes_present=attrs,
        attributes_measured=attrs_measured,
    )


def fetch_pdp(url: str, item_id: str | None = None, *, timeout_ms: int = 35000) -> PdpRecord:
    """Load a Walmart PDP in a real browser and parse it into a PdpRecord.

    Raises :class:`FetchBlocked` if bot detection triggers, or :class:`FetchError`
    on any other failure. Playwright is imported here (not at module top) so the
    app doesn't hard-depend on it; a missing install raises a clear FetchError.

    This drives a full browser and is slow (seconds per item) — it must run in a
    background job, never inline in a request handler.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise FetchError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`playwright install chromium`."
        ) from e

    import json

    logger.info("Fetching PDP url=%s item_id=%s", url, item_id)
    spec_pairs: list[dict] | None = None
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

                product = json.loads(nd_raw)["props"]["pageProps"]["initialData"]["data"]["product"]

                # Specs lazy-load into the DOM (not __NEXT_DATA__), so read them
                # while the page is still open, before we tear the browser down.
                spec_pairs = _extract_spec_pairs(page)
            finally:
                browser.close()
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"Failed to fetch {url}: {e}") from e

    # Image dimensions live outside __NEXT_DATA__; measure them from the bytes.
    max_px = _measure_max_image_px(_image_urls(product))
    return parse_product(
        product, url=url, item_id=item_id, max_image_px=max_px, spec_pairs=spec_pairs
    )
