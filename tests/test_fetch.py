"""Tests for parsing a Walmart __NEXT_DATA__ product into a PdpRecord.

Live fetching drives a real browser and isn't exercised here; the parsing that
turns the page JSON into a scorable record is what these lock down.
"""

from PIL import Image

from app.fetch import (
    _count_spec_pairs,
    _extract_brand,
    _extract_category,
    _extract_idml_bullets,
    _extract_idml_specs,
    _extract_in_stock,
    _extract_price,
    _extract_rating,
    _extract_review_count,
    _extract_size_from_specs,
    _is_white_background,
    _load_pdp_data,
    _next_data_from_html,
    parse_product,
)
from app.scoring import score_pdp

# A trimmed product object shaped like Walmart's
# props.pageProps.initialData.data.product.
_PRODUCT = {
    "name": "Great Value Purified Drinking Water, 16.9 fl oz, 40 Count",
    "brand": "Great Value",
    "shortDescription": "Stay hydrated with this 40-count case of purified water.",
    "longDescription": " ".join(["Purified water is great."] * 40),
    "keyFeatures": [
        "40-count case of 16.9 fl oz bottles",
        "Purified through reverse osmosis",
        "100% recyclable bottles",
    ],
    "imageInfo": {
        "allImages": [
            {"url": "https://i5.walmartimages.com/a.jpg"},
            {"url": "https://i5.walmartimages.com/b.jpg"},
            {"url": "https://i5.walmartimages.com/c.jpg"},
            {"url": "https://i5.walmartimages.com/d.jpg"},
            {"url": ""},  # ignored
        ]
    },
    "contentLayout": {"modules": [{"type": "ProductVideo"}, {"type": "ComparisonChart"}]},
    "specifications": [
        {"name": "Brand", "value": "Great Value"},
        {"name": "Count", "value": "40"},
        {"name": "Form", "value": "Liquid"},
        {"name": "Empty", "value": ""},  # not counted
    ],
    "averageRating": 4.6,
    "numberOfReviews": 1200,
}


def test_parse_product_maps_fields():
    pdp = parse_product(_PRODUCT, url="https://www.walmart.com/ip/10294528", item_id="10294528")
    assert pdp.item_id == "10294528"
    assert pdp.title.startswith("Great Value Purified")
    assert pdp.image_count == 4  # empty url dropped
    assert pdp.has_video is True
    assert len(pdp.bullets) == 3
    assert "Purified water" in pdp.description
    assert pdp.attributes_present == 3  # empty spec value dropped
    assert pdp.brand == "Great Value"


def test_extract_size_from_specs_prefers_net_content():
    specs = [
        {"name": "Brand", "value": "Tabasco"},
        {"name": "Size", "value": "Medium"},
        {"name": "Net Content", "value": "12 oz"},
    ]
    # "net content" ranks above the generic "size".
    assert _extract_size_from_specs(specs) == "12 oz"
    # Falls back to "size" when there's no net content.
    assert _extract_size_from_specs([{"name": "Size", "value": "6 pack"}]) == "6 pack"
    # Nothing size-ish, or no specs at all -> ''.
    assert _extract_size_from_specs([{"name": "Color", "value": "Red"}]) == ""
    assert _extract_size_from_specs(None) == ""


def test_extract_category_handles_breadcrumb_string_and_absent():
    # Breadcrumb path -> leaf name.
    assert _extract_category(
        {"category": {"path": [{"name": "Food"}, {"name": "Hot Sauce"}]}}) == "Hot Sauce"
    # Delimited string -> last segment.
    assert _extract_category({"categoryPathName": "Food/Condiments/Hot Sauce"}) == "Hot Sauce"
    assert _extract_category({"productType": "Hot Sauce"}) == "Hot Sauce"
    # Absent -> '' (KW3 left blank for the user).
    assert _extract_category({}) == ""


def test_extract_brand_handles_string_dict_and_absent():
    assert _extract_brand({"brand": "  Tabasco "}) == "Tabasco"
    # Some payloads nest the brand as {"name": ...}.
    assert _extract_brand({"brand": {"name": "Frank's RedHot"}}) == "Frank's RedHot"
    # Absent or unexpected shapes read as "no brand".
    assert _extract_brand({}) == ""
    assert _extract_brand({"brand": 123}) == ""


def test_parsed_record_is_scorable():
    pdp = parse_product(_PRODUCT, url="u", item_id="10294528")
    result = score_pdp(pdp)
    assert 0 <= result.overall <= 100
    # Video + 4 images present, so imagery shouldn't be zero.
    imagery = next(d for d in result.dimensions if d.key == "imagery")
    assert imagery.score > 0


def test_parse_handles_empty_product():
    pdp = parse_product({}, url="u")
    assert pdp.title == ""
    assert pdp.image_count == 0
    assert pdp.has_video is False
    # A completely empty PDP still scores (very low) without raising.
    assert score_pdp(pdp).overall >= 0


def test_count_spec_pairs_ignores_blank_values():
    pairs = [
        {"name": "Brand", "value": "Tabasco"},
        {"name": "Heat", "value": "Medium"},
        {"name": "Missing", "value": ""},  # unfilled attribute, not counted
        {"name": "Whitespace", "value": "   "},  # effectively blank
        "junk",  # non-dict, ignored
    ]
    assert _count_spec_pairs(pairs) == 2


def test_idml_spec_pairs_override_product_json_and_mark_measured():
    """idml specs are authoritative over any product-level __NEXT_DATA__ specs.

    The full attribute set lives on data.idml.specifications, so when we pass
    those rows they win over the product node and populate the record. (Scoring
    of the attributes dimension is paused, so we assert the captured fields, not
    a dimension score — the data is still collected for when it's re-enabled.)
    """
    pairs = [{"name": f"Attr {i}", "value": str(i)} for i in range(12)]
    pdp = parse_product(_PRODUCT, url="u", item_id="10294528", spec_pairs=pairs)
    assert pdp.attributes_present == 12  # idml count, not the 3 product-JSON specs
    assert pdp.attributes_measured is True
    # Attributes are captured but intentionally not among the scored dimensions.
    assert "attributes" not in {d.key for d in score_pdp(pdp).dimensions}


def test_empty_idml_specs_still_count_as_measured():
    """An idml node present but carrying no specs is a real zero, not 'unknown'."""
    pdp = parse_product({"name": "No specs here"}, url="u", spec_pairs=[])
    assert pdp.attributes_present == 0
    assert pdp.attributes_measured is True


def test_extract_idml_specs_flat_list():
    """The preferred shape: a flat list of {name, value} rows (live Walmart)."""
    idml = {
        "specifications": [
            {"name": "Flavor", "value": "Chipotle"},
            {"name": "Material", "value": "Glass"},
            {"name": "Blank", "value": ""},  # kept here; blanks dropped by the counter
            {"value": "orphan"},  # no name, dropped
        ]
    }
    pairs = _extract_idml_specs(idml)
    assert [p["name"] for p in pairs] == ["Flavor", "Material", "Blank"]
    assert _count_spec_pairs(pairs) == 2  # blank value not counted


def test_extract_idml_specs_v2_fallback():
    """When only the grouped specificationsV2 shape exists, flatten it."""
    idml = {
        "specificationsV2": [
            {
                "groupName": "default",
                "specificationGroup": [
                    {"displayName": "Flavor", "attributeValue": ["Chipotle"]},
                    {"displayName": "Sizes", "attributeValue": ["5 oz", "12 oz"]},
                ],
            }
        ]
    }
    pairs = _extract_idml_specs(idml)
    assert {"name": "Flavor", "value": "Chipotle"} in pairs
    assert {"name": "Sizes", "value": "5 oz, 12 oz"} in pairs


def _image(bg, center=None, mode="RGB"):
    """A 200x200 test image: solid ``bg`` border with an optional center block."""
    img = Image.new(mode, (200, 200), bg)
    if center:
        for y in range(70, 130):
            for x in range(70, 130):
                img.putpixel((x, y), center)
    return img


def test_white_background_detected():
    # Product (red block) centered on a white border -> white background.
    assert _is_white_background(_image((255, 255, 255), (200, 20, 20))) is True


def test_non_white_background_rejected():
    # Colored border -> not a white background, even with a white center.
    assert _is_white_background(_image((30, 60, 90), (255, 255, 255))) is False


def test_transparent_background_treated_as_white():
    # Walmart treats transparency as white; a transparent border should pass.
    img = _image((0, 0, 0, 0), (200, 20, 20, 255), mode="RGBA")
    assert _is_white_background(img) is True


def test_extract_idml_specs_missing_node_is_unknown():
    """No idml node -> None (unknown), so the caller leaves attributes unmeasured."""
    assert _extract_idml_specs(None) is None
    assert _extract_idml_specs({}) is None


# The HTML <ul> shape Walmart ships in idml.longDescription (the PDP "Key item
# features"), including inner markup, entities, and a non-breaking space.
_LONG_DESC_HTML = (
    "<ul>\n"
    "  <li>Perfect balance of <b>smoke</b> and heat</li>\n"
    "  <li>Scoville rating of 1500-2500&nbsp;SHU</li>\n"
    "  <li>Gluten free, kosher &amp; non-GMO</li>\n"
    "  <li></li>\n"  # empty item, dropped
    "</ul>"
)


def test_extract_idml_bullets_from_long_description():
    """idml.longDescription's <li> items become clean bullet strings."""
    bullets = _extract_idml_bullets({"longDescription": _LONG_DESC_HTML})
    assert bullets == [
        "Perfect balance of smoke and heat",
        "Scoville rating of 1500-2500 SHU",  # &nbsp; normalized to a space
        "Gluten free, kosher & non-GMO",  # entity unescaped, empty <li> dropped
    ]


def test_extract_idml_bullets_absent_returns_empty():
    """No idml node, or prose (not a <ul>), yields no bullets rather than erroring."""
    assert _extract_idml_bullets(None) == []
    assert _extract_idml_bullets({}) == []
    assert _extract_idml_bullets({"longDescription": "Just a paragraph, no list."}) == []


def test_bullets_override_feeds_key_features_scoring():
    """Supplied bullets win over the (empty) product keyFeatures and get scored.

    Reproduces the live gap: a product with no keyFeatures still has its "Key
    item features" in idml.longDescription; passing those bullets should score
    the key_features dimension above zero.
    """
    resolved = _extract_idml_bullets({"longDescription": _LONG_DESC_HTML})
    pdp = parse_product({"name": "Sauce with no product.keyFeatures"},
                        url="u", bullets=resolved)
    assert len(pdp.bullets) == 3

    key_features = next(
        d for d in score_pdp(pdp).dimensions if d.key == "key_features"
    )
    assert key_features.score > 0


def test_fetch_main_image_url_returns_first_image(monkeypatch):
    # Stub the browser load so we exercise only the URL-picking logic.
    import app.fetch as fetch
    monkeypatch.setattr(fetch, "_load_pdp_data", lambda url, timeout_ms: {
        "product": {"imageInfo": {"allImages": [
            {"url": "https://i5.walmartimages.com/main.jpg"},
            {"url": "https://i5.walmartimages.com/alt.jpg"},
        ]}}
    })
    assert fetch.fetch_main_image_url("https://www.walmart.com/ip/x/1", "1") \
        == "https://i5.walmartimages.com/main.jpg"


def test_fetch_main_image_url_none_when_no_images(monkeypatch):
    import app.fetch as fetch
    monkeypatch.setattr(fetch, "_load_pdp_data", lambda url, timeout_ms: {"product": {}})
    assert fetch.fetch_main_image_url("https://www.walmart.com/ip/x/1") is None


# ── Commerce-signal extraction (daily item-tracking facts) ───────────────────

def test_extract_price_reads_current_price():
    product = {"priceInfo": {"currentPrice": {"price": 3.98, "priceString": "$3.98"}}}
    assert _extract_price(product) == (3.98, "$3.98")


def test_extract_price_handles_string_and_missing():
    # Numbers can arrive as strings; an out-of-stock item may omit the block.
    assert _extract_price({"priceInfo": {"currentPrice": {"price": "12.5"}}}) == (12.5, None)
    assert _extract_price({}) == (None, None)
    assert _extract_price({"priceInfo": {}}) == (None, None)


def test_extract_rating_direct_and_nested():
    assert _extract_rating({"averageRating": 4.6}) == 4.6
    assert _extract_rating({"rating": {"averageRating": 4.2}}) == 4.2
    assert _extract_rating({}) is None


def test_extract_review_count_direct_and_nested():
    assert _extract_review_count({"numberOfReviews": 1200}) == 1200
    assert _extract_review_count({"rating": {"numberOfReviews": 7}}) == 7
    assert _extract_review_count({}) is None


def test_extract_in_stock_status_then_cart_flag():
    assert _extract_in_stock({"availabilityStatus": "IN_STOCK"}) is True
    assert _extract_in_stock({"availabilityStatus": "OUT_OF_STOCK"}) is False
    assert _extract_in_stock({"canAddToCart": True}) is True
    assert _extract_in_stock({"canAddToCart": False}) is False
    assert _extract_in_stock({}) is None


# --- HTML-sourced __NEXT_DATA__ (the managed scraping API path) ---------------

def _wrap_next_data(product: dict) -> str:
    """Build page HTML carrying `product` in a __NEXT_DATA__ blob, as Walmart does."""
    import json

    blob = {"props": {"pageProps": {"initialData": {"data": {"product": product}}}}}
    return (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(blob)
        + "</script></body></html>"
    )


def test_next_data_from_html_returns_data_dict():
    data = _next_data_from_html(_wrap_next_data(_PRODUCT))
    assert data["product"]["name"] == _PRODUCT["name"]


def test_next_data_from_html_missing_blob_raises():
    import pytest

    from app.fetch import FetchError

    with pytest.raises(FetchError):
        _next_data_from_html("<html><body>no next data here</body></html>")


def test_load_pdp_data_uses_scraper_api_when_enabled(monkeypatch):
    """When the API is configured, _load_pdp_data parses API HTML — no browser."""
    from app import scraper_api

    monkeypatch.setattr(scraper_api, "scraper_enabled", lambda: True)
    monkeypatch.setattr(scraper_api, "fetch_html", lambda url, **kw: _wrap_next_data(_PRODUCT))

    # No Playwright import happens: the API branch returns before the browser block.
    data = _load_pdp_data("https://www.walmart.com/ip/10294528", 35000)
    assert data["product"]["name"] == _PRODUCT["name"]
