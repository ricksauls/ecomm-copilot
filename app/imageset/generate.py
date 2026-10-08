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

from app.imageset import compose, storage, templates
from app.imageset import store as isstore
from app.imageset.prompts import build_lifestyle_prompt
from app.imageset.providers.background_removal import get_background_removal_provider
from app.imageset.providers.image_generation import get_image_generation_provider

logger = logging.getLogger(__name__)

# Asset types the slice can generate end-to-end. A run enqueues only these; the
# others get their generators in a later phase.
IMPLEMENTED_TYPES = frozenset({"LIFESTYLE", "SIZE_COMPARISON"})

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


def _gen_lifestyle(project, asset, cutout, logo) -> tuple[bytes, bytes, dict]:
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


def _gen_size_comparison(project, asset, cutout, logo) -> tuple[bytes, None, dict]:
    """Fully programmatic exact-measurement diagram. Returns (final, None, meta)."""
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


_GENERATORS = {
    "LIFESTYLE": _gen_lifestyle,
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
    final_bytes, scene_bytes, meta = generator(project, asset, cutout, logo)

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


def enqueue_project_assets(conn, project, user_id: int, *, only_implemented: bool = True) -> list[int]:
    """Queue generation jobs for a project's assets; return the job ids.

    With ``only_implemented`` (the default for the slice), only asset types with a
    generator are queued, so unbuilt types don't show as failures. Moves the
    project to ``generating``.
    """
    from app.imageset import jobs as isjobs

    job_ids: list[int] = []
    for asset in isstore.assets_for_project(conn, project["id"], user_id):
        if only_implemented and asset["asset_type"] not in IMPLEMENTED_TYPES:
            continue
        jid = isjobs.enqueue_asset_job(
            conn, user_id=user_id, project_id=project["id"], asset_id=asset["id"]
        )
        if jid is not None:
            job_ids.append(jid)
    isstore.set_status(conn, project["id"], isstore.STATUS_GENERATING)
    logger.info("Enqueued %d asset job(s) for project=%s", len(job_ids), project["id"])
    return job_ids
