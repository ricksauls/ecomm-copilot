"""Tests for the calendar-bucket roll-up readers (app.item_rank).

Covers the absolute week/month/quarter bucketing (anchored at Jan 1), the
available-buckets selector source, and the per-keyword rank + share aggregation
with its delta-vs-previous-bucket. There is no daily grain.
"""

from datetime import date

from app import item_rank, portfolio
from app.db import get_db
from app.users import create_local_user

_URL = "https://www.walmart.com/ip/Tabasco-Original/111"


# ── calendar helpers (pure) ──────────────────────────────────────────────────

def test_week_bounds_anchored_at_jan1_with_stub():
    assert item_rank.week_bounds(2026, 1) == ("2026-01-01", "2026-01-07")
    assert item_rank.week_bounds(2026, 2) == ("2026-01-08", "2026-01-14")
    # 2026 has 365 days -> 53 bins; the last is a 1-day stub (Dec 31).
    assert item_rank.weeks_in_year(2026) == 53
    assert item_rank.week_bounds(2026, 53) == ("2026-12-31", "2026-12-31")


def test_month_and_quarter_bounds():
    assert item_rank.month_bounds(2026, 1) == ("2026-01-01", "2026-01-31")
    assert item_rank.quarter_bounds(2026, 1) == ("2026-01-01", "2026-03-31")
    assert item_rank.quarter_bounds(2026, 2) == ("2026-04-01", "2026-06-30")


def test_bucket_of_and_labels():
    assert item_rank.bucket_of("week", date(2026, 1, 1)) == (2026, 1)
    assert item_rank.bucket_of("week", date(2026, 1, 8)) == (2026, 2)
    assert item_rank.bucket_of("month", date(2026, 2, 15)) == (2026, 2)
    assert item_rank.bucket_of("quarter", date(2026, 5, 1)) == (2026, 2)
    assert item_rank.bucket_label("week", 2026, 1) == "Jan 1–7, 2026"
    # A week straddling two months names both months.
    assert item_rank.bucket_label("week", 2026, 5) == "Jan 29 – Feb 4, 2026"
    assert item_rank.bucket_label("month", 2026, 1) == "January 2026"
    assert item_rank.bucket_label("quarter", 2026, 1) == "Q1 2026"


def test_prev_bucket_wraps_year():
    assert item_rank._prev_bucket("week", 2026, 1) == (2025, item_rank.weeks_in_year(2025))
    assert item_rank._prev_bucket("month", 2026, 1) == (2025, 12)
    assert item_rank._prev_bucket("quarter", 2026, 1) == (2025, 4)


# ── roll-up over stored readings ─────────────────────────────────────────────

def _setup(db):
    uid = create_local_user("roll@example.com", "password123")
    portfolio.add_item(db, uid, "Tabasco", _URL, None)
    item = db.execute("SELECT id FROM portfolio_items WHERE item_id = '111'").fetchone()
    kid = db.execute(
        "SELECT id FROM portfolio_keywords WHERE portfolio_item_id = ? AND slot = 1",
        (item["id"],)).fetchone()["id"]
    return uid, item["id"], kid


def _reading(db, uid, item_id, kid, when, rank, item_slots, total_slots):
    db.execute(
        "INSERT INTO portfolio_rank_readings "
        "(portfolio_item_id, keyword_id, user_id, walmart_item_id, captured_at, "
        " rank, item_slots, total_slots) VALUES (?, ?, ?, '111', ?, ?, ?, ?)",
        (item_id, kid, uid, when, rank, item_slots, total_slots))
    db.commit()


def test_available_buckets_newest_first(app):
    with app.app_context():
        db = get_db()
        uid, item_id, kid = _setup(db)
        _reading(db, uid, item_id, kid, "2026-01-02", 4, 1, 40)   # week 1
        _reading(db, uid, item_id, kid, "2026-01-09", 6, 1, 50)   # week 2
        weeks = item_rank.available_buckets(db, item_id, "week")
        assert [(b["year"], b["index"]) for b in weeks] == [(2026, 2), (2026, 1)]
        # Both January readings fall in one month / one quarter bucket.
        assert len(item_rank.available_buckets(db, item_id, "month")) == 1
        assert len(item_rank.available_buckets(db, item_id, "quarter")) == 1


def test_keyword_rollup_aggregates_and_deltas(app):
    with app.app_context():
        db = get_db()
        uid, item_id, kid = _setup(db)
        # Week 1: avg rank (4+2)/2 = 3; share (1+2)/(40+40) = 0.0375.
        _reading(db, uid, item_id, kid, "2026-01-02", 4, 1, 40)
        _reading(db, uid, item_id, kid, "2026-01-05", 2, 2, 40)
        # Week 2: avg rank 6; share 1/50 = 0.02.
        _reading(db, uid, item_id, kid, "2026-01-09", 6, 1, 50)

        rows = item_rank.keyword_rollup(db, uid, item_id, "week", 2026, 2)
        row = next(r for r in rows if r["slot"] == 1)
        assert row["rank_current"] == 6
        assert row["rank_delta"] == 3          # 6 vs week 1's 3 (worse)
        assert abs(row["share_current"] - 0.02) < 1e-9
        assert abs(row["share_delta"] - (0.02 - 0.0375)) < 1e-9
        assert row["n"] == 1                   # one reading in week 2
        # Series carry both buckets (oldest -> newest), nulls dropped.
        assert row["rank_series"][-1] == 6 and 3 in row["rank_series"]


def test_keyword_rollup_first_bucket_has_no_delta(app):
    with app.app_context():
        db = get_db()
        uid, item_id, kid = _setup(db)
        _reading(db, uid, item_id, kid, "2026-01-02", 4, 1, 40)  # week 1 only
        row = next(r for r in item_rank.keyword_rollup(db, uid, item_id, "week", 2026, 1)
                   if r["slot"] == 1)
        assert row["rank_current"] == 4
        assert row["rank_delta"] is None       # no prior bucket with data
        assert row["share_delta"] is None
