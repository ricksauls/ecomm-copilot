"""Tests for the scoring job queue (enqueue / claim / result store)."""

import json

from app import copy_jobs, jobs
from app.db import get_db
from app.users import create_local_user


def test_count_managed_products_unions_scoring_and_copy_per_user(app):
    # A product counts once whether scored, copy-created, or both; keyed on item id
    # and scoped to the user.
    with app.app_context():
        db = get_db()
        uid = create_local_user("m@example.com", "password123")
        other = create_local_user("n@example.com", "password123")
        jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        copy_jobs.enqueue_copy_items(db, uid, [
            {"url": "https://www.walmart.com/ip/2", "item": "2"},  # overlap -> not double
            {"url": "https://www.walmart.com/ip/3", "item": "3"},
        ])
        jobs.enqueue_items(db, other, [{"url": "https://www.walmart.com/ip/9", "item": "9"}])

        assert jobs.count_managed_products(db, uid) == 3    # {1, 2, 3}
        assert jobs.count_managed_products(db, other) == 1  # {9}


def test_product_counts_are_distinct_and_month_scoped(app):
    from datetime import date

    from app import copy_jobs
    with app.app_context():
        db = get_db()
        uid = create_local_user("month@example.com", "password123")
        # scored: item 1 in a prior month, item 2 twice this month, item 3 this month.
        db.execute("INSERT INTO scored_items (user_id, item_id, url, created_at) "
                   "VALUES (?,?,?,?)", (uid, "1", "u", "2020-01-05 00:00:00"))
        for it in ("2", "2", "3"):
            db.execute("INSERT INTO scored_items (user_id, item_id, url, created_at) "
                       "VALUES (?,?,?,datetime('now'))", (uid, it, "u"))
        # copy: item 3 (also scored) + item 4, both this month.
        for it in ("3", "4"):
            db.execute("INSERT INTO copy_items (user_id, item_id, url, created_at) "
                       "VALUES (?,?,?,datetime('now'))", (uid, it, "u"))
        db.commit()
        month = date.today().replace(day=1).isoformat()

        assert jobs.count_scored_products(db, uid) == 3            # {1,2,3}
        assert jobs.count_scored_products(db, uid, since=month) == 2   # {2,3}
        assert copy_jobs.count_copy_products(db, uid) == 2         # {3,4}
        assert copy_jobs.count_copy_products(db, uid, since=month) == 2
        # Managed union: {1,2,3,4} all-time; {2,3,4} this month (item 1 is old).
        assert jobs.count_managed_products(db, uid) == 4
        assert jobs.count_managed_products(db, uid, since=month) == 3


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
