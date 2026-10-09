"""Unit tests for the size-comparison building blocks.

Covers the pure pieces the AI scale-comparison relies on: reference-object
selection, the scene prompt, the dimensions line, the scale caption, and brand-
color auto-detection. The end-to-end generation (AI scene + bars vs. the
no-dimensions diagram fallback) is exercised in test_imageset_generate.py.
"""

import io

from PIL import Image, ImageChops, ImageDraw

from app.imageset import compose, reference_objects
from app.imageset.config import CANVAS_SIZE
from app.imageset.generate import _scale_caption
from app.imageset.prompts import build_size_comparison_prompt
from app.imageset.templates import _recenter_vertically, format_dimensions_line


def test_reference_objects_pick_closest_by_size():
    # A 7" product: banana (7.0) is within ~2% so it's skipped; the two closest
    # distinct objects are the TV remote (6.5") and the paperback book (7.8").
    phrases, names = reference_objects.reference_phrases(7.0, "in", count=2)
    assert names == ["TV remote", "paperback book"]
    assert phrases == ["a TV remote (about 6.5 in tall)", "a paperback book (about 7.8 in tall)"]


def test_reference_objects_handle_metric_and_missing():
    # 20 cm ≈ 7.87 in → same neighborhood as the 7.8" paperback.
    _, names = reference_objects.reference_phrases(20, "cm", count=2)
    assert names and all(isinstance(n, str) for n in names)
    # No usable size → no references (generation then uses the diagram fallback).
    assert reference_objects.reference_phrases(None, "in") == ([], [])
    assert reference_objects.select_reference_objects(0, "in") == []


def test_to_inches_covers_feet_and_metric():
    assert reference_objects.to_inches(7, "ft") == 84.0  # a 7-ft couch, not 7 in
    assert abs(reference_objects.to_inches(1, "m") - reference_objects.to_inches(100, "cm")) < 1e-6
    assert reference_objects.to_inches(10, "whatever") == 10.0  # unknown unit → inches


def test_longest_dimension_anchors_on_widest_axis():
    # A flat, wide keyboard: anchor on its 16" width, not its 0.13" thickness.
    dims = {"width": 16, "height": 0.13, "depth": 6.3, "unit": "in"}
    assert reference_objects.longest_dimension(dims) == 16
    _, names = reference_objects.reference_phrases(
        reference_objects.longest_dimension(dims), "in", count=2)
    # Closest everyday objects to 16" are the ruler + wine bottle, not a golf ball.
    assert "12-inch ruler" in names and "golf ball" not in names


def test_longest_dimension_none_without_dims():
    assert reference_objects.longest_dimension({"unit": "in"}) is None
    assert reference_objects.longest_dimension({"width": 0, "unit": "in"}) is None


def test_is_comparable_size_gates_large_products():
    # Small / tabletop products compare well against everyday objects.
    assert reference_objects.is_comparable_size({"height": 7.8, "unit": "in"})
    assert reference_objects.is_comparable_size({"width": 20, "unit": "cm"})
    # Furniture-scale items are too large — longest dimension past the cap.
    assert not reference_objects.is_comparable_size({"width": 84, "height": 36, "unit": "in"})
    assert not reference_objects.is_comparable_size({"height": 7, "unit": "ft"})  # 84 in
    # No usable dimensions → not comparable (diagram fallback).
    assert not reference_objects.is_comparable_size({"unit": "in"})
    assert not reference_objects.is_comparable_size({"width": 0, "unit": "in"})


def test_size_comparison_prompt_contains_scale_contract():
    prompt = build_size_comparison_prompt(
        product_name="OFF! Deep Woods", product_height="7 in", product_width="3 in",
        reference_phrases=["a TV remote (about 6.5 in tall)"])
    assert "OFF! Deep Woods" in prompt and "7 in tall" in prompt
    assert "a TV remote (about 6.5 in tall)" in prompt
    # The model must preserve the product and draw no text (figures are composited).
    assert "Preserve the supplied product" in prompt
    assert "rulers, or measurement labels" in prompt


def test_format_dimensions_line():
    assert format_dimensions_line(
        {"height": 7, "width": 3, "depth": 3, "unit": "in", "weight": "8 oz"}
    ) == '7" × 3" × 3"  ·  Net 8 oz'
    assert format_dimensions_line({"height": 18, "width": 8, "unit": "cm"}) == "18 × 8 cm"
    assert format_dimensions_line({"unit": "in"}) == ""


def _glyph_ink(font, ch):
    m = font.getmask(ch)
    return sum(bytes(m))


def test_bar_font_is_inter_with_real_glyphs():
    """Bars use bundled Inter (real bold), and "×" is a real glyph (not tofu)."""
    bold = compose.font(72, bold=True)
    assert "Inter" in " ".join(bold.getname())
    # A real "×" differs from the font's .notdef box (the old default font failed this).
    assert _glyph_ink(bold, "×") not in (0, _glyph_ink(bold, ""))
    # Bold is heavier than regular at the same size (true bold, not stroke-faked).
    assert _glyph_ink(bold, "x") > _glyph_ink(compose.font(72), "x")


def test_scale_caption_grammar():
    assert _scale_caption(["TV remote", "paperback book"]) == \
        "Shown next to a TV remote and a paperback book for scale"
    assert _scale_caption(["AA battery"]) == "Shown next to an AA battery for scale"
    assert _scale_caption([]) == ""


def test_detect_dominant_color_finds_brand_hue():
    # A mostly-red product on a transparent background → a reddish detected color.
    img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
    for y in range(8, 32):
        for x in range(12, 28):
            img.putpixel((x, y), (200, 20, 20, 255))
    hex_color = compose.detect_dominant_color(img)
    assert hex_color is not None
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    assert r > 120 and g < 90 and b < 90  # clearly in the red family


def test_detect_dominant_color_none_on_greyscale():
    # Pure white/grey product → no brand hue → None (caller falls back to default).
    img = Image.new("RGBA", (20, 20), (255, 255, 255, 255))
    assert compose.detect_dominant_color(img) is None


def test_detect_dominant_color_survives_png_roundtrip():
    img = Image.new("RGBA", (20, 20), (20, 120, 200, 255))
    loaded = Image.open(io.BytesIO(compose.to_png_bytes(img)))
    assert compose.detect_dominant_color(loaded) is not None


def _content_mid(img):
    bbox = ImageChops.difference(
        img, Image.new("RGB", img.size, (255, 255, 255))).getbbox()
    return (bbox[1] + bbox[3]) / 2 if bbox else None


def test_recenter_vertically_centers_content_in_band():
    # Content sitting high on a white field is moved to the band's vertical center.
    size = CANVAS_SIZE
    img = Image.new("RGB", (size, size), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([800, 200, 1200, 600], fill=(10, 10, 10))
    band_top, band_bottom = round(size * 0.085), size - round(size * 0.145)
    out = _recenter_vertically(img, band_top, band_bottom)
    assert abs(_content_mid(out) - (band_top + band_bottom) / 2) <= 2


def test_recenter_vertically_noop_on_blank():
    size = CANVAS_SIZE
    blank = Image.new("RGB", (size, size), (255, 255, 255))
    out = _recenter_vertically(blank, 170, 1710)
    assert ImageChops.difference(out, blank).getbbox() is None  # unchanged, still blank


def test_recenter_vertically_ignores_faint_noise():
    # A faint near-white speck (below threshold) must not anchor the content box.
    size = CANVAS_SIZE
    img = Image.new("RGB", (size, size), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([900, 300, 1100, 500], fill=(0, 0, 0))     # real content
    ImageDraw.Draw(img).point([(10, size - 10)], fill=(252, 252, 252))        # faint speck
    band_top, band_bottom = 170, size - 290
    out = _recenter_vertically(img, band_top, band_bottom)
    # Centered on the real block (mid 400 → band center), not dragged toward the speck.
    assert abs(_content_mid(out) - (band_top + band_bottom) / 2) <= 3
