"""Creative plan for the image-set pipeline.

The plan proposes exactly seven assets (2 lifestyle, 2 feature-callout, 2
product-in-use, 1 size-comparison) from the approved product facts. It is the
text-planning step the source app ran on ``gpt-4o-mini``; here
it runs on **Claude** (reusing the app's existing Anthropic integration — see
:mod:`app.copygen`), with a deterministic **mock** planner so the pipeline runs
offline with no key.

Correctness rule carried over from the source: the planner may only reference
**approved** feature ids. Any id the model invents is dropped before validation,
so a plan can never introduce a feature (and therefore a factual claim) that
isn't real. The provider is selected by ``IMAGESET_PLAN_PROVIDER`` (default mock).
"""

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default Claude model for planning; overridable so a cheaper model can be used
# for the planning call without a code change.
DEFAULT_MODEL = "claude-opus-5"
# The plan is 7 assets each with scene/usage/instruction text, so the JSON is
# large; with medium effort (thinking tokens) a small budget truncates the output
# mid-string and the JSON won't parse. Keep this generous.
_MAX_TOKENS = 8000

ASSET_TYPES = ("LIFESTYLE", "FEATURE_CALLOUT", "PRODUCT_IN_USE", "SIZE_COMPARISON")
LAYOUT_STYLES = ("product-left", "product-center", "product-right")

# A feature callout is a product shown beside a column of icon → headline → divider
# → benefit rows, so it only reads correctly in a side-by-side layout. The centered
# layout stacks the product above the rows and squeezes them into a bottom band,
# which looks wrong for this asset type — so reserve "product-center" for the
# LIFESTYLE / SIZE_COMPARISON scenes and keep feature callouts on a side layout.
_SIDE_ONLY_ASSET_TYPES = frozenset({"FEATURE_CALLOUT"})

# The exact package composition every plan must contain (7 assets total).
REQUIRED_COMPOSITION = {
    "LIFESTYLE": 2,
    "FEATURE_CALLOUT": 2,
    "PRODUCT_IN_USE": 2,
    "SIZE_COMPARISON": 1,
}

# Field length caps mirror the source app's schema; they bound untrusted model
# output so an overlong field can't bloat storage or later rendering.
_MAX_TITLE = 90
_MAX_SCENE = 700
_MAX_USAGE = 500
_MAX_INSTRUCTIONS = 700
_MAX_FEATURE_IDS = 6

_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "assetType": {"type": "string", "enum": list(ASSET_TYPES)},
                    "variationNumber": {"type": "integer"},
                    "title": {"type": "string"},
                    "sceneDescription": {"type": "string"},
                    "usageScenario": {"type": "string"},
                    "assignedFeatureIds": {"type": "array", "items": {"type": "string"}},
                    "layoutStyle": {"type": "string", "enum": list(LAYOUT_STYLES)},
                    "generationInstructions": {"type": "string"},
                },
                "required": [
                    "assetType", "variationNumber", "title", "sceneDescription",
                    "usageScenario", "assignedFeatureIds", "layoutStyle",
                    "generationInstructions",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = "You output only valid JSON. Never invent product facts."


class PlanError(Exception):
    """A creative plan could not be produced or failed validation."""


@dataclass
class Feature:
    """One approved product feature the planner may reference."""

    id: str
    title: str
    feature_type: str = ""


@dataclass
class PlanProductContext:
    """Approved product facts supplied to the planner (the only inputs it sees)."""

    name: str
    brand: str
    category: str
    description: str = ""
    target_audience: str = ""
    intended_environments: list[str] = field(default_factory=list)
    directions: str = ""
    has_dimensions: bool = False
    features: list[Feature] = field(default_factory=list)


@dataclass
class PlanItem:
    """One proposed asset in the creative plan."""

    asset_type: str
    variation_number: int
    title: str
    scene_description: str = ""
    usage_scenario: str = ""
    assigned_feature_ids: list[str] = field(default_factory=list)
    layout_style: str = "product-left"
    generation_instructions: str = ""


@dataclass
class CreativePlan:
    """A validated eight-asset creative plan."""

    items: list[PlanItem]


def resolve_model() -> str:
    """Return the configured planning model (env override, else default)."""
    return os.environ.get("IMAGESET_PLAN_MODEL") or DEFAULT_MODEL


def build_creative_plan_prompt(ctx: PlanProductContext) -> str:
    """Build the planner prompt. The model returns JSON validated downstream."""
    feature_list = "\n".join(
        f"- id={f.id} | {f.title} ({f.feature_type})" for f in ctx.features
    )
    return (
        "You are a marketplace creative director. Return ONLY JSON matching the schema.\n\n"
        "Given the approved product information below, propose EXACTLY:\n"
        "- Two distinct LIFESTYLE scenes (variationNumber 1 and 2)\n"
        "- Two FEATURE_CALLOUT concepts (variationNumber 1 and 2)\n"
        "- Two distinct PRODUCT_IN_USE concepts (variationNumber 1 and 2)\n"
        "- One SIZE_COMPARISON concept (variationNumber 1)\n\n"
        "Rules:\n"
        "- Only use approved facts. Do NOT invent dimensions, claims, ingredients, durations, "
        "certifications, safety information, or comparisons.\n"
        "- The two LIFESTYLE scenes must use meaningfully different environments.\n"
        "- The two PRODUCT_IN_USE concepts must be meaningfully different usage scenarios.\n"
        "- FEATURE_CALLOUT items must set assignedFeatureIds to 2-3 approved feature ids; the "
        "two callouts should not unnecessarily repeat features.\n"
        "- FEATURE_CALLOUT layoutStyle MUST be \"product-left\" or \"product-right\" (the product "
        "sits beside the column of feature rows); NEVER \"product-center\" for a callout. Give the "
        "two callouts opposite sides so they mirror each other.\n"
        "- Each asset has ONE primary communication objective. Keep copy brief for mobile.\n"
        "- assignedFeatureIds MUST be chosen only from the ids listed below.\n\n"
        "Product:\n"
        f"name: {ctx.name}\n"
        f"brand: {ctx.brand}\n"
        f"category: {ctx.category}\n"
        f"description: {ctx.description or '(none)'}\n"
        f"targetAudience: {ctx.target_audience or '(none)'}\n"
        f"intendedEnvironments: {', '.join(ctx.intended_environments) or '(none)'}\n"
        f"directions: {ctx.directions or '(none)'}\n"
        f"hasDimensions: {str(ctx.has_dimensions).lower()}\n\n"
        "Approved features:\n"
        f"{feature_list or '(none provided)'}\n\n"
        'Return JSON of shape: { "items": PlanItem[] } where PlanItem = {\n'
        "  assetType, variationNumber, title, sceneDescription, usageScenario,\n"
        '  assignedFeatureIds, layoutStyle ("product-left"|"product-center"|"product-right"),\n'
        "  generationInstructions\n}."
    )


def _resolve_layout(asset_type: str, raw_layout, variation: int) -> str:
    """Return a valid, renderable ``layout_style`` for this asset.

    An unrecognized value falls back to the package default (``product-left``).
    For asset types that can't use the centered layout (see
    ``_SIDE_ONLY_ASSET_TYPES``), a ``product-center`` choice is redirected to a
    side layout, alternating by variation so the two feature callouts mirror each
    other (variation 1 → product on the right, variation 2 → product on the left).
    """
    layout = raw_layout if raw_layout in LAYOUT_STYLES else "product-left"
    if layout == "product-center" and asset_type in _SIDE_ONLY_ASSET_TYPES:
        layout = "product-left" if variation == 2 else "product-right"
    return layout


def _parse_item(raw: dict, valid_ids: set[str]) -> PlanItem:
    """Parse and bound one plan item, dropping any hallucinated feature ids."""
    asset_type = raw.get("assetType")
    if asset_type not in ASSET_TYPES:
        raise PlanError(f"Plan item has invalid assetType {asset_type!r}")
    try:
        variation = int(raw.get("variationNumber", 1))
    except (TypeError, ValueError):
        variation = 1
    variation = max(1, min(2, variation))
    # Keep only approved ids so the model can never introduce a non-existent fact.
    ids = [i for i in (raw.get("assignedFeatureIds") or []) if isinstance(i, str) and i in valid_ids]
    return PlanItem(
        asset_type=asset_type,
        variation_number=variation,
        title=(raw.get("title") or "").strip()[:_MAX_TITLE],
        scene_description=(raw.get("sceneDescription") or "").strip()[:_MAX_SCENE],
        usage_scenario=(raw.get("usageScenario") or "").strip()[:_MAX_USAGE],
        assigned_feature_ids=ids[:_MAX_FEATURE_IDS],
        layout_style=_resolve_layout(asset_type, raw.get("layoutStyle"), variation),
        generation_instructions=(raw.get("generationInstructions") or "").strip()[:_MAX_INSTRUCTIONS],
    )


def _validate_and_parse(obj: dict, valid_ids: set[str]) -> CreativePlan:
    """Validate the raw plan object and build a :class:`CreativePlan`.

    Enforces the exact required composition (``REQUIRED_COMPOSITION``) so
    downstream generation can rely on it.
    """
    required_total = sum(REQUIRED_COMPOSITION.values())
    raw_items = obj.get("items")
    if not isinstance(raw_items, list) or len(raw_items) != required_total:
        raise PlanError(
            f"Creative plan must contain exactly {required_total} items, "
            f"got {len(raw_items or [])}")
    items = [_parse_item(it if isinstance(it, dict) else {}, valid_ids) for it in raw_items]

    counts: dict[str, int] = {}
    for it in items:
        counts[it.asset_type] = counts.get(it.asset_type, 0) + 1
    for asset_type, expected in REQUIRED_COMPOSITION.items():
        if counts.get(asset_type, 0) != expected:
            raise PlanError(
                f"Expected {expected} {asset_type} item(s), got {counts.get(asset_type, 0)}"
            )
    return CreativePlan(items=items)


def _get_client():
    """Construct the Anthropic client lazily (same pattern as app.copygen)."""
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise PlanError("The anthropic SDK is not installed. Run `pip install anthropic`.") from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise PlanError(
            "ANTHROPIC_API_KEY is not set; cannot plan the image set. Set it in the "
            "worker's environment (.env on the droplet)."
        )
    return anthropic.Anthropic()


def _claude_plan(ctx: PlanProductContext, *, model: str | None = None, client=None) -> CreativePlan:
    """Ask Claude for the creative plan and validate the structured output."""
    model = model or resolve_model()
    client = client or _get_client()
    valid_ids = {f.id for f in ctx.features}
    logger.info("Planning image set product=%r model=%s features=%d", ctx.name, model, len(valid_ids))
    try:
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": build_creative_plan_prompt(ctx)}],
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            },
        )
    except PlanError:
        raise
    except Exception as e:  # noqa: BLE001 - normalize any SDK/transport error
        raise PlanError(f"Creative plan request failed: {e}") from e

    if getattr(response, "stop_reason", None) == "refusal":
        raise PlanError("The model declined to plan this image set.")
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if not text:
        raise PlanError("The model returned no plan content.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise PlanError(f"Could not parse the plan JSON: {e}") from e
    plan = _validate_and_parse(data, valid_ids)
    logger.info("Planned image set product=%r items=%d", ctx.name, len(plan.items))
    return plan


def mock_plan(ctx: PlanProductContext) -> CreativePlan:
    """Deterministic offline plan: the required composition from the context.

    No model call — assigns approved features round-robin and derives scene text
    from the category/environments, so the full pipeline runs with no key and
    tests stay stable.
    """
    envs = ctx.intended_environments or [f"a setting where {ctx.category} is used"]
    feat_ids = [f.id for f in ctx.features]

    def pick(start: int, count: int) -> list[str]:
        return [feat_ids[(start + i) % len(feat_ids)] for i in range(min(count, len(feat_ids)))] if feat_ids else []

    items = [
        PlanItem("LIFESTYLE", 1, f"{ctx.name} in {envs[0]}",
                 scene_description=f"{ctx.name} shown in {envs[0]}.", layout_style="product-center"),
        PlanItem("LIFESTYLE", 2, f"{ctx.name} in {envs[-1] if len(envs) > 1 else envs[0]}",
                 scene_description=f"{ctx.name} shown in {envs[-1] if len(envs) > 1 else envs[0]}.",
                 layout_style="product-left"),
        PlanItem("FEATURE_CALLOUT", 1, f"Why {ctx.brand} {ctx.name}",
                 assigned_feature_ids=pick(0, 3), layout_style="product-right"),
        PlanItem("FEATURE_CALLOUT", 2, f"{ctx.name} highlights",
                 assigned_feature_ids=pick(3, 3), layout_style="product-left"),
        PlanItem("PRODUCT_IN_USE", 1, f"Using {ctx.name}",
                 usage_scenario=f"A person using {ctx.name} in {envs[0]}."),
        PlanItem("PRODUCT_IN_USE", 2, f"{ctx.name} at work",
                 usage_scenario=f"A person using {ctx.name} in {envs[-1] if len(envs) > 1 else envs[0]}."),
        PlanItem("SIZE_COMPARISON", 1, f"{ctx.name} size", layout_style="product-center"),
    ]
    logger.debug("mock plan product=%r items=%d", ctx.name, len(items))
    return CreativePlan(items=items)


def plan_provider() -> str:
    """Configured planning provider ("mock" default, or "claude")."""
    return (os.environ.get("IMAGESET_PLAN_PROVIDER") or "mock").strip().lower()


def generate_plan(ctx: PlanProductContext, *, model: str | None = None, client=None) -> CreativePlan:
    """Produce the creative plan via the configured provider.

    Uses Claude when ``IMAGESET_PLAN_PROVIDER=claude``; otherwise the deterministic
    mock. A Claude failure (API error, truncated/invalid JSON, bad composition)
    falls back to the built-in plan rather than failing the whole run, so Approve
    never hard-fails on a planning hiccup. ``client``/``model`` are injectable for
    testing the Claude path.
    """
    if plan_provider() == "claude":
        try:
            return _claude_plan(ctx, model=model, client=client)
        except PlanError as e:
            logger.warning("Claude plan failed (%s); falling back to the built-in plan", e)
            return mock_plan(ctx)
    return mock_plan(ctx)
