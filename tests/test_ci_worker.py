"""Tests for the worker's CI run processor (browser stubbed out)."""

import io

import pytest

import worker
from app import ci_config, ci_images, ci_jobs, ci_scraper
from app.db import get_db
from app.fetch import FetchBlocked
from app.users import create_local_user


@pytest.fixture(autouse=True)
def _no_image_fetch(monkeypatch):
    """Stub the product-image fetch so process_ci_run never launches a browser.

    Image caching runs inside process_ci_run; the browser-driven URL lookup is
    irrelevant to the keyword-sweep tests, so default it to a no-op here.
    """
    monkeypatch.setattr(worker, "fetch_main_image_url", lambda *a, **k: None)


def _png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (80, 80), (10, 10, 10)).save(buf, "PNG")
    return buf.getvalue()


def _seed_group(db, *, keywords=("hot sauce",)):
    uid = create_local_user("w@example.com", "password123")
    gid = ci_config.create_group(db, uid, "G")
    bid = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
    ci_config.add_product(db, gid, bid, uid, "https://www.walmart.com/ip/x/10294528")
    for kw in keywords:
        ci_config.add_keyword(db, gid, uid, kw)
    return uid, gid, bid


def test_run_scrapes_keyword_and_writes_results(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid, gid, bid = _seed_group(db)

        # Stub the browser scrape: one matching card (mine) + one "other", plus a
        # headline ad for the tracked brand (no image bytes -> row without a thumb).
        def fake_scrape(keyword, **kw):
            return {
                "cards": [
                    {"name": "Tabasco", "item_id": "10294528", "listing_type": "organic", "product_url": ""},
                    {"name": "Other", "item_id": "999", "listing_type": "sponsored", "product_url": ""},
                ],
                "ads": [{"ad_type": "headline", "brand_text": "Tabasco", "image_bytes": None}],
            }
        monkeypatch.setattr(ci_scraper, "scrape_keyword_page", fake_scrape)
        monkeypatch.setattr(ci_scraper, "INTER_KEYWORD_DELAY_S", (0, 0))

        rid = ci_jobs.enqueue_run(db, gid, "one_time")
        run = ci_jobs.claim_next_run(db)
        worker.process_ci_run(db, run)

        assert ci_jobs.get_run(db, rid, uid)["status"] == "done"
        assert db.execute("SELECT COUNT(*) FROM ci_search_results WHERE run_id=?", (rid,)).fetchone()[0] == 2
        sos = {r["brand_id"]: r for r in db.execute(
            "SELECT * FROM ci_share_of_search WHERE run_id=?", (rid,)).fetchall()}
        assert sos[bid]["organic_count"] == 1
        assert sos[None]["sponsored_count"] == 1
        # The headline ad was recorded and attributed to the tracked brand.
        ad = db.execute("SELECT * FROM ci_ad_units WHERE run_id=?", (rid,)).fetchone()
        assert ad["ad_type"] == "headline" and ad["brand_id"] == bid


def test_run_with_all_keywords_blocked_is_marked_error(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid, gid, _ = _seed_group(db)

        def blocked(keyword, **kw):
            raise FetchBlocked("bot wall")
        monkeypatch.setattr(ci_scraper, "scrape_keyword_page", blocked)

        rid = ci_jobs.enqueue_run(db, gid)
        worker.process_ci_run(db, ci_jobs.claim_next_run(db))
        assert ci_jobs.get_run(db, rid, uid)["status"] == "error"


def test_run_with_no_keywords_finishes_done(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid = create_local_user("nokw@example.com", "password123")
        gid = ci_config.create_group(db, uid, "Empty")
        rid = ci_jobs.enqueue_run(db, gid)
        worker.process_ci_run(db, ci_jobs.claim_next_run(db))
        assert ci_jobs.get_run(db, rid, uid)["status"] == "done"


def test_one_bad_keyword_does_not_sink_the_run(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid, gid, _ = _seed_group(db, keywords=("good", "bad"))

        def flaky(keyword, **kw):
            if keyword == "bad":
                raise FetchBlocked("blocked")
            return {"cards": [{"name": "X", "item_id": "1", "listing_type": "organic",
                              "product_url": ""}], "ads": []}
        monkeypatch.setattr(ci_scraper, "scrape_keyword_page", flaky)
        monkeypatch.setattr(ci_scraper, "INTER_KEYWORD_DELAY_S", (0, 0))

        rid = ci_jobs.enqueue_run(db, gid)
        worker.process_ci_run(db, ci_jobs.claim_next_run(db))
        # Run still done (one keyword succeeded); only the good keyword's card stored.
        assert ci_jobs.get_run(db, rid, uid)["status"] == "done"
        assert db.execute("SELECT COUNT(*) FROM ci_search_results WHERE run_id=?", (rid,)).fetchone()[0] == 1


def test_cache_ci_product_images_fetches_uncached_only(monkeypatch, tmp_path):
    # Directly exercise the image-cache helper: a product with a cached image is
    # skipped; an uncached one is fetched (URL) and handed to the cache.
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    ci_images.save_product_image("111", _png_bytes())  # pre-cached

    monkeypatch.setattr(worker, "fetch_main_image_url", lambda url, iid: f"http://img/{iid}")
    monkeypatch.setattr(ci_scraper, "INTER_KEYWORD_DELAY_S", (0, 0))
    cached = []
    monkeypatch.setattr(worker.ci_images, "cache_product_image_from_url",
                        lambda iid, url: cached.append((iid, url)) or True)

    item_map = {
        "111": {"walmart_url": "https://www.walmart.com/ip/x/111"},
        "222": {"walmart_url": "https://www.walmart.com/ip/x/222"},
    }
    worker._cache_ci_product_images(run_id=1, item_map=item_map)

    # Only the uncached product (222) is fetched and cached.
    assert cached == [("222", "http://img/222")]
