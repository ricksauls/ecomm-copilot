"""Seasonal-campaign pipelines run by the worker.

Two phases, each called by the worker for a claimed unit of work:

- :func:`process_product` (ACQUIRE) — resolve one product URL to its image gallery
  (via the shared Acquire-by-URL resolver) and record the images. Never raises: a
  bad URL settles the product ``error`` so one product can't sink the campaign.
- :func:`process_image` (GENERATE) — for one product's main image: download the
  full-res source and render it into the seasonal scene. Two render modes
  (:data:`RENDER_EDIT` default, :data:`RENDER_COMPOSITE` fallback) decide *how* the
  product lands in the scene — the model places it, or we composite a cutout — then
  it stores the final + thumbnail. Raises :class:`GenerationError` on a missing input
  or provider failure; the worker catches it and marks the job + image failed, so one
  bad image never sinks the run.

The heavy lifting is all reused: :mod:`app.imgimport.sources` for resolution +
SSRF-safe download, the configured image-generation (edit + generate) and
background-removal providers, and :mod:`app.imageset.compose` for the composite path.
"""

import json
import logging
import os

from app.imageset import compose
from app.imageset import config as imageset_config
from app.imageset.providers.background_removal import get_background_removal_provider
from app.imageset.providers.image_generation import get_image_generation_provider
from app.imgimport import sources
from app.seasonal import jobs, storage, store, themes

logger = logging.getLogger(__name__)

# Render mode — how the product ends up in the seasonal scene:
#   'edit'      (default): the image model PLACES the real product into a scene it
#               builds around it (correct perspective + grounding, scalable across
#               shapes/backdrops; regenerates pixels, so label fidelity is a risk).
#   'composite':           cut the product out + generate an empty backdrop + paste
#               it on with a fixed ground line (label pixel-exact, but placement can
#               mismatch the generated surface). Fallback if 'edit' garbles labels.
# Switchable via the SEASONAL_RENDER_MODE env var without a code change.
RENDER_EDIT = "edit"
RENDER_COMPOSITE = "composite"


def _render_mode() -> str:
    """The configured render mode (defaults to 'edit'; unknown values fall back)."""
    mode = (os.environ.get("SEASONAL_RENDER_MODE") or RENDER_EDIT).strip().lower()
    return mode if mode in (RENDER_EDIT, RENDER_COMPOSITE) else RENDER_EDIT


# Product placement on the seasonal backdrop — the image-set LIFESTYLE look (a
# grounded product with a contact/cast shadow and a softly-blurred background), so
# the composite reads as a real staged seasonal photo. ``ground_line`` matches
# :data:`app.seasonal.themes.GROUND_LINE` so the product lands on the surface the
# backdrop prompt pinned there. Grounding is stronger than the image-set default
# (a firmer contact shadow + an ambient-occlusion band) so the product doesn't look
# like it's floating; see _CONTACT_AO.
_PLACEMENT = {
    "scale": 0.62,
    "offset_x": 0.0,
    "offset_y": 0.0,
    "ground_line": themes.GROUND_LINE,
    "shadow_strength": 0.78,
}
# Strength of the tight ambient-occlusion contact band at the product base.
_CONTACT_AO = 0.7
# How strongly to relight the product toward the scene's ambient light (0 = keep the
# product exactly as shot; 1 = full match). Kept mild so the label stays legible —
# this only shifts lighting, never redraws the product (its pixels/geometry are the
# real cutout).
_RELIGHT_STRENGTH = 0.35


class GenerationError(Exception):
    """A seasonal image could not be generated (missing input or provider error)."""


# --- ACQUIRE phase ----------------------------------------------------------

def process_product(conn, product) -> None:
    """Resolve one claimed product and queue its MAIN image for seasonal treatment.

    The seasonal process only ever treats a product's **main image** — the first
    image the resolver returns (the acquire resolver yields the gallery in order,
    main first; see :mod:`app.fetch`, which treats ``image_urls[0]`` as the main
    everywhere). So we take ``refs[0]``, record it, and **enqueue its generation
    immediately** (no user selection step). Resolution yields image URLs only (no
    bytes downloaded here); the full-resolution main image is fetched later, at
    generation time. Never raises — a resolution failure marks the product ``error``
    with a user-facing reason.
    """
    product_id, campaign_id = product["id"], product["campaign_id"]
    try:
        label, refs = sources.resolve_images(product["source_url"], product["source_type"])
        if not refs:
            store.set_product_status(conn, product_id, store.PRODUCT_ERROR, label=label,
                                     error="No images could be found for this URL.")
            return
        main = refs[0]  # the product's main image (resolver returns main first)
        image_id = store.add_main_image(
            conn, product_id=product_id, campaign_id=campaign_id,
            user_id=product["user_id"], full_url=main["full"], thumb_url=main.get("thumb"),
            width=main.get("width"), height=main.get("height"))
        store.set_product_status(conn, product_id, store.PRODUCT_DONE, label=label)
        # Auto-queue the main image's seasonal generation — no selection step.
        jobs.enqueue_image_job(conn, user_id=product["user_id"], campaign_id=campaign_id,
                               image_id=image_id)
        logger.info("Queued main image for seasonal product=%s image=%s", product_id, image_id)
    except sources.SourceError as e:
        store.set_product_status(conn, product_id, store.PRODUCT_ERROR, error=str(e))
    except Exception as e:  # noqa: BLE001 - never let one product crash the worker
        logger.exception("Unexpected error acquiring seasonal product=%s", product_id)
        store.set_product_status(conn, product_id, store.PRODUCT_ERROR,
                                 error=f"Unexpected error: {e}")


# --- GENERATE phase ---------------------------------------------------------

def _download_source(full_url: str) -> bytes:
    """Download a selected image's full-res bytes (SSRF-safe) and normalize to PNG.

    Reuses the import downloader's SSRF guard (http(s) only, public IPs, size cap,
    real-image verification). Flattens to RGB-on-white PNG so the cutout step gets a
    predictable format regardless of the source (JPEG/WebP/…). Raises
    :class:`GenerationError` (user-facing) on any download/decode failure.
    """
    try:
        data, _ext, _w, _h = sources.download_image(full_url)
    except sources.SourceError as e:
        raise GenerationError(str(e)) from e
    # Re-encode to PNG on a white matte (marketplace mains are white-bg) so the
    # background-removal provider always receives a clean, consistent input.
    img = compose.load_image(data).convert("RGB")
    return compose.to_png_bytes(img)


def _render_edit(campaign, image_id: int, original_bytes: bytes) -> tuple[bytes, bytes, None, dict]:
    """AI-edit render: the model places the real product into a seasonal scene.

    Sends the product's main image to the image model's edit endpoint with a strong
    product-preservation prompt; the model returns a finished scene with the product
    already grounded in it — no separate cutout or fixed-ground-line composite, so
    placement/perspective/shadow are the model's job and can't mismatch a generated
    surface. Returns ``(final_bytes, raw_edit_bytes, None, meta)`` (no cutout).
    """
    prompt = themes.build_seasonal_edit_prompt(campaign["theme"], campaign["custom_prompt"])
    provider = get_image_generation_provider()
    size = f"{imageset_config.AI_SCENE_SIZE}x{imageset_config.AI_SCENE_SIZE}"
    result = provider.edit_image(
        prompt=prompt, image=original_bytes, content_type="image/png",
        size=size, background="opaque",
        context={"campaign_id": campaign["id"], "image_id": image_id,
                 "feature": "seasonal", "mode": RENDER_EDIT},
    )
    # The edit output is the finished image; normalize to the 2000px square canvas.
    final_bytes = compose.export_image(compose.cover_scene(compose.load_image(result.png)))
    meta = {"provider": result.provider, "model": result.model,
            "estimated_cost_usd": result.estimated_cost_usd, "mode": RENDER_EDIT}
    return final_bytes, result.png, None, meta


def _render_composite(campaign, image_id: int,
                      original_bytes: bytes) -> tuple[bytes, bytes, bytes, dict]:
    """Composite render: cut the product out, generate an empty backdrop, paste it on.

    Cuts the product out, generates a product-free seasonal backdrop, relights the
    cutout toward the scene, and composites it with a grounded lifestyle look + a
    firm ambient-occlusion contact. Keeps the label pixel-exact but places at a fixed
    ground line (can mismatch the generated surface). Returns
    ``(final_bytes, scene_bytes, cutout_bytes, meta)``.
    """
    bg_provider = get_background_removal_provider()
    cutout_result = bg_provider.remove_background(data=original_bytes, content_type="image/png")
    cutout = compose.trim_to_content(compose.load_image(cutout_result.png))
    logger.info("Seasonal cutout campaign=%s image=%s content_size=%s",
                campaign["id"], image_id, cutout.size)

    prompt = themes.build_seasonal_prompt(campaign["theme"], campaign["custom_prompt"])
    img_provider = get_image_generation_provider()
    size = f"{imageset_config.AI_SCENE_SIZE}x{imageset_config.AI_SCENE_SIZE}"
    scene = img_provider.generate_image(
        prompt=prompt, size=size, background="opaque",
        context={"campaign_id": campaign["id"], "image_id": image_id,
                 "feature": "seasonal", "mode": RENDER_COMPOSITE},
    )

    scene_img = compose.load_image(scene.png)
    ambient_rgb, ambient_lum = compose.sample_scene_light(
        scene_img, ground_line=_PLACEMENT["ground_line"])
    light_side = compose.detect_light_side(scene_img)
    relit = compose.relight_cutout(
        cutout, ambient_rgb=ambient_rgb, ambient_lum=ambient_lum,
        light_side=light_side, strength=_RELIGHT_STRENGTH)
    composed = compose.composite_product_on_scene(
        scene_img, relit,
        scale=_PLACEMENT["scale"], offset_x=_PLACEMENT["offset_x"],
        offset_y=_PLACEMENT["offset_y"], ground_line=_PLACEMENT["ground_line"],
        shadow_strength=_PLACEMENT["shadow_strength"],
        blur_background=True, cast_shadow=True, contact_ao=_CONTACT_AO,
    )
    meta = {"provider": scene.provider, "model": scene.model,
            "estimated_cost_usd": scene.estimated_cost_usd, "mode": RENDER_COMPOSITE}
    return compose.export_image(composed), scene.png, cutout_result.png, meta


def process_image(conn, image, campaign) -> None:
    """Generate one seasonal image end to end and persist it. Raises on failure.

    Downloads the product's main image, renders it into the seasonal scene via the
    configured mode (:func:`_render_edit` by default, or :func:`_render_composite`),
    and stores the outputs (final + thumbnail, plus the raw scene and, in composite
    mode, the cutout). The caller (the worker) catches :class:`GenerationError` /
    unexpected errors and marks the job + image failed.
    """
    image_id, campaign_id = image["id"], campaign["id"]
    store.update_image(conn, image_id, gen_status=store.GEN_GENERATING, error=None)

    # Download the full-resolution main image, then render it into the scene.
    original_bytes = _download_source(image["full_url"])
    original_path = storage.save(campaign_id, "original", image_id, original_bytes)

    mode = _render_mode()
    render = _render_edit if mode == RENDER_EDIT else _render_composite
    final_bytes, scene_bytes, cutout_bytes, meta = render(campaign, image_id, original_bytes)

    cutout_path = (storage.save(campaign_id, "cutout", image_id, cutout_bytes)
                   if cutout_bytes else None)
    scene_path = (storage.save(campaign_id, "scene", image_id, scene_bytes)
                  if scene_bytes else None)
    final_path = storage.save(campaign_id, "final", image_id, final_bytes)
    thumb_path = storage.save(
        campaign_id, "thumb", image_id,
        compose.to_png_bytes(compose.create_thumbnail(compose.load_image(final_bytes))),
    )
    if not final_path or not thumb_path:
        raise GenerationError("Could not store the generated image")

    store.update_image(
        conn, image_id, gen_status=store.GEN_READY,
        original_path=original_path, cutout_path=cutout_path, scene_path=scene_path,
        final_path=final_path, thumb_path=thumb_path, error=None,
    )
    store.recompute_campaign_status(conn, campaign_id)
    logger.info("Seasonal image ready campaign=%s image=%s mode=%s meta=%s",
                campaign_id, image_id, mode, json.dumps(meta))
