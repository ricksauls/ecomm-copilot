#!/usr/bin/env python3
"""Recolor the DISCOtech wordmark's dark-navy text to white for dark surfaces.

The delivered AI-rendered wordmark master has dark-navy "DISC"/"tech" text,
built for a light background. This app's chrome is dark everywhere the logo
appears, so the text needs to be white instead — but the ring's own internal
shadow band is almost the same dark navy as the letters, so a naive "recolor
every dark pixel" pass would also wash out the ring's shading.

The fix classifies every pixel by its own HLS color rather than its position:
a pixel counts as "ring" (left untouched) only if it's genuinely saturated and
not too dark; sampling real pixels found the dividing line between letter-navy
(lightness tops out ~0.13) and the ring's darkest shadow (starts ~0.15) --
see docs/HANDOFF.md §12 for the full story of what didn't work before this.

Needs numpy, scipy, and Pillow -- none of which are app dependencies, so this
won't run under the project's .venv as-is. Use the system Python (verified
against 2026-09-30's macOS system python3) or `pip install numpy scipy` into
a scratch venv; don't add these to requirements.txt for a one-off asset script.

Usage:
    python3 scripts/recolor_wordmark.py <source.png> <dest.png>

Re-run this against a new/updated source master if the logo ever needs to be
regenerated. Always spot-check the four letter-tip contact points afterward
(top/bottom horns of "C", left edge and top of "t") plus the ring's own
shading for white speckling -- see the HANDOFF gotcha entry for why a quick
visual check isn't enough on its own.
"""

import sys

import numpy as np
from PIL import Image
from scipy import ndimage

# Thresholds found by sampling real pixels in the 2026-09-30 rebrand session
# (see docs/HANDOFF.md §12). Re-derive these if the source art changes its
# palette -- they are specific to this ring's exact gradient and shadow tones.
SATURATION_FLOOR = 0.30
LIGHTNESS_FLOOR = 0.14  # letters top out ~0.13; ring shadow starts ~0.15
ALPHA_FLOOR = 40
# No dilation on the protected (ring) mask -- even 1px re-opens a visible notch
# at the two spots where the letterform intentionally almost touches the ring.
# A small closing on the *recolor* mask afterward mops up the last handful of
# antialiased stray pixels without that risk.
CLOSING_KERNEL = 7


def recolor(src_path: str, dest_path: str) -> None:
    im = Image.open(src_path).convert("RGBA")
    arr = np.array(im)

    r = arr[:, :, 0].astype(float) / 255
    g = arr[:, :, 1].astype(float) / 255
    b = arr[:, :, 2].astype(float) / 255
    alpha = arr[:, :, 3]

    maxc = np.maximum(np.maximum(r, g), b)
    minc = np.minimum(np.minimum(r, g), b)
    lightness = (maxc + minc) / 2
    diff = maxc - minc
    saturation = np.where(
        lightness <= 0.5,
        diff / np.maximum(maxc + minc, 1e-6),
        diff / np.maximum(2 - maxc - minc, 1e-6),
    )

    opaque = alpha > ALPHA_FLOOR
    vivid = opaque & (saturation > SATURATION_FLOOR) & (lightness > LIGHTNESS_FLOOR)
    recolor_mask = opaque & ~vivid
    closed = ndimage.binary_closing(recolor_mask, structure=np.ones((CLOSING_KERNEL, CLOSING_KERNEL)))
    recolor_mask = closed & opaque

    out = arr.copy()
    out[recolor_mask, 0] = 255
    out[recolor_mask, 1] = 255
    out[recolor_mask, 2] = 255
    # alpha channel is left untouched throughout

    Image.fromarray(out, "RGBA").save(dest_path)
    print(f"wrote {dest_path} ({recolor_mask.sum()} pixels recolored to white)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <source.png> <dest.png>", file=sys.stderr)
        sys.exit(1)
    recolor(sys.argv[1], sys.argv[2])
