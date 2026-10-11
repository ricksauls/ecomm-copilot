"""Acquire one submitted item: resolve its product page to image URLs.

Called by the worker (:func:`worker.process_import_one`) for a claimed item. The
web request never runs this — a Walmart/Amazon source does a headed-Chrome scrape
through the residential proxy.

No-pre-download model (v1): we only **resolve** the image URLs here (a lightweight
thumbnail URL for the gallery + the full-resolution URL), and store those. Nothing
is downloaded or upscaled at acquire time — the full image is fetched only when the
user clicks Download (see the import download routes). This keeps acquisition fast
and cheap and never fetches full-res bytes for images the user discards.

Never raises (a bad item must not kill the worker): a resolution failure marks the
item ``error`` with a user-facing reason; otherwise the item is ``done`` and the
run's status is rolled up.
"""

import logging
import sqlite3

from app.imgimport import sources, store

logger = logging.getLogger(__name__)


def process_item(conn: sqlite3.Connection, item: sqlite3.Row) -> None:
    """Resolve one claimed item's images into ``ready`` asset rows, then settle it."""
    item_id, run_id = item["id"], item["run_id"]
    try:
        label, refs = sources.resolve_images(item["source_url"], item["source_type"])
        for ref in refs:
            store.add_ready_asset(
                conn, item_id=item_id, run_id=run_id, user_id=item["user_id"],
                full_url=ref["full"], thumb_url=ref.get("thumb"),
                width=ref.get("width"), height=ref.get("height"))
        if not refs:
            store.set_item_status(conn, item_id, store.ITEM_ERROR, label=label,
                                  error="No images could be found for this URL.")
        else:
            store.set_item_status(conn, item_id, store.ITEM_DONE, label=label)
            logger.info("Resolved %d image(s) for import item=%s", len(refs), item_id)
    except sources.SourceError as e:
        store.set_item_status(conn, item_id, store.ITEM_ERROR, error=str(e))
    except Exception as e:  # noqa: BLE001 - never let one item crash the worker
        logger.exception("Unexpected error processing import item=%s", item_id)
        store.set_item_status(conn, item_id, store.ITEM_ERROR, error=f"Unexpected error: {e}")
    finally:
        store.recompute_run_status(conn, run_id)
