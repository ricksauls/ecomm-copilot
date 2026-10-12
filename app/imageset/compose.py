"""Core image-composition primitives (Pillow port of the source canvas engine).

The final canvas is a fixed square (:data:`app.imageset.config.CANVAS_SIZE`) in
RGB. Product cutouts are scaled proportionally (never stretched or cropped) and
grounded with a soft contact shadow plus an optional directional cast shadow that
falls away from the scene's light. Product colors are never altered — the approved
product must be preserved faithfully.

This is the Python/Pillow rebuild of the source app's Sharp+SVG engine: ellipse /
capsule shadow shapes become ``ImageDraw`` fills + ``ImageFilter.GaussianBlur``,
the depth-of-field gradient becomes an alpha mask, and SVG ``rotate`` becomes a
layer ``Image.rotate`` about the shape center. All functions take and return
Pillow ``Image`` objects (RGB/RGBA); byte<->image conversion stays at the edges.
"""

import colorsys
import functools
import io
import logging
import os

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from app.imageset.config import CANVAS_SIZE, SAFE_MARGIN

logger = logging.getLogger(__name__)

# Bundled Inter (the app's brand font, also what the source app uses for creative
# text) as TTF so Pillow can rasterize it. Real Regular/Bold faces give crisp,
# properly-spaced text — no stroke-faked bold, which smeared glyphs together.
_FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
_FONT_FILES = {False: "Inter-Regular.ttf", True: "Inter-Bold.ttf"}


def detect_dominant_color(cutout: "Image.Image") -> str | None:
    """Sample a product cutout's dominant *brand* color as '#rrggbb', or None.

    Used to auto-suggest the size-comparison bar color from the product photo.
    Looks only at opaque, saturated, mid-brightness pixels (ignores the
    transparent background and near-white/near-black/greyscale label areas),
    buckets them by hue, and returns the average color of the most prominent hue
    bucket weighted by saturation. Returns None when the product has no clearly
    colored region, so the caller can fall back to the default palette.
    """
    img = cutout.convert("RGBA").resize((80, 80), Image.LANCZOS)
    # 12 hue buckets (30° each): accumulate (count, r, g, b, saturation) per bucket.
    # Iterate raw RGBA bytes (4 per pixel) rather than getdata() (deprecated in Pillow 14).
    raw = img.tobytes()
    buckets: dict[int, list[float]] = {}
    for i in range(0, len(raw), 4):
        r, g, b, a = raw[i], raw[i + 1], raw[i + 2], raw[i + 3]
        if a < 128:
            continue  # transparent background
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        if s < 0.25 or v < 0.15 or (v > 0.95 and s < 0.35):
            continue  # greyscale / near-white / near-black — not a brand hue
        key = int(h * 12) % 12
        acc = buckets.setdefault(key, [0.0, 0.0, 0.0, 0.0, 0.0])
        acc[0] += 1
        acc[1] += r
        acc[2] += g
        acc[3] += b
        acc[4] += s
    if not buckets:
        logger.debug("No dominant brand color detected in cutout")
        return None
    # Prominence = pixel count × average saturation (favors a vivid, common hue).
    key = max(buckets, key=lambda k: buckets[k][0] * (buckets[k][4] / buckets[k][0]))
    count, rs, gs, bs, _ = buckets[key]
    hex_color = "#{:02x}{:02x}{:02x}".format(
        round(rs / count), round(gs / count), round(bs / count))
    logger.debug("Detected dominant brand color %s (bucket=%d, px=%d)", hex_color, key, int(count))
    return hex_color


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def load_image(data: bytes) -> Image.Image:
    """Decode image bytes into a Pillow image (format-agnostic)."""
    return Image.open(io.BytesIO(data))


def to_png_bytes(img: Image.Image) -> bytes:
    """Encode a Pillow image to PNG bytes."""
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@functools.lru_cache(maxsize=128)
def _load_font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    """Load (and cache) the bundled Inter face at ``size``; fall back to default."""
    path = os.path.join(_FONT_DIR, _FONT_FILES[bold])
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        logger.warning("Bundled Inter font missing at %s; using Pillow default", path)
        return ImageFont.load_default(size=size)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Return bundled Inter at ``size`` px — Inter Bold when ``bold``.

    Inter is the app's brand font (and what the source app uses for creative
    text). Having a real Bold face means callers no longer fake bold with a
    stroke, which smeared adjacent glyphs together.
    """
    return _load_font(bold, size)


def draw_text_centered(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    *,
    size: int,
    fill,
    bold: bool = False,
    anchor: str = "mm",
) -> None:
    """Draw text with Inter (real Bold when ``bold`` — no faked stroke)."""
    draw.text(xy, text, font=font(size, bold=bold), fill=fill, anchor=anchor)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, max_width: float,
              max_lines: int = 2) -> list[str]:
    """Greedily wrap ``text`` to lines that fit ``max_width`` px in ``font``.

    Caps at ``max_lines``, truncating the final line with an ellipsis when the
    text is longer — so a long feature headline/benefit never overruns its column.
    """
    words = (text or "").split()
    if not words:
        return []
    lines, cur = [], words[0]
    for w in words[1:]:
        if font.getlength(f"{cur} {w}") <= max_width:
            cur = f"{cur} {w}"
        else:
            lines.append(cur)
            cur = w
    lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and font.getlength(f"{last}…") > max_width:
            last = last[:-1].rstrip()
        lines[-1] = f"{last}…" if last else "…"
    return lines


def cover_scene(scene: Image.Image, size: int = CANVAS_SIZE) -> Image.Image:
    """Resize a scene to fully cover the square canvas (center-crop overflow)."""
    return ImageOps.fit(scene.convert("RGB"), (size, size), method=Image.LANCZOS)


def fit_cutout(cutout: Image.Image, box_w: float, box_h: float) -> Image.Image:
    """Resize a transparent cutout to fit inside a box, preserving aspect ratio.

    Scales to fill the box on its tighter axis — enlarging as well as shrinking
    (matching Sharp's ``fit:"inside", withoutEnlargement:false``), so a small
    source cutout isn't left tiny on the 2000px canvas.
    """
    img = cutout.convert("RGBA")
    w, h = img.size
    scale = min(box_w / w, box_h / h)
    return img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def trim_to_content(cutout: Image.Image) -> Image.Image:
    """Crop a cutout to its visible content (alpha bbox); no-op when fully transparent.

    A matting provider may return the subject on a transparent background at the
    original image size (a wide transparent border), and transparent pixels can
    still carry RGB — so a plain ``getbbox()`` won't trim them. Cropping to the
    alpha channel's bbox yields a tight product that ``fit_cutout`` can scale to
    fill its box instead of shrinking a mostly-empty image. Applied at render time
    so a cutout stored before this fix is tightened on regeneration (no re-cutout).
    """
    rgba = cutout.convert("RGBA")
    bbox = rgba.getchannel("A").getbbox()
    return rgba.crop(bbox) if bbox else rgba


def content_aspect_ratio(cutout: Image.Image) -> float:
    """Return the width/height ratio of a cutout's visible content (alpha bbox).

    Measures the non-transparent bounding box so transparent padding around the
    product doesn't skew the shape. ~1.0 is square, >1 is wider than tall, <1 is
    taller than wide. Falls back to the full image size when there's no alpha
    channel or no visible content (so it never divides by zero).
    """
    img = cutout.convert("RGBA")
    bbox = img.getchannel("A").getbbox()  # tight box around non-transparent pixels
    if bbox:
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    else:
        w, h = img.size
    return (w / h) if h else 1.0


def _grey_samples(scene: Image.Image, w: int = 64, h: int = 64) -> list[int]:
    """Downscale to a tiny greyscale and return its pixel values (row-major)."""
    small = cover_scene(scene, max(w, h)).resize((w, h), Image.LANCZOS).convert("L")
    # tobytes() on an "L" image is row-major pixel values — same as getdata() but
    # without the Pillow-14 deprecation on getdata().
    return list(small.tobytes())


def detect_light_side(scene: Image.Image) -> str:
    """Return the side ('left'/'right') a composited cast shadow should fall toward.

    Heuristic: on a 64×64 greyscale copy, compare mean brightness of the left vs.
    right half of the upper 60% (where the light/sky usually sits). The shadow
    falls away from the brighter (light) side; ties → 'left' (matches the prompt's
    default "light from the upper right").
    """
    w = h = 64
    px = _grey_samples(scene, w, h)
    max_row = int(h * 0.6)
    half = w // 2
    left_sum = right_sum = 0
    for y in range(max_row):
        row = y * w
        for x in range(w):
            if x < half:
                left_sum += px[row + x]
            else:
                right_sum += px[row + x]
    return "right" if left_sum > right_sum else "left"


def scene_shadow_scale(scene: Image.Image) -> float:
    """Multiplier easing shadow darkness as the ground brightens (0.6–1.0).

    A fixed-alpha black shadow reads heavier on bright/warm ground than on dark
    ground, so the same strength that looks right in a dim scene looks too dark on
    a sunlit table. Samples the lower 40% (where the shadow lands) and eases off as
    it brightens. Capped at 1.0 so the scene check can only lighten, never darken.
    """
    w = h = 64
    px = _grey_samples(scene, w, h)
    start_row = int(h * 0.6)
    vals = px[start_row * w:]
    mean = sum(vals) / len(vals) if vals else 128
    return _clamp(1 - (mean - 100) * 0.0045, 0.6, 1.0)


def _blank_layer(size: int) -> Image.Image:
    return Image.new("RGBA", (size, size), (0, 0, 0, 0))


def contact_shadow(
    product_width: float, strength: float, size: int = CANVAS_SIZE, max_opacity: float = 0.5
) -> Image.Image:
    """Soft elliptical contact-shadow pool, centered on the canvas (RGBA layer).

    A soft outer pool plus a darker, wider core: the dense core at the contact
    point is what reads as "grounded" even on bright/busy surfaces, while the outer
    pool keeps it soft. Kept wider than the product base so the base edges sit well
    inside the solid core rather than on its fading rim (which looks "floating").
    """
    s = _clamp(strength, 0, 1)
    rx = round(product_width * 0.61)
    ry = round(rx * 0.3)
    blur = max(6, round(rx * 0.12))
    o = max_opacity * s
    cx = cy = size / 2

    layer = _blank_layer(size)
    draw = ImageDraw.Draw(layer)
    outer = round(255 * o * 0.6)
    core = round(255 * o)
    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=(0, 0, 0, outer))
    crx, cry = round(rx * 0.95), round(ry * 0.85)
    draw.ellipse([cx - crx, cy - cry, cx + crx, cy + cry], fill=(0, 0, 0, core))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def directional_ground_shadow(
    product_width: float,
    strength: float,
    direction: str,
    length: float,
    size: int = CANVAS_SIZE,
    max_opacity: float = 0.6,
) -> Image.Image:
    """Soft elongated capsule tail extending toward ``direction`` (RGBA layer).

    A capsule (stadium) rather than an ellipse so the tail keeps an even darkness
    along its length instead of fading to a point. Not a silhouette projection —
    projecting a shaped package yields hooked artifacts — just a clean directional
    cast, tilted a few degrees down toward the viewer.
    """
    s = _clamp(strength, 0, 1)
    o = max_opacity * s
    sign = -1 if direction == "left" else 1
    reach = product_width * (0.5 + 0.9 * _clamp(length, 0.2, 2))
    rx = round(reach * 0.5)
    ry = round(product_width * 0.17)
    blur = max(4, round(product_width * 0.045))
    cx = size / 2 + sign * round(rx * 0.55)
    cy = size / 2 + round(product_width * 0.13)
    angle = sign * 12  # degrees; SVG rotates clockwise, so negate for PIL below

    layer = _blank_layer(size)
    draw = ImageDraw.Draw(layer)
    draw.rounded_rectangle(
        [cx - rx, cy - ry, cx + rx, cy + ry], radius=ry, fill=(0, 0, 0, round(255 * o))
    )
    # PIL rotate is counter-clockwise; SVG rotate(angle) is clockwise → use -angle.
    layer = layer.rotate(-angle, resample=Image.BICUBIC, center=(cx, cy))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def depth_of_field_base(scene: Image.Image, ground_line: float, size: int = CANVAS_SIZE) -> Image.Image:
    """Cover the canvas, then blur only the background above the ground line.

    Strong blur at the top fading to sharp by the ground line (a vertical alpha
    gradient reveals the sharp base below the product), so the composited product
    reads as the in-focus subject.
    """
    base = cover_scene(scene, size)
    blurred = base.filter(ImageFilter.GaussianBlur(18))
    fade_start = max(0.0, ground_line - 0.28) * size
    fade_end = ground_line * size

    # Vertical mask: 255 (keep blur) above fade_start, ramping to 0 by fade_end.
    mask = Image.new("L", (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    span = max(1.0, fade_end - fade_start)
    for y in range(size):
        if y <= fade_start:
            a = 255
        elif y >= fade_end:
            a = 0
        else:
            a = round(255 * (1 - (y - fade_start) / span))
        mdraw.line([(0, y), (size, y)], fill=a)
    base.paste(blurred, (0, 0), mask)
    return base


def grounding_shadow_layers(
    scene: Image.Image,
    product_width: float,
    base_center_x: float,
    base_y: float,
    *,
    shadow_strength: float = 0.6,
    cast_shadow: bool = False,
    shadow_direction: str = "auto",
    shadow_length: float = 1.0,
    shadow_offset_x: float = 0.0,
    shadow_offset_y: float = 0.0,
    size: int = CANVAS_SIZE,
) -> list[tuple[Image.Image, int, int]]:
    """Build the grounding layers (directional tail + contact pool) for a product.

    Returns ``(layer, left, top)`` tuples to alpha-composite BENEATH the product
    (tail first, then pool). Darkness is scene-adaptive: the tail takes the full
    lightening, the pool only ~40% so it stays an even symmetric grounding.
    """
    shadow_strength = _clamp(shadow_strength, 0, 1)
    scene_scale = scene_shadow_scale(scene)
    eff = _clamp(shadow_strength * scene_scale, 0, 1)
    pool_strength = _clamp(shadow_strength * (1 - 0.4 * (1 - scene_scale)), 0, 1)
    layers: list[tuple[Image.Image, int, int]] = []

    if cast_shadow:
        direction = shadow_direction if shadow_direction in ("left", "right") else detect_light_side(scene)
        tail = directional_ground_shadow(product_width, eff, direction, shadow_length, size)
        dx = shadow_offset_x * product_width
        dy = shadow_offset_y * product_width
        layers.append((tail, round(base_center_x - size / 2 + dx), round(base_y - size / 2 + dy)))

    pool = contact_shadow(product_width, pool_strength, size, 0.7)
    pool_drop = round(product_width * 0.07)
    layers.append((pool, round(base_center_x - size / 2), round(base_y - size / 2 + pool_drop)))
    return layers


def sample_scene_light(scene: Image.Image, ground_line: float = 0.82,
                       size: int = CANVAS_SIZE) -> tuple[tuple[int, int, int], float]:
    """Sample a scene's ambient colour + luminance near where the product grounds.

    Reads a horizontal band around the ground line (the surface the product rests on,
    which drives the bounce light hitting it) and returns ``((r, g, b), luminance)``.
    Used by :func:`relight_cutout` so a composited product can be tinted/toned to the
    scene instead of keeping its studio lighting.
    """
    img = cover_scene(scene, size).convert("RGB")
    y0 = int(_clamp(ground_line - 0.18, 0.0, 1.0) * size)
    y1 = int(_clamp(ground_line + 0.08, 0.0, 1.0) * size)
    band = img.crop((0, y0, size, max(y1, y0 + 1))).resize((32, 8), Image.LANCZOS)
    raw = band.tobytes()  # RGB, row-major
    n = max(1, len(raw) // 3)
    r = sum(raw[0::3]) / n
    g = sum(raw[1::3]) / n
    b = sum(raw[2::3]) / n
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return (round(r), round(g), round(b)), lum


def relight_cutout(
    cutout: Image.Image,
    *,
    ambient_rgb: tuple[int, int, int],
    ambient_lum: float,
    light_side: str = "left",
    strength: float = 0.35,
) -> Image.Image:
    """Gently relight a product cutout toward a scene's ambient light (RGBA out).

    Three subtle, global adjustments so a studio-lit product doesn't read as pasted
    onto a warm, low-key scene: (1) a low-opacity tint toward the scene's ambient
    colour, (2) a brightness ease-down toward the scene's luminance (only darkens,
    never brightens, so label contrast is preserved), and (3) a soft directional
    shade on the side away from the light. Deliberately mild — the product's label
    and design stay fully legible; only the lighting shifts. Alpha is preserved, so
    the real product geometry/text is never redrawn.
    """
    s = _clamp(strength, 0.0, 1.0)
    rgba = cutout.convert("RGBA")
    alpha = rgba.getchannel("A")
    rgb = rgba.convert("RGB")

    # 1) Tint toward the scene's ambient colour (flat overlay at low opacity).
    ambient = Image.new("RGB", rgb.size, ambient_rgb)
    rgb = Image.blend(rgb, ambient, 0.16 * s)

    # 2) Brightness match: ease a bright product down toward a dark scene. Factor in
    #    [0.72, 1.0]; only darkens. A luminance of ~180+ leaves it essentially as-is.
    factor = _clamp(0.60 + 0.40 * (ambient_lum / 180.0), 0.72, 1.0)
    factor = 1.0 - (1.0 - factor) * s
    rgb = ImageEnhance.Brightness(rgb).enhance(factor)

    # 3) Directional shade: a horizontal gradient darkening the side away from the
    #    light (max ~24% at the far edge), multiplied over the product.
    w, h = rgb.size
    row = Image.new("L", (w, 1))
    for x in range(w):
        t = x / max(1, w - 1)  # 0 at left edge, 1 at right edge
        away = t if light_side == "left" else (1.0 - t)  # 1 on the shadowed side
        row.putpixel((x, 0), int(255 - away * 60 * s))
    shade = Image.merge("RGB", (row, row, row)).resize((w, h))
    rgb = ImageChops.multiply(rgb, shade)

    out = rgb.convert("RGBA")
    out.putalpha(alpha)
    return out


def contact_ao_layer(product_width: float, strength: float = 0.6,
                     size: int = CANVAS_SIZE) -> Image.Image:
    """A tight, dark ambient-occlusion band for right under the product base (RGBA).

    A small, dense, lightly-blurred ellipse centred on the canvas — composited at the
    product's base beneath the soft contact pool, it's the hard "pressed into the
    surface" contact that keeps the base from looking like it floats above the ground.
    """
    s = _clamp(strength, 0.0, 1.0)
    rx = round(product_width * 0.42)
    ry = max(2, round(rx * 0.16))
    blur = max(2, round(product_width * 0.02))
    cx = cy = size / 2
    layer = _blank_layer(size)
    ImageDraw.Draw(layer).ellipse(
        [cx - rx, cy - ry, cx + rx, cy + ry], fill=(0, 0, 0, round(255 * s * 0.6)))
    return layer.filter(ImageFilter.GaussianBlur(blur))


def composite_product_on_scene(
    scene: Image.Image,
    cutout: Image.Image,
    *,
    scale: float = 0.58,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
    shadow_strength: float = 0.6,
    ground_line: float = 0.8,
    blur_background: bool = False,
    cast_shadow: bool = False,
    shadow_direction: str = "auto",
    shadow_length: float = 1.0,
    shadow_offset_x: float = 0.0,
    shadow_offset_y: float = 0.0,
    contact_ao: float = 0.0,
) -> Image.Image:
    """Composite an approved cutout onto a scene with a grounding shadow (RGB out).

    The product stays proportional and inside the safe boundaries; it is grounded
    at ``ground_line`` (fraction of canvas height) with the offsets applied.
    ``contact_ao`` (0 disables it, the default, so existing callers are unchanged)
    adds a tight ambient-occlusion band at the base for a firmer "pressed into the
    surface" contact.
    """
    size = CANVAS_SIZE
    scale = _clamp(scale, 0.2, 0.9)
    offset_x = _clamp(offset_x, -1, 1)
    offset_y = _clamp(offset_y, -1, 1)
    shadow_strength = _clamp(shadow_strength, 0, 1)
    ground_line = _clamp(ground_line, 0.5, 0.95)

    base = (depth_of_field_base(scene, ground_line, size) if blur_background
            else cover_scene(scene, size)).convert("RGBA")

    box_h = size * scale
    box_w = size * min(0.86, scale + 0.2)
    product = fit_cutout(cutout, box_w, box_h)
    pw, ph = product.size

    rng = (size - pw) / 2 - SAFE_MARGIN / 2
    left = round((size - pw) / 2 + offset_x * max(0, rng))
    baseline = size * ground_line
    top = round(_clamp(baseline - ph + offset_y * (size * 0.12),
                       SAFE_MARGIN / 2, size - ph - SAFE_MARGIN / 4))

    base_center_x = left + pw / 2
    base_y = top + ph

    for layer, lx, ly in grounding_shadow_layers(
        scene, pw, base_center_x, base_y,
        shadow_strength=shadow_strength, cast_shadow=cast_shadow,
        shadow_direction=shadow_direction, shadow_length=shadow_length,
        shadow_offset_x=shadow_offset_x, shadow_offset_y=shadow_offset_y, size=size,
    ):
        base.alpha_composite(layer, (lx, ly))
    # Tight ambient-occlusion contact at the base (above the soft pool, below the
    # product) so the base reads as pressed into the surface rather than floating.
    if contact_ao > 0:
        ao = contact_ao_layer(pw, contact_ao, size)
        base.alpha_composite(ao, (round(base_center_x - size / 2),
                                  round(base_y - size / 2)))
    base.alpha_composite(product, (left, top))
    return base.convert("RGB")


def composite_logo(
    image: Image.Image,
    logo: Image.Image,
    *,
    corner: str = "top-left",
    max_width_fraction: float = 0.22,
    max_height_fraction: float = 0.12,
    margin: int | None = None,
    size: int = CANVAS_SIZE,
) -> Image.Image:
    """Composite a brand logo into a corner as a lightweight watermark (RGB out)."""
    margin = SAFE_MARGIN if margin is None else margin
    fitted = fit_cutout(logo, size * max_width_fraction, size * max_height_fraction)
    fw, fh = fitted.size
    left = margin if corner.endswith("left") else size - fw - margin
    top = margin if corner.startswith("top") else size - fh - margin
    out = image.convert("RGBA")
    out.alpha_composite(fitted, (round(left), round(top)))
    return out.convert("RGB")


def create_thumbnail(image: Image.Image, width: int = 480) -> Image.Image:
    """Small square thumbnail for gallery cards."""
    return ImageOps.fit(image.convert("RGB"), (width, width), method=Image.LANCZOS)


def export_image(image: Image.Image) -> bytes:
    """Normalize a composed image to a final sRGB PNG and return the bytes."""
    return to_png_bytes(image.convert("RGB"))
