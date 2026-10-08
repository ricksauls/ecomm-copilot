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


def test_unimplemented_type_raises(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        assets = _plan_and_assets(db, uid, pid)
        piu = _asset_of(assets, "PRODUCT_IN_USE")
        with pytest.raises(generate.GenerationError):
            generate.process_asset(db, piu, isstore.get_project(db, pid, uid))


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


def test_enqueue_only_implemented_filters(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("f@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        _plan_and_assets(db, uid, pid)
        job_ids = generate.enqueue_project_assets(db, isstore.get_project(db, pid, uid), uid)
        # 2 LIFESTYLE + 1 SIZE_COMPARISON are implemented; the other 5 are skipped.
        assert len(job_ids) == 3
        assert isstore.get_project(db, pid, uid)["status"] == isstore.STATUS_GENERATING


def test_enqueue_respects_selected_types(app, tmp_path, monkeypatch):
    """Only the user-selected (and implemented) types are queued."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("sel@example.com", "password123")
        pid = _project_with_cutout(db, uid)
        _plan_and_assets(db, uid, pid)
        # User ticks only SIZE_COMPARISON (plus an unbuilt type that generates nothing).
        isstore.set_selected_types(db, pid, ["SIZE_COMPARISON", "INFOGRAPHIC"])
        job_ids = generate.enqueue_project_assets(db, isstore.get_project(db, pid, uid), uid)
        # Only the 1 SIZE_COMPARISON is both selected and implemented.
        assert len(job_ids) == 1


def test_asset_type_choices_match_plan_types(app):
    """The cutout picker's keys stay in sync with the planner's asset types."""
    choices = generate.asset_type_choices()
    assert [c["key"] for c in choices] == list(planmod.ASSET_TYPES)
    # Only the implemented types are checked-by-default.
    ready = {c["key"] for c in choices if c["ready"]}
    assert ready == set(generate.IMPLEMENTED_TYPES)


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
        # An unimplemented type fails loudly → job + asset both marked failed.
        piu = _asset_of(assets, "PRODUCT_IN_USE")
        isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=piu["id"])
        row = isjobs.claim_next_job(db)

        worker.process_imageset_one(db, row)
        assert isjobs.get_job(db, row["id"], uid)["status"] == "error"
        assert isstore.get_asset(db, piu["id"], uid)["status"] == "failed"
