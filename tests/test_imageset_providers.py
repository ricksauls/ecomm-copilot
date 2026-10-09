"""Tests for the image-set provider shims (image generation + background removal).

The mock providers must run fully offline and deterministically; the real
providers (OpenAI gpt-image, remove.bg) are exercised against a fake ``requests``
so the request shape, auth header, and response handling are verified without a
network call or a key.
"""

import base64
import io

from PIL import Image

from app.imageset import config
from app.imageset.providers import background_removal as bg
from app.imageset.providers import image_generation as ig


def _png_bytes(size=(8, 8), color=(10, 120, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def _b64_png() -> str:
    return base64.b64encode(_png_bytes()).decode("ascii")


class _FakeResp:
    def __init__(self, status=200, json_data=None, content=b"", text="", headers=None):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


# --- provider selection + config -------------------------------------------

def test_provider_selection_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("IMAGESET_IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("IMAGESET_BG_PROVIDER", raising=False)
    assert isinstance(ig.get_image_generation_provider(), ig.MockImageGenerationProvider)
    assert isinstance(bg.get_background_removal_provider(), bg.MockBackgroundRemovalProvider)


def test_provider_selection_openai_and_removebg(monkeypatch):
    monkeypatch.setenv("IMAGESET_IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "removebg")
    assert isinstance(ig.get_image_generation_provider(), ig.OpenAIImageProvider)
    assert isinstance(bg.get_background_removal_provider(), bg.RemoveBgProvider)


def test_price_per_image_contract(monkeypatch):
    monkeypatch.delenv("IMAGESET_PRICE_PER_IMAGE", raising=False)
    assert config.price_per_image() == 0.04
    monkeypatch.setenv("IMAGESET_PRICE_PER_IMAGE", "0.12")
    assert config.price_per_image() == 0.12
    monkeypatch.setenv("IMAGESET_PRICE_PER_IMAGE", "")  # opt out of the figure
    assert config.price_per_image() is None
    monkeypatch.setenv("IMAGESET_PRICE_PER_IMAGE", "nope")  # typo never hides cost
    assert config.price_per_image() == 0.04


def test_bg_is_configured_gating(monkeypatch):
    # The mock is always ready; remove.bg needs its key.
    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "mock")
    assert bg.is_configured() is True
    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "removebg")
    monkeypatch.delenv("BACKGROUND_REMOVAL_API_KEY", raising=False)
    assert bg.is_configured() is False
    monkeypatch.setenv("BACKGROUND_REMOVAL_API_KEY", "rb-key")
    assert bg.is_configured() is True


# --- mock image generation --------------------------------------------------

def test_mock_generate_is_deterministic_and_valid_png():
    prov = ig.MockImageGenerationProvider()
    a = prov.generate_image(prompt="a sunny kitchen", size="256x256")
    b = prov.generate_image(prompt="a sunny kitchen", size="256x256")
    assert a.png == b.png  # same prompt → identical scene (stable tests)
    assert a.width == 256 and a.height == 256
    assert a.estimated_cost_usd == 0.0
    Image.open(io.BytesIO(a.png)).verify()
    # A different prompt yields a different scene.
    c = prov.generate_image(prompt="a rainy street", size="256x256")
    assert c.png != a.png


def test_mock_edit_places_product_on_scene():
    prov = ig.MockImageGenerationProvider()
    res = prov.edit_image(prompt="held outdoors", image=_png_bytes((40, 40)), size="256x256")
    assert res.width == 256 and res.height == 256
    Image.open(io.BytesIO(res.png)).verify()


# --- OpenAI image generation (fake requests) --------------------------------

def test_openai_generate_request_shape_and_decode(monkeypatch):
    monkeypatch.setenv("IMAGESET_IMAGE_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-2")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured.update(url=url, json=json, auth=headers.get("Authorization"), timeout=timeout)
        return _FakeResp(200, json_data={"data": [{"b64_json": _b64_png()}]})

    monkeypatch.setattr("requests.post", fake_post)
    res = ig.OpenAIImageProvider().generate_image(prompt="scene", size="1024x1024")
    assert res.provider == "openai" and res.width == 1024
    assert captured["url"] == "https://api.openai.com/v1/images/generations"
    assert captured["auth"] == "Bearer sk-secret"  # key sent, never logged
    assert captured["json"]["model"] == "gpt-image-2"
    assert captured["json"]["size"] == "1024x1024"
    Image.open(io.BytesIO(res.png)).verify()


def test_openai_bad_size_is_coerced(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, **kw):
        captured["size"] = json["size"]
        return _FakeResp(200, json_data={"data": [{"b64_json": _b64_png()}]})

    monkeypatch.setattr("requests.post", fake_post)
    ig.OpenAIImageProvider().generate_image(prompt="x", size="4000x4000")
    assert captured["size"] == "1024x1024"  # unsupported → default


def test_openai_missing_key_raises_not_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    import pytest

    with pytest.raises(ig.ImageGenNotConfigured):
        ig.OpenAIImageProvider().generate_image(prompt="x")


def test_openai_provider_error_and_bad_shape(monkeypatch):
    import pytest

    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(429, text="rate limited"))
    with pytest.raises(ig.ImageGenError):
        ig.OpenAIImageProvider().generate_image(prompt="x")
    # 200 but empty data → treated as a failure, not a silent pass.
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(200, json_data={"data": []}))
    with pytest.raises(ig.ImageGenError):
        ig.OpenAIImageProvider().generate_image(prompt="x")


# --- mock background removal ------------------------------------------------

def test_mock_cutout_clears_white_border_keeps_product():
    # Red disc centered on white → border cleared, center preserved, trimmed tight.
    img = Image.new("RGB", (120, 120), (255, 255, 255))
    from PIL import ImageDraw

    ImageDraw.Draw(img).ellipse([30, 30, 90, 90], fill=(200, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    res = bg.MockBackgroundRemovalProvider().remove_background(data=buf.getvalue())
    out = Image.open(io.BytesIO(res.png)).convert("RGBA")
    # Trimmed to roughly the disc bounds (not the full 120px frame).
    assert out.width < 100 and out.height < 100
    assert out.getpixel((out.width // 2, out.height // 2))[3] == 255  # product opaque
    assert out.getpixel((0, 0))[3] == 0  # a trimmed corner stays transparent


# --- remove.bg (fake requests) ----------------------------------------------

def test_removebg_request_shape_and_trim(monkeypatch):
    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "removebg")
    monkeypatch.setenv("BACKGROUND_REMOVAL_API_KEY", "rb-secret")
    captured = {}

    # remove.bg returns a transparent PNG; include a transparent margin to prove trim.
    canvas = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
    canvas.paste((0, 200, 0, 255), (20, 20, 40, 40))
    rb_buf = io.BytesIO()
    canvas.save(rb_buf, format="PNG")

    def fake_post(url, data=None, files=None, headers=None, timeout=None, **kw):
        captured.update(url=url, apikey=headers.get("X-Api-Key"), fields=data, has_file="image_file" in (files or {}))
        return _FakeResp(200, content=rb_buf.getvalue(), headers={"x-credits-charged": "1"})

    monkeypatch.setattr("requests.post", fake_post)
    res = bg.RemoveBgProvider().remove_background(data=_png_bytes(), content_type="image/png")
    assert captured["url"] == "https://api.remove.bg/v1.0/removebg"
    assert captured["apikey"] == "rb-secret"
    assert captured["fields"]["format"] == "png" and captured["has_file"]
    assert res.provider_ref == "1"
    out = Image.open(io.BytesIO(res.png)).convert("RGBA")
    assert out.size == (20, 20)  # trimmed to the opaque square


def test_removebg_missing_key_raises(monkeypatch):
    import pytest

    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "removebg")
    monkeypatch.delenv("BACKGROUND_REMOVAL_API_KEY", raising=False)
    with pytest.raises(bg.BackgroundRemovalNotConfigured):
        bg.RemoveBgProvider().remove_background(data=_png_bytes())


def test_removebg_provider_error_is_generic(monkeypatch):
    import pytest

    monkeypatch.setenv("BACKGROUND_REMOVAL_API_KEY", "rb-secret")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(403, text="account suspended: id=123"))
    with pytest.raises(bg.BackgroundRemovalError) as exc:
        bg.RemoveBgProvider().remove_background(data=_png_bytes())
    # The raw body (which can carry account details) must not leak into the message.
    assert "account suspended" not in str(exc.value)


# --- PhotoRoom (fake requests) ----------------------------------------------

def test_provider_selection_photoroom(monkeypatch):
    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "photoroom")
    assert isinstance(bg.get_background_removal_provider(), bg.PhotoRoomProvider)


def test_photoroom_request_shape_and_trim(monkeypatch):
    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "photoroom")
    # A sandbox key is passed through unchanged (PhotoRoom enables sandbox by prefix).
    monkeypatch.setenv("BACKGROUND_REMOVAL_API_KEY", "sandbox_pr-secret")
    captured = {}

    # PhotoRoom returns a transparent PNG; include a transparent margin to prove trim.
    canvas = Image.new("RGBA", (50, 50), (0, 0, 0, 0))
    canvas.paste((0, 180, 220, 255), (15, 15, 35, 35))
    pr_buf = io.BytesIO()
    canvas.save(pr_buf, format="PNG")

    def fake_post(url, data=None, files=None, headers=None, timeout=None, **kw):
        captured.update(url=url, apikey=headers.get("x-api-key"),
                        fmt=(data or {}).get("format"), has_file="image_file" in (files or {}))
        return _FakeResp(200, content=pr_buf.getvalue())

    monkeypatch.setattr("requests.post", fake_post)
    res = bg.PhotoRoomProvider().remove_background(data=_png_bytes(), content_type="image/png")
    assert captured["url"] == "https://sdk.photoroom.com/v1/segment"
    assert captured["apikey"] == "sandbox_pr-secret"  # key (incl. sandbox prefix) sent as-is
    assert captured["fmt"] == "png" and captured["has_file"]
    assert res.provider == "photoroom"
    out = Image.open(io.BytesIO(res.png)).convert("RGBA")
    assert out.size == (20, 20)  # trimmed to the opaque square


def test_mock_edit_transparent_returns_product_on_transparency():
    # The straighten step asks for a transparent cutout; the mock returns the product
    # centered on transparency (not a filled scene).
    res = ig.MockImageGenerationProvider().edit_image(
        prompt="straighten", image=_png_bytes((40, 40)), background="transparent")
    out = Image.open(io.BytesIO(res.png)).convert("RGBA")
    assert out.getchannel("A").getbbox() is not None  # has opaque product content
    assert out.getpixel((0, 0))[3] == 0  # corner is transparent


def test_openai_edit_sends_transparent_background(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    captured = {}

    def fake_post(url, data=None, files=None, headers=None, timeout=None, **kw):
        captured.update(bg=(data or {}).get("background"))
        return _FakeResp(200, json_data={"data": [{"b64_json": _b64_png()}]})

    monkeypatch.setattr("requests.post", fake_post)
    ig.OpenAIImageProvider().edit_image(prompt="x", image=_png_bytes(), background="transparent")
    assert captured["bg"] == "transparent"
    # The default (opaque) path omits the field, leaving existing callers untouched.
    ig.OpenAIImageProvider().edit_image(prompt="x", image=_png_bytes())
    assert captured["bg"] is None


def test_photoroom_missing_key_raises(monkeypatch):
    import pytest

    monkeypatch.setenv("IMAGESET_BG_PROVIDER", "photoroom")
    monkeypatch.delenv("BACKGROUND_REMOVAL_API_KEY", raising=False)
    with pytest.raises(bg.BackgroundRemovalNotConfigured):
        bg.PhotoRoomProvider().remove_background(data=_png_bytes())


def test_photoroom_provider_error_is_generic(monkeypatch):
    import pytest

    monkeypatch.setenv("BACKGROUND_REMOVAL_API_KEY", "pr-secret")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(402, text="quota exceeded: acct=99"))
    with pytest.raises(bg.BackgroundRemovalError) as exc:
        bg.PhotoRoomProvider().remove_background(data=_png_bytes())
    assert "quota exceeded" not in str(exc.value)  # no account details leak
