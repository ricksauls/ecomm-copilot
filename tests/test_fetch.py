"""Tests for parsing a Walmart __NEXT_DATA__ product into a PdpRecord.

Live fetching drives a real browser and isn't exercised here; the parsing that
turns the page JSON into a scorable record is what these lock down.
"""

from app.fetch import _count_spec_pairs, parse_product
from app.scoring import score_pdp

# A trimmed product object shaped like Walmart's
# props.pageProps.initialData.data.product.
_PRODUCT = {
    "name": "Great Value Purified Drinking Water, 16.9 fl oz, 40 Count",
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


def test_dom_spec_pairs_override_json_and_mark_measured():
    """DOM-scraped specs are authoritative over any __NEXT_DATA__ specs.

    Live pages lazy-load the spec table into the DOM, so when we pass scraped
    pairs they win and the dimension becomes measurable and scored.
    """
    pairs = [{"name": f"Attr {i}", "value": str(i)} for i in range(12)]
    pdp = parse_product(_PRODUCT, url="u", item_id="10294528", spec_pairs=pairs)
    assert pdp.attributes_present == 12  # DOM count, not the 3 JSON specs
    assert pdp.attributes_measured is True

    attributes = next(d for d in score_pdp(pdp).dimensions if d.key == "attributes")
    assert attributes.available is True
    assert attributes.score > 0


def test_empty_dom_specs_still_count_as_measured():
    """Reaching an empty spec section is a real zero, not an 'unknown'."""
    pdp = parse_product({"name": "No specs here"}, url="u", spec_pairs=[])
    assert pdp.attributes_present == 0
    assert pdp.attributes_measured is True

    attributes = next(d for d in score_pdp(pdp).dimensions if d.key == "attributes")
    assert attributes.available is True  # counted toward the overall
    assert attributes.score == 0
