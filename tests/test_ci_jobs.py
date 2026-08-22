"""Tests for the CI run queue and result writers (enqueue/claim/finish + rollup)."""

from app import ci_config, ci_jobs
from app.db import get_db
from app.users import create_local_user


def _group(db, email="q@example.com"):
    uid = create_local_user(email, "password123")
    return uid, ci_config.create_group(db, uid, "G")


def test_enqueue_claim_finish_lifecycle(app):
    with app.app_context():
        db = get_db()
        uid, gid = _group(db)
        rid = ci_jobs.enqueue_run(db, gid, "one_time")
        assert ci_jobs.has_active_run(db, gid) is True

        claimed = ci_jobs.claim_next_run(db)
        assert claimed is not None and claimed["id"] == rid
        assert claimed["status"] == "running"
        assert claimed["started_at"] is not None
        # Nothing else queued.
        assert ci_jobs.claim_next_run(db) is None

        ci_jobs.finish_run(db, rid)
        row = ci_jobs.get_run(db, rid, uid)
        assert row["status"] == "done"
        assert row["finished_at"] is not None
        assert ci_jobs.has_active_run(db, gid) is False


def test_fail_run_records_message(app):
    with app.app_context():
        db = get_db()
        _, gid = _group(db)
        rid = ci_jobs.enqueue_run(db, gid, "monitoring", slot="morning")
        ci_jobs.claim_next_run(db)
        ci_jobs.fail_run(db, rid, "boom" * 500)
        row = db.execute("SELECT * FROM ci_runs WHERE id = ?", (rid,)).fetchone()
        assert row["status"] == "error"
        assert len(row["error"]) <= 500


def test_get_run_is_ownership_scoped(app):
    with app.app_context():
        db = get_db()
        uid, gid = _group(db, "owner@example.com")
        intruder = create_local_user("intruder@example.com", "password123")
        rid = ci_jobs.enqueue_run(db, gid)
        assert ci_jobs.get_run(db, rid, uid) is not None
        assert ci_jobs.get_run(db, rid, intruder) is None


def test_write_search_results_and_share_of_search_rollup(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("r@example.com", "password123")
        gid = ci_config.create_group(db, uid, "G")
        b_mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        b_comp = ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
        kid = ci_config.add_keyword(db, gid, uid, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid)

        # Page-1 mix: mine has 1 organic + 1 sponsored, competitor 1 organic,
        # and 2 unmatched cards ("other", brand_id None).
        results = [
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": "2026-08-22",
             "position": 1, "position_type": "sponsored", "item_id": "1", "brand_id": b_mine, "is_new_sku": 0},
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": "2026-08-22",
             "position": 2, "position_type": "organic", "item_id": "2", "brand_id": b_mine, "is_new_sku": 0},
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": "2026-08-22",
             "position": 3, "position_type": "organic", "item_id": "3", "brand_id": b_comp, "is_new_sku": 0},
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": "2026-08-22",
             "position": 4, "position_type": "organic", "item_id": "4", "brand_id": None, "is_new_sku": 0},
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": "2026-08-22",
             "position": 5, "position_type": "sponsored", "item_id": "5", "brand_id": None, "is_new_sku": 0},
        ]
        ci_jobs.write_search_results(db, results)
        ci_jobs.write_share_of_search(db, rid, gid, kid, "2026-08-22", None, results)

        assert db.execute("SELECT COUNT(*) FROM ci_search_results").fetchone()[0] == 5

        sos = {r["brand_id"]: r for r in db.execute(
            "SELECT * FROM ci_share_of_search WHERE run_id = ?", (rid,)
        ).fetchall()}
        # mine: 1 organic + 1 sponsored = 2 total
        assert (sos[b_mine]["organic_count"], sos[b_mine]["sponsored_count"], sos[b_mine]["total_count"]) == (1, 1, 2)
        # competitor: 1 organic
        assert (sos[b_comp]["organic_count"], sos[b_comp]["total_count"]) == (1, 1)
        # "other" bucket (NULL brand): 1 organic + 1 sponsored
        assert (sos[None]["organic_count"], sos[None]["sponsored_count"]) == (1, 1)


def test_list_and_count_runs_for_admin(app):
    with app.app_context():
        db = get_db()
        uid, gid = _group(db, "admin-view@example.com")
        ci_jobs.enqueue_run(db, gid, "one_time")
        ci_jobs.enqueue_run(db, gid, "monitoring", "night")
        assert ci_jobs.count_runs(db) == 2
        rows = ci_jobs.list_runs(db)
        assert rows[0]["group_name"] == "G"
        assert rows[0]["user_email"] == "admin-view@example.com"
