"""Route tests for PDP Copy Content Creation (intake, results, generate, cross-link)."""

from app import copy_jobs, jobs
from app.db import get_db


def test_copy_routes_require_login(client):
    assert client.get("/app/pdp-copy").status_code == 302
    assert client.get("/app/pdp-copy/results").status_code == 302
    assert client.get("/app/pdp-copy/results.pdf").status_code == 302
    assert client.post("/app/pdp-copy/generate").status_code == 302
    assert client.post("/app/pdp-scoring/create-copy").status_code == 302


def test_copy_intake_renders(client, auth):
    auth.register()
    resp = client.get("/app/pdp-copy")
    assert resp.status_code == 200
    assert b"Create PDP Copy Content" in resp.data
    # The fetch button carries the exact requested label.
    assert b"Get Current Copy Content" in resp.data
    # CSV cap mirrors app.pdp.MAX_ITEMS (100).
    assert b"up to 100" in resp.data


def test_copy_intake_enqueues_and_redirects(client, auth):
    auth.register()
    resp = client.post(
        "/app/pdp-copy",
        data={"urls": "https://www.walmart.com/ip/10294528"},
    )
    # Redirects to the results page on success.
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/pdp-copy/results")
    # The results page then shows the queued item.
    results = client.get("/app/pdp-copy/results")
    assert results.status_code == 200
    assert b"Fetching" in results.data


def test_copy_intake_rejects_empty(client, auth):
    auth.register()
    resp = client.post("/app/pdp-copy", data={"urls": "not-a-url"})
    assert resp.status_code == 400
    assert b"No valid item URLs" in resp.data


def test_copy_status_json(client, auth):
    auth.register()
    client.post("/app/pdp-copy", data={"urls": "https://www.walmart.com/ip/1"})
    resp = client.get("/app/pdp-copy/status")
    data = resp.get_json()
    assert data["pending"] is True
    assert len(data["items"]) == 1
    assert data["items"][0]["status"] == "queued"


def test_generate_advances_fetched_items(client, auth, app):
    auth.register()
    client.post("/app/pdp-copy", data={"urls": "https://www.walmart.com/ip/1"})
    # Simulate the worker having fetched the current copy.
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT id FROM copy_items").fetchone()
        copy_jobs.save_current_copy(
            db, row["id"], title="P", current={"record": {"url": "u"}},
            current_overall=50, keywords=[], next_status="fetched",
        )
    resp = client.post("/app/pdp-copy/generate")
    assert resp.status_code == 302
    with app.app_context():
        status = get_db().execute("SELECT status FROM copy_items").fetchone()["status"]
        assert status == "gen_queued"


def test_copy_results_pdf_download(client, auth, app):
    auth.register()
    client.post("/app/pdp-copy", data={"urls": "https://www.walmart.com/ip/1"})
    # Simulate a completed item (current + new copy + scores).
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT id FROM copy_items ORDER BY id DESC LIMIT 1").fetchone()
        copy_jobs.save_current_copy(
            db, row["id"], title="Acme Widget",
            current={"title": "OLD", "bullets": ["a"], "description": "old desc",
                     "record": {"url": "u"}},
            current_overall=60, keywords=[], next_status="fetched",
        )
        copy_jobs.save_generated_copy(
            db, row["id"],
            new={"title": "NEW", "bullets": ["b1", "b2"], "description": "new desc"},
            projected_overall=88,
        )
    resp = client.get("/app/pdp-copy/results.pdf")
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert resp.data[:5] == b"%PDF-"  # real PDF, not an error page
    assert "attachment" in resp.headers.get("Content-Disposition", "")


def test_copy_results_shows_download_only_when_done(client, auth, app):
    auth.register()
    client.post("/app/pdp-copy", data={"urls": "https://www.walmart.com/ip/1"})
    # Still fetching -> no Download PDF button yet.
    assert b"Download PDF" not in client.get("/app/pdp-copy/results").data
    with app.app_context():
        db = get_db()
        row = db.execute("SELECT id FROM copy_items ORDER BY id DESC LIMIT 1").fetchone()
        copy_jobs.save_current_copy(
            db, row["id"], title="P",
            current={"title": "OLD", "bullets": ["a"], "description": "d",
                     "record": {"url": "u"}},
            current_overall=60, keywords=[], next_status="fetched",
        )
        copy_jobs.save_generated_copy(
            db, row["id"], new={"title": "NEW", "bullets": ["b"], "description": "d"},
            projected_overall=88,
        )
    # Now done -> the button appears.
    assert b"Download PDF" in client.get("/app/pdp-copy/results").data


def test_scoring_cross_link_creates_auto_generate_copy_jobs(client, auth, app):
    auth.register()
    # Seed a scored item for this user.
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users").fetchone()["id"]
        ids = jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/5", "item": "5"}])
        jobs.save_result(db, ids[0], 70, {"overall": 70, "dimensions": []}, "Prod")
        scored_id = ids[0]

    resp = client.post("/app/pdp-scoring/create-copy", data={"item_ids": str(scored_id)})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/pdp-copy/results")
    with app.app_context():
        row = get_db().execute("SELECT url, auto_generate, status FROM copy_items").fetchone()
        assert row["url"] == "https://www.walmart.com/ip/5"
        assert row["auto_generate"] == 1
        assert row["status"] == "queued"


def test_scoring_cross_link_ignores_other_users_items(client, auth, app):
    auth.register()
    with app.app_context():
        from app.users import create_local_user
        other = create_local_user("someone@else.com", "password123")
        db = get_db()
        ids = jobs.enqueue_items(db, other, [{"url": "https://www.walmart.com/ip/9", "item": "9"}])
        jobs.save_result(db, ids[0], 70, {"overall": 70, "dimensions": []}, "Prod")
        other_id = ids[0]

    # Trying to copy another user's item creates nothing (IDOR guard) and bounces
    # back to the scoring results.
    resp = client.post("/app/pdp-scoring/create-copy", data={"item_ids": str(other_id)})
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/pdp-scoring/results")
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) AS c FROM copy_items").fetchone()["c"] == 0
