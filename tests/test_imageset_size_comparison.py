"""Unit tests for the size-comparison building blocks.

Covers the pure pieces the AI scale-comparison relies on: reference-object
selection, the scene prompt, the dimensions line, the scale caption, and brand-
color auto-detection. The end-to-end generation (AI scene + bars vs. the
no-dimensions diagram fallback) is exercised in test_imageset_generate.py.
"""

import io

from PIL import Image

from app.imageset import compose, reference_objects
from app.imageset.generate import _scale_caption
from app.imageset.prompts import build_size_comparison_prompt
from app.imageset.templates import format_dimensions_line


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
    ) == '7" x 3" x 3"  ·  Net 8 oz'
    assert format_dimensions_line({"height": 18, "width": 8, "unit": "cm"}) == "18 x 8 cm"
    assert format_dimensions_line({"unit": "in"}) == ""


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
