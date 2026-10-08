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
