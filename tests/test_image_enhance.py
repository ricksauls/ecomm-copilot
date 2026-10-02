"""Tests for the config-gated AI image-upscaling client and its cache."""

import pytest

from app import ci_images, image_enhance


class _FakeResp:
    def __init__(self, status=200, json_data=None, content=b"", text=""):
        self.status_code = status
        self._json = json_data
        self.content = content
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_is_configured_gating(monkeypatch):
    monkeypatch.delenv("IMAGE_UPSCALE_API_KEY", raising=False)
    monkeypatch.setenv("IMAGE_UPSCALE_PROVIDER", "claid")
    assert image_enhance.is_configured() is False
    monkeypatch.setenv("IMAGE_UPSCALE_API_KEY", "secret-key")
    assert image_enhance.is_configured() is True
    # Unknown provider is treated as not configured.
    monkeypatch.setenv("IMAGE_UPSCALE_PROVIDER", "somethingelse")
    assert image_enhance.is_configured() is False


def test_enhance_raises_when_not_configured(monkeypatch):
    monkeypatch.delenv("IMAGE_UPSCALE_API_KEY", raising=False)
    with pytest.raises(image_enhance.EnhanceNotConfigured):
        image_enhance.enhance("https://img.example/x.jpg")


def test_output_format_helpers(monkeypatch):
    monkeypatch.setenv("IMAGE_UPSCALE_OUTPUT_FORMAT", "png")
    assert image_enhance.output_format() == "png"
    assert image_enhance.output_ext() == "png"
    assert image_enhance.output_mime() == "image/png"
    monkeypatch.setenv("IMAGE_UPSCALE_OUTPUT_FORMAT", "jpeg")
    assert image_enhance.output_ext() == "jpg"
    assert image_enhance.output_mime() == "image/jpeg"


def test_claid_upscale_success(monkeypatch):
    monkeypatch.setenv("IMAGE_UPSCALE_PROVIDER", "claid")
    monkeypatch.setenv("IMAGE_UPSCALE_API_KEY", "secret-key")
    monkeypatch.setenv("IMAGE_UPSCALE_MODE", "smart_enhance")
    monkeypatch.setenv("IMAGE_UPSCALE_TARGET_PX", "2000")

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["auth"] = headers.get("Authorization")
        return _FakeResp(200, json_data={"data": {"output": {
            "tmp_url": "https://storage.example/out.jpg", "width": 2000, "height": 2000}}})

    def fake_get(url, timeout=None):
        captured["download_url"] = url
        return _FakeResp(200, content=b"UPSCALED-BYTES")

    monkeypatch.setattr("requests.post", fake_post)
    monkeypatch.setattr("requests.get", fake_get)

    out = image_enhance.enhance("https://i5.walmartimages.com/seo/x.jpg")
    assert out == b"UPSCALED-BYTES"
    # Request shape: source URL in, correct endpoint/auth/operations.
    assert captured["url"] == "https://api.claid.ai/v1/image/edit"
    assert captured["auth"] == "Bearer secret-key"
    assert captured["json"]["input"] == "https://i5.walmartimages.com/seo/x.jpg"
    assert captured["json"]["operations"]["restorations"]["upscale"] == "smart_enhance"
    assert captured["json"]["operations"]["resizing"]["width"] == 2000
    assert captured["download_url"] == "https://storage.example/out.jpg"


def test_claid_upscale_provider_error(monkeypatch):
    monkeypatch.setenv("IMAGE_UPSCALE_PROVIDER", "claid")
    monkeypatch.setenv("IMAGE_UPSCALE_API_KEY", "secret-key")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(402, text="payment required"))
    with pytest.raises(image_enhance.EnhanceError):
        image_enhance.enhance("https://img.example/x.jpg")


def test_claid_upscale_missing_output_url(monkeypatch):
    monkeypatch.setenv("IMAGE_UPSCALE_PROVIDER", "claid")
    monkeypatch.setenv("IMAGE_UPSCALE_API_KEY", "secret-key")
    monkeypatch.setattr("requests.post", lambda *a, **k: _FakeResp(200, json_data={"data": {"output": {}}}))
    with pytest.raises(image_enhance.EnhanceError):
        image_enhance.enhance("https://img.example/x.jpg")


# --- enhanced-image cache (ci_images) ---------------------------------------

def test_enhanced_image_path_guards(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    assert ci_images.enhanced_image_path("abc", 1) is None       # non-digit key
    assert ci_images.enhanced_image_path("5", "x") is None        # non-int index
    assert ci_images.enhanced_image_path("5", 1, "gif") is None   # ext not allowed
    assert ci_images.enhanced_image_path("5", 1, "jpg").endswith("enhanced/5-1.jpg")


def test_enhanced_image_cache_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    assert ci_images.has_enhanced_image("5", 2) is False
    assert ci_images.save_enhanced_image("5", 2, b"IMGDATA") is True
    assert ci_images.has_enhanced_image("5", 2) is True
    assert ci_images.save_enhanced_image("bad-key", 2, b"x") is False  # guard rejects
