"""Tests for the Walmart URL prefill flow (draft fetch queue, worker, routes)."""

import io

from PIL import Image, ImageDraw

from app.db import get_db
from app.fetch import FetchError
from app.imageset import generate
from app.imageset import store as isstore
from app.scoring import PdpRecord
from app.users import create_local_user


def _record():
    return PdpRecord(
        url="https://www.walmart.com/ip/123", item_id="123",
        title="Red Pepper Sauce", brand="Demo Co", description="A bold sauce.",
        bullets=["Aged peppers", "Bold heat", "Gluten free"],
        main_image_url="https://i5.walmartimages.com/x.jpg",
    )


def _png() -> bytes:
    img = Image.new("RGB", (300, 480), (255, 255, 255))
    ImageDraw.Draw(img).rounded_rectangle([110, 70, 190, 430], radius=20, fill=(190, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- store: draft fetch queue ----------------------------------------------

def test_create_draft_and_claim_is_conditional(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("a@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="https://www.walmart.com/ip/1")
        p = isstore.get_project(db, pid, uid)
        assert p["status"] == isstore.STATUS_FETCHING and p["source_url"].endswith("/ip/1")
        assert isstore.has_claimable_fetch(db) is True
        claimed = isstore.claim_next_fetch(db)
        assert claimed["id"] == pid and claimed["status"] == isstore.STATUS_FETCHING_ACTIVE
        assert isstore.claim_next_fetch(db) is None  # nothing left to claim


def test_apply_record_and_bullets(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("b@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="u")
        isstore.apply_fetched_record(db, pid, name="N", brand="B", description="D")
        n = isstore.replace_features_from_bullets(db, pid, ["x", "y", "", "z"])
        assert n == 3  # blank skipped
        p = isstore.get_project(db, pid, uid)
        assert p["name"] == "N" and p["brand"] == "B" and p["description"] == "D"
        assert [f["feature_key"] for f in isstore.features_for_project(db, pid)] == ["f1", "f2", "f3"]


def test_reclaim_orphaned_fetch(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("c@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="u")
        isstore.claim_next_fetch(db)  # -> fetching_active
        assert isstore.reclaim_orphaned_fetches(db) == 1
        p = isstore.get_project(db, pid, uid)
        assert p["status"] == isstore.STATUS_DRAFT and p["error"]


# --- generate.run_prefill ---------------------------------------------------

def test_run_prefill_fills_fields(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="https://www.walmart.com/ip/123")
        monkeypatch.setattr(generate, "_download_product_image", lambda url: None)
        generate.run_prefill(db, isstore.get_project(db, pid, uid), fetch=lambda url, item: _record())
        p = isstore.get_project(db, pid, uid)
        assert p["name"] == "Red Pepper Sauce" and p["brand"] == "Demo Co"
        assert len(isstore.features_for_project(db, pid)) == 3


def test_run_prefill_stores_image_without_advancing(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("e@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="https://www.walmart.com/ip/123")
        isstore.claim_next_fetch(db)  # fetching_active (worker holds it)
        monkeypatch.setattr(generate, "_download_product_image", lambda url: _png())
        generate.run_prefill(db, isstore.get_project(db, pid, uid), fetch=lambda url, item: _record())
        p = isstore.get_project(db, pid, uid)
        assert p["original_path"]  # fetched image stored
        assert p["status"] == isstore.STATUS_FETCHING_ACTIVE  # run_prefill leaves status to the caller


# --- worker ----------------------------------------------------------------

def test_worker_fetch_success(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    import worker

    with app.app_context():
        db = get_db()
        uid = create_local_user("f@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="https://www.walmart.com/ip/123")
        claimed = isstore.claim_next_fetch(db)
        monkeypatch.setattr("app.fetch.fetch_pdp", lambda url, item=None: _record())
        monkeypatch.setattr(generate, "_download_product_image", lambda url: None)
        worker.process_imageset_fetch_one(db, claimed)
        p = isstore.get_project(db, pid, uid)
        assert p["status"] == isstore.STATUS_DRAFT and p["name"] == "Red Pepper Sauce"


def test_worker_fetch_failure_leaves_usable_draft(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    import worker

    with app.app_context():
        db = get_db()
        uid = create_local_user("g@example.com", "password123")
        pid = isstore.create_draft_for_url(db, user_id=uid, url="https://www.walmart.com/ip/123")
        claimed = isstore.claim_next_fetch(db)

        def boom(url, item=None):
            raise FetchError("blocked")

        monkeypatch.setattr("app.fetch.fetch_pdp", boom)
        worker.process_imageset_fetch_one(db, claimed)
        p = isstore.get_project(db, pid, uid)
        assert p["status"] == isstore.STATUS_DRAFT and p["error"]  # usable, with a note


# --- routes ----------------------------------------------------------------

def test_fetch_route_creates_draft(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    resp = client.post("/app/pdp-image-set/fetch",
                       data={"url": "https://www.walmart.com/ip/10294528"})
    assert resp.status_code == 302 and "/edit" in resp.headers["Location"]
    with client.application.app_context():
        p = isstore.list_projects(get_db(), 1)[0]
        assert p["status"] == isstore.STATUS_FETCHING


def test_fetch_route_rejects_bad_url(client, auth):
    auth.register()
    assert client.post("/app/pdp-image-set/fetch", data={"url": "nonsense"}).status_code == 400


def test_fetch_route_rejects_bare_prefix(client, auth):
    # The autofilled prefix with no item number must be rejected, not fetched.
    auth.register()
    resp = client.post("/app/pdp-image-set/fetch", data={"url": "https://www.walmart.com/ip/"})
    assert resp.status_code == 400


def test_intake_url_field_autofills_prefix(client, auth):
    auth.register()
    resp = client.get("/app/pdp-image-set")
    assert b'value="https://www.walmart.com/ip/"' in resp.data


def test_edit_shows_fetching_then_prefilled(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    client.post("/app/pdp-image-set/fetch", data={"url": "https://www.walmart.com/ip/123"})
    with client.application.app_context():
        pid = isstore.list_projects(get_db(), 1)[0]["id"]
    assert b"Fetching product details" in client.get(f"/app/pdp-image-set/{pid}/edit").data
    assert client.get(f"/app/pdp-image-set/{pid}/fetch-status").get_json()["fetching"] is True
    # Simulate the worker finishing the prefill.
    with client.application.app_context():
        db = get_db()
        monkeypatch.setattr(generate, "_download_product_image", lambda url: None)
        generate.run_prefill(db, isstore.get_project(db, pid, 1), fetch=lambda url, item: _record())
        isstore.finish_fetch(db, pid)
    page = client.get(f"/app/pdp-image-set/{pid}/edit")
    assert b"Red Pepper Sauce" in page.data  # prefilled into the form
    assert client.get(f"/app/pdp-image-set/{pid}/fetch-status").get_json()["fetching"] is False


def test_submit_from_prefilled_draft_reuses_image(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    client.post("/app/pdp-image-set/fetch", data={"url": "https://www.walmart.com/ip/123"})
    with client.application.app_context():
        db = get_db()
        pid = isstore.list_projects(db, 1)[0]["id"]
        monkeypatch.setattr(generate, "_download_product_image", lambda url: _png())
        generate.run_prefill(db, isstore.get_project(db, pid, 1), fetch=lambda url, item: _record())
        isstore.finish_fetch(db, pid)
    # Submit with project_id and NO upload → reuse the fetched image, run the cutout.
    resp = client.post("/app/pdp-image-set", data={
        "project_id": str(pid), "name": "Red Pepper Sauce", "category": "hot sauce", "unit": "in"})
    assert resp.status_code == 302 and "/cutout" in resp.headers["Location"]
    with client.application.app_context():
        assert isstore.get_project(get_db(), pid, 1)["cutout_path"]
