"""Tests for scheduled (cadenced) business reports.

Covers app.report_schedules (CRUD, validation, IDOR, due-date math),
app.report_store's async queue (enqueue/claim/finish/fail/reclaim, seen badge,
retention), app.enqueue_reports (enqueue due + dedupe + prune), the worker's
process_report_one, and the /app/reports/schedules routes.
"""

import os
from datetime import date, timedelta

import pytest

from app import ci_config, report_schedules as rs, report_store
from app.db import get_db
from app.users import create_local_user


@pytest.fixture
def media(tmp_path, monkeypatch):
    """Point the media root at a clean temp dir for file-writing tests."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    return tmp_path


def _uid(db, email="user@example.com"):
    return db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def _group(db, uid, name="Tabasco"):
    """A monitoring group with one mine brand (enough for assemble to build)."""
    gid = ci_config.create_group(db, uid, name, mode="monitoring")
    ci_config.add_brand(db, gid, uid, name, "mine")
    db.commit()
    return gid


# ── Validation + helpers ─────────────────────────────────────────────────────

def test_parse_formats():
    assert rs.parse_formats("pdf,pptx") == ["pdf", "pptx"]
    assert rs.parse_formats(["pptx"]) == ["pptx"]
    assert rs.parse_formats("pdf,exe,pdf") == ["pdf"]   # unknown dropped, deduped
    assert rs.parse_formats("") == []


def test_period_for_cadence():
    assert rs.period_for_cadence("weekly") == "wow"
    assert rs.period_for_cadence("monthly") == "mom"


def test_create_validation(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("v@example.com", "password123")
        gid = _group(db, uid)
        with pytest.raises(rs.ScheduleError):  # bad cadence
            rs.create_schedule(db, user_id=uid, group_id=gid, cadence="daily",
                               day_of_week=1, formats=["pdf"])
        with pytest.raises(rs.ScheduleError):  # weekly without a valid weekday
            rs.create_schedule(db, user_id=uid, group_id=gid, cadence="weekly",
                               day_of_week=9, formats=["pdf"])
        with pytest.raises(rs.ScheduleError):  # monthly day out of range (29)
            rs.create_schedule(db, user_id=uid, group_id=gid, cadence="monthly",
                               day_of_month=29, formats=["pdf"])
        with pytest.raises(rs.ScheduleError):  # no formats
            rs.create_schedule(db, user_id=uid, group_id=gid, cadence="weekly",
                               day_of_week=0, formats=[])


# ── CRUD + IDOR ──────────────────────────────────────────────────────────────

def test_crud_and_idor(app):
    with app.app_context():
        db = get_db()
        owner = create_local_user("o@example.com", "password123")
        other = create_local_user("x@example.com", "password123")
        gid = _group(db, owner)
        sid = rs.create_schedule(db, user_id=owner, group_id=gid, cadence="monthly",
                                 day_of_month=15, formats=["pdf", "pptx"])
        row = rs.get_schedule(db, sid, owner)
        assert row["cadence"] == "monthly" and row["day_of_month"] == 15
        assert row["formats"] == "pdf,pptx" and row["day_of_week"] is None
        # Another user can't see, update, toggle, or delete it.
        assert rs.get_schedule(db, sid, other) is None
        assert rs.set_enabled(db, sid, other, False) is False
        assert rs.delete_schedule(db, sid, other) is False
        # Owner can update it to weekly (clears day_of_month).
        assert rs.update_schedule(db, sid, owner, group_id=gid, cadence="weekly",
                                  day_of_week=2, formats=["pdf"]) is True
        row = rs.get_schedule(db, sid, owner)
        assert row["cadence"] == "weekly" and row["day_of_week"] == 2
        assert row["day_of_month"] is None
        # Pause + delete.
        assert rs.set_enabled(db, sid, owner, False) is True
        assert rs.get_schedule(db, sid, owner)["enabled"] == 0
        assert rs.delete_schedule(db, sid, owner) is True
        assert rs.get_schedule(db, sid, owner) is None


# ── Due-date math ────────────────────────────────────────────────────────────

def test_is_due_and_next_run(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        gid = _group(db, uid)
        # Weekly on Wednesday (weekday 2).
        wid = rs.create_schedule(db, user_id=uid, group_id=gid, cadence="weekly",
                                 day_of_week=2, formats=["pdf"])
        wed = date(2026, 10, 14)   # a Wednesday
        thu = date(2026, 10, 15)   # a Thursday
        w = rs.get_schedule(db, wid, uid)
        assert rs.is_due(w, wed) is True
        assert rs.is_due(w, thu) is False
        assert rs.next_run_date(w, thu) == date(2026, 10, 21)  # next Wednesday
        # Monthly on the 15th.
        mid = rs.create_schedule(db, user_id=uid, group_id=gid, cadence="monthly",
                                 day_of_month=15, formats=["pdf"])
        m = rs.get_schedule(db, mid, uid)
        assert rs.is_due(m, date(2026, 10, 15)) is True
        assert rs.is_due(m, date(2026, 10, 16)) is False
        assert rs.next_run_date(m, date(2026, 10, 16)) == date(2026, 11, 15)
        # A paused schedule is never due.
        rs.set_enabled(db, wid, uid, False)
        assert rs.is_due(rs.get_schedule(db, wid, uid), wed) is False


def test_due_schedules_dedupe(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("dd@example.com", "password123")
        gid = _group(db, uid)
        today = date(2026, 10, 15)  # Thursday, weekday 3
        sid = rs.create_schedule(db, user_id=uid, group_id=gid, cadence="weekly",
                                 day_of_week=3, formats=["pdf"])
        assert [s["id"] for s in rs.due_schedules(db, today)] == [sid]
        # Once marked ran today, it drops out of the due set (dedupe).
        rs.mark_ran(db, sid, today)
        assert rs.due_schedules(db, today) == []
        # But it's due again next week.
        assert [s["id"] for s in rs.due_schedules(db, today + timedelta(days=7))] == [sid]


# ── report_store async queue ─────────────────────────────────────────────────

def test_async_queue(app, media):
    with app.app_context():
        db = get_db()
        uid = create_local_user("q@example.com", "password123")
        gid = _group(db, uid)
        rid = report_store.enqueue_report(
            db, user_id=uid, group_id=gid, brand_label="Tabasco", period="wow",
            period_label="This week", fmt="pdf", filename="r.pdf", schedule_id=1)
        row = report_store.get_report(db, rid, uid)
        assert row["status"] == "queued" and row["seen"] == 0 and row["rel_path"] == ""
        # Claim → running; a second claim finds nothing.
        claimed = report_store.claim_next_report(db)
        assert claimed["id"] == rid
        assert report_store.claim_next_report(db) is None
        # Finish writes the file + marks done.
        report_store.finish_report(db, rid, b"%PDF-xyz")
        row = report_store.get_report(db, rid, uid)
        assert row["status"] == "done" and row["size_bytes"] == 8
        assert os.path.isfile(report_store.abspath(row["rel_path"]))
        # Unseen badge counts it until viewed.
        assert report_store.count_unseen_scheduled(db, uid) == 1
        report_store.mark_scheduled_seen(db, uid)
        assert report_store.count_unseen_scheduled(db, uid) == 0


def test_async_fail_and_reclaim(app, media):
    with app.app_context():
        db = get_db()
        uid = create_local_user("f@example.com", "password123")
        gid = _group(db, uid)
        rid = report_store.enqueue_report(
            db, user_id=uid, group_id=gid, brand_label="B", period="wow",
            period_label=None, fmt="pdf", filename="r.pdf")
        report_store.claim_next_report(db)
        report_store.fail_report(db, rid, "boom")
        assert report_store.get_report(db, rid, uid)["status"] == "error"
        # Reclaim only touches rows left 'running'.
        r2 = report_store.enqueue_report(db, user_id=uid, group_id=gid, brand_label="B",
                                         period="wow", period_label=None, fmt="pdf",
                                         filename="r2.pdf")
        report_store.claim_next_report(db)  # -> running
        assert report_store.reclaim_orphaned_reports(db) == 1
        assert report_store.get_report(db, r2, uid)["status"] == "error"


def test_prune_old_reports(app, media):
    with app.app_context():
        db = get_db()
        uid = create_local_user("p@example.com", "password123")
        rid = report_store.save_report(db, user_id=uid, group_id=None, brand_label="B",
                                       period="wow", period_label=None, fmt="pdf",
                                       data=b"%PDF-x", filename="r.pdf")
        # Backdate it beyond the window, then prune.
        db.execute("UPDATE generated_reports SET created_at = datetime('now','-200 days') "
                   "WHERE id = ?", (rid,))
        db.commit()
        path = report_store.abspath(report_store.get_report(db, rid, uid)["rel_path"])
        assert report_store.prune_old_reports(db, days=90) == 1
        assert report_store.get_report(db, rid, uid) is None
        assert not os.path.isfile(path)


# ── enqueue_reports + worker ─────────────────────────────────────────────────

def test_enqueue_due_and_worker_build(app, media):
    import worker
    from app import enqueue_reports
    with app.app_context():
        db = get_db()
        uid = create_local_user("e@example.com", "password123")
        gid = _group(db, uid)
        today = date(2026, 10, 15)  # Thursday, weekday 3
        rs.create_schedule(db, user_id=uid, group_id=gid, cadence="weekly",
                           day_of_week=3, formats=["pdf", "pptx"])
        # Enqueue → one queued row per format.
        assert enqueue_reports.enqueue_due(db, today) == 2
        rows = report_store.list_reports(db, uid)
        assert {r["fmt"] for r in rows} == {"pdf", "pptx"}
        assert all(r["status"] == "queued" and r["source"] == "scheduled" for r in rows)
        # Re-running the same day is a no-op (schedule marked ran).
        assert enqueue_reports.enqueue_due(db, today) == 0
        # Worker builds both.
        while True:
            row = report_store.claim_next_report(db)
            if row is None:
                break
            worker.process_report_one(db, row)
        rows = report_store.list_reports(db, uid)
        assert all(r["status"] == "done" and r["size_bytes"] > 1000 for r in rows)


def test_worker_fails_when_group_deleted(app, media):
    import worker
    with app.app_context():
        db = get_db()
        uid = create_local_user("g@example.com", "password123")
        gid = _group(db, uid)
        rid = report_store.enqueue_report(db, user_id=uid, group_id=gid, brand_label="B",
                                          period="wow", period_label=None, fmt="pdf",
                                          filename="r.pdf")
        row = report_store.claim_next_report(db)
        # Delete the group out from under the queued report, then build.
        db.execute("DELETE FROM ci_groups WHERE id = ?", (gid,))
        db.commit()
        worker.process_report_one(db, row)
        assert report_store.get_report(db, rid, uid)["status"] == "error"


# ── Routes ───────────────────────────────────────────────────────────────────

def test_schedule_routes(client, auth, app, media):
    auth.register()
    with app.app_context():
        gid = _group(get_db(), _uid(get_db()))
    # Create a weekly schedule via the form.
    resp = client.post("/app/reports/schedules", data={
        "group_id": gid, "cadence": "weekly", "day_of_week": 1, "day_of_month": 1,
        "formats": ["pdf", "pptx"]})
    assert resp.status_code == 302
    with app.app_context():
        schedules = rs.list_schedules(get_db(), _uid(get_db()))
        assert len(schedules) == 1
        sid = schedules[0]["id"]
        assert schedules[0]["cadence"] == "weekly" and schedules[0]["formats"] == "pdf,pptx"
    # Toggle (pause).
    client.post(f"/app/reports/schedules/{sid}/toggle", data={"enabled": "0"})
    with app.app_context():
        assert rs.get_schedule(get_db(), sid, _uid(get_db()))["enabled"] == 0
    # Schedules tab renders.
    assert client.get("/app/reports?tab=schedules").status_code == 200
    # Delete.
    client.post(f"/app/reports/schedules/{sid}/delete")
    with app.app_context():
        assert rs.get_schedule(get_db(), sid, _uid(get_db())) is None


def test_schedule_create_validation_flashes(client, auth, app):
    auth.register()
    with app.app_context():
        gid = _group(get_db(), _uid(get_db()))
    # Monthly with an out-of-range day → redirect (flash), no schedule created.
    resp = client.post("/app/reports/schedules", data={
        "group_id": gid, "cadence": "monthly", "day_of_month": 31, "formats": ["pdf"]})
    assert resp.status_code == 302
    with app.app_context():
        assert rs.list_schedules(get_db(), _uid(get_db())) == []


def test_schedule_idor_other_users_group(client, auth, app):
    with app.app_context():
        other = create_local_user("stranger@example.com", "password123")
        gid = _group(get_db(), other)
    auth.register()
    # Scheduling against someone else's brand → 404.
    assert client.post("/app/reports/schedules", data={
        "group_id": gid, "cadence": "weekly", "day_of_week": 1,
        "formats": ["pdf"]}).status_code == 404


def test_history_clears_new_badge(client, auth, app, media):
    auth.register()
    with app.app_context():
        db = get_db()
        uid = _uid(db)
        gid = _group(db, uid)
        rid = report_store.enqueue_report(db, user_id=uid, group_id=gid, brand_label="B",
                                          period="wow", period_label=None, fmt="pdf",
                                          filename="r.pdf")
        report_store.claim_next_report(db)
        report_store.finish_report(db, rid, b"%PDF-x")
        assert report_store.count_unseen_scheduled(db, uid) == 1
    # Visiting History marks scheduled reports seen.
    client.get("/app/reports?tab=history")
    with app.app_context():
        assert report_store.count_unseen_scheduled(get_db(), _uid(get_db())) == 0
