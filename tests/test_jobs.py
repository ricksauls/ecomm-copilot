"""Tests for the scoring job queue (enqueue / claim / result store)."""

import json

from app import jobs
from app.db import get_db
from app.users import create_local_user


def test_enqueue_and_get_items_scoped_to_user(app):
    with app.app_context():
        uid = create_local_user("a@example.com", "password123")
        other = create_local_user("b@example.com", "password123")
        db = get_db()
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        assert len(ids) == 2
        rows = jobs.get_items(db, ids, uid)
        assert [r["status"] for r in rows] == ["queued", "queued"]
        # Another user can't read this user's rows (IDOR guard).
        assert jobs.get_items(db, ids, other) == []


def test_claim_next_marks_scoring_and_drains(app):
    with app.app_context():
        uid = create_local_user("c@example.com", "password123")
        db = get_db()
        jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/1", "item": "1"}])
        claimed = jobs.claim_next(db)
        assert claimed is not None
        assert claimed["status"] == "scoring"
        # Nothing left to claim once drained.
        assert jobs.claim_next(db) is None


def test_save_result_and_mark_failed(app):
    with app.app_context():
        uid = create_local_user("d@example.com", "password123")
        db = get_db()
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        jobs.save_result(db, ids[0], 82, {"overall": 82, "dimensions": []},
                         "Tabasco Chipotle Pepper Sauce, 5 fl oz")
        jobs.mark_failed(db, ids[1], "blocked", "bot detection")

        rows = {r["id"]: r for r in jobs.get_items(db, ids, uid)}
        assert rows[ids[0]]["status"] == "scored"
        assert rows[ids[0]]["overall"] == 82
        assert rows[ids[0]]["title"] == "Tabasco Chipotle Pepper Sauce, 5 fl oz"
        assert json.loads(rows[ids[0]]["result_json"])["overall"] == 82
        assert rows[ids[1]]["status"] == "blocked"
        assert rows[ids[1]]["error"] == "bot detection"
