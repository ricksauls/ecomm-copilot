"""Suggest feature-image callouts (headline + benefit) from a product's features.

For each approved key feature this produces a marketing callout as it appears on a
feature image: a tight **headline** (2–4 words naming the feature) above a
separator, and a short **benefit** line beneath it stating what the feature does
for the shopper — e.g. ``Long Lasting Protection`` / ``Repels mosquitoes for up to
8 hours``. Up to three callouts share one image, so both parts stay brief.
Prefill seeds the feature **title** from the headline and the "Potential Feature
Image Short Detail" from the benefit. Uses the app's existing Anthropic/Claude
integration (the same pattern as :mod:`app.copygen`) on the worker during prefill.

Best-effort by design: it is **inert without ``ANTHROPIC_API_KEY``** and any
failure returns no suggestions, so a hiccup never fails the prefill — the caller
then falls back to the raw bullet as the title with no benefit line. The model is
told to ground each callout in the given feature only, so it can't invent a spec
or claim. The key is read from the environment by the SDK and never logged.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# The feature title + "Short detail" fields both cap at 120 chars (form + store);
# these are the hard safety limits. The targets below keep callouts image-ready.
MAX_HEADLINE_CHARS = 60
MAX_BENEFIT_CHARS = 120
# A headline is a 2–4 word title; the benefit is one short line. Three callouts
# share an image, so both stay brief. These are soft targets; MAX is the hard cap.
TARGET_HEADLINE_CHARS = 28
TARGET_BENEFIT_CHARS = 55
DEFAULT_MODEL = "claude-opus-5"
_MAX_TOKENS = 1500

_SYSTEM_PROMPT = (
    "You write product-image feature callouts — a tight feature headline and the short "
    "benefit line beneath it. You output only valid JSON. Base each callout ONLY on the "
    "feature given — never invent specifications, durations, certifications, ingredients, "
    "dimensions, or claims."
)

# Structured-output contract: one {headline, benefit} object per input feature, in order.
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "callouts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "benefit": {"type": "string"},
                },
                "required": ["headline", "benefit"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["callouts"],
    "additionalProperties": False,
}


def is_configured() -> bool:
    """True when the Anthropic key is present (callout suggestion is live)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def resolve_model() -> str:
    """Callout model: a dedicated override, else the copy model, else the default."""
    return (
        os.environ.get("IMAGESET_BENEFITS_MODEL")
        or os.environ.get("COPYGEN_MODEL")
        or DEFAULT_MODEL
    )


def _get_client():
    """Construct the Anthropic client lazily (same pattern as app.copygen)."""
    import anthropic  # local import keeps the module import cheap / SDK optional

    return anthropic.Anthropic()


def _build_prompt(features: list[str], *, product_name: str, brand: str, category: str) -> str:
    numbered = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(features))
    return (
        "Each item below is a key feature taken from a product listing (it may be a short "
        "phrase or a full sentence). For EACH feature create one feature-image callout with "
        "two parts:\n"
        "- headline: a tight 2–4 word title naming the feature (Title Case).\n"
        "- benefit: one short line beneath it stating the shopper benefit the feature "
        "provides.\n\n"
        "Style (how it should read — do NOT copy these words or their specifics):\n"
        "  feature 'Repels mosquitoes for up to 8 hours' -> headline 'Long Lasting "
        "Protection', benefit 'Repels mosquitoes for up to 8 hours'\n"
        "  feature 'EvenSpray technology for even coverage' -> headline 'EvenSpray "
        "Technology', benefit 'Delivers an even, consistent spray'\n\n"
        "Rules:\n"
        f"- headline: about {TARGET_HEADLINE_CHARS} characters (2–4 words), never exceed "
        f"{MAX_HEADLINE_CHARS}.\n"
        f"- benefit: about {TARGET_BENEFIT_CHARS} characters, never exceed "
        f"{MAX_BENEFIT_CHARS}; state the benefit, don't just repeat the headline.\n"
        "- No surrounding quotes.\n"
        "- Base both parts ONLY on the given feature and product; never invent durations, "
        "diseases, certifications, percentages, or other specifics not present in the "
        "feature.\n"
        "Return JSON { \"callouts\": [{\"headline\": ..., \"benefit\": ...}, ...] } with "
        "exactly one entry per feature, in order.\n\n"
        f"Product: {product_name or '(unnamed)'}"
        f"{' by ' + brand if brand else ''}"
        f"{' (' + category + ')' if category else ''}\n\n"
        f"Features:\n{numbered}"
    )


def suggest_callouts(
    features: list[str],
    *,
    product_name: str = "",
    brand: str = "",
    category: str = "",
    model: str | None = None,
    client=None,
) -> list[dict]:
    """Return a ``{"headline", "benefit"}`` callout per feature (by index), or ``[]``.

    Inert without a key or SDK, and any error returns ``[]`` — the caller then
    falls back to the raw feature as the title with no benefit. ``client`` is
    injectable for tests. The result is padded/truncated to match ``features``
    length; each part is capped at its hard limit.
    """
    features = [f.strip() for f in features if f and f.strip()]
    if not features:
        return []
    # Inert unless a client is injected (tests) or a key is configured.
    if client is None and not is_configured():
        return []

    model = model or resolve_model()
    try:
        client = client or _get_client()
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_prompt(
                features, product_name=product_name, brand=brand, category=category)}],
            output_config={
                "effort": "low",  # short lines — minimize latency/cost on the prefill path
                "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            },
        )
    except Exception as e:  # noqa: BLE001 - best-effort; callouts must never fail prefill
        logger.warning("Callout suggestion failed: %s", e)
        return []

    if getattr(response, "stop_reason", None) == "refusal":
        return []
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if not text:
        return []
    try:
        raw = (json.loads(text) or {}).get("callouts") or []
    except (json.JSONDecodeError, TypeError):
        return []

    callouts = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        headline = str(c.get("headline") or "").strip().strip('"')[:MAX_HEADLINE_CHARS]
        benefit = str(c.get("benefit") or "").strip().strip('"')[:MAX_BENEFIT_CHARS]
        callouts.append({"headline": headline, "benefit": benefit})
    # Align to the feature list: one callout per feature, extras dropped, gaps blank.
    callouts = callouts[: len(features)]
    callouts += [{"headline": "", "benefit": ""}] * (len(features) - len(callouts))
    logger.info(
        "Suggested %d callout(s) for %d feature(s)",
        sum(1 for c in callouts if c["benefit"] or c["headline"]), len(features),
    )
    return callouts
