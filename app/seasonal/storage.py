"""Filesystem storage for seasonal-campaign artifacts.

Image bytes live under ``MEDIA_DIR/seasonal/<campaign_id>/<kind>/<image_id>.<ext>``
— outside the git checkout (so a deploy ``git pull`` never wipes them), beside the
SQLite DB, sharing the exact media root the CI image cache resolves (so the web app
and the worker always agree on the location). DB rows store the *relative* path; the
serving route maps it back to an absolute path.

Every path component is server-controlled and validated (campaign id + image id are
ints, ``kind`` is a fixed allowlist), so a crafted value can never escape the media
directory (path traversal — see security-standards). This mirrors
:mod:`app.imageset.storage`; it's a separate module only so the media subtree and
the ``KINDS`` allowlist are scoped to this feature.
"""

import logging
import os
import re

from app import ci_images

logger = logging.getLogger(__name__)

# The artifact kinds (one subdirectory each) a seasonal image's files fall into.
KINDS = ("original", "cutout", "scene", "final", "thumb")
_ID_RE = re.compile(r"^\d+$")


def _root() -> str:
    """Absolute seasonal media root, under the shared MEDIA_DIR."""
    # Reuse the CI cache's media-root resolution so the worker and web app resolve
    # the identical location without duplicating the DATABASE_URL/MEDIA_DIR logic.
    return os.path.join(ci_images._media_root(), "seasonal")


def rel_path(campaign_id, kind: str, image_id, ext: str = "png") -> str | None:
    """Relative artifact path (under MEDIA_DIR), or ``None`` for unsafe inputs."""
    if campaign_id is None or not _ID_RE.match(str(campaign_id)):
        return None
    if kind not in KINDS:
        return None
    if image_id is None or not _ID_RE.match(str(image_id)):
        return None
    if ext not in ("png", "jpg"):
        return None
    return os.path.join("seasonal", str(campaign_id), kind, f"{image_id}.{ext}")


def abs_path(rel: str | None) -> str | None:
    """Absolute path for a stored relative path, guaranteed inside the media root.

    Defends the serving route: even a relative path read back from the DB is
    re-checked for containment so it can't point outside MEDIA_DIR.
    """
    if not rel:
        return None
    root = ci_images._media_root()
    full = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.abspath(full), os.path.abspath(root)]) != os.path.abspath(root):
        logger.warning("Refusing out-of-root seasonal media path %r", rel)
        return None
    return full


def save(campaign_id, kind: str, image_id, data: bytes, ext: str = "png") -> str | None:
    """Write artifact ``data`` and return its relative path, or ``None`` on failure."""
    rel = rel_path(campaign_id, kind, image_id, ext)
    if not rel:
        logger.warning("Refusing to store seasonal artifact for campaign=%r kind=%r image=%r",
                       campaign_id, kind, image_id)
        return None
    path = abs_path(rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        logger.info("Stored seasonal artifact %s (%d bytes)", rel, len(data))
        return rel
    except OSError:
        logger.exception("Failed to store seasonal artifact campaign=%s kind=%s image=%s",
                         campaign_id, kind, image_id)
        return None


def load(rel: str | None) -> bytes | None:
    """Read a stored artifact's bytes, or ``None`` if missing/unsafe."""
    path = abs_path(rel)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        logger.exception("Failed to read seasonal artifact %s", rel)
        return None
