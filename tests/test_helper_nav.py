"""Phase 5 tests: the Competitive Tracking setup tool is owner-only.

A helper (a user account holding ≥1 grant) reads competitive data under
Insights but may not reach the setup tool that configures what's tracked — the
routes 403 and the rail leaf is hidden. Owners (admins and plain self-service
accounts) are unaffected.
"""

from app import access
from app.db import get_db
from app.users import create_local_user, get_by_email

_ADMIN = "mgr@example.com"
_PW = "password123"

_MONITORING = "/app/competitive-intel/monitoring"
_CREATE_GROUP = "/app/competitive-intel/monitoring/groups"


def _as_admin(app):
    app.config["ADMIN_EMAILS"] = {_ADMIN}


def _mk_user(db, email, *, role="user"):
    uid = create_local_user(email, _PW)
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    db.commit()
    return uid


def _make_helper(app, admin_email=_ADMIN):
    """Create an admin + a granted helper; return the helper's email."""
    with app.app_context():
        db = get_db()
        admin_id = get_by_email(admin_email)["id"]
        helper = _mk_user(db, "help@example.com")
        access.grant_access(db, admin_id, helper, "all", None)
    return "help@example.com"


# --- route gating ----------------------------------------------------------

def test_helper_cannot_open_ci_setup(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)          # provisions the admin owner
    helper_email = _make_helper(app)
    auth.logout()
    auth.login(email=helper_email, password=_PW)

    assert client.get(_MONITORING).status_code == 403
    assert client.post(_CREATE_GROUP,
                       data={"name": "x", "description": "y"}).status_code == 403


def test_owner_admin_can_open_ci_setup(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    assert client.get(_MONITORING).status_code == 200


def test_plain_account_can_open_ci_setup(client, auth, app):
    """A non-admin with no grants is an owner, not a helper — still allowed."""
    _as_admin(app)
    auth.register(email="solo@example.com", password=_PW)  # not in the allowlist
    assert client.get(_MONITORING).status_code == 200


# --- rail nav flag ---------------------------------------------------------

def test_rail_hides_competitive_tracking_for_helper(client, auth, app):
    _as_admin(app)
    auth.register(email=_ADMIN, password=_PW)
    helper_email = _make_helper(app)
    auth.logout()
    auth.login(email=helper_email, password=_PW)

    body = client.get("/app/portfolio").data
    # The read-only Insights area stays; only the setup leaf goes.
    assert b"Competitive Tracking" not in body
    assert b"Competitive Intelligence" in body


def test_rail_shows_competitive_tracking_for_plain_account(client, auth, app):
    _as_admin(app)
    auth.register(email="solo@example.com", password=_PW)
    body = client.get("/app/portfolio").data
    assert b"Competitive Tracking" in body
