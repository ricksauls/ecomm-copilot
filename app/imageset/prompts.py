"""Prompt builders for the image-set pipeline.

Pure functions that assemble the text prompts sent to the image/text models,
ported from the source app's ``src/prompts``. Two design rules carry over and
matter for correctness:

- **The AI generates scenes/backgrounds only — never the product or any text.**
  The real product (a transparent cutout) and all factual copy are composited
  programmatically afterward, so the model can never hallucinate a dimension,
  claim, or a second competing product into the image.
- **The lifestyle prompt pins the resting surface to the compositor's ground
  line** so the composited product lands ON a surface instead of floating.

These are prompt strings only; they perform no I/O and hold no secrets.
"""


def build_lifestyle_prompt(
    *,
    category: str,
    scene_description: str,
    placement_area: str = "the lower-center foreground",
    product_width: str = "a typical retail package width",
    product_height: str = "a typical retail package height",
    lighting: str | None = None,
    camera_direction: str | None = None,
    ground_line: float = 0.86,
    props: bool = True,
) -> str:
    """Build the lifestyle-background prompt (scene only; product added later).

    ``ground_line`` is the fraction of image height where the compositor will set
    the product's base; the prompt converts it to a "% down from the top" and
    pins the resting surface there — the single most important constraint, so the
    product doesn't float above a gap. Clamped to a sane [0.5, 0.95] band.
    """
    lighting = lighting or (
        "warm, natural daylight with the main light coming from the upper right, "
        "casting soft shadows toward the lower left across the foreground surface"
    )
    camera = camera_direction or (
        "eye-level, ~50mm, very shallow depth of field (background strongly blurred)"
    )

    # Convert the compositor's ground line to a percentage the model can target.
    ground_line = min(0.95, max(0.5, ground_line))
    surface_pct = round(ground_line * 100)
    surface_band_top = round((ground_line - 0.1) * 100)

    # Contextual staging fills the negative space beside/behind the product while
    # keeping the placement patch clear for the composited product and its shadow.
    staging = ""
    if props:
        staging = (
            "\n\nContextual staging (fill empty space so the scene doesn't look bare):\n"
            "- Arrange a few tasteful, relevant, softly-focused supporting props ON the "
            "surface toward the sides and slightly behind the placement patch — items "
            "appropriate to the scene (e.g. a mug, utensils, a folded cloth or napkin, "
            "foliage, simple food, small outdoor gear).\n"
            "- Keep the central placement patch and the surface immediately in front of and "
            "beside its base clear (this is where the product and its shadow go). Props "
            "frame the product; they never crowd or touch it.\n"
            "- Do NOT stage another competing product. Props are generic set dressing, not "
            "products."
        )

    return (
        f"Create a photorealistic eCommerce lifestyle background for a product in the "
        f"category: {category}.\n\n"
        f"Scene:\n{scene_description}\n\n"
        "Foreground resting surface (critical — a product is composited onto it later):\n"
        f"- Include a real, solid, in-focus horizontal resting surface in {placement_area} — "
        "a natural, relevant platform such as a flat rock, wooden picnic table, log, or "
        "tabletop appropriate to the scene.\n"
        f"- The TOP of this surface (the flat plane the product will sit on) must run "
        f"horizontally across the frame at approximately {surface_pct}% down from the top of "
        f"the image. Continuous solid surface must span the FULL WIDTH of the frame and fill "
        f"it from about {surface_band_top}% down to the bottom edge — no gap, no drop-off, and "
        f"no front edge of the table sitting above the {surface_pct}% line at the center.\n"
        f"- Directly at the horizontal center on that {surface_pct}% line there must be a flat, "
        f"empty, unobstructed patch of surface large enough for a product roughly "
        f"{product_width} wide by {product_height} high to rest on. A vertical object placed "
        "with its base on that line must appear to sit firmly ON the surface, not float in "
        "front of or above it.\n"
        "- The surface is sharp and well-lit; do not put the placement patch on a steep edge, "
        "a slope, or the far side of a gap.\n\n"
        "Depth of field:\n"
        f"- The resting surface and foreground are in sharp focus; everything behind the "
        f"surface is softly blurred (bokeh), so a product placed on the surface reads as the "
        f"crisp subject. The blur begins BEHIND and ABOVE the {surface_pct}% line, never in "
        f"front of the placement patch.{staging}\n\n"
        f"Lighting: {lighting}\n"
        f"Camera: {camera}\n\n"
        "Requirements:\n"
        "- Do not include the product itself, any packaging, bottle, or can (the real product "
        "is added later).\n"
        "- Do not include brand logos, readable text, or competing products.\n"
        "- Keep the placement patch empty and unobstructed, and the surface beneath it "
        "continuous, solid, and full-width.\n"
        "- Use realistic scale, perspective, lighting, and shadows.\n"
        "- One coherent light source: every object and prop casts its shadow in the SAME "
        "direction (away from the light described above), with consistent softness — no "
        "conflicting or crossing shadows.\n"
        "- Make the scene suitable for a professional eCommerce marketplace listing."
    )


def build_backdrop_prompt(
    *,
    category: str,
    environment: str | None = None,
    visual_style: str | None = None,
) -> str:
    """Build the atmospheric backdrop prompt for hybrid feature/infographic assets.

    Produces an out-of-focus photographic background only — never the product,
    text, logos, or people — onto which programmatic overlays are composited.
    """
    setting = (environment or "").strip() or f"a setting where {category} is used"
    style = (visual_style or "").strip() or "clean, professional, muted"
    return (
        f"A photographic background of {setting}, shot from a low, near-ground camera\n"
        "angle across a receding ground plane.\n"
        f"Style: {style}, natural light, calm muted tones suitable as a backdrop behind "
        "product\nmarketing text.\n\n"
        "Depth of field — this is important:\n"
        "- Graduated focus. The ground plane along the BOTTOM of the frame is in sharper "
        "focus,\n  with a visible surface the product can rest on.\n"
        "- Blur increases progressively toward the TOP of the frame; the sky, horizon, and "
        "distant\n  background are the softest and most out of focus.\n"
        "- The result is a natural vertical focus gradient (crisp foreground ground → dreamy "
        "blurred\n  distance), NOT a uniformly blurred image.\n\n"
        "Strict requirements:\n"
        "- No product, no packaging, no bottles or cans.\n"
        "- No text, letters, numbers, or logos.\n"
        "- No people, no hands, no faces.\n"
        "- Simple, with soft negative space; no sharp subject or focal object competing for "
        "attention.\n"
        "- Photorealistic, premium eCommerce look."
    )


def build_size_comparison_prompt(
    *,
    product_name: str,
    product_height: str,
    product_width: str | None,
    reference_phrases: list[str],
) -> str:
    """Scene prompt for the size-comparison IMAGE-EDIT workflow.

    Unlike the lifestyle prompt (which forbids rendering the product), this one is
    sent to ``edit_image`` with the real product image as the reference: the model
    PRESERVES the supplied product and places one or two everyday reference objects
    beside it at ACCURATE relative scale, anchored by the real dimensions. The model
    draws NO text — the exact figures and headline are composited on afterward.
    Ported from the source app's ``buildSizeComparisonScenePrompt``; because AI
    relative scale isn't guaranteed, the result always needs human review.
    """
    ref_list = "\n".join(f"- {r}" for r in reference_phrases)
    width_clause = f", {product_width} wide" if product_width else ""
    return (
        "Create a photorealistic size-comparison image using the supplied product "
        "image as the product reference.\n\n"
        f"Product: {product_name}\n"
        f"Real product size: {product_height} tall{width_clause}.\n\n"
        "Scene:\n"
        "- Place the product upright on a clean, seamless PURE WHITE studio background "
        "(white #FFFFFF, evenly lit — no gray, no colored gradient, no vignette) with "
        "soft, even lighting and gentle contact shadows.\n"
        "- Next to the product, place these everyday reference objects for scale:\n"
        f"{ref_list}\n"
        "- Arrange the product and the reference objects side by side on the SAME flat "
        "surface, at the same eye level, so their heights can be compared directly.\n"
        "- Center the product in the row and space the items EVENLY: the horizontal gap "
        "between the product and each neighboring object must be equal, and the gaps "
        "between all items consistent.\n\n"
        "Accurate relative scale (critical):\n"
        "- Size every object correctly RELATIVE to the product and to each other, based "
        "on the real sizes given above. This image exists to show true size — the "
        "proportions must be believable.\n"
        "- Do NOT resize the product to match the references; resize the references to "
        "their real size relative to the product.\n\n"
        "Preserve the supplied product's exact package shape, brand colors, "
        "cap/nozzle/closure, proportions, logo placement, and label structure.\n\n"
        "Do not:\n"
        "- Invent claims, add new text, numbers, rulers, or measurement labels (these "
        "are added separately).\n"
        "- Add any objects other than the product and the listed reference objects.\n"
        "- Change the package color or product type, or add a competing brand.\n\n"
        "This result requires human review to confirm the relative sizes are accurate."
    )
