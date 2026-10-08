"""Suggest short Feature:Benefit lines from a product's key features.

Expands each approved key feature into one concise, shopper-facing benefit line
(<= 120 characters) to prefill the "Potential Feature Image Short Detail" field.
Uses the app's existing Anthropic/Claude integration (the same pattern as
:mod:`app.copygen`) and runs on the worker during URL prefill.

Best-effort by design: it is **inert without ``ANTHROPIC_API_KEY``** and any
failure returns no suggestions, so a benefit hiccup never fails the prefill — the
feature titles are still filled, just without the benefit line. The model is told
to ground each benefit in the given feature only, so it can't invent a spec or
claim. The key is read from the environment by the SDK and never logged.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# The "Short detail" field caps at 120 chars (form + store); keep suggestions within it.
MAX_BENEFIT_CHARS = 120
DEFAULT_MODEL = "claude-opus-5"
_MAX_TOKENS = 1200

_SYSTEM_PROMPT = (
    "You write concise e-commerce feature benefits. You output only valid JSON. "
    "Base each benefit ONLY on the feature given — never invent specifications, "
    "certifications, ingredients, dimensions, or claims."
)

# Structured-output contract: one benefit string per input feature, in order.
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"benefits": {"type": "array", "items": {"type": "string"}}},
    "required": ["benefits"],
    "additionalProperties": False,
}


def is_configured() -> bool:
    """True when the Anthropic key is present (benefit suggestion is live)."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def resolve_model() -> str:
    """Benefit model: a dedicated override, else the copy model, else the default."""
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
        "Write ONE short shopper-facing benefit line for each product feature below.\n"
        f"Each benefit must be at most {MAX_BENEFIT_CHARS} characters, lead with the "
        "shopper benefit (not the spec), and carry no surrounding quotes.\n"
        "Return JSON { \"benefits\": [...] } with exactly one entry per feature, in the "
        "same order.\n\n"
        f"Product: {product_name or '(unnamed)'}"
        f"{' by ' + brand if brand else ''}"
        f"{' (' + category + ')' if category else ''}\n\n"
        f"Features:\n{numbered}"
    )


def suggest_benefits(
    features: list[str],
    *,
    product_name: str = "",
    brand: str = "",
    category: str = "",
    model: str | None = None,
    client=None,
) -> list[str]:
    """Return a ≤120-char benefit per feature (aligned by index), or ``[]``.

    Inert without a key or SDK, and any error returns ``[]`` — the caller then
    keeps the feature titles with no benefit line. ``client`` is injectable for
    tests. The returned list is padded/truncated to match ``features`` length.
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
    except Exception as e:  # noqa: BLE001 - best-effort; benefits must never fail prefill
        logger.warning("Benefit suggestion failed: %s", e)
        return []

    if getattr(response, "stop_reason", None) == "refusal":
        return []
    text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), None)
    if not text:
        return []
    try:
        raw = (json.loads(text) or {}).get("benefits") or []
    except (json.JSONDecodeError, TypeError):
        return []

    benefits = [str(b).strip().strip('"')[:MAX_BENEFIT_CHARS] for b in raw if isinstance(b, str)]
    # Align to the feature list: one benefit per feature, extras dropped, gaps blank.
    benefits = benefits[: len(features)]
    benefits += [""] * (len(features) - len(benefits))
    logger.info("Suggested %d benefit(s) for %d feature(s)", sum(1 for b in benefits if b), len(features))
    return benefits
