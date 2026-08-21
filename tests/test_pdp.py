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


def test_pdp_post_accepts_urls_and_parses_item_number(client, auth):
    auth.register()
    resp = client.post(
        "/app/pdp-scoring",
        data={"urls": ["https://www.walmart.com/ip/10294528", "not-a-url"]},
    )
    assert resp.status_code == 200
    assert b"1 item accepted" in resp.data
    assert b"10294528" in resp.data
    assert b"Skipped" in resp.data  # the invalid entry is surfaced


def test_pdp_post_accepts_csv_upload(client, auth):
    auth.register()
    resp = client.post(
        "/app/pdp-scoring",
        data={"csv": (io.BytesIO(b"https://www.walmart.com/ip/555\n"), "items.csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200
    assert b"555" in resp.data
