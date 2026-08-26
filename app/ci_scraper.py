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
import os
import random
from datetime import date

from app.fetch import _BLOCK_MARKERS, FetchBlocked, FetchError
from app.pdp import item_number_from_url

logger = logging.getLogger(__name__)


def _proxy_from_env() -> dict | None:
    """Playwright proxy config from env, or ``None`` when unset (the default).

    Walmart's ad exchange serves headline/sponsored-video creatives only to
    residential-looking clients, so a residential proxy is what makes those ads
    actually render for the scraper — a datacenter IP (our droplet) gets ~zero
    fill. Configure it in the droplet's ``.env``:

        WALMART_PROXY_SERVER   e.g. "http://gate.provider.com:7000"  (required)
        WALMART_PROXY_USERNAME provider username                     (optional)
        WALMART_PROXY_PASSWORD provider password                     (optional)

    When ``WALMART_PROXY_SERVER`` is empty the scraper runs directly, exactly as
    before, so this is inert until an endpoint is supplied. The password is never
    logged (see :func:`scrape_keyword_cards`).
    """
    server = os.environ.get("WALMART_PROXY_SERVER", "").strip()
    if not server:
        return None
    proxy = {"server": server}
    username = os.environ.get("WALMART_PROXY_USERNAME", "").strip()
    if username:
        # Only attach credentials when a username is set; password may be blank.
        proxy["username"] = username
        proxy["password"] = os.environ.get("WALMART_PROXY_PASSWORD", "")
    return proxy

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


def _norm_name(s: str) -> str:
    """Lowercase and strip non-alphanumerics for tolerant name matching.

    Turns "Frank's RedHot" and "franks redhot" alike into "franksredhot" so a
    brand name matches inside a product title regardless of spacing/punctuation.
    """
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _brand_index(brand_map: dict) -> list[tuple[str, int]]:
    """Normalized brand-name -> brand_id index, longest name first.

    Longest-first so a specific brand ("frank's red hot") wins over a shorter one
    it contains. Shared by product-card attribution (:func:`build_result_rows`) and
    ad attribution (:func:`build_ad_rows`).
    """
    return sorted(
        ((_norm_name(b["name"]), b["id"]) for b in brand_map.values() if b["name"]),
        key=lambda t: -len(t[0]),
    )


def build_result_rows(cards: list[dict], *, run_id: int, group_id: int, keyword_id: int,
                      item_map: dict, brand_map: dict,
                      seen_ids_by_brand: dict | None = None,
                      scrape_date: str | None = None) -> list[dict]:
    """Turn ordered card dicts into ci_search_results row dicts (pure).

    Each card is ``{name, item_id, listing_type, product_url}``. The card's raw
    ``item_id`` (Walmart's ``data-item-id``) is an *opaque* code (e.g.
    ``3K2RMCS1KI5D``), NOT the numeric item number tracked products are keyed by,
    so matching is driven by the numeric item number parsed from the card's
    ``/ip/<slug>/<number>`` URL (same rule as intake, :func:`pdp.item_number_from_url`).
    That numeric id is also what we store, so the ranking join
    (``ci_search_results.item_id = ci_products.walmart_item_id``) lines up.
    A cleaned-URL match is kept as a fallback. A tracked product's **sponsored**
    slot, though, carries a *different* opaque id and a tracking URL (no
    /ip/<number>), so none of those reach it — but Walmart shows it the *same
    product title* as the product's organic card. So we first learn each tracked
    product's title from its id-matched (organic) card, then tie a sponsored slot
    with an identical title back to that tracked item, storing the tracked numeric
    id so the ranking join reaches it. (Ported from the WM SOV tool, which
    name-matches sponsored variant ids to their canonical tracked id.) Only the
    *tracked* SKU's sponsored slots are tied back this way — a brand's other
    sponsored SKUs are not. Finally a **brand-name** match attributes whatever is
    left (untracked SKUs, sponsored or organic) so it still counts toward that
    brand's share of shelf. ``seen_ids_by_brand`` (brand_id -> set of item ids
    seen) drives new-SKU detection for tracked competitor brands; it is mutated in
    place so a caller can carry it across a run.
    """
    scrape_date = scrape_date or date.today().isoformat()
    seen_ids_by_brand = seen_ids_by_brand if seen_ids_by_brand is not None else {}

    # URL index for the fallback match (built once per call).
    url_map = {
        _clean_url(p["walmart_url"]): p
        for p in item_map.values() if p["walmart_url"]
    }
    # Brand-name index for attributing cards we can't match to a tracked product —
    # crucially the SPONSORED slots, which Walmart gives a different item id and a
    # tracking URL (no /ip/<number>), so id/URL matching never reaches them.
    brand_index = _brand_index(brand_map)

    def _match_by_id(card: dict) -> dict | None:
        """Match a card to a tracked product by numeric id, raw id, then cleaned URL."""
        url = card.get("product_url") or ""
        numeric = item_number_from_url(url) if url else None
        product = item_map.get(numeric) if numeric else None
        if product is None and card.get("item_id"):
            product = item_map.get(card["item_id"])
        if product is None and url:
            product = url_map.get(_clean_url(url))
        return product

    # Pre-pass: learn each tracked product's normalized title from a card we can tie
    # to it by id (its organic slot). A sponsored slot of the *same* product shows an
    # identical title, so this lets the loop below attribute that sponsored slot back
    # to the tracked item even though its id is opaque. Sponsored cards usually sit
    # ABOVE the organic ones, so this must run before the positional loop.
    tracked_title_map: dict[str, dict] = {}
    for card in cards:
        product = _match_by_id(card)
        if product is not None:
            key = _norm_name(card.get("name"))
            if key:
                tracked_title_map.setdefault(key, product)

    rows: list[dict] = []
    for position, card in enumerate(cards, start=1):
        listing_type = "sponsored" if card.get("listing_type") == "sponsored" else "organic"
        raw_id = card.get("item_id") or ""
        product_url = card.get("product_url") or ""

        # The numeric item number from the card URL is the reliable match/join key;
        # fall back to the raw card id only if the URL has none.
        numeric_id = item_number_from_url(product_url) if product_url else None
        item_id = numeric_id or raw_id or None

        # 1) Precise product match (item number, raw id, then cleaned URL).
        matched = _match_by_id(card)

        # 2) Tracked-item sponsored match: same title as the product's organic card
        # (learned above) but a different opaque id. Tie it back and store the tracked
        # numeric id so the ranking join (item_id = walmart_item_id) reaches it.
        if matched is None:
            title_match = tracked_title_map.get(_norm_name(card.get("name")))
            if title_match is not None:
                matched = title_match
                item_id = title_match["walmart_item_id"]

        brand_id = matched["brand_id"] if matched else None

        # 3) Brand-name fallback: attribute by the brand name in the card title.
        # This is how a brand's untracked SKUs (sponsored or organic) get counted
        # toward that brand's share of the shelf.
        if brand_id is None:
            card_norm = _norm_name(card.get("name"))
            if card_norm:
                for brand_norm, bid in brand_index:
                    if brand_norm and brand_norm in card_norm:
                        brand_id = bid
                        break

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


# ── Brand ad units (headline "Brand Amplifier" + sponsored video) ────────────────

# Ad types persisted in ci_ad_units.ad_type.
AD_HEADLINE = "headline"
AD_VIDEO = "video"

# Extracts the search page's brand ad units. Ported from a live-DOM discovery pass
# (2026-08): the headline ad is the "Sponsored Brand Ad" card, and the sponsored
# video ad is the video-player module. Both are client-served (their brand isn't in
# __NEXT_DATA__), so we read the rendered DOM. Returns, per ad, its type, the brand
# it advertises (parsed from the "Sponsored by <brand>" line or the logo alt), and a
# stable selector the caller screenshots for the creative image.
_EXTRACT_ADS_JS = r"""() => {
    const ads = [];
    const bySponsoredBy = (el) => {
        const m = (el && el.innerText || '').match(/sponsored by\s+([^\n]+)/i);
        return m ? m[1].trim() : null;
    };

    // Headline: the Sponsored Brand Ad ("Brand Amplifier") card at the top.
    const sba = document.querySelector('[data-testid="sba-container"]');
    if (sba) {
        let brand = bySponsoredBy(sba);
        if (!brand) {
            const logo = sba.querySelector('img[alt]');
            if (logo) brand = (logo.getAttribute('alt') || '').trim() || null;
        }
        ads.push({ad_type: 'headline', brand_text: brand,
                  selector: '[data-testid="sba-container"]'});
    }

    // Sponsored video ad: the video-player module. The <video> itself carries no
    // brand, so look for a "Sponsored by" line on an ancestor (best-effort).
    const vpw = document.querySelector('[data-testid="VideoPlayerWrapper"]');
    if (vpw) {
        let brand = null, n = vpw;
        for (let d = 0; n && d < 6 && !brand; d++, n = n.parentElement) brand = bySponsoredBy(n);
        ads.push({ad_type: 'video', brand_text: brand,
                  selector: '[data-testid="VideoPlayerWrapper"]'});
    }
    return ads;
}"""


def build_ad_rows(ads: list[dict], *, run_id: int, group_id: int, keyword_id: int,
                  brand_map: dict, scrape_date: str | None = None) -> list[dict]:
    """Turn raw ad descriptors into ci_ad_units row dicts (pure).

    Each ``ad`` is ``{ad_type, brand_text, image_path}``. The ad's own brand label
    (``brand_text`` — e.g. "Frank's RedHot" from "Sponsored by Frank's RedHot") is
    matched to one of the group's tracked brands by normalized name, so a headline
    or video ad for a tracked brand (mine or competitor) is attributed to it. An ad
    whose brand isn't tracked is still recorded with ``brand_id = None`` (it just
    won't surface under a tracked brand in the report). ``image_path`` is the saved
    creative, relative to MEDIA_DIR (the caller captures + saves it).
    """
    scrape_date = scrape_date or date.today().isoformat()
    brand_index = _brand_index(brand_map)

    rows = []
    for ad in ads:
        brand_norm = _norm_name(ad.get("brand_text"))
        brand_id = None
        if brand_norm:
            for cand_norm, bid in brand_index:
                # Match either direction so "Frank's RedHot" ad ties to a "Frank's"
                # tracked brand and vice versa.
                if cand_norm and (cand_norm in brand_norm or brand_norm in cand_norm):
                    brand_id = bid
                    break
        rows.append({
            "run_id": run_id,
            "group_id": group_id,
            "keyword_id": keyword_id,
            "scraped_at": scrape_date,
            "ad_type": ad["ad_type"],
            "brand_id": brand_id,
            "brand_text": (ad.get("brand_text") or None),
            "image_path": ad.get("image_path"),
        })
    return rows


def _capture_ads(page) -> list[dict]:
    """Extract + screenshot the page's brand ad units (best-effort, in-session).

    Ads load lazily, so nudge-scroll to trigger them, read the ad descriptors, then
    screenshot each ad's element for the creative image. Returns a list of
    ``{ad_type, brand_text, image_bytes}`` (``image_bytes`` is PNG or ``None`` if the
    shot failed). Never raises — ads are a bonus on top of the card scrape, so any
    failure is logged and yields fewer/no ads rather than sinking the keyword.
    """
    for y in (400, 1100, 0):  # trigger lazy ad fill, then return to the top
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(1500)
    descriptors = page.evaluate(_EXTRACT_ADS_JS) or []
    ads = []
    for d in descriptors:
        image_bytes = None
        try:
            image_bytes = page.locator(d["selector"]).first.screenshot(timeout=8000)
        except Exception:  # noqa: BLE001 - a missed screenshot must not fail the scrape
            logger.warning("CI ad screenshot failed ad_type=%s selector=%s",
                           d.get("ad_type"), d.get("selector"))
        ads.append({"ad_type": d["ad_type"], "brand_text": d.get("brand_text"),
                    "image_bytes": image_bytes})
    return ads


def scrape_keyword_page(keyword: str, *, timeout_ms: int = 30000,
                        capture_ads: bool = True) -> dict:
    """Load a keyword's search page once and return its cards and brand ad units.

    Returns ``{"cards": [...], "ads": [...]}``: ``cards`` are the raw product-card
    dicts (see :func:`build_result_rows`); ``ads`` are ``{ad_type, brand_text,
    image_bytes}`` for the headline + sponsored-video ad units (see
    :func:`build_ad_rows`), captured on the same page load so we don't re-fetch.

    Fresh browser per call (the reference's anti-detection approach — Walmart flags
    a session after the first scrape). Raises :class:`FetchBlocked` on bot detection
    or when no cards appear, and :class:`FetchError` on any other failure. Playwright
    is imported lazily so this module imports without it. Slow (seconds per keyword)
    and serial — call only from the background worker.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise FetchError(
            "Playwright is not installed. Run `pip install playwright` and "
            "`playwright install chromium`."
        ) from e

    url = search_url(keyword)
    # A residential proxy (when configured) is what lets Walmart's ad exchange serve
    # headline/sponsored-video ads to the scrape; set at launch so every request in
    # the session routes through it. Log the server but never the password.
    proxy = _proxy_from_env()
    logger.info("CI scraping keyword=%r url=%s proxy=%s", keyword, url,
                proxy["server"] if proxy else "direct")
    ads: list[dict] = []
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

                # Ad capture rides on the same page load; guarded so it never breaks
                # the card scrape (which is the run's primary output).
                if capture_ads and cards:
                    try:
                        ads = _capture_ads(page)
                    except Exception:  # noqa: BLE001 - ads are a bonus, cards are not
                        logger.warning("CI ad capture failed keyword=%r", keyword)
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
    logger.info("CI scraped keyword=%r cards=%d ads=%d", keyword, len(cards), len(ads))
    return {"cards": cards, "ads": ads}


def scrape_keyword_cards(keyword: str, *, timeout_ms: int = 30000) -> list[dict]:
    """Cards-only wrapper over :func:`scrape_keyword_page` (skips ad capture)."""
    return scrape_keyword_page(keyword, timeout_ms=timeout_ms, capture_ads=False)["cards"]
