"""Tests for the Seasonal Content Campaigns data layer + the two worker queues."""

from app.db import get_db
from app.seasonal import jobs, store
from app.users import create_local_user


def _campaign(db, uid, **kw):
    return store.create_campaign(db, user_id=uid, name=kw.get("name", "Holiday 2026"),
                                 theme=kw.get("theme", "christmas"),
                                 custom_prompt=kw.get("custom_prompt", ""),
                                 launch_date=kw.get("launch_date"),
                                 expiration_date=kw.get("expiration_date"),
                                 reminder_date=kw.get("reminder_date"))


def _product(db, cid, uid, url="https://www.walmart.com/ip/10294528", stype="walmart"):
    return store.add_product(db, campaign_id=cid, user_id=uid, source_url=url, source_type=stype)


# --- campaigns --------------------------------------------------------------

def test_campaign_round_trip_and_idor(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-a@example.com", "password123")
        other = create_local_user("sea-b@example.com", "password123")
        cid = _campaign(db, uid, launch_date="2026-12-01", expiration_date="2026-12-31")
        row = store.get_campaign(db, cid, uid)
        assert row["name"] == "Holiday 2026"
        assert row["theme"] == "christmas"
        assert row["status"] == store.CAMPAIGN_DRAFT
        assert row["launch_date"] == "2026-12-01"
        # IDOR: another user can't read it.
        assert store.get_campaign(db, cid, other) is None
        assert [c["id"] for c in store.list_campaigns(db, uid)] == [cid]
        assert store.list_campaigns(db, other) == []


def test_update_campaign_sets_and_clears_dates(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-c@example.com", "password123")
        cid = _campaign(db, uid, launch_date="2026-12-01")
        store.update_campaign(db, cid, uid, name="Renamed", launch_date=None,
                              expiration_date="2027-01-15", clear_dates=True)
        row = store.get_campaign(db, cid, uid)
        assert row["name"] == "Renamed"
        assert row["launch_date"] is None            # cleared
        assert row["expiration_date"] == "2027-01-15"  # set


def test_delete_campaign_cascades_and_is_idor_scoped(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-d@example.com", "password123")
        other = create_local_user("sea-e@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                 full_url="https://cdn/x.jpg", thumb_url=None)
        # Another user can't delete it.
        assert store.delete_campaign(db, cid, other) is False
        assert store.get_campaign(db, cid, uid) is not None
        # Owner deletes; children cascade.
        assert store.delete_campaign(db, cid, uid) is True
        assert store.products_for_campaign(db, cid, uid) == []
        assert store.images_for_campaign(db, cid, uid) == []


def test_recompute_campaign_status(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-f@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                       full_url="https://cdn/x.jpg", thumb_url=None)
        assert store.recompute_campaign_status(db, cid) == store.CAMPAIGN_DRAFT
        store.update_image(db, iid, gen_status=store.GEN_READY, final_path="seasonal/1/final/1.png")
        assert store.recompute_campaign_status(db, cid) == store.CAMPAIGN_READY


# --- products (ACQUIRE queue) ----------------------------------------------

def test_product_claim_is_atomic(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-g@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        assert store.has_claimable_product(db) is True
        claimed = store.claim_next_product(db)
        assert claimed["id"] == pid and claimed["status"] == store.PRODUCT_PROCESSING
        # Already claimed → nothing left.
        assert store.claim_next_product(db) is None
        assert store.has_claimable_product(db) is False


def test_reclaim_orphaned_products(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-h@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        store.set_product_status(db, pid, store.PRODUCT_PROCESSING)
        assert store.reclaim_orphaned_products(db) == 1
        assert store.get_product(db, pid, uid)["status"] == store.PRODUCT_ERROR


# --- images: selection + generation state -----------------------------------

def test_ready_images_only_returns_kept_ready(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-j@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        ready = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                         full_url="https://cdn/r.jpg", thumb_url=None)
        discarded = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                             full_url="https://cdn/d.jpg", thumb_url=None)
        store.update_image(db, ready, gen_status=store.GEN_READY, final_path="seasonal/1/final/1.png")
        store.update_image(db, discarded, gen_status=store.GEN_READY, final_path="seasonal/1/final/2.png")
        store.set_image_kept(db, discarded, uid, False)
        ids = {im["id"] for im in store.ready_images_for_campaign(db, cid, uid)}
        assert ids == {ready}  # the discarded one is excluded


def test_update_image_rejects_unknown_column(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-k@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                       full_url="https://cdn/x.jpg", thumb_url=None)
        try:
            store.update_image(db, iid, bogus_column="x")
        except ValueError:
            pass
        else:
            raise AssertionError("update_image must reject unknown fields")


# --- generation queue (seasonal_jobs) ---------------------------------------

def test_enqueue_is_idempotent_and_marks_image_queued(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-l@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                       full_url="https://cdn/x.jpg", thumb_url=None)
        j1 = jobs.enqueue_image_job(db, user_id=uid, campaign_id=cid, image_id=iid)
        # Image flipped into the queue.
        assert store.get_image(db, iid, uid)["gen_status"] == store.GEN_QUEUED
        # A live job for the same image is a no-op (same id back).
        j2 = jobs.enqueue_image_job(db, user_id=uid, campaign_id=cid, image_id=iid)
        assert j1 == j2


def test_dashboard_reminders_classify_by_date(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-n@example.com", "password123")
        today = "2026-12-10"
        overdue = _campaign(db, uid, name="Overdue", expiration_date="2026-12-01")
        expiring = _campaign(db, uid, name="Expiring", expiration_date="2026-12-15")
        launching = _campaign(db, uid, name="Launching", launch_date="2026-12-18")
        due = _campaign(db, uid, name="ReminderDue", reminder_date="2026-12-10")
        far = _campaign(db, uid, name="Far", launch_date="2027-06-01")  # out of window
        _campaign(db, uid, name="NoDates")                               # excluded
        reminders = store.dashboard_reminders(db, uid, today, soon_days=14)
        by_id = {r["id"]: r for r in reminders}
        assert by_id[overdue]["kind"] == "overdue" and by_id[overdue]["days"] == -9
        assert by_id[expiring]["kind"] == "expiring_soon" and by_id[expiring]["days"] == 5
        assert by_id[launching]["kind"] == "launching_soon" and by_id[launching]["days"] == 8
        assert by_id[due]["kind"] == "reminder"
        assert far not in by_id  # beyond soon_days, no overdue/reminder
        # Most urgent first: overdue leads.
        assert reminders[0]["kind"] == "overdue"
        # Only this user's campaigns (IDOR).
        other = create_local_user("sea-o@example.com", "password123")
        assert store.dashboard_reminders(db, other, today) == []


def test_claim_and_reclaim_seasonal_jobs(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("sea-m@example.com", "password123")
        cid = _campaign(db, uid)
        pid = _product(db, cid, uid)
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                       full_url="https://cdn/x.jpg", thumb_url=None)
        jobs.enqueue_image_job(db, user_id=uid, campaign_id=cid, image_id=iid)
        assert jobs.has_claimable_jobs(db) is True
        claimed = jobs.claim_next_job(db)
        assert claimed["image_id"] == iid
        assert jobs.claim_next_job(db) is None
        # A worker crash leaves it 'processing'; reclaim fails it + syncs the image.
        assert jobs.reclaim_orphaned_jobs(db) == 1
        assert store.get_image(db, iid, uid)["gen_status"] == store.GEN_FAILED
