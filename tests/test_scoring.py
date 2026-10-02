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
    # Attributes scoring is paused, so it isn't among the returned dimensions.
    assert keys == {"imagery", "title", "key_features", "description"}
    # A weak PDP should generate recommendations to act on.
    assert any(d.recommendations for d in result.dimensions)


def test_attributes_dimension_is_not_scored():
    # Attributes scoring is paused: the dimension must not appear at all, even
    # when the record carries measured attributes.
    pdp = PdpRecord(
        url="u", title="A reasonable product title with enough words here",
        image_count=6, max_image_px=2000,
        bullets=["one benefit line here", "another benefit line", "third line ok"],
        description=" ".join(["copy"] * 200),
        attributes_present=16, attributes_measured=True,
    )
    result = score_pdp(pdp)
    assert "attributes" not in {d.key for d in result.dimensions}
    # Overall is the weighted average of the dimensions that ARE present.
    expected = round(
        sum(d.score * d.weight for d in result.dimensions)
        / sum(d.weight for d in result.dimensions)
    )
    assert result.overall == expected


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


def test_white_background_blends_into_imagery():
    # Full gallery at zoom resolution -> base imagery 100. White-bg blends at 20%.
    base = dict(url="u", image_count=6, max_image_px=2000)

    def imagery(white_bg):
        pdp = PdpRecord(main_image_white_bg=white_bg, **base)
        return next(d for d in score_pdp(pdp).dimensions if d.key == "imagery").score

    assert imagery(None) == 100   # not measured -> count/resolution only
    assert imagery(True) == 100   # 100*0.8 + 100*0.2
    assert imagery(False) == 80   # 100*0.8 + 0*0.2


def test_non_white_main_image_recommends_fix():
    pdp = PdpRecord(url="u", image_count=6, max_image_px=2000, main_image_white_bg=False)
    imagery = next(d for d in score_pdp(pdp).dimensions if d.key == "imagery")
    assert any("white background" in rec for rec in imagery.recommendations)


def test_all_caps_title_is_penalized():
    caps = PdpRecord(url="u", title="THIS IS AN ALL CAPS PRODUCT TITLE HERE NOW")
    mixed = PdpRecord(url="u", title="This Is An All Caps Product Title Here Now")
    caps_title = next(d for d in score_pdp(caps).dimensions if d.key == "title")
    mixed_title = next(d for d in score_pdp(mixed).dimensions if d.key == "title")
    assert mixed_title.score > caps_title.score


def _imagery(result):
    return next(d for d in result.dimensions if d.key == "imagery")


def test_imagery_grades_every_image_and_flags_sub_spec_ones():
    # With per-image resolutions, imagery credits each image on its own tier and
    # records the sub-2000px ones (by gallery position) so the UI can name them.
    pdp = PdpRecord(url="u", image_count=6, image_dims=[
        {"url": "a.jpg", "px": 2200},   # zoom-ready
        {"url": "b.jpg", "px": 1200},   # mid  -> flagged
        {"url": "c.jpg", "px": 800},    # low  -> flagged
        {"url": "d.jpg", "px": 2000},   # zoom-ready
        {"url": "e.jpg", "px": 0},      # unmeasured -> neither graded nor flagged
        {"url": "f.jpg", "px": 2500},   # zoom-ready
    ])
    imagery = _imagery(score_pdp(pdp))

    issues = imagery.image_issues
    assert [i["index"] for i in issues] == [2, 3]          # gallery positions, in order
    assert {i["px"] for i in issues} == {1200, 800}
    assert {i["url"] for i in issues} == {"b.jpg", "c.jpg"}
    sev = {i["index"]: i["severity"] for i in issues}
    assert sev == {2: "mid", 3: "low"}

    # count 6 -> 60; resolution: measured=5, zoom_ready=3, partial=1 ->
    # credit (3 + 0.5)/5 = 0.7 -> round(40*0.7)=28. base 88, no white-bg blend.
    assert imagery.score == 88


def test_imagery_all_zoom_ready_has_no_flags():
    pdp = PdpRecord(url="u", image_count=6, image_dims=[
        {"url": "a.jpg", "px": 2000},
        {"url": "b.jpg", "px": 3000},
    ])
    imagery = _imagery(score_pdp(pdp))
    assert imagery.image_issues == []
    assert any("zoom-ready" in f for f in imagery.findings)


def test_imagery_falls_back_to_max_px_without_per_image_data():
    # No image_dims (e.g. an older result) -> legacy single-max tier, no flags.
    pdp = PdpRecord(url="u", image_count=6, max_image_px=2000)
    imagery = _imagery(score_pdp(pdp))
    assert imagery.image_issues == []
    assert imagery.score == 100  # 60 (count) + 40 (resolution)


def test_imagery_flags_white_background_with_main_image_url():
    # A non-white main image sets white_bg_url (the main image) so the UI can
    # offer a one-click fix.
    pdp = PdpRecord(url="u", image_count=6, image_dims=[{"url": "a.jpg", "px": 2200}],
                    main_image_white_bg=False, main_image_url="https://i5/main.jpg")
    assert _imagery(score_pdp(pdp)).white_bg_url == "https://i5/main.jpg"


def test_imagery_no_white_bg_url_when_compliant_or_unmeasured():
    ok = PdpRecord(url="u", image_count=6, image_dims=[{"url": "a.jpg", "px": 2200}],
                   main_image_white_bg=True, main_image_url="https://i5/main.jpg")
    assert _imagery(score_pdp(ok)).white_bg_url is None
    unknown = PdpRecord(url="u", image_count=6, image_dims=[{"url": "a.jpg", "px": 2200}],
                        main_image_white_bg=None)
    assert _imagery(score_pdp(unknown)).white_bg_url is None
