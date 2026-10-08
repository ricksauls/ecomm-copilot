"""Feature-callout icons: pick one per feature and load its bundled glyph.

Icons are pre-rasterized white PNGs under ``icons/`` (converted from the source
app's SVG set), composited into a brand-colored badge by the feature-callout
template. ``infer_icon`` is a Python port of the source app's ``inferIcon`` so
the same feature copy picks the same icon.
"""

import functools
import logging
import os
import re

from PIL import Image

logger = logging.getLogger(__name__)

_ICON_DIR = os.path.join(os.path.dirname(__file__), "icons")

# Title keyword → icon, tried in order (first match wins). Mirrors the source.
_RULES: list[tuple[str, str]] = [
    (r"mosquito", "mosquito"),
    (r"\btick", "tick"),
    (r"bug|insect|\bfly|flies|gnat|chigger|pest", "mosquito"),
    (r"spray|even|application|nozzle|mist|aerosol", "spray"),
    (r"hour|long[- ]?last|duration|all[- ]?day|8h", "clock"),
    (r"protect|defen[sc]e|guard", "shieldCheck"),
    (r"shield", "shield"),
    (r"camp|hike|hiking|trail|outdoor|adventure|woods", "pine"),
    (r"natural|formula|plant|eco|leaf", "leaf"),
    (r"water|sweat|resist|drop|moist|rain", "droplet"),
    (r"family|child|children|kid|confidence|safe", "people"),
    (r"skin|cloth|apply|wear|shirt", "shirt"),
    (r"fast|quick|instant|power|strong", "bolt"),
    (r"size|dimension|compact|portable|travel", "ruler"),
    (r"trust|quality|premium|brand|award|best", "star"),
    (r"sun|uv|day", "sun"),
]

# Fallback by feature_type when no title keyword matches.
_TYPE_MAP = {
    "USAGE": "spray",
    "DESIGN": "pine",
    "FORMULATION": "leaf",
    "TECHNOLOGY": "bolt",
    "PRIMARY_BENEFIT": "shieldCheck",
}


def infer_icon(title: str, feature_type: str = "") -> str:
    """Choose an icon name for a feature from its title (then feature_type)."""
    t = (title or "").lower()
    for pattern, icon in _RULES:
        if re.search(pattern, t):
            return icon
    return _TYPE_MAP.get((feature_type or "").upper(), "check")


@functools.lru_cache(maxsize=32)
def load_icon(name: str) -> Image.Image:
    """Load a bundled white icon glyph (RGBA); falls back to ``check``."""
    path = os.path.join(_ICON_DIR, f"{name}.png")
    if not os.path.isfile(path):
        logger.debug("Icon %r missing; using 'check'", name)
        path = os.path.join(_ICON_DIR, "check.png")
    return Image.open(path).convert("RGBA")
