"""End-to-end tests for image-set generation: cutout, orchestrator, worker wiring.

Exercises the real pipeline against the offline mock providers with a temporary
MEDIA_DIR, so finals are actually composited and written to disk — no network, no
keys. The vertical slice's two asset types (LIFESTYLE, SIZE_COMPARISON) are driven
all the way to ``ready`` 2000×2000 PNGs.
"""

import io

import pytest
from PIL import Image, ImageDraw

from app.db import get_db
from app.imageset import generate, storage
from app.imageset import jobs as isjobs
from app.imageset import plan as planmod
from app.imageset import store as isstore
from app.imageset.config import CANVAS_SIZE
from app.users import create_local_user


def _product_png() -> bytes:
    img = Image.new("RGB", (400, 640), (255, 255, 255))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([140, 90, 260, 600], radius=30, fill=(190, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _project_with_cutout(db, uid):
    """Create a project, store an original, run the cutout, and approve it."""
    pid = isstore.create_project(
        db, user_id=uid, name="Bug Spray", brand="Tabasco", category="insect repellent",
        intended_environments=["a backyard patio", "a hiking trail"],
        dimensions={"width": 2.2, "height": 7.8, "depth": 2.2, "unit": "in", "weight": "6 oz"},
    )
    for i in range(1, 6):
        isstore.add_feature(db, pid, feature_key=f"f{i}", title=f"Feature {i}", position=i)
    rel = storage.save(pid, "original", "product", _product_png())
    isstore.set_original_image(db, pid, rel)
    project = isstore.get_project(db, pid, uid)
    generate.run_cutout(db, project, uid)
    isstore.approve_cutout(db, pid)
    return pid


def _plan_and_assets(db, uid, pid):
    project = isstore.get_project(db, pid, uid)
    plan = planmod.mock_plan(isstore.plan_context(db, project))
    isstore.save_plan_and_create_assets(db, pid, uid, plan)
    return isstore.assets_for_project(db, pid, uid)


def _asset_of(assets, asset_type):
    return next(a for a in assets if a["asset_type"] == asset_type)


def test_run_cutout_writes_cutout(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("a@example.com", "password123")
        pid = isstore.create_project(db, user_id=uid, name="X", category="repellent")
        rel = storage.save(pid, "original", "product", _product_png())
        isstore.set_original_image(db, pid, rel)
        generate.run_cutout(db, isstore.get_project(db, pid, uid), uid)
        project = isstore.get_project(db, pid, uid)
        assert project["cutout_path"]
        assert storage.load(project["cutout_path"]) is not None


def test_lifestyle_asset_generates_final_2000(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("b@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        lifestyle = _asset_of(assets, "LIFESTYLE")

        generate.process_asset(db, lifestyle, isstore.get_project(db, pid, uid))
        a = isstore.get_asset(db, lifestyle["id"], uid)
        assert a["status"] == "ready"
        # Final, thumb, AND the raw AI scene are all written.
        for col in ("final_path", "thumb_path", "scene_path"):
            assert a[col] and storage.load(a[col]) is not None
        final = Image.open(io.BytesIO(storage.load(a["final_path"])))
        assert final.size == (CANVAS_SIZE, CANVAS_SIZE)


def test_size_comparison_uses_ai_scene_with_dimensions(app, tmp_path, monkeypatch):
    """With a real height anchor, size-comparison renders an AI scene + branded bars."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("c@example.com", "password123")
        pid = _project_with_cutout(db, uid)  # has dimensions (height 7.8 in)
        assets = _plan_and_assets(db, uid, pid)
        sc = _asset_of(assets, "SIZE_COMPARISON")

        generate.process_asset(db, sc, isstore.get_project(db, pid, uid))
        a = isstore.get_asset(db, sc["id"], uid)
        assert a["status"] == "ready"
        assert a["final_path"] and storage.load(a["final_path"]) is not None
        assert a["scene_path"] is not None  # AI scale-comparison scene was produced
        final = Image.open(io.BytesIO(storage.load(a["final_path"])))
        assert final.size == (CANVAS_SIZE, CANVAS_SIZE)


def test_feature_callout_generates_with_backdrop(app, tmp_path, monkeypatch):
    """Feature-callout composites rows over an AI backdrop → ready 2000² final."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("fc@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        fc = _asset_of(assets, "FEATURE_CALLOUT")
        generate.process_asset(db, fc, isstore.get_project(db, pid, uid))
        a = isstore.get_asset(db, fc["id"], uid)
        assert a["status"] == "ready"
        assert a["scene_path"] is not None  # AI backdrop was generated
        assert Image.open(io.BytesIO(storage.load(a["final_path"]))).size == (CANVAS_SIZE, CANVAS_SIZE)


def test_straighten_cutout_returns_trimmed_product(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("st@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        project = isstore.get_project(db, pid, uid)
        cut = Image.new("RGBA", (300, 200), (0, 0, 0, 0))
        ImageDraw.Draw(cut).rectangle([20, 20, 279, 179], fill=(10, 10, 10, 255))
        out, cost = generate._straighten_cutout(project, cut)
        assert out.width > 20 and out.height > 20  # mock returns a transparent product
        assert cost == 0.0  # mock is free


def test_straighten_failsafe_returns_original(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("sf@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        project = isstore.get_project(db, pid, uid)
        cut = Image.new("RGBA", (120, 90), (0, 0, 0, 0))
        ImageDraw.Draw(cut).rectangle([10, 10, 110, 80], fill=(0, 0, 0, 255))

        class _Boom:
            def edit_image(self, **kw):
                raise RuntimeError("provider down")

        monkeypatch.setattr(generate, "get_image_generation_provider", lambda: _Boom())
        out, cost = generate._straighten_cutout(project, cut)
        assert out is cut and cost == 0.0  # fail-safe: raw cutout, no cost


def test_straighten_flag_defaults_on_and_can_disable(monkeypatch):
    from app.imageset import config
    monkeypatch.delenv("IMAGESET_STRAIGHTEN_PRODUCT", raising=False)
    assert config.straighten_feature_product() is True
    monkeypatch.setenv("IMAGESET_STRAIGHTEN_PRODUCT", "0")
    assert config.straighten_feature_product() is False


def test_display_aspect_prefers_dimensions_over_cutout():
    import json
    # Near-square cutout (an angled keyboard shot) but wide REAL dimensions.
    cut = Image.new("RGBA", (420, 400), (0, 0, 0, 0))
    ImageDraw.Draw(cut).rectangle([10, 10, 409, 389], fill=(20, 20, 20, 255))
    wide = {"dimensions_json": json.dumps({"width": 16, "height": 0.13, "unit": "in"})}
    # Dimensions win: a 16" x 0.13" product reads wide despite the ~square cutout.
    assert generate._display_aspect(wide, cut) > 1.25
    # No usable dimensions → fall back to the (near-square) cutout bounding box.
    assert generate._display_aspect({"dimensions_json": None}, cut) < 1.25


def test_product_in_use_generates_via_ai(app, tmp_path, monkeypatch):
    """Product-in-use edits the product photo → ready 2000² final with an AI scene."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("piu@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        piu = _asset_of(assets, "PRODUCT_IN_USE")
        generate.process_asset(db, piu, isstore.get_project(db, pid, uid))
        a = isstore.get_asset(db, piu["id"], uid)
        assert a["status"] == "ready"
        assert a["scene_path"] is not None  # AI edit scene was produced
        assert Image.open(io.BytesIO(storage.load(a["final_path"]))).size == (CANVAS_SIZE, CANVAS_SIZE)


def test_size_comparison_falls_back_to_diagram_without_dimensions(app, tmp_path, monkeypatch):
    """No height anchor → the programmatic measurement diagram (no AI scene)."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("cd@example.com", "password123")
        # Project with a cutout but NO dimensions (so no scale anchor).
        pid = isstore.create_project(db, user_id=uid, name="No Dims", category="repellent")
        for i in range(1, 6):
            isstore.add_feature(db, pid, feature_key=f"f{i}", title=f"Feature {i}", position=i)
        rel = storage.save(pid, "original", "product", _product_png())
        isstore.set_original_image(db, pid, rel)
        generate.run_cutout(db, isstore.get_project(db, pid, uid), uid)
        isstore.approve_cutout(db, pid)
        sc = _asset_of(_plan_and_assets(db, uid, pid), "SIZE_COMPARISON")

        generate.process_asset(db, sc, isstore.get_project(db, pid, uid))
        a = isstore.get_asset(db, sc["id"], uid)
        assert a["status"] == "ready"
        assert a["scene_path"] is None  # programmatic diagram: no AI scene
        assert Image.open(io.BytesIO(storage.load(a["final_path"]))).size == (CANVAS_SIZE, CANVAS_SIZE)


def test_size_comparison_large_product_uses_diagram(app, tmp_path, monkeypatch):
    """A furniture-scale product (too big for everyday objects) → measurement diagram."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("big@example.com", "password123")
        # A couch: has a height anchor, but far larger than the reference library.
        pid = isstore.create_project(
            db, user_id=uid, name="Sofa", category="furniture",
            dimensions={"width": 84, "height": 36, "depth": 38, "unit": "in"})
        for i in range(1, 6):
            isstore.add_feature(db, pid, feature_key=f"f{i}", title=f"Feature {i}", position=i)
        rel = storage.save(pid, "original", "product", _product_png())
        isstore.set_original_image(db, pid, rel)
        generate.run_cutout(db, isstore.get_project(db, pid, uid), uid)
        isstore.approve_cutout(db, pid)
        sc = _asset_of(_plan_and_assets(db, uid, pid), "SIZE_COMPARISON")

        generate.process_asset(db, sc, isstore.get_project(db, pid, uid))
        a = isstore.get_asset(db, sc["id"], uid)
        assert a["status"] == "ready"
        assert a["scene_path"] is None  # diagram (no AI object comparison for furniture)


def test_asset_type_without_generator_raises(app, tmp_path, monkeypatch):
    # Every plan type now has a generator; simulate a missing one (future type).
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.delitem(generate._GENERATORS, "SIZE_COMPARISON", raising=False)
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        sc = _asset_of(assets, "SIZE_COMPARISON")
        with pytest.raises(generate.GenerationError):
            generate.process_asset(db, sc, isstore.get_project(db, pid, uid))


def test_missing_cutout_raises(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("e@example.com", "password123")
        # Project with a plan but no cutout ever produced.
        pid = isstore.create_project(db, user_id=uid, name="X", category="repellent")
        assets = _plan_and_assets(db, uid, pid)
        with pytest.raises(generate.GenerationError):
            generate.process_asset(db, _asset_of(assets, "LIFESTYLE"), isstore.get_project(db, pid, uid))


def test_enqueue_all_implemented_variations(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("f@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        _plan_and_assets(db, uid, pid)
        job_ids = generate.enqueue_project_assets(db, isstore.get_project(db, pid, uid), uid)
        # All 7 variations are now implemented (2 lifestyle + 2 feature + 2 PIU + 1 size).
        assert len(job_ids) == 7
        assert isstore.get_project(db, pid, uid)["status"] == isstore.STATUS_GENERATING


def test_enqueue_only_implemented_filters(app, tmp_path, monkeypatch):
    # With only SIZE_COMPARISON "implemented", the other types are skipped.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.setattr(generate, "IMPLEMENTED_TYPES", frozenset({"SIZE_COMPARISON"}))
    with app.app_context():
        db = get_db()
        uid = create_local_user("f2@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        _plan_and_assets(db, uid, pid)
        job_ids = generate.enqueue_project_assets(db, isstore.get_project(db, pid, uid), uid)
        assert len(job_ids) == 1


def test_enqueue_respects_selected_variations(app, tmp_path, monkeypatch):
    """Only the user-selected variations are queued (per-variation keys)."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("sel@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        _plan_and_assets(db, uid, pid)
        # Tick just Lifestyle 2 and Size Comparison.
        isstore.set_selected_types(db, pid, ["LIFESTYLE:2", "SIZE_COMPARISON:1"])
        job_ids = generate.enqueue_project_assets(db, isstore.get_project(db, pid, uid), uid)
        assert len(job_ids) == 2


def test_asset_type_choices_cover_plan_variations(app):
    """The cutout picker's keys cover exactly the variations the plan creates."""
    choices = generate.asset_type_choices()
    keys = [c["key"] for c in choices]
    assert all(k.split(":")[0] in planmod.ASSET_TYPES for k in keys)
    expected = {f"{t}:{v}"
                for t, n in planmod.REQUIRED_COMPOSITION.items() for v in range(1, n + 1)}
    assert set(keys) == expected
    assert all(c["ready"] for c in choices)  # every type is implemented now


# --- worker integration -----------------------------------------------------

def test_worker_processes_asset_job(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    import worker

    with app.app_context():
        db = get_db()
        uid = create_local_user("g@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        sc = _asset_of(assets, "SIZE_COMPARISON")
        isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=sc["id"])
        row = isjobs.claim_next_job(db)  # -> processing

        worker.process_imageset_one(db, row)
        assert isjobs.get_job(db, row["id"], uid)["status"] == "done"
        assert isstore.get_asset(db, sc["id"], uid)["status"] == "ready"


def test_worker_marks_failed_on_bad_asset(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    import worker

    with app.app_context():
        db = get_db()
        uid = create_local_user("h@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        # A generator that raises fails loudly → job + asset both marked failed.
        monkeypatch.setitem(generate._GENERATORS, "PRODUCT_IN_USE",
                            lambda *a, **k: (_ for _ in ()).throw(generate.GenerationError("boom")))
        piu = _asset_of(assets, "PRODUCT_IN_USE")
        isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=piu["id"])
        row = isjobs.claim_next_job(db)

        worker.process_imageset_one(db, row)
        assert isjobs.get_job(db, row["id"], uid)["status"] == "error"
        assert isstore.get_asset(db, piu["id"], uid)["status"] == "failed"
