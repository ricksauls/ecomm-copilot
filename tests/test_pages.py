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
    # Registering logs the user in, so the guarded dashboard is reachable.
    auth.register()
    resp = client.get("/app")
    assert resp.status_code == 200
    # The five "this month" activity tables replace the old demo table. Assert on
    # apostrophe-free substrings of each single-line title (the titles use a
    # curly apostrophe, awkward to match as bytes).
    assert b"Scored This Month" in resp.data
    assert b"With New Copy Created This Month" in resp.data
    assert b"New Image Set" in resp.data
    assert b"Snapshots Created And Run This Month" in resp.data
    assert b"Daily Monitoring Created This Month" in resp.data
    # A brand-new account has no activity, so each table shows its empty state.
    assert b"Nothing scored this month yet." in resp.data
    # The demo table and the (non-functional) Export report button are gone.
    assert b"Products losing ground" not in resp.data
    assert b"Export report" not in resp.data
    # The sort + row-cap enhancement script is wired in.
    assert b"js/dashboard.js" in resp.data


def test_dashboard_is_personalized_to_the_user(client, auth):
    # Portfolio header shows the signed-in user; the old agency name is gone from
    # the header and the topbar breadcrumb.
    auth.register(email="rick@example.com")
    body = client.get("/app").data
    assert b"rick@example.com" in body
    assert b"Meridian Commerce Group" not in body
    # The KPI row: four per-user product metrics plus the two CI activity cards.
    assert b"Products managed" in body
    assert b"scored" in body            # PDP's scored
    assert b"copy created" in body      # PDP's copy created
    assert b"images created" in body    # PDP's images created
    assert b"One-Time Snapshot" in body  # CI snapshot card
    assert b"Daily Monitoring" in body   # CI monitoring card
    assert b"this month" in body        # each card's this-month footnote


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

    # Dashboard: this-month table hides the old row but carries the View all link.
    dash = client.get("/app").data
    assert b"/app/activity/scored" in dash
    assert b"Old Scored Product" not in dash

    # View All: all-time, so the old row appears.
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


def test_view_all_unknown_kind_404s(client, auth):
    auth.register(email="va2@example.com")
    assert client.get("/app/activity/bogus").status_code == 404


def test_view_all_requires_login(client):
    # Guarded like the rest of the workspace — anonymous is redirected to sign-in.
    resp = client.get("/app/activity/scored")
    assert resp.status_code in (301, 302)


def test_activity_rows_link_to_results(client, auth, app):
    # Dashboard rows carry a per-item results link, and that route opens the item's
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

    # The dashboard renders the row as a link to the per-item results route.
    body = client.get("/app").data
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


def test_content_activity_shows_all_three_tables_all_time(client, auth, app):
    # The View Content Activity screen has the three Content Studio tables, all-time
    # (a prior-month scored item appears here even though the dashboard hides it),
    # with clickable rows and the nav link present.
    from app import jobs
    from app.db import get_db

    auth.register(email="ca@example.com")
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", ("ca@example.com",)).fetchone()["id"]
        ids = jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/808", "item": "808"}])
        jobs.save_result(db, ids[0], 91, {"overall": 91}, "Old Content Item")
        db.execute("UPDATE scored_items SET created_at = '2020-03-01 00:00:00' WHERE id = ?", (ids[0],))
        db.commit()
        sid = ids[0]

    resp = client.get("/app/content-activity")
    assert resp.status_code == 200
    assert b"View All Content Activity" in resp.data
    assert b"Products Scored" in resp.data
    assert b"Copy Created" in resp.data
    assert b"Image Sets Created" in resp.data
    # All-time: the prior-month item shows, and its row links to the run's results.
    assert b"Old Content Item" in resp.data
    assert f"/app/pdp-scoring/item/{sid}".encode() in resp.data

    # The rail no longer carries the combined "View All Content Activity" link —
    # it was replaced by per-category history items under each Studio group. The
    # page itself is still reachable by URL; the rail now links the per-category
    # histories (scoring/copy/creative) instead.
    rail = client.get("/app").data
    assert b"/app/activity/scored" in rail  # View Scoring History
    assert b"/app/activity/copy" in rail  # View Copy Content Creation History
    assert b"/app/activity/images" in rail  # View Creative Content Creation History


def test_content_activity_requires_login(client):
    resp = client.get("/app/content-activity")
    assert resp.status_code in (301, 302)


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

    # Rail: the new item is present; the two removed items are gone.
    dash = client.get("/app").data
    assert b"/app/competitive-intel/activity" in dash
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
