"""Programmatic (non-AI) asset templates.

These assets carry **factual** content — exact dimensions, net contents, feature
copy — so they are composited deterministically with Pillow from user-entered
data ONLY; no image model is involved, so a measurement or claim can never be
AI-invented. This module currently implements the size-comparison measurement
diagram (the vertical-slice programmatic asset); feature-callout and infographic
templates land here in a later phase.
"""

import logging

from PIL import Image, ImageDraw

from app.imageset import compose
from app.imageset.config import CANVAS_SIZE, SAFE_MARGIN

logger = logging.getLogger(__name__)

# Brand palette defaults (match the source app's fallback brand colors). Overridden
# per project when the user supplies brand colors.
DEFAULT_BRAND = {"primary": "#0B1F3A", "secondary": "#3E5C8A", "accent": "#F4B740"}


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """Parse a '#rrggbb' hex color to an RGB tuple, tolerating a missing '#'."""
    v = (value or "").lstrip("#")
    if len(v) != 6:
        return (11, 31, 58)  # fall back to the default primary on bad input
    try:
        return tuple(int(v[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (11, 31, 58)


def _measure_line(draw: ImageDraw.ImageDraw, p1, p2, color, cap=16) -> None:
    """Draw a double-headed measurement arrow between two points with tick caps."""
    x1, y1 = p1
    x2, y2 = p2
    draw.line([x1, y1, x2, y2], fill=color, width=4)
    horizontal = y1 == y2
    if horizontal:
        for x in (x1, x2):
            draw.line([x, y1 - cap, x, y1 + cap], fill=color, width=4)
        draw.polygon([(x1, y1), (x1 + 22, y1 - 12), (x1 + 22, y1 + 12)], fill=color)
        draw.polygon([(x2, y2), (x2 - 22, y2 - 12), (x2 - 22, y2 + 12)], fill=color)
    else:
        for y in (y1, y2):
            draw.line([x1 - cap, y, x1 + cap, y], fill=color, width=4)
        draw.polygon([(x1, y1), (x1 - 12, y1 + 22), (x1 + 12, y1 + 22)], fill=color)
        draw.polygon([(x2, y2), (x2 - 12, y2 - 22), (x2 + 12, y2 - 22)], fill=color)


def _vertical_label(text: str, size: int, color) -> Image.Image:
    """Render a text label rotated 90° (reads bottom-to-top) for a height gutter."""
    f = compose.font(size)
    probe = Image.new("RGBA", (1, 1))
    bbox = ImageDraw.Draw(probe).textbbox((0, 0), text, font=f)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    label = Image.new("RGBA", (w + 8, h + 8), (0, 0, 0, 0))
    ImageDraw.Draw(label).text((4 - bbox[0], 4 - bbox[1]), text, font=f, fill=color)
    return label.rotate(90, expand=True)


def create_size_comparison(
    *,
    title: str,
    dimensions: dict,
    cutout: Image.Image,
    brand: dict | None = None,
) -> bytes:
    """Build the fully programmatic exact-measurement diagram; return PNG bytes.

    ``dimensions`` is ``{width, height, depth, unit, weight}`` with any numeric
    value possibly None. Measurement lines are drawn only for the dimensions the
    user actually supplied; with none, a prompt to add dimensions is shown instead.
    """
    size = CANVAS_SIZE
    brand = brand or DEFAULT_BRAND
    primary = _hex_to_rgb(brand.get("primary", DEFAULT_BRAND["primary"]))
    secondary = _hex_to_rgb(brand.get("secondary", DEFAULT_BRAND["secondary"]))
    accent = _hex_to_rgb(brand.get("accent", DEFAULT_BRAND["accent"]))
    unit = dimensions.get("unit") or ""

    canvas = Image.new("RGB", (size, size), (255, 255, 255))

    # Fit the product into a central box, leaving room for measurement gutters.
    product = compose.fit_cutout(cutout, size * 0.46, size * 0.5)
    pw, ph = product.size
    px = round((size - pw) / 2)
    py = round(size * 0.28)
    canvas_rgba = canvas.convert("RGBA")
    canvas_rgba.alpha_composite(product, (px, py))
    canvas = canvas_rgba  # keep RGBA until the final export

    draw = ImageDraw.Draw(canvas)

    def label(value) -> str | None:
        return f"{value} {unit}".strip() if value is not None else None

    height_label = label(dimensions.get("height"))
    width_label = label(dimensions.get("width"))
    gx = px - 90  # vertical (height) measurement line x
    gy = py + ph + 90  # horizontal (width) measurement line y

    if height_label:
        _measure_line(draw, (gx, py), (gx, py + ph), secondary)
        vlabel = _vertical_label(height_label, 52, primary)
        canvas.alpha_composite(vlabel, (gx - 30 - vlabel.width // 2, py + ph // 2 - vlabel.height // 2))
    if width_label:
        _measure_line(draw, (px, gy), (px + pw, gy), secondary)
        compose.draw_text_centered(draw, (px + pw / 2, gy + 78), width_label,
                                   size=52, fill=primary, bold=True)

    # Header bar + accent underline + left-aligned title (drawn last, on top).
    draw.rectangle([0, 0, size, 180], fill=primary)
    draw.rectangle([0, 180, size, 188], fill=accent)
    draw.text((SAFE_MARGIN, 90), title, font=compose.font(76), fill=(255, 255, 255),
              anchor="lm", stroke_width=2, stroke_fill=(255, 255, 255))

    footer = []
    depth_label = label(dimensions.get("depth"))
    if depth_label:
        footer.append(f"Depth: {depth_label}")
    if dimensions.get("weight"):
        footer.append(f"Net: {dimensions['weight']}")
    if footer:
        compose.draw_text_centered(draw, (size / 2, size - 150), "    ·    ".join(footer),
                                   size=46, fill=secondary)
    if not height_label and not width_label:
        compose.draw_text_centered(draw, (size / 2, size / 2),
                                   "Add product dimensions to render measurements",
                                   size=44, fill=secondary)

    logger.debug("Built size-comparison diagram: h=%s w=%s", height_label, width_label)
    return compose.export_image(canvas)
