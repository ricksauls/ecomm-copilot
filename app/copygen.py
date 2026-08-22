"""AI rewrite of PDP copy for the Copy Content Creation feature.

Given the *current* PDP content (a :class:`~app.scoring.PdpRecord`) and its target
keyword set, ask Claude to rewrite the **Title**, **Description**, and **Key
Features** so they satisfy the same rules the rule-based scorer enforces (length
bands, bullet count, keyword coverage). In other words: generate copy engineered
to score well, grounded in the item's existing content so it never invents specs
or claims.

Design notes:
- **Worker-only.** This makes an external API call that can take several seconds,
  so it must run in the background worker, never inline in a request.
- **Lazy SDK import.** Like :mod:`app.fetch` with Playwright, the ``anthropic``
  SDK is imported inside the call, so importing this module never requires the
  SDK to be installed and unit tests can inject a fake client.
- **Configurable model.** ``COPYGEN_MODEL`` selects the model (default the most
  capable Claude model) so it can be switched — e.g. to a cheaper model for large
  batches — without a code change.
- **Secrets.** The API key is read from ``ANTHROPIC_API_KEY`` by the SDK. It is
  never logged, echoed, or surfaced in an error message (see security-standards).
"""

import json
import logging
import os
from dataclasses import dataclass

from app.scoring import PdpRecord

logger = logging.getLogger(__name__)

# Default when COPYGEN_MODEL is unset. Kept in sync with app.config["COPYGEN_MODEL"].
DEFAULT_MODEL = "claude-opus-5"

# Output ceiling. The generated copy is small (a title, a handful of bullets, and
# a ~150-300 word description) but the model also spends thinking tokens, so this
# leaves comfortable headroom without inviting runaway output.
_MAX_TOKENS = 2500

# The JSON shape the model must return. ``additionalProperties: false`` + a full
# ``required`` list makes the structured-output contract exact, so parsing can't
# silently receive an unexpected shape.
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "bullets": {"type": "array", "items": {"type": "string"}},
        "description": {"type": "string"},
    },
    "required": ["title", "bullets", "description"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are an expert Walmart Marketplace copywriter. You rewrite product detail "
    "page (PDP) content — Title, Description, and Key Features — to maximize search "
    "discoverability and conversion while following Walmart's content guidelines.\n\n"
    "Hard rules:\n"
    "- TITLE: 50-75 characters. Title Case (never ALL CAPS). Lead with the brand, "
    "then the product name, then a key attribute (size/count/variant). Work in 2-3 "
    "of the target keywords naturally.\n"
    "- KEY FEATURES: 3-10 benefit-led bullets, each roughly 30-180 characters. Lead "
    "with the shopper benefit, not just the spec.\n"
    "- DESCRIPTION: 150-300 words, scannable, and it should weave in the top target "
    "keywords naturally (no keyword stuffing).\n\n"
    "Grounding rule (important): base everything on the CURRENT copy provided. Do "
    "not invent specifications, certifications, ingredients, dimensions, or claims "
    "that are not supported by the current content. Improve clarity, structure, "
    "benefit framing, and keyword coverage — do not fabricate facts."
)


class CopyGenError(Exception):
    """Copy generation could not be completed (API error, refusal, or bad output)."""


@dataclass
class GeneratedCopy:
    """The AI-rewritten copy for one PDP."""

    title: str
    bullets: list[str]
    description: str


def resolve_model() -> str:
    """Return the configured copy-generation model (env override, else default)."""
    return os.environ.get("COPYGEN_MODEL") or DEFAULT_MODEL


def _build_user_prompt(record: PdpRecord, keywords: list[str] | None) -> str:
    """Assemble the per-item prompt from the current copy and target keywords."""
    current_bullets = "\n".join(f"- {b}" for b in record.bullets) or "(none)"
    kw = ", ".join(keywords) if keywords else "(none discovered)"
    return (
        "Rewrite the following Walmart PDP copy per the rules.\n\n"
        f"CURRENT TITLE:\n{record.title or '(none)'}\n\n"
        f"CURRENT KEY FEATURES:\n{current_bullets}\n\n"
        f"CURRENT DESCRIPTION:\n{record.description or '(none)'}\n\n"
        f"TARGET KEYWORDS (ranked, use the most relevant naturally):\n{kw}\n\n"
        "Return the improved title, key-feature bullets, and description."
    )


def _get_client():
    """Construct the Anthropic client, importing the SDK lazily.

    Kept separate so tests can monkeypatch it with a fake client and so a missing
    install surfaces as a clear CopyGenError rather than an import error at module
    load. The SDK reads ANTHROPIC_API_KEY from the environment itself.
    """
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - depends on optional install
        raise CopyGenError(
            "The anthropic SDK is not installed. Run `pip install anthropic`."
        ) from e
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # Fail loud and early with an actionable message — but never echo the key
        # (there is nothing to echo here) or any secret.
        raise CopyGenError(
            "ANTHROPIC_API_KEY is not set; cannot generate copy. Set it in the "
            "worker's environment (.env on the droplet)."
        )
    return anthropic.Anthropic()


def _parse_response(response) -> GeneratedCopy:
    """Validate the model response and parse its JSON into a GeneratedCopy."""
    # A safety refusal comes back as HTTP 200 with stop_reason "refusal"; treat it
    # as a hard failure for this item (a refusal on product copy is effectively
    # impossible, but we fail loud rather than persist empty copy).
    if getattr(response, "stop_reason", None) == "refusal":
        raise CopyGenError("The model declined to generate copy for this item.")

    text = next(
        (b.text for b in response.content if getattr(b, "type", None) == "text"),
        None,
    )
    if not text:
        raise CopyGenError("The model returned no text content.")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise CopyGenError(f"Could not parse the model's JSON output: {e}") from e

    title = (data.get("title") or "").strip()
    description = (data.get("description") or "").strip()
    bullets = [b.strip() for b in (data.get("bullets") or []) if isinstance(b, str) and b.strip()]
    if not title or not description or not bullets:
        raise CopyGenError("The generated copy was missing a title, bullets, or description.")
    return GeneratedCopy(title=title, bullets=bullets, description=description)


def generate_copy(
    record: PdpRecord,
    keywords: list[str] | None = None,
    *,
    model: str | None = None,
    client=None,
) -> GeneratedCopy:
    """Generate improved Title/Description/Key Features for one PDP.

    ``client`` is injectable for testing; in production it is the Anthropic SDK
    client (built lazily). ``model`` overrides the configured model. Raises
    :class:`CopyGenError` on any failure so the worker can mark the single item
    failed without crashing the batch.
    """
    model = model or resolve_model()
    client = client or _get_client()
    logger.info(
        "Generating copy item_id=%s model=%s keywords=%d",
        record.item_id, model, len(keywords or []),
    )
    try:
        response = client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(record, keywords)}],
            # Structured output guarantees valid JSON in the text block; medium
            # effort balances copy quality against latency/cost across a batch.
            output_config={
                "effort": "medium",
                "format": {"type": "json_schema", "schema": _OUTPUT_SCHEMA},
            },
        )
    except CopyGenError:
        raise
    except Exception as e:  # noqa: BLE001 - normalize any SDK/transport error
        # Deliberately do not include request internals (which could carry the
        # prompt) beyond the exception's own message; never the API key.
        raise CopyGenError(f"Copy generation request failed: {e}") from e

    generated = _parse_response(response)
    logger.info(
        "Generated copy item_id=%s title_len=%d bullets=%d desc_words=%d",
        record.item_id, len(generated.title), len(generated.bullets),
        len(generated.description.split()),
    )
    return generated
