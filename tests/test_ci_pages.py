"""HTTP-layer tests for the restructured Competitive Intelligence pages.

Three flows now: One-Time Snapshot, Monitoring Setup, View Monitoring. These
check auth guards, the create/config flows, snapshot results (no trends) + PDF,
monitoring schedule-run, the View dropdown scoping, monitoring PDF, and IDOR
across the new routes.
"""

from datetime import date, timedelta

import io

from app import ci_analysis, ci_config, ci_images, ci_jobs
from app.db import get_db
from app.users import create_local_user


def _png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 120), (200, 16, 46)).save(buf, "PNG")
    return buf.getvalue()


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


def test_add_multiple_keywords_comma_separated(client, auth):
    auth.register()
    gid = _snapshot_group(client)
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/keywords",
        data={"keyword": "hot sauce, chipotle sauce, wing sauce,"},  # trailing comma ok
    )
    assert resp.status_code in (302, 303)
    with client.application.app_context():
        kws = {k["keyword"] for k in ci_config.list_keywords(get_db(), gid, 1)}
    assert kws == {"hot sauce", "chipotle sauce", "wing sauce"}


def test_monitoring_create_shows_tracking_setup(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert cfg.status_code == 200
    # A fresh monitoring group lands on the redesigned Competitive Tracking setup
    # screen: tracking is off, so it offers to turn it on and names the schedule.
    assert b"Turn on tracking" in cfg.data
    # The sweep now runs once a day at a rotating time (not 3x/day).
    assert b"rotating time" in cfg.data
    assert b"7 AM, 3 PM, or 11 PM CST" in cfg.data
    # The setup sections (your brand, competitors, keywords) are all present.
    assert b"Your brand" in cfg.data
    assert b"Competitors" in cfg.data


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
        ci_jobs.write_share_of_search(db, rid, gid, kid, date.today().isoformat(), None, rows, {"10294528"})
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
    # ...and the brand-level section (all of a brand's SKUs) closes the report.
    assert b"Brand Share of Digital Shelf" in resp.data
    # The old head-to-head "Ranking by Term" section was removed.
    assert b"Ranking by Term" not in resp.data
    # Share of shelf carries a stacked-bar view alongside the table.
    assert b"sos-chart" in resp.data
    # No trend affordances on a snapshot: no chart container, sparkline, or delta col.
    assert b"data-ci-chart" not in resp.data
    assert b"ci-spark" not in resp.data
    assert b"vs prior" not in resp.data


def test_snapshot_shows_brand_ads_section_and_serves_creative(client, auth, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    gid = _snapshot_group(client)
    today = date.today().isoformat()
    with client.application.app_context():
        db = get_db()
        b = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        ci_config.add_product(db, gid, b, 1, "https://www.walmart.com/ip/x/10294528")
        kid = ci_config.add_keyword(db, gid, 1, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid)
        rows = [{"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": today,
                 "position": 1, "position_type": "organic", "item_id": "10294528",
                 "brand_id": b, "is_new_sku": 0}]
        ci_jobs.write_search_results(db, rows)
        ci_jobs.write_share_of_search(db, rid, gid, kid, today, None, rows, {"10294528"})
        rel = ci_images.save_ad_image(rid, kid, "headline", _png_bytes())
        ci_jobs.write_ad_units(db, [{"run_id": rid, "group_id": gid, "keyword_id": kid,
            "scraped_at": today, "ad_type": "headline", "brand_id": b,
            "brand_text": "Tabasco", "image_path": rel}])
        ci_jobs.finish_run(db, rid)

    resp = client.get(f"/app/competitive-intel/groups/{gid}/results")
    assert resp.status_code == 200
    assert b"Brand Advertising Presence" in resp.data
    # The captured creative is served same-origin (CSP img-src 'self').
    img = client.get(f"/media/ci-ad/{rid}/{kid}/headline")
    assert img.status_code == 200 and img.mimetype == "image/jpeg"
    # An unknown ad_type is rejected (no such creative).
    assert client.get(f"/media/ci-ad/{rid}/{kid}/banner").status_code == 404


def test_snapshot_results_show_rank_placement_map(client, auth):
    auth.register()
    gid = _snapshot_group(client)
    with client.application.app_context():
        db = get_db()
        b_mine = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        b_comp = ci_config.add_brand(db, gid, 1, "Frank's", "competitor")
        kid = ci_config.add_keyword(db, gid, 1, "hot sauce")
        # Ranking counts only tracked items, so register the two SKUs below.
        for bid, item in ((b_mine, "1"), (b_comp, "2")):
            db.execute(
                "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url) "
                "VALUES (?,?,?,?,?)",
                (gid, bid, None, item, f"https://www.walmart.com/ip/x/{item}"),
            )
        rid = ci_jobs.enqueue_run(db, gid)
        rows = [
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": date.today().isoformat(),
             "position": 3, "position_type": "organic", "item_id": "1", "brand_id": b_mine, "is_new_sku": 0},
            {"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": date.today().isoformat(),
             "position": 9, "position_type": "organic", "item_id": "2", "brand_id": b_comp, "is_new_sku": 0},
        ]
        ci_jobs.write_search_results(db, rows)
        ci_jobs.finish_run(db, rid)

    resp = client.get(f"/app/competitive-intel/groups/{gid}/results")
    assert resp.status_code == 200
    # The placement grid renders under Overall Search Ranking, with a lit tile
    # carrying each brand's exact average.
    assert b"rankmap" in resp.data
    assert b"rankmap-cell mine" in resp.data
    assert b"avg #3.0" in resp.data


def test_ci_product_image_route(client, auth, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    # Auth required.
    assert client.get("/media/ci-product/10294528").status_code in (302, 401)
    auth.register()
    # Missing image -> 404; non-numeric id -> 404 (path-traversal guard).
    assert client.get("/media/ci-product/10294528").status_code == 404
    assert client.get("/media/ci-product/notanid").status_code == 404
    # Cached image -> 200 image/jpeg.
    assert ci_images.save_product_image("10294528", _png_bytes()) is True
    resp = client.get("/media/ci-product/10294528")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"


def test_snapshot_results_show_product_thumbnails(client, auth, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    gid = _snapshot_group(client)
    with client.application.app_context():
        db = get_db()
        b = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        ci_config.add_product(db, gid, b, 1, "https://www.walmart.com/ip/x/10294528")
        ci_config.add_keyword(db, gid, 1, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid)
        ci_jobs.finish_run(db, rid)
    ci_images.save_product_image("10294528", _png_bytes())

    resp = client.get(f"/app/competitive-intel/groups/{gid}/results")
    assert resp.status_code == 200
    # The tracked-product grid renders, with the cached image served same-origin.
    assert b"track-grid" in resp.data
    assert b"/media/ci-product/10294528" in resp.data


def test_snapshot_products_listed_mine_first(client, auth):
    auth.register()
    gid = _snapshot_group(client)
    with client.application.app_context():
        db = get_db()
        # Competitor brand sorts alphabetically before "Tabasco"; without the
        # mine-first rule it would render on the left.
        comp = ci_config.add_brand(db, gid, 1, "Aardvark", "competitor")
        mine = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        ci_config.add_product(db, gid, comp, 1, "https://www.walmart.com/ip/x/11100000")
        ci_config.add_product(db, gid, mine, 1, "https://www.walmart.com/ip/x/22200000")
        ci_config.add_keyword(db, gid, 1, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid)
        ci_jobs.finish_run(db, rid)

    body = client.get(f"/app/competitive-intel/groups/{gid}/results").data.decode()
    # My item (22200000) appears before the competitor's (11100000).
    assert body.index("22200000") < body.index("11100000")


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
        ci_jobs.write_share_of_search(db, rid, gid, kid, date.today().isoformat(), None, rows, set())
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


def test_schedule_from_snapshot_clones_into_monitoring_and_enqueues(client, auth):
    auth.register()
    gid = _snapshot_group(client)
    with client.application.app_context():
        db = get_db()
        b = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        ci_config.add_product(db, gid, b, 1, "https://www.walmart.com/ip/x/10294528")
        ci_config.add_keyword(db, gid, 1, "hot sauce")

    resp = client.post(f"/app/competitive-intel/groups/{gid}/schedule-monitoring")
    # Redirects to the NEW group's config screen.
    assert resp.status_code == 302
    new_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    assert new_id != gid

    with client.application.app_context():
        db = get_db()
        new = ci_config.get_group(db, new_id, 1)
        assert new["mode"] == "monitoring" and new["monitoring_enabled"] == 1
        assert ci_jobs.has_active_run(db, new_id) is True
        # Source snapshot untouched: still a snapshot, no run queued on it.
        src = ci_config.get_group(db, gid, 1)
        assert src["mode"] == "snapshot" and src["monitoring_enabled"] == 0
        assert ci_jobs.has_active_run(db, gid) is False


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


def test_monitoring_view_matches_snapshot_layout_with_trends(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "MonTrend"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    # Seed data inside the last *completed* week (the route uses the real "today"),
    # so the aggregated tables + week button are available.
    from app import ci_analysis
    wk_start, _wk_end = ci_analysis.period_bounds("wow", 0)
    d1 = wk_start
    d2 = (date.fromisoformat(wk_start) + timedelta(days=2)).isoformat()
    with client.application.app_context():
        db = get_db()
        b_mine = ci_config.add_brand(db, gid, 1, "Tabasco", "mine")
        ci_config.add_product(db, gid, b_mine, 1, "https://www.walmart.com/ip/x/10294528")
        kid = ci_config.add_keyword(db, gid, 1, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid, run_type="monitoring")
        rows = []
        for d, pos in ((d1, 5), (d2, 3)):
            rows.append({"run_id": rid, "group_id": gid, "keyword_id": kid, "scraped_at": d,
                         "position": pos, "position_type": "organic", "item_id": "10294528",
                         "brand_id": b_mine, "is_new_sku": 0})
        ci_jobs.write_search_results(db, rows)
        for d in (d1, d2):
            day_rows = [r for r in rows if r["scraped_at"] == d]
            ci_jobs.write_share_of_search(db, rid, gid, kid, d, None, day_rows, {"10294528"})
        ci_jobs.finish_run(db, rid)

    resp = client.get(f"/app/competitive-intel/view?group_id={gid}")
    assert resp.status_code == 200
    # Same section set as the One-Time Snapshot results page…
    assert b"What this group tracks" in resp.data
    assert b"Overall Search Ranking" in resp.data
    assert b"Overall Share of Digital Shelf" in resp.data
    assert b"Brand Share of Digital Shelf" in resp.data  # brand-level section (all SKUs)
    assert b"sos-chart" in resp.data
    assert b"rankmap" in resp.data
    # …plus the monitoring extras: the "vs prior" delta column and trend sparklines.
    assert b"ci-spark" in resp.data
    assert b"vs prior" in resp.data
    assert b"data-better=\"high\"" in resp.data  # share trend renders higher = up


def test_view_snapshot_dropdown_and_results_without_trends(client, auth):
    auth.register()
    # One snapshot + one monitoring group; only the snapshot one appears in View Snapshot.
    client.post("/app/competitive-intel/monitoring/groups", data={"name": "ZzzMonOnlyGrp"})
    resp = client.post("/app/competitive-intel/snapshot/groups", data={"name": "SnapView"})
    sgid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    with client.application.app_context():
        db = get_db()
        b_mine = ci_config.add_brand(db, sgid, 1, "Tabasco", "mine")
        ci_config.add_product(db, sgid, b_mine, 1, "https://www.walmart.com/ip/x/10294528")
        kid = ci_config.add_keyword(db, sgid, 1, "hot sauce")
        rid = ci_jobs.enqueue_run(db, sgid)
        rows = [{"run_id": rid, "group_id": sgid, "keyword_id": kid, "scraped_at": date.today().isoformat(),
                 "position": 3, "position_type": "organic", "item_id": "10294528",
                 "brand_id": b_mine, "is_new_sku": 0}]
        ci_jobs.write_search_results(db, rows)
        ci_jobs.write_share_of_search(db, rid, sgid, kid, date.today().isoformat(), None, rows, {"10294528"})
        ci_jobs.finish_run(db, rid)

    view = client.get("/app/competitive-intel/view-snapshot")
    assert view.status_code == 200
    assert b"SnapView" in view.data
    assert b"ZzzMonOnlyGrp" not in view.data  # monitoring group not offered here

    resp = client.get(f"/app/competitive-intel/view-snapshot?group_id={sgid}")
    assert resp.status_code == 200
    # Same snapshot sections…
    assert b"What this group tracks" in resp.data
    assert b"Overall Search Ranking" in resp.data
    assert b"Overall Share of Digital Shelf" in resp.data
    assert b"sos-chart" in resp.data
    # …and NO trend affordances (that's the monitoring view's addition).
    assert b"ci-spark" not in resp.data


def test_view_snapshot_rejects_monitoring_group(client, auth, app):
    # A monitoring group id passed to View Snapshot isn't shown (mode guard).
    _uid, mgid = _make_group(app, "snapowner@example.com", "monitoring")
    auth.register(email="snapviewer@example.com")
    resp = client.get(f"/app/competitive-intel/view-snapshot?group_id={mgid}")
    assert resp.status_code == 200  # renders, but the group isn't selected/shown
    assert b"What this group tracks" not in resp.data


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
    assert client.post(
        f"/app/competitive-intel/groups/{other_gid}/schedule-monitoring").status_code == 404
    with app.app_context():
        db = get_db()
        grp = db.execute("SELECT monitoring_enabled FROM ci_groups WHERE id=?", (other_gid,)).fetchone()
        assert grp["monitoring_enabled"] == 0
        assert not ci_jobs.has_active_run(db, other_gid)
    # A monitoring group the intruder doesn't own isn't listed in their View dropdown.
    assert b"View Monitoring" in client.get("/app/competitive-intel/view").data


def test_next_monitoring_run_follows_daily_rotation():
    from datetime import date, datetime
    from zoneinfo import ZoneInfo

    from app import scrape_rotation
    cst = ZoneInfo("America/Chicago")
    hours = ci_analysis.MONITORING_HOURS

    # Early in the day -> today's one rotation slot (whichever the date selects).
    today_hour = hours[scrape_rotation.active_index(date(2026, 8, 22))]
    n = ci_analysis.next_monitoring_run(datetime(2026, 8, 22, 0, 1, tzinfo=cst))
    assert (n.year, n.month, n.day, n.hour) == (2026, 8, 22, today_hour)

    # After the last possible slot time -> tomorrow's rotation slot.
    tmr_hour = hours[scrape_rotation.active_index(date(2026, 8, 23))]
    n = ci_analysis.next_monitoring_run(datetime(2026, 8, 22, 23, 59, tzinfo=cst))
    assert (n.year, n.month, n.day, n.hour) == (2026, 8, 23, tmr_hour)


def test_format_run_time_cst_converts_utc_to_central():
    # A stored UTC run timestamp renders as Central wall-clock (matches the
    # schedule label), not the raw UTC that confused the "Latest run" line.
    # 04:00 UTC on the 24th is the prior day's 11 PM CDT night run.
    assert ci_analysis.format_run_time_cst("2026-08-24 04:00:04") == "Sun Aug 23, 11:00 PM CST"
    # DST off in January: 13:00 UTC -> 7 AM CST (UTC-6).
    assert ci_analysis.format_run_time_cst("2026-01-15 13:00:00") == "Thu Jan 15, 7:00 AM CST"
    # Empty -> None; an unparseable value falls back to the raw string, not a crash.
    assert ci_analysis.format_run_time_cst(None) is None
    assert ci_analysis.format_run_time_cst("garbage") == "garbage"


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
            ci_jobs.write_share_of_search(db, rid, gid, kid, d, None, rows, {"10294528"})
        ranks = ci_analysis.snapshot_rank(db, gid, r2)
        assert len(ranks) == 1 and ranks[0]["current_position"] == 2  # only r2's row


# ── Competitive Tracking setup: portfolio-backed bulk product add ─────────────

def _monitoring_group(client):
    """Create a monitoring group via HTTP; return its id (test user is user 1)."""
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Track"})
    return int(resp.headers["Location"].rstrip("/").split("/")[-1])


def test_tracking_config_renders_setup_sections(client, auth):
    # A monitoring group opens the redesigned setup screen with all three sections.
    auth.register()
    gid = _monitoring_group(client)
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert cfg.status_code == 200
    assert b"Your brand" in cfg.data
    assert b"Competitors" in cfg.data
    assert b"Keywords" in cfg.data
    # The portfolio picker only appears once a brand exists (products attach to one).
    with client.application.app_context():
        ci_config.add_brand(get_db(), gid, 1, "Tabasco", "mine")
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert b"managed items" in cfg.data  # the portfolio picker macro's label


def test_add_products_bulk_and_dedupe(client, auth):
    # The picker posts several `urls`; duplicates (repeated or already tracked) are
    # skipped rather than creating double rows, since ci_products has no DB uniqueness.
    auth.register()
    gid = _monitoring_group(client)
    with client.application.app_context():
        brand_id = ci_config.add_brand(get_db(), gid, 1, "Tabasco", "mine")

    u1 = "https://www.walmart.com/ip/Tabasco-Original/10294528"
    u2 = "https://www.walmart.com/ip/Tabasco-Chipotle/20857711518"
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/products/bulk",
        data={"brand_id": brand_id, "urls": [u1, u2, u1]},  # u1 repeated in one submit
    )
    assert resp.status_code in (302, 303)
    with client.application.app_context():
        ids = {p["walmart_item_id"] for p in ci_config.list_products(get_db(), gid, 1)}
    assert ids == {"10294528", "20857711518"}  # repeat collapsed to one

    # Re-submitting an already-tracked item adds nothing.
    client.post(
        f"/app/competitive-intel/groups/{gid}/products/bulk",
        data={"brand_id": brand_id, "urls": [u1]},
    )
    with client.application.app_context():
        count = len(ci_config.list_products(get_db(), gid, 1))
    assert count == 2


def test_add_products_manual_field_unions_with_picker(client, auth):
    # The manual `url` field is added alongside the picker's `urls` in one submit.
    auth.register()
    gid = _monitoring_group(client)
    with client.application.app_context():
        brand_id = ci_config.add_brand(get_db(), gid, 1, "Tabasco", "mine")
    client.post(
        f"/app/competitive-intel/groups/{gid}/products/bulk",
        data={"brand_id": brand_id,
              "urls": ["https://www.walmart.com/ip/a/10294528"],
              "url": "https://www.walmart.com/ip/b/20857711518"},
    )
    with client.application.app_context():
        ids = {p["walmart_item_id"] for p in ci_config.list_products(get_db(), gid, 1)}
    assert ids == {"10294528", "20857711518"}


def test_add_products_requires_brand(client, auth):
    # Without a brand_id nothing is added (products must attach to a brand).
    auth.register()
    gid = _monitoring_group(client)
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/products/bulk",
        data={"urls": ["https://www.walmart.com/ip/a/10294528"]},
        follow_redirects=True,
    )
    assert b"Choose which of your brands" in resp.data
    with client.application.app_context():
        assert ci_config.list_products(get_db(), gid, 1) == []


def test_add_products_reports_invalid_url(client, auth):
    # A URL with no trailing item number is reported invalid and not added.
    auth.register()
    gid = _monitoring_group(client)
    with client.application.app_context():
        brand_id = ci_config.add_brand(get_db(), gid, 1, "Tabasco", "mine")
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/products/bulk",
        data={"brand_id": brand_id, "urls": ["https://www.walmart.com/ip/no-number"]},
        follow_redirects=True,
    )
    assert b"Not a valid Walmart product URL" in resp.data
    with client.application.app_context():
        assert ci_config.list_products(get_db(), gid, 1) == []


def test_add_products_bulk_idor_guard(client, auth, app):
    # A user cannot bulk-add products to another user's group.
    owner_uid, gid = _make_group(app, "owner@example.com", "monitoring")
    with app.app_context():
        brand_id = ci_config.add_brand(get_db(), gid, owner_uid, "B", "mine")
    auth.register()  # logs in as a second, unrelated user — the group isn't theirs
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/products/bulk",
        data={"brand_id": brand_id, "urls": ["https://www.walmart.com/ip/a/10294528"]},
    )
    assert resp.status_code == 404
    with client.application.app_context():
        assert ci_config.list_products(get_db(), gid, owner_uid) == []
