"""Tests for the seasonal theme presets and the backdrop prompt builder."""

from datetime import date

from app.seasonal import themes


def test_known_themes_validate_and_label():
    assert themes.is_valid_theme("christmas") is True
    assert themes.is_valid_theme("mothers_day") is True
    assert themes.is_valid_theme("custom") is True
    assert themes.is_valid_theme("easter") is False
    assert themes.is_valid_theme(None) is False
    assert themes.theme_label("halloween") == "Halloween"
    # Unknown slug falls back to the slug itself (never raises).
    assert themes.theme_label("easter") == "easter"


def test_new_themes_present_and_labeled():
    # The seasons added in this round all validate and carry their UI labels.
    expected = {
        "thanksgiving": "Thanksgiving",
        "new_years": "New Year's",
        "valentines_day": "Valentine's Day",
        "memorial_day": "Memorial Day",
        "fathers_day": "Father's Day",
        "fourth_of_july": "4th of July",
        "labor_day": "Labor Day",
    }
    for slug, label in expected.items():
        assert themes.is_valid_theme(slug) is True
        assert themes.theme_label(slug) == label
        # Every non-custom preset carries a non-empty backdrop scene.
        assert themes.resolve_scene(slug).strip() != ""


def test_theme_choices_cover_every_preset_in_order():
    slugs = [c["slug"] for c in themes.theme_choices()]
    assert slugs == list(themes.THEMES.keys())
    assert "custom" in slugs


def test_resolve_scene_preset_vs_custom():
    # A preset returns its canned scene text.
    assert "Christmas" in themes.resolve_scene("christmas")
    # Custom uses the user's prompt verbatim.
    assert themes.resolve_scene("custom", "A spooky foggy graveyard") == \
        "A spooky foggy graveyard"
    # A blank custom prompt falls back to a neutral brief (never empty).
    assert themes.resolve_scene("custom", "   ").strip() != ""
    assert themes.resolve_scene("custom", None).strip() != ""


def test_build_seasonal_prompt_pins_surface_and_excludes_product():
    prompt = themes.build_seasonal_prompt("christmas")
    low = prompt.lower()
    # Pins a resting surface (so the product grounds) and forbids the product/text.
    assert "resting surface" in low
    assert "do not include the product" in low
    # Must forbid discrete raised props the composited product could hang over, and
    # require a single full-width continuous surface (the reliability fix).
    assert "slab" in low and "pedestal" in low
    assert "continuous" in low and "full width" in low
    # The theme's scene brief is woven into the prompt.
    assert "christmas" in low


def test_build_seasonal_prompt_custom_uses_user_scene():
    prompt = themes.build_seasonal_prompt("custom", "A Diwali table with marigolds and diyas")
    assert "marigolds" in prompt


def test_default_dates_fixed_holiday():
    # Christmas (Dec 25). From mid-October the upcoming one is this year.
    d = themes.default_dates("christmas", today=date(2026, 10, 12))
    assert d["expiration_date"] == "2026-12-26"           # day after the holiday
    assert d["launch_date"] == "2026-11-25"               # 30 days before
    # Reminder is 3 WORKING days before launch: Nov 25 (Wed) -> Nov 20 (Fri),
    # skipping the weekend (Sat 21 / Sun 22).
    assert d["reminder_date"] == "2026-11-20"


def test_default_dates_floating_holiday_thanksgiving():
    # 4th Thursday of November 2026 is the 26th.
    d = themes.default_dates("thanksgiving", today=date(2026, 10, 12))
    assert d["expiration_date"] == "2026-11-27"
    assert d["launch_date"] == "2026-10-27"               # 30 days before
    assert d["reminder_date"] == "2026-10-22"             # 3 working days before


def test_default_dates_roll_to_next_year_when_passed():
    # New Year's (Jan 1) has already passed by October, so default to next year.
    d = themes.default_dates("new_years", today=date(2026, 10, 12))
    assert d["expiration_date"] == "2027-01-02"
    assert d["launch_date"] == "2026-12-02"


def test_default_dates_approximate_season_has_dates():
    # Seasons with no true calendar date still get close-enough defaults, not blanks.
    d = themes.default_dates("summer", today=date(2026, 10, 12))
    assert d["launch_date"] and d["expiration_date"] and d["reminder_date"]


def test_default_dates_custom_is_blank():
    d = themes.default_dates("custom", today=date(2026, 10, 12))
    assert d == {"launch_date": "", "expiration_date": "", "reminder_date": ""}


def test_theme_defaults_covers_every_theme():
    defaults = themes.theme_defaults(today=date(2026, 10, 12))
    assert set(defaults) == set(themes.THEMES)
    # Each entry always has all three keys (the form reads them positionally).
    for dates in defaults.values():
        assert set(dates) == {"launch_date", "expiration_date", "reminder_date"}


def test_build_seasonal_edit_prompt_preserves_product_and_carries_scene():
    prompt = themes.build_seasonal_edit_prompt("halloween")
    low = prompt.lower()
    # Forceful product-preservation language (the edit endpoint regenerates pixels).
    assert "exactly as shown" in low
    assert "do not" in low and ("label" in low or "text" in low)
    # The theme scene is woven in, and it asks the model to place + ground the product.
    assert "halloween" in low or "pumpkin" in low
    assert "contact shadow" in low
