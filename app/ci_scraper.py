"""Scrape Walmart.com page-1 search results for Competitive Intelligence.

For a keyword we load ``https://www.walmart.com/search?q=...`` in a real browser
and read every product card in order, capturing its overall position, whether the
slot is organic or sponsored, the item id, and the product URL. A pure builder
then matches each card to the group's tracked products (item id first, URL as a
fallback), assigns a brand, and flags brand-new competitor SKUs.

Two halves, deliberately separated:
  * :func:`build_result_rows` is pure (card dicts -> DB row dicts) and unit-tested
    without a browser.
  * :func:`scrape_keyword_cards` drives headed Chrome and returns raw card dicts.

The browser approach mirrors :func:`app.fetch.fetch_pdp` (headed Chrome via
Playwright under the droplet's Xvfb :99 — Walmart blocks headless and plain
``requests``), and reuses that module's bot-block markers and exceptions. The
card-extraction JS + sponsored detection are ported from the reference
wm-dot-com-competitive-intelligence ``daily.py`` scraper.
"""

import logging
import random
from datetime import date

from app.fetch import _BLOCK_MARKERS, FetchBlocked, FetchError

logger = logging.getLogger(__name__)

# Politeness delays (seconds): a pause after load before reading, and the wait
# the worker leaves between keywords. Matches the reference scraper's cadence.
LOAD_SETTLE_S = (3.0, 5.0)
INTER_KEYWORD_DELAY_S = (15.0, 25.0)

# How long to poll for product cards to appear before giving up (12 x ~1s).
_CARD_POLL_ATTEMPTS = 12

# Ported verbatim from the reference daily.extract_products: runs as one atomic
# page.evaluate so element handles can't go stale mid-iteration. Returns, in DOM
# order, each card's {name, item_id, listing_type, product_url}.
_EXTRACT_JS = r"""() => {
    const results = [];

    // Try primary selector, fall back to item-stack children
    let items = Array.from(document.querySelectorAll('div[data-item-id]'));
    if (!items.length) {
        items = Array.from(document.querySelectorAll('[data-testid="item-stack"] > div'));
    }

    for (const item of items) {
        try {
            const item_id = item.getAttribute('data-item-id') || '';

            // Name
            const nameEl = (
                item.querySelector('[data-automation-id="product-title"]') ||
                item.querySelector('[data-testid="product-title"]') ||
                item.querySelector('span.w_iUH7') ||
                item.querySelector('span[class*="lh-title"]')
            );
            if (!nameEl) continue;
            const name = nameEl.innerText.trim();
            if (!name) continue;

            // Sponsored detection
            const sponsoredEl = (
                item.querySelector('[data-testid="ad-label"]') ||
                item.querySelector('[data-testid="sponsored-label"]') ||
                item.querySelector('span.sponsored-product-badge') ||
                item.querySelector('[aria-label*="sponsored" i]') ||
                item.querySelector('span[class*="sponsored" i]')
            );
            let listing_type = 'organic';
            if (sponsoredEl) {
                listing_type = 'sponsored';
            } else if (/\bSponsored\b/.test(item.innerText)) {
                listing_type = 'sponsored';
            }

            // Product URL
            const linkEl = (
                item.querySelector('a[link-identifier]') ||
                item.querySelector('a[data-testid="product-title-link"]') ||
                item.querySelector('a[href*="/ip/"]')
            );
            let product_url = '';
            if (linkEl) {
                const href = linkEl.getAttribute('href') || '';
                product_url = href.startsWith('/') ? 'https://www.walmart.com' + href : href;
            }

            results.push({ name, item_id, listing_type, product_url });
        } catch(e) {
            continue;
        }
    }
    return results;
}"""


def search_url(keyword: str) -> str:
    """Return the Walmart search URL for a keyword (spaces -> '+')."""
    return f"https://www.walmart.com/search?q={keyword.strip().replace(' ', '+')}"


def _clean_url(url: str) -> str:
    """Normalize a product URL for matching: drop query string and trailing slash."""
    return (url or "").split("?")[0].rstrip("/").lower()


def build_result_rows(cards: list[dict], *, run_id: int, group_id: int, keyword_id: int,
                      item_map: dict, brand_map: dict,
                      seen_ids_by_brand: dict | None = None,
                      scrape_date: str | None = None) -> list[dict]:
    """Turn ordered card dicts into ci_search_results row dicts (pure).

    Each card is ``{name, item_id, listing_type, product_url}``. Matching mirrors
    the reference daily.scrape_keyword: item id first, then a cleaned-URL fallback
    against the group's products. ``seen_ids_by_brand`` (brand_id -> set of item
    ids already seen) drives new-SKU detection for tracked competitor brands; it
    is mutated in place so a caller can carry it across keywords in one run.
    """
    scrape_date = scrape_date or date.today().isoformat()
    seen_ids_by_brand = seen_ids_by_brand if seen_ids_by_brand is not None else {}

    # URL index for the fallback match (built once per call).
    url_map = {
        _clean_url(p["walmart_url"]): p
        for p in item_map.values() if p["walmart_url"]
    }

    rows: list[dict] = []
    for position, card in enumerate(cards, start=1):
        listing_type = "sponsored" if card.get("listing_type") == "sponsored" else "organic"
        item_id = card.get("item_id") or ""

        matched = item_map.get(item_id)
        if not matched and card.get("product_url"):
            matched = url_map.get(_clean_url(card["product_url"]))
        brand_id = matched["brand_id"] if matched else None

        is_new_sku = False
        if brand_id is not None:
            brand = brand_map.get(brand_id)
            if brand and brand["type"] == "competitor" and brand["tracked"] and item_id:
                seen = seen_ids_by_brand.setdefault(brand_id, set())
                if item_id not in seen:
                    is_new_sku = True
                    seen.add(item_id)
                    logger.info("New competitor SKU seen item_id=%s brand=%s name=%s",
                                item_id, brand["name"], card.get("name"))

        rows.append({
            "run_id": run_id,
            "group_id": group_id,
            "keyword_id": keyword_id,
            "scraped_at": scrape_date,
            "position": position,
            "position_type": listing_type,
            "item_id": item_id or None,
            "brand_id": brand_id,
            "is_new_sku": 1 if is_new_sku else 0,
        })
    return rows


def scrape_keyword_cards(keyword: str, *, timeout_ms: int = 30000) -> list[dict]:
    """Load a keyword's search page in a real browser and return raw card dicts.

    Fresh browser per call (the reference's anti-detection approach — Walmart
    flags a session after the first scrape). Raises :class:`FetchBlocked` on bot
    detection and :class:`FetchError` on any other failure. Playwright is imported
    lazily so this module imports without it (the app degrades to a clear error).

    Slow (seconds per keyword) and serial — call only from the background worker.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise FetchError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`playwright install chromium`."
        ) from e

    url = search_url(keyword)
    logger.info("CI scraping keyword=%r url=%s", keyword, url)
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
                page.wait_for_timeout(int(random.uniform(*LOAD_SETTLE_S) * 1000))

                body = (page.inner_text("body") or "").lower()
                if any(marker in body for marker in _BLOCK_MARKERS):
                    raise FetchBlocked(f"Bot detection blocked the search for {keyword!r}")

                # Cards can hydrate late; poll briefly before deciding it's empty.
                cards: list[dict] = []
                for _ in range(_CARD_POLL_ATTEMPTS):
                    page.wait_for_timeout(1000)
                    cards = page.evaluate(_EXTRACT_JS) or []
                    if cards:
                        break
            finally:
                browser.close()
    except FetchError:
        raise
    except Exception as e:
        raise FetchError(f"Failed to scrape search for {keyword!r}: {e}") from e

    if not cards:
        # No cards after polling — treat as a block/layout change rather than a
        # silent empty result, so the run surfaces it instead of recording zero.
        raise FetchBlocked(
            f"No product cards found for {keyword!r} (possible block or layout change)"
        )
    logger.info("CI scraped keyword=%r cards=%d", keyword, len(cards))
    return cards
