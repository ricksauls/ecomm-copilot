"""Filesystem storage for image-set artifacts (uploads, cutouts, finals, thumbs).

Image bytes live under ``MEDIA_DIR/imageset/<project_id>/<kind>/<name>.<ext>`` —
outside the git checkout (so a deploy ``git pull`` never wipes them), beside the
SQLite DB, sharing the exact media root the CI image cache resolves (so the web
app and the worker always agree on the location). The DB rows store the *relative*
path; the serving route maps it back to an absolute path.

Every path component is server-controlled and validated (project id + asset id are
ints, ``kind`` is a fixed allowlist, ``name`` is digits/lowercase only), so a
crafted value can never escape the media directory (path traversal — see
security-standards).
"""

import logging
import os
import re

from app import ci_images

logger = logging.getLogger(__name__)

# The artifact kinds (one subdirectory each) a project's images fall into.
KINDS = ("original", "cutout", "scene", "final", "thumb", "logo")
_ID_RE = re.compile(r"^\d+$")
_NAME_RE = re.compile(r"^[a-z0-9]+$")


def _root() -> str:
    """Absolute image-set media root, under the shared MEDIA_DIR."""
    # Reuse the CI cache's media-root resolution so the worker and web app resolve
    # the identical location without duplicating the DATABASE_URL/MEDIA_DIR logic.
    return os.path.join(ci_images._media_root(), "imageset")


def rel_path(project_id, kind: str, name, ext: str = "png") -> str | None:
    """Relative artifact path (under MEDIA_DIR), or ``None`` for unsafe inputs."""
    if project_id is None or not _ID_RE.match(str(project_id)):
        return None
    if kind not in KINDS:
        return None
    if name is None or not _NAME_RE.match(str(name)):
        return None
    if ext not in ("png", "jpg"):
        return None
    return os.path.join("imageset", str(project_id), kind, f"{name}.{ext}")


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
        logger.warning("Refusing out-of-root media path %r", rel)
        return None
    return full


def save(project_id, kind: str, name, data: bytes, ext: str = "png") -> str | None:
    """Write artifact ``data`` and return its relative path, or ``None`` on failure."""
    rel = rel_path(project_id, kind, name, ext)
    if not rel:
        logger.warning("Refusing to store image-set artifact for project=%r kind=%r name=%r",
                       project_id, kind, name)
        return None
    path = abs_path(rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        logger.info("Stored image-set artifact %s (%d bytes)", rel, len(data))
        return rel
    except OSError:
        logger.exception("Failed to store image-set artifact project=%s kind=%s name=%s",
                         project_id, kind, name)
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
        logger.exception("Failed to read image-set artifact %s", rel)
        return None
