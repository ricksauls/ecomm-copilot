"""Worker-side tests for the daily rank capture (worker.process_rank_one).

The scrape is monkeypatched (no network/browser): we assert a success writes a
reading and settles the job, and a block/error settles it to 'error' without
raising.
"""

from app import ci_scraper, item_rank, portfolio
from app.db import get_db
from app.fetch import FetchBlocked
from app.users import create_local_user

_URL = "https://www.walmart.com/ip/Tabasco-Original/111"


def _claim_one(db, uid):
    portfolio.add_item(db, uid, "Tabasco", _URL, None)
    item_rank.enqueue_rank_jobs(db)
    return item_rank.claim_next_rank_job(db)


def test_process_rank_one_writes_reading(app, monkeypatch):
    import worker
    with app.app_context():
        db = get_db()
        uid = create_local_user("prw@example.com", "password123")
        job = _claim_one(db, uid)
        monkeypatch.setattr(ci_scraper, "scrape_keyword_cards", lambda kw: [
            {"name": "Other", "item_id": "999", "listing_type": "organic",
             "product_url": "https://www.walmart.com/ip/Other/999"},
            {"name": "Tabasco Original", "item_id": "111", "listing_type": "organic",
             "product_url": _URL},
        ])
        worker.process_rank_one(db, job)
        row = db.execute(
            "SELECT rank, item_slots, total_slots FROM portfolio_rank_readings "
            "WHERE keyword_id = ?", (job["keyword_id"],)).fetchone()
        assert (row["rank"], row["item_slots"], row["total_slots"]) == (2, 1, 2)
        assert db.execute("SELECT status FROM portfolio_rank_jobs WHERE id = ?",
                          (job["id"],)).fetchone()["status"] == "done"


def test_process_rank_one_handles_block(app, monkeypatch):
    import worker
    with app.app_context():
        db = get_db()
        uid = create_local_user("prb@example.com", "password123")
        job = _claim_one(db, uid)

        def _blocked(kw):
            raise FetchBlocked("bot wall")
        monkeypatch.setattr(ci_scraper, "scrape_keyword_cards", _blocked)
        worker.process_rank_one(db, job)  # must not raise
        assert db.execute("SELECT status FROM portfolio_rank_jobs WHERE id = ?",
                          (job["id"],)).fetchone()["status"] == "error"
        # No reading written on failure.
        assert db.execute(
            "SELECT COUNT(*) c FROM portfolio_rank_readings WHERE keyword_id = ?",
            (job["keyword_id"],)).fetchone()["c"] == 0
