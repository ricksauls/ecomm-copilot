"""Tests for the rule-based PDP scorer."""

from app.scoring import PdpRecord, WEIGHTS, score_pdp


def _strong_pdp() -> PdpRecord:
    return PdpRecord(
        url="https://www.walmart.com/ip/10294528",
        item_id="10294528",
        title="Great Value Purified Drinking Water, 16.9 fl oz, 40 Count",  # ~57 chars
        image_count=8,
        max_image_px=2000,
        has_video=True,
        bullets=[
            "40-count case of 16.9 fl oz bottles for home or on the go",
            "Purified through reverse osmosis for a clean, crisp taste",
            "100% recyclable bottles",
            "Sealed for freshness and safety",
            "Great for events, lunches, and emergencies",
        ],
        description=" ".join(["water"] * 320),
        attributes_present=16,
        attributes_measured=True,
    )


def _weak_pdp() -> PdpRecord:
    return PdpRecord(
        url="https://www.walmart.com/ip/999",
        item_id="999",
        title="WATER",
        image_count=1,
        max_image_px=600,
        has_video=False,
        bullets=[],
        description="Water bottle.",
        attributes_present=2,
        attributes_measured=True,
    )


def test_weights_sum_to_100():
    assert sum(WEIGHTS.values()) == 100


def test_strong_pdp_scores_high():
    result = score_pdp(_strong_pdp())
    assert result.overall >= 85
    assert all(0 <= d.score <= 100 for d in result.dimensions)


def test_weak_pdp_scores_low():
    result = score_pdp(_weak_pdp())
    assert result.overall <= 40


def test_overall_is_weighted_average():
    result = score_pdp(_strong_pdp())
    total_weight = sum(d.weight for d in result.dimensions)
    expected = round(sum(d.score * d.weight for d in result.dimensions) / total_weight)
    assert result.overall == expected


def test_every_dimension_present_and_scored():
    result = score_pdp(_weak_pdp())
    keys = {d.key for d in result.dimensions}
    assert keys == {"imagery", "attributes", "title", "key_features", "description"}
    # A weak PDP should generate recommendations to act on.
    assert any(d.recommendations for d in result.dimensions)


def test_unmeasured_attributes_are_excluded_from_overall():
    # Default attributes_measured=False -> attributes dimension is unavailable
    # and must not drag the overall down.
    pdp = PdpRecord(
        url="u", title="A reasonable product title with enough words here",
        image_count=6, max_image_px=2000, has_video=True,
        bullets=["one benefit line here", "another benefit line", "third line ok"],
        description=" ".join(["copy"] * 200),
    )
    result = score_pdp(pdp)
    attrs = next(d for d in result.dimensions if d.key == "attributes")
    assert attrs.available is False
    # Overall is computed over the other four dimensions only.
    scored = [d for d in result.dimensions if d.available]
    expected = round(
        sum(d.score * d.weight for d in scored) / sum(d.weight for d in scored)
    )
    assert result.overall == expected
    assert result.overall >= 80  # a strong PDP isn't punished for the missing metric


def test_video_does_not_affect_imagery_score():
    """Video is not scored right now, so has_video must not change imagery.

    A full gallery at zoom resolution should reach 100 with or without a video.
    """
    base = dict(url="u", image_count=6, max_image_px=2000)
    with_video = score_pdp(PdpRecord(has_video=True, **base))
    without_video = score_pdp(PdpRecord(has_video=False, **base))

    img_with = next(d for d in with_video.dimensions if d.key == "imagery")
    img_without = next(d for d in without_video.dimensions if d.key == "imagery")
    assert img_with.score == img_without.score == 100


def test_all_caps_title_is_penalized():
    caps = PdpRecord(url="u", title="THIS IS AN ALL CAPS PRODUCT TITLE HERE NOW")
    mixed = PdpRecord(url="u", title="This Is An All Caps Product Title Here Now")
    caps_title = next(d for d in score_pdp(caps).dimensions if d.key == "title")
    mixed_title = next(d for d in score_pdp(mixed).dimensions if d.key == "title")
    assert mixed_title.score > caps_title.score
