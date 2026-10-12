"""Seasonal-campaign pipelines run by the worker.

Two phases, each called by the worker for a claimed unit of work:

- :func:`process_product` (ACQUIRE) — resolve one product URL to its image gallery
  (via the shared Acquire-by-URL resolver) and record the images. Never raises: a
  bad URL settles the product ``error`` so one product can't sink the campaign.
- :func:`process_image` (GENERATE) — for one selected image: download the full-res
  source, cut out the product, generate a fresh seasonal backdrop from the
  campaign's theme, composite the cutout onto it, and store the final + thumbnail.
  Raises :class:`GenerationError` on a missing input or provider failure; the worker
  catches it and marks the job + image failed, so one bad image never sinks the run.

The heavy lifting is all reused: :mod:`app.imgimport.sources` for resolution +
SSRF-safe download, :func:`app.imageset.compose.composite_product_on_scene` for the
overlay, and the configured image-generation / background-removal providers.
"""

import json
import logging

from app.imageset import compose
from app.imageset import config as imageset_config
from app.imageset.providers.background_removal import get_background_removal_provider
from app.imageset.providers.image_generation import get_image_generation_provider
from app.imgimport import sources
from app.seasonal import jobs, storage, store, themes

logger = logging.getLogger(__name__)

# Product placement on the seasonal backdrop — the image-set LIFESTYLE look (a
# grounded product with a contact/cast shadow and a softly-blurred background), so
# the composite reads as a real staged seasonal photo. ``ground_line`` matches
# :data:`app.seasonal.themes.GROUND_LINE` so the product lands on the surface the
# backdrop prompt pinned there.
_PLACEMENT = {
    "scale": 0.62,
    "offset_x": 0.0,
    "offset_y": 0.0,
    "ground_line": themes.GROUND_LINE,
    "shadow_strength": 0.6,
}


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


def process_image(conn, image, campaign) -> None:
    """Generate one seasonal image end to end and persist it. Raises on failure.

    download full-res → cut out product → AI seasonal backdrop → composite → store
    (final + thumbnail + the raw backdrop). The caller (the worker) catches
    :class:`GenerationError` / unexpected errors and marks the job + image failed.
    """
    image_id, campaign_id = image["id"], campaign["id"]
    store.update_image(conn, image_id, gen_status=store.GEN_GENERATING, error=None)

    # 1) Download the selected full-resolution source image.
    original_bytes = _download_source(image["full_url"])
    original_path = storage.save(campaign_id, "original", image_id, original_bytes)

    # 2) Cut the product out (transparent PNG), then tighten to its visible content
    #    so it fills its placement box instead of being shrunk by a transparent border.
    bg_provider = get_background_removal_provider()
    cutout_result = bg_provider.remove_background(data=original_bytes, content_type="image/png")
    cutout_path = storage.save(campaign_id, "cutout", image_id, cutout_result.png)
    cutout = compose.trim_to_content(compose.load_image(cutout_result.png))
    logger.info("Seasonal cutout campaign=%s image=%s content_size=%s",
                campaign_id, image_id, cutout.size)

    # 3) Generate the seasonal backdrop (scene only — never the product or text).
    prompt = themes.build_seasonal_prompt(campaign["theme"], campaign["custom_prompt"])
    img_provider = get_image_generation_provider()
    size = f"{imageset_config.AI_SCENE_SIZE}x{imageset_config.AI_SCENE_SIZE}"
    scene = img_provider.generate_image(
        prompt=prompt, size=size, background="opaque",
        context={"campaign_id": campaign_id, "image_id": image_id, "feature": "seasonal"},
    )

    # 4) Composite the cutout onto the backdrop with the grounded lifestyle look.
    composed = compose.composite_product_on_scene(
        compose.load_image(scene.png), cutout,
        scale=_PLACEMENT["scale"], offset_x=_PLACEMENT["offset_x"],
        offset_y=_PLACEMENT["offset_y"], ground_line=_PLACEMENT["ground_line"],
        shadow_strength=_PLACEMENT["shadow_strength"],
        blur_background=True, cast_shadow=True,
    )

    # 5) Store the outputs (final 2000px PNG, gallery thumbnail, raw backdrop).
    final_bytes = compose.export_image(composed)
    scene_path = storage.save(campaign_id, "scene", image_id, scene.png)
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
    meta = {"provider": scene.provider, "model": scene.model,
            "estimated_cost_usd": scene.estimated_cost_usd}
    logger.info("Seasonal image ready campaign=%s image=%s meta=%s",
                campaign_id, image_id, json.dumps(meta))
