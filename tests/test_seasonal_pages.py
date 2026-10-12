"""Route tests for Seasonal Content Campaigns (create → add (auto main-image) → review → download)."""

from app.db import get_db
from app.seasonal import store


def _uid(db, email):
    return db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


# --- guards -----------------------------------------------------------------

def test_home_requires_login(client):
    assert client.get("/app/seasonal").status_code in (302, 401)


def test_detail_requires_login(client):
    assert client.get("/app/seasonal/1").status_code in (302, 401)


def test_detail_404_for_other_users_campaign(client, auth, app):
    auth.register(email="sp-owner@example.com")
    resp = client.post("/app/seasonal", data={"name": "Mine", "theme": "christmas"})
    cid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    auth.logout()
    auth.register(email="sp-intruder@example.com")
    # IDOR: a different user gets a 404, not the campaign.
    assert client.get(f"/app/seasonal/{cid}").status_code == 404


# --- create -----------------------------------------------------------------

def test_home_get_renders_form(client, auth):
    auth.register(email="sp-form@example.com")
    resp = client.get("/app/seasonal")
    assert resp.status_code == 200
    assert b"Seasonal Content Campaigns" in resp.data
    assert b'name="theme"' in resp.data


def test_create_campaign_redirects_to_detail(client, auth, app):
    auth.register(email="sp-create@example.com")
    resp = client.post("/app/seasonal", data={
        "name": "Holiday 2026", "theme": "christmas",
        "launch_date": "2026-12-01", "expiration_date": "2026-12-31",
    })
    assert resp.status_code == 302
    assert "/app/seasonal/" in resp.headers["Location"]
    with app.app_context():
        db = get_db()
        uid = _uid(db, "sp-create@example.com")
        campaigns = store.list_campaigns(db, uid)
        assert len(campaigns) == 1
        assert campaigns[0]["launch_date"] == "2026-12-01"


def test_create_rejects_bad_theme(client, auth):
    auth.register(email="sp-badtheme@example.com")
    resp = client.post("/app/seasonal", data={"name": "X", "theme": "easter"})
    assert resp.status_code == 400


def test_create_rejects_bad_date(client, auth):
    auth.register(email="sp-baddate@example.com")
    resp = client.post("/app/seasonal", data={
        "name": "X", "theme": "summer", "launch_date": "not-a-date"})
    assert resp.status_code == 400


def test_create_requires_name(client, auth):
    auth.register(email="sp-noname@example.com")
    resp = client.post("/app/seasonal", data={"name": "  ", "theme": "summer"})
    assert resp.status_code == 400


# --- add products + generate ------------------------------------------------

def _make_campaign(client, auth, app, email):
    auth.register(email=email)
    resp = client.post("/app/seasonal", data={"name": "C", "theme": "halloween"})
    cid = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    with app.app_context():
        uid = _uid(get_db(), email)
    return cid, uid


def test_add_products_queues_acquire(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-add@example.com")
    resp = client.post(f"/app/seasonal/{cid}/products", data={
        "urls": "https://www.walmart.com/ip/10294528\nhttps://www.amazon.com/dp/B07XYZ1234",
    })
    assert resp.status_code == 302
    with app.app_context():
        db = get_db()
        products = store.products_for_campaign(db, cid, uid)
        assert len(products) == 2
        assert all(p["status"] == store.PRODUCT_QUEUED for p in products)
        # URLs were classified by source.
        assert {p["source_type"] for p in products} == {"walmart", "amazon_pdp"}


def test_add_products_from_managed_items_picker(client, auth, app):
    # The managed-items picker posts one "urls" value per checked item (same field
    # as the manual textarea); the route joins every value, so a multi-select adds
    # each product.
    cid, uid = _make_campaign(client, auth, app, "sp-pick@example.com")
    resp = client.post(f"/app/seasonal/{cid}/products", data={
        "urls": ["https://www.walmart.com/ip/10294528",
                 "https://www.walmart.com/ip/555123"],
    })
    assert resp.status_code == 302
    with app.app_context():
        products = store.products_for_campaign(get_db(), cid, uid)
        assert len(products) == 2


def test_retry_product_requeues_failed_acquire(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-retry@example.com")
    with app.app_context():
        db = get_db()
        pid = store.add_product(db, campaign_id=cid, user_id=uid,
                                source_url="https://www.walmart.com/ip/10294528",
                                source_type="walmart")
        store.set_product_status(db, pid, store.PRODUCT_ERROR, error="Walmart blocked the request.")
    resp = client.post(f"/app/seasonal/{cid}/product/{pid}/retry")
    assert resp.status_code == 302
    with app.app_context():
        row = store.get_product(get_db(), pid, uid)
        assert row["status"] == store.PRODUCT_QUEUED and row["error"] is None


def test_regenerate_requeues_main_image(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-regen@example.com")
    with app.app_context():
        db = get_db()
        pid = store.add_product(db, campaign_id=cid, user_id=uid,
                                source_url="https://cdn/x.jpg", source_type="image")
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                   full_url="https://cdn/a.jpg", thumb_url=None)
        store.update_image(db, iid, gen_status=store.GEN_READY,
                           final_path="seasonal/1/final/1.png")
    resp = client.post(f"/app/seasonal/{cid}/image/{iid}/regenerate")
    assert resp.status_code == 302
    with app.app_context():
        # Re-queued for a fresh backdrop.
        assert store.get_image(get_db(), iid, uid)["gen_status"] == store.GEN_QUEUED


# --- status / download / delete ---------------------------------------------

def test_status_json_reports_pending(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-status@example.com")
    with app.app_context():
        db = get_db()
        store.add_product(db, campaign_id=cid, user_id=uid,
                          source_url="https://cdn/x.jpg", source_type="image")
    resp = client.get(f"/app/seasonal/{cid}/status")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["pending"] is True  # a queued product is pending work
    assert len(data["products"]) == 1


def test_image_download_404_until_ready(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-dl@example.com")
    with app.app_context():
        db = get_db()
        pid = store.add_product(db, campaign_id=cid, user_id=uid,
                                source_url="https://cdn/x.jpg", source_type="image")
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                       full_url="https://cdn/a.jpg", thumb_url=None)
    # Not generated yet → 404 (no final file).
    assert client.get(f"/app/seasonal/{cid}/image/{iid}/download").status_code == 404


def test_edit_campaign_persists_name_and_dates(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-edit@example.com")
    resp = client.post(f"/app/seasonal/{cid}/edit", data={
        "name": "Renamed Campaign", "expiration_date": "2026-10-18",
        "launch_date": "", "reminder_date": ""})
    assert resp.status_code == 302
    with app.app_context():
        row = store.get_campaign(get_db(), cid, uid)
        assert row["name"] == "Renamed Campaign"
        assert row["expiration_date"] == "2026-10-18"
        assert row["launch_date"] is None


def test_delete_campaign(client, auth, app):
    cid, uid = _make_campaign(client, auth, app, "sp-del@example.com")
    resp = client.post(f"/app/seasonal/{cid}/delete")
    assert resp.status_code == 302
    with app.app_context():
        assert store.get_campaign(get_db(), cid, uid) is None


def test_create_tab_has_no_inline_campaign_list(client, auth, app):
    # The Create-New tab no longer carries an inline campaign list (it moved to
    # History); creating a campaign must not make one appear there.
    _make_campaign(client, auth, app, "sp-nolist@example.com")
    resp = client.get("/app/seasonal")
    assert resp.status_code == 200
    assert b"Your campaigns" not in resp.data
    assert b"History - Seasonal Campaigns Created" in resp.data  # the renamed tab


def test_history_lists_all_campaigns_including_drafts(client, auth, app):
    # History is now the single campaign list (the Create-New inline list was
    # removed), so a draft campaign with no ready image must still be listed.
    cid, uid = _make_campaign(client, auth, app, "sp-hist@example.com")
    resp = client.get("/app/seasonal/history")
    assert resp.status_code == 200
    assert b"No campaigns yet" not in resp.data
    assert f"/app/seasonal/{cid}".encode() in resp.data  # row links to the campaign
    assert b"Draft" in resp.data                          # draft status pill
    # Once it produces a ready image it's still listed, now flagged Ready.
    with app.app_context():
        db = get_db()
        pid = store.add_product(db, campaign_id=cid, user_id=uid,
                                source_url="https://cdn/x.jpg", source_type="image")
        iid = store.add_main_image(db, product_id=pid, campaign_id=cid, user_id=uid,
                                       full_url="https://cdn/a.jpg", thumb_url=None)
        store.update_image(db, iid, gen_status=store.GEN_READY, final_path="seasonal/1/final/1.png")
        store.recompute_campaign_status(db, cid)
    resp = client.get("/app/seasonal/history")
    assert f"/app/seasonal/{cid}".encode() in resp.data
    assert b"Ready" in resp.data                          # ready status pill
