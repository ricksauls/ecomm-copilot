"""Tests for the copy-content job queue (two-phase fetch/generate lifecycle)."""

import json

from app import copy_jobs
from app.db import get_db
from app.users import create_local_user


def _items(n=1):
    return [{"url": f"https://www.walmart.com/ip/{i}", "item": str(i)} for i in range(1, n + 1)]


def test_enqueue_and_get_scoped_to_user(app):
    with app.app_context():
        uid = create_local_user("a@example.com", "password123")
        other = create_local_user("b@example.com", "password123")
        db = get_db()
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(2))
        rows = copy_jobs.get_copy_items(db, ids, uid)
        assert [r["status"] for r in rows] == ["queued", "queued"]
        assert [r["auto_generate"] for r in rows] == [0, 0]
        # IDOR guard: another user can't read these rows.
        assert copy_jobs.get_copy_items(db, ids, other) == []


def test_auto_generate_flag_persisted(app):
    with app.app_context():
        uid = create_local_user("c@example.com", "password123")
        db = get_db()
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(1), auto_generate=True)
        assert copy_jobs.get_copy_items(db, ids, uid)[0]["auto_generate"] == 1


def test_claim_transitions_queued_then_gen_queued(app):
    with app.app_context():
        uid = create_local_user("d@example.com", "password123")
        db = get_db()
        copy_jobs.enqueue_copy_items(db, uid, _items(1))
        # A queued row is claimed into 'fetching'.
        claimed = copy_jobs.claim_next_copy(db)
        assert claimed is not None and claimed["status"] == "fetching"
        # Nothing else claimable until it advances.
        assert copy_jobs.claim_next_copy(db) is None
        # Move it to gen_queued; the worker should claim it into 'generating'.
        copy_jobs.save_current_copy(
            db, claimed["id"], title="T", current={"record": {"url": "u"}},
            current_overall=50, keywords=["k"], next_status="gen_queued",
        )
        again = copy_jobs.claim_next_copy(db)
        assert again is not None and again["status"] == "generating"


def test_save_current_copy_rests_at_fetched(app):
    with app.app_context():
        uid = create_local_user("e@example.com", "password123")
        db = get_db()
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(1))
        copy_jobs.claim_next_copy(db)
        copy_jobs.save_current_copy(
            db, ids[0], title="Prod", current={"title": "Prod", "record": {"url": "u"}},
            current_overall=60, keywords=["a", "b"], next_status="fetched",
        )
        row = copy_jobs.get_copy_items(db, ids, uid)[0]
        assert row["status"] == "fetched"
        assert row["current_overall"] == 60
        assert row["title"] == "Prod"
        assert json.loads(row["keywords_json"]) == ["a", "b"]


def test_save_generated_and_request_generation(app):
    with app.app_context():
        uid = create_local_user("f@example.com", "password123")
        db = get_db()
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(2))
        # Both fetched.
        for i in ids:
            copy_jobs.save_current_copy(
                db, i, title="P", current={"record": {"url": "u"}},
                current_overall=40, keywords=[], next_status="fetched",
            )
        # Requesting generation only advances 'fetched' rows, and is user-scoped.
        advanced = copy_jobs.request_generation(db, ids, uid)
        assert advanced == 2
        assert all(r["status"] == "gen_queued" for r in copy_jobs.get_copy_items(db, ids, uid))
        # A second request is a no-op (they're no longer 'fetched').
        assert copy_jobs.request_generation(db, ids, uid) == 0

        copy_jobs.save_generated_copy(
            db, ids[0], new={"title": "New", "bullets": ["x"], "description": "d"},
            projected_overall=88,
        )
        row = copy_jobs.get_copy_items(db, [ids[0]], uid)[0]
        assert row["status"] == "done"
        assert row["projected_overall"] == 88
        assert json.loads(row["new_json"])["title"] == "New"


def test_request_generation_is_user_scoped(app):
    with app.app_context():
        uid = create_local_user("g@example.com", "password123")
        other = create_local_user("h@example.com", "password123")
        db = get_db()
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(1))
        copy_jobs.save_current_copy(
            db, ids[0], title="P", current={"record": {"url": "u"}},
            current_overall=40, keywords=[], next_status="fetched",
        )
        # Another user cannot advance this user's rows.
        assert copy_jobs.request_generation(db, ids, other) == 0
        assert copy_jobs.get_copy_items(db, ids, uid)[0]["status"] == "fetched"


def test_mark_copy_failed(app):
    with app.app_context():
        uid = create_local_user("i@example.com", "password123")
        db = get_db()
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(1))
        copy_jobs.mark_copy_failed(db, ids[0], "blocked", "bot detection")
        row = copy_jobs.get_copy_items(db, ids, uid)[0]
        assert row["status"] == "blocked"
        assert row["error"] == "bot detection"
