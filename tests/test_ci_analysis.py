"""Tests for the CI dashboard aggregations (share of shelf + search rank)."""

from datetime import date, timedelta

from app import ci_analysis, ci_config, ci_jobs
from app.db import get_db
from app.users import create_local_user


def _iso(days_ago: int) -> str:
    return (date.today() - timedelta(days=days_ago)).isoformat()


def _sos(db, run_id, gid, kid, d, brand_id, organic, sponsored):
    db.execute(
        "INSERT INTO ci_share_of_search "
        "(run_id, group_id, keyword_id, date, slot, brand_id, organic_count, sponsored_count, total_count) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, gid, kid, d, None, brand_id, organic, sponsored, organic + sponsored),
    )


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
    """Register ``item_id`` as a tracked product of ``brand_id``.

    Ranking now counts only tracked items, so a result row is only scored when its
    item id is a tracked product. Inserts directly (any id) rather than via
    add_product's URL parsing so tests can use short synthetic ids.
    """
    db.execute(
        "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url) "
        "VALUES (?,?,?,?,?)",
        (gid, brand_id, None, item_id, f"https://www.walmart.com/ip/x/{item_id}"),
    )


def _setup(db):
    uid = create_local_user("an@example.com", "password123")
    gid = ci_config.create_group(db, uid, "Hot Sauce")
    mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
    comp = ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
    pid = ci_config.add_product(db, gid, mine, uid, "https://www.walmart.com/ip/x/10294528")
    kid = ci_config.add_keyword(db, gid, uid, "hot sauce")
    rid = ci_jobs.enqueue_run(db, gid)
    return uid, gid, mine, comp, pid, kid, rid


def test_period_bounds_are_last_completed_calendar():
    T = date(2026, 8, 26)  # a Wednesday; this week/month/quarter/year are in progress
    assert ci_analysis.period_bounds("wow", 0, T) == ("2026-08-17", "2026-08-23")
    assert ci_analysis.period_bounds("wow", 1, T) == ("2026-08-10", "2026-08-16")
    assert ci_analysis.period_bounds("mom", 0, T) == ("2026-07-01", "2026-07-31")
    assert ci_analysis.period_bounds("qoq", 0, T) == ("2026-04-01", "2026-06-30")
    assert ci_analysis.period_bounds("yoy", 0, T) == ("2025-01-01", "2025-12-31")
    assert ci_analysis.period_label("wow", 0, T) == "Aug 17–23"
    assert ci_analysis.period_label("mom", 0, T) == "Jul 2026"
    assert ci_analysis.period_label("qoq", 0, T) == "Q2 2026"
    assert ci_analysis.period_label("yoy", 0, T) == "2025"


def test_available_periods_gate_on_completed_period_data(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        T = date(2026, 8, 26)
        # Data only in the last completed WEEK (Aug 17–23) -> only week is offered.
        _sos(db, rid, gid, kid, "2026-08-20", mine, 5, 0)
        db.commit()
        assert ci_analysis.available_periods(db, gid, today=T) == ["wow"]
        # Add data inside last completed month (July) -> month switches on too.
        _sos(db, rid, gid, kid, "2026-07-15", mine, 5, 0)
        db.commit()
        avail = ci_analysis.available_periods(db, gid, today=T)
        assert "wow" in avail and "mom" in avail
        assert "qoq" not in avail and "yoy" not in avail


def test_monitoring_avg_rank_period_delta_and_trend(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)  # mine tracks 10294528
        item = "10294528"
        T = date(2026, 8, 26)
        # Last completed week (Aug 17–23): #5 and #3 -> avg 4.0.
        _result(db, rid, gid, kid, "2026-08-18", 5, item, mine)
        _result(db, rid, gid, kid, "2026-08-20", 3, item, mine)
        # Prior week (Aug 10–16): #8 -> avg 8.0.
        _result(db, rid, gid, kid, "2026-08-12", 8, item, mine)
        db.commit()

        rows = ci_analysis.monitoring_avg_rank(db, gid, "wow", today=T)
        r = next(r for r in rows if r["brand_name"] == "Tabasco")
        assert r["avg_position"] == 4.0
        assert r["delta"] == 4.0  # prior 8.0 - current 4.0 = improved 4 slots
        # Trend is one point per completed week, oldest -> newest, labelled.
        assert r["trend"] == [8.0, 4.0]
        assert r["trend_dates"] == ["Aug 10–16", "Aug 17–23"]


def test_monitoring_share_of_shelf_period_delta_and_trend(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        T = date(2026, 8, 26)
        # Last completed week: mine 7 of 10 = 70%.
        _sos(db, rid, gid, kid, "2026-08-20", mine, 7, 0)
        _sos(db, rid, gid, kid, "2026-08-20", comp, 3, 0)
        # Prior week: mine 4 of 10 = 40%.
        _sos(db, rid, gid, kid, "2026-08-12", mine, 4, 0)
        _sos(db, rid, gid, kid, "2026-08-12", comp, 6, 0)
        db.commit()

        rows = ci_analysis.monitoring_share_of_shelf(db, gid, "wow", today=T)
        by = {r["brand_name"]: r for r in rows}
        assert by["Tabasco"]["total_share"] == 70.0
        assert by["Tabasco"]["delta"] == 30.0  # 70 - 40 percentage points gained
        assert by["Tabasco"]["trend"] == [40.0, 70.0]
        assert by["Tabasco"]["trend_dates"] == ["Aug 10–16", "Aug 17–23"]
        # Ordering: mine first, then competitor.
        assert [r["type"] for r in rows] == ["mine", "competitor"]


def test_snapshot_brand_share_of_shelf_counts_all_skus(app):
    """Brand share counts a brand's UNTRACKED SKUs too (unlike tracked-only share),
    reading ci_search_results directly; unattributed cards form the 'Other' bucket."""
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)  # mine tracks 10294528
        # Tabasco: its tracked item PLUS an untracked SKU (both brand-attributed).
        _result(db, rid, gid, kid, _iso(0), 1, "10294528", mine, ptype="organic")
        _result(db, rid, gid, kid, _iso(0), 2, "t2", mine, ptype="organic")
        # Frank's: one sponsored SKU. Plus a card tied to no tracked brand -> Other.
        _result(db, rid, gid, kid, _iso(0), 3, "f1", comp, ptype="sponsored")
        _result(db, rid, gid, kid, _iso(0), 4, "o1", None, ptype="organic")
        db.commit()

        rows = ci_analysis.snapshot_brand_share_of_shelf(db, gid, rid)
        by = {r["brand_name"]: r for r in rows}
        # 4 placements total; Tabasco 2 (incl. the untracked SKU) = 50%.
        assert by["Tabasco"]["total"] == 2
        assert by["Tabasco"]["total_share"] == 50.0
        # Organic denominator is 3 (2 Tabasco + 1 Other); Tabasco organic share = 66.7%.
        assert by["Tabasco"]["organic_share"] == 66.7
        assert by["Frank's"]["total_share"] == 25.0
        assert by["Frank's"]["sponsored_share"] == 100.0  # the only sponsored card
        assert by["Other"]["total_share"] == 25.0
        # Ordering: mine, competitor, then the Other bucket.
        assert [r["type"] for r in rows] == ["mine", "competitor", "other"]


def test_monitoring_brand_share_of_shelf_period_delta_and_trend(app):
    """Brand-level SoS aggregates ci_search_results over the completed period."""
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        T = date(2026, 8, 26)

        def bulk(d, brand_id, n, start_pos):
            for i in range(n):
                _result(db, rid, gid, kid, d, start_pos + i, f"{brand_id}-{i}", brand_id)

        # Last completed week (Aug 17–23): Tabasco 7 of 10 = 70%.
        bulk("2026-08-20", mine, 7, 1)
        bulk("2026-08-20", comp, 3, 20)
        # Prior week (Aug 10–16): Tabasco 4 of 10 = 40%.
        bulk("2026-08-12", mine, 4, 1)
        bulk("2026-08-12", comp, 6, 20)
        db.commit()

        rows = ci_analysis.monitoring_brand_share_of_shelf(db, gid, "wow", today=T)
        by = {r["brand_name"]: r for r in rows}
        assert by["Tabasco"]["total_share"] == 70.0
        assert by["Tabasco"]["delta"] == 30.0  # 70 - 40 percentage points gained
        assert by["Tabasco"]["trend"] == [40.0, 70.0]
        assert by["Tabasco"]["trend_dates"] == ["Aug 10–16", "Aug 17–23"]
        assert [r["type"] for r in rows] == ["mine", "competitor"]


def test_snapshot_brand_ads_counts_and_latest_image(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        # Tabasco: 2 headline sightings (2nd carries the image) + 1 video.
        _ad(db, rid, gid, kid, _iso(0), "headline", mine, image_path=None)
        _ad(db, rid, gid, kid, _iso(0), "headline", mine, image_path="ci_ads/x.jpg")
        _ad(db, rid, gid, kid, _iso(0), "video", mine, image_path="ci_ads/v.jpg")
        # Frank's: 1 headline. An untracked-brand ad (brand_id NULL) is ignored here.
        _ad(db, rid, gid, kid, _iso(0), "headline", comp, image_path=None)
        _ad(db, rid, gid, kid, _iso(0), "headline", None, image_path=None)
        db.commit()

        rows = ci_analysis.snapshot_brand_ads(db, gid, rid)
        by = {r["brand_name"]: r for r in rows}
        assert by["Tabasco"]["headline_count"] == 2
        assert by["Tabasco"]["video_count"] == 1
        # Latest image ref is the run/keyword that has an image.
        assert by["Tabasco"]["headline_img"] == {"run_id": rid, "keyword_id": kid}
        assert by["Frank's"]["headline_count"] == 1
        assert by["Frank's"]["video_count"] == 0 and by["Frank's"]["video_img"] is None
        # Only tracked brands surface; mine sorts before competitor.
        assert [r["type"] for r in rows] == ["mine", "competitor"]


def test_monitoring_brand_ads_counts_over_period(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        T = date(2026, 8, 26)
        # Two headline sightings in the last completed week; one in a prior week (excluded).
        _ad(db, rid, gid, kid, "2026-08-18", "headline", mine)
        _ad(db, rid, gid, kid, "2026-08-20", "headline", mine)
        _ad(db, rid, gid, kid, "2026-08-12", "headline", mine)  # prior week, not counted
        db.commit()

        rows = ci_analysis.monitoring_brand_ads(db, gid, "wow", today=T)
        by = {r["brand_name"]: r for r in rows}
        assert by["Tabasco"]["headline_count"] == 2  # only the completed-week sightings


def test_snapshot_rank_splits_organic_and_sponsored(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, _comp, _, kid, rid = _setup(db)
        _result(db, rid, gid, kid, _iso(0), 1, "x", mine, ptype="sponsored")
        _result(db, rid, gid, kid, _iso(0), 6, "10294527", mine, ptype="organic")
        db.commit()
        rows = ci_analysis.snapshot_rank(db, gid, rid)
        r = next(r for r in rows if r["brand_name"] == "Tabasco")
        assert r["current_position"] == 1        # best overall (the sponsored #1)
        assert r["organic_position"] == 6
        assert r["sponsored_position"] == 1


def test_snapshot_brand_avg_rank_averages_per_keyword_then_across_terms(app):
    with app.app_context():
        db = get_db()
        uid, gid, mine, comp, _, kid, rid = _setup(db)
        # Second keyword to exercise the two-stage average (per-keyword, then terms).
        kid2 = ci_config.add_keyword(db, gid, uid, "chipotle sauce")
        # Tabasco tracks x/y/z; Frank's tracks f (only tracked items are averaged).
        _track(db, gid, mine, "x")
        _track(db, gid, mine, "y")
        _track(db, gid, mine, "z")
        _track(db, gid, comp, "f")
        # Tabasco kw1: #4 & #6 -> avg 5.0; kw2: #1 -> avg 1.0. Overall (5+1)/2 = 3.0.
        _result(db, rid, gid, kid, _iso(0), 4, "x", mine)
        _result(db, rid, gid, kid, _iso(0), 6, "y", mine)
        _result(db, rid, gid, kid2, _iso(0), 1, "z", mine)
        # Frank's kw1: #6 -> avg 6.0 over one term. Absent from kw2 (not penalized).
        _result(db, rid, gid, kid, _iso(0), 6, "f", comp)
        db.commit()

        rows = ci_analysis.snapshot_brand_avg_rank(db, gid, rid)
        by_brand = {r["brand_name"]: r for r in rows}
        assert by_brand["Tabasco"]["avg_position"] == 3.0
        assert by_brand["Frank's"]["avg_position"] == 6.0
        # keyword_count column was removed from the payload.
        assert "keyword_count" not in by_brand["Tabasco"]
        # Mine sorts first, then best (lowest) average first.
        assert rows[0]["type"] == "mine"


def test_snapshot_share_by_keyword_uses_per_keyword_denominators(app):
    with app.app_context():
        db = get_db()
        uid, gid, mine, comp, _, kid, rid = _setup(db)  # kid == "hot sauce"
        kid2 = ci_config.add_keyword(db, gid, uid, "aaa sauce")
        # hot sauce: Tabasco 9 organic + 6 sponsored (15); Frank's 3 organic + 2 sponsored (5).
        _sos(db, rid, gid, kid, _iso(0), mine, 9, 6)
        _sos(db, rid, gid, kid, _iso(0), comp, 3, 2)
        # aaa sauce: Tabasco only, 3 organic.
        _sos(db, rid, gid, kid2, _iso(0), mine, 3, 0)
        db.commit()

        rows = ci_analysis.snapshot_share_by_keyword(db, gid, rid)
        # Ordered by keyword (aaa before hot).
        assert rows[0]["keyword"] == "aaa sauce"
        assert rows[0]["total_share"] == 100.0 and rows[0]["organic_share"] == 100.0

        hot = [r for r in rows if r["keyword"] == "hot sauce"]
        by = {r["brand_name"]: r for r in hot}
        # Shares use this keyword's own slots: Tabasco 15 of 20 total = 75%.
        assert by["Tabasco"]["total_share"] == 75.0
        assert by["Tabasco"]["organic_share"] == 75.0    # 9 of 12 organic slots
        assert by["Tabasco"]["sponsored_share"] == 75.0  # 6 of 8 sponsored slots
        # Within a keyword, ordered by total share descending (Tabasco 75 > Frank's 25).
        assert [r["brand_name"] for r in hot] == ["Tabasco", "Frank's"]


def test_snapshot_rank_by_keyword_brand_orders_and_averages(app):
    with app.app_context():
        db = get_db()
        uid, gid, mine, comp, _, kid, rid = _setup(db)  # kid == "hot sauce"
        kid2 = ci_config.add_keyword(db, gid, uid, "aaa sauce")
        # Tabasco tracks a/b; Frank's tracks c/d (only tracked items are averaged).
        _track(db, gid, mine, "a")
        _track(db, gid, mine, "b")
        _track(db, gid, comp, "c")
        _track(db, gid, comp, "d")
        # hot sauce: Tabasco #2 & #4 -> avg 3.0; Frank's #5 -> 5.0.
        _result(db, rid, gid, kid, _iso(0), 2, "a", mine)
        _result(db, rid, gid, kid, _iso(0), 4, "b", mine)
        _result(db, rid, gid, kid, _iso(0), 5, "c", comp)
        # An untracked Frank's SKU at #1 must not pull the average down to 3.0.
        _result(db, rid, gid, kid, _iso(0), 1, "zz", comp)
        # aaa sauce: Frank's #1 -> 1.0.
        _result(db, rid, gid, kid2, _iso(0), 1, "d", comp)
        db.commit()

        rows = ci_analysis.snapshot_rank_by_keyword_brand(db, gid, rid)
        # Ordered by keyword (aaa before hot), then avg ranking ascending within a term.
        assert [(r["keyword"], r["brand_name"], r["avg_ranking"]) for r in rows] == [
            ("aaa sauce", "Frank's", 1.0),
            ("hot sauce", "Tabasco", 3.0),
            ("hot sauce", "Frank's", 5.0),
        ]
        assert rows[1]["type"] == "mine"


def test_build_rank_placement_map_rounds_ties_and_sizes_grid():
    # Pure transform — no DB. Half-up rounding, ties share a tile, grid rounds up.
    avg = [
        {"brand_name": "Tabasco", "type": "mine", "avg_position": 6.4},
        {"brand_name": "Frank's", "type": "competitor", "avg_position": 7.9},
        {"brand_name": "Louisiana", "type": "competitor", "avg_position": 8.1},
        {"brand_name": "Cholula", "type": "competitor", "avg_position": 11.5},
    ]
    m = ci_analysis.build_rank_placement_map(avg, depth=13)
    # depth 13 -> next full row of 4 = 16.
    assert m["total"] == 16 and m["rows"] == 4 and m["cols"] == 4
    # 6.4 -> #6; 7.9 and 8.1 both -> #8 (tie); 11.5 -> #12 (half-up).
    assert sorted(m["marks"]) == [6, 8, 12]
    assert [b["brand_name"] for b in m["marks"][8]] == ["Frank's", "Louisiana"]
    assert m["marks"][8][0]["avg_position"] == 7.9   # exact average preserved
    assert m["marks"][6][0]["type"] == "mine"


def test_build_rank_placement_map_grows_to_fit_deepest_brand():
    # A brand deeper than the run's captured depth still gets a real tile.
    avg = [{"brand_name": "X", "type": "competitor", "avg_position": 22.0}]
    m = ci_analysis.build_rank_placement_map(avg, depth=4)
    assert m["total"] == 24 and 22 in m["marks"]


def test_build_rank_placement_map_none_when_no_averages():
    assert ci_analysis.build_rank_placement_map([], depth=10) is None
    only_null = [{"brand_name": "X", "type": "mine", "avg_position": None}]
    assert ci_analysis.build_rank_placement_map(only_null, depth=10) is None


def test_snapshot_page1_depth_returns_deepest_slot(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        _result(db, rid, gid, kid, _iso(0), 3, "a", mine)
        _result(db, rid, gid, kid, _iso(0), 17, "b", comp)
        db.commit()
        assert ci_analysis.snapshot_page1_depth(db, gid, rid) == 17
        # A run with nothing recorded reports depth 0.
        empty_rid = ci_jobs.enqueue_run(db, gid)
        assert ci_analysis.snapshot_page1_depth(db, gid, empty_rid) == 0


def test_monitoring_avg_rank_empty_when_no_period_data(app):
    with app.app_context():
        db = get_db()
        _, gid, *_ = _setup(db)
        assert ci_analysis.monitoring_avg_rank(db, gid, "wow", today=date(2026, 8, 26)) == []
