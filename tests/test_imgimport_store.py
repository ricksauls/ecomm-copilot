"""Tests for the Import Product Images data layer (runs/items/assets + queue) and
the resolve-only acquire orchestrator (no-pre-download model)."""

from app.db import get_db
from app.imgimport import acquire, sources
from app.imgimport import store
from app.users import create_local_user


def _run_with_item(db, uid, url="https://www.walmart.com/ip/10294528", stype="walmart"):
    run_id = store.create_run(db, uid)
    item_id = store.add_item(db, run_id=run_id, user_id=uid, source_url=url, source_type=stype)
    return run_id, item_id


# --- store + queue ----------------------------------------------------------

def test_run_item_asset_round_trip_idor(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-a@example.com", "password123")
        other = create_local_user("imp-b@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_PROCESSING
        assert store.get_run(db, run_id, other) is None  # IDOR
        assert [i["id"] for i in store.items_for_run(db, run_id, uid)] == [item_id]

        aid = store.add_ready_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                                    full_url="https://cdn/x.jpg",
                                    thumb_url="https://cdn/x_thumb.jpg")
        assets = store.assets_for_run(db, run_id, uid)
        assert len(assets) == 1 and assets[0]["status"] == store.STATUS_READY
        assert assets[0]["source_image_url"] == "https://cdn/x.jpg"
        assert assets[0]["thumb_url"] == "https://cdn/x_thumb.jpg"
        assert store.get_asset(db, aid, other) is None  # IDOR


def test_claim_next_item_is_atomic(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-c@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        assert store.has_claimable_item(db) is True
        claimed = store.claim_next_item(db)
        assert claimed["id"] == item_id and claimed["status"] == store.ITEM_PROCESSING
        assert store.claim_next_item(db) is None
        assert store.has_claimable_item(db) is False


def test_recompute_run_status(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-d@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        assert store.recompute_run_status(db, run_id) == store.STATUS_PROCESSING
        store.add_ready_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                              full_url="https://cdn/x.jpg", thumb_url=None)
        store.set_item_status(db, item_id, store.ITEM_DONE)
        assert store.recompute_run_status(db, run_id) == store.STATUS_READY

        run2 = store.create_run(db, uid)
        it2 = store.add_item(db, run_id=run2, user_id=uid, source_url="x", source_type="unknown")
        store.set_item_status(db, it2, store.ITEM_ERROR, error="nope")
        assert store.recompute_run_status(db, run2) == store.STATUS_FAILED


def test_reclaim_orphaned_items(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-e@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        store.claim_next_item(db)  # -> processing (orphaned by a "crash")
        assert store.reclaim_orphaned_items(db) == 1
        assert store.items_for_run(db, run_id, uid)[0]["status"] == store.ITEM_ERROR
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_FAILED


# --- acquire orchestrator (resolve-only, no download) -----------------------

def test_process_item_records_resolved_images(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-f@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        item = store.claim_next_item(db)

        refs = [
            {"full": "https://i5.walmartimages.com/a.jpg",
             "thumb": "https://i5.walmartimages.com/a.jpg?odnHeight=300",
             "width": None, "height": None},
            {"full": "https://i5.walmartimages.com/b.jpg",
             "thumb": "https://i5.walmartimages.com/b.jpg?odnHeight=300",
             "width": None, "height": None},
        ]
        monkeypatch.setattr(acquire.sources, "resolve_images",
                            lambda url, st: ("My Product", refs))

        acquire.process_item(db, item)

        assets = store.assets_for_run(db, run_id, uid)
        assert len(assets) == 2
        assert all(a["status"] == store.STATUS_READY for a in assets)
        assert assets[0]["source_image_url"] == "https://i5.walmartimages.com/a.jpg"
        assert "odnHeight" in assets[0]["thumb_url"]
        done = store.items_for_run(db, run_id, uid)[0]
        assert done["status"] == store.ITEM_DONE and done["label"] == "My Product"
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_READY


def test_process_item_marks_error_on_source_error(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-h@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid, url="https://www.amazon.com/dp/B0",
                                         stype="amazon_pdp")
        item = store.claim_next_item(db)
        monkeypatch.setattr(
            acquire.sources, "resolve_images",
            lambda url, st: (_ for _ in ()).throw(sources.SourceError("blocked")))
        acquire.process_item(db, item)
        row = store.items_for_run(db, run_id, uid)[0]
        assert row["status"] == store.ITEM_ERROR and "blocked" in row["error"]
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_FAILED


def test_process_item_no_images_is_error(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-i@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        item = store.claim_next_item(db)
        monkeypatch.setattr(acquire.sources, "resolve_images", lambda url, st: ("x", []))
        acquire.process_item(db, item)
        assert store.items_for_run(db, run_id, uid)[0]["status"] == store.ITEM_ERROR
