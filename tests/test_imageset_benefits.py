"""Tests for AI benefit suggestion (Feature:Benefit lines for the feature images)."""

import json

from app.imageset import benefits as bmod


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResp:
    def __init__(self, text, stop_reason="end_turn"):
        self.content = [_FakeBlock(text)]
        self.stop_reason = stop_reason


class _FakeMessages:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def create(self, **kw):
        self.calls.append(kw)
        return self._resp


class _FakeClient:
    def __init__(self, resp):
        self.messages = _FakeMessages(resp)


def _client(benefits):
    return _FakeClient(_FakeResp(json.dumps({"benefits": benefits})))


def test_inert_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert bmod.suggest_benefits(["Cuts grease", "Gentle"]) == []


def test_empty_features_returns_empty():
    assert bmod.suggest_benefits([]) == []
    assert bmod.suggest_benefits(["", "  "]) == []


def test_happy_path_aligns_and_caps():
    client = _client(["Cuts grease fast for quick cleanup", "Gentle on hands"])
    out = bmod.suggest_benefits(["Cuts grease", "Gentle on hands"], client=client)
    assert out == ["Cuts grease fast for quick cleanup", "Gentle on hands"]
    # Structured-output contract was requested.
    assert client.messages.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_overlong_benefit_truncated_to_120():
    long = "x" * 300
    out = bmod.suggest_benefits(["Feature"], client=_client([long]))
    assert len(out) == 1 and len(out[0]) == bmod.MAX_BENEFIT_CHARS


def test_fewer_benefits_than_features_pads_blank():
    out = bmod.suggest_benefits(["A", "B", "C"], client=_client(["only one"]))
    assert out == ["only one", "", ""]


def test_more_benefits_than_features_truncated():
    out = bmod.suggest_benefits(["A"], client=_client(["one", "two", "three"]))
    assert out == ["one"]


def test_refusal_returns_empty():
    client = _FakeClient(_FakeResp("{}", stop_reason="refusal"))
    assert bmod.suggest_benefits(["A"], client=client) == []


def test_bad_json_returns_empty():
    client = _FakeClient(_FakeResp("not json"))
    assert bmod.suggest_benefits(["A"], client=client) == []
