"""Route tests for Import Product Images (intake → gallery hotlink → on-demand download)."""

import io
import zipfile

from app.db import get_db
from app.imgimport import store as imp_store


def _uid(db, email):
    return db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def _fake_download(url):
    """Stand-in for sources.download_image — returns tiny bytes + ext, no network."""
    return (b"IMGBYTES", "jpg", 1200, 900)


def test_intake_requires_login(client):
    resp = client.get("/app/import-images")
    assert resp.status_code in (302, 401)


def test_intake_get_renders(client, auth):
    auth.register()
    resp = client.get("/app/import-images")
    assert resp.status_code == 200
    assert b"Acquire Product Images From An Existing Site" in resp.data
    assert b'name="urls"' in resp.data and b'name="csv"' in resp.data


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
        assert sorted(i["source_type"] for i in items) == ["amazon_pdp", "image", "walmart"]


def test_intake_post_no_valid_urls_is_400(client, auth):
    auth.register(email="imp-empty@example.com")
    resp = client.post("/app/import-images", data={"urls": "nonsense\nalso nonsense"})
    assert resp.status_code == 400
    assert b"No valid product or image URLs" in resp.data


def _seed_ready_run(app, email, urls):
    """Create a run + one done item with ready URL-only assets; return (run_id, [aids], uid)."""
    with app.app_context():
        db = get_db()
        uid = _uid(db, email)
        run_id = imp_store.create_run(db, uid)
        item_id = imp_store.add_item(db, run_id=run_id, user_id=uid,
                                     source_url="https://cdn/x", source_type="image")
        imp_store.set_item_status(db, item_id, imp_store.ITEM_DONE, label="Thing")
        aids = [imp_store.add_ready_asset(db, item_id=item_id, run_id=run_id, user_id=uid,
                                          full_url=u, thumb_url=u + "?thumb")
                for u in urls]
        imp_store.recompute_run_status(db, run_id)
    return run_id, aids, uid


def test_gallery_hotlinks_thumbnails(client, auth, app):
    auth.register(email="imp-gal@example.com")
    full = "https://m.media-amazon.com/images/I/71abc.jpg"
    run_id, aids, uid = _seed_ready_run(app, "imp-gal@example.com", [full])

    gal = client.get(f"/app/import-images/{run_id}")
    assert gal.status_code == 200
    assert b"Thing" in gal.data and b"Download Selected" in gal.data
    # The gallery hotlinks the external thumbnail (no same-origin image-serve route).
    assert (full + "?thumb").encode() in gal.data
    # And the page relaxes CSP img-src so those CDN images load.
    assert "img-src 'self' data: https:" in gal.headers["Content-Security-Policy"]


def test_download_fetches_on_demand(client, auth, app, monkeypatch):
    auth.register(email="imp-dl@example.com")
    run_id, aids, uid = _seed_ready_run(app, "imp-dl@example.com",
                                        ["https://m.media-amazon.com/images/I/71abc.jpg"])
    monkeypatch.setattr("app.imgimport.sources.download_image", lambda u: _fake_download(u))
    dl = client.get(f"/app/import-images/{run_id}/asset/{aids[0]}/download")
    assert dl.status_code == 200 and dl.mimetype == "image/jpeg"
    assert "attachment" in dl.headers.get("Content-Disposition", "")
    assert dl.data == b"IMGBYTES"


def test_zip_respects_selection(client, auth, app, monkeypatch):
    auth.register(email="imp-sel@example.com")
    urls = [f"https://m.media-amazon.com/images/I/{n}.jpg" for n in ("a", "b", "c")]
    run_id, aids, uid = _seed_ready_run(app, "imp-sel@example.com", urls)
    monkeypatch.setattr("app.imgimport.sources.download_image", lambda u: _fake_download(u))

    z = client.get(f"/app/import-images/{run_id}/download.zip"
                   f"?scope=selected&ids={aids[0]}&ids={aids[2]}")
    assert z.status_code == 200 and z.mimetype == "application/zip"
    with zipfile.ZipFile(io.BytesIO(z.data)) as zf:
        assert len(zf.namelist()) == 2

    assert client.get(
        f"/app/import-images/{run_id}/download.zip?scope=selected").status_code == 404

    zall = client.get(f"/app/import-images/{run_id}/download.zip")
    with zipfile.ZipFile(io.BytesIO(zall.data)) as zf:
        assert len(zf.namelist()) == 3


def test_status_json(client, auth, app):
    auth.register(email="imp-status@example.com")
    run_id, aids, uid = _seed_ready_run(app, "imp-status@example.com",
                                        ["https://m.media-amazon.com/images/I/71abc.jpg"])
    body = client.get(f"/app/import-images/{run_id}/status").get_json()
    assert body["pending"] is False and body["status"] == "ready"
    assert body["items"][0]["assets"][0]["ready"] is True


def test_idor_other_user_cannot_access(client, auth, app):
    auth.register(email="imp-owner@example.com")
    run_id, aids, uid = _seed_ready_run(app, "imp-owner@example.com",
                                        ["https://m.media-amazon.com/images/I/71abc.jpg"])
    auth.logout()
    auth.register(email="imp-intruder@example.com")
    assert client.get(f"/app/import-images/{run_id}").status_code == 404
    assert client.get(
        f"/app/import-images/{run_id}/asset/{aids[0]}/download").status_code == 404
    assert client.get(f"/app/import-images/{run_id}/download.zip").status_code == 404
