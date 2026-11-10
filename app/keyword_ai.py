"""AI selection of a managed item's tracking keywords.

Given what we know about a product — brand, title, size, and leaf category — ask
OpenAI for the handful of real **shopper search terms** someone would type into
Walmart to find it. These become the item's tracking keywords (the terms the
daily rank / share-of-search sweep is measured against), replacing the older
mechanical defaults (brand / brand+product+size / category).

Design notes (mirrors :mod:`app.copygen` and :mod:`app.imageset.providers.image_generation`):
- **Worker-only.** This is an external HTTP call that can take a second or two, so
  it runs in the background descriptors backfill, never inline in a web request.
- **Dependency-light.** Called over HTTPS with ``requests`` (imported lazily), the
  same style as the image provider — no new SDK is pulled in.
- **Fail-safe for the caller.** Any failure (no key, disabled, transport error,
  bad output) raises :class:`KeywordAIError`; the worker catches it and falls back
  to the rule-based defaults, so an item is never left without keywords.
- **Configurable + gated.** ``KEYWORD_AI_MODEL`` selects the model;
  ``KEYWORD_AI_ENABLED`` turns the feature on/off (**default on**) so an operator
  can disable the spend without a code change. Each AI keyword is a new daily
  Oxylabs scrape, so this scales the feature's dominant cost — see the cost note
  in ``docs``/memory.
- **Secrets.** The API key is read from ``OPENAI_API_KEY`` and is never logged,
  echoed, or placed in an error message (security-standards).
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# How many keywords to request/track per item. The slot model in
# :mod:`app.item_keywords` carries this many positional slots.
MAX_KEYWORDS = 5

# One keyword can't exceed this (matches item_keywords.MAX_KEYWORD_LEN / the input
# caps) so a chatty model response can't bloat a row or a SERP query.
_MAX_KEYWORD_LEN = 120

# Default when KEYWORD_AI_MODEL is unset. A small, inexpensive chat model is plenty
# for picking a few search phrases; an operator can point it at another model
# (e.g. a newer mini) via the env var without a code change.
DEFAULT_MODEL = "gpt-4o-mini"

_CHAT_URL = "https://api.openai.com/v1/chat/completions"
# Picking a few keywords is a fast call; keep the timeout tight so a stall fails
# over to the rule-based defaults quickly rather than holding the worker.
_REQUEST_TIMEOUT_S = 30

# Structured-output contract: an object with exactly a `keywords` array of strings.
# strict + additionalProperties:false makes the shape exact so parsing can't
# silently receive something unexpected.
_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "keywords": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": MAX_KEYWORDS,
        },
    },
    "required": ["keywords"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are a Walmart Marketplace search strategist. Given one product, return the "
    f"{MAX_KEYWORDS} search terms a real shopper is most likely to type into "
    "Walmart's search bar to find a product like this one, ordered best first.\n\n"
    "Rules:\n"
    f"- Return exactly {MAX_KEYWORDS} terms (fewer only if the product genuinely "
    "supports fewer distinct searches).\n"
    "- Natural shopper phrasing, lowercase, 1-5 words each. No punctuation, no "
    "quotes, no SKU/model numbers.\n"
    "- Mix specific and general intent: include at least one brand-led term and at "
    "least one generic category term a non-brand-loyal shopper would use.\n"
    "- No duplicates and no near-duplicates (don't just append size/variant to the "
    "same phrase).\n"
    "- Base terms only on the product facts provided; do not invent attributes."
)


class KeywordAIError(Exception):
    """Keyword suggestion could not be completed (disabled, no key, or bad output).

    The caller (the descriptors backfill) treats this as "use the rule-based
    defaults" rather than a hard error — an item is never left keyword-less.
    """


def ai_keywords_enabled() -> bool:
    """Whether AI keyword selection is turned on (env, **default on**).

    An env switch so an operator can disable the spend on the droplet without a
    code change. Each AI keyword is a daily Oxylabs scrape once rank capture is on,
    so this gates the feature's dominant cost. Unset/blank is treated as on;
    ``KEYWORD_AI_ENABLED=0`` (or false/no/off) turns it off.
    """
    raw = os.environ.get("KEYWORD_AI_ENABLED")
    if raw is None or raw.strip() == "":
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def resolve_model() -> str:
    """Return the configured keyword model (env override, else default)."""
    return (os.environ.get("KEYWORD_AI_MODEL") or "").strip() or DEFAULT_MODEL


def _api_key() -> str:
    """Read OPENAI_API_KEY, or raise an actionable error (never echoing the key)."""
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        raise KeywordAIError(
            "OPENAI_API_KEY is not set; cannot suggest keywords. Set it in the "
            "worker's environment (.env on the droplet)."
        )
    return key


def _build_user_prompt(brand: str | None, title: str | None,
                       size: str | None, category: str | None) -> str:
    """Assemble the per-item prompt from the product facts we have."""
    return (
        "Suggest search keywords for this Walmart product.\n\n"
        f"BRAND: {(brand or '').strip() or '(unknown)'}\n"
        f"PRODUCT TITLE: {(title or '').strip() or '(unknown)'}\n"
        f"SIZE: {(size or '').strip() or '(unknown)'}\n"
        f"CATEGORY: {(category or '').strip() or '(unknown)'}\n"
    )


def _clean_terms(raw_terms: list) -> list[str]:
    """Normalize the model's terms: strip, cap length, drop blanks/dupes, keep order.

    The model response is untrusted input — validate and bound it before it reaches
    a DB row or a SERP query. Case-insensitive de-dupe preserving first occurrence.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for term in raw_terms:
        if not isinstance(term, str):
            continue
        value = term.strip()[:_MAX_KEYWORD_LEN].strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
        if len(cleaned) >= MAX_KEYWORDS:
            break
    return cleaned


def suggest_keywords(brand: str | None, title: str | None,
                     size: str | None, category: str | None, *,
                     model: str | None = None, session=None) -> list[str]:
    """Return up to :data:`MAX_KEYWORDS` AI-chosen shopper search terms for a product.

    ``session`` is injectable for testing (a fake with a ``post`` method); in
    production it's the ``requests`` module. ``model`` overrides the configured
    model. Raises :class:`KeywordAIError` on any failure (disabled, no key,
    transport/HTTP error, or empty/bad output) so the caller can fall back to the
    rule-based defaults. Needs at least a brand or a title to work from.
    """
    if not ai_keywords_enabled():
        raise KeywordAIError("AI keyword selection is disabled (KEYWORD_AI_ENABLED).")
    if not ((brand or "").strip() or (title or "").strip()):
        # Nothing to reason from — don't spend a call that would only echo blanks.
        raise KeywordAIError("No brand or title available to suggest keywords from.")

    model = model or resolve_model()
    if session is None:
        import requests  # local import keeps module import cheap / dependency-light
        session = requests

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(brand, title, size, category)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "keywords", "strict": True, "schema": _OUTPUT_SCHEMA},
        },
    }
    headers = {"Authorization": f"Bearer {_api_key()}"}  # key never logged
    logger.info("Suggesting keywords model=%s brand=%r title_len=%d",
                model, (brand or "").strip()[:40], len((title or "").strip()))

    try:
        resp = session.post(_CHAT_URL, json=payload, headers=headers,
                            timeout=_REQUEST_TIMEOUT_S)
    except Exception as e:  # normalize any transport error for the caller
        # The exception message does not contain our key; still, don't add request
        # internals (which carry the prompt) beyond the message itself.
        raise KeywordAIError(f"Keyword suggestion request failed: {e}") from e

    if resp.status_code != 200:
        # Body may carry a provider error message; it does not contain our key.
        raise KeywordAIError(
            f"Keyword provider returned {resp.status_code}: {resp.text[:300]}"
        )
    try:
        body = resp.json() or {}
    except ValueError as e:
        raise KeywordAIError(f"Keyword provider returned non-JSON: {e}") from e

    content = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
    if not content:
        raise KeywordAIError("Keyword provider returned no content.")
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise KeywordAIError(f"Could not parse the model's JSON output: {e}") from e

    terms = _clean_terms(data.get("keywords") or [])
    if not terms:
        raise KeywordAIError("The model returned no usable keywords.")
    logger.info("Suggested %d keyword(s) model=%s", len(terms), model)
    return terms
