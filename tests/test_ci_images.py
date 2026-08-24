"""Tests for the tracked-product image cache (app/ci_images.py).

Covers the path-traversal guard, the download→store→serve round trip, and the
transparency-flatten behaviour. MEDIA_DIR is redirected to a tmp dir so tests
never touch a real cache.
"""

import io

import pytest

from app import ci_images


@pytest.fixture(autouse=True)
def _tmp_media(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    return tmp_path


def _png_bytes(color=(200, 16, 46), size=(300, 300), mode="RGB"):
    from PIL import Image

    buf = io.BytesIO()
    Image.new(mode, size, color).save(buf, "PNG")
    return buf.getvalue()


def test_product_image_path_rejects_non_numeric_ids():
    assert ci_images.product_image_path("10294528").endswith("/ci_products/10294528.jpg")
    # Path-traversal / injection attempts resolve to None, never a filesystem path.
    for bad in ("../etc/passwd", "1/../../x", "abc", "", None, "12 34"):
        assert ci_images.product_image_path(bad) is None


def test_save_and_has_round_trip():
    assert ci_images.has_product_image("10294528") is False
    assert ci_images.save_product_image("10294528", _png_bytes()) is True
    assert ci_images.has_product_image("10294528") is True

    # Stored as a JPEG regardless of the source format.
    from PIL import Image
    with Image.open(ci_images.product_image_path("10294528")) as img:
        assert img.format == "JPEG"


def test_save_flattens_transparency_to_white():
    # A fully transparent RGBA image must not come out black.
    data = _png_bytes(color=(0, 0, 0, 0), mode="RGBA")
    assert ci_images.save_product_image("55", data) is True
    from PIL import Image
    with Image.open(ci_images.product_image_path("55")) as img:
        assert img.convert("RGB").getpixel((0, 0)) == (255, 255, 255)


def test_save_refuses_invalid_id():
    assert ci_images.save_product_image("../evil", _png_bytes()) is False


def test_save_handles_non_image_bytes():
    # Garbage bytes fail gracefully (best-effort), leaving nothing cached.
    assert ci_images.save_product_image("77", b"not an image") is False
    assert ci_images.has_product_image("77") is False
