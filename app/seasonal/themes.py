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

This module also defaults a campaign's *planning dates* from the chosen season
(see :func:`default_dates`): the launch date is a lead time before the season's
holiday, the expiration is just after it, and the reminder is a few business days
before launch. Seasons with no true calendar date (summer, back to school) use a
representative date; ``custom`` has no date and so leaves the fields blank.

These are data + pure date/string assembly only: no I/O, no secrets.
"""

from datetime import date, timedelta

# The product is grounded low in the frame; the backdrop must have its resting
# surface at this line (fraction of height). Matches the image-set LIFESTYLE
# placement so the shared compositor lands the product naturally on the surface.
GROUND_LINE = 0.82

# Season presets. ``slug`` is the stored ``seasonal_campaigns.theme`` value; ``label``
# is the UI name; ``scene`` is the backdrop brief. ``custom`` is handled specially:
# the user supplies the scene text, so it carries no fixed ``scene`` here.
# Ordered roughly by calendar (New Year's → Christmas), then the season presets
# with no single date, then Custom last. Order here drives the dropdown order.
THEMES: dict[str, dict] = {
    "new_years": {
        "label": "New Year's",
        "scene": (
            "An elegant New Year's celebration setting: a dark, softly reflective "
            "surface with gold accents set to the sides and sparkling champagne-gold "
            "bokeh lights, with subtle confetti glinting out of focus in the blurred "
            "midnight background."
        ),
    },
    "valentines_day": {
        "label": "Valentine's Day",
        "scene": (
            "A romantic Valentine's Day setting: a soft blush-and-cream surface with a "
            "few delicate red and pink roses and loose rose petals set gently to the "
            "sides, warm candlelight and heart-shaped bokeh softly out of focus in the "
            "background."
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
    "memorial_day": {
        "label": "Memorial Day",
        "scene": (
            "A warm, patriotic late-spring Memorial Day setting: a clean light wooden "
            "surface with softly-focused red, white, and blue accents set to the sides "
            "and a bright, sunny backyard mood blurred in the background."
        ),
    },
    "fathers_day": {
        "label": "Father's Day",
        "scene": (
            "A relaxed, handsome Father's Day setting: a rich warm wooden surface with a "
            "few softly-focused masculine accents set to the sides and a cozy den or "
            "sunny backyard mood blurred in the background."
        ),
    },
    "fourth_of_july": {
        "label": "4th of July",
        "scene": (
            "A festive Fourth of July setting: a rustic wooden surface with softly-"
            "focused red, white, and blue accents and small star details to the sides, "
            "warm summer-evening light and fireworks-sparkle bokeh in the blurred "
            "background."
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
    "back_to_school": {
        "label": "Back to School",
        "scene": (
            "A bright, upbeat back-to-school setting: a clean wooden desk with a few "
            "softly-focused supplies set to the sides — notebooks, colored pencils, a "
            "small stack of books — and a sunny classroom or home-study space blurred "
            "in the background."
        ),
    },
    "labor_day": {
        "label": "Labor Day",
        "scene": (
            "A relaxed end-of-summer Labor Day setting: a light wooden picnic surface "
            "with a few softly-focused summery props set to the sides and a sunny "
            "backyard-barbecue mood with warm blue-sky bokeh in the blurred background."
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
    "thanksgiving": {
        "label": "Thanksgiving",
        "scene": (
            "A warm, rustic Thanksgiving setting: a wooden harvest table with softly-"
            "focused autumn accents to the sides — amber and russet leaves, a few small "
            "gourds, wheat sprigs — and a cozy candlelit dining mood blurred in the warm "
            "golden background."
        ),
    },
    "christmas": {
        "label": "Christmas",
        "scene": (
            "A cozy Christmas holiday setting: a wooden table dressed with soft "
            "evergreen sprigs, warm string lights glowing out of focus behind, a few "
            "red and gold ornaments and pinecones set to the sides, gentle snowfall "
            "visible through a window in the blurred background."
        ),
    },
    "custom": {
        "label": "Custom",
        "scene": "",  # supplied by the user's custom_prompt
    },
}

DEFAULT_THEME = "christmas"

# ── Campaign date defaults ──────────────────────────────────────────────────
# A campaign's planning dates default from the chosen season. The reference point
# is the season's holiday; launch leads it by LAUNCH_LEAD_DAYS calendar days, the
# expiration trails it by one day, and the reminder sits REMINDER_BUSINESS_DAYS
# working days (Mon–Fri) before launch. All three are editable in the form.
LAUNCH_LEAD_DAYS = 30
REMINDER_BUSINESS_DAYS = 3

# Holiday rules, keyed by theme slug. Each is one of:
#   (_FIXED, month, day)            — a calendar-fixed date (incl. "close enough"
#                                     stand-ins for seasons with no true date)
#   (_NTH,  month, weekday, n)      — the n-th <weekday> of <month>
#   (_LAST, month, weekday)         — the last <weekday> of <month>
# weekday uses date.weekday(): Monday=0 … Sunday=6. ``custom`` is intentionally
# absent (no real date → blank defaults).
_FIXED, _NTH, _LAST = "fixed", "nth", "last"
_HOLIDAYS: dict[str, tuple] = {
    "new_years": (_FIXED, 1, 1),
    "valentines_day": (_FIXED, 2, 14),
    "mothers_day": (_NTH, 5, 6, 2),      # 2nd Sunday of May
    "memorial_day": (_LAST, 5, 0),       # last Monday of May
    "fathers_day": (_NTH, 6, 6, 3),      # 3rd Sunday of June
    "summer": (_FIXED, 6, 21),           # ~solstice (season has no single date)
    "fourth_of_july": (_FIXED, 7, 4),
    "back_to_school": (_FIXED, 8, 15),   # ~mid-August (season has no single date)
    "labor_day": (_NTH, 9, 0, 1),        # 1st Monday of September
    "halloween": (_FIXED, 10, 31),
    "thanksgiving": (_NTH, 11, 3, 4),    # 4th Thursday of November
    "christmas": (_FIXED, 12, 25),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th ``weekday`` (Mon=0…Sun=6) of a month, e.g. 4th Thursday."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """The last ``weekday`` (Mon=0…Sun=6) of a month, e.g. last Monday of May."""
    last = (date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)) \
        - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _holiday_in_year(spec: tuple, year: int) -> date:
    """Resolve a holiday rule to its concrete date in ``year``."""
    kind = spec[0]
    if kind == _FIXED:
        return date(year, spec[1], spec[2])
    if kind == _NTH:
        return _nth_weekday(year, spec[1], spec[2], spec[3])
    if kind == _LAST:
        return _last_weekday(year, spec[1], spec[2])
    raise ValueError(f"unknown holiday spec kind: {kind!r}")


def _next_holiday(spec: tuple, today: date) -> date:
    """The next upcoming occurrence of a holiday on or after ``today``.

    Defaults are for *planning the next run of the season*, so a holiday already
    past this year rolls to next year.
    """
    d = _holiday_in_year(spec, today.year)
    return d if d >= today else _holiday_in_year(spec, today.year + 1)


def _minus_business_days(d: date, n: int) -> date:
    """``d`` moved back ``n`` working days (skipping Saturdays and Sundays)."""
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # Mon–Fri
            n -= 1
    return d


def default_dates(slug: str | None, today: date | None = None) -> dict[str, str]:
    """Default launch/expiration/reminder dates for a season, as ISO strings.

    Launch leads the season's next holiday by :data:`LAUNCH_LEAD_DAYS` days,
    expiration trails it by one day, and the reminder sits
    :data:`REMINDER_BUSINESS_DAYS` working days before launch. A season with no
    holiday rule (notably ``custom``) returns blank strings so the form fields are
    left empty for the user to fill in. ``today`` is injectable for testing.
    """
    spec = _HOLIDAYS.get(slug or "")
    if spec is None:
        return {"launch_date": "", "expiration_date": "", "reminder_date": ""}
    today = today or date.today()
    holiday = _next_holiday(spec, today)
    launch = holiday - timedelta(days=LAUNCH_LEAD_DAYS)
    return {
        "launch_date": launch.isoformat(),
        "expiration_date": (holiday + timedelta(days=1)).isoformat(),
        "reminder_date": _minus_business_days(launch, REMINDER_BUSINESS_DAYS).isoformat(),
    }


def theme_defaults(today: date | None = None) -> dict[str, dict]:
    """Default dates for every theme, keyed by slug — the form's lookup table.

    Serialized to the page so the client can fill the date fields when the user
    picks a season, with the date math kept here as the single source of truth.
    """
    return {slug: default_dates(slug, today) for slug in THEMES}


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
    """Assemble the backdrop prompt for the COMPOSITE render path (product-free scene).

    The composite pastes the real product at a fixed ground line, so the backdrop
    must present a *predictable* place to land — the original failure was the model
    inventing a stone slab / cutting board / pedestal with an edge the product then
    hung over. This prompt therefore forbids any discrete raised object under the
    product and demands ONE continuous, flat, full-width surface at :data:`GROUND_LINE`,
    with all seasonal decor pushed into the blurred background. Keeps placement
    reliable across any product shape while the real label stays pixel-exact.
    """
    scene = resolve_scene(theme, custom_prompt)
    surface_pct = round(GROUND_LINE * 100)
    band_top = round((GROUND_LINE - 0.12) * 100)
    return (
        "Create a photorealistic seasonal product-photography BACKGROUND. The product "
        "itself is added later — do not include the product.\n\n"
        f"Seasonal mood:\n{scene}\n\n"
        "Foreground resting surface (critical — a product is composited onto it later):\n"
        f"- Exactly ONE continuous, flat, level, horizontal surface fills the bottom of the "
        f"frame edge to edge. Its top runs straight across at about {surface_pct}% down from "
        f"the top, and the solid surface fills from about {band_top}% down to the bottom edge — "
        "full width, no gap, no drop-off.\n"
        "- Do NOT place any slab, stone, board, cutting board, tray, plate, pedestal, riser, "
        "platform, box, crate, pumpkin, or other raised object ON or in FRONT of this surface. "
        "There must be no edge, lip, step, or separate object for a product to hang over, "
        "straddle, or balance on — the product will rest directly on the one continuous surface.\n"
        "- Keep the horizontal center of the surface clear and unobstructed (the product and "
        "its shadow go there).\n\n"
        "Seasonal decor placement:\n"
        "- Put all seasonal elements (e.g. pumpkins, leaves, string lights, ornaments, snow, "
        "candles, flowers) in the BLURRED BACKGROUND, behind and above the surface — never on "
        "the surface itself and never where the product sits.\n\n"
        "Depth of field and light:\n"
        "- The foreground surface is sharp and well-lit; everything behind it is softly "
        "blurred (bokeh) so a product placed on the surface reads as the crisp subject.\n"
        "- One coherent, warm light source; soft shadows all falling the same direction.\n\n"
        "Requirements:\n"
        "- No product, packaging, bottles, cans, people, hands, readable text, or logos.\n"
        "- Realistic scale, perspective and lighting; suitable for a professional eCommerce "
        "marketplace listing."
    )


def build_seasonal_edit_prompt(theme: str, custom_prompt: str | None = None) -> str:
    """Assemble the prompt for the AI-EDIT render path (model places the product).

    Unlike the composite prompt (which generates an *empty* backdrop), this is sent
    to the image model's edit endpoint together with the real product image: the
    model places the actual product into a seasonal scene it builds around it, so
    placement, perspective, contact shadow and relighting are the model's job — no
    fixed ground line to mismatch. The product-preservation block is deliberately
    forceful because the edit endpoint regenerates pixels and we need the label/text
    to survive.
    """
    scene = resolve_scene(theme, custom_prompt)
    return (
        "Place the provided product into a photorealistic seasonal lifestyle scene — "
        "a professional eCommerce marketing image.\n\n"
        "CRITICAL — keep the product EXACTLY as provided:\n"
        "- Preserve its shape, proportions, colors, materials, and finish unchanged.\n"
        "- Reproduce every label, logo, word, number, and graphic on the packaging "
        "exactly as shown — do not reword, restyle, translate, blur, add, or remove any "
        "text or branding, and do not invent new text.\n"
        "- Show a single instance of the product; do not add or duplicate products.\n\n"
        f"Scene to build around it:\n{scene}\n\n"
        "Placement and realism:\n"
        "- Stand the product upright, fully visible and roughly centered, resting "
        "naturally on a believable surface in the scene at correct perspective and scale.\n"
        "- Ground it convincingly: a real contact shadow (and a soft reflection on a "
        "glossy surface) consistent with the scene's main light direction.\n"
        "- Relight the product so its lighting and color temperature match the scene, "
        "while keeping every label fully legible.\n"
        "- Keep the background softly out of focus so the product is the sharp subject.\n\n"
        "Do not add people, hands, captions, watermarks, borders, or any logos other than "
        "the product's own."
    )
