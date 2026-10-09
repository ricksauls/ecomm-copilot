"""Background removal (product cutout) for the image-set pipeline.

Turns an opaque product photo into a tight transparent PNG cutout, which is then
composited onto AI-generated scenes. Per the product principle, the default
provider must NOT be generative AI — it is a dedicated matting service so the
real product is preserved faithfully.

- **remove.bg** and **PhotoRoom** are the real providers, called over HTTPS with
  ``requests``. PhotoRoom isolates the main product and so drops reflections and
  cast shadows that a chroma key keeps — the fix for glossy studio shots.
- A deterministic **mock** does a corner-sampled chroma key + border flood fill
  (studio shots on white become clean cutouts) so the pipeline runs offline with
  no key. It is not a general matting model — busy backgrounds or reflections need
  a real provider.

The provider is selected by ``IMAGESET_BG_PROVIDER`` (default mock); both real
providers share ``BACKGROUND_REMOVAL_API_KEY``, read from the environment and never
logged (security-standards).
"""

import logging

from app.imageset import config

logger = logging.getLogger(__name__)

_REMOVEBG_URL = "https://api.remove.bg/v1.0/removebg"
_PHOTOROOM_URL = "https://sdk.photoroom.com/v1/segment"
_REQUEST_TIMEOUT_S = 60

# Mock chroma-key thresholds: a channel value above which a pixel reads as
# "background-ish", and the color distance from the sampled corner background that
# still counts as background. Tuned for near-white studio backdrops.
_WHITE_THRESHOLD = 236
_EDGE_TOLERANCE = 32


class BackgroundRemovalError(RuntimeError):
    """Background removal failed (provider error, timeout, or bad response)."""


class BackgroundRemovalNotConfigured(BackgroundRemovalError):
    """No remove.bg key configured for the remove.bg provider."""


class CutoutResult:
    """A transparent-PNG cutout plus provider metadata (bytes in, bytes out)."""

    def __init__(self, *, png: bytes, provider: str, provider_ref: str | None = None) -> None:
        self.png = png
        self.provider = provider
        self.provider_ref = provider_ref


def _trim_to_opaque(png_bytes: bytes) -> bytes:
    """Crop a transparent PNG to the bounding box of its opaque pixels.

    Both providers return a cutout surrounded by transparency; trimming yields a
    tight product image the compositor can place and scale predictably.
    """
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(png_bytes)).convert("RGBA")
    bbox = img.getbbox()  # bounds of non-zero (here, non-transparent) regions
    if bbox:
        img = img.crop(bbox)
    buf = BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class RemoveBgProvider:
    """Cutout via the remove.bg API."""

    name = "removebg"

    def remove_background(self, *, data: bytes, content_type: str = "image/png") -> CutoutResult:
        import requests  # local import keeps module import cheap / dependency-light

        key = config.background_removal_api_key()
        if not key:
            raise BackgroundRemovalNotConfigured(
                "BACKGROUND_REMOVAL_API_KEY is not set; cannot remove backgrounds. "
                "Set it in the worker's environment (.env on the droplet)."
            )
        files = {"image_file": ("product", data, content_type)}
        form = {"size": "auto", "format": "png"}
        logger.info("remove.bg cutout: %d bytes in", len(data))
        try:
            resp = requests.post(
                _REMOVEBG_URL,
                data=form,
                files=files,
                headers={"X-Api-Key": key},  # key never logged
                timeout=_REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise BackgroundRemovalError(f"Background removal request failed: {e}") from e
        if resp.status_code != 200:
            # The raw body can carry account details — log only the status and
            # surface a safe generic message (security-standards).
            logger.error("remove.bg failed: status=%d", resp.status_code)
            raise BackgroundRemovalError(f"Background removal failed (status {resp.status_code})")

        provider_ref = resp.headers.get("x-credits-charged")
        png = _trim_to_opaque(resp.content)
        logger.info("remove.bg ok: credits_charged=%s", provider_ref)
        return CutoutResult(png=png, provider=self.name, provider_ref=provider_ref)


class PhotoRoomProvider:
    """Cutout via the PhotoRoom Remove Background API (``/v1/segment``).

    PhotoRoom segments the main product, so reflections and cast shadows a chroma
    key would keep are dropped — the fix for glossy studio shots. A sandbox key
    (``sandbox_…``) is passed through unchanged and returns watermarked test output.
    The key is read from the environment and never logged (security-standards).
    """

    name = "photoroom"

    def remove_background(self, *, data: bytes, content_type: str = "image/png") -> CutoutResult:
        import requests  # local import keeps module import cheap / dependency-light

        key = config.background_removal_api_key()
        if not key:
            raise BackgroundRemovalNotConfigured(
                "BACKGROUND_REMOVAL_API_KEY is not set; cannot remove backgrounds. "
                "Set it in the worker's environment (.env on the droplet)."
            )
        files = {"image_file": ("product", data, content_type)}
        form = {"format": "png"}  # transparent-PNG cutout
        logger.info("PhotoRoom cutout: %d bytes in", len(data))
        try:
            resp = requests.post(
                _PHOTOROOM_URL,
                data=form,
                files=files,
                headers={"x-api-key": key},  # key never logged
                timeout=_REQUEST_TIMEOUT_S,
            )
        except requests.RequestException as e:
            raise BackgroundRemovalError(f"Background removal request failed: {e}") from e
        if resp.status_code != 200:
            # The body can carry account details — log only the status and surface a
            # safe generic message (security-standards).
            logger.error("PhotoRoom failed: status=%d", resp.status_code)
            raise BackgroundRemovalError(f"Background removal failed (status {resp.status_code})")

        png = _trim_to_opaque(resp.content)
        logger.info("PhotoRoom ok: %d bytes out", len(png))
        return CutoutResult(png=png, provider=self.name, provider_ref=None)


class MockBackgroundRemovalProvider:
    """Offline cutout: corner-sampled chroma key + flood fill from the borders."""

    name = "mock"

    def remove_background(self, *, data: bytes, content_type: str = "image/png") -> CutoutResult:
        from collections import deque
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(data)).convert("RGBA")
        width, height = img.size
        px = img.load()

        # Sample the average background color from the four corners.
        corners = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
        br = sum(px[x, y][0] for x, y in corners) / 4
        bg = sum(px[x, y][1] for x, y in corners) / 4
        bb = sum(px[x, y][2] for x, y in corners) / 4

        def is_background(x: int, y: int) -> bool:
            r, g, b, _ = px[x, y]
            near_white = r > _WHITE_THRESHOLD and g > _WHITE_THRESHOLD and b > _WHITE_THRESHOLD
            dist = abs(r - br) + abs(g - bg) + abs(b - bb)
            return near_white or dist < _EDGE_TOLERANCE

        # Flood fill from every border pixel, clearing alpha on background-connected
        # pixels only (so a near-white pixel enclosed by the product is preserved).
        visited = bytearray(width * height)
        stack: deque[tuple[int, int]] = deque()
        for x in range(width):
            stack.append((x, 0))
            stack.append((x, height - 1))
        for y in range(height):
            stack.append((0, y))
            stack.append((width - 1, y))

        cleared = 0
        while stack:
            x, y = stack.pop()
            idx = y * width + x
            if visited[idx]:
                continue
            visited[idx] = 1
            if not is_background(x, y):
                continue
            r, g, b, _ = px[x, y]
            px[x, y] = (r, g, b, 0)  # transparent
            cleared += 1
            if x > 0:
                stack.append((x - 1, y))
            if x < width - 1:
                stack.append((x + 1, y))
            if y > 0:
                stack.append((x, y - 1))
            if y < height - 1:
                stack.append((x, y + 1))

        buf = BytesIO()
        img.save(buf, format="PNG")
        logger.debug("mock cutout: %dx%d, cleared %d px", width, height, cleared)
        return CutoutResult(png=_trim_to_opaque(buf.getvalue()), provider=self.name, provider_ref="mock")


def is_configured() -> bool:
    """True when the selected background-removal provider is ready to run.

    The mock is always ready; remove.bg and PhotoRoom need their key. Lets callers
    surface a clear "not configured" state before enqueuing work.
    """
    if config.background_provider() in ("removebg", "photoroom"):
        return bool(config.background_removal_api_key())
    return True


def get_background_removal_provider():
    """Return the configured provider (remove.bg, PhotoRoom, or the offline mock)."""
    provider = config.background_provider()
    if provider == "removebg":
        return RemoveBgProvider()
    if provider == "photoroom":
        return PhotoRoomProvider()
    return MockBackgroundRemovalProvider()
