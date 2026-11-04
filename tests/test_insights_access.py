"""Phase 2+3 route tests: brand/group-centric surfaces honor access grants.

Performance, Competitive, Reports, and the Dashboard all select a CI monitoring
group as their brand. A helper may only see the owner's groups whose brand they
were granted, and may not schedule reports. Owners are unaffected.
"""

from app.db import get_db
from app.users import create_local_user

_PW = "password123"


def _mk(db, email, *, role="user"):
    uid = create_local_user(email, _PW)
    db.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
    db.commit()
    return uid


def _ci_group(db, owner_id, *, name, mine_brand):
    db.execute(
        "INSERT INTO ci_groups (user_id, name, mode, monitoring_enabled) "
        "VALUES (?, ?, 'monitoring', 1)",
        (owner_id, name),
    )
    gid = db.execute(
        "SELECT id FROM ci_groups WHERE user_id = ? AND name = ?", (owner_id, name)
    ).fetchone()["id"]
    db.execute("INSERT INTO ci_brands (group_id, name, type) VALUES (?, ?, 'mine')",
               (gid, mine_brand))
    db.commit()
    return gid


def _grant(db, owner_id, grantee_id, scope_type, scope_ref=None):
    db.execute(
        "INSERT INTO portfolio_grants (owner_user_id, grantee_user_id, "
        "scope_type, scope_ref) VALUES (?, ?, ?, ?)",
        (owner_id, grantee_id, scope_type, scope_ref),
    )
    db.commit()


def _setup_owner_two_brands_and_helper(app):
    """Owner (admin) with a Tabasco + a Ritz monitoring group; helper granted Tabasco."""
    with app.app_context():
        db = get_db()
        owner = _mk(db, "mgr@example.com", role="admin")
        tab = _ci_group(db, owner, name="Hot Sauce", mine_brand="Tabasco")
        ritz = _ci_group(db, owner, name="Crackers", mine_brand="Ritz")
        helper = _mk(db, "help@example.com")
        _grant(db, owner, helper, "brand", "Tabasco")
        return {"owner": owner, "helper": helper, "tab": tab, "ritz": ritz}


# --- Insights --------------------------------------------------------------

def test_helper_performance_shows_only_granted_brand(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="help@example.com", password=_PW)
    body = client.get("/app/insights/performance").data
    assert b"Tabasco" in body       # granted brand's group is selectable
    assert b"Ritz" not in body      # the owner's other brand is not


def test_helper_competitive_shows_only_granted_brand(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="help@example.com", password=_PW)
    body = client.get("/app/insights/competitive").data
    assert b"Tabasco" in body
    assert b"Ritz" not in body


def test_owner_still_sees_all_their_brands(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="mgr@example.com", password=_PW)
    body = client.get("/app/insights/performance").data
    assert b"Tabasco" in body
    assert b"Ritz" in body          # owner is unaffected


# --- Reports ---------------------------------------------------------------

def test_helper_reports_create_scoped_and_no_schedule_tab(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="help@example.com", password=_PW)
    body = client.get("/app/reports").data
    assert b"Tabasco" in body
    assert b"Ritz" not in body
    assert b"Schedules" not in body   # scheduling is owner-only


def test_helper_cannot_schedule_reports(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="help@example.com", password=_PW)
    assert client.post("/app/reports/schedules",
                       data={"group_id": "1", "cadence": "weekly"}).status_code == 403


def test_helper_report_generate_blocked_for_ungranted_brand(client, auth, app):
    ids = _setup_owner_two_brands_and_helper(app)
    auth.login(email="help@example.com", password=_PW)
    resp = client.post("/app/reports/generate",
                       data={"group_id": str(ids["ritz"]), "fmt": "pdf", "period": "weekly"})
    assert resp.status_code == 404   # Ritz group is outside the helper's grant


def test_owner_can_reach_schedule_tab(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="mgr@example.com", password=_PW)
    body = client.get("/app/reports").data
    assert b"Schedules" in body      # owner keeps scheduling


# --- Dashboard -------------------------------------------------------------

def test_helper_dashboard_renders_with_granted_brand(client, auth, app):
    _setup_owner_two_brands_and_helper(app)
    auth.login(email="help@example.com", password=_PW)
    resp = client.get("/app")
    assert resp.status_code == 200
    assert b"Tabasco" in resp.data
    assert b"Ritz" not in resp.data
