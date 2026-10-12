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
from app.seasonal import jobs as seasonal_jobs
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

def test_process_product_takes_main_image_and_auto_queues(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-a@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        # Stub the resolver (no network): a 2-image gallery, main first.
        monkeypatch.setattr(sources, "resolve_images", lambda url, stype: (
            "Tabasco Hot Sauce",
            [{"full": "https://cdn/main.jpg", "thumb": "https://cdn/main_t.jpg", "width": 1500, "height": 1500},
             {"full": "https://cdn/other.jpg", "thumb": "https://cdn/other_t.jpg", "width": None, "height": None}],
        ))
        product = store.claim_next_product(db)
        generate.process_product(db, product)
        settled = store.get_product(db, pid, uid)
        assert settled["status"] == store.PRODUCT_DONE
        assert settled["label"] == "Tabasco Hot Sauce"
        # Only the MAIN image (refs[0]) is recorded — not the rest of the gallery.
        images = store.images_for_campaign(db, cid, uid)
        assert len(images) == 1
        assert images[0]["full_url"] == "https://cdn/main.jpg"
        # And it's auto-queued for generation (no selection step).
        assert images[0]["gen_status"] == store.GEN_QUEUED
        assert seasonal_jobs.has_claimable_jobs(db) is True


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
    return store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                    full_url="https://cdn/a.jpg", thumb_url="https://cdn/a_t.jpg")


def test_edit_mode_generates_final_2000_no_cutout(app, tmp_path, monkeypatch):
    # Default render mode is 'edit' — the model places the product, so there is no
    # separate cutout artifact. Produces a ready 2000px final + thumbnail.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.delenv("SEASONAL_RENDER_MODE", raising=False)
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-d@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        iid = _acquired_image(db, uid, cid, pid)
        monkeypatch.setattr(sources, "download_image",
                            lambda url: (_product_png(), "png", 400, 640))
        generate.process_image(db, store.get_image(db, iid, uid),
                               store.get_campaign(db, cid, uid))
        done = store.get_image(db, iid, uid)
        assert done["gen_status"] == store.GEN_READY
        assert done["final_path"] and done["thumb_path"]
        assert done["cutout_path"] is None  # edit mode does no background removal
        from app.seasonal import storage
        final = Image.open(io.BytesIO(storage.load(done["final_path"])))
        assert final.size == (CANVAS_SIZE, CANVAS_SIZE)
        assert store.get_campaign(db, cid, uid)["status"] == store.CAMPAIGN_READY


def test_composite_mode_generates_final_2000_with_cutout(app, tmp_path, monkeypatch):
    # The composite fallback cuts the product out, so a cutout artifact is stored.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.setenv("SEASONAL_RENDER_MODE", "composite")
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-d2@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid)
        iid = _acquired_image(db, uid, cid, pid)
        monkeypatch.setattr(sources, "download_image",
                            lambda url: (_product_png(), "png", 400, 640))
        generate.process_image(db, store.get_image(db, iid, uid),
                               store.get_campaign(db, cid, uid))
        done = store.get_image(db, iid, uid)
        assert done["gen_status"] == store.GEN_READY
        assert done["final_path"] and done["thumb_path"] and done["cutout_path"]
        from app.seasonal import storage
        final = Image.open(io.BytesIO(storage.load(done["final_path"])))
        assert final.size == (CANVAS_SIZE, CANVAS_SIZE)


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


def test_edit_mode_prompt_uses_custom_scene_and_preserves_product(app, tmp_path, monkeypatch):
    # In edit mode the product image is sent to edit_image with a prompt that carries
    # the custom scene AND a product-preservation instruction.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    monkeypatch.delenv("SEASONAL_RENDER_MODE", raising=False)
    with app.app_context():
        db = get_db()
        uid = create_local_user("seag-f@example.com", "password123")
        cid, pid = _campaign_with_product(db, uid, theme="custom",
                                          custom_prompt="A Diwali table with marigolds")
        iid = _acquired_image(db, uid, cid, pid)
        monkeypatch.setattr(sources, "download_image",
                            lambda url: (_product_png(), "png", 400, 640))
        captured = {}
        from app.imageset.providers import image_generation as igp
        real = igp.MockImageGenerationProvider.edit_image

        def _spy(self, *, prompt, image, **kw):
            captured["prompt"] = prompt
            captured["image"] = image
            return real(self, prompt=prompt, image=image, **kw)

        monkeypatch.setattr(igp.MockImageGenerationProvider, "edit_image", _spy)
        generate.process_image(db, store.get_image(db, iid, uid),
                               store.get_campaign(db, cid, uid))
        assert "marigolds" in captured["prompt"]
        # The real product image is handed to the model, with a preservation rule.
        assert captured["image"]
        assert "exactly as shown" in captured["prompt"].lower()
