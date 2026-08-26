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


def test_count_managed_brands_is_distinct_case_insensitive_and_unions(app):
    # Distinct brands across scoring + copy, compared case-insensitively, scoped
    # to the user; blank/NULL brands are excluded.
    with app.app_context():
        db = get_db()
        uid = create_local_user("brands@example.com", "password123")
        other = create_local_user("brands2@example.com", "password123")
        jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1", "brand": "Tabasco"},
            {"url": "https://www.walmart.com/ip/2", "item": "2", "brand": "TABASCO"},  # same brand
            {"url": "https://www.walmart.com/ip/3", "item": "3", "brand": None},       # no brand
        ])
        copy_jobs.enqueue_copy_items(db, uid, [
            {"url": "https://www.walmart.com/ip/4", "item": "4", "brand": "Cholula"},
        ])
        jobs.enqueue_items(db, other, [
            {"url": "https://www.walmart.com/ip/9", "item": "9", "brand": "Frank's"},
        ])

        assert jobs.count_managed_brands(db, uid) == 2    # {tabasco, cholula}
        assert jobs.count_managed_brands(db, other) == 1  # {frank's}


def test_save_result_keeps_user_brand_but_fills_blank_from_pdp(app):
    # A user-entered brand wins; a blank one is filled from the scraped PDP brand.
    with app.app_context():
        db = get_db()
        uid = create_local_user("brandfill@example.com", "password123")
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1", "brand": "My Brand"},
            {"url": "https://www.walmart.com/ip/2", "item": "2", "brand": None},
        ])
        jobs.save_result(db, ids[0], 80, {"overall": 80, "dimensions": []},
                         "Prod A", brand="PDP Brand")   # user brand present -> unchanged
        jobs.save_result(db, ids[1], 80, {"overall": 80, "dimensions": []},
                         "Prod B", brand="PDP Brand")   # blank -> filled from PDP

        rows = {r["id"]: r for r in jobs.get_items(db, ids, uid)}
        assert rows[ids[0]]["brand"] == "My Brand"
        assert rows[ids[1]]["brand"] == "PDP Brand"


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


def test_reclaim_orphaned_items_fails_stranded_scoring_rows(app):
    # A row left 'scoring' (worker died mid-fetch) is stranded forever; reclaim marks
    # it 'error' so the user sees the failure and can re-run. Queued/finished rows are
    # left untouched, and the sweep is idempotent.
    with app.app_context():
        db = get_db()
        uid = create_local_user("orphan@example.com", "password123")
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        claimed = jobs.claim_next(db)  # -> status 'scoring'
        assert claimed["id"] == ids[0]

        assert jobs.reclaim_orphaned_items(db) == 1
        rows = {r["id"]: r for r in jobs.get_items(db, ids, uid)}
        assert rows[ids[0]]["status"] == "error"
        assert "Interrupted" in rows[ids[0]]["error"]
        assert rows[ids[1]]["status"] == "queued"  # never claimed -> untouched
        # Idempotent: nothing left 'scoring' now.
        assert jobs.reclaim_orphaned_items(db) == 0


def test_list_scored_this_month_only_completed_in_window(app):
    # Only 'scored' rows created within the window are listed; queued/blocked rows
    # and rows from before the window are excluded.
    with app.app_context():
        db = get_db()
        uid = create_local_user("month@example.com", "password123")
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1", "brand": "Tabasco"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
            {"url": "https://www.walmart.com/ip/3", "item": "3"},
        ])
        jobs.save_result(db, ids[0], 82, {"overall": 82}, "Tabasco Chipotle")
        jobs.save_result(db, ids[1], 70, {"overall": 70}, "Old Item")
        jobs.mark_failed(db, ids[2], "blocked", "bot detection")
        # Backdate the second scored row to before the window.
        db.execute("UPDATE scored_items SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
                   (ids[1],))
        db.commit()

        rows = jobs.list_scored_activity(db, uid, since="2020-06-01")
        assert [r["id"] for r in rows] == [ids[0]]  # only the recent scored row
        assert rows[0]["brand"] == "Tabasco"
        assert rows[0]["overall"] == 82
