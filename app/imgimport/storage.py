"""Filesystem storage for imported product images.

Image bytes live under ``MEDIA_DIR/import/<run_id>/<name>.<ext>`` — outside the git
checkout (a deploy ``git pull`` never wipes them), beside the SQLite DB, sharing the
exact media root the CI image cache and the image-set store resolve, so the web app
and the worker always agree on the location. DB rows store the *relative* path; the
serving route maps it back to an absolute path.

Every path component is server-controlled and validated (run id + name are digits
only, ``ext`` is a fixed allowlist), so a crafted value can never escape the media
directory (path traversal — see security-standards).
"""

import logging
import os
import re

from app import ci_images

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^\d+$")
_EXTS = ("jpg", "png", "webp", "gif")


def rel_path(run_id, name, ext: str = "jpg") -> str | None:
    """Relative artifact path (under MEDIA_DIR), or ``None`` for unsafe inputs."""
    if run_id is None or not _ID_RE.match(str(run_id)):
        return None
    if name is None or not _ID_RE.match(str(name)):
        return None
    if ext not in _EXTS:
        return None
    return os.path.join("import", str(run_id), f"{name}.{ext}")


def abs_path(rel: str | None) -> str | None:
    """Absolute path for a stored relative path, re-checked for media-root containment.

    Defends the serving route: a relative path read back from the DB is re-validated
    so it can't point outside MEDIA_DIR even if the row were tampered with.
    """
    if not rel:
        return None
    root = ci_images._media_root()
    full = os.path.normpath(os.path.join(root, rel))
    if os.path.commonpath([os.path.abspath(full), os.path.abspath(root)]) != os.path.abspath(root):
        logger.warning("Refusing out-of-root import media path %r", rel)
        return None
    return full


def save(run_id, name, data: bytes, ext: str = "jpg") -> str | None:
    """Write image ``data`` and return its relative path, or ``None`` on failure."""
    rel = rel_path(run_id, name, ext)
    if not rel:
        logger.warning("Refusing to store imported image run=%r name=%r ext=%r",
                       run_id, name, ext)
        return None
    path = abs_path(rel)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        logger.info("Stored imported image %s (%d bytes)", rel, len(data))
        return rel
    except OSError:
        logger.exception("Failed to store imported image run=%s name=%s", run_id, name)
        return None


def load(rel: str | None) -> bytes | None:
    """Read a stored image's bytes, or ``None`` if missing/unsafe."""
    path = abs_path(rel)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        logger.exception("Failed to read imported image %s", rel)
        return None
