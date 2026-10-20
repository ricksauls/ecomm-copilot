"""Tests for the one-off tracking-enable admin command (app.enable_tracking)."""

from app import enable_tracking
from app.db import get_db
from app.users import create_local_user


def _insert(db, uid, *, brand, title, item_id):
    # Insert directly as an untracked item with no keywords, to prove the command
    # both enables tracking and seeds keywords.
    db.execute(
        "INSERT INTO portfolio_items (user_id, brand, walmart_url, item_id, title, "
        "title_status, dedupe_key, tracked) VALUES (?, ?, ?, ?, ?, 'done', ?, 0)",
        (uid, brand, f"https://www.walmart.com/ip/{item_id}", item_id, title, item_id))
    db.commit()
    return db.execute("SELECT id FROM portfolio_items WHERE item_id = ?", (item_id,)).fetchone()["id"]


def test_enable_by_prefix_matches_brand_or_title_case_insensitively(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("en@example.com", "password123")
        demo_brand = _insert(db, uid, brand="DEMO Tabasco", title="Hot Sauce", item_id="111")
        demo_title = _insert(db, uid, brand="Acme", title="demo Widget", item_id="222")  # lowercase
        other = _insert(db, uid, brand="Frank's", title="Wing Sauce", item_id="333")

        assert enable_tracking.enable_by_prefix(db, "DEMO") == 2

        tracked = {r["id"]: r["tracked"] for r in db.execute(
            "SELECT id, tracked FROM portfolio_items")}
        assert tracked[demo_brand] == 1
        assert tracked[demo_title] == 1   # matched on title, case-insensitive
        assert tracked[other] == 0        # untouched

        # Matched items got their 3 keyword rows seeded.
        assert db.execute(
            "SELECT COUNT(*) c FROM portfolio_keywords WHERE portfolio_item_id = ?",
            (demo_brand,)).fetchone()["c"] == 3
        # The non-matching item was left without keywords.
        assert db.execute(
            "SELECT COUNT(*) c FROM portfolio_keywords WHERE portfolio_item_id = ?",
            (other,)).fetchone()["c"] == 0


def test_enable_by_prefix_is_idempotent(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("en2@example.com", "password123")
        _insert(db, uid, brand="DEMO Co", title="Thing", item_id="111")
        assert enable_tracking.enable_by_prefix(db, "DEMO") == 1
        assert enable_tracking.enable_by_prefix(db, "DEMO") == 1  # re-run, same result
