"""Tests for keyword discovery (pure parts) and keyword-coverage scoring.

The network steps (autocomplete HTTP, competitor SERP mining) drive real
requests/Chrome and aren't exercised here — like app.fetch, the parsing and
ranking that turn raw responses into a keyword set are what these lock down.
"""

from app.db import get_db
from app.jobs import get_cached_keywords, put_cached_keywords
from app.keywords import (
    cache_key,
    derive_seeds,
    discover_keywords,
    extract_ngrams,
    merge_and_rank,
    parse_autocomplete,
)
from app.scoring import PdpRecord, score_pdp

_TABASCO_TITLE = "Tabasco Chipotle Pepper Sauce, 5 fl oz Regular Glass Bottle"


def test_derive_seeds_from_walmart_title():
    seeds, serp_terms, brand = derive_seeds(PdpRecord(url="u", title=_TABASCO_TITLE))
    assert brand == "tabasco"
    # Product type comes from the head phrase (before the size), brand dropped.
    assert "chipotle pepper sauce" in seeds
    assert "pepper sauce" in seeds  # the generic 2-word tail
    # SERP mining targets the generic terms, capped.
    assert "pepper sauce" in serp_terms
    assert len(serp_terms) <= 3


def test_derive_seeds_empty_title():
    seeds, serp_terms, brand = derive_seeds(PdpRecord(url="u", title=""))
    assert seeds == [] and serp_terms == [] and brand == ""


def test_parse_autocomplete_keeps_query_suggestions():
    data = {
        "queries": [
            {"type": "QUERY", "displayName": "Hot Sauce"},
            {"type": "QUERY", "displayName": "hot sauce"},  # dup after lowercasing
            {"type": "PRODUCT", "displayName": "some product"},  # not a search query
            {"type": "QUERY"},  # no display name
        ]
    }
    assert parse_autocomplete(data) == ["hot sauce"]
    assert parse_autocomplete({}) == []


def test_extract_ngrams_filters_by_competitor_frequency():
    titles = [
        {"title": "Cholula Chipotle Hot Sauce", "organic_position": 1},
        {"title": "Tapatio Hot Sauce Bottle", "organic_position": 2},
    ]
    grams = {g["keyword"] for g in extract_ngrams(titles)}
    assert "hot sauce" in grams  # appears in both titles -> survives
    assert "chipotle hot sauce" not in grams  # appears once -> filtered out


def test_extract_ngrams_drops_number_and_single_char_noise():
    titles = [
        {"title": "Melinda's Hot Sauce 5 oz", "organic_position": 1},
        {"title": "Grace Hot Sauce 5 pack", "organic_position": 2},
    ]
    grams = {g["keyword"] for g in extract_ngrams(titles)}
    assert "hot sauce" in grams
    # No surviving gram contains a bare number or a stray single character.
    assert not any(
        any(tok.isdigit() or len(tok) == 1 for tok in g.split()) for g in grams
    )


def test_merge_and_rank_boosts_terms_in_both_sources():
    auto = [{
        "keyword": "hot sauce", "sources": ["autocomplete"],
        "competitor_frequency": 0, "competitor_weight": 0.0, "score": 3.0,
    }]
    comp = [
        {"keyword": "hot sauce", "sources": ["competitor_titles"],
         "competitor_frequency": 2, "competitor_weight": 1.8, "score": 1.8},
        {"keyword": "pepper sauce", "sources": ["competitor_titles"],
         "competitor_frequency": 2, "competitor_weight": 1.0, "score": 1.0},
    ]
    ranked = merge_and_rank(auto, comp)
    top = ranked[0]
    assert top["keyword"] == "hot sauce"  # boosted above the competitor-only term
    assert set(top["sources"]) == {"autocomplete", "competitor_titles"}
    assert top["score"] == 3.9  # 3.0 + 1.8 * 0.5


def test_discover_keywords_without_seeds_is_empty():
    # No title -> no seeds -> no network calls, empty set (scorer then skips it).
    assert discover_keywords(PdpRecord(url="u", title="")) == []


def test_cache_key_groups_by_category_not_brand():
    # Same product type, different brands -> same key (reuse the discovery).
    tabasco = PdpRecord(url="u", title="Tabasco Chipotle Pepper Sauce, 5 fl oz")
    cholula = PdpRecord(url="u", title="Cholula Chipotle Pepper Sauce, 5 fl oz")
    cumin = PdpRecord(url="u", title="Great Value Ground Cumin, 4.5 oz")
    assert cache_key(tabasco) == cache_key(cholula)
    assert cache_key(tabasco) != cache_key(cumin)
    assert cache_key(PdpRecord(url="u", title="")) == ""


def test_keyword_cache_roundtrip_and_staleness(app):
    with app.app_context():
        db = get_db()
        assert get_cached_keywords(db, "hot sauce") is None  # empty cache
        put_cached_keywords(db, "hot sauce", ["hot sauce", "pepper sauce"])
        assert get_cached_keywords(db, "hot sauce") == ["hot sauce", "pepper sauce"]
        # An empty key never caches or reads.
        put_cached_keywords(db, "", ["x"])
        assert get_cached_keywords(db, "") is None
        # Backdate the entry beyond the freshness window -> treated as a miss.
        db.execute(
            "UPDATE keyword_cache SET created_at = datetime('now', '-30 days') "
            "WHERE cache_key = 'hot sauce'"
        )
        db.commit()
        assert get_cached_keywords(db, "hot sauce", max_age_days=7) is None


# --- keyword-coverage scoring -------------------------------------------------

def _base_record(**overrides) -> PdpRecord:
    """A scorable record; keyword-related fields overridden per test."""
    base = dict(
        url="u",
        title="Tabasco Chipotle Pepper Sauce, 5 fl oz",
        image_count=6, max_image_px=2000,
        bullets=["A benefit line here", "Another benefit line", "Third benefit line"],
        description=" ".join(["Smoky chipotle pepper sauce with real heat."] * 30),
    )
    base.update(overrides)
    return PdpRecord(**base)


def _dim(pdp, key):
    return next(d for d in score_pdp(pdp).dimensions if d.key == key)


def test_keywords_none_leaves_title_unchanged():
    # Default (no keyword set) -> title scores on format alone, unchanged.
    assert _dim(_base_record(target_keywords=None), "title").score \
        == _dim(_base_record(), "title").score


def test_title_keyword_hits_raise_the_title_score():
    covered = _base_record(target_keywords=["chipotle pepper sauce", "pepper sauce"])
    missing = _base_record(target_keywords=["buffalo wing sauce", "ketchup"])
    assert _dim(covered, "title").score > _dim(missing, "title").score


def test_description_keyword_hits_raise_the_description_score():
    covered = _base_record(target_keywords=["chipotle pepper sauce", "heat", "smoky"])
    missing = _base_record(target_keywords=["barbecue rub", "ketchup", "mustard"])
    assert _dim(covered, "description").score > _dim(missing, "description").score


def test_missing_keywords_generate_recommendations():
    missing = _base_record(target_keywords=["buffalo wing sauce", "ketchup"])
    title = _dim(missing, "title")
    assert any("search term" in r for r in title.recommendations)
