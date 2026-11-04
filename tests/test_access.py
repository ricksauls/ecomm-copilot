"""Tests for portfolio access resolution (app.access).

Phase 0 of the account-manager / helper access model: the resolver turns
``portfolio_grants`` rows into the item and competitive-group filters the rest
of the app will apply. These tests pin down the fail-closed behavior and the
grant-to-scope rules before any route depends on them.
"""

from app import access
from app.db import get_db
from app.users import create_local_user


def _make_user(db, email, *, role="user"):
    """Create a user, set its role, and return the full user row."""
    uid = create_local_user(email, "password123")
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    db.commit()
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def _add_item(db, owner_id, *, brand, item_id):
    """Insert one portfolio item owned by ``owner_id`` and return its id."""
    db.execute(
        "INSERT INTO portfolio_items (user_id, brand, walmart_url, item_id, "
        "title, title_status, dedupe_key, tracked) "
        "VALUES (?, ?, ?, ?, ?, 'done', ?, 1)",
        (owner_id, brand, f"https://www.walmart.com/ip/{item_id}", item_id,
         f"{brand} product", item_id),
    )
    db.commit()
    return db.execute(
        "SELECT id FROM portfolio_items WHERE item_id = ? AND user_id = ?",
        (item_id, owner_id),
    ).fetchone()["id"]


def _grant(db, owner_id, grantee_id, scope_type, scope_ref=None):
    db.execute(
        "INSERT INTO portfolio_grants (owner_user_id, grantee_user_id, "
        "scope_type, scope_ref) VALUES (?, ?, ?, ?)",
        (owner_id, grantee_id, scope_type, scope_ref),
    )
    db.commit()


def _add_ci_group(db, owner_id, *, name, mine_brand):
    """Create a monitoring group with one 'mine' brand; return the group id."""
    db.execute(
        "INSERT INTO ci_groups (user_id, name, mode) VALUES (?, ?, 'monitoring')",
        (owner_id, name),
    )
    gid = db.execute(
        "SELECT id FROM ci_groups WHERE user_id = ? AND name = ?",
        (owner_id, name),
    ).fetchone()["id"]
    db.execute(
        "INSERT INTO ci_brands (group_id, name, type) VALUES (?, ?, 'mine')",
        (gid, mine_brand),
    )
    db.commit()
    return gid


# --- item scope ------------------------------------------------------------

def test_anonymous_is_denied(app):
    with app.app_context():
        db = get_db()
        scope = access.resolve_item_scope(db, None)
        assert scope.denied
        assert scope.owner_id is None


def test_admin_sees_own_whole_portfolio(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        _add_item(db, admin["id"], brand="Tabasco", item_id="100")

        scope = access.resolve_item_scope(db, admin)
        assert scope.unrestricted          # no id filter
        assert not scope.denied
        assert scope.owner_id == admin["id"]
        assert scope.item_ids is None


def test_helper_without_grants_sees_nothing(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        _add_item(db, admin["id"], brand="Tabasco", item_id="100")
        helper = _make_user(db, "help@example.com")

        scope = access.resolve_item_scope(db, helper)
        assert scope.denied
        assert scope.owner_id is None


def test_all_grant_gives_whole_portfolio(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        _add_item(db, admin["id"], brand="Tabasco", item_id="100")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "all")

        scope = access.resolve_item_scope(db, helper)
        assert scope.unrestricted
        assert scope.owner_id == admin["id"]
        assert scope.item_ids is None


def test_brand_grant_matches_case_insensitively_and_excludes_others(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        tab_lower = _add_item(db, admin["id"], brand="tabasco", item_id="100")
        tab_upper = _add_item(db, admin["id"], brand="TABASCO", item_id="101")
        ritz = _add_item(db, admin["id"], brand="Ritz", item_id="200")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "brand", "Tabasco")

        scope = access.resolve_item_scope(db, helper)
        assert not scope.unrestricted
        assert scope.owner_id == admin["id"]
        assert scope.item_ids == frozenset({tab_lower, tab_upper})
        assert ritz not in scope.item_ids


def test_brand_grant_picks_up_items_added_later(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "brand", "Tabasco")

        # Granted before any Tabasco item exists -> empty, but not an error.
        assert access.resolve_item_scope(db, helper).item_ids == frozenset()

        new_item = _add_item(db, admin["id"], brand="Tabasco", item_id="100")
        assert access.resolve_item_scope(db, helper).item_ids == frozenset({new_item})


def test_item_grant_scopes_to_that_item_only(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        one = _add_item(db, admin["id"], brand="Tabasco", item_id="100")
        _add_item(db, admin["id"], brand="Tabasco", item_id="101")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "item", str(one))

        scope = access.resolve_item_scope(db, helper)
        assert scope.item_ids == frozenset({one})


def test_item_grant_cannot_reach_an_item_the_owner_does_not_own(app):
    """The owner-id filter is the authorization check against a stale/forged ref."""
    with app.app_context():
        db = get_db()
        owner = _make_user(db, "mgr@example.com", role="admin")
        other = _make_user(db, "other@example.com", role="admin")
        foreign = _add_item(db, other["id"], brand="Secret", item_id="999")
        helper = _make_user(db, "help@example.com")
        # Grant says owner=mgr but points at an item owned by `other`.
        _grant(db, owner["id"], helper["id"], "item", str(foreign))

        scope = access.resolve_item_scope(db, helper)
        assert scope.owner_id == owner["id"]
        assert scope.item_ids == frozenset()   # foreign item never resolves in


# --- competitive scope -----------------------------------------------------

def test_admin_sees_all_own_ci_groups(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        _add_ci_group(db, admin["id"], name="Hot Sauce", mine_brand="Tabasco")

        scope = access.resolve_ci_scope(db, admin)
        assert scope.unrestricted
        assert scope.group_ids is None


def test_ci_brand_grant_matches_only_that_brands_group(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        tab_group = _add_ci_group(db, admin["id"], name="Hot Sauce", mine_brand="Tabasco")
        _add_ci_group(db, admin["id"], name="Crackers", mine_brand="Ritz")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "brand", "tabasco")   # case-insensitive

        scope = access.resolve_ci_scope(db, helper)
        assert scope.owner_id == admin["id"]
        assert scope.group_ids == frozenset({tab_group})


def test_ci_item_only_grant_unlocks_nothing(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        item = _add_item(db, admin["id"], brand="Tabasco", item_id="100")
        _add_ci_group(db, admin["id"], name="Hot Sauce", mine_brand="Tabasco")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "item", str(item))

        scope = access.resolve_ci_scope(db, helper)
        assert scope.denied            # item grant gives no competitive access
        assert scope.group_ids == frozenset()


def test_ci_all_grant_is_unrestricted(app):
    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        _add_ci_group(db, admin["id"], name="Hot Sauce", mine_brand="Tabasco")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "all")

        scope = access.resolve_ci_scope(db, helper)
        assert scope.unrestricted
        assert scope.group_ids is None


# --- schema guard ----------------------------------------------------------

def test_duplicate_grant_is_rejected_by_unique_index(app):
    import sqlite3

    with app.app_context():
        db = get_db()
        admin = _make_user(db, "mgr@example.com", role="admin")
        helper = _make_user(db, "help@example.com")
        _grant(db, admin["id"], helper["id"], "all")

        # The COALESCE unique index must treat a second NULL-ref 'all' as a dup.
        try:
            _grant(db, admin["id"], helper["id"], "all")
            assert False, "expected a uniqueness violation on the duplicate grant"
        except sqlite3.IntegrityError:
            pass
