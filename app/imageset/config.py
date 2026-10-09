"""Runtime configuration for the PDP Image Set Creation feature.

Centralizes the canvas/compositing constants and the provider toggles for the
image-set pipeline so env access is validated in one place (the pattern the rest
of the app follows — see :mod:`app.image_enhance`). Every provider defaults to
its offline ``mock`` implementation, so the whole feature runs with no API keys
until an operator opts in; that keeps the port inert-by-default, exactly like the
Claid enhancer and the residential-proxy plumbing.

Env:
    IMAGESET_IMAGE_PROVIDER      "mock" (default) | "openai" — AI scene generation
    OPENAI_API_KEY               OpenAI key; empty leaves the OpenAI provider inert
    OPENAI_IMAGE_MODEL           image model (default "gpt-image-2")
    IMAGESET_BG_PROVIDER         "mock" (default) | "removebg" | "photoroom" — cutout
    BACKGROUND_REMOVAL_API_KEY   remove.bg / PhotoRoom key (PhotoRoom sandbox keys are
                                 prefixed "sandbox_"); empty leaves the provider inert
    IMAGESET_PRICE_PER_IMAGE     USD per AI image for the cost estimate (default
                                 0.04, OpenAI's approx gpt-image price); set empty
                                 to show counts only (no dollar figure)
"""

import logging
import os

logger = logging.getLogger(__name__)

# Final deliverable geometry. Each exported asset is a fixed square PNG so a
# marketplace listing gets a uniform set; the AI scene is generated smaller and
# the real product cutout is composited on top at full canvas resolution.
CANVAS_SIZE = 2000          # final square output, px (matches the source app)
AI_SCENE_SIZE = 1024        # AI scene generation size, px (OpenAI gpt-image dim)
SAFE_MARGIN = 120           # px safe margin for programmatic text/callouts
MIN_TEXT_PX = 48            # minimum readable text size on the 2000px canvas
OUTPUT_FORMAT = "png"

# Approx OpenAI gpt-image cost per generated image, USD. Estimate only — shown to
# the user before they confirm a batch; never meters, bills, or gates (same
# contract as IMAGE_UPSCALE_PRICE_PER_IMAGE in app.image_enhance).
_DEFAULT_PRICE_PER_IMAGE = 0.04


def image_provider() -> str:
    """Configured AI image-generation provider ("mock" or "openai")."""
    return (os.environ.get("IMAGESET_IMAGE_PROVIDER") or "mock").strip().lower()


def background_provider() -> str:
    """Configured background-removal provider ("mock", "removebg", or "photoroom")."""
    return (os.environ.get("IMAGESET_BG_PROVIDER") or "mock").strip().lower()


def openai_api_key() -> str:
    """OpenAI API key from the environment (never logged)."""
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def openai_image_model() -> str:
    """Configured OpenAI image model (env override, else gpt-image-2)."""
    return (os.environ.get("OPENAI_IMAGE_MODEL") or "gpt-image-2").strip() or "gpt-image-2"


def background_removal_api_key() -> str:
    """Background-removal API key (remove.bg or PhotoRoom) from the env (never logged).

    Both services share one key env var; ``IMAGESET_BG_PROVIDER`` selects which one
    it's for. A PhotoRoom sandbox key (``sandbox_…``) works as-is — it's passed
    straight through to PhotoRoom, which returns watermarked test output.
    """
    return (os.environ.get("BACKGROUND_REMOVAL_API_KEY") or "").strip()


def removebg_api_key() -> str:
    """Deprecated alias for :func:`background_removal_api_key`."""
    return background_removal_api_key()


def price_per_image() -> float | None:
    """Per-image price in USD for the batch cost estimate, or ``None`` for no figure.

    Mirrors :func:`app.image_enhance.price_per_image`: unset → the default
    ($0.04); a valid non-negative number → that; an explicit empty string → None
    (counts only); anything unparseable or negative → the default (logged), so a
    typo never hides the cost. Estimate only — never meters or bills.
    """
    raw = os.environ.get("IMAGESET_PRICE_PER_IMAGE")
    if raw is None:
        return _DEFAULT_PRICE_PER_IMAGE
    raw = raw.strip()
    if raw == "":
        return None  # operator opted out of the dollar figure
    try:
        price = float(raw)
    except ValueError:
        logger.warning(
            "Invalid IMAGESET_PRICE_PER_IMAGE=%r; using %.2f", raw, _DEFAULT_PRICE_PER_IMAGE
        )
        return _DEFAULT_PRICE_PER_IMAGE
    if price < 0:
        logger.warning(
            "Negative IMAGESET_PRICE_PER_IMAGE=%r; using %.2f", raw, _DEFAULT_PRICE_PER_IMAGE
        )
        return _DEFAULT_PRICE_PER_IMAGE
    return price
