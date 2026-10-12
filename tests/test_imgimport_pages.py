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


def test_acquired_history_lists_finished_runs(client, auth, app):
    auth.register(email="imp-hist@example.com")
    full = "https://m.media-amazon.com/images/I/71abc.jpg"
    run_id, aids, uid = _seed_ready_run(app, "imp-hist@example.com", [full])

    resp = client.get("/app/activity/acquired")
    assert resp.status_code == 200
    # The run's product label and a link back to its gallery both show.
    assert b"Thing" in resp.data
    assert f"/app/import-images/{run_id}".encode() in resp.data
    # Thumbnail is hotlinked, so the page relaxes CSP img-src like the gallery.
    assert "img-src 'self' data: https:" in resp.headers["Content-Security-Policy"]
    assert (full + "?thumb").encode() in resp.data


def test_acquired_history_empty_when_no_finished_runs(client, auth, app):
    auth.register(email="imp-hist-empty@example.com")
    # A still-processing run (no ready assets) must not appear as history.
    with app.app_context():
        db = get_db()
        uid = _uid(db, "imp-hist-empty@example.com")
        imp_store.create_run(db, uid)
    resp = client.get("/app/activity/acquired")
    assert resp.status_code == 200
    assert b"No images acquired yet." in resp.data


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


def test_product_id_and_slug_helpers():
    from app.routes import pages
    assert pages._import_product_id(
        {"source_url": "https://www.amazon.com/dp/B07NV281TK", "source_type": "amazon_pdp"}
    ) == "B07NV281TK"
    assert pages._import_product_id(
        {"source_url": "https://www.walmart.com/ip/10294528", "source_type": "walmart"}
    ) == "10294528"
    assert pages._import_product_id(
        {"source_url": "https://cdn/x.jpg", "source_type": "image"}) is None
    assert pages._import_slug("OFF! Deep Woods — Spray") == "off-deep-woods-spray"
    assert pages._import_download_stem(
        {"label": "OFF! Deep Woods", "source_url": "https://www.amazon.com/dp/B07NV281TK",
         "source_type": "amazon_pdp"}, 2) == "off-deep-woods-B07NV281TK-2"


def test_download_filename_uses_product_name(client, auth, app, monkeypatch):
    auth.register(email="imp-name@example.com")
    run_id, aids, uid = _seed_ready_run(app, "imp-name@example.com",
                                        ["https://m.media-amazon.com/images/I/71abc.jpg"])
    monkeypatch.setattr("app.imgimport.sources.download_image", lambda u: _fake_download(u))
    dl = client.get(f"/app/import-images/{run_id}/asset/{aids[0]}/download")
    # Item label is "Thing" (from _seed_ready_run) → filename starts with its slug.
    assert "thing-1.jpg" in dl.headers.get("Content-Disposition", "")


def test_download_upscales_when_below_target(client, auth, app, monkeypatch):
    auth.register(email="imp-up@example.com")
    run_id, aids, uid = _seed_ready_run(app, "imp-up@example.com",
                                        ["https://m.media-amazon.com/images/I/71abc.jpg"])
    # Native image is small → below the 2000px target → should upscale via Claid.
    monkeypatch.setattr("app.imgimport.sources.download_image",
                        lambda u: (b"SMALL", "jpg", 800, 600))
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    monkeypatch.setattr("app.image_enhance.target_px", lambda: 2000)
    monkeypatch.setattr("app.image_enhance.output_ext", lambda: "png")
    monkeypatch.setattr("app.image_enhance.enhance",
                        lambda url, operation="upscale": b"UPSCALED")
    dl = client.get(f"/app/import-images/{run_id}/asset/{aids[0]}/download")
    assert dl.status_code == 200 and dl.data == b"UPSCALED" and dl.mimetype == "image/png"


def test_download_skips_upscale_when_large(client, auth, app, monkeypatch):
    auth.register(email="imp-noup@example.com")
    run_id, aids, uid = _seed_ready_run(app, "imp-noup@example.com",
                                        ["https://m.media-amazon.com/images/I/71abc.jpg"])
    # Already at/above target → serve native, never call Claid.
    monkeypatch.setattr("app.imgimport.sources.download_image",
                        lambda u: (b"BIG", "jpg", 2400, 2400))
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    monkeypatch.setattr("app.image_enhance.target_px", lambda: 2000)

    def _boom(*a, **k):
        raise AssertionError("enhance must not be called for a large image")
    monkeypatch.setattr("app.image_enhance.enhance", _boom)
    dl = client.get(f"/app/import-images/{run_id}/asset/{aids[0]}/download")
    assert dl.status_code == 200 and dl.data == b"BIG"


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
