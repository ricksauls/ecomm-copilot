"""Tests for the seasonal theme presets and the backdrop prompt builder."""

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
    # The lifestyle scaffolding must pin a resting surface (so the product grounds)
    # and forbid the model from drawing the product/text itself.
    assert "resting surface" in prompt.lower()
    assert "do not include the product" in prompt.lower()
    # The theme's scene brief is woven into the prompt.
    assert "christmas" in prompt.lower()


def test_build_seasonal_prompt_custom_uses_user_scene():
    prompt = themes.build_seasonal_prompt("custom", "A Diwali table with marigolds and diyas")
    assert "marigolds" in prompt
