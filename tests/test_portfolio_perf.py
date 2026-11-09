"""Tests for the Discovery-lane portfolio roll-ups (app.portfolio_perf).

These aggregate ``portfolio_rank_readings`` across a user's tracked items over an
arbitrary date window (the window the route derives from the Dashboard period).
Covered: the portfolio summary (avg rank, share-of-search, item/keyword counts),
the per-item all-items table, the single-item drill summary + per-keyword table,
tracked-only scoping, brand filtering, the empty/"gathering" window, and user
scoping (IDOR).
"""

from app import portfolio, portfolio_perf
from app.db import get_db
from app.users import create_local_user

_URL_A = "https://www.walmart.com/ip/Tabasco-Original/111"
_URL_B = "https://www.walmart.com/ip/Cholula-Original/222"
_URL_C = "https://www.walmart.com/ip/Tapatio-Original/333"

_WIN = ("2026-01-01", "2026-01-07")  # a one-week window the readings fall in


def _add(db, uid, brand, url):
    """Add a tracked item and return its portfolio_items.id."""
    portfolio.add_item(db, uid, brand, url, None)
    return db.execute(
        "SELECT id FROM portfolio_items WHERE user_id = ? AND walmart_url = ?",
        (uid, url)).fetchone()["id"]


def _kw(db, item_id, slot, text=None):
    """Return the keyword-slot id, optionally setting its keyword text first."""
    if text is not None:
        db.execute("UPDATE portfolio_keywords SET keyword = ? "
                   "WHERE portfolio_item_id = ? AND slot = ?", (text, item_id, slot))
        db.commit()
    return db.execute("SELECT id FROM portfolio_keywords "
                      "WHERE portfolio_item_id = ? AND slot = ?",
                      (item_id, slot)).fetchone()["id"]


def _reading(db, uid, item_id, kid, when, rank, item_slots, total_slots,
             item_sponsored=None, total_sponsored=None):
    # item_sponsored/total_sponsored default to NULL — a reading captured before the
    # organic/paid split shipped, which the roll-up treats as "split unknown".
    db.execute(
        "INSERT INTO portfolio_rank_readings "
        "(portfolio_item_id, keyword_id, user_id, walmart_item_id, captured_at, "
        " rank, item_slots, total_slots, item_sponsored_slots, total_sponsored_slots) "
        "VALUES (?, ?, ?, '0', ?, ?, ?, ?, ?, ?)",
        (item_id, kid, uid, when, rank, item_slots, total_slots,
         item_sponsored, total_sponsored))
    db.commit()


def _seed(db):
    """Two tracked items with readings inside _WIN, plus one out-of-window reading.

    Item A (Tabasco): kw1 ranks 2 & 4 (share 2/8), kw2 off page 1 (0/5).
    Item B (Cholula): kw1 rank 10 (share 1/2).
    """
    uid = create_local_user("perf@example.com", "password123")
    a = _add(db, uid, "Tabasco", _URL_A)
    b = _add(db, uid, "Cholula", _URL_B)
    a_kw1 = _kw(db, a, 1)                 # slot 1 seeded to the brand on add
    a_kw2 = _kw(db, a, 2, "hot sauce")    # slot 2 starts NULL — give it a term
    b_kw1 = _kw(db, b, 1)
    _reading(db, uid, a, a_kw1, "2026-01-02", 2, 1, 4)
    _reading(db, uid, a, a_kw1, "2026-01-03", 4, 1, 4)
    _reading(db, uid, a, a_kw2, "2026-01-02", None, 0, 5)   # measured, never on page 1
    _reading(db, uid, b, b_kw1, "2026-01-02", 10, 1, 2)
    _reading(db, uid, a, a_kw1, "2025-12-30", 1, 1, 4)      # before the window
    return uid, a, b


def test_portfolio_rank_summary_rolls_up_window(app):
    with app.app_context():
        db = get_db()
        uid, _a, _b = _seed(db)
        s = portfolio_perf.portfolio_rank_summary(db, uid, *_WIN)
        assert s["avg_rank"] == 5.3                 # (2+4+10)/3, nulls ignored
        assert s["share"] == 20                      # 3 own slots / 15 total slots
        assert s["items_total"] == 2
        assert s["items_ranked"] == 2
        assert s["keywords_total"] == 3              # (A,kw1) (A,kw2) (B,kw1)
        assert s["keywords_no_rank"] == 1            # (A,kw2) never on page 1
        assert s["n"] == 4                           # out-of-window reading excluded


def test_portfolio_rank_summary_empty_window_is_gathering(app):
    with app.app_context():
        db = get_db()
        uid, _a, _b = _seed(db)
        s = portfolio_perf.portfolio_rank_summary(db, uid, "2025-06-01", "2025-06-07")
        assert s == {"avg_rank": None, "share": None, "organic_share": None,
                     "paid_share": None, "items_total": 0, "items_ranked": 0,
                     "keywords_total": 0, "keywords_no_rank": 0, "n": 0}


def test_portfolio_rank_summary_brand_filter(app):
    with app.app_context():
        db = get_db()
        uid, _a, _b = _seed(db)
        s = portfolio_perf.portfolio_rank_summary(db, uid, *_WIN, brands=["Tabasco"])
        assert s["items_total"] == 1                 # only item A
        assert s["share"] == round(100 * 2 / 13)     # A's 2 own slots / 13 total (kw1 8 + kw2 5)


def test_portfolio_rank_summary_excludes_untracked(app):
    with app.app_context():
        db = get_db()
        uid, _a, b = _seed(db)
        db.execute("UPDATE portfolio_items SET tracked = 0 WHERE id = ?", (b,))
        db.commit()
        s = portfolio_perf.portfolio_rank_summary(db, uid, *_WIN)
        assert s["items_total"] == 1                 # B dropped


def test_tracked_items_with_readings_includes_empty_item(app):
    with app.app_context():
        db = get_db()
        uid, a, b = _seed(db)
        c = _add(db, uid, "Tapatio", _URL_C)         # tracked, no readings in window
        rows = {r["id"]: r for r in portfolio_perf.tracked_items_with_readings(db, uid, *_WIN)}
        assert set(rows) == {a, b, c}
        assert rows[a]["avg_rank"] == 3.0            # AVG(2,4), kw2 null ignored
        assert rows[a]["share"] == round(100 * 2 / 13)
        assert rows[a]["n"] == 3
        assert rows[b]["avg_rank"] == 10.0 and rows[b]["share"] == 50
        assert rows[c]["avg_rank"] is None and rows[c]["share"] is None and rows[c]["n"] == 0


def test_item_window_summary_and_idor(app):
    with app.app_context():
        db = get_db()
        uid, a, _b = _seed(db)
        s = portfolio_perf.item_window_summary(db, uid, a, *_WIN)
        assert s["avg_rank"] == 3.0 and s["share"] == round(100 * 2 / 13) and s["n"] == 3
        other = create_local_user("intruder@example.com", "password123")
        assert portfolio_perf.item_window_summary(db, other, a, *_WIN)["n"] == 0


def test_item_keyword_readings_per_keyword_and_scoping(app):
    with app.app_context():
        db = get_db()
        uid, a, _b = _seed(db)
        rows = portfolio_perf.item_keyword_readings(db, uid, a, *_WIN)
        assert [r["slot"] for r in rows] == [1, 2]   # both non-blank slots, slot order
        assert rows[0]["avg_rank"] == 3.0 and rows[0]["share"] == 25 and rows[0]["n"] == 2
        assert rows[1]["avg_rank"] is None and rows[1]["share"] == 0 and rows[1]["n"] == 1
        other = create_local_user("intruder2@example.com", "password123")
        assert portfolio_perf.item_keyword_readings(db, other, a, *_WIN) == []


# ── Organic/paid decomposition (share split) ──────────────────────────────────────

def _seed_split(db):
    """One tracked item whose readings carry the organic/paid split, plus one pre-split
    (NULL) day. Two split days: the item holds 2 of 10 page slots, 1 of them paid; the
    page shows 3 ads total. A third day has the same blended ratio but no split — it
    counts toward total share but must be excluded from the organic/paid math.
    """
    uid = create_local_user("split@example.com", "password123")
    a = _add(db, uid, "Tabasco", _URL_A)
    a_kw1 = _kw(db, a, 1)
    _reading(db, uid, a, a_kw1, "2026-01-02", 2, 2, 10, item_sponsored=1, total_sponsored=3)
    _reading(db, uid, a, a_kw1, "2026-01-03", 2, 2, 10, item_sponsored=1, total_sponsored=3)
    _reading(db, uid, a, a_kw1, "2026-01-04", 2, 2, 10)   # pre-split day (NULL split)
    return uid, a


def test_portfolio_summary_organic_paid_decomposition(app):
    with app.app_context():
        db = get_db()
        uid, _a = _seed_split(db)
        s = portfolio_perf.portfolio_rank_summary(db, uid, *_WIN)
        assert s["share"] == 20              # 6 own / 30 total (all three days)
        assert s["paid_share"] == 10         # 2 paid / 20 split-scoped total
        assert s["organic_share"] == 10      # (4 − 2) organic / 20 split-scoped total
        # Decomposition reconstructs the blended share over split-bearing readings.
        assert s["organic_share"] + s["paid_share"] == 20


def test_item_window_summary_organic_paid(app):
    with app.app_context():
        db = get_db()
        uid, a = _seed_split(db)
        s = portfolio_perf.item_window_summary(db, uid, a, *_WIN)
        assert s["share"] == 20
        assert (s["organic_share"], s["paid_share"]) == (10, 10)


def test_organic_paid_collecting_until_split_lands(app):
    with app.app_context():
        db = get_db()
        uid, a, _b = _seed(db)               # _seed inserts NULL-split readings only
        s = portfolio_perf.portfolio_rank_summary(db, uid, *_WIN)
        assert s["share"] == 20              # blended share still reports
        # No reading carries the split yet → organic/paid stay "collecting" (None).
        assert s["organic_share"] is None and s["paid_share"] is None
        it = portfolio_perf.item_window_summary(db, uid, a, *_WIN)
        assert it["organic_share"] is None and it["paid_share"] is None
