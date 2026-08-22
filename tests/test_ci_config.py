"""Tests for Competitive Intelligence config CRUD (groups/brands/products/keywords).

Focus areas: happy-path CRUD, input validation, per-user ownership (IDOR), and
the cross-entity integrity checks (brand-in-group, keyword de-dupe).
"""

import pytest

from app import ci_config
from app.ci_config import ConfigError
from app.db import get_db
from app.users import create_local_user


def test_group_crud_and_child_counts(app):
    with app.app_context():
        uid = create_local_user("owner@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "Hot Sauce", "My hot sauce line")
        assert gid > 0

        b_mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
        ci_config.add_product(db, gid, b_mine, uid,
                              "https://www.walmart.com/ip/tabasco/10294528")
        ci_config.add_keyword(db, gid, uid, "hot sauce")

        groups = ci_config.list_groups(db, uid)
        assert len(groups) == 1
        g = groups[0]
        assert g["name"] == "Hot Sauce"
        assert g["brand_count"] == 2
        assert g["product_count"] == 1
        assert g["keyword_count"] == 1


def test_add_product_validates_url_and_derives_item_id(app):
    with app.app_context():
        uid = create_local_user("p@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "G")
        bid = ci_config.add_brand(db, gid, uid, "Brand", "competitor")

        pid = ci_config.add_product(db, gid, bid, uid,
                                    "https://www.walmart.com/ip/some-slug/55512340")
        prod = ci_config.list_products(db, gid, uid)[0]
        assert prod["id"] == pid
        assert prod["walmart_item_id"] == "55512340"
        assert prod["brand_name"] == "Brand"

        # Bad scheme / no item number are rejected.
        with pytest.raises(ConfigError):
            ci_config.add_product(db, gid, bid, uid, "javascript:alert(1)")
        with pytest.raises(ConfigError):
            ci_config.add_product(db, gid, bid, uid, "https://www.walmart.com/ip/no-number")


def test_brand_must_belong_to_group_for_product(app):
    with app.app_context():
        uid = create_local_user("x@example.com", "password123")
        db = get_db()
        g1 = ci_config.create_group(db, uid, "G1")
        g2 = ci_config.create_group(db, uid, "G2")
        b2 = ci_config.add_brand(db, g2, uid, "B2", "competitor")
        # Brand from g2 can't be used to add a product to g1.
        with pytest.raises(ConfigError):
            ci_config.add_product(db, g1, b2, uid,
                                  "https://www.walmart.com/ip/x/10294528")


def test_keyword_dedupe_case_insensitive(app):
    with app.app_context():
        uid = create_local_user("k@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "G")
        ci_config.add_keyword(db, gid, uid, "Hot Sauce")
        with pytest.raises(ConfigError):
            ci_config.add_keyword(db, gid, uid, "hot sauce")
        assert len(ci_config.list_keywords(db, gid, uid)) == 1


def test_ownership_blocks_cross_user_access(app):
    with app.app_context():
        owner = create_local_user("owner2@example.com", "password123")
        intruder = create_local_user("intruder@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, owner, "Private")
        bid = ci_config.add_brand(db, gid, owner, "B", "mine")
        kid = ci_config.add_keyword(db, gid, owner, "kw")

        # Reads scoped to the intruder see nothing / raise.
        assert ci_config.get_group(db, gid, intruder) is None
        assert ci_config.list_groups(db, intruder) == []
        with pytest.raises(ConfigError):
            ci_config.list_brands(db, gid, intruder)
        # Mutations by the intruder are refused.
        with pytest.raises(ConfigError):
            ci_config.add_keyword(db, gid, intruder, "sneaky")
        with pytest.raises(ConfigError):
            ci_config.delete_brand(db, bid, intruder)
        with pytest.raises(ConfigError):
            ci_config.delete_keyword(db, kid, intruder)
        with pytest.raises(ConfigError):
            ci_config.delete_group(db, gid, intruder)
        # The owner's data is intact after the failed intrusion attempts.
        assert ci_config.get_group(db, gid, owner) is not None


def test_monitoring_toggle_and_groups_with_monitoring(app):
    with app.app_context():
        uid = create_local_user("m@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "G", mode="monitoring")
        assert ci_config.groups_with_monitoring(db) == []
        ci_config.set_monitoring(db, gid, uid, True)
        mon = ci_config.groups_with_monitoring(db)
        assert [g["id"] for g in mon] == [gid]
        ci_config.set_monitoring(db, gid, uid, False)
        assert ci_config.groups_with_monitoring(db) == []


def test_snapshot_group_never_swept_even_if_monitoring_flag_set(app):
    # groups_with_monitoring is mode-scoped: a snapshot group is never returned.
    with app.app_context():
        uid = create_local_user("s2@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "Snap", mode="snapshot")
        ci_config.set_monitoring(db, gid, uid, True)
        assert ci_config.groups_with_monitoring(db) == []


def test_list_groups_filters_by_mode(app):
    with app.app_context():
        uid = create_local_user("mode@example.com", "password123")
        db = get_db()
        s = ci_config.create_group(db, uid, "Snap", mode="snapshot")
        m = ci_config.create_group(db, uid, "Mon", mode="monitoring")
        assert [g["id"] for g in ci_config.list_groups(db, uid, mode="snapshot")] == [s]
        assert [g["id"] for g in ci_config.list_groups(db, uid, mode="monitoring")] == [m]
        assert {g["id"] for g in ci_config.list_groups(db, uid)} == {s, m}


def test_delete_group_cascades(app):
    with app.app_context():
        uid = create_local_user("d@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "G")
        bid = ci_config.add_brand(db, gid, uid, "B", "mine")
        ci_config.add_product(db, gid, bid, uid,
                              "https://www.walmart.com/ip/x/10294528")
        ci_config.add_keyword(db, gid, uid, "kw")
        ci_config.delete_group(db, gid, uid)
        assert ci_config.list_groups(db, uid) == []
        # Children gone via ON DELETE CASCADE.
        assert db.execute("SELECT COUNT(*) FROM ci_products").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM ci_keywords").fetchone()[0] == 0


def test_load_group_config_shapes(app):
    with app.app_context():
        uid = create_local_user("c2@example.com", "password123")
        db = get_db()
        gid = ci_config.create_group(db, uid, "G")
        bid = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        ci_config.add_product(db, gid, bid, uid,
                              "https://www.walmart.com/ip/x/10294528")
        ci_config.add_keyword(db, gid, uid, "hot sauce")

        keywords, item_map, brand_map = ci_config.load_group_config(db, gid)
        assert [k["keyword"] for k in keywords] == ["hot sauce"]
        assert "10294528" in item_map
        assert item_map["10294528"]["brand_id"] == bid
        assert brand_map[bid]["type"] == "mine"
