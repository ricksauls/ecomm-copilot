"""Tests for the image-set data layer: project/feature/asset store + job queue."""

from app.db import get_db
from app.imageset import jobs as isjobs
from app.imageset import plan as planmod
from app.imageset import store
from app.users import create_local_user


def _project(db, uid, **kw):
    return store.create_project(db, user_id=uid, name=kw.pop("name", "Bug Spray"), **kw)


def _plan(n_features=5):
    ctx = planmod.PlanProductContext(
        name="Bug Spray", brand="Tabasco", category="insect repellent",
        intended_environments=["a patio", "a trail"],
        features=[planmod.Feature(id=f"f{i}", title=f"Feature {i}") for i in range(1, n_features + 1)],
    )
    return planmod.mock_plan(ctx)


# --- projects ---------------------------------------------------------------

def test_create_and_get_project_is_idor_scoped(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("a@example.com", "password123")
        other = create_local_user("b@example.com", "password123")
        pid = _project(db, uid, brand="Tabasco", category="repellent",
                       intended_environments=["a patio"])
        row = store.get_project(db, pid, uid)
        assert row["status"] == store.STATUS_DRAFT
        assert row["name"] == "Bug Spray" and row["brand"] == "Tabasco"
        # Another user cannot read it.
        assert store.get_project(db, pid, other) is None


def test_cutout_approval_flow(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("c@example.com", "password123")
        pid = _project(db, uid)
        store.set_original_image(db, pid, "imageset/1/original/x.png")
        assert store.get_project(db, pid, uid)["status"] == store.STATUS_CUTOUT_PENDING
        store.set_cutout(db, pid, "imageset/1/cutout/x.png")
        assert store.get_project(db, pid, uid)["cutout_approved_at"] is None  # not yet approved
        store.approve_cutout(db, pid)
        row = store.get_project(db, pid, uid)
        assert row["status"] == store.STATUS_CUTOUT_APPROVED
        assert row["cutout_approved_at"] is not None


def test_features_round_trip_into_plan_context(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        pid = _project(db, uid, category="repellent",
                       intended_environments=["a patio", "a trail"],
                       dimensions={"width": 2, "height": 6, "unit": "in"})
        store.add_feature(db, pid, feature_key="f1", title="DEET-free", position=0)
        store.add_feature(db, pid, feature_key="f2", title="8-hour protection", position=1)
        ctx = store.plan_context(db, store.get_project(db, pid, uid))
        assert ctx.category == "repellent"
        assert ctx.intended_environments == ["a patio", "a trail"]
        assert ctx.has_dimensions is True
        assert [f.id for f in ctx.features] == ["f1", "f2"]


# --- plan + assets ----------------------------------------------------------

def test_save_plan_creates_eight_assets(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("e@example.com", "password123")
        pid = _project(db, uid)
        ids = store.save_plan_and_create_assets(db, pid, uid, _plan())
        assert len(ids) == 7
        assets = store.assets_for_project(db, pid, uid)
        assert len(assets) == 7
        assert store.get_project(db, pid, uid)["status"] == store.STATUS_PLANNING
        # The plan JSON was persisted for reproducibility.
        assert store.get_project(db, pid, uid)["plan_json"]


def test_save_plan_replaces_prior_assets(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("f@example.com", "password123")
        pid = _project(db, uid)
        store.save_plan_and_create_assets(db, pid, uid, _plan())
        store.save_plan_and_create_assets(db, pid, uid, _plan())  # re-plan
        # A re-plan starts clean — still exactly 8, not 16.
        assert len(store.assets_for_project(db, pid, uid)) == 7


def test_update_asset_whitelist_and_idor(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("g@example.com", "password123")
        other = create_local_user("h@example.com", "password123")
        pid = _project(db, uid)
        aid = store.save_plan_and_create_assets(db, pid, uid, _plan())[0]
        store.update_asset(db, aid, status="ready", final_path="imageset/1/final/1.png",
                           bogus_column="ignored; not in allowlist")
        a = store.get_asset(db, aid, uid)
        assert a["status"] == "ready" and a["final_path"] == "imageset/1/final/1.png"
        # IDOR: another user can't read the asset.
        assert store.get_asset(db, aid, other) is None


def test_assigned_feature_ids_parse(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("i@example.com", "password123")
        pid = _project(db, uid)
        store.save_plan_and_create_assets(db, pid, uid, _plan())
        callout = next(a for a in store.assets_for_project(db, pid, uid)
                       if a["asset_type"] == "FEATURE_CALLOUT")
        ids = store.asset_feature_ids(callout)
        assert isinstance(ids, list) and all(i.startswith("f") for i in ids)


# --- job queue --------------------------------------------------------------

def _asset(db, uid):
    pid = _project(db, uid)
    aid = store.save_plan_and_create_assets(db, pid, uid, _plan())[0]
    return pid, aid


def test_enqueue_and_claim_job(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("j@example.com", "password123")
        pid, aid = _asset(db, uid)
        jid = isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        assert isjobs.get_job(db, jid, uid)["status"] == "queued"
        assert isjobs.has_claimable_jobs(db) is True
        claimed = isjobs.claim_next_job(db)
        assert claimed["id"] == jid and claimed["status"] == "processing"
        assert isjobs.claim_next_job(db) is None  # nothing left queued


def test_enqueue_is_idempotent_per_asset(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("k@example.com", "password123")
        pid, aid = _asset(db, uid)
        first = isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        second = isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        assert first == second
        assert db.execute("SELECT COUNT(*) FROM imageset_jobs").fetchone()[0] == 1


def test_failed_job_can_be_requeued(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("l@example.com", "password123")
        pid, aid = _asset(db, uid)
        jid = isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        isjobs.mark_failed(db, jid, "boom")
        again = isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        assert again == jid
        row = isjobs.get_job(db, jid, uid)
        assert row["status"] == "queued" and row["error"] is None


def test_reclaim_orphaned_jobs(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("m@example.com", "password123")
        pid, aid = _asset(db, uid)
        jid = isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        isjobs.claim_next_job(db)  # -> processing
        assert isjobs.reclaim_orphaned_jobs(db) == 1
        assert isjobs.get_job(db, jid, uid)["status"] == "error"


def test_cancel_queued_for_project(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("n@example.com", "password123")
        pid = _project(db, uid)
        ids = store.save_plan_and_create_assets(db, pid, uid, _plan())
        for aid in ids:
            isjobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
        isjobs.claim_next_job(db)  # one goes processing (not cancellable)
        cancelled = isjobs.cancel_queued_for_project(db, pid, uid)
        assert cancelled == 6  # 7 queued, 1 claimed → 6 removed


# --- plan queue (async planning) --------------------------------------------

def test_plan_queue_claim_and_reclaim(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("plan@example.com", "password123")
        pid = _project(db, uid)
        store.queue_plan(db, pid)
        assert store.get_project(db, pid, uid)["status"] == store.STATUS_PLAN_QUEUED
        assert store.has_claimable_plan(db) is True
        claimed = store.claim_next_plan(db)
        assert claimed["id"] == pid and claimed["status"] == store.STATUS_PLAN_ACTIVE
        assert store.claim_next_plan(db) is None  # conditional claim — not re-grabbed
        # An orphaned (mid-plan) project is failed on reclaim so the user can re-approve.
        assert store.reclaim_orphaned_plans(db) == 1
        assert store.get_project(db, pid, uid)["status"] == store.STATUS_FAILED
