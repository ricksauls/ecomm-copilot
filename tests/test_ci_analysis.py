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


def _setup(db):
    uid = create_local_user("an@example.com", "password123")
    gid = ci_config.create_group(db, uid, "Hot Sauce")
    mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
    comp = ci_config.add_brand(db, gid, uid, "Frank's", "competitor")
    pid = ci_config.add_product(db, gid, mine, uid, "https://www.walmart.com/ip/x/10294528")
    kid = ci_config.add_keyword(db, gid, uid, "hot sauce")
    rid = ci_jobs.enqueue_run(db, gid)
    return uid, gid, mine, comp, pid, kid, rid


def test_share_of_shelf_summary_shares_ordering_and_delta(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        # Current window (today): mine 6 slots (4 org + 2 spon), comp 3 org, other 1 org => 10 total.
        _sos(db, rid, gid, kid, _iso(0), mine, 4, 2)
        _sos(db, rid, gid, kid, _iso(0), comp, 3, 0)
        _sos(db, rid, gid, kid, _iso(0), None, 1, 0)
        # Prior window (~10 days ago, inside wow's prior 7-day window): mine 2 of 10.
        _sos(db, rid, gid, kid, _iso(10), mine, 2, 0)
        _sos(db, rid, gid, kid, _iso(10), None, 8, 0)
        db.commit()

        rows = ci_analysis.share_of_shelf_summary(db, gid, "wow")
        by_id = {r["brand_id"]: r for r in rows}
        assert by_id[mine]["total_share"] == 60.0   # 6/10
        assert by_id[mine]["organic_share"] == 50.0  # 4 of 8 organic slots
        assert by_id[comp]["total_share"] == 30.0
        assert by_id[None]["brand_name"] == "Other"
        # Delta: mine was 20% prior -> +40.0
        assert by_id[mine]["total_share_delta"] == 40.0
        # Ordering: mine first, then competitor, then Other last.
        assert [r["type"] for r in rows] == ["mine", "competitor", "other"]


def test_share_of_shelf_trend_series(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        _sos(db, rid, gid, kid, _iso(1), mine, 5, 0)   # day A: mine 5 of 10 = 50%
        _sos(db, rid, gid, kid, _iso(1), comp, 5, 0)
        _sos(db, rid, gid, kid, _iso(0), mine, 8, 0)   # day B: mine 8 of 10 = 80%
        _sos(db, rid, gid, kid, _iso(0), comp, 2, 0)
        db.commit()

        trend = ci_analysis.share_of_shelf_trend(db, gid, "wow")
        assert trend["dates"] == [_iso(1), _iso(0)]
        mine_series = next(b for b in trend["brands"] if b["brand_id"] == mine)
        assert mine_series["share"] == [50.0, 80.0]


def test_rank_summary_current_prior_and_sparkline(app):
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        item = "10294528"
        # In-window sightings: pos 5 three days ago, pos 3 today (improved).
        _result(db, rid, gid, kid, _iso(3), 5, item, mine)
        _result(db, rid, gid, kid, _iso(0), 3, item, mine)
        # Prior window sighting: best pos 8.
        _result(db, rid, gid, kid, _iso(10), 8, item, mine)
        db.commit()

        rows = ci_analysis.rank_summary(db, gid, "wow")
        assert len(rows) == 1
        r = rows[0]
        assert r["current_position"] == 3
        assert r["prior_position"] == 8
        assert r["delta"] == 5           # 8 - 3, moved up 5 slots
        assert r["positions"] == [5, 3]  # sparkline oldest->newest
        assert r["keyword"] == "hot sauce"
        assert r["brand_name"] == "Tabasco"


def test_rank_summary_includes_competitors_brand_level(app):
    # Ranking is brand-level and includes competitors so the user can compare.
    with app.app_context():
        db = get_db()
        _, gid, mine, comp, _, kid, rid = _setup(db)
        # Same keyword, same day: my brand at #4, competitor at #2 (better).
        _result(db, rid, gid, kid, _iso(0), 4, "10294527", mine)
        _result(db, rid, gid, kid, _iso(0), 2, "88880000", comp)
        db.commit()
        rows = ci_analysis.rank_summary(db, gid, "wow")
        by_brand = {r["brand_name"]: r for r in rows}
        assert "Tabasco" in by_brand and "Frank's" in by_brand
        assert by_brand["Tabasco"]["current_position"] == 4
        assert by_brand["Frank's"]["current_position"] == 2
        # Mine sorts first.
        assert rows[0]["type"] == "mine"


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
        # hot sauce: Tabasco #2 & #4 -> avg 3.0; Frank's #5 -> 5.0.
        _result(db, rid, gid, kid, _iso(0), 2, "a", mine)
        _result(db, rid, gid, kid, _iso(0), 4, "b", mine)
        _result(db, rid, gid, kid, _iso(0), 5, "c", comp)
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


def test_rank_summary_omits_products_with_no_sightings(app):
    with app.app_context():
        db = get_db()
        _, gid, *_ = _setup(db)
        assert ci_analysis.rank_summary(db, gid, "wow") == []


def test_period_windows_do_not_overlap():
    start, end = ci_analysis.get_date_range("wow", today=date(2026, 8, 22))
    p_start, p_end = ci_analysis.get_prior_date_range("wow", today=date(2026, 8, 22))
    assert end == "2026-08-22" and start == "2026-08-15"
    assert p_end == "2026-08-15" and p_start == "2026-08-08"
