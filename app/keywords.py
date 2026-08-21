"""Discover a target SEO keyword set for a Walmart PDP.

Ports the approach proven in the WM share-of-voice tool's ``discover_keywords.py``:
two *grounded* sources, merged and ranked, so the target set reflects real
Walmart search demand rather than a guess —

  1. **Walmart autocomplete** (typeahead API) — real shopper search suggestions
     for the item's head terms. Lightweight HTTP, no browser.
  2. **Competitor SERP title mining** (Playwright) — the 2-3 word phrases that
     recur in the organic, non-same-brand titles Walmart already ranks for those
     terms, i.e. what the algorithm is rewarding.

The ranked result is what the scorer checks the listing's copy against (see the
keyword-coverage signals in :mod:`app.scoring`). Unlike the WM tool — which
hardcodes Tabasco seeds — seeds here are derived per item, so this works for any
product.

Network work is slow, serial, and (for SERP mining) needs headed Chrome under
Xvfb to evade Walmart's bot defense, so callers must run this in the **worker**,
never in a request (mirrors :mod:`app.fetch`). Every network step is best-effort:
a failure degrades to fewer/no keywords rather than raising, so discovery can
never break scoring.
"""

import logging
import re
from collections import defaultdict

from app.scoring import PdpRecord

logger = logging.getLogger(__name__)

AUTOCOMPLETE_URL = "https://www.walmart.com/typeahead/v2/complete"
_AUTOCOMPLETE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.walmart.com/",
}

# N-gram window and the minimum number of competitor titles a phrase must appear
# in to count as a real, rewarded term (filters one-off noise).
_NGRAM_MIN, _NGRAM_MAX = 2, 3
_MIN_COMPETITOR_FREQ = 2

# Bound the browser work: how many SERPs to mine and competitors per SERP. Fewer
# SERPs = faster and less bot exposure; 3 keeps a single item's discovery well
# under a minute while still sampling multiple queries.
_MAX_SERP_TERMS = 3
_MAX_COMPETITORS_PER_SERP = 15

# How many autocomplete seeds to try, and how many top-ranked keywords to keep as
# the "approved" target set the scorer uses.
_MAX_SEEDS = 6
_DEFAULT_TOP_KEYWORDS = 20

# Words that carry no keyword value alone or in a phrase — dropped from seeds and
# n-grams. Units are fine inside a phrase ("16 oz bottle") but not standalone.
_STOPWORDS = {
    "a", "an", "the", "and", "or", "for", "in", "of", "with", "by", "to", "at",
    "from", "on", "as", "is", "it", "its", "be", "are", "was", "were", "this",
    "that", "these", "those",
    "pack", "count", "set", "bundle", "new", "item", "product", "containing",
    "oz", "fl", "ml", "lb", "ct", "regular", "glass", "plastic", "bottle",
}


def _clean_text(text: str) -> str:
    """Lowercase, drop punctuation (keep hyphens), collapse whitespace."""
    text = (text or "").lower()
    text = re.sub(r"[^\w\s\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _is_content_token(token: str) -> bool:
    """A token worth keeping: not a stopword, not a bare number, length > 1."""
    return len(token) > 1 and token not in _STOPWORDS and not token.isdigit()


def _is_valid_ngram(tokens: list[str]) -> bool:
    """Whether a competitor-title n-gram is worth keeping as a keyword.

    Needs at least one content (non-stopword) token, and every token must be a
    real word — no bare numbers or stray single characters. That drops the
    common title noise: sizes/counts ("sauce 4", "4 5") and split possessives
    ("melinda s" from "Melinda's").
    """
    if not any(_is_content_token(t) for t in tokens):
        return False
    return all(len(t) > 1 and not t.isdigit() for t in tokens)


def derive_seeds(record: PdpRecord) -> tuple[list[str], list[str], str]:
    """Derive autocomplete seeds and SERP terms from a record's title.

    Walmart titles read "Brand Type, size/packaging…", so the product *type* —
    what shoppers actually search — sits in the head phrase before the first
    comma, after the brand. We build seeds from that phrase and its 2-word tail
    (e.g. "chipotle pepper sauce" → also "pepper sauce"). The brand (first token,
    the usual position) seeds autocomplete and is returned so SERP mining can
    exclude the seller's own listings.

    Returns ``(autocomplete_seeds, serp_terms, brand)``. Heuristic by design —
    good enough to ground discovery; refine once we have category data.
    """
    head = _clean_text((record.title or "").split(",")[0])
    tokens = head.split()
    brand = tokens[0] if tokens else ""

    # Product-type tokens: everything after the brand that carries meaning.
    content = [t for t in tokens[1:] if _is_content_token(t)]
    product_phrase = " ".join(content)
    tail_2 = " ".join(content[-2:]) if len(content) >= 2 else ""

    # Order matters: most specific first, then the generic tail, then brand.
    seeds: list[str] = []
    for candidate in (product_phrase, tail_2, brand):
        if candidate and candidate not in seeds:
            seeds.append(candidate)
    seeds = seeds[:_MAX_SEEDS]

    # SERP mining is expensive, so mine only the generic head terms (the tail
    # first — broadest competition), capped.
    serp_terms: list[str] = []
    for candidate in (tail_2, product_phrase):
        if candidate and candidate not in serp_terms:
            serp_terms.append(candidate)
    serp_terms = serp_terms[:_MAX_SERP_TERMS]

    logger.info("Derived seeds=%s serp_terms=%s brand=%r", seeds, serp_terms, brand)
    return seeds, serp_terms, brand


def parse_autocomplete(data: dict) -> list[str]:
    """Extract query suggestions from a Walmart typeahead response.

    Pure so it's unit-testable without the network. Keeps only actual search
    QUERY suggestions with a display name, lowercased and de-duplicated.
    """
    seen: set[str] = set()
    out: list[str] = []
    for q in data.get("queries", []) if isinstance(data, dict) else []:
        if not isinstance(q, dict) or q.get("type") != "QUERY":
            continue
        name = (q.get("displayName") or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def fetch_autocomplete(seeds: list[str], *, timeout: int = 10) -> list[dict]:
    """Query Walmart's typeahead API for each seed; return keyword dicts.

    Best-effort HTTP (no browser needed): any per-seed failure is logged and
    skipped. Each hit gets a baseline confidence so autocomplete-only terms still
    rank, and merge with competitor evidence can boost them.
    """
    try:
        import requests
    except ImportError:  # pragma: no cover - requests is a runtime dep
        logger.warning("requests not installed; skipping autocomplete")
        return []

    seen: set[str] = set()
    results: list[dict] = []
    session = requests.Session()
    session.headers.update(_AUTOCOMPLETE_HEADERS)
    try:
        for seed in seeds:
            try:
                resp = session.get(
                    AUTOCOMPLETE_URL,
                    params={"term": seed, "limit": 10, "cat_id": 0},
                    timeout=timeout,
                )
                resp.raise_for_status()
                for kw in parse_autocomplete(resp.json()):
                    if kw in seen:
                        continue
                    seen.add(kw)
                    results.append({
                        "keyword": kw,
                        "sources": ["autocomplete"],
                        "competitor_frequency": 0,
                        "competitor_weight": 0.0,
                        "score": 3.0,  # baseline confidence for autocomplete hits
                    })
            except Exception as e:  # noqa: BLE001 - one seed failing is fine
                logger.debug("Autocomplete failed for seed %r: %s", seed, e)
    finally:
        session.close()
    logger.info("Autocomplete produced %d keyword(s) from %d seed(s)", len(results), len(seeds))
    return results


def extract_ngrams(competitor_titles: list[dict]) -> list[dict]:
    """Extract position-weighted 2-3 word phrases from competitor titles.

    Position 1 weighs 1.0, decaying to ~0.5 by position 10 — a phrase near the
    top of the SERP is a stronger signal of what Walmart rewards. Phrases must
    recur in at least ``_MIN_COMPETITOR_FREQ`` titles to survive (drops noise).
    Pure and unit-tested.
    """
    freq: defaultdict[str, int] = defaultdict(int)
    weight: defaultdict[str, float] = defaultdict(float)

    for entry in competitor_titles:
        tokens = _clean_text(entry.get("title", "")).split()
        pos = entry.get("organic_position", 1)
        pos_weight = max(0.5, 1.0 - (pos - 1) * 0.06)
        for n in range(_NGRAM_MIN, _NGRAM_MAX + 1):
            for i in range(len(tokens) - n + 1):
                gram_tokens = tokens[i:i + n]
                if not _is_valid_ngram(gram_tokens):
                    continue
                gram = " ".join(gram_tokens)
                freq[gram] += 1
                weight[gram] += pos_weight

    results = [
        {
            "keyword": gram,
            "sources": ["competitor_titles"],
            "competitor_frequency": f,
            "competitor_weight": round(weight[gram], 2),
            "score": round(weight[gram], 2),
        }
        for gram, f in freq.items()
        if f >= _MIN_COMPETITOR_FREQ
    ]
    results.sort(key=lambda x: x["score"], reverse=True)
    return results


def merge_and_rank(autocomplete_kws: list[dict], competitor_kws: list[dict]) -> list[dict]:
    """Combine the two sources; a phrase in both is merged and score-boosted.

    Pure and unit-tested. A term backed by real shopper autocomplete *and* strong
    competitor presence is the highest-confidence target, so its evidence adds.
    """
    merged: dict[str, dict] = {kw["keyword"]: kw.copy() for kw in autocomplete_kws}
    for kw in competitor_kws:
        key = kw["keyword"]
        if key in merged:
            existing = merged[key]
            if "competitor_titles" not in existing["sources"]:
                existing["sources"].append("competitor_titles")
            existing["competitor_frequency"] = kw["competitor_frequency"]
            existing["competitor_weight"] = kw["competitor_weight"]
            existing["score"] = round(existing["score"] + kw["competitor_weight"] * 0.5, 2)
        else:
            merged[key] = kw.copy()
    return sorted(merged.values(), key=lambda x: x["score"], reverse=True)


def _extract_serp_titles(page, *, max_items: int) -> list[dict]:
    """Read organic product titles + positions from a loaded Walmart SERP.

    Anchors on Walmart's data attributes with several fallbacks (the markup
    shifts), and labels sponsored vs organic so mining can keep only organic
    results (what the ranking algorithm actually rewards).
    """
    items = page.query_selector_all("div[data-item-id]") or \
        page.query_selector_all('[data-testid="item-stack"] > div')

    results: list[dict] = []
    organic_pos = 0
    for item in items:
        if len(results) >= max_items:
            break
        name_el = (
            item.query_selector('[data-automation-id="product-title"]')
            or item.query_selector('[data-testid="product-title"]')
            or item.query_selector('span[class*="lh-title"]')
        )
        if not name_el:
            continue
        name = (name_el.inner_text() or "").strip()
        if not name:
            continue
        sponsored = (
            item.query_selector('[data-testid="ad-label"]')
            or item.query_selector('[aria-label*="sponsored" i]')
        )
        if sponsored:
            results.append({"name": name, "organic_position": 0, "sponsored": True})
        else:
            organic_pos += 1
            results.append({"name": name, "organic_position": organic_pos, "sponsored": False})
    return results


def mine_competitor_titles(serp_terms: list[str], brand: str, *, timeout_ms: int = 30000) -> list[dict]:
    """Scrape organic, non-same-brand titles from Walmart SERPs for each term.

    Drives headed Chrome (Playwright) the same way :func:`app.fetch.fetch_pdp`
    does — required to get past bot defense. Best-effort: bot blocks, timeouts,
    and missing results are logged and skipped so partial data still flows
    through. Returns ``{"title", "search_term", "organic_position"}`` rows.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:  # pragma: no cover - optional install
        logger.warning("Playwright not installed; skipping competitor mining")
        return []

    brand_l = (brand or "").lower()
    titles: list[dict] = []
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
                for term in serp_terms:
                    url = f"https://www.walmart.com/search?q={term.replace(' ', '+')}"
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        page.wait_for_timeout(3500)
                        body = (page.inner_text("body") or "").lower()
                        if any(m in body for m in ("captcha", "blocked", "verify you are human")):
                            logger.warning("Bot block on SERP for %r; skipping", term)
                            continue
                        rows = _extract_serp_titles(page, max_items=48)
                        collected = 0
                        for row in rows:
                            if row["sponsored"] or collected >= _MAX_COMPETITORS_PER_SERP:
                                continue
                            if brand_l and brand_l in row["name"].lower():
                                continue  # skip the seller's own brand
                            titles.append({
                                "title": row["name"],
                                "search_term": term,
                                "organic_position": row["organic_position"],
                            })
                            collected += 1
                        logger.info("Mined %d competitor title(s) for %r", collected, term)
                    except Exception as e:  # noqa: BLE001 - skip a bad SERP
                        logger.warning("SERP mining failed for %r: %s", term, e)
                    page.wait_for_timeout(2000)
            finally:
                browser.close()
    except Exception as e:  # noqa: BLE001 - never let discovery break scoring
        logger.warning("Competitor mining aborted: %s", e)
    return titles


def discover_keywords(record: PdpRecord, *, top_n: int = _DEFAULT_TOP_KEYWORDS) -> list[str]:
    """Build the ranked target keyword set for a record, best-effort.

    Orchestrates: derive seeds → autocomplete + competitor mining → merge/rank →
    take the top ``top_n`` as the approved set. Returns the keyword strings the
    scorer checks the copy against, or an empty list if discovery yields nothing
    (the scorer then simply leaves keyword coverage unmeasured).

    Phase 1 auto-approves the top-N; a human approval gate (as in the WM tool's
    seo_keywords.json) can be layered on later.
    """
    seeds, serp_terms, brand = derive_seeds(record)
    if not seeds:
        logger.info("No seeds derivable from title=%r; skipping discovery", record.title)
        return []

    autocomplete = fetch_autocomplete(seeds)
    competitors = mine_competitor_titles(serp_terms, brand)
    ranked = merge_and_rank(autocomplete, extract_ngrams(competitors))

    keywords = [kw["keyword"] for kw in ranked[:top_n]]
    logger.info("Discovered %d keyword(s) for item_id=%s", len(keywords), record.item_id)
    return keywords
