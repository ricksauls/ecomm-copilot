"""Unit tests for the Pillow composition primitives + the size-comparison template."""

import io

from PIL import Image, ImageDraw

from app.imageset import compose, templates
from app.imageset.config import CANVAS_SIZE


def _scene(color=(120, 120, 120)):
    return Image.new("RGB", (1024, 1024), color)


def _cutout():
    # Red block on transparency (a stand-in product cutout).
    img = Image.new("RGBA", (200, 500), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([40, 20, 160, 480], fill=(200, 20, 20, 255))
    return img


def _block(w, h):
    # A filled block of exactly w×h content on a larger transparent canvas, so the
    # measured aspect comes from the content bbox, not the canvas size.
    img = Image.new("RGBA", (w + 100, h + 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([50, 50, 50 + w - 1, 50 + h - 1], fill=(10, 120, 60, 255))
    return img


def test_content_aspect_ratio_uses_content_bbox():
    # Measured from the non-transparent content, ignoring transparent padding.
    assert compose.content_aspect_ratio(_block(400, 100)) > 1.25   # wide
    assert compose.content_aspect_ratio(_block(100, 400)) < 0.8    # tall
    assert abs(compose.content_aspect_ratio(_block(300, 300)) - 1.0) < 0.05  # square


def test_content_aspect_ratio_blank_is_one():
    # Fully transparent → falls back to the full image size (square here), never 0-div.
    assert compose.content_aspect_ratio(Image.new("RGBA", (300, 300), (0, 0, 0, 0))) == 1.0


def test_trim_to_content_crops_rgb_colored_transparency():
    # A transparent background that still carries RGB (what defeats a plain getbbox
    # and leaves PhotoRoom cutouts padded) must still be trimmed to the alpha content.
    img = Image.new("RGBA", (400, 400), (200, 200, 200, 0))  # alpha 0 but RGB set
    ImageDraw.Draw(img).rectangle([120, 90, 259, 209], fill=(10, 10, 10, 255))
    out = compose.trim_to_content(img)
    assert out.size == (140, 120)  # tight to the opaque block, border dropped


def test_center_callout_gives_features_the_larger_band():
    # A wide product's callout keeps a short product band so the feature rows get
    # the bulk of the height (no cramming).
    from app.imageset.config import SAFE_MARGIN
    from app.imageset.templates import _feature_callout_regions
    (_, _, _, prod_h), (_, _, _, feat_h) = _feature_callout_regions(
        CANVAS_SIZE, SAFE_MARGIN, "product-center")
    assert feat_h > prod_h


def test_detect_light_side_bright_right():
    # Brighter on the right half of the top → shadow should fall to the "left".
    scene = Image.new("RGB", (400, 400), (40, 40, 40))
    ImageDraw.Draw(scene).rectangle([200, 0, 400, 200], fill=(240, 240, 240))
    assert compose.detect_light_side(scene) == "left"


def test_scene_shadow_scale_is_bounded():
    dark = compose.scene_shadow_scale(Image.new("RGB", (200, 200), (20, 20, 20)))
    bright = compose.scene_shadow_scale(Image.new("RGB", (200, 200), (250, 250, 250)))
    assert dark == 1.0  # dark ground holds full strength (capped at 1.0)
    assert bright == 0.6  # bright ground eases shadow to the floor
    assert 0.6 <= dark <= 1.0 and 0.6 <= bright <= 1.0


def test_fit_cutout_preserves_aspect():
    fitted = compose.fit_cutout(_cutout(), 1000, 1000)
    # Original is 200×500 (0.4 aspect); fitting into a square keeps that ratio.
    assert fitted.height == 1000
    assert abs(fitted.width / fitted.height - 200 / 500) < 0.02


def test_composite_product_on_scene_output():
    out = compose.composite_product_on_scene(
        _scene(), _cutout(), blur_background=True, cast_shadow=True
    )
    assert out.size == (CANVAS_SIZE, CANVAS_SIZE)
    assert out.mode == "RGB"


def test_sample_scene_light_returns_colour_and_luma():
    rgb, lum = compose.sample_scene_light(_scene((200, 100, 50)))
    # Near the flat scene's colour (JPEG-free, so basically exact).
    assert abs(rgb[0] - 200) < 6 and abs(rgb[1] - 100) < 6 and abs(rgb[2] - 50) < 6
    assert 100 < lum < 160


def test_relight_cutout_preserves_size_alpha_and_only_darkens_on_dark_scene():
    cut = _cutout()
    relit = compose.relight_cutout(
        cut, ambient_rgb=(20, 20, 30), ambient_lum=30.0, light_side="left", strength=0.5)
    # Same dimensions + alpha channel preserved (product geometry untouched).
    assert relit.size == cut.size
    assert relit.getchannel("A").getbbox() == cut.getchannel("A").getbbox()
    # A bright red product on a dark scene is toned down (never brightened).
    cx, cy = 100, 250  # inside the block
    before = cut.convert("RGB").getpixel((cx, cy))
    after = relit.convert("RGB").getpixel((cx, cy))
    assert sum(after) < sum(before)


def test_relight_zero_strength_is_a_no_op_on_colour():
    cut = _cutout()
    relit = compose.relight_cutout(
        cut, ambient_rgb=(10, 10, 10), ambient_lum=10.0, light_side="left", strength=0.0)
    assert relit.convert("RGB").getpixel((100, 250)) == cut.convert("RGB").getpixel((100, 250))


def test_composite_with_contact_ao_output():
    out = compose.composite_product_on_scene(
        _scene(), _cutout(), blur_background=True, cast_shadow=True, contact_ao=0.7)
    assert out.size == (CANVAS_SIZE, CANVAS_SIZE)
    assert out.mode == "RGB"


def test_contact_shadow_has_dark_pixels():
    layer = compose.contact_shadow(600, 0.6, size=CANVAS_SIZE)
    assert layer.mode == "RGBA"
    # Center should carry shadow alpha (the grounding pool sits at canvas center).
    assert layer.getpixel((CANVAS_SIZE // 2, CANVAS_SIZE // 2))[3] > 0


def test_create_thumbnail_is_square():
    out = compose.create_thumbnail(Image.new("RGB", (CANVAS_SIZE, CANVAS_SIZE), (10, 20, 30)))
    assert out.size == (480, 480)


def test_size_comparison_renders_with_dimensions():
    png = templates.create_size_comparison(
        title="Exact dimensions",
        dimensions={"width": 2.2, "height": 7.8, "depth": 2.2, "unit": "in", "weight": "6 oz"},
        cutout=_cutout(),
    )
    img = Image.open(io.BytesIO(png))
    assert img.size == (CANVAS_SIZE, CANVAS_SIZE)


def test_size_comparison_without_dimensions_shows_prompt():
    # No numeric dims → still renders (with the "add dimensions" note), never crashes.
    png = templates.create_size_comparison(
        title="Size",
        dimensions={"width": None, "height": None, "depth": None, "unit": "", "weight": ""},
        cutout=_cutout(),
    )
    assert Image.open(io.BytesIO(png)).size == (CANVAS_SIZE, CANVAS_SIZE)
