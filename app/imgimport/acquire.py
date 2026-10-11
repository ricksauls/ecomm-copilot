"""Acquire one submitted item: resolve → download → upscale-if-needed → store.

Called by the worker (:func:`worker.process_import_one`) for a claimed item. The
web request never runs this — a Walmart source does a headed-Chrome scrape, and
even the image downloads + Claid upscales are slow enough to belong off-request.

Policy: get the **native** highest-resolution image first (free, lossless — Amazon
bakes the size into the filename, which :mod:`sources` strips), and **upscale only
when the native image is below the target longest edge** (``image_enhance.target_px``,
the Walmart 2000px zoom spec). This saves spend and beats upscaling a derivative.
Upscaling is gracefully skipped (image kept at native res) when Claid isn't
configured.

Per-image failures are isolated: one bad image in a gallery fails only its own
asset; the item still completes with the rest. The item errors only when the source
itself can't be resolved (blocked Amazon PDP, no images found, scrape blocked).
"""

import logging
import sqlite3

from app import image_enhance
from app.imgimport import sources, storage, store

logger = logging.getLogger(__name__)


def _acquire_one_image(conn: sqlite3.Connection, *, item, image_url: str) -> bool:
    """Acquire a single image into its own asset row. Returns True on success.

    Downloads (SSRF-safe) and measures the native image, upscales it when below the
    target edge (and Claid is configured), then stores the bytes and finalizes the
    asset. Any failure is recorded on the asset and returns False — the caller keeps
    going with the item's other images.
    """
    asset_id = store.create_asset(
        conn, item_id=item["id"], run_id=item["run_id"],
        user_id=item["user_id"], source_image_url=image_url)
    try:
        data, ext, width, height = sources.download_image(image_url)

        # Upscale only if the native image is under spec and the provider is live.
        # The URL already passed the SSRF guard inside download_image, so it's a
        # public address — safe to hand to Claid (which fetches it directly).
        upscaled = False
        target = image_enhance.target_px()
        if max(width, height) < target and image_enhance.is_configured():
            try:
                data = image_enhance.enhance(image_url, operation="upscale")
                ext = image_enhance.output_ext()
                width, height, _ = sources._probe_image(data)
                upscaled = True
                logger.info("Upscaled imported image asset=%s to %dx%d", asset_id, width, height)
            except image_enhance.EnhanceError as e:
                # Upscaling is best-effort: keep the native image rather than fail it.
                logger.warning("Upscale skipped for import asset=%s: %s", asset_id, e)

        rel = storage.save(item["run_id"], asset_id, data, ext)
        if not rel:
            store.fail_asset(conn, asset_id, "Couldn't store the downloaded image.")
            return False
        store.finalize_asset(conn, asset_id, stored_path=rel, ext=ext,
                             width=width, height=height, upscaled=upscaled)
        return True
    except sources.SourceError as e:
        store.fail_asset(conn, asset_id, str(e))
        return False
    except Exception as e:  # noqa: BLE001 - one bad image must not abort the item
        logger.exception("Unexpected error acquiring import image asset=%s", asset_id)
        store.fail_asset(conn, asset_id, f"Unexpected error: {e}")
        return False


def process_item(conn: sqlite3.Connection, item: sqlite3.Row) -> None:
    """Resolve and acquire every image for one claimed item, then settle its status.

    Never raises (a bad item must not kill the worker): a resolution failure marks
    the item ``error`` with a user-facing reason; otherwise the item is ``done``
    once its images are attempted (some assets may have failed individually). The
    run's status is rolled up at the end either way.
    """
    item_id, run_id = item["id"], item["run_id"]
    try:
        label, image_urls = sources.resolve_image_urls(item["source_url"], item["source_type"])
        got = sum(_acquire_one_image(conn, item=item, image_url=u) for u in image_urls)
        if got == 0:
            store.set_item_status(conn, item_id, store.ITEM_ERROR, label=label,
                                  error="None of the images could be acquired.")
        else:
            store.set_item_status(conn, item_id, store.ITEM_DONE, label=label)
            logger.info("Acquired %d/%d image(s) for import item=%s", got, len(image_urls), item_id)
    except sources.SourceError as e:
        # SourceNotSupported (Amazon PDP) and other resolution failures land here.
        store.set_item_status(conn, item_id, store.ITEM_ERROR, error=str(e))
    except Exception as e:  # noqa: BLE001 - never let one item crash the worker
        logger.exception("Unexpected error processing import item=%s", item_id)
        store.set_item_status(conn, item_id, store.ITEM_ERROR, error=f"Unexpected error: {e}")
    finally:
        store.recompute_run_status(conn, run_id)
