"""Tests for the worker's CI run processor (browser stubbed out)."""

import worker
from app import ci_config, ci_jobs, ci_scraper
from app.db import get_db
from app.fetch import FetchBlocked
from app.users import create_local_user


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

        # Stub the browser scrape: one matching card (mine) + one "other".
        def fake_scrape(keyword, **kw):
            return [
                {"name": "Tabasco", "item_id": "10294528", "listing_type": "organic", "product_url": ""},
                {"name": "Other", "item_id": "999", "listing_type": "sponsored", "product_url": ""},
            ]
        monkeypatch.setattr(ci_scraper, "scrape_keyword_cards", fake_scrape)
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


def test_run_with_all_keywords_blocked_is_marked_error(app, monkeypatch):
    with app.app_context():
        db = get_db()
        uid, gid, _ = _seed_group(db)

        def blocked(keyword, **kw):
            raise FetchBlocked("bot wall")
        monkeypatch.setattr(ci_scraper, "scrape_keyword_cards", blocked)

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
            return [{"name": "X", "item_id": "1", "listing_type": "organic", "product_url": ""}]
        monkeypatch.setattr(ci_scraper, "scrape_keyword_cards", flaky)
        monkeypatch.setattr(ci_scraper, "INTER_KEYWORD_DELAY_S", (0, 0))

        rid = ci_jobs.enqueue_run(db, gid)
        worker.process_ci_run(db, ci_jobs.claim_next_run(db))
        # Run still done (one keyword succeeded); only the good keyword's card stored.
        assert ci_jobs.get_run(db, rid, uid)["status"] == "done"
        assert db.execute("SELECT COUNT(*) FROM ci_search_results WHERE run_id=?", (rid,)).fetchone()[0] == 1
