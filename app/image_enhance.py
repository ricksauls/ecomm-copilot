"""Conservative AI image upscaling for the resolution issues content scoring flags.

CONFIG-GATED and provider-pluggable: inert until ``IMAGE_UPSCALE_API_KEY`` is set,
so shipping this changes nothing until an operator supplies credentials (mirrors
the residential-proxy plumbing in :mod:`app.ci_scraper`). The provider implemented
today is **Claid.ai** running its ``smart_enhance`` upscale — a *conservative*,
e-commerce-tuned super-resolution that sharpens and interpolates without inventing
product detail (see the scoring discussion). Swap ``IMAGE_UPSCALE_MODE`` / add a
provider branch to change engines.

Claid fetches the source image by URL itself, so callers pass the flagged image's
URL (which originates from our own stored scrape, never arbitrary user input) and
get back the processed image bytes. The API key is read from the environment and
never logged (security-standards).

Env:
    IMAGE_UPSCALE_PROVIDER      "claid" (default; only provider implemented)
    IMAGE_UPSCALE_API_KEY       Claid API key — empty leaves the feature inert
    IMAGE_UPSCALE_MODE          Claid upscale mode (default "smart_enhance")
    IMAGE_UPSCALE_TARGET_PX     longest-edge target, px (default 2000 — Walmart zoom)
    IMAGE_UPSCALE_OUTPUT_FORMAT "jpeg" (default) or "png"
    IMAGE_UPSCALE_PRICE_PER_IMAGE  provider price per fix in USD (default 0.04,
                                Claid's per-action price) — drives the batch
                                cost-preflight estimate; set empty to show counts
                                only (no dollar figure)
"""

import logging
import os

logger = logging.getLogger(__name__)

_CLAID_API = "https://api.claid.ai/v1/image/edit"
# Timeouts (seconds): the edit call does the heavy AI work server-side; the
# download is a plain GET of the result.
_EDIT_TIMEOUT_S = 120
_DOWNLOAD_TIMEOUT_S = 30

_DEFAULT_MODE = "smart_enhance"   # Claid's e-commerce product-image upscale
_DEFAULT_TARGET_PX = 2000         # Walmart's image-zoom recommendation
_DEFAULT_FORMAT = "jpeg"
# Claid bills one "action" per image edit; this is that published list price in
# USD. Used only to estimate spend before a batch run — never to bill or gate.
_DEFAULT_PRICE_PER_IMAGE = 0.04


class EnhanceError(RuntimeError):
    """Upscaling failed (provider error, timeout, or an unexpected response)."""


class EnhanceNotConfigured(EnhanceError):
    """No upscaling provider/key configured — the feature is inert."""


def _provider() -> str:
    return (os.environ.get("IMAGE_UPSCALE_PROVIDER") or "claid").strip().lower()


def _api_key() -> str:
    return (os.environ.get("IMAGE_UPSCALE_API_KEY") or "").strip()


def is_configured() -> bool:
    """True when an upscaling provider + key are set (the feature is live)."""
    return _provider() == "claid" and bool(_api_key())


def output_format() -> str:
    """Configured output image format ("jpeg" or "png")."""
    fmt = (os.environ.get("IMAGE_UPSCALE_OUTPUT_FORMAT") or _DEFAULT_FORMAT).strip().lower()
    return fmt if fmt in ("jpeg", "png") else _DEFAULT_FORMAT


def output_ext() -> str:
    """File extension matching :func:`output_format` ("jpg" / "png")."""
    return "jpg" if output_format() == "jpeg" else "png"


def output_mime() -> str:
    """MIME type matching :func:`output_format`."""
    return "image/jpeg" if output_format() == "jpeg" else "image/png"


def price_per_image() -> float | None:
    """Per-fix price in USD for the batch cost estimate, or ``None`` for no figure.

    Contract on ``IMAGE_UPSCALE_PRICE_PER_IMAGE``:
    - unset → the default (:data:`_DEFAULT_PRICE_PER_IMAGE`, Claid's $0.04/action);
    - a valid non-negative number → that price;
    - an explicit empty string → ``None`` so the preflight shows counts only;
    - anything unparseable → the default (logged), so a typo never hides the cost.

    This only estimates spend shown to the user before they confirm a batch; it
    never meters, bills, or blocks — the provider is the source of truth on cost.
    """
    raw = os.environ.get("IMAGE_UPSCALE_PRICE_PER_IMAGE")
    if raw is None:
        return _DEFAULT_PRICE_PER_IMAGE
    raw = raw.strip()
    if raw == "":
        return None  # operator opted out of the dollar figure — counts only
    try:
        price = float(raw)
    except ValueError:
        logger.warning(
            "Invalid IMAGE_UPSCALE_PRICE_PER_IMAGE=%r; using %.2f",
            raw, _DEFAULT_PRICE_PER_IMAGE,
        )
        return _DEFAULT_PRICE_PER_IMAGE
    if price < 0:
        logger.warning(
            "Negative IMAGE_UPSCALE_PRICE_PER_IMAGE=%r; using %.2f",
            raw, _DEFAULT_PRICE_PER_IMAGE,
        )
        return _DEFAULT_PRICE_PER_IMAGE
    return price


# Operations the client supports, both config-gated behind the same provider/key:
#   "upscale"  — conservative super-resolution to the 2000px zoom spec
#   "white_bg" — composite the product onto a pure-white background (+ resize),
#                for Walmart's main-image requirement
OPERATIONS = ("upscale", "white_bg")


def enhance(image_url: str, *, operation: str = "upscale") -> bytes:
    """Run ``operation`` on the image at ``image_url``; return the processed bytes.

    ``image_url`` must come from our own stored scrape (a Walmart CDN URL recorded
    on a scored item), not arbitrary user input — the provider fetches it directly.
    Raises :class:`EnhanceNotConfigured` when the feature is inert, or
    :class:`EnhanceError` on any provider/transport failure.
    """
    if operation not in OPERATIONS:
        raise EnhanceError(f"Unknown enhance operation {operation!r}")
    if not is_configured():
        raise EnhanceNotConfigured(
            "Image enhancement is not configured (set IMAGE_UPSCALE_API_KEY)."
        )
    if _provider() != "claid":
        raise EnhanceNotConfigured(f"Unsupported IMAGE_UPSCALE_PROVIDER={_provider()!r}")
    return _claid_edit(image_url, operation)


def _target_px() -> int:
    raw = os.environ.get("IMAGE_UPSCALE_TARGET_PX")
    try:
        return int(raw) if raw else _DEFAULT_TARGET_PX
    except ValueError:
        logger.warning("Invalid IMAGE_UPSCALE_TARGET_PX=%r; using %d", raw, _DEFAULT_TARGET_PX)
        return _DEFAULT_TARGET_PX


def _claid_operations(operation: str) -> dict:
    """Build Claid's ``operations`` object for ``operation``.

    Both variants AI-upscale and resize (fit:"bounds" preserves aspect, so the
    longest edge lands at ~target px — what Walmart's zoom cares about).
    "white_bg" *additionally* composites the product on pure white, so a low-res
    off-white main image comes out fully compliant (white, sharp, 2000px) in one
    call — you can't chain two separate downloads, so both fixes go together.
    """
    target = _target_px()
    mode = (os.environ.get("IMAGE_UPSCALE_MODE") or _DEFAULT_MODE).strip() or _DEFAULT_MODE
    ops = {
        "restorations": {"upscale": mode},
        "resizing": {"width": target, "height": target, "fit": "bounds"},
    }
    if operation == "white_bg":
        ops["background"] = {"remove": {"category": "products"}, "color": "#FFFFFF"}
    return ops


def _claid_edit(image_url: str, operation: str) -> bytes:
    """Call Claid's image/edit for ``operation`` on ``image_url``; return bytes."""
    import requests  # local import keeps module import cheap / dependency-light

    payload = {
        "input": image_url,
        "operations": _claid_operations(operation),
        "output": {"format": output_format()},
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",  # key never logged
        "Content-Type": "application/json",
    }

    logger.info("Claid %s: target=%dpx", operation, _target_px())
    try:
        resp = requests.post(_CLAID_API, json=payload, headers=headers, timeout=_EDIT_TIMEOUT_S)
    except requests.RequestException as e:
        raise EnhanceError(f"Upscale request failed: {e}") from e
    if resp.status_code != 200:
        # Body may carry a provider error message — safe to surface (no secrets).
        raise EnhanceError(f"Upscale provider returned {resp.status_code}: {resp.text[:300]}")

    try:
        out = ((resp.json() or {}).get("data") or {}).get("output") or {}
    except ValueError as e:
        raise EnhanceError(f"Upscale provider returned non-JSON: {e}") from e
    tmp_url = out.get("tmp_url")
    if not tmp_url:
        raise EnhanceError("Upscale succeeded but the response carried no image URL")

    try:
        img = requests.get(tmp_url, timeout=_DOWNLOAD_TIMEOUT_S)
        img.raise_for_status()
    except requests.RequestException as e:
        raise EnhanceError(f"Could not download the upscaled image: {e}") from e

    logger.info(
        "Claid %s done: %sx%s, %d bytes",
        operation, out.get("width"), out.get("height"), len(img.content),
    )
    return img.content
