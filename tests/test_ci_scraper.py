"""Tests for the CI scraper's pure card->row builder (no browser involved)."""

from app import ci_scraper


def _card(name, item_id, listing_type="organic", product_url=""):
    return {"name": name, "item_id": item_id, "listing_type": listing_type,
            "product_url": product_url}


def _product(item_id, brand_id, url):
    return {"walmart_item_id": item_id, "brand_id": brand_id, "walmart_url": url}


def _brand(brand_id, btype, tracked=1, name="B"):
    return {"id": brand_id, "type": btype, "tracked": tracked, "name": name}


def test_positions_and_types_recorded_in_order():
    cards = [
        _card("A", "1", "sponsored"),
        _card("B", "2", "organic"),
        _card("C", "3", "organic"),
    ]
    rows = ci_scraper.build_result_rows(
        cards, run_id=1, group_id=1, keyword_id=1,
        item_map={}, brand_map={}, scrape_date="2026-08-22",
    )
    assert [r["position"] for r in rows] == [1, 2, 3]
    assert [r["position_type"] for r in rows] == ["sponsored", "organic", "organic"]
    # Unmatched cards get no brand.
    assert all(r["brand_id"] is None for r in rows)


def test_brand_match_by_item_id():
    item_map = {"10294528": _product("10294528", brand_id=7, url="https://www.walmart.com/ip/x/10294528")}
    brand_map = {7: _brand(7, "mine")}
    rows = ci_scraper.build_result_rows(
        [_card("Tabasco", "10294528")], run_id=1, group_id=1, keyword_id=1,
        item_map=item_map, brand_map=brand_map,
    )
    assert rows[0]["brand_id"] == 7
    assert rows[0]["is_new_sku"] == 0  # 'mine' brands never flagged new


def test_match_by_numeric_from_url_when_data_item_id_is_opaque():
    # Real Walmart cards carry an opaque data-item-id (e.g. '3K2RMCS1KI5D') but a
    # /ip/<slug>/<number> URL. Match + store must use the numeric item number so
    # brand attribution AND the ranking join work.
    url = "https://www.walmart.com/ip/tabasco-original-5-oz/10294527"
    item_map = {"10294527": _product("10294527", brand_id=1, url="https://www.walmart.com/ip/10294527")}
    brand_map = {1: _brand(1, "mine")}
    rows = ci_scraper.build_result_rows(
        [_card("Tabasco", "3K2RMCS1KI5D", product_url=url + "?classType=0")],
        run_id=1, group_id=1, keyword_id=1, item_map=item_map, brand_map=brand_map,
    )
    assert rows[0]["brand_id"] == 1
    assert rows[0]["item_id"] == "10294527"  # numeric stored, not the opaque code


def test_opaque_id_with_no_url_is_stored_as_is_and_unmatched():
    rows = ci_scraper.build_result_rows(
        [_card("Mystery", "3K2RMCS1KI5D")], run_id=1, group_id=1, keyword_id=1,
        item_map={}, brand_map={},
    )
    assert rows[0]["brand_id"] is None
    assert rows[0]["item_id"] == "3K2RMCS1KI5D"


def test_sponsored_card_attributed_by_brand_name():
    # Sponsored slots carry an opaque id + a tracking URL (no /ip/<number>), so
    # they can only be attributed by the brand name in the card title.
    brand_map = {5: _brand(5, "competitor", name="Frank's Red Hot")}
    rows = ci_scraper.build_result_rows(
        [_card("Frank's RedHot Original Cayenne Pepper Sauce, 12 oz",
               "3K2RMCS1KI5D", listing_type="sponsored", product_url="")],
        run_id=1, group_id=1, keyword_id=1, item_map={}, brand_map=brand_map,
    )
    assert rows[0]["brand_id"] == 5
    assert rows[0]["position_type"] == "sponsored"


def test_tracked_item_sponsored_slot_matched_by_title():
    # A tracked product's sponsored slot has an opaque id + tracking URL, but the
    # SAME title as its organic card. It should be tied back to the tracked item
    # (numeric id stored), so its position counts in the item's ranking. Sponsored
    # sits ABOVE organic here, exercising the two-pass title learning.
    title = "Frank's RedHot Original Cayenne Pepper Sauce, 5 fl oz"
    item_map = {"17247773": _product("17247773", brand_id=6,
                                     url="https://www.walmart.com/ip/x/17247773")}
    brand_map = {6: _brand(6, "competitor", name="Frank's Red Hot")}
    rows = ci_scraper.build_result_rows(
        [
            _card(title, "3K2RMCS1KI5D", listing_type="sponsored", product_url=""),
            _card(title, "opaque-org",
                  product_url="https://www.walmart.com/ip/franks/17247773"),
        ],
        run_id=1, group_id=1, keyword_id=1, item_map=item_map, brand_map=brand_map,
    )
    spon, org = rows[0], rows[1]
    assert org["item_id"] == "17247773" and org["brand_id"] == 6
    # The sponsored slot now carries the tracked numeric id and brand.
    assert spon["position_type"] == "sponsored"
    assert spon["item_id"] == "17247773" and spon["brand_id"] == 6


def test_untracked_brand_sponsored_slot_not_tied_to_tracked_item():
    # A sponsored slot for a DIFFERENT (untracked) SKU of the brand has a different
    # title, so it must NOT inherit the tracked item's numeric id — only brand-name
    # attribution (for share of shelf), keeping its opaque id.
    tracked_title = "Frank's RedHot Original Cayenne Pepper Sauce, 5 fl oz"
    item_map = {"17247773": _product("17247773", brand_id=6,
                                     url="https://www.walmart.com/ip/x/17247773")}
    brand_map = {6: _brand(6, "competitor", name="Frank's Red Hot")}
    rows = ci_scraper.build_result_rows(
        [
            _card("Frank's RedHot Buffalo Wings Sauce, 12 fl oz", "OTHERSKU01",
                  listing_type="sponsored", product_url=""),
            _card(tracked_title, "opaque-org",
                  product_url="https://www.walmart.com/ip/franks/17247773"),
        ],
        run_id=1, group_id=1, keyword_id=1, item_map=item_map, brand_map=brand_map,
    )
    other = rows[0]
    assert other["position_type"] == "sponsored"
    assert other["brand_id"] == 6            # counts toward brand share of shelf
    assert other["item_id"] == "OTHERSKU01"  # but NOT the tracked numeric id


def test_name_match_does_not_override_precise_product_match():
    # A card that matches a tracked product by item id keeps that product's brand,
    # even if another brand's name also appears in the title.
    item_map = {"10294527": _product("10294527", brand_id=1,
                                     url="https://www.walmart.com/ip/10294527")}
    brand_map = {1: _brand(1, "mine", name="Tabasco"),
                 2: _brand(2, "competitor", name="Cholula")}
    rows = ci_scraper.build_result_rows(
        [_card("Tabasco vs Cholula variety", "opaque",
               product_url="https://www.walmart.com/ip/x/10294527")],
        run_id=1, group_id=1, keyword_id=1, item_map=item_map, brand_map=brand_map,
    )
    assert rows[0]["brand_id"] == 1  # product match wins over name match


def test_brand_match_by_url_fallback_when_item_id_missing():
    url = "https://www.walmart.com/ip/frank/55512340"
    item_map = {"55512340": _product("55512340", brand_id=9, url=url)}
    brand_map = {9: _brand(9, "competitor")}
    # Card has no data-item-id but the product URL matches (with query junk).
    rows = ci_scraper.build_result_rows(
        [_card("Frank's", "", product_url=url + "?from=search")],
        run_id=1, group_id=1, keyword_id=1, item_map=item_map, brand_map=brand_map,
    )
    assert rows[0]["brand_id"] == 9


def test_new_sku_flag_for_tracked_competitor_only_once():
    item_map = {"111": _product("111", brand_id=9, url="https://www.walmart.com/ip/a/111")}
    brand_map = {9: _brand(9, "competitor", tracked=1)}
    seen: dict = {}
    # First sighting flags new; a later run with the same seen-set does not.
    first = ci_scraper.build_result_rows(
        [_card("New comp", "111")], run_id=1, group_id=1, keyword_id=1,
        item_map=item_map, brand_map=brand_map, seen_ids_by_brand=seen,
    )
    assert first[0]["is_new_sku"] == 1
    second = ci_scraper.build_result_rows(
        [_card("New comp", "111")], run_id=2, group_id=1, keyword_id=1,
        item_map=item_map, brand_map=brand_map, seen_ids_by_brand=seen,
    )
    assert second[0]["is_new_sku"] == 0


def test_untracked_competitor_not_flagged():
    item_map = {"222": _product("222", brand_id=9, url="https://www.walmart.com/ip/a/222")}
    brand_map = {9: _brand(9, "competitor", tracked=0)}
    rows = ci_scraper.build_result_rows(
        [_card("Untracked", "222")], run_id=1, group_id=1, keyword_id=1,
        item_map=item_map, brand_map=brand_map, seen_ids_by_brand={},
    )
    assert rows[0]["is_new_sku"] == 0


def test_search_url_encodes_spaces():
    assert ci_scraper.search_url("hot sauce") == "https://www.walmart.com/search?q=hot+sauce"


def test_proxy_from_env_unset_is_none(monkeypatch):
    # No proxy configured -> scrape directly (inert), the default.
    monkeypatch.delenv("WALMART_PROXY_SERVER", raising=False)
    assert ci_scraper._proxy_from_env() is None
    # A blank/whitespace server is treated as unset, not a bad proxy.
    monkeypatch.setenv("WALMART_PROXY_SERVER", "   ")
    assert ci_scraper._proxy_from_env() is None


def test_proxy_from_env_server_only(monkeypatch):
    monkeypatch.setenv("WALMART_PROXY_SERVER", "http://gate.example.com:7000")
    monkeypatch.delenv("WALMART_PROXY_USERNAME", raising=False)
    assert ci_scraper._proxy_from_env() == {"server": "http://gate.example.com:7000"}


def test_proxy_from_env_with_credentials(monkeypatch):
    monkeypatch.setenv("WALMART_PROXY_SERVER", "http://gate.example.com:7000")
    monkeypatch.setenv("WALMART_PROXY_USERNAME", "user123")
    monkeypatch.setenv("WALMART_PROXY_PASSWORD", "secret")
    assert ci_scraper._proxy_from_env() == {
        "server": "http://gate.example.com:7000", "username": "user123", "password": "secret"}
