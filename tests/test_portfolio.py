"""Tests for Portfolio Management: the portfolio store and its page routes."""

import io

from werkzeug.datastructures import FileStorage

from app import jobs, portfolio
from app.db import get_db
from app.users import create_local_user


def _csv(text: str) -> FileStorage:
    """Wrap CSV text as an uploaded file, as the request layer would deliver it."""
    return FileStorage(
        stream=io.BytesIO(text.encode("utf-8")),
        filename="portfolio.csv",
        content_type="text/csv",
    )


# ── parse_csv ────────────────────────────────────────────────────────────────

def test_parse_csv_reads_named_columns_in_any_order():
    # Header names (not positions) decide the columns, and casing/spacing is lenient.
    csv_text = (
        "Amazon ASIN URL (Optional),Brand,Walmart Item URL\n"
        "https://www.amazon.com/dp/B001,Tabasco,https://www.walmart.com/ip/10294528\n"
    )
    rows, invalid = portfolio.parse_csv(_csv(csv_text))
    assert invalid == 0
    assert rows == [{
        "brand": "Tabasco",
        "walmart_url": "https://www.walmart.com/ip/10294528",
        "amazon_url": "https://www.amazon.com/dp/B001",
    }]


def test_parse_csv_counts_rows_without_a_valid_url_as_invalid():
    csv_text = (
        "Brand,Walmart Item URL,Amazon ASIN URL (Optional)\n"
        "Good,https://www.walmart.com/ip/1,\n"
        "Bad,not-a-url,\n"
        "javascript:,javascript:alert(1),\n"  # rejected scheme
        ",,\n"  # blank line — noise, not invalid
    )
    rows, invalid = portfolio.parse_csv(_csv(csv_text))
    assert [r["walmart_url"] for r in rows] == ["https://www.walmart.com/ip/1"]
    assert invalid == 2


def test_parse_csv_requires_a_brand_on_each_row():
    # Brand is required on every managed item, so a row without one is dropped
    # (counted invalid), not imported with a blank brand.
    csv_text = (
        "Brand,Walmart Item URL,Amazon ASIN URL (Optional)\n"
        "Tabasco,https://www.walmart.com/ip/1,\n"
        ",https://www.walmart.com/ip/2,\n"  # no brand -> invalid
    )
    rows, invalid = portfolio.parse_csv(_csv(csv_text))
    assert [r["brand"] for r in rows] == ["Tabasco"]
    assert invalid == 1


def test_parse_csv_falls_back_to_positional_without_a_recognized_header():
    # No "Walmart" header: treat columns positionally as brand, walmart, amazon,
    # and re-check the first row as data (nothing is silently dropped).
    csv_text = (
        "Tabasco,https://www.walmart.com/ip/1,https://www.amazon.com/dp/B1\n"
        "Cholula,https://www.walmart.com/ip/2,\n"
    )
    rows, invalid = portfolio.parse_csv(_csv(csv_text))
    assert invalid == 0
    assert [r["brand"] for r in rows] == ["Tabasco", "Cholula"]
    assert [r["walmart_url"] for r in rows] == [
        "https://www.walmart.com/ip/1",
        "https://www.walmart.com/ip/2",
    ]


# ── import_rows (dedupe / skip) ───────────────────────────────────────────────

def test_import_skips_items_already_in_the_portfolio(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("p@example.com", "password123")
        rows = [
            {"brand": "Tabasco", "walmart_url": "https://www.walmart.com/ip/slug/10294528", "amazon_url": None},
            {"brand": "Cholula", "walmart_url": "https://www.walmart.com/ip/222", "amazon_url": None},
        ]
        first = portfolio.import_rows(db, uid, rows)
        assert first == {"added": 2, "skipped": 0}

        # Re-import: same item number (even via a different slug) is skipped.
        again = portfolio.import_rows(db, uid, [
            {"brand": "Tabasco", "walmart_url": "https://www.walmart.com/ip/other-slug/10294528", "amazon_url": None},
            {"brand": "New", "walmart_url": "https://www.walmart.com/ip/333", "amazon_url": None},
        ])
        assert again == {"added": 1, "skipped": 1}
        assert len(portfolio.list_items(db, uid)) == 3


def test_import_dedupes_within_one_batch(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("dupe@example.com", "password123")
        result = portfolio.import_rows(db, uid, [
            {"brand": "A", "walmart_url": "https://www.walmart.com/ip/1", "amazon_url": None},
            {"brand": "A", "walmart_url": "https://www.walmart.com/ip/1", "amazon_url": None},
        ])
        assert result == {"added": 1, "skipped": 1}


def test_import_is_scoped_per_user(app):
    with app.app_context():
        db = get_db()
        a = create_local_user("a@example.com", "password123")
        b = create_local_user("b@example.com", "password123")
        portfolio.import_rows(db, a, [{"brand": "X", "walmart_url": "https://www.walmart.com/ip/1", "amazon_url": None}])
        # Same product for a different user is a genuinely new row, not a skip.
        result = portfolio.import_rows(db, b, [{"brand": "X", "walmart_url": "https://www.walmart.com/ip/1", "amazon_url": None}])
        assert result == {"added": 1, "skipped": 0}


# ── add_item / delete_item ────────────────────────────────────────────────────

def test_add_item_validates_and_dedupes(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("add@example.com", "password123")

        ok, err = portfolio.add_item(db, uid, "Tabasco", "https://www.walmart.com/ip/10294528", None)
        assert ok and err is None

        ok, err = portfolio.add_item(db, uid, "Tabasco", "https://www.walmart.com/ip/10294528", None)
        assert not ok and "already" in err.lower()

        ok, err = portfolio.add_item(db, uid, "Bad", "not-a-url", None)
        assert not ok and "valid" in err.lower()

        # Brand is required — a blank/whitespace brand is rejected.
        ok, err = portfolio.add_item(db, uid, "  ", "https://www.walmart.com/ip/999", None)
        assert not ok and "brand" in err.lower()


def test_delete_item_is_owner_scoped(app):
    with app.app_context():
        db = get_db()
        owner = create_local_user("owner@example.com", "password123")
        other = create_local_user("other@example.com", "password123")
        portfolio.add_item(db, owner, "X", "https://www.walmart.com/ip/1", None)
        pk = portfolio.list_items(db, owner)[0]["id"]

        # A non-owner can't delete it (IDOR guard); the owner can.
        assert portfolio.delete_item(db, other, pk) is False
        assert portfolio.delete_item(db, owner, pk) is True
        assert portfolio.list_items(db, owner) == []


def test_distinct_brands_sorted_case_insensitive(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("brands@example.com", "password123")
        for brand, n in [("Zed", 1), ("apple", 2), ("Mango", 3)]:
            portfolio.add_item(db, uid, brand, f"https://www.walmart.com/ip/{n}", None)
        # A legacy NULL-brand row (brand is required on new items, but an older row
        # could be NULL) must be excluded from the filter list.
        db.execute("INSERT INTO portfolio_items (user_id, walmart_url, dedupe_key) "
                   "VALUES (?, ?, ?)", (uid, "https://www.walmart.com/ip/4", "4"))
        db.commit()
        assert portfolio.distinct_brands(db, uid) == ["apple", "Mango", "Zed"]


# ── Title backfill queue ──────────────────────────────────────────────────────

def test_new_items_are_queued_for_a_title(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("title@example.com", "password123")
        portfolio.add_item(db, uid, "Tabasco", "https://www.walmart.com/ip/10294528", None)

        assert portfolio.has_claimable_title(db) is True
        item = portfolio.claim_next_title(db)
        assert item is not None and item["title_status"] == "fetching"
        # Claiming moved it out of the queue (no double-processing).
        assert portfolio.has_claimable_title(db) is False

        portfolio.save_title(db, item["id"], "  Tabasco Original Red Pepper Sauce ")
        row = db.execute(
            "SELECT title, title_status FROM portfolio_items WHERE id = ?", (item["id"],)
        ).fetchone()
        assert row["title"] == "Tabasco Original Red Pepper Sauce"  # trimmed
        assert row["title_status"] == "done"


def test_fail_title_is_terminal(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("fail@example.com", "password123")
        portfolio.add_item(db, uid, "Tabasco", "https://www.walmart.com/ip/1", None)
        item = portfolio.claim_next_title(db)
        portfolio.fail_title(db, item["id"])
        row = db.execute(
            "SELECT title_status FROM portfolio_items WHERE id = ?", (item["id"],)
        ).fetchone()
        assert row["title_status"] == "error"
        # A failed item is not re-queued (no spin).
        assert portfolio.has_claimable_title(db) is False


def test_add_item_tracks_on_add_and_seeds_keywords(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("ta@example.com", "password123")
        ok, _ = portfolio.add_item(db, uid, "Tabasco",
                                   "https://www.walmart.com/ip/10294528", None)
        assert ok
        item = db.execute(
            "SELECT id, tracked FROM portfolio_items WHERE item_id = '10294528'").fetchone()
        assert item["tracked"] == 1  # tracked on add
        kws = db.execute(
            "SELECT slot, kind, keyword FROM portfolio_keywords "
            "WHERE portfolio_item_id = ? ORDER BY slot", (item["id"],)).fetchall()
        assert [(r["slot"], r["kind"]) for r in kws] == [
            (1, "brand"), (2, "brand_product_size"), (3, "category")]
        assert kws[0]["keyword"] == "Tabasco"       # KW1 known at add
        assert kws[1]["keyword"] == "Tabasco"       # KW2 brand-only until the PDP fetch
        assert kws[2]["keyword"] is None            # KW3 category filled later / by the user


def test_import_rows_tracks_and_seeds_keywords(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("ir@example.com", "password123")
        result = portfolio.import_rows(db, uid, [
            {"brand": "Tabasco", "walmart_url": "https://www.walmart.com/ip/111",
             "amazon_url": None},
        ])
        assert result["added"] == 1
        item = db.execute(
            "SELECT id, tracked FROM portfolio_items WHERE item_id = '111'").fetchone()
        assert item["tracked"] == 1
        assert db.execute(
            "SELECT COUNT(*) c FROM portfolio_keywords WHERE portfolio_item_id = ?",
            (item["id"],)).fetchone()["c"] == 3


def test_worker_backfills_descriptors(app, monkeypatch):
    import worker
    with app.app_context():
        db = get_db()
        uid = create_local_user("w@example.com", "password123")
        portfolio.add_item(db, uid, "Tabasco", "https://www.walmart.com/ip/10294528", None)
        item = portfolio.claim_next_title(db)
        monkeypatch.setattr(worker, "fetch_item_descriptors",
                            lambda url, item_id: {"title": "Original Hot Sauce",
                                                  "brand": "Tabasco", "size": "12 oz",
                                                  "category": "Hot Sauce"})
        worker.process_portfolio_descriptors_one(db, item)
        row = db.execute(
            "SELECT title, title_status FROM portfolio_items WHERE id = ?", (item["id"],)
        ).fetchone()
        assert row["title"] == "Original Hot Sauce"
        assert row["title_status"] == "done"
        # KW2/KW3 refined from the fetched size/category (KW1 brand stays).
        kws = {r["slot"]: r["keyword"]
               for r in db.execute(
                   "SELECT slot, keyword FROM portfolio_keywords WHERE portfolio_item_id = ?",
                   (item["id"],))}
        assert kws[1] == "Tabasco"
        assert kws[2] == "Tabasco Original Hot Sauce 12 oz"
        assert kws[3] == "Hot Sauce"


def test_worker_marks_error_when_fetch_blocked(app, monkeypatch):
    import worker
    from app.fetch import FetchBlocked
    with app.app_context():
        db = get_db()
        uid = create_local_user("blocked@example.com", "password123")
        portfolio.add_item(db, uid, "Tabasco", "https://www.walmart.com/ip/10294528", None)
        item = portfolio.claim_next_title(db)

        def _blocked(url, item_id):
            raise FetchBlocked("bot wall")
        monkeypatch.setattr(worker, "fetch_item_descriptors", _blocked)
        worker.process_portfolio_descriptors_one(db, item)  # must not raise
        row = db.execute(
            "SELECT title_status FROM portfolio_items WHERE id = ?", (item["id"],)
        ).fetchone()
        assert row["title_status"] == "error"


# ── dashboard union ───────────────────────────────────────────────────────────

def test_portfolio_feeds_managed_product_and_brand_counts(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("kpi@example.com", "password123")
        jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/1", "item": "1"}])
        db.execute("UPDATE scored_items SET brand = 'Scored' WHERE user_id = ?", (uid,))
        db.commit()
        portfolio.add_item(db, uid, "Portfolio", "https://www.walmart.com/ip/2", None)

        # Product 2 only exists in the portfolio -> union counts both products,
        # and the portfolio brand joins the distinct-brand count.
        assert jobs.count_managed_products(db, uid) == 2
        assert jobs.count_managed_brands(db, uid) == 2


# ── routes ────────────────────────────────────────────────────────────────────

def test_portfolio_page_requires_login(client):
    resp = client.get("/app/portfolio")
    assert resp.status_code in (302, 401)


def test_import_add_delete_flow_via_routes(client, auth):
    auth.register(email="flow@example.com", password="password123")
    auth.login(email="flow@example.com", password="password123")

    # Import a CSV.
    csv_bytes = (
        b"Brand,Walmart Item URL,Amazon ASIN URL (Optional)\n"
        b"Tabasco,https://www.walmart.com/ip/10294528,\n"
    )
    resp = client.post(
        "/app/portfolio/import",
        data={"csv": (io.BytesIO(csv_bytes), "p.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert b"10294528" in resp.data  # the item appears in the table

    # Add one more inline.
    client.post("/app/portfolio/add", data={
        "brand": "Cholula",
        "walmart_url": "https://www.walmart.com/ip/222",
        "amazon_url": "",
    }, follow_redirects=True)

    page = client.get("/app/portfolio")
    assert b"Cholula" in page.data

    # The pick-list shows on the scoring and copy intakes.
    scoring = client.get("/app/pdp-scoring")
    assert b"Choose from your managed items" in scoring.data
    assert b"10294528" in scoring.data
    copy = client.get("/app/pdp-copy")
    assert b"Choose from your managed items" in copy.data


def test_copy_intake_does_not_require_a_brand(client, auth):
    # Product Copy no longer collects a brand — the URL is fetched and the worker
    # reads the brand from the PDP — so a URL with no brand enqueues and redirects.
    auth.register(email="copybrand@example.com", password="password123")
    resp = client.post("/app/pdp-copy", data={"urls": "https://www.walmart.com/ip/10294528"})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/pdp-copy/results")


def test_template_download_has_expected_headers(client, auth):
    auth.register(email="tmpl@example.com", password="password123")
    auth.login(email="tmpl@example.com", password="password123")
    resp = client.get("/app/portfolio/template.csv")
    assert resp.status_code == 200
    assert "text/csv" in resp.headers["Content-Type"]
    assert b"Brand,Walmart Item URL,Amazon ASIN URL (Optional)" in resp.data


# ── Track toggle (item tracking) ──────────────────────────────────────────────

def _add_one(client, brand, url):
    client.post("/app/portfolio/add",
                data={"brand": brand, "walmart_url": url, "amazon_url": ""},
                follow_redirects=True)


def test_portfolio_toggle_track_on_and_off(client, auth, app):
    auth.register(email="track@example.com", password="password123")
    _add_one(client, "Tabasco", "https://www.walmart.com/ip/10294528")
    with app.app_context():
        row = get_db().execute(
            "SELECT id FROM portfolio_items WHERE item_id = '10294528'").fetchone()
        pid = row["id"]

    # Items are tracked on add — the active "Tracking" pill renders from the start.
    assert b"pf-track-on" in client.get("/app/portfolio").data
    with app.app_context():
        assert get_db().execute(
            "SELECT tracked FROM portfolio_items WHERE id = ?", (pid,)).fetchone()["tracked"] == 1

    off = client.post(f"/app/portfolio/track/{pid}", data={"tracked": "0"},
                      follow_redirects=True)
    assert b"Stopped tracking" in off.data
    with app.app_context():
        assert get_db().execute(
            "SELECT tracked FROM portfolio_items WHERE id = ?", (pid,)).fetchone()["tracked"] == 0

    on = client.post(f"/app/portfolio/track/{pid}", data={"tracked": "1"},
                     follow_redirects=True)
    assert b"pf-track-on" in on.data  # the active "Tracking" pill renders again
    with app.app_context():
        assert get_db().execute(
            "SELECT tracked FROM portfolio_items WHERE id = ?", (pid,)).fetchone()["tracked"] == 1


def test_portfolio_toggle_track_is_user_scoped(client, auth, app):
    # Owner adds an item (tracked on add); a second user cannot flip its flag.
    auth.register(email="owner2@example.com", password="password123")
    _add_one(client, "Tabasco", "https://www.walmart.com/ip/10294528")
    with app.app_context():
        pid = get_db().execute(
            "SELECT id FROM portfolio_items WHERE item_id = '10294528'").fetchone()["id"]
    auth.logout()
    auth.register(email="attacker@example.com", password="password123")

    resp = client.post(f"/app/portfolio/track/{pid}", data={"tracked": "0"},
                       follow_redirects=True)
    assert b"no longer in your portfolio" in resp.data  # benign, no leak
    with app.app_context():
        assert get_db().execute(
            "SELECT tracked FROM portfolio_items WHERE id = ?", (pid,)).fetchone()["tracked"] == 1


# ── Per-item tracking detail page (build #2b) ─────────────────────────────────

def _seed_facts(app, pid, rows):
    """Insert portfolio_item_facts snapshots directly. rows: (day, price, rating, reviews)."""
    with app.app_context():
        db = get_db()
        for day, price, rating, reviews in rows:
            db.execute(
                "INSERT INTO portfolio_item_facts "
                "(portfolio_item_id, user_id, walmart_item_id, captured_at, price, "
                " price_string, in_stock, rating, review_count, image_count, has_video) "
                "VALUES (?, 1, '10294528', ?, ?, ?, 1, ?, ?, 5, 0)",
                (pid, day, price, f"${price:.2f}", rating, reviews),
            )
        db.commit()


def test_item_tracking_page_renders_trends(client, auth, app):
    auth.register(email="trkui@example.com", password="password123")
    _add_one(client, "Tabasco", "https://www.walmart.com/ip/Tabasco/10294528")
    with app.app_context():
        pid = get_db().execute(
            "SELECT id FROM portfolio_items WHERE item_id = '10294528'").fetchone()["id"]
    _seed_facts(app, pid, [("2026-10-10", 3.98, 4.5, 100), ("2026-10-11", 3.50, 4.6, 120)])

    page = client.get(f"/app/portfolio/{pid}/tracking")
    assert page.status_code == 200
    assert b"Current" in page.data
    assert b"Snapshot history" in page.data
    assert b"$3.50" in page.data  # latest price in the KPI/metric
    assert b"trk-spark-line" in page.data  # a sparkline rendered (>=2 points)


def test_item_tracking_page_empty_state(client, auth, app):
    auth.register(email="trkempty@example.com", password="password123")
    _add_one(client, "Tabasco", "https://www.walmart.com/ip/Tabasco/10294528")
    with app.app_context():
        pid = get_db().execute(
            "SELECT id FROM portfolio_items WHERE item_id = '10294528'").fetchone()["id"]
    page = client.get(f"/app/portfolio/{pid}/tracking")
    assert page.status_code == 200
    assert b"No tracking data yet" in page.data


def test_item_tracking_page_is_user_scoped(client, auth, app):
    auth.register(email="trkowner@example.com", password="password123")
    _add_one(client, "Tabasco", "https://www.walmart.com/ip/Tabasco/10294528")
    with app.app_context():
        pid = get_db().execute(
            "SELECT id FROM portfolio_items WHERE item_id = '10294528'").fetchone()["id"]
    auth.logout()
    auth.register(email="trkattacker@example.com", password="password123")
    assert client.get(f"/app/portfolio/{pid}/tracking").status_code == 404
