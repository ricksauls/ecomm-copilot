"""Season presets and the seasonal-backdrop prompt builder.

Each preset is a season the user can pick for a campaign; its ``scene`` text is the
creative brief fed to the image model as the backdrop description. The actual prompt
is assembled by :func:`build_seasonal_prompt`, which wraps the scene in the image-
set lifestyle scaffolding (a pinned resting surface + depth-of-field) so the product
cutout the compositor adds later lands ON a surface instead of floating.

Design rule carried over from the image-set prompts (see app.imageset.prompts): the
model generates the *background only* — never the product, text, logos, or people.
The real product (a transparent cutout) is composited programmatically afterward, so
the model can't hallucinate a competing product or a readable claim into the scene.

These are data + pure string assembly only: no I/O, no secrets.
"""

from app.imageset.prompts import build_lifestyle_prompt

# The product is grounded low in the frame; the backdrop must have its resting
# surface at this line (fraction of height). Matches the image-set LIFESTYLE
# placement so the shared compositor lands the product naturally on the surface.
GROUND_LINE = 0.82

# Season presets. ``slug`` is the stored ``seasonal_campaigns.theme`` value; ``label``
# is the UI name; ``scene`` is the backdrop brief. ``custom`` is handled specially:
# the user supplies the scene text, so it carries no fixed ``scene`` here.
THEMES: dict[str, dict] = {
    "christmas": {
        "label": "Christmas",
        "scene": (
            "A cozy Christmas holiday setting: a wooden table dressed with soft "
            "evergreen sprigs, warm string lights glowing out of focus behind, a few "
            "red and gold ornaments and pinecones set to the sides, gentle snowfall "
            "visible through a window in the blurred background."
        ),
    },
    "halloween": {
        "label": "Halloween",
        "scene": (
            "A playful autumn Halloween setting: a rustic wooden surface with a couple "
            "of small pumpkins and gourds and a scatter of fallen orange and amber "
            "leaves to the sides, warm candlelit glow and soft spooky mist in the "
            "out-of-focus background."
        ),
    },
    "back_to_school": {
        "label": "Back to School",
        "scene": (
            "A bright, upbeat back-to-school setting: a clean wooden desk with a few "
            "softly-focused supplies set to the sides — notebooks, colored pencils, a "
            "small stack of books — and a sunny classroom or home-study space blurred "
            "in the background."
        ),
    },
    "summer": {
        "label": "Summer",
        "scene": (
            "A fresh, sunny summer setting: a light wooden table or picnic surface with "
            "a few bright seasonal props to the sides — citrus slices, a sprig of mint, "
            "a folded striped napkin — and warm sunshine with blue-sky bokeh in the "
            "blurred background."
        ),
    },
    "mothers_day": {
        "label": "Mother's Day",
        "scene": (
            "A soft, elegant Mother's Day setting: a pale marble or light wooden surface "
            "with a few delicate fresh flowers (blush and cream roses, eucalyptus) set "
            "gently to the sides and a warm, airy pastel background softly out of focus."
        ),
    },
    "custom": {
        "label": "Custom",
        "scene": "",  # supplied by the user's custom_prompt
    },
}

DEFAULT_THEME = "christmas"


def is_valid_theme(slug: str | None) -> bool:
    """True when ``slug`` is a known theme (input-validation allowlist)."""
    return slug in THEMES


def theme_label(slug: str | None) -> str:
    """Human label for a theme slug; falls back to the slug itself if unknown."""
    preset = THEMES.get(slug or "")
    return preset["label"] if preset else (slug or "")


def theme_choices() -> list[dict]:
    """Theme presets as display dicts (slug/label) for the campaign form, in order."""
    return [{"slug": slug, "label": preset["label"]} for slug, preset in THEMES.items()]


def resolve_scene(theme: str, custom_prompt: str | None = None) -> str:
    """The backdrop scene brief for a theme.

    For a preset theme this is its canned ``scene``; for ``custom`` it's the user's
    free-text ``custom_prompt``. A blank custom prompt falls back to a neutral
    seasonal brief so generation never sends an empty scene to the model.
    """
    if theme == "custom":
        text = (custom_prompt or "").strip()
        return text or "A warm, tasteful seasonal setting with soft, relevant props."
    preset = THEMES.get(theme)
    return preset["scene"] if preset else resolve_scene("custom", custom_prompt)


def build_seasonal_prompt(theme: str, custom_prompt: str | None = None) -> str:
    """Assemble the image-model prompt for a campaign's seasonal backdrop.

    Wraps the theme's scene brief in the lifestyle scaffolding (resting surface
    pinned to :data:`GROUND_LINE`, depth of field, single coherent light), so the
    product the compositor adds afterward is grounded and reads as the sharp
    subject. ``category`` is generic ("seasonal lifestyle") because a campaign spans
    many products — the season, not the product category, drives the scene.
    """
    return build_lifestyle_prompt(
        category="seasonal lifestyle product photography",
        scene_description=resolve_scene(theme, custom_prompt),
        ground_line=GROUND_LINE,
    )
