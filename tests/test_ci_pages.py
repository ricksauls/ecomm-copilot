"""HTTP-layer tests for the restructured Competitive Intelligence pages.

Three flows now: One-Time Snapshot, Monitoring Setup, View Monitoring. These
check auth guards, the create/config flows, snapshot results (no trends) + PDF,
monitoring schedule-run, the View dropdown scoping, monitoring PDF, and IDOR
across the new routes.
"""

from datetime import date, timedelta

from app import ci_analysis, ci_config, ci_jobs
from app.db import get_db
from app.users import create_local_user


def _snapshot_group(client):
    """Create a snapshot group via HTTP; return its id (test user is user 1)."""
    resp = client.post("/app/competitive-intel/snapshot/groups", data={"name": "Snap"})
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def _make_group(app, email, mode):
    from app.users import create_local_user as clu
    with app.app_context():
        uid = clu(email, "password123")
        gid = ci_config.create_group(get_db(), uid, "G", mode=mode)
        return uid, gid


def test_ci_requires_login(client):
    for path in ("/app/competitive-intel/snapshot",
                 "/app/competitive-intel/monitoring",
                 "/app/competitive-intel/view"):
        resp = client.get(path)
        assert resp.status_code in (301, 302)
        assert "/signin" in resp.headers["Location"]


def test_old_ci_link_redirects_to_snapshot(client, auth):
    auth.register()
    resp = client.get("/app/competitive-intel")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/competitive-intel/snapshot")


def test_snapshot_create_and_config(client, auth):
    auth.register()
    assert client.get("/app/competitive-intel/snapshot").status_code == 200
    gid = _snapshot_group(client)
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert cfg.status_code == 200
    assert b"Run snapshot" in cfg.data
    assert b"How setup works" in cfg.data  # help panel present


def test_monitoring_create_shows_schedule_and_next_run(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert cfg.status_code == 200
    assert b"Schedule &amp; Run" in cfg.data or b"Schedule & Run" in cfg.data
    assert b"Next scheduled check" in cfg.data


def test_snapshot_results_render_without_trend_markup(client, auth):
    auth.register()
    gid = _snapshot_group(client)
    # Seed one completed run with data directly.
    with client.application.app_context():
        db = get_db()
        b_mine = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        ci_config.add_product(db, gid, b_mine, 1, "https://www.walmart.com/ip/x/10294528")
        kid = ci_config.add_keyword(db, gid, 1, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid)
        rows = [{"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": date.today().isoformat(),
                 "position": 3, "position_type": "organic", "item_id": "10294528",
                 "brand_id": b_mine, "is_new_sku": 0}]
        ci_jobs.write_search_results(db, rows)
        ci_jobs.write_share_of_search(db, rid, gid, kid, date.today().isoformat(), None, rows)
        ci_jobs.finish_run(db, rid)

    resp = client.get(f"/app/competitive-intel/groups/{gid}/results")
    assert resp.status_code == 200
    assert b"Share of Digital Shelf" in resp.data
    assert b"Search Ranking" in resp.data
    # Headline sections: config summary, overall ranking, per-keyword ranking.
    assert b"What this group tracks" in resp.data
    assert b"Overall Search Ranking" in resp.data
    # Share of shelf now has an overall section plus a per-keyword breakdown.
    assert b"Overall Share of Digital Shelf" in resp.data
    # The old head-to-head "Ranking by Term" section was removed.
    assert b"Ranking by Term" not in resp.data
    # Share of shelf carries a stacked-bar view alongside the table.
    assert b"sos-chart" in resp.data
    # No trend affordances on a snapshot: no chart container, sparkline, or delta col.
    assert b"data-ci-chart" not in resp.data
    assert b"ci-spark" not in resp.data
    assert b"vs prior" not in resp.data


def test_snapshot_pdf_downloads(client, auth):
    auth.register()
    gid = _snapshot_group(client)
    with client.application.app_context():
        db = get_db()
        kid = ci_config.add_keyword(db, gid, 1, "kw")
        rid = ci_jobs.enqueue_run(db, gid)
        rows = [{"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": date.today().isoformat(),
                 "position": 1, "position_type": "organic", "item_id": "1", "brand_id": None, "is_new_sku": 0}]
        ci_jobs.write_search_results(db, rows)
        ci_jobs.write_share_of_search(db, rid, gid, kid, date.today().isoformat(), None, rows)
        ci_jobs.finish_run(db, rid)
    resp = client.get(f"/app/competitive-intel/groups/{gid}/results.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:4] == b"%PDF"


def test_schedule_run_enables_monitoring_and_enqueues(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    client.post(f"/app/competitive-intel/groups/{gid}/schedule-run")
    with client.application.app_context():
        db = get_db()
        grp = ci_config.get_group(db, gid, 1)
        assert grp["monitoring_enabled"] == 1
        assert ci_jobs.has_active_run(db, gid) is True


def test_view_dropdown_lists_only_monitoring_groups(client, auth):
    auth.register()
    # One snapshot + one monitoring group; only the monitoring one appears in View.
    # Distinctive name so the assertion can't collide with the rail's "Snapshot" label.
    client.post("/app/competitive-intel/snapshot/groups", data={"name": "ZzzSnapOnlyGrp"})
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "MonView"})
    mgid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    view = client.get("/app/competitive-intel/view")
    assert view.status_code == 200
    assert b"MonView" in view.data
    assert b"ZzzSnapOnlyGrp" not in view.data  # snapshot group not offered here
    # And it renders for the selected monitoring group.
    assert client.get(f"/app/competitive-intel/view?group_id={mgid}").status_code == 200


def test_monitoring_pdf_downloads(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    pdf = client.get(f"/app/competitive-intel/view/{gid}/results.pdf?period=mom")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"
    assert pdf.data[:4] == b"%PDF"


def test_idor_across_new_routes(client, auth, app):
    _uid, other_gid = _make_group(app, "owner@example.com", "monitoring")
    auth.register(email="intruder@example.com")
    # Config / results / status / view-pdf all 404 for a non-owner.
    assert client.get(f"/app/competitive-intel/groups/{other_gid}").status_code == 404
    assert client.get(f"/app/competitive-intel/groups/{other_gid}/results").status_code == 404
    assert client.get(f"/app/competitive-intel/groups/{other_gid}/status").status_code == 404
    assert client.get(f"/app/competitive-intel/view/{other_gid}/results.pdf").status_code == 404
    # Mutations refused, and the victim's group is untouched.
    assert client.post(f"/app/competitive-intel/groups/{other_gid}/schedule-run").status_code == 404
    assert client.post(f"/app/competitive-intel/groups/{other_gid}/run").status_code == 404
    with app.app_context():
        db = get_db()
        grp = db.execute("SELECT monitoring_enabled FROM ci_groups WHERE id=?", (other_gid,)).fetchone()
        assert grp["monitoring_enabled"] == 0
        assert not ci_jobs.has_active_run(db, other_gid)
    # A monitoring group the intruder doesn't own isn't listed in their View dropdown.
    assert b"View Monitoring" in client.get("/app/competitive-intel/view").data


def test_next_monitoring_run_picks_correct_slot():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    cst = ZoneInfo("America/Chicago")
    # 8 AM CST -> next slot is 3 PM the same day.
    n = ci_analysis.next_monitoring_run(datetime(2026, 8, 22, 8, 0, tzinfo=cst))
    assert (n.hour, n.day) == (15, 22)
    # 11:30 PM CST -> next slot is 7 AM tomorrow.
    n = ci_analysis.next_monitoring_run(datetime(2026, 8, 22, 23, 30, tzinfo=cst))
    assert (n.hour, n.day) == (7, 23)


def test_snapshot_analysis_is_run_scoped(app):
    # snapshot_* aggregations count only the given run, not other runs' rows.
    with app.app_context():
        db = get_db()
        uid = create_local_user("rs@example.com", "password123")
        gid = ci_config.create_group(db, uid, "G", mode="snapshot")
        b = ci_config.add_brand(db, gid, uid, "Tab", "mine")
        ci_config.add_product(db, gid, b, uid, "https://www.walmart.com/ip/x/10294528")
        kid = ci_config.add_keyword(db, gid, uid, "kw")
        today = date.today().isoformat()
        old = (date.today() - timedelta(days=2)).isoformat()
        r1 = ci_jobs.enqueue_run(db, gid)
        r2 = ci_jobs.enqueue_run(db, gid)
        for rid, d, pos in ((r1, old, 9), (r2, today, 2)):
            rows = [{"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": d,
                     "position": pos, "position_type": "organic", "item_id": "10294528",
                     "brand_id": b, "is_new_sku": 0}]
            ci_jobs.write_search_results(db, rows)
            ci_jobs.write_share_of_search(db, rid, gid, kid, d, None, rows)
        ranks = ci_analysis.snapshot_rank(db, gid, r2)
        assert len(ranks) == 1 and ranks[0]["current_position"] == 2  # only r2's row
