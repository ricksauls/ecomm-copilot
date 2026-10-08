"""Tests for the image-set creative plan (mock planner + Claude path + validation)."""

import json
from collections import Counter

import pytest

from app.imageset import plan as planmod


def _ctx(n_features=5):
    return planmod.PlanProductContext(
        name="Bug Spray",
        brand="Tabasco",
        category="insect repellent",
        intended_environments=["a backyard patio", "a hiking trail"],
        features=[
            planmod.Feature(id=f"f{i}", title=f"Feature {i}", feature_type="benefit")
            for i in range(1, n_features + 1)
        ],
    )


# --- mock planner -----------------------------------------------------------

def test_mock_plan_has_required_composition(monkeypatch):
    monkeypatch.delenv("IMAGESET_PLAN_PROVIDER", raising=False)  # default mock
    plan = planmod.generate_plan(_ctx())
    assert len(plan.items) == 8
    counts = Counter(i.asset_type for i in plan.items)
    assert dict(counts) == planmod.REQUIRED_COMPOSITION
    # Only approved feature ids are ever assigned.
    valid = {f.id for f in _ctx().features}
    for item in plan.items:
        assert set(item.assigned_feature_ids) <= valid


def test_mock_plan_handles_no_features():
    # No approved features → callouts/infographic simply carry none (no crash).
    plan = planmod.mock_plan(_ctx(n_features=0))
    assert len(plan.items) == 8
    assert all(i.assigned_feature_ids == [] for i in plan.items)


# --- validation -------------------------------------------------------------

def test_validation_requires_exactly_eight_items():
    with pytest.raises(planmod.PlanError):
        planmod._validate_and_parse({"items": []}, set())


def test_validation_enforces_composition():
    bad = {"items": [{"assetType": "LIFESTYLE", "variationNumber": 1, "title": "x"}] * 8}
    with pytest.raises(planmod.PlanError):
        planmod._validate_and_parse(bad, set())


def test_validation_drops_hallucinated_feature_ids():
    items = [
        {"assetType": "LIFESTYLE", "variationNumber": 1, "title": "a"},
        {"assetType": "LIFESTYLE", "variationNumber": 2, "title": "b"},
        {"assetType": "FEATURE_CALLOUT", "variationNumber": 1, "title": "c",
         "assignedFeatureIds": ["f1", "ghost", "f2"]},
        {"assetType": "FEATURE_CALLOUT", "variationNumber": 2, "title": "d"},
        {"assetType": "PRODUCT_IN_USE", "variationNumber": 1, "title": "e"},
        {"assetType": "PRODUCT_IN_USE", "variationNumber": 2, "title": "f"},
        {"assetType": "SIZE_COMPARISON", "variationNumber": 1, "title": "g"},
        {"assetType": "INFOGRAPHIC", "variationNumber": 1, "title": "h"},
    ]
    plan = planmod._validate_and_parse({"items": items}, valid_ids={"f1", "f2"})
    callout = next(i for i in plan.items if i.asset_type == "FEATURE_CALLOUT" and i.variation_number == 1)
    assert callout.assigned_feature_ids == ["f1", "f2"]  # "ghost" dropped


def test_validation_bounds_overlong_fields():
    long = "x" * 5000
    items = [{"assetType": t, "variationNumber": v, "title": long, "sceneDescription": long}
             for t, v in [("LIFESTYLE", 1), ("LIFESTYLE", 2), ("FEATURE_CALLOUT", 1),
                          ("FEATURE_CALLOUT", 2), ("PRODUCT_IN_USE", 1), ("PRODUCT_IN_USE", 2),
                          ("SIZE_COMPARISON", 1), ("INFOGRAPHIC", 1)]]
    plan = planmod._validate_and_parse({"items": items}, set())
    assert all(len(i.title) <= planmod._MAX_TITLE for i in plan.items)
    assert all(len(i.scene_description) <= planmod._MAX_SCENE for i in plan.items)


# --- Claude path (fake client) ----------------------------------------------

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


def _valid_plan_json(include_ghost=False):
    ids = ["f1", "ghost"] if include_ghost else ["f1"]
    items = [
        {"assetType": "LIFESTYLE", "variationNumber": 1, "title": "L1", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-left",
         "generationInstructions": ""},
        {"assetType": "LIFESTYLE", "variationNumber": 2, "title": "L2", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-left",
         "generationInstructions": ""},
        {"assetType": "FEATURE_CALLOUT", "variationNumber": 1, "title": "F1", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": ids, "layoutStyle": "product-left",
         "generationInstructions": ""},
        {"assetType": "FEATURE_CALLOUT", "variationNumber": 2, "title": "F2", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-left",
         "generationInstructions": ""},
        {"assetType": "PRODUCT_IN_USE", "variationNumber": 1, "title": "P1", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-left",
         "generationInstructions": ""},
        {"assetType": "PRODUCT_IN_USE", "variationNumber": 2, "title": "P2", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-left",
         "generationInstructions": ""},
        {"assetType": "SIZE_COMPARISON", "variationNumber": 1, "title": "S1", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-center",
         "generationInstructions": ""},
        {"assetType": "INFOGRAPHIC", "variationNumber": 1, "title": "I1", "sceneDescription": "",
         "usageScenario": "", "assignedFeatureIds": [], "layoutStyle": "product-left",
         "generationInstructions": ""},
    ]
    return json.dumps({"items": items})


def test_claude_plan_parses_and_filters(monkeypatch):
    monkeypatch.setenv("IMAGESET_PLAN_PROVIDER", "claude")
    client = _FakeClient(_FakeResp(_valid_plan_json(include_ghost=True)))
    plan = planmod.generate_plan(_ctx(), client=client, model="claude-test")
    assert len(plan.items) == 8
    callout = next(i for i in plan.items if i.asset_type == "FEATURE_CALLOUT" and i.variation_number == 1)
    assert callout.assigned_feature_ids == ["f1"]  # "ghost" filtered against approved ids
    # The structured-output contract was requested.
    assert client.messages.calls[0]["output_config"]["format"]["type"] == "json_schema"


def test_claude_plan_refusal_raises():
    client = _FakeClient(_FakeResp("{}", stop_reason="refusal"))
    with pytest.raises(planmod.PlanError):
        planmod._claude_plan(_ctx(), client=client)


def test_claude_plan_bad_json_raises():
    client = _FakeClient(_FakeResp("not json"))
    with pytest.raises(planmod.PlanError):
        planmod._claude_plan(_ctx(), client=client)


def test_generate_plan_falls_back_to_mock_on_failure(monkeypatch):
    # A Claude failure (here: unparseable JSON) must not break Approve — fall back
    # to the deterministic 8-asset plan instead of raising.
    monkeypatch.setenv("IMAGESET_PLAN_PROVIDER", "claude")
    plan = planmod.generate_plan(_ctx(), client=_FakeClient(_FakeResp("not json")))
    assert len(plan.items) == 8
