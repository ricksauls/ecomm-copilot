"""Smoke tests for the three built pages.

These assert the routes render and return the expected surface, not pixel
fidelity. They exist so CI catches template/route breakage on every push.
"""


def test_landing_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Verbatim hero copy from the design.
    assert b"Your eCommerce Team, Amplified." in resp.data


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
    assert b"Score PDPs (Product Detail Pages)" in resp.data
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


def test_results_subtitle_flashes_while_pending(client, auth):
    # A freshly enqueued (unscored) batch is pending, so the subtitle carries the
    # flash class that CSS animates.
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": "https://www.walmart.com/ip/12345"})
    resp = client.get("/app/pdp-scoring/results")
    assert b"subtitle-scoring" in resp.data
    assert b"scoring in progress" in resp.data


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
    assert b"All activity" in resp.data


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
