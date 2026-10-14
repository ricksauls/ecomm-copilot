"""Tests for the scoring job queue (enqueue / claim / result store)."""

import json

from app import copy_jobs, jobs
from app.db import get_db
from app.users import create_local_user


def test_count_managed_products_unions_scoring_and_copy_per_user(app):
    # A product counts once whether scored, copy-created, or both; keyed on item id
    # and scoped to the user.
    with app.app_context():
        db = get_db()
        uid = create_local_user("m@example.com", "password123")
        other = create_local_user("n@example.com", "password123")
        jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        copy_jobs.enqueue_copy_items(db, uid, [
            {"url": "https://www.walmart.com/ip/2", "item": "2"},  # overlap -> not double
            {"url": "https://www.walmart.com/ip/3", "item": "3"},
        ])
        jobs.enqueue_items(db, other, [{"url": "https://www.walmart.com/ip/9", "item": "9"}])

        assert jobs.count_managed_products(db, uid) == 3    # {1, 2, 3}
        assert jobs.count_managed_products(db, other) == 1  # {9}


def test_product_counts_are_distinct_and_month_scoped(app):
    from datetime import date

    from app import copy_jobs
    with app.app_context():
        db = get_db()
        uid = create_local_user("month@example.com", "password123")
        # scored: item 1 in a prior month, item 2 twice this month, item 3 this month.
        db.execute("INSERT INTO scored_items (user_id, item_id, url, created_at) "
                   "VALUES (?,?,?,?)", (uid, "1", "u", "2020-01-05 00:00:00"))
        for it in ("2", "2", "3"):
            db.execute("INSERT INTO scored_items (user_id, item_id, url, created_at) "
                       "VALUES (?,?,?,datetime('now'))", (uid, it, "u"))
        # copy: item 3 (also scored) + item 4, both this month.
        for it in ("3", "4"):
            db.execute("INSERT INTO copy_items (user_id, item_id, url, created_at) "
                       "VALUES (?,?,?,datetime('now'))", (uid, it, "u"))
        db.commit()
        month = date.today().replace(day=1).isoformat()

        assert jobs.count_scored_products(db, uid) == 3            # {1,2,3}
        assert jobs.count_scored_products(db, uid, since=month) == 2   # {2,3}
        assert copy_jobs.count_copy_products(db, uid) == 2         # {3,4}
        assert copy_jobs.count_copy_products(db, uid, since=month) == 2
        # Managed union: {1,2,3,4} all-time; {2,3,4} this month (item 1 is old).
        assert jobs.count_managed_products(db, uid) == 4
        assert jobs.count_managed_products(db, uid, since=month) == 3


def test_count_managed_brands_is_distinct_case_insensitive_and_unions(app):
    # Distinct brands across scoring + copy, compared case-insensitively, scoped
    # to the user; blank/NULL brands are excluded.
    with app.app_context():
        db = get_db()
        uid = create_local_user("brands@example.com", "password123")
        other = create_local_user("brands2@example.com", "password123")
        jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1", "brand": "Tabasco"},
            {"url": "https://www.walmart.com/ip/2", "item": "2", "brand": "TABASCO"},  # same brand
            {"url": "https://www.walmart.com/ip/3", "item": "3", "brand": None},       # no brand
        ])
        copy_jobs.enqueue_copy_items(db, uid, [
            {"url": "https://www.walmart.com/ip/4", "item": "4", "brand": "Cholula"},
        ])
        jobs.enqueue_items(db, other, [
            {"url": "https://www.walmart.com/ip/9", "item": "9", "brand": "Frank's"},
        ])

        assert jobs.count_managed_brands(db, uid) == 2    # {tabasco, cholula}
        assert jobs.count_managed_brands(db, other) == 1  # {frank's}


def test_save_result_keeps_user_brand_but_fills_blank_from_pdp(app):
    # A user-entered brand wins; a blank one is filled from the scraped PDP brand.
    with app.app_context():
        db = get_db()
        uid = create_local_user("brandfill@example.com", "password123")
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1", "brand": "My Brand"},
            {"url": "https://www.walmart.com/ip/2", "item": "2", "brand": None},
        ])
        jobs.save_result(db, ids[0], 80, {"overall": 80, "dimensions": []},
                         "Prod A", brand="PDP Brand")   # user brand present -> unchanged
        jobs.save_result(db, ids[1], 80, {"overall": 80, "dimensions": []},
                         "Prod B", brand="PDP Brand")   # blank -> filled from PDP

        rows = {r["id"]: r for r in jobs.get_items(db, ids, uid)}
        assert rows[ids[0]]["brand"] == "My Brand"
        assert rows[ids[1]]["brand"] == "PDP Brand"


def test_enqueue_and_get_items_scoped_to_user(app):
    with app.app_context():
        uid = create_local_user("a@example.com", "password123")
        other = create_local_user("b@example.com", "password123")
        db = get_db()
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        assert len(ids) == 2
        rows = jobs.get_items(db, ids, uid)
        assert [r["status"] for r in rows] == ["queued", "queued"]
        # Another user can't read this user's rows (IDOR guard).
        assert jobs.get_items(db, ids, other) == []


def test_claim_next_marks_scoring_and_drains(app):
    with app.app_context():
        uid = create_local_user("c@example.com", "password123")
        db = get_db()
        jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/1", "item": "1"}])
        claimed = jobs.claim_next(db)
        assert claimed is not None
        assert claimed["status"] == "scoring"
        # Nothing left to claim once drained.
        assert jobs.claim_next(db) is None


def test_save_result_and_mark_failed(app):
    with app.app_context():
        uid = create_local_user("d@example.com", "password123")
        db = get_db()
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        jobs.save_result(db, ids[0], 82, {"overall": 82, "dimensions": []},
                         "Tabasco Chipotle Pepper Sauce, 5 fl oz")
        jobs.mark_failed(db, ids[1], "blocked", "bot detection")

        rows = {r["id"]: r for r in jobs.get_items(db, ids, uid)}
        assert rows[ids[0]]["status"] == "scored"
        assert rows[ids[0]]["overall"] == 82
        assert rows[ids[0]]["title"] == "Tabasco Chipotle Pepper Sauce, 5 fl oz"
        assert json.loads(rows[ids[0]]["result_json"])["overall"] == 82
        assert rows[ids[1]]["status"] == "blocked"
        assert rows[ids[1]]["error"] == "bot detection"


def test_reclaim_orphaned_items_fails_stranded_scoring_rows(app):
    # A row left 'scoring' (worker died mid-fetch) is stranded forever; reclaim marks
    # it 'error' so the user sees the failure and can re-run. Queued/finished rows are
    # left untouched, and the sweep is idempotent.
    with app.app_context():
        db = get_db()
        uid = create_local_user("orphan@example.com", "password123")
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
        ])
        claimed = jobs.claim_next(db)  # -> status 'scoring'
        assert claimed["id"] == ids[0]

        assert jobs.reclaim_orphaned_items(db) == 1
        rows = {r["id"]: r for r in jobs.get_items(db, ids, uid)}
        assert rows[ids[0]]["status"] == "error"
        assert "Interrupted" in rows[ids[0]]["error"]
        assert rows[ids[1]]["status"] == "queued"  # never claimed -> untouched
        # Idempotent: nothing left 'scoring' now.
        assert jobs.reclaim_orphaned_items(db) == 0


def test_list_scored_this_month_only_completed_in_window(app):
    # Only 'scored' rows created within the window are listed; queued/blocked rows
    # and rows from before the window are excluded.
    with app.app_context():
        db = get_db()
        uid = create_local_user("month@example.com", "password123")
        ids = jobs.enqueue_items(db, uid, [
            {"url": "https://www.walmart.com/ip/1", "item": "1", "brand": "Tabasco"},
            {"url": "https://www.walmart.com/ip/2", "item": "2"},
            {"url": "https://www.walmart.com/ip/3", "item": "3"},
        ])
        jobs.save_result(db, ids[0], 82, {"overall": 82}, "Tabasco Chipotle")
        jobs.save_result(db, ids[1], 70, {"overall": 70}, "Old Item")
        jobs.mark_failed(db, ids[2], "blocked", "bot detection")
        # Backdate the second scored row to before the window.
        db.execute("UPDATE scored_items SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
                   (ids[1],))
        db.commit()

        rows = jobs.list_scored_activity(db, uid, since="2020-06-01")
        assert [r["id"] for r in rows] == [ids[0]]  # only the recent scored row
        assert rows[0]["brand"] == "Tabasco"
        assert rows[0]["overall"] == 82


def test_has_queued_items(app):
    # Reports whether any row is waiting to be claimed — the worker uses it to
    # decide when to spin up the scoring pool.
    with app.app_context():
        db = get_db()
        uid = create_local_user("q@example.com", "password123")
        assert jobs.has_queued_items(db) is False
        ids = jobs.enqueue_items(db, uid, [{"url": "https://www.walmart.com/ip/1", "item": "1"}])
        assert jobs.has_queued_items(db) is True
        jobs.claim_next(db)  # -> 'scoring', no longer queued
        assert jobs.has_queued_items(db) is False
        jobs.save_result(db, ids[0], 90, {"overall": 90}, "Done")
        assert jobs.has_queued_items(db) is False


def test_concurrent_claim_next_never_double_claims(app):
    # The scoring worker claims from several threads at once. claim_next's
    # conditional UPDATE must hand each queued row to exactly one claimer — no
    # row claimed twice, none dropped — even under contention across connections.
    import sqlite3
    import threading

    from app import db as dbmod

    with app.app_context():
        uid = create_local_user("race@example.com", "password123")
        ids = jobs.enqueue_items(get_db(), uid, [
            {"url": f"https://www.walmart.com/ip/{i}", "item": str(i)}
            for i in range(1, 61)
        ])
        db_path = app.config["DATABASE"]

    claimed = []
    lock = threading.Lock()

    def claim_until_empty():
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        dbmod.tune_connection(conn)  # WAL + busy timeout, as the worker uses
        try:
            while True:
                row = jobs.claim_next(conn)
                if row is None:
                    return
                with lock:
                    claimed.append(row["id"])
        finally:
            conn.close()

    threads = [threading.Thread(target=claim_until_empty) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(claimed) == sorted(ids)        # every item claimed...
    assert len(claimed) == len(set(claimed))     # ...exactly once (no double-claim)


def _score(db, uid, item_id, brand, overall, created_at):
    """Insert one scored_items row with explicit brand/overall/timestamp."""
    db.execute(
        "INSERT INTO scored_items (user_id, item_id, url, brand, overall, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (uid, item_id, "u", brand, overall, created_at),
    )


def test_readiness_score_averages_latest_per_product_by_brand(app):
    # Conversion card: average each product's LATEST score, scoped to the given
    # brand(s); products below the strong band count as needing attention.
    with app.app_context():
        db = get_db()
        uid = create_local_user("ready@example.com", "password123")
        # item 1 (Tabasco) re-scored: 60 then 90 -> latest 90 is what counts.
        _score(db, uid, "1", "Tabasco", 60, "2026-01-01 00:00:00")
        _score(db, uid, "1", "Tabasco", 90, "2026-03-01 00:00:00")
        _score(db, uid, "2", "Tabasco", 70, "2026-03-01 00:00:00")  # <80 -> needs attention
        _score(db, uid, "3", "Frank's", 40, "2026-03-01 00:00:00")
        _score(db, uid, "4", "Tabasco", None, "2026-03-01 00:00:00")  # unscored -> ignored
        db.commit()

        tabasco = jobs.readiness_score(db, uid, ["Tabasco"])
        assert tabasco == {"avg": 80, "scored_count": 2, "needs_attention_count": 1}

        # Brand match is case/space-insensitive.
        assert jobs.readiness_score(db, uid, [" tabasco "])["avg"] == 80

        both = jobs.readiness_score(db, uid, ["Tabasco", "Frank's"])
        assert both == {"avg": 67, "scored_count": 3, "needs_attention_count": 2}  # (90+70+40)/3

        # No brand in scope -> empty result, never a degenerate query.
        assert jobs.readiness_score(db, uid, []) == {
            "avg": None, "scored_count": 0, "needs_attention_count": 0}
        assert jobs.readiness_score(db, uid, ["Unknown"]) == {
            "avg": None, "scored_count": 0, "needs_attention_count": 0}


def _score_detailed(db, uid, item_id, brand, overall, dims, created_at, title=None):
    """Insert a scored_items row carrying a dimension breakdown (result_json).

    ``dims`` is a list of ``(key, score)`` or ``(key, score, available)`` tuples,
    shaped into the ``{"dimensions": [...]}`` payload readiness_breakdown parses.
    """
    dimensions = [
        {"key": k, "label": k, "score": s, "available": (a[0] if a else True)}
        for d in dims for (k, s, *a) in [d]
    ]
    db.execute(
        "INSERT INTO scored_items (user_id, item_id, url, brand, overall, title, "
        "result_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (uid, item_id, "u", brand, overall, title,
         json.dumps({"overall": overall, "dimensions": dimensions}), created_at),
    )


def test_readiness_breakdown_buckets_image_vs_copy_gaps(app):
    # Opportunities list: per-product, flag an image gap (imagery < 80) and/or a
    # copy gap (any text dimension < 80), scoped to the brand, latest score only.
    with app.app_context():
        db = get_db()
        uid = create_local_user("breakdown@example.com", "password123")
        # item 1: weak imagery only -> image gap.
        _score_detailed(db, uid, "1", "Tabasco", 70,
                        [("imagery", 50), ("title", 90), ("description", 85)],
                        "2026-03-01 00:00:00")
        # item 2: weak title only -> copy gap.
        _score_detailed(db, uid, "2", "Tabasco", 72,
                        [("imagery", 100), ("title", 40), ("key_features", 90)],
                        "2026-03-01 00:00:00")
        # item 3: weak on both -> counts in both buckets.
        _score_detailed(db, uid, "3", "Tabasco", 55,
                        [("imagery", 30), ("description", 20)],
                        "2026-03-01 00:00:00")
        # item 4: all strong -> no gap.
        _score_detailed(db, uid, "4", "Tabasco", 95,
                        [("imagery", 100), ("title", 90)],
                        "2026-03-01 00:00:00")
        # item 5 (other brand) -> excluded from the Tabasco breakdown.
        _score_detailed(db, uid, "5", "Frank's", 10,
                        [("imagery", 10), ("title", 10)],
                        "2026-03-01 00:00:00")
        db.commit()

        b = jobs.readiness_breakdown(db, uid, ["Tabasco"])
        assert b["scored_count"] == 4
        assert b["image_gap_count"] == 2   # items 1 and 3
        assert b["copy_gap_count"] == 2    # items 2 and 3
        assert {i["item_id"] for i in b["image_items"]} == {"1", "3"}
        assert {i["item_id"] for i in b["copy_items"]} == {"2", "3"}

        # Empty / unmatched brand scope short-circuits cleanly.
        assert jobs.readiness_breakdown(db, uid, [])["scored_count"] == 0
        assert jobs.readiness_breakdown(db, uid, ["Nope"])["image_gap_count"] == 0


def test_readiness_breakdown_latest_score_and_unmeasured_dimension(app):
    # Only the latest score per product is judged, and a dimension marked
    # unavailable (e.g. images that couldn't be fetched) is never a gap.
    with app.app_context():
        db = get_db()
        uid = create_local_user("breakdown2@example.com", "password123")
        # item 1 re-scored: an early weak imagery, then a strong latest -> no gap.
        _score_detailed(db, uid, "1", "Tabasco", 40,
                        [("imagery", 20)], "2026-01-01 00:00:00")
        _score_detailed(db, uid, "1", "Tabasco", 95,
                        [("imagery", 100)], "2026-03-01 00:00:00")
        # item 2: imagery not measured -> not counted as an image gap.
        _score_detailed(db, uid, "2", "Tabasco", 85,
                        [("imagery", 0, False), ("title", 90)], "2026-03-01 00:00:00")
        db.commit()

        b = jobs.readiness_breakdown(db, uid, ["Tabasco"])
        assert b["scored_count"] == 2
        assert b["image_gap_count"] == 0
        assert b["copy_gap_count"] == 0


def test_readiness_score_as_of_sees_only_scores_up_to_that_moment(app):
    # The card derives its period delta by asking for readiness "as of" a past
    # date — the latest score on or before it.
    with app.app_context():
        db = get_db()
        uid = create_local_user("asof@example.com", "password123")
        _score(db, uid, "1", "Tabasco", 50, "2026-01-01 00:00:00")
        _score(db, uid, "1", "Tabasco", 90, "2026-03-01 00:00:00")
        db.commit()
        # Before the March re-score, readiness reflects the January 50.
        assert jobs.readiness_score(db, uid, ["Tabasco"],
                                    as_of="2026-02-01 23:59:59")["avg"] == 50
        # With no bound, the current 90 wins.
        assert jobs.readiness_score(db, uid, ["Tabasco"])["avg"] == 90


def test_readiness_items_lists_all_products_worst_first(app):
    # The Content Readiness tab shows every scored product (not just gapped ones),
    # worst-first, scoped to the brand.
    with app.app_context():
        db = get_db()
        uid = create_local_user("rdi@example.com", "password123")
        _score_detailed(db, uid, "1", "Tabasco", 90, [("imagery", 90)], "2026-03-01 00:00:00", title="Strong")
        _score_detailed(db, uid, "2", "Tabasco", 55, [("imagery", 55)], "2026-03-01 00:00:00", title="Weak")
        _score_detailed(db, uid, "3", "Frank's", 10, [("imagery", 10)], "2026-03-01 00:00:00", title="Rival")
        db.commit()

        items = jobs.readiness_items(db, uid, ["Tabasco"])
        assert [i["item_id"] for i in items] == ["2", "1"]  # worst-first; other brand excluded
        assert items[0]["score"] == 55 and items[0]["title"] == "Weak"
        assert jobs.readiness_items(db, uid, []) == []


def test_readiness_item_returns_dimensions_and_respects_as_of(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("rdi2@example.com", "password123")
        _score_detailed(db, uid, "1", "Tabasco", 40, [("imagery", 30), ("title", 50)],
                        "2026-01-01 00:00:00", title="X")
        _score_detailed(db, uid, "1", "Tabasco", 85, [("imagery", 90), ("title", 80)],
                        "2026-03-01 00:00:00", title="X")
        db.commit()

        latest = jobs.readiness_item(db, uid, "1")
        assert latest["overall"] == 85
        assert {d["label"]: d["score"] for d in latest["dimensions"]} == {"imagery": 90, "title": 80}
        # as_of reads the score as it stood earlier (for the per-item trend).
        earlier = jobs.readiness_item(db, uid, "1", as_of="2026-02-01 00:00:00")
        assert earlier["overall"] == 40
        assert jobs.readiness_item(db, uid, "999") is None
