"""AI image generation provider for the image-set pipeline.

Two operations, both returning PNG bytes:

- :meth:`generate_image` — create a scene/background from a text prompt
  (lifestyle backdrops; the product is composited in afterward).
- :meth:`edit_image` — transform a supplied product image with a prompt
  (product-in-use renders).

The real provider is **OpenAI gpt-image-2**, called over HTTPS with ``requests``
(the same dependency-light style as :mod:`app.image_enhance`'s Claid calls, so no
new SDK is pulled in). A deterministic **mock** renders prompt-seeded gradient
scenes with Pillow so the whole pipeline — generation, compositing, review,
export — runs offline in tests with no key or spend. The provider is selected by
``IMAGESET_IMAGE_PROVIDER`` and defaults to the mock, leaving the feature inert
until an operator sets a key (see :mod:`app.imageset.config`).

The OpenAI key is read from the environment and never logged (security-standards).
"""

import base64
import logging

from app.imageset import config

logger = logging.getLogger(__name__)

# OpenAI image endpoints (gpt-image family returns base64 PNG data).
_GENERATE_URL = "https://api.openai.com/v1/images/generations"
_EDIT_URL = "https://api.openai.com/v1/images/edits"
# gpt-image can take real time to render; the download is part of the same JSON
# response (base64), so a single generous timeout covers the whole call.
_REQUEST_TIMEOUT_S = 180

# Sizes the OpenAI image API accepts. Anything else is coerced to the default so a
# bad caller value degrades to a valid request rather than a provider 400.
_ALLOWED_SIZES = ("1024x1024", "1536x1024", "1024x1536")
_DEFAULT_SIZE = "1024x1024"

# Rough per-image cost for observability/estimates only (see config.price_per_image).
_APPROX_COST_PER_IMAGE_USD = 0.04


class ImageGenError(RuntimeError):
    """Image generation failed (provider error, timeout, or unexpected response)."""


class ImageGenNotConfigured(ImageGenError):
    """No image-generation provider/key configured for the OpenAI provider."""


class ImageGenerationResult:
    """The generated image plus metadata for cost/observability.

    A plain value object (bytes in, bytes out) so the pipeline stays decoupled
    from any web framework or storage layer.
    """

    def __init__(
        self,
        *,
        png: bytes,
        provider: str,
        model: str,
        width: int,
        height: int,
        provider_ref: str | None = None,
        estimated_cost_usd: float | None = None,
    ) -> None:
        self.png = png
        self.provider = provider
        self.model = model
        self.width = width
        self.height = height
        self.provider_ref = provider_ref
        self.estimated_cost_usd = estimated_cost_usd


def _size_to_dims(size: str) -> tuple[int, int]:
    """Parse a ``"WxH"`` size string into ``(width, height)`` pixels."""
    w, _, h = size.partition("x")
    return int(w), int(h)


def _coerce_openai_size(size: str | None) -> str:
    """Return ``size`` if OpenAI accepts it, else the default (logged)."""
    if size in _ALLOWED_SIZES:
        return size
    if size is not None:
        logger.warning("Unsupported image size %r; using %s", size, _DEFAULT_SIZE)
    return _DEFAULT_SIZE


class OpenAIImageProvider:
    """Generates/edits images with OpenAI gpt-image via the REST API."""

    name = "openai"

    def __init__(self) -> None:
        self.model = config.openai_image_model()
        self.quality = config.image_quality()

    def _headers(self) -> dict:
        key = config.openai_api_key()
        if not key:
            # Fail loud with an actionable message; never echo the (absent) key.
            raise ImageGenNotConfigured(
                "OPENAI_API_KEY is not set; cannot generate images. Set it in the "
                "worker's environment (.env on the droplet)."
            )
        return {"Authorization": f"Bearer {key}"}  # key never logged

    def _decode(self, payload: dict, *, op: str) -> bytes:
        """Pull the base64 PNG out of an OpenAI image response, validating shape."""
        # Treat the provider response as untrusted — validate before trusting it.
        data = payload.get("data")
        if not isinstance(data, list) or not data:
            raise ImageGenError(f"Image {op} returned no data")
        b64 = (data[0] or {}).get("b64_json")
        if not isinstance(b64, str) or not b64:
            raise ImageGenError(f"Image {op} returned no image content")
        try:
            return base64.b64decode(b64)
        except (ValueError, TypeError) as e:
            raise ImageGenError(f"Image {op} returned undecodable data: {e}") from e

    def generate_image(
        self,
        *,
        prompt: str,
        size: str | None = None,
        background: str = "opaque",
        context: dict | None = None,
    ) -> ImageGenerationResult:
        """Create a scene/background PNG from ``prompt``."""
        import requests  # local import keeps module import cheap / dependency-light

        size = _coerce_openai_size(size)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "size": size,
            "background": background,
            "quality": self.quality,
            "n": 1,
        }
        logger.info("OpenAI image.generate model=%s size=%s quality=%s %s",
                    self.model, size, self.quality, context or {})
        try:
            resp = requests.post(
                _GENERATE_URL, json=payload, headers=self._headers(), timeout=_REQUEST_TIMEOUT_S
            )
        except requests.RequestException as e:
            raise ImageGenError(f"Image generation request failed: {e}") from e
        if resp.status_code != 200:
            # Body may carry a provider error message; it does not contain our key.
            raise ImageGenError(
                f"Image generation provider returned {resp.status_code}: {resp.text[:300]}"
            )
        try:
            body = resp.json() or {}
        except ValueError as e:
            raise ImageGenError(f"Image generation returned non-JSON: {e}") from e

        png = self._decode(body, op="generation")
        width, height = _size_to_dims(size)
        return ImageGenerationResult(
            png=png,
            provider=self.name,
            model=self.model,
            width=width,
            height=height,
            estimated_cost_usd=_APPROX_COST_PER_IMAGE_USD,
        )

    def edit_image(
        self,
        *,
        prompt: str,
        image: bytes,
        content_type: str = "image/png",
        size: str | None = None,
        background: str = "opaque",
        context: dict | None = None,
    ) -> ImageGenerationResult:
        """Transform the supplied ``image`` per ``prompt`` (product-in-use, straighten)."""
        import requests

        size = _coerce_openai_size(size)
        # The edit endpoint is multipart (image file + form fields), not JSON.
        files = {"image": ("product.png", image, content_type)}
        form = {"model": self.model, "prompt": prompt, "size": size,
                "quality": self.quality, "n": "1"}
        # Only send background when a transparent cutout is wanted (the straighten
        # step); the default opaque path is left untouched for existing callers.
        if background == "transparent":
            form["background"] = "transparent"
        logger.info("OpenAI image.edit model=%s size=%s %s", self.model, size, context or {})
        try:
            resp = requests.post(
                _EDIT_URL,
                data=form,
                files=files,
                headers=self._headers(),
                timeout=_REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise ImageGenError(f"Image edit request failed: {e}") from e
        if resp.status_code != 200:
            raise ImageGenError(
                f"Image edit provider returned {resp.status_code}: {resp.text[:300]}"
            )
        try:
            body = resp.json() or {}
        except ValueError as e:
            raise ImageGenError(f"Image edit returned non-JSON: {e}") from e

        png = self._decode(body, op="edit")
        width, height = _size_to_dims(size)
        return ImageGenerationResult(
            png=png,
            provider=self.name,
            model=self.model,
            width=width,
            height=height,
            estimated_cost_usd=_APPROX_COST_PER_IMAGE_USD,
        )


# --- Offline mock provider -------------------------------------------------

# Hue step (degrees) between the top and bottom of the mock sky gradient, and the
# fraction of image height where the mock "floor" begins. Small, cosmetic — just
# enough structure that compositing/placement has a believable surface to target.
_MOCK_HORIZON_FRAC = 0.62


def _hsl_to_rgb(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    """Convert HSL (h in degrees, s/l in 0..100) to an 8-bit RGB tuple."""
    lightness /= 100
    a = (s * min(lightness, 1 - lightness)) / 100

    def channel(n: float) -> int:
        k = (n + h / 30) % 12
        c = lightness - a * max(-1, min(k - 3, 9 - k, 1))
        return round(255 * c)

    return channel(0), channel(8), channel(4)


class MockImageGenerationProvider:
    """Deterministic offline provider: prompt-seeded gradient "scenes".

    Renders a vertical sky gradient, a floor band, a soft radial highlight, and a
    faint label with Pillow. It is intentionally NOT photorealistic — it exists so
    the full compositing/review/export pipeline can run with no external API, and
    so the same prompt always yields the same image (stable tests).
    """

    name = "mock"
    model = "mock-scene-1"

    def _scene(self, prompt: str, width: int, height: int) -> bytes:
        from io import BytesIO

        from PIL import Image, ImageDraw

        # Stable hash so identical prompts render identical scenes (test stability).
        seed = 2166136261
        for ch in prompt:
            seed = ((seed ^ ord(ch)) * 16777619) & 0xFFFFFFFF
        hue = seed % 360

        top = _hsl_to_rgb(hue, 35, 82)
        bottom = _hsl_to_rgb((hue + 24) % 360, 30, 62)
        floor = _hsl_to_rgb((hue + 12) % 360, 22, 48)
        horizon = round(height * _MOCK_HORIZON_FRAC)

        img = Image.new("RGB", (width, height))
        draw = ImageDraw.Draw(img, "RGBA")
        # Vertical sky gradient, interpolated per row.
        for y in range(horizon):
            t = y / max(1, horizon - 1)
            draw.line(
                [(0, y), (width, y)],
                fill=tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
            )
        # Solid floor band and a soft central highlight ellipse.
        draw.rectangle([0, horizon, width, height], fill=floor)
        draw.ellipse(
            [width * 0.16, horizon - height * 0.1, width * 0.84, height * 0.95],
            fill=(255, 255, 255, 28),
        )
        draw.text(
            (width / 2, height - 28),
            f"mock scene · {prompt[:48]}",
            fill=(255, 255, 255, 90),
            anchor="ms",
        )

        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def generate_image(
        self,
        *,
        prompt: str,
        size: str | None = None,
        background: str = "opaque",
        context: dict | None = None,
    ) -> ImageGenerationResult:
        width, height = _size_to_dims(size or f"{config.AI_SCENE_SIZE}x{config.AI_SCENE_SIZE}")
        logger.debug("mock image.generate size=%dx%d", width, height)
        return ImageGenerationResult(
            png=self._scene(prompt, width, height),
            provider=self.name,
            model=self.model,
            width=width,
            height=height,
            provider_ref="mock",
            estimated_cost_usd=0.0,
        )

    def edit_image(
        self,
        *,
        prompt: str,
        image: bytes,
        content_type: str = "image/png",
        size: str | None = None,
        background: str = "opaque",
        context: dict | None = None,
    ) -> ImageGenerationResult:
        from io import BytesIO

        from PIL import Image

        width, height = _size_to_dims(size or _DEFAULT_SIZE)
        product = Image.open(BytesIO(image)).convert("RGBA")
        buf = BytesIO()
        if background == "transparent":
            # Simulate a transparent product edit (e.g. straighten): center the
            # supplied product on a transparent canvas, preserving it as-is.
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            p = product.copy()
            p.thumbnail((round(width * 0.9), round(height * 0.9)), Image.LANCZOS)
            canvas.alpha_composite(p, (round((width - p.width) / 2), round((height - p.height) / 2)))
            canvas.save(buf, format="PNG")
            logger.debug("mock image.edit (transparent) size=%dx%d", width, height)
            return ImageGenerationResult(
                png=buf.getvalue(), provider=self.name, model=self.model,
                width=width, height=height, provider_ref="mock", estimated_cost_usd=0.0,
            )
        scene = Image.open(BytesIO(self._scene(prompt, width, height))).convert("RGBA")
        # Place the supplied product over the scene to simulate an edit result.
        product.thumbnail((round(width * 0.5), round(height * 0.6)), Image.LANCZOS)
        scene.alpha_composite(
            product, (round((width - product.width) / 2), height - product.height)
        )
        scene.convert("RGB").save(buf, format="PNG")
        logger.debug("mock image.edit size=%dx%d", width, height)
        return ImageGenerationResult(
            png=buf.getvalue(),
            provider=self.name,
            model=self.model,
            width=width,
            height=height,
            provider_ref="mock",
            estimated_cost_usd=0.0,
        )


def get_image_generation_provider():
    """Return the configured image-generation provider (OpenAI or the mock)."""
    if config.image_provider() == "openai":
        return OpenAIImageProvider()
    return MockImageGenerationProvider()
