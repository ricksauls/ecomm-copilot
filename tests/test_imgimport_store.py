"""Tests for the Import Product Images data layer (runs/items/assets + queue) and
the acquire orchestrator."""

from app.db import get_db
from app.imgimport import acquire, sources
from app.imgimport import store
from app.users import create_local_user


def _run_with_item(db, uid, url="https://www.walmart.com/ip/10294528", stype=store.ITEM_QUEUED):
    run_id = store.create_run(db, uid)
    item_id = store.add_item(db, run_id=run_id, user_id=uid, source_url=url, source_type="walmart")
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

        aid = store.create_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                                 source_image_url="https://cdn/x.jpg")
        store.finalize_asset(db, aid, stored_path="import/1/1.jpg", ext="jpg",
                             width=2000, height=1500, upscaled=False)
        assets = store.assets_for_run(db, run_id, uid)
        assert len(assets) == 1 and assets[0]["status"] == store.STATUS_READY
        assert store.get_asset(db, aid, other) is None  # IDOR


def test_claim_next_item_is_atomic(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-c@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        assert store.has_claimable_item(db) is True
        claimed = store.claim_next_item(db)
        assert claimed["id"] == item_id and claimed["status"] == store.ITEM_PROCESSING
        # A second claim finds nothing (the only item is now processing).
        assert store.claim_next_item(db) is None
        assert store.has_claimable_item(db) is False


def test_recompute_run_status(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-d@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid)
        # Still queued → processing.
        assert store.recompute_run_status(db, run_id) == store.STATUS_PROCESSING
        # Item done with a ready asset → ready.
        aid = store.create_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                                 source_image_url="https://cdn/x.jpg")
        store.finalize_asset(db, aid, stored_path="import/1/1.jpg", ext="jpg",
                             width=1000, height=1000, upscaled=False)
        store.set_item_status(db, item_id, store.ITEM_DONE)
        assert store.recompute_run_status(db, run_id) == store.STATUS_READY

        # A run whose only item errored with no ready assets → failed.
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
        row = store.items_for_run(db, run_id, uid)[0]
        assert row["status"] == store.ITEM_ERROR
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_FAILED


# --- acquire orchestrator ---------------------------------------------------

def test_process_item_acquires_and_finalizes(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-f@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid, url="https://cdn/x.jpg")
        item = store.claim_next_item(db)

        # Native image is already at/above target → no upscale; store returns a path.
        monkeypatch.setattr(acquire.sources, "resolve_image_urls",
                            lambda url, st: ("My Product", ["https://cdn/x.jpg"]))
        monkeypatch.setattr(acquire.sources, "download_image",
                            lambda url: (b"BYTES", "jpg", 2400, 1800))
        monkeypatch.setattr(acquire.storage, "save",
                            lambda run, name, data, ext: f"import/{run}/{name}.{ext}")
        monkeypatch.setattr(acquire.image_enhance, "is_configured", lambda: True)

        acquire.process_item(db, item)

        assets = store.assets_for_run(db, run_id, uid)
        assert len(assets) == 1
        a = assets[0]
        assert a["status"] == store.STATUS_READY and a["upscaled"] == 0
        assert a["width"] == 2400 and a["height"] == 1800
        assert store.items_for_run(db, run_id, uid)[0]["status"] == store.ITEM_DONE
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_READY
        # Label from the source was recorded on the item.
        assert store.items_for_run(db, run_id, uid)[0]["label"] == "My Product"


def test_process_item_upscales_when_below_target(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-g@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid, url="https://cdn/small.jpg")
        item = store.claim_next_item(db)

        monkeypatch.setattr(acquire.sources, "resolve_image_urls",
                            lambda url, st: (None, ["https://cdn/small.jpg"]))
        # Native is small → below the 2000px target → should upscale.
        monkeypatch.setattr(acquire.sources, "download_image",
                            lambda url: (b"SMALL", "jpg", 800, 600))
        monkeypatch.setattr(acquire.sources, "_probe_image", lambda data: (2000, 1500, "png"))
        monkeypatch.setattr(acquire.storage, "save",
                            lambda run, name, data, ext: f"import/{run}/{name}.{ext}")
        monkeypatch.setattr(acquire.image_enhance, "is_configured", lambda: True)
        monkeypatch.setattr(acquire.image_enhance, "target_px", lambda: 2000)
        monkeypatch.setattr(acquire.image_enhance, "output_ext", lambda: "png")
        calls = []
        monkeypatch.setattr(acquire.image_enhance, "enhance",
                            lambda url, operation="upscale": calls.append(url) or b"BIG")

        acquire.process_item(db, item)

        assert calls == ["https://cdn/small.jpg"]  # upscale was invoked
        a = store.assets_for_run(db, run_id, uid)[0]
        assert a["status"] == store.STATUS_READY and a["upscaled"] == 1
        assert a["width"] == 2000 and a["ext"] == "png"


def test_process_item_marks_error_on_unsupported_source(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("imp-h@example.com", "password123")
        run_id, item_id = _run_with_item(db, uid, url="https://www.amazon.com/dp/B0")
        item = store.claim_next_item(db)
        monkeypatch.setattr(
            acquire.sources, "resolve_image_urls",
            lambda url, st: (_ for _ in ()).throw(sources.SourceNotSupported("not yet")))
        acquire.process_item(db, item)
        row = store.items_for_run(db, run_id, uid)[0]
        assert row["status"] == store.ITEM_ERROR and "not yet" in row["error"]
        assert store.get_run(db, run_id, uid)["status"] == store.STATUS_FAILED
