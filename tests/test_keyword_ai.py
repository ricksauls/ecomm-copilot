"""Tests for AI keyword selection (app.keyword_ai).

The OpenAI HTTP call is faked and injected (a stand-in ``session`` with a
``post``), so these run offline with no key — asserting our request shaping,
response parsing, cleaning, and the fail-safe errors the caller relies on, never
a live model.
"""

import json

import pytest

from app import keyword_ai


class _Resp:
    """Stand-in for a requests.Response (status_code / .json() / .text)."""

    def __init__(self, *, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body
        self.text = text

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class _Session:
    """Captures the post() kwargs and returns a canned response."""

    def __init__(self, resp):
        self._resp = resp
        self.captured = {}

    def post(self, url, **kwargs):
        self.captured = {"url": url, **kwargs}
        return self._resp


def _chat_body(keywords):
    """Shape an OpenAI chat-completions response carrying the JSON content."""
    return {"choices": [{"message": {"content": json.dumps({"keywords": keywords})}}]}


@pytest.fixture(autouse=True)
def _key(monkeypatch):
    # A key is present by default so tests exercise the request path; AI is on by
    # default (KEYWORD_AI_ENABLED unset == on). Individual tests override as needed.
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real")
    monkeypatch.delenv("KEYWORD_AI_ENABLED", raising=False)
    monkeypatch.delenv("KEYWORD_AI_MODEL", raising=False)


def test_happy_path_returns_terms_and_shapes_request(monkeypatch):
    session = _Session(_Resp(body=_chat_body(
        ["hot sauce", "tabasco hot sauce", "pepper sauce", "cayenne", "louisiana hot sauce"])))
    terms = keyword_ai.suggest_keywords(
        "Tabasco", "Original Hot Sauce", "12 oz", "Hot Sauce", session=session)
    assert terms == ["hot sauce", "tabasco hot sauce", "pepper sauce", "cayenne", "louisiana hot sauce"]
    # Request shaping: default model, json_schema structured output, two messages.
    payload = session.captured["json"]
    assert payload["model"] == keyword_ai.DEFAULT_MODEL
    assert payload["response_format"]["type"] == "json_schema"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]
    # The bearer token rides the header, never the payload/logs.
    assert session.captured["headers"]["Authorization"] == "Bearer sk-test-not-real"


def test_cleans_caps_dedupes_and_limits(monkeypatch):
    session = _Session(_Resp(body=_chat_body(
        ["  Hot Sauce  ", "hot sauce", "", "x" * 200, "a", "b", "c", "d"])))
    terms = keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=session)
    # Trimmed; case-insensitive dupe dropped; blank dropped; long one capped; max 5.
    assert terms[0] == "Hot Sauce"
    assert "hot sauce" not in terms[1:]          # case-insensitive de-dupe
    assert len(terms) == keyword_ai.MAX_KEYWORDS  # capped at 5
    assert all(len(t) <= 120 for t in terms)


def test_disabled_raises(monkeypatch):
    monkeypatch.setenv("KEYWORD_AI_ENABLED", "0")
    with pytest.raises(keyword_ai.KeywordAIError, match="disabled"):
        keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=_Session(_Resp()))


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(keyword_ai.KeywordAIError, match="OPENAI_API_KEY"):
        keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=_Session(_Resp()))


def test_no_brand_or_title_raises():
    with pytest.raises(keyword_ai.KeywordAIError, match="brand or title"):
        keyword_ai.suggest_keywords(None, "  ", "12 oz", "Hot Sauce", session=_Session(_Resp()))


def test_http_error_raises():
    session = _Session(_Resp(status_code=429, text="rate limited"))
    with pytest.raises(keyword_ai.KeywordAIError, match="429"):
        keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=session)


def test_bad_json_content_raises():
    session = _Session(_Resp(body={"choices": [{"message": {"content": "not json"}}]}))
    with pytest.raises(keyword_ai.KeywordAIError, match="parse"):
        keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=session)


def test_empty_keywords_raises():
    session = _Session(_Resp(body=_chat_body([])))
    with pytest.raises(keyword_ai.KeywordAIError, match="no usable"):
        keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=session)


def test_model_env_override(monkeypatch):
    monkeypatch.setenv("KEYWORD_AI_MODEL", "gpt-5-mini")
    session = _Session(_Resp(body=_chat_body(["hot sauce"])))
    keyword_ai.suggest_keywords("Tabasco", "Hot Sauce", None, None, session=session)
    assert session.captured["json"]["model"] == "gpt-5-mini"
