"""Tests for per-item tracking keywords (app.item_keywords).

Covers default generation (brand / brand+product+size / category), the
user-edited protection the descriptors backfill must respect, the edit API with
its length/blank validation, and the user-scoped IDOR gate.
"""

from app import item_keywords, portfolio
from app.db import get_db
from app.users import create_local_user


def _add(db, uid, brand="Tabasco", url="https://www.walmart.com/ip/10294528"):
    portfolio.add_item(db, uid, brand, url, None)
    return db.execute(
        "SELECT id FROM portfolio_items WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (uid,)).fetchone()["id"]


def _kw(db, item_id):
    return {r["slot"]: r["keyword"] for r in db.execute(
        "SELECT slot, keyword FROM portfolio_keywords WHERE portfolio_item_id = ?",
        (item_id,))}


def test_compose_avoids_duplicate_brand_and_appends_size():
    # Title already leads with the brand -> brand not doubled; size appended.
    assert item_keywords._compose_product_keyword(
        "Tabasco", "Tabasco Original Hot Sauce", "12 oz") == "Tabasco Original Hot Sauce 12 oz"
    # Title lacks the brand -> brand prepended.
    assert item_keywords._compose_product_keyword(
        "Tabasco", "Original Hot Sauce", "12 oz") == "Tabasco Original Hot Sauce 12 oz"
    # Size already in the title -> not repeated.
    assert item_keywords._compose_product_keyword(
        "Tabasco", "Tabasco Hot Sauce 12 oz", "12 oz") == "Tabasco Hot Sauce 12 oz"
    # Nothing usable -> None.
    assert item_keywords._compose_product_keyword(None, None, None) is None


def test_generate_defaults_seeds_three_slots(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("g@example.com", "password123")
        item_id = _add(db, uid)  # add_item already seeds; assert the shape
        rows = item_keywords.list_keywords(db, uid, item_id)
        assert [r["slot"] for r in rows] == [1, 2, 3]
        assert [r["kind"] for r in rows] == ["brand", "brand_product_size", "category"]
        assert rows[0]["keyword"] == "Tabasco"


def test_apply_descriptors_fills_kw2_kw3_but_not_user_edited(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("d@example.com", "password123")
        item_id = _add(db, uid)

        # User edits KW3 before the backfill runs.
        assert item_keywords.set_keywords(db, uid, item_id, {3: "Pepper Sauce"}) is True

        item_keywords.apply_descriptors(
            db, item_id, brand="Tabasco", title="Original Hot Sauce",
            size="12 oz", category="Hot Sauce")
        kws = _kw(db, item_id)
        assert kws[2] == "Tabasco Original Hot Sauce 12 oz"  # refined
        assert kws[3] == "Pepper Sauce"                      # user edit preserved


def test_set_keywords_validates_and_flags_edited(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("v@example.com", "password123")
        item_id = _add(db, uid)

        long = "x" * 200
        assert item_keywords.set_keywords(db, uid, item_id, {1: long, 2: "  spaced  ", 3: ""}) is True
        rows = {r["slot"]: r for r in item_keywords.list_keywords(db, uid, item_id)}
        assert len(rows[1]["keyword"]) == item_keywords.MAX_KEYWORD_LEN  # capped
        assert rows[2]["keyword"] == "spaced"                            # trimmed
        assert rows[3]["keyword"] is None                                # blank -> NULL
        assert all(rows[s]["user_edited"] == 1 for s in (1, 2, 3))


def test_set_keywords_is_user_scoped(app):
    with app.app_context():
        db = get_db()
        owner = create_local_user("owner@example.com", "password123")
        other = create_local_user("other@example.com", "password123")
        item_id = _add(db, owner)
        # A foreign user can't edit the owner's keywords.
        assert item_keywords.set_keywords(db, other, item_id, {1: "Hijack"}) is False
        assert _kw(db, item_id)[1] == "Tabasco"  # unchanged
