"""HTTP-layer tests for the Competitive Intelligence pages.

Complements the module-level tests (test_ci_config/jobs/analysis): here we go
through the routes to check auth guards, that pages render, that the full config
flow works over HTTP, and that one user cannot reach or mutate another's group
(IDOR) via the route layer.
"""

from app import ci_config, ci_jobs
from app.db import get_db


def _make_group(app, email):
    """Create a user + group directly and return (user_id, group_id)."""
    from app.users import create_local_user
    with app.app_context():
        uid = create_local_user(email, "password123")
        gid = ci_config.create_group(get_db(), uid, "G")
        return uid, gid


def test_ci_requires_login(client):
    # Guarded: an anonymous request is redirected to sign-in, not served.
    resp = client.get("/app/competitive-intel")
    assert resp.status_code in (301, 302)
    assert "/signin" in resp.headers["Location"]


def test_groups_page_and_create_flow(client, auth):
    auth.register()
    assert client.get("/app/competitive-intel").status_code == 200

    # Create a group -> redirect to its config screen.
    resp = client.post("/app/competitive-intel/groups",
                       data={"name": "Hot Sauce", "description": "line"})
    assert resp.status_code == 302
    gid = int(resp.headers["Location"].rstrip("/").split("/")[-1])

    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert cfg.status_code == 200
    assert b"Hot Sauce" in cfg.data


def test_full_config_flow_over_http(client, auth):
    auth.register()
    gid = int(client.post("/app/competitive-intel/groups",
              data={"name": "G"}).headers["Location"].rstrip("/").split("/")[-1])

    client.post(f"/app/competitive-intel/groups/{gid}/brands",
                data={"name": "Tabasco", "type": "mine"})
    # Grab the brand id to add a product under it.
    with client.application.app_context():
        db = get_db()
        # The test client's user is user_id 1 (first registered).
        brands = ci_config.list_brands(db, gid, 1)
        bid = brands[0]["id"]

    client.post(f"/app/competitive-intel/groups/{gid}/products",
                data={"brand_id": bid, "url": "https://www.walmart.com/ip/x/10294528"})
    client.post(f"/app/competitive-intel/groups/{gid}/keywords",
                data={"keyword": "hot sauce"})

    cfg = client.get(f"/app/competitive-intel/groups/{gid}")
    assert b"Tabasco" in cfg.data
    assert b"10294528" in cfg.data
    assert b"hot sauce" in cfg.data


def test_run_now_enqueues_and_status_endpoint(client, auth):
    auth.register()
    gid = int(client.post("/app/competitive-intel/groups",
              data={"name": "G"}).headers["Location"].rstrip("/").split("/")[-1])
    client.post(f"/app/competitive-intel/groups/{gid}/run")
    status = client.get(f"/app/competitive-intel/groups/{gid}/status").get_json()
    assert status["status"] == "queued"
    assert status["run_type"] == "one_time"


def test_dashboard_renders_with_period(client, auth):
    auth.register()
    gid = int(client.post("/app/competitive-intel/groups",
              data={"name": "G"}).headers["Location"].rstrip("/").split("/")[-1])
    resp = client.get(f"/app/competitive-intel/groups/{gid}/dashboard?period=mom")
    assert resp.status_code == 200
    assert b"Share of Digital Shelf" in resp.data


def test_idor_cannot_reach_or_mutate_another_users_group(client, auth, app):
    # A group owned by someone else...
    _other_uid, other_gid = _make_group(app, "owner@example.com")
    # ...is invisible/untouchable to the signed-in user.
    auth.register(email="intruder@example.com")

    assert client.get(f"/app/competitive-intel/groups/{other_gid}").status_code == 404
    assert client.get(f"/app/competitive-intel/groups/{other_gid}/dashboard").status_code == 404
    assert client.get(f"/app/competitive-intel/groups/{other_gid}/status").status_code == 404
    # A mutation attempt doesn't create anything on the victim's group.
    client.post(f"/app/competitive-intel/groups/{other_gid}/keywords",
                data={"keyword": "sneaky"})
    with app.app_context():
        assert get_db().execute(
            "SELECT COUNT(*) FROM ci_keywords WHERE group_id=?", (other_gid,)
        ).fetchone()[0] == 0
    # Running a scrape on the victim's group is refused too.
    assert client.post(f"/app/competitive-intel/groups/{other_gid}/run").status_code == 404
    with app.app_context():
        assert not ci_jobs.has_active_run(get_db(), other_gid)
