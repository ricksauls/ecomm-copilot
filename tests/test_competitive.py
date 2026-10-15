"""Tests for the Competitive Intelligence Insights screen.

Covers the new analytics (paid-vs-organic mix, branded-keyword detection, branded-
keyword ad exposure, aligned multi-brand trend) and the route/tabs. Reuses the raw
seeding style of ``test_ci_analysis`` so results/share rows can be placed on exact
dates and a fixed ``today`` drives the period math.
"""

from datetime import date

from app import ci_analysis, ci_config, ci_jobs
from app.db import get_db
from app.users import create_local_user

T = date(2026, 8, 26)  # a Wednesday; last completed week is Aug 17–23, prior Aug 10–16
_WOW0 = "2026-08-20"   # inside the last completed week
_WOW1 = "2026-08-13"   # inside the week before


def _result(db, run_id, gid, kid, d, position, item_id, brand_id, ptype="organic"):
    db.execute(
        "INSERT INTO ci_search_results "
        "(run_id, group_id, keyword_id, scraped_at, position, position_type, item_id, brand_id, is_new_sku) "
        "VALUES (?,?,?,?,?,?,?,?,0)",
        (run_id, gid, kid, d, position, ptype, item_id, brand_id),
    )


def _ad(db, run_id, gid, kid, d, ad_type, brand_id, image_path=None):
    db.execute(
        "INSERT INTO ci_ad_units "
        "(run_id, group_id, keyword_id, scraped_at, ad_type, brand_id, brand_text, image_path) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (run_id, gid, kid, d, ad_type, brand_id, None, image_path),
    )


def _track(db, gid, brand_id, item_id):
    db.execute(
        "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url) "
        "VALUES (?,?,?,?,?)",
        (gid, brand_id, None, item_id, f"https://www.walmart.com/ip/x/{item_id}"),
    )


def _setup(db, email="ci@example.com"):
    """A monitoring group with one mine brand + two competitors and four keywords."""
    uid = create_local_user(email, "password123")
    gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
    brands = {
        "mine": ci_config.add_brand(db, gid, uid, "Tabasco", "mine"),
        "frank": ci_config.add_brand(db, gid, uid, "Frank's", "competitor"),
        "chol": ci_config.add_brand(db, gid, uid, "Cholula", "competitor"),
    }
    kw = {
        "hot": ci_config.add_keyword(db, gid, uid, "hot sauce"),
        "sauce": ci_config.add_keyword(db, gid, uid, "tabasco sauce"),
        "chip": ci_config.add_keyword(db, gid, uid, "tabasco chipotle"),
        "orig": ci_config.add_keyword(db, gid, uid, "tabasco original"),
    }
    _track(db, gid, brands["mine"], "T1")
    _track(db, gid, brands["frank"], "F1")
    _track(db, gid, brands["chol"], "C1")
    rid = ci_jobs.enqueue_run(db, gid)
    return uid, gid, brands, kw, rid


# ── Analytics ────────────────────────────────────────────────────────────────────

def test_branded_keywords_match_mine_brand_name(app):
    with app.app_context():
        db = get_db()
        _, gid, _, _, _ = _setup(db)
        db.commit()
        got = {k["keyword"] for k in ci_analysis.branded_keywords(db, gid)}
        # Every term containing "tabasco" is branded; the generic term is not.
        assert got == {"tabasco sauce", "tabasco chipotle", "tabasco original"}


def test_competitive_paid_organic_mix(app):
    with app.app_context():
        db = get_db()
        _, gid, b, kw, rid = _setup(db)
        # hot sauce: Tabasco organic, Frank's organic + sponsored, Cholula organic.
        _result(db, rid, gid, kw["hot"], _WOW0, 3, "T1", b["mine"])
        _result(db, rid, gid, kw["hot"], _WOW0, 1, "F1", b["frank"])
        _result(db, rid, gid, kw["hot"], _WOW0, 5, "F1", b["frank"], ptype="sponsored")
        _result(db, rid, gid, kw["hot"], _WOW0, 2, "C1", b["chol"])
        db.commit()

        rows = ci_analysis.competitive_paid_organic(db, gid, "wow", today=T)
        by_name = {r["brand_name"]: r for r in rows}
        # Mine brand is always first; competitors follow by total presence descending.
        assert rows[0]["brand_name"] == "Tabasco"
        assert [r["brand_name"] for r in rows] == ["Tabasco", "Frank's", "Cholula"]
        assert by_name["Tabasco"]["organic_pct"] == 100.0
        assert by_name["Tabasco"]["sponsored_pct"] == 0.0
        assert by_name["Frank's"]["organic_pct"] == 50.0
        assert by_name["Frank's"]["sponsored_pct"] == 50.0


def test_branded_keyword_ads_exposure_and_order(app):
    with app.app_context():
        db = get_db()
        _, gid, b, kw, rid = _setup(db)
        # Defended: Frank's advertises on "tabasco sauce" but Tabasco holds a slot too.
        _result(db, rid, gid, kw["sauce"], _WOW0, 2, "F1", b["frank"], ptype="sponsored")
        _result(db, rid, gid, kw["sauce"], _WOW0, 1, "T1", b["mine"])
        # Clear: only Tabasco is on "tabasco chipotle".
        _result(db, rid, gid, kw["chip"], _WOW0, 1, "T1", b["mine"])
        # Exposed: Cholula advertises on "tabasco original" and Tabasco is absent.
        _result(db, rid, gid, kw["orig"], _WOW0, 1, "C1", b["chol"], ptype="sponsored")
        # A non-branded term with a sponsored competitor must NOT appear.
        _result(db, rid, gid, kw["hot"], _WOW0, 2, "F1", b["frank"], ptype="sponsored")
        db.commit()

        rows = ci_analysis.competitive_branded_keyword_ads(db, gid, "wow", today=T)
        assert [r["keyword"] for r in rows] == [
            "tabasco original", "tabasco sauce", "tabasco chipotle"]  # exposed, defended, clear
        by_kw = {r["keyword"]: r for r in rows}
        assert by_kw["tabasco original"]["status"] == "exposed"
        assert by_kw["tabasco original"]["you_present"] is False
        assert [c["name"] for c in by_kw["tabasco original"]["competitors"]] == ["Cholula"]
        assert by_kw["tabasco sauce"]["status"] == "defended"
        assert by_kw["tabasco sauce"]["you_present"] is True
        assert by_kw["tabasco chipotle"]["status"] == "clear"
        assert by_kw["tabasco chipotle"]["competitors"] == []
        assert "hot sauce" not in by_kw


def test_competitive_rank_trend_aligns_series_with_gaps(app):
    with app.app_context():
        db = get_db()
        _, gid, b, kw, rid = _setup(db)
        # Both weeks: Tabasco + Frank's present; Cholula only in the latest week.
        _result(db, rid, gid, kw["hot"], _WOW1, 3, "T1", b["mine"])
        _result(db, rid, gid, kw["hot"], _WOW1, 1, "F1", b["frank"])
        _result(db, rid, gid, kw["hot"], _WOW0, 3, "T1", b["mine"])
        _result(db, rid, gid, kw["hot"], _WOW0, 1, "F1", b["frank"])
        _result(db, rid, gid, kw["hot"], _WOW0, 2, "C1", b["chol"])
        db.commit()

        trend = ci_analysis.competitive_rank_trend(
            db, gid, "wow", ["Tabasco", "Frank's", "Cholula"], today=T)
        assert len(trend["labels"]) == 2  # two completed weeks with data
        # Cholula had no slot in the earlier week → a None gap, then its real value.
        assert trend["series"]["Cholula"] == [None, 2.0]
        assert trend["series"]["Frank's"] == [1.0, 1.0]


def test_branded_keyword_ads_empty_without_mine_brand(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("nomine@example.com", "password123")
        gid = ci_config.create_group(db, uid, "No Mine", mode="monitoring")
        ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
        ci_config.add_keyword(db, gid, uid, "hot sauce")
        db.commit()
        assert ci_analysis.branded_keywords(db, gid) == []
        assert ci_analysis.competitive_branded_keyword_ads(db, gid, "wow", today=T) == []


# ── Route / tabs ───────────────────────────────────────────────────────────────

_URL = "/app/insights/competitive"


def _seed_live_group(app, email):
    """A monitoring group with data in the *real* last completed week, so the route
    (which uses the real today) sees an enabled period."""
    d = ci_analysis.period_bounds("wow", 0)[0]  # first day of the last completed week
    with app.app_context():
        db = get_db()
        uid = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()[0]
        gid = ci_config.create_group(db, uid, "Hot Sauce", mode="monitoring")
        mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
        frank = ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
        k_hot = ci_config.add_keyword(db, gid, uid, "hot sauce")
        k_sauce = ci_config.add_keyword(db, gid, uid, "tabasco sauce")
        _track(db, gid, mine, "T1")
        _track(db, gid, frank, "F1")
        rid = ci_jobs.enqueue_run(db, gid)
        _result(db, rid, gid, k_hot, d, 2, "T1", mine)
        _result(db, rid, gid, k_hot, d, 1, "F1", frank)
        _result(db, rid, gid, k_hot, d, 4, "F1", frank, ptype="sponsored")
        _result(db, rid, gid, k_sauce, d, 2, "F1", frank, ptype="sponsored")
        _result(db, rid, gid, k_sauce, d, 1, "T1", mine)
        db.commit()
        return gid


def test_competitive_requires_login(client):
    resp = client.get(_URL)
    assert resp.status_code in (301, 302)
    assert "/signin" in resp.headers["Location"]


def test_competitive_prompts_setup_without_monitoring_group(client, auth):
    auth.register(email="empty@example.com")
    auth.login(email="empty@example.com")
    resp = client.get(_URL)
    assert resp.status_code == 200
    assert b"No tracked brand yet" in resp.data


def test_competitive_rank_tab_renders_leaderboard_and_nav(client, auth, app):
    auth.register(email="rank@example.com")
    auth.login(email="rank@example.com")
    _seed_live_group(app, "rank@example.com")
    resp = client.get(_URL)
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Competitive Intelligence" in body       # rail leaf + page title
    assert "Top competitors by search ranking" in body
    assert "Frank" in body                           # the competitor leaderboard row (name is HTML-escaped)
    assert "competitive.js" in body                  # the trend/controls script is wired


def test_competitive_paid_tab_renders(client, auth, app):
    auth.register(email="paid@example.com")
    auth.login(email="paid@example.com")
    _seed_live_group(app, "paid@example.com")
    resp = client.get(_URL + "?tab=paid")
    assert resp.status_code == 200
    assert b"Organic vs. paid, by brand" in resp.data


def test_competitive_ads_tab_shows_branded_table_and_dormant_creatives(client, auth, app):
    auth.register(email="ads@example.com")
    auth.login(email="ads@example.com")
    _seed_live_group(app, "ads@example.com")
    resp = client.get(_URL + "?tab=ads")
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "Competitors on your branded keywords" in body
    assert "tabasco sauce" in body
    # ci_ad_units has no rows (needs the residential proxy), so the dormant box shows.
    assert "Activates with residential-proxy monitoring" in body
