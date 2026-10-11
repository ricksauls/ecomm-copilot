"""Route tests for Import Product Images (intake → gallery → serve/download/zip)."""

import io
import zipfile

from PIL import Image

from app.db import get_db
from app.imgimport import storage as imp_storage
from app.imgimport import store as imp_store


def _png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (120, 90), (30, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _uid(db, email):
    return db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def test_intake_requires_login(client):
    resp = client.get("/app/import-images")
    assert resp.status_code in (302, 401)


def test_intake_get_renders(client, auth):
    auth.register()
    resp = client.get("/app/import-images")
    assert resp.status_code == 200
    assert b"Import Product Images" in resp.data
    assert b'name="urls"' in resp.data  # the textarea
    assert b'name="csv"' in resp.data   # CSV upload


def test_intake_post_creates_classified_items(client, auth, app):
    auth.register(email="imp-intake@example.com")
    urls = "\n".join([
        "https://www.walmart.com/ip/10294528",
        "https://m.media-amazon.com/images/I/71abc._AC_SL1500_.jpg",
        "https://www.amazon.com/dp/B07XYZ",
        "not a url",
    ])
    resp = client.post("/app/import-images", data={"urls": urls})
    assert resp.status_code == 302
    assert "/app/import-images/" in resp.headers["Location"]

    with app.app_context():
        db = get_db()
        uid = _uid(db, "imp-intake@example.com")
        run = db.execute("SELECT id FROM import_runs WHERE user_id = ?", (uid,)).fetchone()
        items = imp_store.items_for_run(db, run["id"], uid)
        # The three valid URLs became items; the junk line was dropped.
        types = sorted(i["source_type"] for i in items)
        assert types == ["amazon_pdp", "image", "walmart"]


def test_intake_post_no_valid_urls_is_400(client, auth):
    auth.register(email="imp-empty@example.com")
    resp = client.post("/app/import-images", data={"urls": "nonsense\nalso nonsense"})
    assert resp.status_code == 400
    assert b"No valid product or image URLs" in resp.data


def _seed_ready_run(app, email, tmp_path, monkeypatch):
    """Create a run with one ready, file-backed image asset; return (run_id, aid, uid)."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = _uid(db, email)
        run_id = imp_store.create_run(db, uid)
        item_id = imp_store.add_item(db, run_id=run_id, user_id=uid,
                                     source_url="https://cdn/x.png", source_type="image")
        imp_store.set_item_status(db, item_id, imp_store.ITEM_DONE, label="Thing")
        aid = imp_store.create_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                                     source_image_url="https://cdn/x.png")
        rel = imp_storage.save(run_id, aid, _png(), "png")
        assert rel is not None
        imp_store.finalize_asset(db, aid, stored_path=rel, ext="png",
                                 width=120, height=90, upscaled=True)
        imp_store.recompute_run_status(db, run_id)
    return run_id, aid, uid


def test_gallery_and_serve_and_download(client, auth, app, tmp_path, monkeypatch):
    auth.register(email="imp-gal@example.com")
    run_id, aid, uid = _seed_ready_run(app, "imp-gal@example.com", tmp_path, monkeypatch)

    # Gallery renders the item + an "upscaled" badge + the selection controls.
    gal = client.get(f"/app/import-images/{run_id}")
    assert gal.status_code == 200
    assert b"Thing" in gal.data and b"upscaled" in gal.data
    assert b"Download Selected" in gal.data
    assert b'name="ids"' in gal.data  # per-image selection checkbox

    # Inline serve returns the stored PNG.
    img = client.get(f"/app/import-images/{run_id}/asset/{aid}/image")
    assert img.status_code == 200 and img.mimetype == "image/png"

    # Download is an attachment.
    dl = client.get(f"/app/import-images/{run_id}/asset/{aid}/download")
    assert dl.status_code == 200
    assert "attachment" in dl.headers.get("Content-Disposition", "")

    # ZIP bundles the ready image.
    z = client.get(f"/app/import-images/{run_id}/download.zip")
    assert z.status_code == 200 and z.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(z.data)) as zf:
        assert len(zf.namelist()) == 1


def test_zip_respects_selection(client, auth, app, tmp_path, monkeypatch):
    # The ZIP includes only the ticked images when a selection is submitted; a bare
    # hit (no scope) still bundles everything; an empty selection is a 404.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register(email="imp-sel@example.com")
    with app.app_context():
        db = get_db()
        uid = _uid(db, "imp-sel@example.com")
        run_id = imp_store.create_run(db, uid)
        item_id = imp_store.add_item(db, run_id=run_id, user_id=uid,
                                     source_url="https://cdn/x.png", source_type="image")
        imp_store.set_item_status(db, item_id, imp_store.ITEM_DONE)
        aids = []
        for _ in range(3):
            aid = imp_store.create_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                                         source_image_url="https://cdn/x.png")
            rel = imp_storage.save(run_id, aid, _png(), "png")
            imp_store.finalize_asset(db, aid, stored_path=rel, ext="png",
                                     width=120, height=90, upscaled=False)
            aids.append(aid)
        imp_store.recompute_run_status(db, run_id)

    # Only the two selected ids are bundled.
    z = client.get(f"/app/import-images/{run_id}/download.zip"
                   f"?scope=selected&ids={aids[0]}&ids={aids[2]}")
    assert z.status_code == 200
    with zipfile.ZipFile(io.BytesIO(z.data)) as zf:
        assert len(zf.namelist()) == 2

    # A selection with nothing ticked is a 404 (not "download everything").
    assert client.get(
        f"/app/import-images/{run_id}/download.zip?scope=selected").status_code == 404

    # A bare hit (no scope, e.g. a bookmark) still bundles all three.
    zall = client.get(f"/app/import-images/{run_id}/download.zip")
    with zipfile.ZipFile(io.BytesIO(zall.data)) as zf:
        assert len(zf.namelist()) == 3


def test_status_json(client, auth, app, tmp_path, monkeypatch):
    auth.register(email="imp-status@example.com")
    run_id, aid, uid = _seed_ready_run(app, "imp-status@example.com", tmp_path, monkeypatch)
    resp = client.get(f"/app/import-images/{run_id}/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["pending"] is False and body["status"] == "ready"
    assert len(body["items"]) == 1 and body["items"][0]["assets"][0]["ready"] is True


def test_idor_other_user_cannot_access(client, auth, app, tmp_path, monkeypatch):
    auth.register(email="imp-owner@example.com")
    run_id, aid, uid = _seed_ready_run(app, "imp-owner@example.com", tmp_path, monkeypatch)
    auth.logout()
    auth.register(email="imp-intruder@example.com")
    assert client.get(f"/app/import-images/{run_id}").status_code == 404
    assert client.get(f"/app/import-images/{run_id}/asset/{aid}/image").status_code == 404
    assert client.get(f"/app/import-images/{run_id}/download.zip").status_code == 404
