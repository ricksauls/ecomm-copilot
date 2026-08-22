"""Tests for the monitoring enqueue logic (systemd calls app.enqueue_monitoring)."""

from app import ci_config, ci_jobs
from app.db import get_db
from app.enqueue_monitoring import enqueue_all
from app.users import create_local_user


def test_enqueues_only_monitoring_enabled_groups(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("m@example.com", "password123")
        on = ci_config.create_group(db, uid, "On")
        ci_config.create_group(db, uid, "Off")  # monitoring off by default
        ci_config.set_monitoring(db, on, uid, True)

        n = enqueue_all(db, "morning")
        assert n == 1
        run = ci_jobs.latest_run(db, on)
        assert run["run_type"] == "monitoring"
        assert run["slot"] == "morning"


def test_skips_group_with_active_run(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("s@example.com", "password123")
        gid = ci_config.create_group(db, uid, "G")
        ci_config.set_monitoring(db, gid, uid, True)
        # A run is already queued — the next sweep should not pile on a duplicate.
        ci_jobs.enqueue_run(db, gid, "monitoring", "morning")
        assert enqueue_all(db, "afternoon") == 0
        assert db.execute("SELECT COUNT(*) FROM ci_runs WHERE group_id=?", (gid,)).fetchone()[0] == 1
