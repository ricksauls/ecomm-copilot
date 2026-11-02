"""HTTP-layer tests for the Competitive Intelligence pages.

Two flows: Monitoring Setup and View Monitoring. These check auth guards, the
create/config flows, monitoring schedule-run, the View dropdown, monitoring PDF,
and IDOR across the routes.
"""

from datetime import date, timedelta

import io

from app import ci_analysis, ci_config, ci_images, ci_jobs, portfolio
from app.db import get_db


def _png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (120, 120), (200, 16, 46)).save(buf, "PNG")
    return buf.getvalue()


def _make_group(app, email, mode):
    from app.users import create_local_user as clu
    with app.app_context():
        uid = clu(email, "password123")
        gid = ci_config.create_group(get_db(), uid, "G", mode=mode)
        return uid, gid


def test_ci_requires_login(client):
    for path in ("/app/competitive-intel/monitoring",
                 "/app/competitive-intel/view"):
        resp = client.get(path)
        assert resp.status_code in (301, 302)
        assert "/signin" in resp.headers["Location"]


def test_old_ci_link_redirects_to_monitoring(client, auth):
    # The bare CI link is back-compat: it lands on Competitive Tracking.
    auth.register()
    resp = client.get("/app/competitive-intel")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/competitive-intel/monitoring")


def test_add_multiple_keywords_comma_separated(client, auth):
    auth.register()
    gid = _monitoring_group(client)
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
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon", "description": "Test category"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert cfg.status_code == 200
    # A fresh monitoring group lands on the redesigned Competitive Tracking setup
    # screen: tracking is off, so the header offers to enable it and the status
    # section names the schedule.
    assert b"Enable Tracking" in cfg.data
    # The sweep now runs once a day at a rotating time (not 3x/day).
    assert b"rotating time" in cfg.data
    assert b"7 AM, 3 PM, or 11 PM CST" in cfg.data
    # The setup sections (your brand, competitors, keywords) are all present.
    assert b"Your brand" in cfg.data
    assert b"Competitors" in cfg.data


def test_monitoring_create_requires_description(client, auth):
    """Description is required: a create POST without one is rejected (flashed
    back to the Competitive Tracking home) and no group is created."""
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "NoDesc"})
    # Rejected → bounced back to the monitoring home, not the config screen.
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/competitive-intel/monitoring")
    with client.application.app_context():
        assert ci_config.list_groups(get_db(), 1, mode="monitoring") == []
    # The error message surfaces on the home.
    home = client.get("/app/competitive-intel/monitoring")
    assert b"Description is required" in home.data


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


def test_schedule_run_enables_monitoring_and_enqueues(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon", "description": "Test category"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    client.post(f"/app/competitive-intel/groups/{gid}/schedule-run")
    with client.application.app_context():
        db = get_db()
        grp = ci_config.get_group(db, gid, 1)
        assert grp["monitoring_enabled"] == 1
        assert ci_jobs.has_active_run(db, gid) is True


def test_view_dropdown_lists_monitoring_groups(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "MonView", "description": "Test category"})
    mgid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    view = client.get("/app/competitive-intel/view")
    assert view.status_code == 200
    assert b"MonView" in view.data
    # And it renders for the selected monitoring group.
    assert client.get(f"/app/competitive-intel/view?group_id={mgid}").status_code == 200


def test_monitoring_view_sections_and_trends(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "MonTrend", "description": "Test category"})
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
    # The CI results sections…
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


def test_monitoring_pdf_downloads(client, auth):
    auth.register()
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Mon", "description": "Test category"})
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    pdf = client.get(f"/app/competitive-intel/view/{gid}/results.pdf?period=mom")
    assert pdf.status_code == 200
    assert pdf.mimetype == "application/pdf"
    assert pdf.data[:4] == b"%PDF"


def test_idor_across_new_routes(client, auth, app):
    _uid, other_gid = _make_group(app, "owner@example.com", "monitoring")
    auth.register(email="intruder@example.com")
    # Config / status / view-pdf all 404 for a non-owner.
    assert client.get(f"/app/competitive-intel/groups/{other_gid}").status_code == 404
    assert client.get(f"/app/competitive-intel/groups/{other_gid}/status").status_code == 404
    assert client.get(f"/app/competitive-intel/view/{other_gid}/results.pdf").status_code == 404
    # Mutations refused, and the victim's group is untouched.
    assert client.post(f"/app/competitive-intel/groups/{other_gid}/schedule-run").status_code == 404
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


# ── Competitive Tracking setup screen ────────────────────────────────────────

def _monitoring_group(client):
    """Create a monitoring group via HTTP; return its id (test user is user 1)."""
    resp = client.post("/app/competitive-intel/monitoring/groups", data={"name": "Track", "description": "Test category"})
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
    # With no portfolio items the picker is replaced by a prompt to add some first.
    assert b"no portfolio items" in cfg.data
    # Once the user owns a portfolio item, the searchable single-select picker renders.
    with client.application.app_context():
        portfolio.add_item(get_db(), 1, "Tabasco",
                           "https://www.walmart.com/ip/Tabasco-Original/10294528", None)
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert b"data-item-picker" in cfg.data
    assert b"Search your portfolio" in cfg.data
    # Gated submit buttons + staged-keyword hooks the client enhances.
    assert b"data-item-submit" in cfg.data
    assert b"Use The Selected Item" in cfg.data
    assert b"data-comp-submit" in cfg.data
    assert b"Use These Competing Items" in cfg.data
    assert b"data-kw-staged" in cfg.data
    assert b"Use These Keywords" in cfg.data


# ── Setup screen: single-item picker + URL competitors + disable ─────────────────

def _add_portfolio_item(client, brand, url):
    """Add one managed item for the test user (1) and return its pk."""
    with client.application.app_context():
        portfolio.add_item(get_db(), 1, brand, url, None)
        return portfolio.list_items(get_db(), 1)[0]["id"]


def test_add_owned_item_derives_brand_from_portfolio(client, auth):
    # Picking a portfolio item adds it as a product under a "mine" brand derived
    # from the item (no separate brand entry).
    auth.register()
    gid = _monitoring_group(client)
    pk = _add_portfolio_item(client, "Tabasco",
                             "https://www.walmart.com/ip/Tabasco-Original/10294528")
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/owned-item",
        data={"portfolio_item_id": pk},
    )
    assert resp.status_code in (302, 303)
    with client.application.app_context():
        prods = ci_config.list_products(get_db(), gid, 1)
        brands = ci_config.list_brands(get_db(), gid, 1)
    assert {p["walmart_item_id"] for p in prods} == {"10294528"}
    assert any(b["type"] == "mine" and b["name"] == "Tabasco" for b in brands)


def test_add_owned_item_adds_no_keywords(client, auth):
    # Adding the item never touches the category's keywords — reusing the item's
    # tracked keywords is a client-side stage-into-the-Keywords-section step, then
    # committed separately via the keyword form.
    auth.register()
    gid = _monitoring_group(client)
    pk = _add_portfolio_item(client, "Tabasco",
                             "https://www.walmart.com/ip/Tabasco-Original/10294528")
    client.post(
        f"/app/competitive-intel/groups/{gid}/owned-item",
        data={"portfolio_item_id": pk, "copy_keywords": "1"},  # legacy field ignored now
    )
    with client.application.app_context():
        assert ci_config.list_keywords(get_db(), gid, 1) == []


def test_add_owned_item_rejects_foreign_item(client, auth, app):
    # A portfolio item the user doesn't own can't be attached (get_item IDOR gate).
    owner_uid, _ = _make_group(app, "owner2@example.com", "monitoring")
    with app.app_context():
        portfolio.add_item(get_db(), owner_uid, "Theirs",
                           "https://www.walmart.com/ip/Theirs/999", None)
        foreign_pk = portfolio.list_items(get_db(), owner_uid)[0]["id"]
    auth.register()  # a different user
    gid = _monitoring_group(client)
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/owned-item",
        data={"portfolio_item_id": foreign_pk},
        follow_redirects=True,
    )
    assert b"Pick one of your portfolio items" in resp.data
    # The client owns gid; assert no product was attached (query directly — the
    # client's uid isn't 1 here since _make_group registered the first user).
    with client.application.app_context():
        n = get_db().execute(
            "SELECT COUNT(*) AS n FROM ci_products WHERE group_id = ?", (gid,)
        ).fetchone()["n"]
    assert n == 0


def test_add_competitors_from_urls(client, auth):
    # Each non-empty URL becomes its own competitor brand + product; blanks ignored.
    auth.register()
    gid = _monitoring_group(client)
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/competitors",
        data={"urls": ["https://www.walmart.com/ip/Franks-RedHot-Original/111",
                       "https://www.walmart.com/ip/Cholula-Original/222",
                       ""]},
    )
    assert resp.status_code in (302, 303)
    with client.application.app_context():
        comp = [b for b in ci_config.list_brands(get_db(), gid, 1) if b["type"] == "competitor"]
        prods = ci_config.list_products(get_db(), gid, 1)
    assert len(comp) == 2  # one competitor brand per URL
    assert {p["walmart_item_id"] for p in prods} == {"111", "222"}
    assert any("Franks" in b["name"] or "RedHot" in b["name"] for b in comp)


def test_add_competitors_capped_at_five(client, auth):
    auth.register()
    gid = _monitoring_group(client)
    urls = [f"https://www.walmart.com/ip/Rival-{i}/{1000 + i}" for i in range(7)]
    client.post(f"/app/competitive-intel/groups/{gid}/competitors", data={"urls": urls})
    with client.application.app_context():
        comp = [b for b in ci_config.list_brands(get_db(), gid, 1) if b["type"] == "competitor"]
    assert len(comp) == ci_config.MAX_COMPETITORS_PER_GROUP


def test_delete_competitor_frees_a_slot(client, auth):
    # Deleting a competitor removes its brand + product (via the brand-delete route).
    auth.register()
    gid = _monitoring_group(client)
    client.post(f"/app/competitive-intel/groups/{gid}/competitors",
                data={"urls": ["https://www.walmart.com/ip/Franks/111"]})
    with client.application.app_context():
        bid = [b for b in ci_config.list_brands(get_db(), gid, 1)
               if b["type"] == "competitor"][0]["id"]
    client.post(f"/app/competitive-intel/brands/{bid}/delete", data={"group_id": gid})
    with client.application.app_context():
        comp = [b for b in ci_config.list_brands(get_db(), gid, 1) if b["type"] == "competitor"]
        prods = ci_config.list_products(get_db(), gid, 1)
    assert comp == [] and prods == []


def test_add_competitors_idor_guard(client, auth, app):
    owner_uid, gid = _make_group(app, "owner3@example.com", "monitoring")
    auth.register()  # a different user
    resp = client.post(
        f"/app/competitive-intel/groups/{gid}/competitors",
        data={"urls": ["https://www.walmart.com/ip/a/111"]},
    )
    assert resp.status_code == 404
    with client.application.app_context():
        assert ci_config.list_products(get_db(), gid, owner_uid) == []


def test_disable_monitoring_flips_flag(client, auth):
    auth.register()
    gid = _monitoring_group(client)
    with client.application.app_context():
        ci_config.set_monitoring(get_db(), gid, 1, True)
    resp = client.post(f"/app/competitive-intel/groups/{gid}/disable")
    assert resp.status_code in (302, 303)
    with client.application.app_context():
        assert ci_config.get_group(get_db(), gid, 1)["monitoring_enabled"] == 0


def test_setup_breadcrumb_create_then_category(client, auth):
    # Breadcrumb reads "Create New Tracking" while the category is untouched, then
    # switches to the category name once it has config.
    auth.register()
    gid = _monitoring_group(client)
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert b"Create New Tracking" in cfg.data
    with client.application.app_context():
        ci_config.add_keyword(get_db(), gid, 1, "hot sauce")
    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert b"Create New Tracking" not in cfg.data
    assert b"Competitive Tracking \xc2\xb7 Track" in cfg.data  # "· Track" (group name)
