"""Programmatic (non-AI) asset templates.

These assets carry **factual** content — exact dimensions, net contents, feature
copy — so they are composited deterministically with Pillow from user-entered
data ONLY; no image model is involved, so a measurement or claim can never be
AI-invented. This module currently implements the size-comparison measurement
diagram (the vertical-slice programmatic asset); feature-callout and infographic
templates land here in a later phase.
"""

import logging

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from app.imageset import compose, iconlib
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


def format_dimensions_line(dimensions: dict) -> str:
    """One-line size summary like ``7" × 3" × 3" · Net 8 oz`` from the facts.

    Uses the inch mark only when the unit is inches; otherwise appends the unit
    (e.g. ``18 × 8 × 8 cm``). Net contents, when present, follow after a middot.
    Returns '' when there are no measurements and no weight to show.
    """
    unit = (dimensions.get("unit") or "in").strip()

    def _num(v) -> str | None:
        if v is None or v == "":
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        return str(int(f)) if f == int(f) else f"{f:g}"

    hwd = [_num(dimensions.get(k)) for k in ("height", "width", "depth")]
    hwd = [v for v in hwd if v is not None]
    parts: list[str] = []
    if hwd:
        # Inches: an inch mark on each number (7" × 3" × 3"); other units: trailing
        # unit. The "×" renders correctly now that we use Inter (which has the glyph).
        parts.append(' × '.join(f'{v}"' for v in hwd) if unit == "in"
                     else f"{' × '.join(hwd)} {unit}")
    if dimensions.get("weight"):
        parts.append(f"Net {dimensions['weight']}")
    return "  ·  ".join(parts)


# Muted neutral for the benefit line — a tier below the headline, still legible.
_BENEFIT_COLOR = (65, 72, 79)
_FEATURE_BG = (245, 247, 250)  # clean light surface
# Gaussian radius for the photographic backdrop: enough to read as out-of-focus
# without washing the scene out, so the product/text still win the eye. Lower =
# sharper scene, higher = dreamier.
_BACKDROP_BLUR = 6


def _feature_callout_regions(size: int, margin: int, layout: str):
    """Return (product_rect, feature_rect) as (x, y, w, h) for a callout layout."""
    if layout == "product-center":
        prod = (margin, margin, size - 2 * margin, round(size * 0.44))
        fy = round(size * 0.50)
        return prod, (margin, fy, size - 2 * margin, size - margin - fy)
    col_w = (size - 3 * margin) // 2
    left_col = (margin, margin, col_w, size - 2 * margin)
    right_col = (2 * margin + col_w, margin, col_w, size - 2 * margin)
    # product-right puts the product in the right column, features on the left.
    return (right_col, left_col) if layout == "product-right" else (left_col, right_col)


def _badge_image(r: int, primary: tuple, icon: Image.Image, ss: int = 3) -> Image.Image:
    """A layered circular icon badge (white/primary rings + centered white glyph).

    Rendered supersampled and downscaled so the circles are smooth. Mirrors the
    source app's concentric-ring badge (outer white, primary, white, primary center).
    """
    d = int(r * 2)
    tile = Image.new("RGBA", (d * ss, d * ss), (0, 0, 0, 0))
    td = ImageDraw.Draw(tile)
    c = d * ss / 2
    for frac, fill in ((1.0, (255, 255, 255, 255)), (0.93, (*primary, 255)),
                       (0.86, (255, 255, 255, 255)), (0.79, (*primary, 255))):
        rr = r * frac * ss
        td.ellipse([c - rr, c - rr, c + rr, c + rr], fill=fill)
    tile = tile.resize((d, d), Image.LANCZOS)
    box = int(r * 0.79 * 1.3)  # glyph fills the center comfortably
    glyph = icon.resize((box, box), Image.LANCZOS)
    tile.alpha_composite(glyph, (int(d / 2 - box / 2), int(d / 2 - box / 2)))
    return tile


def create_feature_callout(
    *,
    features: list[dict],
    cutout: Image.Image,
    brand: dict | None = None,
    layout: str = "product-left",
    backdrop_png: bytes | None = None,
) -> bytes:
    """Feature-callout card: product + icon/headline/divider/benefit rows.

    Hybrid, ported from the source app: an optional blurred photographic backdrop
    (``backdrop_png``) behind the product cutout and a vertically-centered stack of
    feature rows — each a layered icon badge, a bold uppercase headline (brand
    primary), a short accent divider, and a muted benefit line. A soft blurred white
    halo sits behind each row for legibility over the photo. All copy is the user's
    approved feature text drawn with Pillow (Inter) — nothing AI-invented.
    ``features`` is ``[{title, description, icon}, …]``.
    """
    size, m = CANVAS_SIZE, SAFE_MARGIN
    brand = brand or DEFAULT_BRAND
    primary = _hex_to_rgb(brand.get("primary", DEFAULT_BRAND["primary"]))
    secondary = _hex_to_rgb(brand.get("secondary", DEFAULT_BRAND["secondary"]))

    if backdrop_png:
        canvas = compose.cover_scene(compose.load_image(backdrop_png), size).filter(
            ImageFilter.GaussianBlur(_BACKDROP_BLUR)).convert("RGBA")
    else:
        canvas = Image.new("RGB", (size, size), _FEATURE_BG).convert("RGBA")

    (pbx, pby, pbw, pbh), (frx, fry, frw, frh) = _feature_callout_regions(size, m, layout)
    product = compose.fit_cutout(cutout, pbw * 0.92, pbh * 0.92)
    canvas.alpha_composite(product, (round(pbx + (pbw - product.width) / 2),
                                     round(pby + (pbh - product.height) / 2)))

    n = max(1, len(features))
    icon_r = round(min(96, frh / n * 0.30))
    gap = 40  # badge → text gap
    text_x = frx + 2 * icon_r + gap
    text_w = frw - (2 * icon_r + gap)
    head_fs = 56 if n <= 2 else 50
    ben_fs = 40 if n <= 2 else 36
    head_font, ben_font = compose.font(head_fs, bold=True), compose.font(ben_fs)
    head_lh, ben_lh, div_gap, div_h = 1.16, 1.3, 18, 6

    # Pass 1 — measure each row (wrapped lines + heights) to center the stack.
    blocks = []
    for feat in features:
        head_lines = compose.wrap_text((feat.get("title") or "").upper(), head_font, text_w, 2)
        benefit = (feat.get("description") or "").strip()
        ben_lines = compose.wrap_text(benefit, ben_font, text_w, 2) if benefit else []
        head_h = len(head_lines) * head_fs * head_lh
        ben_h = len(ben_lines) * ben_fs * ben_lh
        text_h = head_h + ((div_gap + div_h + div_gap + ben_h) if ben_lines else 0)
        content_h = max(2 * icon_r, text_h)
        widest = max([head_font.getlength(ln) for ln in head_lines]
                     + [ben_font.getlength(ln) for ln in ben_lines] + [1.0])
        blocks.append({"head": head_lines, "ben": ben_lines, "head_h": head_h,
                       "text_h": text_h, "content_h": content_h, "widest": widest,
                       "icon": feat.get("icon") or "check"})

    row_gap = round(min(frh * 0.08, max(24, (frh - sum(b["content_h"] for b in blocks)) / max(1, n))))
    stack_h = sum(b["content_h"] for b in blocks) + row_gap * (n - 1)
    y0 = fry + max(0, (frh - stack_h) / 2)

    # Halo pass — soft blurred white rounded rects behind each row (legibility over
    # a photo). Drawn on their own layer, blurred, then composited under the content.
    # The halo extends past the row's content by these pads; because the blur fades
    # the edges, a generous pad keeps the solid core comfortably under all the text.
    halo_pad_x, halo_pad_y = 72, 48
    if backdrop_png:
        halo = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        hd = ImageDraw.Draw(halo)
        y = y0
        for b in blocks:
            right = text_x + min(text_w, b["widest"]) + halo_pad_x
            hd.rounded_rectangle([frx - halo_pad_x, y - halo_pad_y,
                                  right, y + b["content_h"] + halo_pad_y],
                                 radius=90, fill=(255, 255, 255, 150))
            y += b["content_h"] + row_gap
        canvas.alpha_composite(halo.filter(ImageFilter.GaussianBlur(26)))

    # Pass 2 — draw badges + text.
    draw = ImageDraw.Draw(canvas)
    y = y0
    for b in blocks:
        cy = y + b["content_h"] / 2
        canvas.alpha_composite(_badge_image(icon_r, primary, iconlib.load_icon(b["icon"])),
                               (round(frx), round(cy - icon_r)))
        yy = y + max(0, (b["content_h"] - b["text_h"]) / 2)
        for line in b["head"]:
            draw.text((text_x, yy), line, font=head_font, fill=primary)
            yy += head_fs * head_lh
        if b["ben"]:
            yy += div_gap
            draw.rounded_rectangle([text_x, yy, text_x + min(text_w, b["widest"]), yy + div_h],
                                   radius=div_h / 2, fill=secondary)
            yy += div_h + div_gap
            for line in b["ben"]:
                draw.text((text_x, yy), line, font=ben_font, fill=_BENEFIT_COLOR)
                yy += ben_fs * ben_lh
        y += b["content_h"] + row_gap

    logger.debug("Built feature-callout: layout=%s features=%d backdrop=%s",
                 layout, n, bool(backdrop_png))
    return compose.export_image(canvas)


def _recenter_vertically(canvas: Image.Image, band_top: int, band_bottom: int,
                         *, bg=(255, 255, 255), threshold: int = 14) -> Image.Image:
    """Shift the image's content so it's vertically centered in [band_top, band_bottom].

    The size-comparison scene is items on a pure-white field, and the image model
    tends to place them high; this centers them in the whitespace band between the
    bars. Finds the content's vertical extent (non-white beyond ``threshold``,
    ignoring faint shadow/anti-alias noise) and translates the whole scene so that
    extent's midpoint lands on the band's midpoint. Vacated area is filled with
    ``bg``. A no-op when there's no content or it already fits centered.
    """
    diff = ImageChops.difference(canvas.convert("RGB"), Image.new("RGB", canvas.size, bg))
    mask = diff.convert("L").point(lambda p: 255 if p > threshold else 0)
    bbox = mask.getbbox()
    if not bbox:
        return canvas
    content_mid = (bbox[1] + bbox[3]) / 2
    shift = round((band_top + band_bottom) / 2 - content_mid)
    if shift == 0:
        return canvas
    shifted = Image.new("RGB", canvas.size, bg)
    shifted.paste(canvas, (0, shift))
    logger.debug("Re-centered size-comparison content by %dpx (band %d–%d)",
                 shift, band_top, band_bottom)
    return shifted


def render_size_comparison_bars(
    scene_png: bytes,
    *,
    headline: str,
    dimensions_line: str,
    caption: str = "",
    brand: dict | None = None,
) -> bytes:
    """Composite the branded header/footer bars over the AI size-comparison scene.

    The scene (product + everyday reference objects on pure white, from the image
    model) carries no text; here we overlay a top headline bar and a bottom bar
    with the exact dimensions line and a scale caption — in the project's brand
    color so it reads as on-brand creative. All copy is drawn programmatically, so
    no figure or claim can be AI-invented.
    """
    size = CANVAS_SIZE
    brand = brand or DEFAULT_BRAND
    primary = _hex_to_rgb(brand.get("primary", DEFAULT_BRAND["primary"]))
    canvas = compose.cover_scene(compose.load_image(scene_png), size)  # 2000² RGB

    top_h = round(size * 0.085)
    bottom_h = round(size * 0.145)
    by = size - bottom_h
    # Center the items in the whitespace between the bars before drawing the bars
    # (the model tends to place them high, leaving uneven space top vs. bottom).
    canvas = _recenter_vertically(canvas, top_h, by)
    draw = ImageDraw.Draw(canvas)

    # Font sizes follow the source app's proven size-comparison caption (title 76,
    # facts 72, reference 44 on a 2000px canvas), drawn in real Inter Bold.
    draw.rectangle([0, 0, size, top_h], fill=primary)
    if headline:
        compose.draw_text_centered(draw, (size / 2, top_h / 2), headline,
                                   size=76, fill=(255, 255, 255), bold=True)

    draw.rectangle([0, by, size, size], fill=primary)
    if dimensions_line:
        compose.draw_text_centered(draw, (size / 2, by + bottom_h * 0.37), dimensions_line,
                                   size=72, fill=(255, 255, 255), bold=True)
    if caption:
        compose.draw_text_centered(draw, (size / 2, by + bottom_h * 0.74), caption,
                                   size=44, fill=(236, 240, 244))

    logger.debug("Rendered size-comparison bars: headline=%r dims=%r", headline, dimensions_line)
    return compose.export_image(canvas)


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
