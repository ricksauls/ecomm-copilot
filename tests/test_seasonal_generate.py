"""End-to-end tests for the seasonal pipelines: acquire + generation.

Exercises the real pipelines against the offline mock image/background providers
(selected automatically when no API keys are set) with a temporary MEDIA_DIR, so
finals are actually composited and written to disk — no network, no keys. The
external resolver/downloader (app.imgimport.sources) is stubbed so nothing leaves
the box.
"""

import io

from PIL import Image, ImageDraw

from app.db import get_db
from app.imageset.config import CANVAS_SIZE
from app.imgimport import sources
from app.seasonal import generate, store
from app.users import create_local_user


def _product_png() -> bytes:
    """A simple opaque product image (what download_image would return)."""
    img = Image.new("RGB", (400, 640), (255, 255, 255))
    ImageDraw.Draw(img).rounded_rectangle([140, 90, 260, 600], radius=30, fill=(190, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _campaign_with_product(db, uid, **kw):
    cid = store.create_campaign(db, user_id=uid, name="Holiday", theme=kw.get("theme", "christmas"),
                                custom_prompt=kw.get("custom_prompt", ""))
    pid = store.add_product(db, campaign_id=cid, user_id=uid,
                            source_url="https://www.walmart.com/ip/10294528",
                            source_type="walmart")
    return cid, pid


# --- ACQUIRE phase ----------------------------------------------------------

def test_process_product_records_gallery(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-a@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        # Stub the resolver (no network): a 2-image gallery with a title.
        monkeypatch.setattr(sources, "resolve_images", lambda url, stype: (
            "Tabasco Hot Sauce",
            [{"full": "https://cdn/a.jpg", "thumb": "https://cdn/a_t.jpg", "width": 1500, "height": 1500},
             {"full": "https://cdn/b.jpg", "thumb": "https://cdn/b_t.jpg", "width": None, "height": None}],
        ))
        product = store.claim_next_product(db)
        generate.process_product(db, product)
        settled = store.get_product(db, pid, uid)
        assert settled["status"] == store.PRODUCT_DONE
        assert settled["label"] == "Tabasco Hot Sauce"
        images = store.images_for_campaign(db, cid, uid)
        assert len(images) == 2
        assert all(im["gen_status"] == store.GEN_ACQUIRED for im in images)
        assert images[0]["full_url"] == "https://cdn/a.jpg"


def test_process_product_marks_error_on_source_failure(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-b@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)

        def _boom(url, stype):
            raise sources.SourceError("Walmart blocked the request.")

        monkeypatch.setattr(sources, "resolve_images", _boom)
        product = store.claim_next_product(db)
        generate.process_product(db, product)  # must not raise
        settled = store.get_product(db, pid, uid)
        assert settled["status"] == store.PRODUCT_ERROR
        assert "blocked" in (settled["error"] or "")
        assert store.images_for_campaign(db, cid, uid) == []


def test_process_product_no_images_is_error(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-c@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        monkeypatch.setattr(sources, "resolve_images", lambda url, stype: ("Empty", []))
        generate.process_product(db, store.claim_next_product(db))
        assert store.get_product(db, pid, uid)["status"] == store.PRODUCT_ERROR


# --- GENERATE phase ---------------------------------------------------------

def _acquired_image(db, uid, cid, pid):
    return store.add_acquired_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                    full_url="https://cdn/a.jpg", thumb_url="https://cdn/a_t.jpg")


def test_process_image_generates_final_2000(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-d@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        iid = _acquired_image(db, uid, cid, pid)
        # Stub the full-res download (no network): returns our product PNG.
        monkeypatch.setattr(sources, "download_image",
                            lambda url: (_product_png(), "png", 400, 640))
        image = store.get_image(db, iid, uid)
        campaign = store.get_campaign(db, cid, uid)
        generate.process_image(db, image, campaign)

        done = store.get_image(db, iid, uid)
        assert done["gen_status"] == store.GEN_READY
        assert done["final_path"] and done["thumb_path"] and done["cutout_path"]
        # The composited final is the full 2000px square canvas.
        from app.seasonal import storage
        final = Image.open(io.BytesIO(storage.load(done["final_path"])))
        assert final.size == (CANVAS_SIZE, CANVAS_SIZE)
        # The campaign rolled up to ready.
        assert store.get_campaign(db, cid, uid)["status"] == store.CAMPAIGN_READY


def test_process_image_download_failure_raises_generation_error(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-e@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        iid = _acquired_image(db, uid, cid, pid)

        def _boom(url):
            raise sources.SourceError("That URL is not an allowed public image address.")

        monkeypatch.setattr(sources, "download_image", _boom)
        image = store.get_image(db, iid, uid)
        campaign = store.get_campaign(db, cid, uid)
        try:
            generate.process_image(db, image, campaign)
        except generate.GenerationError as e:
            assert "allowed public image" in str(e)
        else:
            raise AssertionError("expected GenerationError on a blocked download")


def test_process_image_custom_theme_uses_user_scene(app, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-f@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid, theme="custom",
                                          custom_prompt="A Diwali table with marigolds")
        iid = _acquired_image(db, uid, cid, pid)
        monkeypatch.setattr(sources, "download_image",
                            lambda url: (_product_png(), "png", 400, 640))
        # Capture the prompt the image provider receives.
        captured = {}
        from app.imageset.providers import image_generation as igp
        real = igp.MockImageGenerationProvider.generate_image

        def _spy(self, *, prompt, **kw):
            captured["prompt"] = prompt
            return real(self, prompt=prompt, **kw)

        monkeypatch.setattr(igp.MockImageGenerationProvider, "generate_image", _spy)
        generate.process_image(db, store.get_image(db, iid, uid),
                               store.get_campaign(db, cid, uid))
        assert "marigolds" in captured["prompt"]
