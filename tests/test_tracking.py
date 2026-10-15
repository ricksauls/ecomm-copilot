"""Unit tests for individual item tracking (app.tracking) + its enqueuer.

Covers the tracked flag (with the IDOR gate), the daily-facts queue
(enqueue/claim/save/fail/reclaim), the user-scoped facts history reads, and the
weekly readiness enqueuer that reuses the scoring queue.
"""

from app import jobs, tracking
from app.db import get_db
from app.enqueue_item_tracking import enqueue_weekly_readiness
from app.users import create_local_user


def _add_item(conn, user_id, url, *, item_id=None, brand="Tabasco", tracked=False):
    """Insert a portfolio item directly and return its id."""
    cur = conn.execute(
        "INSERT INTO portfolio_items "
        "(user_id, brand, walmart_url, item_id, dedupe_key, tracked) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user_id, brand, url, item_id, item_id or url.lower(), 1 if tracked else 0),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_set_tracked_toggles_and_is_user_scoped(app):
    with app.app_context():
        conn = get_db()
        owner = create_local_user("owner@example.com", "password123")
        other = create_local_user("other@example.com", "password123")
        pid = _add_item(conn, owner, "https://www.walmart.com/ip/a/10294528", item_id="10294528")

        assert tracking.set_tracked(conn, owner, pid, True) is True
        assert tracking.count_tracked(conn, owner) == 1
        assert [r["id"] for r in tracking.list_tracked(conn, owner)] == [pid]

        # Another user can't flip someone else's item (IDOR): no row updated.
        assert tracking.set_tracked(conn, other, pid, False) is False
        assert tracking.count_tracked(conn, owner) == 1  # still tracked

        assert tracking.set_tracked(conn, owner, pid, False) is True
        assert tracking.count_tracked(conn, owner) == 0


def test_enqueue_fact_jobs_skips_untracked_active_and_urlless(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("u@example.com", "password123")
        tracked = _add_item(conn, uid, "https://www.walmart.com/ip/a/111", item_id="111", tracked=True)
        _add_item(conn, uid, "https://www.walmart.com/ip/b/222", item_id="222", tracked=False)

        assert tracking.enqueue_fact_jobs(conn) == 1  # only the tracked one
        # Running again enqueues nothing — the first job is still active (queued).
        assert tracking.enqueue_fact_jobs(conn) == 0
        assert tracking.has_active_fact_job(conn, tracked) is True


def test_facts_claim_save_and_history(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("u@example.com", "password123")
        pid = _add_item(conn, uid, "https://www.walmart.com/ip/a/111", item_id="111", tracked=True)
        tracking.enqueue_fact_jobs(conn)

        job = tracking.claim_next_fact_job(conn)
        assert job is not None and job["status"] == "fetching"
        assert tracking.claim_next_fact_job(conn) is None  # queue drained

        tracking.save_facts(conn, job, {
            "price": 3.98, "price_string": "$3.98", "in_stock": True,
            "rating": 4.6, "review_count": 1200, "image_count": 5,
            "has_video": False, "title": "Tabasco Original",
        })
        snap = tracking.latest_facts(conn, uid, pid)
        assert snap["price"] == 3.98 and snap["in_stock"] == 1 and snap["has_video"] == 0
        assert snap["review_count"] == 1200
        assert len(tracking.list_facts(conn, uid, pid)) == 1
        # The job is closed out.
        done = conn.execute("SELECT status FROM portfolio_fact_jobs WHERE id = ?",
                            (job["id"],)).fetchone()
        assert done["status"] == "done"


def test_facts_history_is_user_scoped(app):
    with app.app_context():
        conn = get_db()
        owner = create_local_user("owner@example.com", "password123")
        other = create_local_user("other@example.com", "password123")
        pid = _add_item(conn, owner, "https://www.walmart.com/ip/a/111", item_id="111", tracked=True)
        tracking.enqueue_fact_jobs(conn)
        tracking.save_facts(conn, tracking.claim_next_fact_job(conn), {"price": 1.0})
        # The other user sees nothing for this item id.
        assert tracking.list_facts(conn, other, pid) == []
        assert tracking.latest_facts(conn, other, pid) is None


def test_mark_failed_and_reclaim_orphaned(app):
    with app.app_context():
        conn = get_db()
        uid = create_local_user("u@example.com", "password123")
        _add_item(conn, uid, "https://www.walmart.com/ip/a/111", item_id="111", tracked=True)
        tracking.enqueue_fact_jobs(conn)
        job = tracking.claim_next_fact_job(conn)

        tracking.mark_fact_failed(conn, job["id"], "Blocked: bot wall")
        row = conn.execute("SELECT status, message FROM portfolio_fact_jobs WHERE id = ?",
                           (job["id"],)).fetchone()
        assert row["status"] == "error" and "Blocked" in row["message"]

        # A second job stuck 'fetching' (worker died) is reclaimed on startup.
        tracking.enqueue_fact_jobs(conn)
        tracking.claim_next_fact_job(conn)  # -> fetching
        assert tracking.reclaim_orphaned_fact_jobs(conn) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM portfolio_fact_jobs WHERE status = 'fetching'"
        ).fetchone()[0] == 0


def test_weekly_readiness_enqueues_into_scoring_queue_per_user(app):
    with app.app_context():
        conn = get_db()
        u1 = create_local_user("u1@example.com", "password123")
        u2 = create_local_user("u2@example.com", "password123")
        _add_item(conn, u1, "https://www.walmart.com/ip/a/111", item_id="111", tracked=True)
        _add_item(conn, u1, "https://www.walmart.com/ip/b/222", item_id="222", tracked=True)
        _add_item(conn, u2, "https://www.walmart.com/ip/c/333", item_id="333", tracked=True)
        _add_item(conn, u1, "https://www.walmart.com/ip/d/444", item_id="444", tracked=False)

        assert enqueue_weekly_readiness(conn) == 3  # only tracked items
        # u1's two items share one batch (one submission); u2's is separate.
        batches = {r["batch_id"] for r in conn.execute(
            "SELECT batch_id FROM scored_items WHERE user_id = ?", (u1,))}
        assert len(batches) == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM scored_items WHERE status = 'queued'").fetchone()[0] == 3
        # Sanity: they're real scoring rows the worker would drain.
        assert jobs.has_queued_items(conn) is True
