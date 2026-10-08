"""Asset generation orchestrator.

Given a planned asset + its project, this loads the approved cutout (and original)
from storage, dispatches to the generator for the asset's type, writes the outputs
(final 2000×2000 PNG, a gallery thumbnail, and the raw AI scene when there is one),
and updates the asset row. Port of the source app's ``jobs/runner.processAsset``.

The vertical slice implements two asset types end-to-end — **LIFESTYLE** (AI scene
+ composited cutout) and **SIZE_COMPARISON** (fully programmatic diagram). The
remaining types raise :class:`GenerationError` until their generators land, so a
run can enqueue only the implemented ones (see :func:`enqueue_project_assets`).
"""

import json
import logging

from app.imageset import compose, config, iconlib, reference_objects, storage, templates
from app.imageset import store as isstore
from app.imageset.prompts import (
    build_backdrop_prompt,
    build_lifestyle_prompt,
    build_product_in_use_prompt,
    build_size_comparison_prompt,
)
from app.imageset.providers.background_removal import get_background_removal_provider
from app.imageset.providers.image_generation import get_image_generation_provider

logger = logging.getLogger(__name__)

# Asset types the slice can generate end-to-end. A run enqueues only these; the
# others get their generators in a later phase.
IMPLEMENTED_TYPES = frozenset({"LIFESTYLE", "FEATURE_CALLOUT", "PRODUCT_IN_USE", "SIZE_COMPARISON"})

# The cutout-review picker: one entry per generatable asset *variation* so the
# user can pick individual images (e.g. just Lifestyle 1), not whole types. Keys
# are "TYPE:VARIATION" and must match the variations the plan creates
# (``plan.REQUIRED_COMPOSITION``); a test guards the pairing. ``ready`` mirrors
# IMPLEMENTED_TYPES.
_VARIATION_META = (
    ("LIFESTYLE", 1, "Lifestyle 1", "Your product staged in a real-world scene."),
    ("LIFESTYLE", 2, "Lifestyle 2", "A second lifestyle scene, in a different setting."),
    ("FEATURE_CALLOUT", 1, "Feature 1", "Headline + benefit callouts beside the product."),
    ("FEATURE_CALLOUT", 2, "Feature 2", "A second feature-callout, highlighting other benefits."),
    ("PRODUCT_IN_USE", 1, "Product In Use 1", "A person using the product in context."),
    ("PRODUCT_IN_USE", 2, "Product In Use 2", "A second product-in-use scenario."),
    ("SIZE_COMPARISON", 1, "Size Comparison", "Shown next to everyday objects for scale."),
)


def asset_type_choices() -> list[dict]:
    """The cutout-review picker, as display dicts (key/label/description/ready).

    One dict per asset variation; ``key`` is "TYPE:VARIATION". ``ready`` entries
    are checked by default in the template.
    """
    return [
        {"key": f"{t}:{v}", "label": label, "description": desc, "ready": t in IMPLEMENTED_TYPES}
        for t, v, label, desc in _VARIATION_META
    ]

# Lifestyle placement baseline (product ~62% tall, low ground line, soft
# directional cast shadow + shallow depth of field). Tunable per asset later.
LIFESTYLE_DEFAULTS = {
    "scale": 0.62,
    "offset_x": 0.0,
    "offset_y": 0.0,
    "ground_line": 0.82,
    "shadow_strength": 0.6,
}


class GenerationError(Exception):
    """An asset could not be generated (missing input, provider error, or unsupported type)."""


def brand_colors_for(project) -> dict:
    """Resolve a project's brand palette, falling back to the template defaults.

    Stored as a JSON array of hex strings ``[primary, secondary, accent]``; any
    missing slot falls back so the programmatic templates always have a full set.
    """
    try:
        colors = json.loads(project["brand_colors"]) if project["brand_colors"] else []
    except (json.JSONDecodeError, TypeError):
        colors = []
    out = dict(templates.DEFAULT_BRAND)
    for key, value in zip(("primary", "secondary", "accent"), colors):
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
    return out


def resolve_brand_palette(project, cutout=None) -> dict:
    """Brand palette for the size-comparison bars: user override → auto-detect → default.

    If the user entered brand colors on intake, those win. Otherwise, when a cutout
    is available, the product's dominant color is auto-detected and used as the
    primary (the bar color). Falls back to the default palette when neither applies.
    """
    palette = brand_colors_for(project)
    # Did the user set the PRIMARY slot specifically? (Other slots being set must not
    # suppress auto-detect of the bar color.) brand_colors is [primary, secondary, accent].
    user_set_primary = False
    try:
        stored = json.loads(project["brand_colors"]) if project["brand_colors"] else []
        user_set_primary = bool(stored and isinstance(stored[0], str) and stored[0].strip())
    except (json.JSONDecodeError, TypeError, IndexError):
        user_set_primary = False
    if not user_set_primary and cutout is not None:
        detected = compose.detect_dominant_color(cutout)
        if detected:
            palette = dict(palette, primary=detected)
            logger.info("Auto-detected brand color %s for project=%s", detected, project["id"])
    return palette


def _scale_caption(names: list[str]) -> str:
    """Human caption like 'Shown next to a TV remote and a paperback book for scale'."""
    if not names:
        return ""
    phrases = [f"{'an' if n[:1].lower() in 'aeiou' else 'a'} {n}" for n in names]
    if len(phrases) == 1:
        joined = phrases[0]
    elif len(phrases) == 2:
        joined = " and ".join(phrases)
    else:
        joined = ", ".join(phrases[:-1]) + ", and " + phrases[-1]
    return f"Shown next to {joined} for scale"


def _dimensions(project) -> dict:
    """Parse the project's stored dimensions JSON into a plain dict."""
    try:
        return json.loads(project["dimensions_json"]) if project["dimensions_json"] else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _dim_string(dims: dict, key: str) -> str:
    """Human-readable dimension like '7.8 in', or '' when unknown."""
    value, unit = dims.get(key), dims.get("unit", "")
    return f"{value} {unit}".strip() if value is not None else ""


def _gen_lifestyle(conn, project, asset, cutout, logo) -> tuple[bytes, bytes, dict]:
    """AI scene background + composited approved cutout. Returns (final, scene, meta)."""
    category = project["category"] or "product"
    scene_desc = "\n".join(
        s for s in (asset["scene_description"], asset["generation_instructions"]) if s
    ) or f"A realistic environment for {category}."
    dims = _dimensions(project)
    p = LIFESTYLE_DEFAULTS
    # The product's base after a vertical offset; the scene must have a surface at
    # this line (keeps the generated surface aligned with where we ground the cutout).
    contact_line = p["ground_line"] + p["offset_y"] * 0.12

    prompt = build_lifestyle_prompt(
        category=category,
        scene_description=scene_desc,
        product_width=_dim_string(dims, "width"),
        product_height=_dim_string(dims, "height"),
        ground_line=contact_line,
    )
    provider = get_image_generation_provider()
    scene = provider.generate_image(
        prompt=prompt, size="1024x1024", background="opaque",
        context={"project_id": project["id"], "asset_type": "LIFESTYLE"},
    )

    composed = compose.composite_product_on_scene(
        compose.load_image(scene.png), cutout,
        scale=p["scale"], offset_x=p["offset_x"], offset_y=p["offset_y"],
        ground_line=p["ground_line"], shadow_strength=p["shadow_strength"],
        blur_background=True, cast_shadow=True,
    )
    if logo is not None:
        composed = compose.composite_logo(composed, logo, corner="top-left")
    meta = {"provider": scene.provider, "model": scene.model,
            "estimated_cost_usd": scene.estimated_cost_usd, "prompt": prompt}
    return compose.export_image(composed), scene.png, meta


def _gen_size_comparison(conn, project, asset, cutout, logo) -> tuple[bytes, bytes | None, dict]:
    """Scale comparison: AI scene (product + everyday reference objects) + brand bars.

    The product image is sent to the image model, which places one or two familiar
    objects (chosen by real size) beside it at true relative scale on pure white;
    then the exact dimensions + a scale caption are composited into branded header/
    footer bars. Needs a real product height as the scale anchor — without one we
    fall back to the programmatic measurement diagram. Returns (final, scene, meta).
    """
    dims = _dimensions(project)
    unit = dims.get("unit") or "in"
    height = dims.get("height")
    phrases, names = reference_objects.reference_phrases(height, unit, count=2) if height else ([], [])
    if not phrases:
        # No scale anchor (or no distinct reference) → the measurement diagram.
        logger.info("Size-comparison: no scale anchor for project=%s, using diagram", project["id"])
        return _gen_size_comparison_diagram(project, asset, cutout)

    prompt = build_size_comparison_prompt(
        product_name=project["name"] or project["category"] or "the product",
        product_height=f"{height} {unit}".strip(),
        product_width=_dim_string(dims, "width"),
        reference_phrases=phrases,
    )
    provider = get_image_generation_provider()
    scene = provider.edit_image(
        prompt=prompt, image=compose.to_png_bytes(cutout),
        size=f"{config.AI_SCENE_SIZE}x{config.AI_SCENE_SIZE}",
        context={"project_id": project["id"], "asset_type": "SIZE_COMPARISON"},
    )
    final = templates.render_size_comparison_bars(
        scene.png,
        headline=asset["title"] or "See It Next to Everyday Items",
        dimensions_line=templates.format_dimensions_line(dims),
        caption=_scale_caption(names),
        brand=resolve_brand_palette(project, cutout),
    )
    meta = {"provider": scene.provider, "model": scene.model,
            "estimated_cost_usd": scene.estimated_cost_usd, "prompt": prompt}
    return final, scene.png, meta


def _gen_size_comparison_diagram(project, asset, cutout) -> tuple[bytes, None, dict]:
    """Fully programmatic exact-measurement diagram (no AI). Returns (final, None, meta).

    The fallback when a product has no dimensions to anchor a true-scale comparison:
    draws measurement lines/labels from the user-entered facts only.
    """
    dims = _dimensions(project)
    final = templates.create_size_comparison(
        title=asset["title"] or "Exact dimensions",
        dimensions={
            "width": dims.get("width"),
            "height": dims.get("height"),
            "depth": dims.get("depth"),
            "unit": dims.get("unit", ""),
            "weight": dims.get("weight", ""),
        },
        cutout=cutout,
        brand=brand_colors_for(project),
    )
    meta = {"provider": "composition", "model": "size-comparison-v1", "estimated_cost_usd": 0.0}
    return final, None, meta


def _project_environments(project) -> list[str]:
    """The project's intended-use environments (JSON array), or an empty list."""
    try:
        envs = json.loads(project["intended_environments"]) if project["intended_environments"] else []
    except (json.JSONDecodeError, TypeError):
        envs = []
    return [e for e in envs if isinstance(e, str) and e.strip()]


def _gen_product_in_use(conn, project, asset, cutout, logo) -> tuple[bytes, bytes, dict]:
    """AI product-in-use: a forward-facing person using the product. (final, scene, meta).

    Uses the ORIGINAL product photo as the edit reference (per the source app) so the
    model preserves the real packaging while placing it in a hand at true scale.
    """
    dims = _dimensions(project)
    envs = _project_environments(project)
    environment = envs[0] if envs else f"a realistic setting for {project['category'] or 'the product'}"
    name = project["name"] or project["category"] or "the product"
    prompt = build_product_in_use_prompt(
        product_name=name,
        usage_scenario=asset["usage_scenario"] or f"A person using {name} as directed.",
        environment=environment,
        product_height=_dim_string(dims, "height"),
        product_width=_dim_string(dims, "width"),
        weight=dims.get("weight", ""),
        extra_instructions=asset["generation_instructions"] or "",
    )
    # The original (opaque) photo is the best edit reference; fall back to the cutout.
    reference = storage.load(project["original_path"]) or compose.to_png_bytes(cutout)
    provider = get_image_generation_provider()
    edited = provider.edit_image(
        prompt=prompt, image=reference,
        size=f"{config.AI_SCENE_SIZE}x{config.AI_SCENE_SIZE}",
        context={"project_id": project["id"], "asset_type": "PRODUCT_IN_USE"},
    )
    canvas = compose.cover_scene(compose.load_image(edited.png))
    if logo is not None:
        canvas = compose.composite_logo(canvas, logo, corner="top-right")
    meta = {"provider": edited.provider, "model": edited.model,
            "estimated_cost_usd": edited.estimated_cost_usd, "prompt": prompt}
    return compose.export_image(canvas), edited.png, meta


def _features_for_asset(conn, project, asset) -> list[dict]:
    """The approved features assigned to a callout asset (title/description dicts).

    Reads the asset's ``assigned_feature_ids`` (feature_key values) and resolves
    them against the project's features; falls back to the first few features when
    none are assigned, so a callout always has something to show.
    """
    try:
        ids = json.loads(asset["assigned_feature_ids"]) if asset["assigned_feature_ids"] else []
    except (json.JSONDecodeError, TypeError):
        ids = []
    by_key = {r["feature_key"]: r for r in isstore.features_for_project(conn, project["id"])}
    chosen = [by_key[k] for k in ids if k in by_key] or list(by_key.values())[:3]
    # Each row carries an inferred icon (from the feature's title/type).
    return [{"title": r["title"], "description": r["description"],
             "icon": iconlib.infer_icon(r["title"], r["feature_type"])} for r in chosen]


def _gen_feature_callout(conn, project, asset, cutout, logo) -> tuple[bytes, bytes, dict]:
    """Feature-callout: icon/headline/divider/benefit rows over a blurred AI backdrop.

    The copy + icons + layout are all programmatic (approved feature text only, no
    AI-invented claims); only the out-of-focus photographic backdrop is AI-generated.
    Returns (final, backdrop, meta).
    """
    features = _features_for_asset(conn, project, asset)
    envs = _project_environments(project)
    prompt = build_backdrop_prompt(
        category=project["category"] or "product",
        environment=envs[0] if envs else None,
    )
    provider = get_image_generation_provider()
    backdrop = provider.generate_image(
        prompt=prompt, size=f"{config.AI_SCENE_SIZE}x{config.AI_SCENE_SIZE}", background="opaque",
        context={"project_id": project["id"], "asset_type": "FEATURE_CALLOUT"},
    )
    final = templates.create_feature_callout(
        features=features, cutout=cutout,
        brand=resolve_brand_palette(project, cutout),
        layout=asset["layout_style"] or "product-left",
        backdrop_png=backdrop.png,
    )
    meta = {"provider": backdrop.provider, "model": backdrop.model,
            "estimated_cost_usd": backdrop.estimated_cost_usd, "prompt": prompt}
    return final, backdrop.png, meta


_GENERATORS = {
    "LIFESTYLE": _gen_lifestyle,
    "FEATURE_CALLOUT": _gen_feature_callout,
    "PRODUCT_IN_USE": _gen_product_in_use,
    "SIZE_COMPARISON": _gen_size_comparison,
}


def process_asset(conn, asset, project) -> None:
    """Generate one asset's images and persist them. Raises on failure.

    The caller (the worker) catches :class:`GenerationError` / unexpected errors and
    marks the job + asset failed, so one bad asset never sinks the run.
    """
    asset_id = asset["id"]
    asset_type = asset["asset_type"]
    generator = _GENERATORS.get(asset_type)
    if generator is None:
        raise GenerationError(f"Asset type {asset_type} is not implemented yet")

    isstore.update_asset(conn, asset_id, status="generating")

    cutout_bytes = storage.load(project["cutout_path"])
    if not cutout_bytes:
        raise GenerationError("The approved product cutout is missing")
    cutout = compose.load_image(cutout_bytes)
    logo_bytes = storage.load(project["logo_path"])
    logo = compose.load_image(logo_bytes) if logo_bytes else None

    logger.info("Generating asset id=%s type=%s project=%s", asset_id, asset_type, project["id"])
    final_bytes, scene_bytes, meta = generator(conn, project, asset, cutout, logo)

    pid = project["id"]
    scene_path = storage.save(pid, "scene", asset_id, scene_bytes) if scene_bytes else None
    final_path = storage.save(pid, "final", asset_id, final_bytes)
    thumb_path = storage.save(
        pid, "thumb", asset_id,
        compose.to_png_bytes(compose.create_thumbnail(compose.load_image(final_bytes))),
    )
    if not final_path or not thumb_path:
        raise GenerationError("Could not store the generated image")

    isstore.update_asset(
        conn, asset_id, status="ready",
        final_path=final_path, thumb_path=thumb_path, scene_path=scene_path,
        review_json=json.dumps(meta),
    )
    logger.info("Asset ready id=%s type=%s provider=%s", asset_id, asset_type, meta.get("provider"))


def run_cutout(conn, project, user_id: int) -> str | None:
    """Produce the transparent cutout from the uploaded original. Returns its rel path.

    Runs the background-removal provider on the stored original and records the
    cutout (awaiting human approval). Raises :class:`GenerationError` if there is no
    original or the provider fails loudly.
    """
    original_bytes = storage.load(project["original_path"])
    if not original_bytes:
        raise GenerationError("No uploaded product image to cut out")
    provider = get_background_removal_provider()
    result = provider.remove_background(data=original_bytes, content_type="image/png")
    rel = storage.save(project["id"], "cutout", "cutout", result.png)
    if not rel:
        raise GenerationError("Could not store the product cutout")
    isstore.set_cutout(conn, project["id"], rel)
    logger.info("Cutout ready for project=%s provider=%s", project["id"], result.provider)
    return rel


def _download_product_image(url: str) -> bytes | None:
    """Download a Walmart main image and normalize it to PNG bytes. Best-effort.

    Returns None on any failure (network, decode) — a missing fetched image just
    means the user uploads one; it never fails the prefill. Walmart's image CDN
    serves plain HTTP (no browser needed), same as the CI image cache.
    """
    if not url:
        return None
    try:
        import io

        import requests
        from PIL import Image

        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        # Flatten to RGB on white (Walmart mains are white-bg) and re-encode as PNG
        # so the stored original is a predictable format for the cutout step.
        img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001 - a bad image just means the user uploads one
        logger.warning("Could not fetch product image url=%s: %s", (url or "")[:80], e)
        return None


# How many feature callouts a prefill produces (3 per feature image × 2 images).
_PREFILL_CALLOUT_COUNT = 6


def run_prefill(conn, project, *, fetch=None, callouts=None) -> None:
    """Fetch a Walmart PDP and prefill the draft project. Raises on fetch failure.

    Fills name/brand/description; turns each PDP bullet into a feature callout — a
    tight AI-generated headline (the feature title) plus its benefit line (the
    "Short detail"); and stores the main image as the (replaceable) product photo
    without advancing the status, so the user reviews the prefilled form next.
    When the callout model is unavailable the raw bullet becomes the title with no
    benefit (best-effort). ``fetch``/``callouts`` are injectable for tests; in
    production they are :func:`app.fetch.fetch_pdp` (headed-Chrome, worker-only)
    and :func:`app.imageset.benefits.suggest_callouts`. The caller marks the fetch
    finished/failed.
    """
    from app import pdp as pdp_mod
    from app.imageset import benefits as benefits_mod

    url = project["source_url"]
    if not url:
        raise GenerationError("This project has no product URL to fetch")
    if fetch is None:
        from app.fetch import fetch_pdp
        fetch = fetch_pdp
    suggest = callouts or benefits_mod.suggest_callouts

    record = fetch(url, pdp_mod.item_number_from_url(url))
    isstore.apply_fetched_record(
        conn, project["id"], name=record.title, brand=record.brand,
        description=record.description,
    )

    # Produce a fixed set of feature callouts (headline + benefit) from the bullets,
    # topping up from the description so we reach the target even when there are
    # fewer bullets. Fall back to the raw bullets as titles if the model is down.
    bullets = [b for b in (record.bullets or []) if b and b.strip()][:12]
    suggested = (
        suggest(bullets, count=_PREFILL_CALLOUT_COUNT, description=record.description,
                product_name=record.title, brand=record.brand)
        if (bullets or (record.description or "").strip()) else []
    )
    items = [{"title": c["headline"], "description": c["benefit"]}
             for c in suggested if c.get("headline") or c.get("benefit")]
    if not items:  # model unavailable → raw bullets as titles, no benefit
        items = [{"title": b, "description": ""} for b in bullets[:_PREFILL_CALLOUT_COUNT]]
    isstore.set_features(conn, project["id"], items)

    data = _download_product_image(record.main_image_url)
    if data:
        rel = storage.save(project["id"], "original", "product", data)
        if rel:
            isstore.set_original_path(conn, project["id"], rel)
    logger.info("Prefilled project=%s from %s (features=%d, callouts=%d, image=%s)",
                project["id"], (url or "")[:60], len(bullets),
                sum(1 for c in suggested if c.get("benefit")), bool(data))


def _selected_types(project) -> frozenset[str] | None:
    """The asset types the user chose on the cutout screen, or None for 'all'.

    Stored as a JSON array in ``selected_types``; NULL/absent/malformed means the
    user made no explicit choice, so we fall back to all implemented types.
    """
    raw = project["selected_types"] if "selected_types" in project.keys() else None
    if not raw:
        return None
    try:
        picked = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("Ignoring malformed selected_types for project=%s", project["id"])
        return None
    return frozenset(str(t) for t in picked) if isinstance(picked, list) and picked else None


def enqueue_project_assets(conn, project, user_id: int, *, only_implemented: bool = True) -> list[int]:
    """Queue generation jobs for a project's assets; return the job ids.

    With ``only_implemented`` (the default for the slice), only asset types with a
    generator are queued, so unbuilt types don't show as failures. When the user
    picked a subset on the cutout screen (``selected_types``), only those types are
    queued — so they pay to generate only what they asked for. Moves the project to
    ``generating``.
    """
    from app.imageset import jobs as isjobs

    chosen = _selected_types(project)
    job_ids: list[int] = []
    for asset in isstore.assets_for_project(conn, project["id"], user_id):
        if only_implemented and asset["asset_type"] not in IMPLEMENTED_TYPES:
            continue
        # Selection is per-variation ("TYPE:VARIATION"); accept a bare "TYPE" too
        # for backward compatibility with any pre-per-variation selection.
        if chosen is not None:
            key = f'{asset["asset_type"]}:{asset["variation_number"]}'
            if key not in chosen and asset["asset_type"] not in chosen:
                continue
        jid = isjobs.enqueue_asset_job(
            conn, user_id=user_id, project_id=project["id"], asset_id=asset["id"]
        )
        if jid is not None:
            job_ids.append(jid)
    isstore.set_status(conn, project["id"], isstore.STATUS_GENERATING)
    logger.info("Enqueued %d asset job(s) for project=%s (selected_types=%s)",
                len(job_ids), project["id"], sorted(chosen) if chosen else "all")
    return job_ids
