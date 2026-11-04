"""Phase 4 tests: the admin grant-management UI (User Management panel).

An account manager (admin) assigns a helper (a 'user' account) access to a slice
of the manager's own portfolio — whole portfolio, a brand, or a single item —
and can revoke it. These exercise the real admin routes end-to-end, and prove
the write path fails closed: a brand or item that isn't the admin's own can
never be granted, and a manager can only revoke grants they issued.
"""

from app import access
from app.db import get_db
from app.users import create_local_user, get_by_email

_ADMIN = "mgr@example.com"
_PW = "password123"


def _as_admin(app):
    """Make the manager the only admin for this test app."""
    app.config["ADMIN_EMAILS"] = {_ADMIN}


def _mk_user(db, email, *, role="user"):
    uid = create_local_user(email, _PW)
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    db.commit()
    return uid


def _add_item(db, owner_id, *, brand, item_id):
    db.execute(
        "INSERT INTO portfolio_items (user_id, brand, walmart_url, item_id, "
        "title, title_status, dedupe_key, tracked) "
        "VALUES (?, ?, ?, ?, ?, 'done', ?, 0)",
        (owner_id, brand, f"https://www.walmart.com/ip/{item_id}", item_id,
         f"{brand} product", item_id),
    )
    db.commit()
    return db.execute(
        "SELECT id FROM portfolio_items WHERE item_id = ? AND user_id = ?",
        (item_id, owner_id),
    ).fetchone()["id"]


def _grants(db, owner_id, grantee_id):
    return access.list_grants(db, owner_id, grantee_id)


# --- authorization ---------------------------------------------------------

def test_grant_routes_forbidden_for_regular_user(client, auth, app):
    _as_admin(app)
    auth.register(email="regular@example.com", password=_PW)  # not an admin
    assert client.post("/admin/users/1/grants",
                       data={"scope_type": "all"}).status_code == 403
    assert client.post("/admin/users/grants/1/delete").status_code == 403


# --- create ----------------------------------------------------------------

def test_admin_grants_whole_portfolio(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        helper = _mk_user(db, "help@example.com")

    resp = client.post(f"/admin/users/{helper}/grants",
                       data={"scope_type": "all"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        db = get_db()
        rows = _grants(db, admin_id, helper)
        assert len(rows) == 1
        assert rows[0]["scope_type"] == "all"
        assert rows[0]["scope_ref"] is None
        # The grant is what makes the account a helper.
        assert access.is_helper(db, get_by_email("help@example.com"))


def test_admin_grants_brand_stores_canonical_casing(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        _add_item(db, admin_id, brand="Tabasco", item_id="11111")
        helper = _mk_user(db, "help@example.com")

    # Submit a differently-cased brand; the stored ref should match the
    # portfolio's casing, while matching stays case-insensitive.
    client.post(f"/admin/users/{helper}/grants",
                data={"scope_type": "brand", "scope_brand": "tabasco"})
    with app.app_context():
        db = get_db()
        rows = _grants(db, admin_id, helper)
        assert len(rows) == 1
        assert rows[0]["scope_type"] == "brand"
        assert rows[0]["scope_ref"] == "Tabasco"


def test_admin_cannot_grant_brand_outside_own_portfolio(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        _add_item(db, admin_id, brand="Tabasco", item_id="11111")
        helper = _mk_user(db, "help@example.com")

    client.post(f"/admin/users/{helper}/grants",
                data={"scope_type": "brand", "scope_brand": "Ritz"})
    with app.app_context():
        db = get_db()
        assert _grants(db, admin_id, helper) == []


def test_admin_grants_item_only_when_owned(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        mine = _add_item(db, admin_id, brand="Tabasco", item_id="11111")
        # An item owned by someone else — must never be grantable by this admin.
        other = _mk_user(db, "other@example.com", role="admin")
        theirs = _add_item(db, other, brand="Ritz", item_id="99999")
        helper = _mk_user(db, "help@example.com")

    # Owned item → granted.
    client.post(f"/admin/users/{helper}/grants",
                data={"scope_type": "item", "scope_item": str(mine)})
    # Someone else's item → rejected.
    client.post(f"/admin/users/{helper}/grants",
                data={"scope_type": "item", "scope_item": str(theirs)})
    with app.app_context():
        db = get_db()
        rows = _grants(db, admin_id, helper)
        assert len(rows) == 1
        assert rows[0]["scope_type"] == "item"
        assert rows[0]["scope_ref"] == str(mine)


def test_admin_cannot_grant_to_another_admin(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        other_admin = _mk_user(db, "other@example.com", role="admin")

    resp = client.post(f"/admin/users/{other_admin}/grants",
                       data={"scope_type": "all"}, follow_redirects=True)
    assert resp.status_code == 200
    with app.app_context():
        db = get_db()
        assert _grants(db, admin_id, other_admin) == []


def test_duplicate_grant_is_rejected(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        helper = _mk_user(db, "help@example.com")

    client.post(f"/admin/users/{helper}/grants", data={"scope_type": "all"})
    client.post(f"/admin/users/{helper}/grants", data={"scope_type": "all"})
    with app.app_context():
        db = get_db()
        assert len(_grants(db, admin_id, helper)) == 1


def test_bad_scope_type_rejected(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        helper = _mk_user(db, "help@example.com")

    client.post(f"/admin/users/{helper}/grants", data={"scope_type": "everything"})
    with app.app_context():
        db = get_db()
        assert _grants(db, admin_id, helper) == []


# --- revoke ----------------------------------------------------------------

def test_admin_revokes_own_grant(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        helper = _mk_user(db, "help@example.com")
        grant_id = access.grant_access(db, admin_id, helper, "all", None)

    client.post(f"/admin/users/grants/{grant_id}/delete")
    with app.app_context():
        db = get_db()
        assert _grants(db, admin_id, helper) == []


def test_admin_cannot_revoke_another_managers_grant(client, auth, app):
    """A revoke is owner-scoped: admin A can't remove a grant admin B issued."""
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        # Second manager issues a grant of their own.
        other_admin = _mk_user(db, "other@example.com", role="admin")
        helper = _mk_user(db, "help@example.com")
        foreign_grant = access.grant_access(db, other_admin, helper, "all", None)

    # The acting admin (_ADMIN) tries to revoke the other manager's grant.
    client.post(f"/admin/users/grants/{foreign_grant}/delete")
    with app.app_context():
        db = get_db()
        # Still present — the owner filter matched nothing for the acting admin.
        assert len(_grants(db, other_admin, helper)) == 1


# --- panel render ----------------------------------------------------------

def test_user_management_renders_grant_panel(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(_ADMIN)["id"]
        _add_item(db, admin_id, brand="Tabasco", item_id="11111")
        helper = _mk_user(db, "help@example.com")
        access.grant_access(db, admin_id, helper, "brand", "Tabasco")

    body = client.get("/admin/users").data
    assert b"Portfolio access" in body       # panel header
    assert b"Brand &middot; Tabasco" in body or b"Brand \xc2\xb7 Tabasco" in body
    assert b"Grant access" in body           # add form
