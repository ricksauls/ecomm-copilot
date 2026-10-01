"""Tests for the scoring worker's concurrency orchestration.

These cover the parts that don't need a real browser: the SCORING_CONCURRENCY
parsing and the drain_scoring thread-pool orchestration (with the per-item
process step stubbed out, so no Playwright/Chrome is involved).
"""

import worker
from app import jobs
from app.db import get_db
from app.users import create_local_user


def test_resolve_concurrency_clamps_and_defaults():
    assert worker.resolve_concurrency(None) == 3        # unset -> default
    assert worker.resolve_concurrency("") == 3          # blank -> default
    assert worker.resolve_concurrency("abc") == 3       # garbage -> default
    assert worker.resolve_concurrency("1") == 1
    assert worker.resolve_concurrency("5") == 5
    assert worker.resolve_concurrency("0") == 1         # below floor -> 1
    assert worker.resolve_concurrency("-4") == 1
    assert worker.resolve_concurrency("10") == 10
    assert worker.resolve_concurrency("99") == 10       # above cap -> 10


def test_drain_scoring_processes_every_item_once(app, monkeypatch):
    # drain_scoring should claim and process the whole queue across its thread
    # pool, each item exactly once — even with concurrency > 1. The slow browser
    # step is stubbed; we assert on the queue draining, not on real scoring.
    with app.app_context():
        uid = create_local_user("drain@example.com", "password123")
        ids = jobs.enqueue_items(get_db(), uid, [
            {"url": f"https://www.walmart.com/ip/{i}", "item": str(i)}
            for i in range(1, 26)
        ])

    seen = []
    import threading
    seen_lock = threading.Lock()

    def fake_process_one(conn, row):
        # Stand in for fetch+score: record the item and mark it scored via the
        # thread's own connection (exercises concurrent writes under WAL).
        with seen_lock:
            seen.append(row["id"])
        jobs.save_result(conn, row["id"], 80, {"overall": 80, "dimensions": []}, "Stub")

    # Make the pool wide and strip the politeness/stagger sleeps so the test is fast.
    monkeypatch.setattr(worker, "process_one", fake_process_one)
    monkeypatch.setattr(worker, "SCORING_CONCURRENCY", 5)
    monkeypatch.setattr(worker, "FETCH_DELAY_RANGE_S", (0, 0))
    monkeypatch.setattr(worker, "SUBMIT_STAGGER_S", (0, 0))

    processed = worker.drain_scoring()

    assert processed == len(ids)
    assert sorted(seen) == sorted(ids)            # each item processed...
    assert len(seen) == len(set(seen))            # ...exactly once

    with app.app_context():
        db = get_db()
        statuses = {
            r["status"]
            for r in db.execute(
                "SELECT status FROM scored_items WHERE user_id = ?", (uid,)
            ).fetchall()
        }
    assert statuses == {"scored"}                 # queue fully drained
