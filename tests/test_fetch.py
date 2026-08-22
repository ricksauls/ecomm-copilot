"""Tests for parsing a Walmart __NEXT_DATA__ product into a PdpRecord.

Live fetching drives a real browser and isn't exercised here; the parsing that
turns the page JSON into a scorable record is what these lock down.
"""

from PIL import Image

from app.fetch import (
    _count_spec_pairs,
    _extract_idml_bullets,
    _extract_idml_specs,
    _is_white_background,
    parse_product,
)
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
