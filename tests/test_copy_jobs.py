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


def test_reclaim_orphaned_copy_items_fails_stranded_in_flight_rows(app):
    # Rows left mid-flight ('fetching' from a queued claim, 'generating' from a
    # gen_queued claim) are stranded forever when the worker dies; reclaim marks both
    # 'error'. Queued/resting/done rows are untouched, and the sweep is idempotent.
    with app.app_context():
        db = get_db()
        uid = create_local_user("orphan-copy@example.com", "password123")
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(2))
        # Advance the second row to 'gen_queued' so its claim yields 'generating'.
        db.execute("UPDATE copy_items SET status = 'fetched' WHERE id = ?", (ids[1],))
        copy_jobs.request_generation(db, [ids[1]], uid)

        first = copy_jobs.claim_next_copy(db)   # queued -> 'fetching'
        assert first["id"] == ids[0] and first["status"] == "fetching"
        second = copy_jobs.claim_next_copy(db)  # gen_queued -> 'generating'
        assert second["id"] == ids[1] and second["status"] == "generating"

        assert copy_jobs.reclaim_orphaned_copy_items(db) == 2
        rows = {r["id"]: r for r in copy_jobs.get_copy_items(db, ids, uid)}
        assert rows[ids[0]]["status"] == "error"
        assert rows[ids[1]]["status"] == "error"
        assert "Interrupted" in rows[ids[0]]["error"]
        # Idempotent: nothing left in flight now.
        assert copy_jobs.reclaim_orphaned_copy_items(db) == 0


def test_reclaim_orphaned_copy_items_leaves_resting_rows_alone(app):
    # A 'fetched' row (resting, waiting for the user) and a 'done' row must survive a
    # reclaim sweep untouched — only in-flight statuses are orphans.
    with app.app_context():
        db = get_db()
        uid = create_local_user("resting-copy@example.com", "password123")
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(2))
        db.execute("UPDATE copy_items SET status = 'fetched' WHERE id = ?", (ids[0],))
        db.execute("UPDATE copy_items SET status = 'done' WHERE id = ?", (ids[1],))
        db.commit()

        assert copy_jobs.reclaim_orphaned_copy_items(db) == 0
        rows = {r["id"]: r for r in copy_jobs.get_copy_items(db, ids, uid)}
        assert rows[ids[0]]["status"] == "fetched"
        assert rows[ids[1]]["status"] == "done"


def test_list_copy_created_this_month_only_done_in_window(app):
    # Only 'done' rows (copy actually generated) within the window are listed;
    # fetched/queued rows and out-of-window rows are excluded.
    with app.app_context():
        db = get_db()
        uid = create_local_user("copymonth@example.com", "password123")
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(3))
        db.execute("UPDATE copy_items SET status = 'done' WHERE id = ?", (ids[0],))
        db.execute("UPDATE copy_items SET status = 'done', created_at = '2020-01-01 00:00:00' "
                   "WHERE id = ?", (ids[1],))
        db.execute("UPDATE copy_items SET status = 'fetched' WHERE id = ?", (ids[2],))
        db.commit()

        rows = copy_jobs.list_copy_activity(db, uid, since="2020-06-01")
        assert [r["id"] for r in rows] == [ids[0]]  # only the recent done row


def test_has_claimable_items(app):
    # Reports whether any copy row is waiting for a worker; a 'fetched' row
    # resting for the user's click is not claimable.
    with app.app_context():
        uid = create_local_user("claim@example.com", "password123")
        db = get_db()
        assert copy_jobs.has_claimable_items(db) is False
        ids = copy_jobs.enqueue_copy_items(db, uid, _items(1))
        assert copy_jobs.has_claimable_items(db) is True
        copy_jobs.claim_next_copy(db)  # -> 'fetching' (in-flight, not claimable)
        assert copy_jobs.has_claimable_items(db) is False
        # Park it at 'fetched' (resting for the user) — still not claimable.
        db.execute("UPDATE copy_items SET status = 'fetched' WHERE id = ?", (ids[0],))
        db.commit()
        assert copy_jobs.has_claimable_items(db) is False


def test_concurrent_claim_next_copy_never_double_claims(app):
    # The copy worker claims from several threads at once; claim_next_copy's
    # conditional UPDATE must hand each claimable row to exactly one claimer.
    import sqlite3
    import threading

    from app import db as dbmod

    with app.app_context():
        uid = create_local_user("copyrace@example.com", "password123")
        ids = copy_jobs.enqueue_copy_items(get_db(), uid, _items(40))
        db_path = app.config["DATABASE"]

    claimed = []
    lock = threading.Lock()

    def claim_until_empty():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        dbmod.tune_connection(conn)
        try:
            while True:
                row = copy_jobs.claim_next_copy(conn)
                if row is None:
                    return
                with lock:
                    claimed.append(row["id"])
        finally:
            conn.close()

    threads = [threading.Thread(target=claim_until_empty) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Each queued row claimed exactly once (advanced to 'fetching', then no longer
    # claimable since these rows aren't auto_generate).
    assert sorted(claimed) == sorted(ids)
    assert len(claimed) == len(set(claimed))


def test_copy_states_matches_by_item_id_and_url(app):
    with app.app_context():
        uid = create_local_user("states@example.com", "password123")
        db = get_db()
        # One done copy (item 100), one in-flight (item 200), one failed (item 300).
        done = copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/100", "item": "100"}])[0]
        copy_jobs.save_generated_copy(db, done, new={}, projected_overall=95)
        copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/200", "item": "200"}])
        copy_jobs.claim_next_copy(db)  # 100 is 'done'; claims 200 -> 'fetching' (in-flight)
        failed = copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/300", "item": "300"}])[0]
        copy_jobs.mark_copy_failed(db, failed, "error", "boom")

        keys = [("100", "https://w/ip/100"), ("200", "https://w/ip/200"),
                ("300", "https://w/ip/300"), ("999", "https://w/ip/999")]
        states = copy_jobs.copy_states_for_items(db, uid, keys)
        assert states["100"]["has_done"] is True
        assert states["100"]["latest_projected"] == 95
        assert states["200"]["has_inflight"] is True   # 'fetching' counts as in-flight
        assert states["300"]["has_failed"] is True
        assert "999" not in states                      # no copy for this item
        # Matchable by URL too (not just item id).
        assert states["https://w/ip/100"]["has_done"] is True


def test_copy_states_scoped_to_user(app):
    with app.app_context():
        uid = create_local_user("own2@example.com", "password123")
        other = create_local_user("other2@example.com", "password123")
        db = get_db()
        cid = copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/55", "item": "55"}])[0]
        copy_jobs.save_generated_copy(db, cid, new={}, projected_overall=90)
        # Another user sees nothing for the same key (IDOR guard).
        assert copy_jobs.copy_states_for_items(db, other, [("55", "https://w/ip/55")]) == {}


def test_copy_item_ids_for_items(app):
    with app.app_context():
        uid = create_local_user("ids@example.com", "password123")
        db = get_db()
        copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/1", "item": "1"}])
        b = copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/2", "item": "2"}])[0]
        copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/3", "item": "3"}])
        # A second version for product 1 — only the latest (higher id) is returned.
        a2 = copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/1", "item": "1"}])[0]
        got = copy_jobs.copy_item_ids_for_items(
            db, uid, [("1", "https://w/ip/1"), ("2", "https://w/ip/2")]
        )
        assert got == sorted([a2, b])  # one id per product, the latest copy of #1
        assert copy_jobs.copy_item_ids_for_items(db, uid, []) == []
