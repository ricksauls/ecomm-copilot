"""Smoke tests for the three built pages.

These assert the routes render and return the expected surface, not pixel
fidelity. They exist so CI catches template/route breakage on every push.
"""


def test_landing_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Verbatim hero copy from the design.
    assert b"Your eCommerce CoPilot." in resp.data


def test_static_assets_are_cache_busted(client):
    # static_url() appends a ?v=<mtime> so edited CSS/JS reaches returning users
    # despite the long Expires header on /static.
    resp = client.get("/")
    assert b"css/tokens.css?v=" in resp.data


def test_signin_renders(client):
    resp = client.get("/signin")
    assert resp.status_code == 200
    assert b"Welcome back" in resp.data


def test_dashboard_renders_when_authenticated(client, auth):
    # Registering logs the user in, so the guarded dashboard is reachable. The page
    # is now just the Portfolio header + the performance briefing; everything that
    # used to sit below the cards (KPI row, activity tables, seasonal nudges) is
    # gone while the Opportunities area is rebuilt.
    auth.register()
    resp = client.get("/app")
    assert resp.status_code == 200
    assert b"Portfolio" in resp.data
    assert b"brands" in resp.data and b"products" in resp.data
    # A fresh account has no monitoring group, so the briefing prompts setup.
    assert b"Set up tracking" in resp.data
    # The old under-card content and its enhancement script are removed.
    assert b"Scored This Month" not in resp.data
    assert b"Products managed" not in resp.data
    assert b"Products losing ground" not in resp.data
    assert b"js/dashboard.js" not in resp.data


def test_dashboard_header_drops_agency_name(client, auth):
    # The header is a plain "Portfolio" + counts; the demo agency name and the old
    # per-user email headline are no longer the page title.
    auth.register(email="rick@example.com")
    body = client.get("/app").data
    assert b"Portfolio" in body
    assert b"Meridian Commerce Group" not in body


def test_set_default_brand_persists_and_loads_and_guards_ownership(client, auth, app):
    # Saving a default brand persists it and makes the dashboard load that brand
    # when no ?group_id= is given; a group the user doesn't own is rejected.
    from app import ci_config
    from app.db import get_db
    from app.users import create_local_user

    auth.register(email="def@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?",
                         ("def@example.com",)).fetchone()[0]
        g1 = ci_config.create_group(db, uid, "Brand A", mode="monitoring")
        g2 = ci_config.create_group(db, uid, "Brand B", mode="monitoring")
        ci_config.add_brand(db, g1, uid, "Alpha", "mine")
        ci_config.add_brand(db, g2, uid, "Beta", "mine")
        other_uid = create_local_user("other@example.com", "password123")
        og = ci_config.create_group(db, other_uid, "Theirs", mode="monitoring")
        db.commit()

    resp = client.post("/app/dashboard/default-brand", data={"group_id": g1})
    assert resp.status_code in (302, 303)
    with app.app_context():
        db = get_db()
        saved = db.execute("SELECT default_ci_group_id FROM users WHERE id = ?",
                           (uid,)).fetchone()[0]
        assert saved == g1

    # With no group_id, the dashboard now loads the saved default (Brand A), not the
    # newest group (Brand B) — assert g1 is the selected option, since both brand
    # labels appear in the selector regardless.
    body = client.get("/app").data
    assert f'value="{g1}" selected'.encode() in body

    # A forged id for another user's group is rejected, not stored (IDOR gate).
    assert client.post("/app/dashboard/default-brand", data={"group_id": og}).status_code == 404


def test_pdp_scoring_page_renders(client, auth):
    # The guarded intake page renders with its heading, and no longer shows the
    # removed "Content Scoring" eyebrow or the topbar search / client-view UI.
    auth.register()
    resp = client.get("/app/pdp-scoring")
    assert resp.status_code == 200
    assert b"Score Product Detail Page(s)" in resp.data
    # The eyebrow line is gone (the rail nav item keeps its own label).
    assert b"(Product Detail Page) Content Scoring" not in resp.data
    assert b"Search products, brands" not in resp.data
    assert b"Client view" not in resp.data
    # "What we score" band reflects the paused dimensions: 4 cards, no Attributes
    # card, no video mention.
    assert resp.data.count(b'class="dim-name"') == 4
    assert b"four dimensions" in resp.data
    assert b">Attributes<" not in resp.data
    assert b"video" not in resp.data
    # Infographic and lifestyle scoring are off, so they're not advertised; the
    # length signals are.
    assert b"infographic" not in resp.data
    assert b"lifestyle" not in resp.data
    # "&" is HTML-escaped to "&amp;" in the rendered output.
    assert b"Character &amp; word count" in resp.data
    assert b"Word count, depth" in resp.data


def test_results_page_shows_product_title(client, auth, app):
    # End-to-end through the real route (not just the template): enqueue an item,
    # score it with a title, and confirm the results page renders that title.
    # Guards against the route's row-view dropping the title column.
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        row = db.execute("SELECT id FROM scored_items ORDER BY id DESC LIMIT 1").fetchone()
        jobs.save_result(db, row["id"], 80, {"overall": 80, "dimensions": []},
                         "Acme Widget Deluxe, 3-Pack")
    resp = client.get("/app/pdp-scoring/results")
    assert resp.status_code == 200
    assert b"Acme Widget Deluxe, 3-Pack" in resp.data
    # A scored batch offers the PDF download.
    assert b"Download PDF" in resp.data


def test_results_pdf_download(client, auth, app):
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        row = db.execute("SELECT id FROM scored_items ORDER BY id DESC LIMIT 1").fetchone()
        jobs.save_result(db, row["id"], 80, {"overall": 80, "dimensions": [
            {"key": "title", "label": "Title", "score": 80, "weight": 18,
             "available": True, "findings": ["ok"], "recommendations": ["do x"]},
        ]}, "Acme Widget")
    resp = client.get("/app/pdp-scoring/results.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:5] == b"%PDF-"  # real PDF, not an error page
    assert "attachment" in resp.headers.get("Content-Disposition", "")


def test_results_shows_progress_while_pending(client, auth):
    # A freshly enqueued (unscored) batch is pending: the subtitle shows the
    # in-progress text and a progress bar (0 of 1 so far), and no longer uses the
    # flashing class (that cue moved to the progress bar on this screen).
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    resp = client.get("/app/pdp-scoring/results")
    assert b"scoring in progress" in resp.data
    assert b'role="progressbar"' in resp.data
    assert b"0 of 1 scored" in resp.data
    assert b"less than a minute left" in resp.data  # ETA for one remaining item
    assert b"subtitle-scoring" not in resp.data  # no text flashing on this screen


def test_eta_label_thresholds():
    from app.routes.pages import _eta_label

    assert _eta_label(0) is None            # nothing left -> no estimate
    assert _eta_label(1) == "less than a minute left"   # 12s
    assert _eta_label(4) == "less than a minute left"   # 48s
    assert _eta_label(5) == "about 1 min left"          # 60s
    assert _eta_label(25) == "about 5 min left"         # 300s


def test_results_pdf_requires_login(client):
    resp = client.get("/app/pdp-scoring/results.pdf")
    assert resp.status_code == 302
    assert "/signin" in resp.headers["Location"]


def test_unknown_route_404(client):
    resp = client.get("/does-not-exist")
    assert resp.status_code == 404
    assert b"This page isn't here." in resp.data


def test_security_headers_present(client):
    resp = client.get("/")
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert "Content-Security-Policy" in resp.headers


def test_view_all_shows_all_time_records(client, auth, app):
    # The dashboard table is month-scoped, but each View All screen shows every
    # record for that activity — including rows from earlier months.
    from app import jobs
    from app.db import get_db

    auth.register(email="va@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("va@example.com",)).fetchone()["id"]
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/555", "item": "555", "brand": "Acme"},
        ])
        jobs.save_result(db, ids[0], 77, {"overall": 77}, "Old Scored Product")
        # Backdate to a prior month so "this month" would exclude it.
        db.execute("UPDATE scored_items SET created_at = '2020-01-05 00:00:00' WHERE id = ?",
                   (ids[0],))
        db.commit()

    # View All: all-time, so the old (prior-month) row appears.
    resp = client.get("/app/activity/scored")
    assert resp.status_code == 200
    assert b"Old Scored Product" in resp.data
    assert b"Brands/Products Scored" in resp.data


def test_scoring_history_groups_by_run(client, auth, app):
    # View Scoring History rolls items up by scoring run: a run of five items is
    # ONE row showing up to three brands/products plus "And 2 more…", retitled
    # "Brands/Products Scored", and still linking back to reopen the run.
    from app import jobs
    from app.db import get_db

    auth.register(email="runs@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("runs@example.com",)).fetchone()["id"]
        # One enqueue_items call == one batch == one run.
        ids = jobs.enqueue_items(db, uid, [
            {"url": f"https://www.walmart.com/ip/{n}", "item": str(n), "brand": f"Brand{n}"}
            for n in range(1, 6)
        ])
        for i, sid in enumerate(ids, start=1):
            jobs.save_result(db, sid, 80 + i, {"overall": 80 + i}, f"Product {i}")
        db.commit()

    body = client.get("/app/activity/scored").data
    assert b"Brands/Products Scored" in body          # retitled heading + table
    assert b"And 2 more" in body                      # 5 items - 3 shown = 2 more
    # Five items collapse into a single run row (one row link).
    assert body.count(b"dash-row-runs") == 1
    # The three most-recent items in the run show; the other two are hidden.
    assert b"Product 5" in body and b"Product 3" in body
    assert b"Product 1" not in body and b"Product 2" not in body


def test_history_search_bar_and_blob_cover_hidden_run_items(client, auth, app):
    # Every history screen carries a search box, and a grouped scoring run's
    # data-search blob covers ALL items in the run (incl. the two not shown on the
    # row) and their item numbers — so a search finds a run by any of its items.
    from app import jobs
    from app.db import get_db

    auth.register(email="search@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("search@example.com",)).fetchone()["id"]
        ids = jobs.enqueue_items(db, uid, [
            {"url": f"https://www.walmart.com/ip/{n}", "item": str(n), "brand": f"Brand{n}"}
            for n in range(1, 6)
        ])
        for i, sid in enumerate(ids, start=1):
            jobs.save_result(db, sid, 80 + i, {"overall": 80 + i}, f"Product {i}")
        db.commit()

    body = client.get("/app/activity/scored").data
    assert b'class="dash-search"' in body                       # the filter box renders
    assert b"data-search=" in body
    # "Product 1" / "Product 2" are hidden from the row display, but their lowercased
    # text and item numbers live in the searchable blob so the run is findable by them.
    assert b"Product 1" not in body and b"product 1" in body
    assert b"brand2" in body                                    # brand of a hidden item


def test_view_all_unknown_kind_404s(client, auth):
    auth.register(email="va2@example.com")
    assert client.get("/app/activity/bogus").status_code == 404


def test_view_all_requires_login(client):
    # Guarded like the rest of the workspace — anonymous is redirected to sign-in.
    resp = client.get("/app/activity/scored")
    assert resp.status_code in (301, 302)


def test_activity_rows_link_to_results(client, auth, app):
    # View All rows carry a per-item results link, and that route opens the item's
    # results (ownership-checked).
    from app import jobs
    from app.db import get_db

    auth.register(email="rowlink@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("rowlink@example.com",)).fetchone()["id"]
        ids = jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/321", "item": "321"}])
        jobs.save_result(db, ids[0], 88, {"overall": 88, "dimensions": []}, "Linked Product")
        db.commit()
        sid = ids[0]

    # The View All screen renders the row as a link to the per-item results route.
    body = client.get("/app/activity/scored").data
    assert f"/app/pdp-scoring/item/{sid}".encode() in body

    # Following it lands on the scoring results page for that item.
    resp = client.get(f"/app/pdp-scoring/item/{sid}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Linked Product" in resp.data


def test_activity_item_route_is_ownership_scoped(client, auth, app):
    # Another user's scored item id must not be viewable.
    from app import jobs
    from app.db import get_db

    auth.register(email="owner-a@example.com")
    with app.app_context():
        db = get_db()
        owner = db.execute("SELECT id FROM users WHERE email = ?", ("owner-a@example.com",)).fetchone()["id"]
        ids = jobs.enqueue_items(db, owner, [{"url": "https://www.walmart.com/ip/1", "item": "1"}])
        jobs.save_result(db, ids[0], 50, {"overall": 50}, "Private")
        db.commit()
        foreign_sid = ids[0]

    # Sign in as a different user; the first user's item id 404s.
    auth.logout()
    auth.register(email="intruder-b@example.com")
    assert client.get(f"/app/pdp-scoring/item/{foreign_sid}").status_code == 404


def test_view_copy_scopes_to_selection_and_lists_missing(client, auth, app):
    # "View copy results" scoped to the ticked items shows copy for those that
    # have it and lists the rest with a "copy not created" note.
    from app import jobs, copy_jobs
    from app.db import get_db
    from app.routes.pages import _BATCH_KEY

    auth.register(email="vc@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("vc@example.com",)).fetchone()["id"]
        sids = jobs.enqueue_items(db, uid, [
            {"url": "https://w/ip/10", "item": "10", "brand": "Acme"},
            {"url": "https://w/ip/11", "item": "11", "brand": "Globex"},
        ])
        jobs.save_result(db, sids[0], 80, {"overall": 80}, "Has Copy Product")
        jobs.save_result(db, sids[1], 70, {"overall": 70}, "No Copy Product")
        # Copy exists only for the first item.
        copy_jobs.enqueue_copy_items(db, uid, [{"url": "https://w/ip/10", "item": "10"}])
        db.commit()

    with client.session_transaction() as sess:
        sess[_BATCH_KEY] = sids

    resp = client.get(
        f"/app/pdp-scoring/view-copy?item_ids={sids[0]}&item_ids={sids[1]}",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data
    assert b"Copy has not been created for this item." in body  # the missing note
    assert b"No Copy Product" in body                           # the uncovered item is listed


def test_row_click_opens_whole_run(client, auth, app):
    # Clicking one item's row opens the whole run it was submitted with — all items
    # scored together (sharing a batch_id) appear, not just the clicked one.
    from app import jobs
    from app.db import get_db

    auth.register(email="run@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("run@example.com",)).fetchone()["id"]
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/111", "item": "111"},
            {"url": "https://www.walmart.com/ip/222", "item": "222"},
            {"url": "https://www.walmart.com/ip/333", "item": "333"},
        ])
        jobs.save_result(db, ids[0], 70, {"overall": 70}, "Run Item A")
        jobs.save_result(db, ids[1], 80, {"overall": 80}, "Run Item B")
        jobs.save_result(db, ids[2], 90, {"overall": 90}, "Run Item C")
        # A separate, later run — must NOT bleed into the first run's results.
        other = jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/999", "item": "999"}])
        jobs.save_result(db, other[0], 60, {"overall": 60}, "Other Run Item")
        db.commit()
        clicked = ids[1]

    # Clicking the middle item of the run opens all three run items, not the other run.
    resp = client.get(f"/app/pdp-scoring/item/{clicked}", follow_redirects=True)
    assert resp.status_code == 200
    assert b"Run Item A" in resp.data
    assert b"Run Item B" in resp.data
    assert b"Run Item C" in resp.data
    assert b"Other Run Item" not in resp.data


def test_content_activity_route_removed(client, auth):
    # The combined "View All Content Activity" roll-up was retired in the nav
    # redesign (history now lives inside each tool's workspace); the route 404s.
    auth.register(email="ca@example.com")
    assert client.get("/app/content-activity").status_code == 404


def test_rail_routes_workspaces_not_separate_histories(client, auth):
    # After the redesign the rail routes to each workspace by its feature name and
    # carries NO separate "View … History" items — history is a tab inside the
    # tool (see test_workspace_has_create_and_history_tabs). CI is unchanged.
    auth.register()
    rail = client.get("/app").data

    # Workspaces are present under their shortened feature labels. Content Scoring
    # and Product Copy are single-workspace leaves (the copy tool is reached
    # straight off the "Product Copy" leaf, no sub-menu); Creative Studio keeps
    # its three children.
    assert b"Content Scoring" in rail
    assert b"Product Copy" in rail
    assert b"Creative Studio" in rail
    for label in (b"Product Image Import",
                  b"Product Image Set Creator", b"Seasonal Campaigns"):
        assert label in rail

    # The old action-worded labels and the per-category history twins are gone.
    for gone in (b"View Scoring History", b"View Copy Content Creation History",
                 b"View Creative Content Creation History",
                 b"View Acquired Images History", b"View Seasonal Campaign History",
                 b"Product Content Scoring", b"Copy Content Studio",
                 b"Creative Content Studio"):
        assert gone not in rail


def test_workspace_has_create_and_history_tabs(client, auth):
    # Each content tool renders the Create New / History workspace tab bar, with
    # the History tab linking to that tool's activity view.
    auth.register()
    page = client.get("/app/pdp-scoring").data
    assert b"ws-tabs" in page
    assert b"Create New" in page and b"History" in page
    assert b"/app/activity/scored" in page  # the History tab's href


# The History tab is named after what each workspace produces, on both the
# Create-New view and the History view. Keep the two in sync (a mismatch would
# show two different names for the same tab as the user switches).
_WORKSPACE_HISTORY_LABELS = {
    "/app/pdp-scoring": b"History - Content Scored",
    "/app/pdp-copy": b"History - Copy Created",
    "/app/import-images": b"History - Images Acquired",
    "/app/pdp-image-set": b"History - Image Sets Created",
    "/app/seasonal": b"History - Seasonal Campaigns Created",
}
_ACTIVITY_HISTORY_LABELS = {
    "/app/activity/scored": b"History - Content Scored",
    "/app/activity/copy": b"History - Copy Created",
    "/app/activity/acquired": b"History - Images Acquired",
    "/app/activity/images": b"History - Image Sets Created",
}


def test_create_views_render_named_history_labels(client, auth):
    auth.register()
    for url, label in _WORKSPACE_HISTORY_LABELS.items():
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert label in resp.data, url


def test_history_views_render_named_history_labels(client, auth):
    auth.register()
    for url, label in _ACTIVITY_HISTORY_LABELS.items():
        resp = client.get(url)
        assert resp.status_code == 200, url
        assert label in resp.data, url


def test_ci_activity_shows_both_ci_tables_all_time(client, auth, app):
    # View All CI Activity: the two CI tables, all-time, with the new nav item, and
    # the old View Snapshot / View Monitoring nav items removed from the rail.
    from app import ci_config, ci_jobs
    from app.db import get_db

    auth.register(email="cia@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("cia@example.com",)).fetchone()["id"]
        snap = ci_config.create_group(db, uid, "Snap Group", mode="snapshot")
        ci_config.create_group(db, uid, "Mon Group", mode="monitoring")
        ci_jobs.enqueue_run(db, snap, "one_time")  # gives the snapshot table a row

    resp = client.get("/app/competitive-intel/activity")
    assert resp.status_code == 200
    assert b"View All Competitive Intelligence Activity" in resp.data
    assert b"One-Time Snapshots" in resp.data
    assert b"Daily Monitoring" in resp.data
    assert b"Snap Group" in resp.data  # the snapshot run's group row
    # Snapshot rows link to that group's results.
    assert f"/app/competitive-intel/groups/{snap}/results".encode() in resp.data

    # Rail (as of session 31): the Insights area was reworked, so the old
    # View-Snapshot/Monitoring leaves AND this View-All-CI-Activity leaf are all
    # hidden from the rail. The activity page above still renders directly.
    dash = client.get("/app").data
    assert b"/app/competitive-intel/activity" not in dash
    assert b">View Snapshot<" not in dash
    assert b">View Monitoring<" not in dash


def test_ci_activity_requires_login(client):
    resp = client.get("/app/competitive-intel/activity")
    assert resp.status_code in (301, 302)


# --- AI image enhancement (config-gated) ------------------------------------

_IMAGERY_WITH_ISSUE = {
    "overall": 72,
    "dimensions": [{
        "key": "imagery", "label": "Imagery", "score": 72, "available": True,
        "recommendations": [],
        "image_issues": [{"index": 2, "url": "https://i5.walmartimages.com/seo/x.jpg",
                          "px": 1200, "severity": "mid"}],
    }],
}


def _seed_scored_with_issue(client, auth, app):
    """Register, enqueue one item (into the session batch), score it with a
    flagged image, and return its scored_items id."""
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        row = db.execute("SELECT id FROM scored_items ORDER BY id DESC LIMIT 1").fetchone()
        jobs.save_result(db, row["id"], 72, _IMAGERY_WITH_ISSUE, "Prod")
        return row["id"]


def _seed_cached_enhanced(app, sid, slot, data=b"ENHANCED", ext="jpg"):
    """Simulate the worker having produced a fix: write the slot's cache file."""
    with app.app_context():
        from app import ci_images
        ci_images.save_enhanced_image(sid, slot, data, ext)


def _seed_other_users_item(app, result, email):
    """Create a second user with one scored item carrying ``result``; return its id.

    Used by the IDOR tests: the logged-in user must not reach another user's item.
    """
    with app.app_context():
        from app import jobs
        from app.db import get_db
        from app.users import create_local_user
        db = get_db()
        other = create_local_user(email, "password123")
        ids = jobs.enqueue_items(db, other, [{"url": "https://www.walmart.com/ip/9", "item": "9"}])
        jobs.save_result(db, ids[0], result.get("overall", 70), result, "Other")
        return ids[0]


def test_enhance_button_hidden_when_not_configured(client, auth, app):
    _seed_scored_with_issue(client, auth, app)
    resp = client.get("/app/pdp-scoring/results")
    assert b"Image 2" in resp.data              # the flag is shown
    assert b"Enhance to 2000px" not in resp.data  # but not the upscale action


def test_enhance_button_shown_when_configured(client, auth, app, monkeypatch):
    _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    resp = client.get("/app/pdp-scoring/results")
    assert b"Enhance to 2000px" in resp.data
    # The action is now a POST form targeting the enqueue route.
    assert b'method="post"' in resp.data


def test_enhance_enqueue_is_post_only(client, auth, app, monkeypatch):
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    # The enqueue action moved from GET-download to POST; a GET is not allowed.
    assert client.get(f"/app/pdp-scoring/enhance/{sid}/2").status_code == 405


def test_enhance_enqueue_not_configured_returns_503(client, auth, app, monkeypatch):
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: False)
    assert client.post(f"/app/pdp-scoring/enhance/{sid}/2").status_code == 503


def test_enhance_enqueue_creates_job(client, auth, app, monkeypatch):
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    resp = client.post(f"/app/pdp-scoring/enhance/{sid}/2")
    assert resp.status_code == 302  # redirects back to results
    with app.app_context():
        from app.db import get_db
        row = get_db().execute(
            "SELECT * FROM image_jobs WHERE scored_item_id = ? AND slot = 'img2'", (sid,)
        ).fetchone()
        assert row["status"] == "queued"
        assert row["operation"] == "upscale"
        # The provider gets the URL from our stored scrape, never request input.
        assert row["source_url"] == "https://i5.walmartimages.com/seo/x.jpg"


def test_enhance_enqueue_is_idempotent(client, auth, app, monkeypatch):
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    client.post(f"/app/pdp-scoring/enhance/{sid}/2")
    client.post(f"/app/pdp-scoring/enhance/{sid}/2")  # double click
    with app.app_context():
        from app.db import get_db
        n = get_db().execute(
            "SELECT COUNT(*) FROM image_jobs WHERE scored_item_id = ? AND slot = 'img2'",
            (sid,),
        ).fetchone()[0]
        assert n == 1


def test_enhance_enqueue_rejects_other_users_item(client, auth, app, monkeypatch):
    _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    other_sid = _seed_other_users_item(app, _IMAGERY_WITH_ISSUE, "other@example.com")
    assert client.post(f"/app/pdp-scoring/enhance/{other_sid}/2").status_code == 404


def test_enhanced_serve_inline_and_download(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    _seed_cached_enhanced(app, sid, "img2", b"UPSCALED")

    inline = client.get(f"/app/pdp-scoring/enhanced/{sid}/img2")
    assert inline.status_code == 200
    assert inline.data == b"UPSCALED"
    assert "attachment" not in inline.headers.get("Content-Disposition", "")

    dl = client.get(f"/app/pdp-scoring/enhanced/{sid}/img2/download")
    assert dl.status_code == 200
    assert dl.data == b"UPSCALED"
    assert "attachment" in dl.headers.get("Content-Disposition", "")


def test_enhanced_serve_404_before_ready(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert client.get(f"/app/pdp-scoring/enhanced/{sid}/img2").status_code == 404


def test_enhanced_serve_rejects_bad_slot(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    # A slot outside the img{N}/whitebg allowlist is rejected before any FS access.
    assert client.get(f"/app/pdp-scoring/enhanced/{sid}/evil").status_code == 404


def test_enhanced_serve_rejects_other_users_item(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    other_sid = _seed_other_users_item(app, _IMAGERY_WITH_ISSUE, "other@example.com")
    _seed_cached_enhanced(app, other_sid, "img2", b"UPSCALED")
    assert client.get(f"/app/pdp-scoring/enhanced/{other_sid}/img2").status_code == 404


def test_results_shows_enhancing_while_queued(client, auth, app, monkeypatch):
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    client.post(f"/app/pdp-scoring/enhance/{sid}/2")  # enqueue (not yet processed)
    data = client.get("/app/pdp-scoring/results").data
    assert b"Enhancing" in data
    # An in-flight fix keeps the auto-refresh alive so the result appears live.
    assert b'http-equiv="refresh"' in data


def test_results_shows_download_link_when_done(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_with_issue(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    _seed_cached_enhanced(app, sid, "img2", b"UPSCALED")
    data = client.get("/app/pdp-scoring/results").data
    # The finished fix offers a download link (+ a ZIP of what's ready). The inline
    # thumbnail was dropped to save space, so there's no <img> preview of the result.
    assert f"/app/pdp-scoring/enhanced/{sid}/img2/download".encode() in data
    assert b"Fix and Download" in data
    assert b"Enhanced image preview" not in data  # the thumbnail <img> is gone
    assert b"Fix And Download All As Zip" in data


# --- Fix-all + ZIP (Phase 2) -------------------------------------------------

_IMAGERY_MANY = {"overall": 66, "dimensions": [{
    "key": "imagery", "label": "Imagery", "score": 66, "available": True,
    "recommendations": [],
    "image_issues": [
        {"index": 1, "url": "https://i5/1.jpg", "px": 1024, "severity": "low"},
        {"index": 2, "url": "https://i5/2.jpg", "px": 1266, "severity": "mid"},
    ],
    "white_bg_url": "https://i5/main.jpg",
}]}


def _seed_scored_many(client, auth, app):
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        row = db.execute("SELECT id FROM scored_items ORDER BY id DESC LIMIT 1").fetchone()
        jobs.save_result(db, row["id"], 66, _IMAGERY_MANY, "Prod")
        return row["id"]


def test_fix_all_enqueues_every_slot(client, auth, app, monkeypatch):
    sid = _seed_scored_many(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert client.post(f"/app/pdp-scoring/enhance-all/{sid}").status_code == 302
    with app.app_context():
        from app.db import get_db
        slots = {r["slot"] for r in get_db().execute(
            "SELECT slot FROM image_jobs WHERE scored_item_id = ?", (sid,)
        ).fetchall()}
        # Image 1 is covered by the combined whitebg fix, so it isn't enqueued alone.
        assert slots == {"img2", "whitebg"}


def test_fix_all_skips_already_cached(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_many(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    _seed_cached_enhanced(app, sid, "img2", b"DONE")  # already fixed
    client.post(f"/app/pdp-scoring/enhance-all/{sid}")
    with app.app_context():
        from app.db import get_db
        slots = {r["slot"] for r in get_db().execute(
            "SELECT slot FROM image_jobs WHERE scored_item_id = ?", (sid,)
        ).fetchall()}
        assert slots == {"whitebg"}  # the cached img2 wasn't re-queued


def test_enhance_zip_bundles_finished_fixes(client, auth, app, monkeypatch, tmp_path):
    import io
    import zipfile

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_many(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    _seed_cached_enhanced(app, sid, "img2", b"IMG2")
    _seed_cached_enhanced(app, sid, "whitebg", b"MAIN")
    resp = client.get(f"/app/pdp-scoring/enhance-all/{sid}/download.zip")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    names = set(zipfile.ZipFile(io.BytesIO(resp.data)).namelist())
    assert names == {"image-2-2000px.jpg", "main-image-fixed.jpg"}


def test_enhance_zip_404_when_nothing_ready(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sid = _seed_scored_many(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert client.get(f"/app/pdp-scoring/enhance-all/{sid}/download.zip").status_code == 404


# --- White-background fix (config-gated) -------------------------------------

_IMAGERY_WHITE_BG = {
    "overall": 70,
    "dimensions": [{
        "key": "imagery", "label": "Imagery", "score": 70, "available": True,
        "recommendations": ["Set the main image to the product on a pure white background "
                            "(a Walmart main-image requirement)"],
        "image_issues": [],
        "white_bg_url": "https://i5.walmartimages.com/seo/main.jpg",
    }],
}


def _seed_scored_white_bg(client, auth, app):
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        row = db.execute("SELECT id FROM scored_items ORDER BY id DESC LIMIT 1").fetchone()
        jobs.save_result(db, row["id"], 70, _IMAGERY_WHITE_BG, "Prod")
        return row["id"]


def test_whitebg_route_requires_login(client):
    # POST-only enqueue route; an unauthenticated POST redirects to sign-in.
    assert client.post("/app/pdp-scoring/whitebg/1").status_code == 302


def test_whitebg_button_hidden_when_not_configured(client, auth, app):
    _seed_scored_white_bg(client, auth, app)
    assert b"Fix &amp; enhance main image" not in client.get("/app/pdp-scoring/results").data


def test_whitebg_button_shown_when_configured(client, auth, app, monkeypatch):
    _seed_scored_white_bg(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert b"Fix &amp; enhance main image" in client.get("/app/pdp-scoring/results").data


def test_whitebg_enqueue_creates_job(client, auth, app, monkeypatch):
    sid = _seed_scored_white_bg(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert client.post(f"/app/pdp-scoring/whitebg/{sid}").status_code == 302
    with app.app_context():
        from app.db import get_db
        row = get_db().execute(
            "SELECT * FROM image_jobs WHERE scored_item_id = ? AND slot = 'whitebg'", (sid,)
        ).fetchone()
        assert row["operation"] == "white_bg"
        assert row["source_url"] == "https://i5.walmartimages.com/seo/main.jpg"


def test_whitebg_enqueue_rejects_other_users_item(client, auth, app, monkeypatch):
    _seed_scored_white_bg(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    other_sid = _seed_other_users_item(app, _IMAGERY_WHITE_BG, "other2@example.com")
    assert client.post(f"/app/pdp-scoring/whitebg/{other_sid}").status_code == 404


def test_main_image_fix_consolidates_enhance_and_white_bg(client, auth, app, monkeypatch):
    # When the main image (index 1) needs both resolution and white-bg, its separate
    # "Enhance" action is suppressed and the single combined action is offered;
    # gallery images keep their own enhance action.
    sid = _seed_scored_many(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    data = client.get("/app/pdp-scoring/results").data
    # Main image (index 1): no standalone enhance form; combined action instead.
    assert f'action="/app/pdp-scoring/enhance/{sid}/1"'.encode() not in data
    assert b"Fix &amp; enhance main image" in data
    assert f'action="/app/pdp-scoring/whitebg/{sid}"'.encode() in data
    # Both issues (resolution + white bg) are noted on the main image's own line,
    # not split into a separate paragraph below the list.
    assert b"not on a pure white background" in data
    assert b'class="pdp-whitebg-fix"' not in data  # the standalone paragraph isn't used here
    # Gallery image (index 2): keeps its own enhance form.
    assert f'action="/app/pdp-scoring/enhance/{sid}/2"'.encode() in data


def test_whitebg_only_uses_standalone_line(client, auth, app, monkeypatch):
    # When the main image is the right resolution but NOT on white (so it isn't in
    # the flagged-resolution list), the combined fix gets its own labelled line.
    sid = _seed_scored_white_bg(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    data = client.get("/app/pdp-scoring/results").data
    assert b'class="pdp-whitebg-fix"' in data
    assert b"Main image" in data
    assert f'action="/app/pdp-scoring/whitebg/{sid}"'.encode() in data


# --- Batch image fixing (whole-batch + selected) + cost preflight ------------

def _seed_batch_two(client, auth, app):
    """Register and score a two-item batch into the session.

    Item A (``_IMAGERY_MANY``): one gallery upscale (img2) + the combined
    main-image white-bg fix (whitebg) — img1 is covered by whitebg, not enqueued
    alone. Item B (``_IMAGERY_WITH_ISSUE``): one gallery upscale (img2). So the
    batch offers 3 fixes total — 2 upscales + 1 white-bg — across 2 items.
    """
    auth.register()
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        uid = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        sids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/111", "item": "111"},
            {"url": "https://www.walmart.com/ip/222", "item": "222"},
        ])
        jobs.save_result(db, sids[0], 66, _IMAGERY_MANY, "ProdA")
        jobs.save_result(db, sids[1], 72, _IMAGERY_WITH_ISSUE, "ProdB")
    # Point the session's "current batch" at these two items (the route normally
    # does this at intake; we seed directly so the test controls the batch).
    with client.session_transaction() as sess:
        sess["pdp_batch_ids"] = sids
    return sids


def test_batch_bar_hidden_when_not_configured(client, auth, app):
    _seed_batch_two(client, auth, app)
    assert b"Fix Images For All Items" not in client.get("/app/pdp-scoring/results").data


def test_batch_bar_shown_when_configured(client, auth, app, monkeypatch):
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    data = client.get("/app/pdp-scoring/results").data
    assert b"Fix Images For All Items" in data
    assert b"Fix Images For Selected" in data


def test_batch_estimate_counts_whole_batch(client, auth, app, monkeypatch):
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    monkeypatch.setenv("IMAGE_UPSCALE_PRICE_PER_IMAGE", "0.04")
    est = client.post("/app/pdp-scoring/enhance-batch/estimate", data={"all": "1"}).get_json()
    assert est["total"] == 3
    assert est["whitebg"] == 1
    assert est["upscale"] == 2
    assert est["items"] == 2
    assert est["already_fixed"] == 0
    assert est["price_per_image"] == 0.04
    assert est["est_cost"] == 0.12  # 3 × $0.04


def test_batch_estimate_selected_subset_only(client, auth, app, monkeypatch):
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    # Only item B (one upscale) is ticked.
    est = client.post(
        "/app/pdp-scoring/enhance-batch/estimate", data={"item_ids": str(sids[1])}
    ).get_json()
    assert est["total"] == 1
    assert est["upscale"] == 1
    assert est["whitebg"] == 0
    assert est["items"] == 1


def test_batch_estimate_skips_already_fixed(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    _seed_cached_enhanced(app, sids[0], "img2", b"DONE")  # A's upscale already done
    est = client.post("/app/pdp-scoring/enhance-batch/estimate", data={"all": "1"}).get_json()
    assert est["total"] == 2          # A whitebg + B img2
    assert est["already_fixed"] == 1  # A img2 skipped


def test_batch_estimate_skips_in_flight(client, auth, app, monkeypatch):
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    client.post(f"/app/pdp-scoring/enhance/{sids[1]}/2")  # B img2 already queued
    est = client.post("/app/pdp-scoring/enhance-batch/estimate", data={"all": "1"}).get_json()
    # The queued slot isn't a *new* charge, so it's excluded from the estimate.
    assert est["total"] == 2  # A img2 + A whitebg


def test_batch_enqueue_all_sets_priority(client, auth, app, monkeypatch):
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert client.post("/app/pdp-scoring/enhance-batch", data={"all": "1"}).status_code == 302
    with app.app_context():
        from app.db import get_db
        rows = get_db().execute(
            "SELECT slot, operation, priority FROM image_jobs"
        ).fetchall()
        assert len(rows) == 3
        by_op = {r["operation"]: r["priority"] for r in rows}
        # White-bg (the hard Walmart gate) drains ahead of gallery upscales.
        assert by_op["white_bg"] == 10
        assert by_op["upscale"] == 0


def test_batch_enqueue_selected_only(client, auth, app, monkeypatch):
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    client.post("/app/pdp-scoring/enhance-batch", data={"item_ids": str(sids[1])})
    with app.app_context():
        from app.db import get_db
        rows = get_db().execute(
            "SELECT scored_item_id FROM image_jobs"
        ).fetchall()
        assert {r["scored_item_id"] for r in rows} == {sids[1]}


def test_batch_enqueue_not_configured_returns_503(client, auth, app, monkeypatch):
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: False)
    assert client.post("/app/pdp-scoring/enhance-batch", data={"all": "1"}).status_code == 503


def test_batch_enqueue_nothing_selected_is_noop(client, auth, app, monkeypatch):
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    resp = client.post("/app/pdp-scoring/enhance-batch", data={})  # neither all nor ids
    assert resp.status_code == 302
    with app.app_context():
        from app.db import get_db
        assert get_db().execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0] == 0


def test_batch_enqueue_ignores_foreign_ids(client, auth, app, monkeypatch):
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    other_sid = _seed_other_users_item(app, _IMAGERY_WITH_ISSUE, "stranger@example.com")
    client.post("/app/pdp-scoring/enhance-batch", data={"item_ids": str(other_sid)})
    with app.app_context():
        from app.db import get_db
        # A foreign id resolves to no owned rows → nothing enqueued (IDOR guard).
        assert get_db().execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0] == 0


def test_batch_retry_requeues_failed(client, auth, app, monkeypatch):
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    client.post(f"/app/pdp-scoring/enhance/{sids[1]}/2")  # queue B img2
    with app.app_context():
        from app import image_jobs
        from app.db import get_db
        db = get_db()
        job = db.execute("SELECT id FROM image_jobs").fetchone()
        image_jobs.mark_image_failed(db, job["id"], "boom")
    client.post("/app/pdp-scoring/enhance-batch/retry-failed", data={"all": "1"})
    with app.app_context():
        from app.db import get_db
        assert get_db().execute("SELECT status FROM image_jobs").fetchone()["status"] == "queued"


def test_batch_cancel_drops_queued(client, auth, app, monkeypatch):
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    client.post(f"/app/pdp-scoring/enhance/{sids[1]}/2")  # queue B img2
    client.post("/app/pdp-scoring/enhance-batch/cancel", data={"all": "1"})
    with app.app_context():
        from app.db import get_db
        assert get_db().execute("SELECT COUNT(*) FROM image_jobs").fetchone()[0] == 0


def test_batch_zip_by_item_with_manifest(client, auth, app, monkeypatch, tmp_path):
    import io
    import zipfile

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    sids = _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    _seed_cached_enhanced(app, sids[0], "img2", b"A2")
    _seed_cached_enhanced(app, sids[0], "whitebg", b"AMAIN")
    _seed_cached_enhanced(app, sids[1], "img2", b"B2")
    resp = client.get("/app/pdp-scoring/enhance-batch/download.zip")
    assert resp.status_code == 200
    assert resp.mimetype == "application/zip"
    names = set(zipfile.ZipFile(io.BytesIO(resp.data)).namelist())
    # One folder per item (by Walmart item number), files named by position.
    assert "item-111/image-2-2000px.jpg" in names
    assert "item-111/main-image-fixed.jpg" in names
    assert "item-222/image-2-2000px.jpg" in names
    assert "manifest.csv" in names
    manifest = zipfile.ZipFile(io.BytesIO(resp.data)).read("manifest.csv").decode()
    assert "Item ID,Product Name,Original Image URL,Fixed File" in manifest
    assert "item-111/main-image-fixed.jpg" in manifest


def test_batch_zip_404_when_nothing_ready(client, auth, app, monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    _seed_batch_two(client, auth, app)
    monkeypatch.setattr("app.image_enhance.is_configured", lambda: True)
    assert client.get("/app/pdp-scoring/enhance-batch/download.zip").status_code == 404


# --- Batch copy rewrites (scoring-page bulk action + cost preflight) ----------

_COPY_RECORD = {"title": "Acme Hot Sauce", "bullets": ["a", "b"],
                "description": "desc", "target_keywords": ["hot sauce"]}


def _seed_batch_for_copy(client, auth, app):
    """Two scored items carrying record_json (so a rewrite reuses the scored content)."""
    auth.register()
    with app.app_context():
        from app import jobs
        from app.db import get_db
        db = get_db()
        uid = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        sids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/501", "item": "501"},
            {"url": "https://www.walmart.com/ip/502", "item": "502"},
        ])
        jobs.save_result(db, sids[0], 70, {"dimensions": []}, "ProdA", record=_COPY_RECORD)
        jobs.save_result(db, sids[1], 75, {"dimensions": []}, "ProdB", record=_COPY_RECORD)
    with client.session_transaction() as sess:
        sess["pdp_batch_ids"] = sids
    return sids


def _give_done_copy(app, item, url, projected=95):
    with app.app_context():
        from app import copy_jobs
        from app.db import get_db
        db = get_db()
        uid = db.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()["id"]
        cid = copy_jobs.enqueue_copy_items(db, uid, [{"url": url, "item": item}])[0]
        copy_jobs.save_generated_copy(db, cid, new={}, projected_overall=projected)
        return cid


def test_copy_bar_shown_when_scored(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    data = client.get("/app/pdp-scoring/results").data
    assert b"Copy rewrites" in data
    assert b"Rewrite Copy For All Items" in data


def test_copy_estimate_counts_all_reused(client, auth, app, monkeypatch):
    _seed_batch_for_copy(client, auth, app)
    monkeypatch.delenv("COPYGEN_PRICE_PER_ITEM", raising=False)
    est = client.post("/app/pdp-scoring/create-copy/estimate", data={"all": "1"}).get_json()
    assert est["total"] == 2
    assert est["reused"] == 2          # both carry record_json → no re-fetch
    assert est["refetch"] == 0
    assert est["already_copied"] == 0
    assert est["price_per_item"] is None  # no default → counts only
    assert est["cost"] == ""
    assert "2 items will be rewritten" in est["summary"]


def test_copy_estimate_with_price(client, auth, app, monkeypatch):
    _seed_batch_for_copy(client, auth, app)
    monkeypatch.setenv("COPYGEN_PRICE_PER_ITEM", "0.10")
    est = client.post("/app/pdp-scoring/create-copy/estimate", data={"all": "1"}).get_json()
    assert est["est_cost"] == 0.20
    assert "$0.20" in est["cost"]


def test_copy_estimate_skips_already_copied(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    _give_done_copy(app, "501", "https://www.walmart.com/ip/501")
    est = client.post("/app/pdp-scoring/create-copy/estimate", data={"all": "1"}).get_json()
    assert est["total"] == 1
    assert est["already_copied"] == 1


def test_copy_estimate_selected_subset(client, auth, app):
    sids = _seed_batch_for_copy(client, auth, app)
    est = client.post(
        "/app/pdp-scoring/create-copy/estimate", data={"item_ids": str(sids[1])}
    ).get_json()
    assert est["total"] == 1


def test_copy_batch_enqueue_all_creates_jobs(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    resp = client.post("/app/pdp-scoring/create-copy", data={"all": "1"})
    assert resp.status_code == 302
    assert "/app/pdp-copy/results" in resp.headers["Location"]
    with app.app_context():
        from app.db import get_db
        assert get_db().execute("SELECT COUNT(*) FROM copy_items").fetchone()[0] == 2


def test_copy_batch_enqueue_selected_only(client, auth, app):
    sids = _seed_batch_for_copy(client, auth, app)
    client.post("/app/pdp-scoring/create-copy", data={"item_ids": str(sids[1])})
    with app.app_context():
        from app.db import get_db
        rows = get_db().execute("SELECT item_id FROM copy_items").fetchall()
        assert {r["item_id"] for r in rows} == {"502"}


def test_copy_batch_skips_already_copied(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    _give_done_copy(app, "501", "https://www.walmart.com/ip/501")  # 501 already has copy
    client.post("/app/pdp-scoring/create-copy", data={"all": "1"})
    with app.app_context():
        from app.db import get_db
        items = [r["item_id"] for r in get_db().execute(
            "SELECT item_id FROM copy_items"
        ).fetchall()]
        assert items.count("501") == 1  # not re-created
        assert items.count("502") == 1  # the only new rewrite


def test_copy_batch_nothing_to_do_returns_to_scoring(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    _give_done_copy(app, "501", "https://www.walmart.com/ip/501")
    _give_done_copy(app, "502", "https://www.walmart.com/ip/502")
    resp = client.post("/app/pdp-scoring/create-copy", data={"all": "1"})
    assert resp.status_code == 302
    # Everything already had copy → nothing enqueued, bounce back to scoring.
    assert "/app/pdp-scoring/results" in resp.headers["Location"]


def test_copy_badge_rendered_for_done(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    _give_done_copy(app, "501", "https://www.walmart.com/ip/501")
    data = client.get("/app/pdp-scoring/results").data
    assert b"pdp-copy-badge all-done" in data


def test_view_copy_sets_batch_and_redirects(client, auth, app):
    _seed_batch_for_copy(client, auth, app)
    _give_done_copy(app, "501", "https://www.walmart.com/ip/501")
    resp = client.get("/app/pdp-scoring/view-copy")
    assert resp.status_code == 302
    assert "/app/pdp-copy/results" in resp.headers["Location"]


def test_dashboard_briefing_prompts_setup_without_monitoring(client, auth):
    # A fresh account has no monitoring groups, so the performance briefing shows
    # the "set up tracking" prompt rather than empty gauges.
    auth.register()
    body = client.get("/app").data
    assert b"Set up Daily Monitoring" in body
    assert b"Set up tracking" in body
    # No cards are built in this state.
    assert b"briefing-card" not in body


def test_dashboard_briefing_renders_three_cards_with_data(client, auth, app):
    # With a monitoring group that has current-week data, the three performance
    # cards render for the group's own brand.
    from app import ci_analysis, ci_config, ci_jobs
    from app.db import get_db

    auth.register(email="brief@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?",
                         ("brief@example.com",)).fetchone()[0]
        gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
        mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        comp = ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
        kid = ci_config.add_keyword(db, gid, uid, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid, run_type="monitoring")
        # Seed share-of-search inside the last completed week so a period is offered.
        day = ci_analysis.period_bounds("wow", 0)[0]
        db.execute(
            "INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
            "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, gid, kid, day, None, mine, 8, 0, 8))
        db.execute(
            "INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
            "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, gid, kid, day, None, comp, 2, 0, 2))
        # Register x1 as a tracked product so Search Ranking (Discoverability) and
        # shelf share (Competition) both have a tracked SKU to measure, then place
        # it on page 1.
        db.execute(
            "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url) "
            "VALUES (?,?,?,?,?)", (gid, mine, None, "x1", "https://www.walmart.com/ip/x/x1"))
        db.execute(
            "INSERT INTO ci_search_results (run_id, group_id, keyword_id, scraped_at, position, "
            "position_type, item_id, brand_id, is_new_sku) VALUES (?,?,?,?,?,?,?,?,0)",
            (rid, gid, kid, day, 1, "organic", "x1", mine))
        # A scored product for the brand so the Conversion card has a readiness score.
        db.execute("INSERT INTO scored_items (user_id, item_id, url, brand, overall) "
                   "VALUES (?,?,?,?,?)", (uid, "1", "u", "Tabasco", 85))
        db.commit()

    body = client.get("/app").data
    assert b"briefing-card" in body
    assert b"Discoverability" in body
    assert b"Conversion" in body
    assert b"Competition" in body
    assert b"Tabasco" in body        # the brand label
    assert b"Dig it" in body         # the renamed Explore link
    assert b"#1.0" in body           # Discoverability shows the tracked item's page-1 rank


def test_sparkline_spec_scales_and_signs_direction():
    # The Opportunities sparkline helper: empty -> None; a single point -> just a
    # dot; direction is signed last-vs-first; a flat series lands on the mid-line.
    from app.routes.pages import _SPARK_H, _sparkline

    assert _sparkline([]) is None

    one = _sparkline([50])
    assert one["single"] is True and one["dir"] == "flat"

    up = _sparkline([60, 70, 90])
    assert up["single"] is False and up["dir"] == "up"
    assert len(up["points"].split()) == 3          # one point per value
    down = _sparkline([90, 50])
    assert down["dir"] == "down"

    flat = _sparkline([80, 80, 80])
    assert flat["dir"] == "flat"
    # A flat series draws along the vertical mid-line (every y equals H/2).
    ys = {pt.split(",")[1] for pt in flat["points"].split()}
    assert ys == {str(_SPARK_H / 2)}


def test_dashboard_header_lifts_controls_and_uses_context_line(client, auth, app):
    # The brand/period controls live in the page header (not a separate low row),
    # and the old stacked "Your Summary" block is now a slim brand·period context
    # line above the cards.
    from app import ci_config
    from app.db import get_db

    auth.register(email="hdr@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("hdr@example.com",)).fetchone()[0]
        gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
        ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        db.commit()

    body = client.get("/app").data
    assert b"dash-header" in body         # merged header
    assert b"briefing-select" in body     # the brand selector renders in the header
    assert b"briefing-context" in body       # slim brand/period line replaces the old block
    assert b'class="briefing-brand"' not in body  # old stacked "Your Summary" heading is gone


def test_dashboard_brand_selector_is_csp_safe(client, auth, app):
    # Regression: the brand selector must NOT use an inline onchange handler — the
    # app's CSP is `script-src 'self'`, which blocks inline scripts, so an inline
    # handler silently fails in production (the dropdown can't switch brands). It
    # auto-submits via the external briefing.js, with a no-JS submit button fallback.
    from app import ci_config
    from app.db import get_db

    auth.register(email="csp@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("csp@example.com",)).fetchone()[0]
        gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
        ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        db.commit()

    body = client.get("/app").data
    assert b"briefing-select" in body       # the selector renders (a group exists)
    assert b"onchange" not in body          # no inline handler (CSP would block it)
    assert b"js/briefing.js" in body        # auto-submit comes from the external file
    assert b"briefing-go" in body           # no-JS submit fallback is present


def test_insights_placeholder_renders_and_guards_section(client, auth):
    # The two new Insights rail leaves link to in-design stubs; an unknown section
    # 404s rather than rendering an arbitrary heading.
    auth.register()
    disco = client.get("/app/insights/disco-conv")
    assert disco.status_code == 200
    assert b"Discoverability &amp; Conversion" in disco.data
    assert b"In design" in disco.data

    comp = client.get("/app/insights/competitive")
    assert comp.status_code == 200
    assert b"Competitive Intelligence" in comp.data

    assert client.get("/app/insights/bogus").status_code == 404


def test_rail_shows_new_insights_items_and_hides_old(client, auth):
    # The Insights group now lists the two new sections; the three prior leaves are
    # hidden from the rail (their routes still exist, just unlinked here).
    auth.register()
    body = client.get("/app").data
    assert b"Discoverability &amp; Conversion" in body
    assert b"Competitive Intelligence" in body
    # The old rail label was unique to that leaf, so its absence proves it's hidden.
    assert b"View All Competitive Intelligence Activity" not in body


def test_dashboard_opportunities_rank_content_and_competitive_gaps(client, auth, app):
    # Below the three cards, the Opportunities list surfaces ranked, deep-linked
    # actions: image/copy content gaps (from scoring) + keywords a competitor leads.
    import json

    from app import ci_analysis, ci_config, ci_jobs
    from app.db import get_db

    auth.register(email="opps@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?",
                         ("opps@example.com",)).fetchone()[0]
        gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
        mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        comp = ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
        kw_win = ci_config.add_keyword(db, gid, uid, "hot sauce")
        kw_lose = ci_config.add_keyword(db, gid, uid, "pepper sauce")
        rid = ci_jobs.enqueue_run(db, gid, run_type="monitoring")
        day = ci_analysis.period_bounds("wow", 0)[0]
        # "hot sauce": we lead (8 vs 2). "pepper sauce": a competitor leads (2 vs 8),
        # so it's one lost keyword -> a competitive opportunity.
        for kid, mine_ct, comp_ct in ((kw_win, 8, 2), (kw_lose, 2, 8)):
            db.execute(
                "INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
                "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, gid, kid, day, None, mine, mine_ct, 0, mine_ct))
            db.execute(
                "INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
                "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
                (rid, gid, kid, day, None, comp, comp_ct, 0, comp_ct))

        # Two scored Tabasco products with dimension detail: one weak imagery (image
        # gap), one weak title (copy gap).
        def score(item_id, dims):
            db.execute(
                "INSERT INTO scored_items (user_id, item_id, url, brand, overall, result_json) "
                "VALUES (?,?,?,?,?,?)",
                (uid, item_id, "u", "Tabasco", 60,
                 json.dumps({"overall": 60, "dimensions":
                             [{"key": k, "label": k, "score": s, "available": True}
                              for k, s in dims]})))
        score("1", [("imagery", 40), ("title", 95)])
        score("2", [("imagery", 100), ("title", 30)])
        db.commit()

    body = client.get("/app").data
    assert b"opp-row" in body
    assert b"opp-meter-wrap" in body   # content rows use the proportion meter
    assert b"opp-spark" in body        # the competitive row uses the trend sparkline
    assert b"Improve images on 1 product" in body
    assert b"Strengthen copy on 1 product" in body
    assert b"Win back 1 keyword" in body


def test_dashboard_opportunities_empty_state_when_all_strong(client, auth, app):
    # With a monitoring group but no content/competitive gaps, the list shows the
    # "all caught up" note instead of rows.
    from app import ci_analysis, ci_config, ci_jobs
    from app.db import get_db

    auth.register(email="allgood@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?",
                         ("allgood@example.com",)).fetchone()[0]
        gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
        mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        kid = ci_config.add_keyword(db, gid, uid, "hot sauce")
        rid = ci_jobs.enqueue_run(db, gid, run_type="monitoring")
        day = ci_analysis.period_bounds("wow", 0)[0]
        # We lead the only keyword and have no scored products -> no opportunities.
        db.execute(
            "INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
            "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
            (rid, gid, kid, day, None, mine, 10, 0, 10))
        db.commit()

    body = client.get("/app").data
    assert b"opp-row" not in body
    assert b"No open opportunities" in body
