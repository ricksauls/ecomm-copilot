"""Tests for the daily rank/share capture (app.item_rank).

Covers the pure SERP matcher (build_reading), the enqueue gate + dedupe, and the
claim/save/fail/reclaim queue lifecycle. The calendar-bucket roll-up readers are
exercised in test_item_rank_rollup.py.
"""

from app import item_rank, portfolio
from app.db import get_db
from app.users import create_local_user


def _card(name, item_id, url, listing_type="organic"):
    return {"name": name, "item_id": item_id, "listing_type": listing_type,
            "product_url": url}


_URL_111 = "https://www.walmart.com/ip/Tabasco-Original-Hot-Sauce/111"


def test_build_reading_matches_by_item_number():
    cards = [
        _card("Frank's RedHot", "999", "https://www.walmart.com/ip/Franks/999"),
        _card("Tabasco Original Hot Sauce", "111", _URL_111),
        _card("Generic Hot Sauce", "222", "https://www.walmart.com/ip/Generic/222"),
    ]
    r = item_rank.build_reading(cards, "111")
    assert r["rank"] == 2 and r["organic_rank"] == 2
    assert r["sponsored_rank"] is None
    assert r["item_slots"] == 1
    assert r["total_slots"] == 3


def test_build_reading_ties_back_sponsored_by_title():
    # The item's sponsored slot carries an opaque id but the same title as its
    # organic card — it must still count (and set sponsored_rank).
    cards = [
        _card("Tabasco Original Hot Sauce", "SPONSORED-OPAQUE",
              "https://www.walmart.com/track/abc", listing_type="sponsored"),
        _card("Frank's RedHot", "999", "https://www.walmart.com/ip/Franks/999"),
        _card("Tabasco Original Hot Sauce", "111", _URL_111),
    ]
    r = item_rank.build_reading(cards, "111")
    assert r["item_slots"] == 2
    assert r["sponsored_rank"] == 1  # the sponsored slot sits first
    assert r["organic_rank"] == 3
    assert r["rank"] == 1  # best of the two
    assert r["total_slots"] == 3


def test_build_reading_item_absent_is_none_rank():
    cards = [_card("Frank's RedHot", "999", "https://www.walmart.com/ip/Franks/999")]
    r = item_rank.build_reading(cards, "111")
    assert r["rank"] is None and r["item_slots"] == 0 and r["total_slots"] == 1


def test_rank_capture_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ITEM_TRACKING_RANK_ENABLED", raising=False)
    assert item_rank.rank_capture_enabled() is False
    monkeypatch.setenv("ITEM_TRACKING_RANK_ENABLED", "1")
    assert item_rank.rank_capture_enabled() is True


def _tracked_item(db, uid):
    portfolio.add_item(db, uid, "Tabasco", _URL_111, None)
    return db.execute("SELECT id FROM portfolio_items WHERE item_id = '111'").fetchone()["id"]


def test_enqueue_rank_jobs_skips_blank_keywords_and_dedupes(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("rk@example.com", "password123")
        item_id = _tracked_item(db, uid)
        # Seeded keywords: KW1=brand, KW2=brand (title not fetched), KW3=NULL(blank).
        # Give KW3 a value so we have 3 non-blank; leave one blank to prove it's skipped.
        db.execute("UPDATE portfolio_keywords SET keyword = NULL WHERE portfolio_item_id = ? AND slot = 2",
                   (item_id,))
        db.execute("UPDATE portfolio_keywords SET keyword = 'Hot Sauce' WHERE portfolio_item_id = ? AND slot = 3",
                   (item_id,))
        db.commit()

        # Non-blank keywords now: slot 1 (Tabasco) + slot 3 (Hot Sauce) = 2.
        assert item_rank.enqueue_rank_jobs(db) == 2
        # Idempotent within a sweep — the active jobs block a re-enqueue.
        assert item_rank.enqueue_rank_jobs(db) == 0
        assert db.execute("SELECT COUNT(*) c FROM portfolio_rank_jobs").fetchone()["c"] == 2


def test_enqueue_skips_untracked(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("unt@example.com", "password123")
        item_id = _tracked_item(db, uid)
        db.execute("UPDATE portfolio_items SET tracked = 0 WHERE id = ?", (item_id,))
        db.commit()
        assert item_rank.enqueue_rank_jobs(db) == 0


def test_claim_save_reading_lifecycle(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("life@example.com", "password123")
        _tracked_item(db, uid)
        item_rank.enqueue_rank_jobs(db)

        assert item_rank.has_claimable_rank_job(db) is True
        job = item_rank.claim_next_rank_job(db)
        assert job is not None and job["status"] == "fetching"

        reading = {"rank": 3, "organic_rank": 3, "sponsored_rank": None,
                   "item_slots": 1, "total_slots": 40}
        item_rank.save_reading(db, job, reading, "2026-01-03")
        row = db.execute(
            "SELECT rank, item_slots, total_slots, captured_at FROM portfolio_rank_readings "
            "WHERE keyword_id = ?", (job["keyword_id"],)).fetchone()
        assert (row["rank"], row["item_slots"], row["total_slots"]) == (3, 1, 40)
        assert row["captured_at"] == "2026-01-03"
        assert db.execute("SELECT status FROM portfolio_rank_jobs WHERE id = ?",
                          (job["id"],)).fetchone()["status"] == "done"


def test_mark_failed_and_reclaim(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("fail2@example.com", "password123")
        _tracked_item(db, uid)
        item_rank.enqueue_rank_jobs(db)
        job = item_rank.claim_next_rank_job(db)
        item_rank.mark_rank_failed(db, job["id"], "Blocked: bot wall")
        assert db.execute("SELECT status FROM portfolio_rank_jobs WHERE id = ?",
                          (job["id"],)).fetchone()["status"] == "error"

        # A job stuck 'fetching' is failed (not requeued) at startup.
        another = item_rank.claim_next_rank_job(db)
        assert another is not None
        assert item_rank.reclaim_orphaned_rank_jobs(db) == 1
        assert db.execute("SELECT status FROM portfolio_rank_jobs WHERE id = ?",
                          (another["id"],)).fetchone()["status"] == "error"
