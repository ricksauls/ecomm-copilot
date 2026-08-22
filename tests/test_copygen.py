"""Tests for the AI copy generator.

The Anthropic client is faked and injected, so these run offline with no API key
(exactly how they run in CI) and assert on our request shaping and response
parsing — never on a live model.
"""

import json

import pytest

from app import copygen
from app.scoring import PdpRecord


class _Block:
    """Stand-in for an SDK content block (has .type and .text)."""

    def __init__(self, type, text=None):
        self.type = type
        self.text = text


class _Response:
    def __init__(self, content, stop_reason="end_turn"):
        self.content = content
        self.stop_reason = stop_reason


class _FakeClient:
    """Captures the create() kwargs and returns a canned response."""

    def __init__(self, response):
        self._response = response
        self.captured = {}

        outer = self

        class _Messages:
            def create(self, **kwargs):
                outer.captured = kwargs
                return outer._response

        self.messages = _Messages()


def _record():
    return PdpRecord(
        url="https://www.walmart.com/ip/10294528",
        item_id="10294528",
        title="TABASCO SAUCE 5OZ",
        bullets=["Original red pepper sauce"],
        description="A pepper sauce.",
    )


def _json_response(payload):
    return _Response([_Block("thinking", ""), _Block("text", json.dumps(payload))])


def test_generate_copy_parses_structured_json():
    payload = {
        "title": "Tabasco Original Red Pepper Sauce, 5 fl oz Bottle",
        "bullets": ["Adds bold flavor to any dish", "Aged three years for depth"],
        "description": "A long, keyword-rich description of the sauce.",
    }
    client = _FakeClient(_json_response(payload))
    result = copygen.generate_copy(_record(), ["pepper sauce", "hot sauce"], client=client)
    assert result.title == payload["title"]
    assert result.bullets == payload["bullets"]
    assert result.description == payload["description"]


def test_prompt_carries_current_copy_and_keywords():
    client = _FakeClient(_json_response({"title": "T", "bullets": ["b"], "description": "d"}))
    copygen.generate_copy(_record(), ["pepper sauce"], model="claude-opus-5", client=client)
    sent = client.captured
    assert sent["model"] == "claude-opus-5"
    # Structured output is requested.
    assert sent["output_config"]["format"]["type"] == "json_schema"
    user_msg = sent["messages"][0]["content"]
    assert "TABASCO SAUCE 5OZ" in user_msg      # current title grounds the rewrite
    assert "pepper sauce" in user_msg           # target keyword is passed in


def test_refusal_raises():
    client = _FakeClient(_Response([_Block("text", "{}")], stop_reason="refusal"))
    with pytest.raises(copygen.CopyGenError):
        copygen.generate_copy(_record(), None, client=client)


def test_bad_json_raises():
    client = _FakeClient(_Response([_Block("text", "not json")]))
    with pytest.raises(copygen.CopyGenError):
        copygen.generate_copy(_record(), None, client=client)


def test_missing_fields_raises():
    # Empty bullets is an incomplete result and must fail rather than persist.
    client = _FakeClient(_json_response({"title": "T", "bullets": [], "description": "d"}))
    with pytest.raises(copygen.CopyGenError):
        copygen.generate_copy(_record(), None, client=client)


def test_resolve_model_env_override(monkeypatch):
    monkeypatch.setenv("COPYGEN_MODEL", "claude-sonnet-5")
    assert copygen.resolve_model() == "claude-sonnet-5"
    monkeypatch.delenv("COPYGEN_MODEL", raising=False)
    assert copygen.resolve_model() == copygen.DEFAULT_MODEL
