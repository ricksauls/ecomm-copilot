"""Tests for the async image-fix job queue (upscale / white-background)."""

from app import image_jobs, jobs
from app.db import get_db
from app.users import create_local_user


def _scored_item(db, uid, url="https://www.walmart.com/ip/1", item="1"):
    """Create a scored_items row to hang image jobs off (satisfies the FK)."""
    return jobs.enqueue_items(db, uid, [{"url": url, "item": item}])[0]


def _enqueue(db, uid, sid, slot="img2", operation="upscale",
             source_url="https://i5/x.jpg"):
    return image_jobs.enqueue_image_job(
        db, user_id=uid, scored_item_id=sid, slot=slot,
        operation=operation, source_url=source_url,
    )


def test_enqueue_creates_queued_job(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("a@example.com", "password123")
        sid = _scored_item(db, uid)
        job_id = _enqueue(db, uid, sid)
        row = image_jobs.get_image_job(db, job_id, uid)
        assert row["status"] == "queued"
        assert row["slot"] == "img2"
        assert row["operation"] == "upscale"


def test_enqueue_is_idempotent_while_active(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("b@example.com", "password123")
        sid = _scored_item(db, uid)
        first = _enqueue(db, uid, sid)
        second = _enqueue(db, uid, sid)  # same slot, still queued
        assert first == second
        n = db.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0]
        assert n == 1


def test_enqueue_resets_errored_job(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("c@example.com", "password123")
        sid = _scored_item(db, uid)
        job_id = _enqueue(db, uid, sid)
        image_jobs.mark_image_failed(db, job_id, "boom")
        # Re-enqueue a failed slot: same row, back to queued, error cleared.
        again = _enqueue(db, uid, sid)
        assert again == job_id
        row = image_jobs.get_image_job(db, job_id, uid)
        assert row["status"] == "queued"
        assert row["error"] is None


def test_claim_transitions_queued_to_processing(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        sid = _scored_item(db, uid)
        _enqueue(db, uid, sid)
        claimed = image_jobs.claim_next_image_job(db)
        assert claimed is not None and claimed["status"] == "processing"
        # Nothing else claimable once it's in flight.
        assert image_jobs.claim_next_image_job(db) is None
        assert image_jobs.has_claimable_image_jobs(db) is False


def test_mark_done_and_failed(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("e@example.com", "password123")
        sid = _scored_item(db, uid)
        j1 = _enqueue(db, uid, sid, slot="img2")
        j2 = _enqueue(db, uid, sid, slot="whitebg", operation="white_bg")
        image_jobs.mark_image_done(db, j1)
        image_jobs.mark_image_failed(db, j2, "x" * 999)
        assert image_jobs.get_image_job(db, j1, uid)["status"] == "done"
        failed = image_jobs.get_image_job(db, j2, uid)
        assert failed["status"] == "error"
        assert len(failed["error"]) <= 500  # message is truncated


def test_jobs_for_items_is_user_scoped(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("f@example.com", "password123")
        other = create_local_user("g@example.com", "password123")
        sid = _scored_item(db, uid)
        _enqueue(db, uid, sid, slot="img2")
        _enqueue(db, uid, sid, slot="whitebg", operation="white_bg")
        mine = image_jobs.jobs_for_items(db, [sid], uid)
        assert set(mine.keys()) == {(sid, "img2"), (sid, "whitebg")}
        # Another user sees none of them (IDOR guard).
        assert image_jobs.jobs_for_items(db, [sid], other) == {}
        # Empty id list short-circuits.
        assert image_jobs.jobs_for_items(db, [], uid) == {}


def test_get_image_job_idor(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("h@example.com", "password123")
        other = create_local_user("i@example.com", "password123")
        sid = _scored_item(db, uid)
        job_id = _enqueue(db, uid, sid)
        assert image_jobs.get_image_job(db, job_id, other) is None


def test_reclaim_orphaned_marks_processing_as_error(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("j@example.com", "password123")
        sid = _scored_item(db, uid)
        _enqueue(db, uid, sid)
        claimed = image_jobs.claim_next_image_job(db)  # -> processing
        reclaimed = image_jobs.reclaim_orphaned_image_jobs(db)
        assert reclaimed == 1
        assert image_jobs.get_image_job(db, claimed["id"], uid)["status"] == "error"


def test_claim_honors_priority_over_fifo(app):
    """A higher-priority job is claimed before an older lower-priority one."""
    with app.app_context():
        db = get_db()
        uid = create_local_user("prio@example.com", "password123")
        sid = _scored_item(db, uid)
        # Enqueue a low-priority upscale first, then a high-priority whitebg.
        low = image_jobs.enqueue_image_job(
            db, user_id=uid, scored_item_id=sid, slot="img2",
            operation="upscale", source_url="https://i5/2.jpg", priority=0,
        )
        high = image_jobs.enqueue_image_job(
            db, user_id=uid, scored_item_id=sid, slot="whitebg",
            operation="white_bg", source_url="https://i5/main.jpg", priority=10,
        )
        # Despite being enqueued second (higher id), the whitebg job claims first.
        first = image_jobs.claim_next_image_job(db)
        assert first["id"] == high
        second = image_jobs.claim_next_image_job(db)
        assert second["id"] == low


def test_cancel_queued_drops_only_queued(app):
    """Cancel removes queued rows but leaves in-flight/done/error untouched."""
    with app.app_context():
        db = get_db()
        uid = create_local_user("cancel@example.com", "password123")
        sid = _scored_item(db, uid)
        _enqueue(db, uid, sid, slot="img2")  # enqueued first → claimed first
        _enqueue(db, uid, sid, slot="img3")  # stays queued
        image_jobs.claim_next_image_job(db)  # claims img2 (oldest) -> processing
        n = image_jobs.cancel_queued_for_items(db, [sid], uid)
        assert n == 1  # only the still-queued img3 is dropped
        remaining = {r["slot"]: r["status"] for r in db.execute(
            "SELECT slot, status FROM image_jobs WHERE scored_item_id = ?", (sid,)
        ).fetchall()}
        assert remaining == {"img2": "processing"}


def test_cancel_queued_scoped_to_user(app):
    """One user's cancel never touches another user's queued jobs (IDOR)."""
    with app.app_context():
        db = get_db()
        uid = create_local_user("own@example.com", "password123")
        other = create_local_user("other@example.com", "password123")
        sid = _scored_item(db, uid)
        _enqueue(db, uid, sid)
        # 'other' cancelling the same sid id affects nothing they don't own.
        assert image_jobs.cancel_queued_for_items(db, [sid], other) == 0
        assert db.execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0] == 1


def test_requeue_failed_resets_errors_only(app):
    """Retry re-queues error rows; done/queued rows are left as they are."""
    with app.app_context():
        db = get_db()
        uid = create_local_user("retry@example.com", "password123")
        sid = _scored_item(db, uid)
        failed = _enqueue(db, uid, sid, slot="img2")
        image_jobs.mark_image_failed(db, failed, "boom")
        done = _enqueue(db, uid, sid, slot="img3")
        image_jobs.mark_image_done(db, done)
        n = image_jobs.requeue_failed_for_items(db, [sid], uid)
        assert n == 1
        states = {r["slot"]: r["status"] for r in db.execute(
            "SELECT slot, status FROM image_jobs WHERE scored_item_id = ?", (sid,)
        ).fetchall()}
        assert states == {"img2": "queued", "img3": "done"}
        assert image_jobs.get_image_job(db, failed, uid)["error"] is None
