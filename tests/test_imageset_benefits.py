"""Tests for AI callout suggestion (feature headline + benefit for feature images)."""

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


def _client(callouts):
    return _FakeClient(_FakeResp(json.dumps({"callouts": callouts})))


def test_inert_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert bmod.suggest_callouts(["Cuts grease", "Gentle"]) == []


def test_empty_features_returns_empty():
    assert bmod.suggest_callouts([]) == []
    assert bmod.suggest_callouts(["", "  "]) == []


def test_happy_path_returns_headline_and_benefit():
    client = _client([
        {"headline": "Long Lasting Protection", "benefit": "Repels mosquitoes for up to 8 hours"},
        {"headline": "Gentle Formula", "benefit": "Soft on hands, tough on grease"},
    ])
    out = bmod.suggest_callouts(["Repels mosquitoes 8 hrs", "Gentle on hands"], count=2, client=client)
    assert out[0] == {"headline": "Long Lasting Protection",
                      "benefit": "Repels mosquitoes for up to 8 hours"}
    assert out[1]["headline"] == "Gentle Formula"
    # Structured-output contract was requested.
    assert client.messages.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_default_count_is_six():
    # A single returned callout is padded out to the default target of 6.
    out = bmod.suggest_callouts(["A"], client=_client([{"headline": "One", "benefit": "a"}]))
    assert len(out) == bmod.DEFAULT_CALLOUT_COUNT == 6
    assert out[0]["headline"] == "One" and out[1] == {"headline": "", "benefit": ""}


def test_overlong_parts_truncated_to_caps():
    out = bmod.suggest_callouts(["Feature"], count=1, client=_client([
        {"headline": "H" * 200, "benefit": "B" * 300},
    ]))
    assert len(out[0]["headline"]) == bmod.MAX_HEADLINE_CHARS
    assert len(out[0]["benefit"]) == bmod.MAX_BENEFIT_CHARS


def test_shortfall_padded_to_count():
    out = bmod.suggest_callouts(["A", "B", "C"], count=3, client=_client([
        {"headline": "Only One", "benefit": "just one"},
    ]))
    assert out[0]["headline"] == "Only One"
    assert out[1] == {"headline": "", "benefit": ""}
    assert out[2] == {"headline": "", "benefit": ""}


def test_more_callouts_than_count_truncated():
    out = bmod.suggest_callouts(["A"], count=1, client=_client([
        {"headline": "One", "benefit": "a"}, {"headline": "Two", "benefit": "b"},
    ]))
    assert len(out) == 1 and out[0]["headline"] == "One"


def test_refusal_returns_empty():
    client = _FakeClient(_FakeResp("{}", stop_reason="refusal"))
    assert bmod.suggest_callouts(["A"], count=1, client=client) == []


def test_bad_json_returns_empty():
    client = _FakeClient(_FakeResp("not json"))
    assert bmod.suggest_callouts(["A"], count=1, client=client) == []
