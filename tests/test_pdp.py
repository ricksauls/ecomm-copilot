"""Tests for PDP Content Scoring: URL/CSV parsing and the intake route."""

import io

from werkzeug.datastructures import FileStorage

from app import pdp


# --- Unit: validation and parsing ----------------------------------------


def test_validate_item_url_accepts_http_and_https():
    url = "https://www.walmart.com/ip/10294528"
    assert pdp.validate_item_url(url) == url
    assert pdp.validate_item_url("  http://walmart.com/ip/1  ") == "http://walmart.com/ip/1"


def test_validate_item_url_rejects_non_http_and_junk():
    assert pdp.validate_item_url("javascript:alert(1)") is None
    assert pdp.validate_item_url("ftp://host/x") is None
    assert pdp.validate_item_url("just some text") is None
    assert pdp.validate_item_url("") is None
    assert pdp.validate_item_url("https://" + "a" * 3000) is None


def test_item_number_from_url():
    assert pdp.item_number_from_url("https://www.walmart.com/ip/seort/10294528") == "10294528"
    assert pdp.item_number_from_url("https://www.walmart.com/ip/10294528") == "10294528"
    assert pdp.item_number_from_url("https://www.walmart.com/ip/some-name") is None


def test_clean_brand_trims_bounds_and_blanks_to_none():
    assert pdp.clean_brand("  Tabasco  ") == "Tabasco"
    assert pdp.clean_brand("") is None
    assert pdp.clean_brand("   ") is None
    assert pdp.clean_brand(None) is None
    assert len(pdp.clean_brand("x" * 500)) == pdp.MAX_BRAND_LEN


def test_collect_items_dedupes_and_reports_rejects():
    accepted, rejected = pdp.collect_items(
        [
            "https://www.walmart.com/ip/1",
            "https://www.walmart.com/ip/1",  # duplicate
            "not-a-url",
            "",  # empty is ignored, not rejected
        ],
        None,
    )
    assert accepted == ["https://www.walmart.com/ip/1"]
    assert rejected == ["not-a-url"]


def test_collect_items_skips_untouched_autofill_prefix():
    # The intake fields autofill WALMART_IP_PREFIX; a row left at just the prefix
    # (no item number) is incomplete and should be dropped silently, not rejected.
    accepted, rejected = pdp.collect_items(
        [
            pdp.WALMART_IP_PREFIX,           # untouched autofill -> skipped
            pdp.WALMART_IP_PREFIX + "10294528",  # completed -> accepted
            "https://www.walmart.com/ip/no-number",  # item-less, edited -> rejected
        ],
        None,
    )
    assert accepted == ["https://www.walmart.com/ip/10294528"]
    assert rejected == ["https://www.walmart.com/ip/no-number"]


def test_urls_from_csv_scans_any_cell():
    data = b"url\nhttps://www.walmart.com/ip/111\nsomething,https://www.walmart.com/ip/222\ngarbage\n"
    fs = FileStorage(stream=io.BytesIO(data), filename="items.csv")
    assert pdp.urls_from_csv(fs) == [
        "https://www.walmart.com/ip/111",
        "https://www.walmart.com/ip/222",
    ]


# --- Route ---------------------------------------------------------------


def test_pdp_requires_login(client):
    resp = client.get("/app/pdp-scoring")
    assert resp.status_code == 302
    assert "/signin" in resp.headers["Location"]


def test_pdp_get_renders_form(client, auth):
    auth.register()
    resp = client.get("/app/pdp-scoring")
    assert resp.status_code == 200
    assert b"Score items" in resp.data
    assert b"walmart.com/ip/10294528" in resp.data
    # The optional batch-level brand field is present.
    assert b'name="brand"' in resp.data


def test_pdp_post_persists_entered_brand(client, auth):
    from app.db import get_db

    auth.register()
    client.post(
        "/app/pdp-scoring",
        data={"urls": ["https://www.walmart.com/ip/10294528"], "brand": "  Tabasco "},
    )
    with client.application.app_context():
        row = get_db().execute(
            "SELECT brand FROM scored_items WHERE item_id = '10294528'"
        ).fetchone()
        assert row["brand"] == "Tabasco"  # trimmed by clean_brand


def test_pdp_post_enqueues_and_redirects(client, auth):
    auth.register()
    resp = client.post(
        "/app/pdp-scoring",
        data={"urls": ["https://www.walmart.com/ip/10294528", "not-a-url"]},
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/app/pdp-scoring/results")
    # The results page shows the enqueued item as queued (worker not running).
    results = client.get("/app/pdp-scoring/results")
    assert results.status_code == 200
    assert b"10294528" in results.data
    assert b"Queued" in results.data


def test_pdp_post_csv_enqueues(client, auth):
    auth.register()
    resp = client.post(
        "/app/pdp-scoring",
        data={"csv": (io.BytesIO(b"https://www.walmart.com/ip/555\n"), "items.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 302
    assert b"555" in client.get("/app/pdp-scoring/results").data


def test_pdp_post_empty_is_rejected(client, auth):
    auth.register()
    resp = client.post("/app/pdp-scoring", data={"urls": ["not-a-url"]})
    assert resp.status_code == 400
    assert b"No valid item URLs" in resp.data


def test_pdp_status_reports_pending(client, auth):
    auth.register()
    client.post("/app/pdp-scoring", data={"urls": ["https://www.walmart.com/ip/10294528"]})
    status = client.get("/app/pdp-scoring/status").get_json()
    assert status["pending"] is True
    assert status["items"][0]["item_id"] == "10294528"
    assert status["items"][0]["status"] == "queued"
