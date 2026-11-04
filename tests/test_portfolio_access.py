"""Phase 1 route tests: the Portfolio surface honors access grants.

A helper (a 'user' role) sees and manages only the items an account manager
(an 'admin') granted them, and can never add, import, or delete items. These
exercise the real routes end-to-end via the test client.
"""

from app.db import get_db
from app.users import create_local_user

_PW = "password123"


def _mk(db, email, *, role="user"):
    uid = create_local_user(email, _PW)
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    db.commit()
    return uid


def _add_item(db, owner_id, *, brand, item_id, tracked=0):
    db.execute(
        "INSERT INTO portfolio_items (user_id, brand, walmart_url, item_id, "
        "title, title_status, dedupe_key, tracked) "
        "VALUES (?, ?, ?, ?, ?, 'done', ?, ?)",
        (owner_id, brand, f"https://www.walmart.com/ip/{item_id}", item_id,
         f"{brand} product", item_id, tracked),
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


def _tracked(db, item_pk):
    return db.execute(
        "SELECT tracked FROM portfolio_items WHERE id = ?", (item_pk,)
    ).fetchone()["tracked"]


# --- listing ---------------------------------------------------------------

def test_helper_sees_only_granted_brand(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        _add_item(db, owner, brand="Tabasco", item_id="11111")
        _add_item(db, owner, brand="Ritz", item_id="22222")
        _mk(db, "help@example.com")
        helper_grant_owner = owner
    with app.app_context():
        db = get_db()
        helper = db.execute(
            "SELECT id FROM users WHERE email = ?", ("help@example.com",)
        ).fetchone()["id"]
        _grant(db, helper_grant_owner, helper, "brand", "Tabasco")

    auth.login(email="help@example.com", password=_PW)
    body = client.get("/app/portfolio").data
    assert b"11111" in body        # Tabasco item, granted
    assert b"22222" not in body    # Ritz item, not granted


def test_helper_with_grant_but_no_matching_items_sees_shared_nothing(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        _add_item(db, owner, brand="Tabasco", item_id="11111")
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "brand", "Ritz")   # owner has no Ritz items

    auth.login(email="help@example.com", password=_PW)
    body = client.get("/app/portfolio").data
    assert b"11111" not in body
    assert b"No items have been shared with you yet." in body


def test_plain_user_without_grants_owns_their_own_portfolio(client, auth, app):
    """Backward compat: a self-service account (no grants) manages its own items."""
    auth.register(email="solo@example.com", password=_PW)
    # Adding is allowed (they are their own owner, not a helper).
    resp = client.post(
        "/app/portfolio/add",
        data={"brand": "Solo", "walmart_url": "https://www.walmart.com/ip/77777"},
    )
    assert resp.status_code == 302
    body = client.get("/app/portfolio").data
    assert b"77777" in body
    assert b"Upload Items" in body   # owns their portfolio -> add surface shown


def test_admin_sees_all_and_has_upload_tab(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        _add_item(db, owner, brand="Tabasco", item_id="11111")
        _add_item(db, owner, brand="Ritz", item_id="22222")

    auth.login(email="mgr@example.com", password=_PW)
    body = client.get("/app/portfolio").data
    assert b"11111" in body
    assert b"22222" in body
    assert b"Upload Items" in body   # owner keeps the add surface


# --- owner-only mutations --------------------------------------------------

def test_helper_cannot_add_import_or_delete(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        item = _add_item(db, owner, brand="Tabasco", item_id="11111")
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "all")

    auth.login(email="help@example.com", password=_PW)
    assert client.post("/app/portfolio/add",
                       data={"brand": "X", "walmart_url": "https://www.walmart.com/ip/5"}
                       ).status_code == 403
    assert client.post("/app/portfolio/import").status_code == 403
    assert client.post(f"/app/portfolio/delete/{item}").status_code == 403


def test_helper_upload_tab_redirects_home(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "all")

    auth.login(email="help@example.com", password=_PW)
    resp = client.get("/app/portfolio/upload")
    assert resp.status_code == 302
    assert "/app/portfolio" in resp.headers["Location"]


# --- scoped management -----------------------------------------------------

def test_helper_can_toggle_tracking_on_granted_item(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        item = _add_item(db, owner, brand="Tabasco", item_id="11111", tracked=0)
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "brand", "Tabasco")

    auth.login(email="help@example.com", password=_PW)
    resp = client.post(f"/app/portfolio/track/{item}", data={"tracked": "1"})
    assert resp.status_code == 302
    with app.app_context():
        assert _tracked(get_db(), item) == 1   # the owner's item was tracked


def test_helper_cannot_toggle_ungranted_item(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        tabasco = _add_item(db, owner, brand="Tabasco", item_id="11111", tracked=0)
        ritz = _add_item(db, owner, brand="Ritz", item_id="22222", tracked=0)
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "brand", "Tabasco")

    auth.login(email="help@example.com", password=_PW)
    resp = client.post(f"/app/portfolio/track/{ritz}", data={"tracked": "1"},
                       follow_redirects=True)
    assert b"no longer in your portfolio" in resp.data
    with app.app_context():
        assert _tracked(get_db(), ritz) == 0    # untouched
        assert _tracked(get_db(), tabasco) == 0  # nothing else changed either


def test_helper_cannot_edit_keywords_on_ungranted_item(client, auth, app):
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        _add_item(db, owner, brand="Tabasco", item_id="11111", tracked=1)
        ritz = _add_item(db, owner, brand="Ritz", item_id="22222", tracked=1)
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "brand", "Tabasco")

    auth.login(email="help@example.com", password=_PW)
    resp = client.post(f"/app/portfolio/{ritz}/keywords",
                       data={"kw1": "hijack"}, follow_redirects=True)
    assert b"no longer in your portfolio" in resp.data
