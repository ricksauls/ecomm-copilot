"""Route tests for the PDP Image Set Creation screens (intake → cutout → gallery)."""

import io

from PIL import Image, ImageDraw

from app.db import get_db
from app.imageset import generate
from app.imageset import store as isstore


def _png() -> bytes:
    img = Image.new("RGB", (300, 480), (255, 255, 255))
    ImageDraw.Draw(img).rounded_rectangle([110, 70, 190, 430], radius=20, fill=(190, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _intake_data(**over):
    data = {
        "name": "Bug Spray", "brand": "Tabasco", "category": "insect repellent",
        "environments": "a patio, a trail", "feature_title": "DEET-free",
        "height": "7.8", "width": "2.2", "unit": "in", "weight": "6 oz",
        "photo": (io.BytesIO(_png()), "product.png"),
    }
    data.update(over)
    return data


def _submit_intake(client, **over):
    return client.post("/app/pdp-image-set", data=_intake_data(**over),
                       content_type="multipart/form-data")


def _run_plan(db):
    """Simulate the worker's plan step: claim the queued project, plan + enqueue it."""
    import worker

    project = isstore.claim_next_plan(db)
    assert project is not None
    worker.process_imageset_plan_one(db, project)


def test_intake_requires_login(client):
    # Unauthenticated → redirected to sign in (login_required).
    resp = client.get("/app/pdp-image-set")
    assert resp.status_code in (302, 401)


def test_intake_get_renders(client, auth):
    auth.register()
    resp = client.get("/app/pdp-image-set")
    assert resp.status_code == 200
    assert b"Create a Product Detail Page Image Set" in resp.data


def test_intake_post_creates_project_and_cutout(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    resp = _submit_intake(client)
    assert resp.status_code == 302
    assert "/cutout" in resp.headers["Location"]


def test_intake_post_requires_name_and_category(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    resp = _submit_intake(client, category="")
    assert resp.status_code == 400


def test_intake_post_rejects_non_image(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    resp = client.post("/app/pdp-image-set", content_type="multipart/form-data",
                       data=_intake_data(photo=(io.BytesIO(b"not a real image"), "x.png")))
    assert resp.status_code == 400


def test_cutout_page_and_image_serve(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        pid = isstore.list_projects(get_db(), 1)[0]["id"]
    page = client.get(f"/app/pdp-image-set/{pid}/cutout")
    assert page.status_code == 200 and b"Review the product cutout" in page.data
    img = client.get(f"/app/pdp-image-set/{pid}/cutout-image")
    assert img.status_code == 200 and img.mimetype == "image/png"


def test_approve_enqueues_and_redirects_to_gallery(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        db = get_db()
        pid = isstore.list_projects(db, 1)[0]["id"]
    resp = client.post(f"/app/pdp-image-set/{pid}/approve")
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/pdp-image-set/{pid}")
    with client.application.app_context():
        db = get_db()
        # Approve only queues planning (the worker plans) — no assets/jobs yet.
        assert isstore.get_project(db, pid, 1)["status"] == isstore.STATUS_PLAN_QUEUED
        _run_plan(db)
        project = isstore.get_project(db, pid, 1)
        assert project["status"] == isstore.STATUS_GENERATING
        # All 7 variations are implemented (2 lifestyle + 2 feature + 2 PIU + 1 size).
        assert db.execute("SELECT COUNT(*) FROM imageset_jobs WHERE project_id = ?", (pid,)).fetchone()[0] == 7


def test_gallery_and_status(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        pid = isstore.list_projects(get_db(), 1)[0]["id"]
    client.post(f"/app/pdp-image-set/{pid}/approve")
    # Right after approve the project is planning (no assets yet) — gallery still 200.
    gallery = client.get(f"/app/pdp-image-set/{pid}")
    assert gallery.status_code == 200 and b"Planning your image set" in gallery.data
    planning_status = client.get(f"/app/pdp-image-set/{pid}/status").get_json()
    assert planning_status["planning"] is True and planning_status["pending"] is True
    # After the worker plans, assets appear in the status feed.
    with client.application.app_context():
        _run_plan(get_db())
    body = client.get(f"/app/pdp-image-set/{pid}/status").get_json()
    assert body["planning"] is False and isinstance(body["assets"], list) and body["assets"]


def test_asset_image_and_zip_after_generation(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        db = get_db()
        pid = isstore.list_projects(db, 1)[0]["id"]
    client.post(f"/app/pdp-image-set/{pid}/approve")
    with client.application.app_context():
        db = get_db()
        _run_plan(db)  # worker plans + creates the assets
        project = isstore.get_project(db, pid, 1)
        sc = next(a for a in isstore.assets_for_project(db, pid, 1)
                  if a["asset_type"] == "SIZE_COMPARISON")
        generate.process_asset(db, sc, project)  # produce a real final
        aid = sc["id"]
    img = client.get(f"/app/pdp-image-set/{pid}/asset/{aid}/image")
    assert img.status_code == 200 and img.mimetype == "image/png"
    zresp = client.get(f"/app/pdp-image-set/{pid}/download.zip")
    assert zresp.status_code == 200 and zresp.mimetype == "application/zip"


def _ready_asset(client, pid, asset_type="SIZE_COMPARISON"):
    """Approve, plan, and generate one asset to 'ready'; return its id."""
    client.post(f"/app/pdp-image-set/{pid}/approve")
    with client.application.app_context():
        db = get_db()
        _run_plan(db)
        project = isstore.get_project(db, pid, 1)
        a = next(x for x in isstore.assets_for_project(db, pid, 1)
                 if x["asset_type"] == asset_type)
        generate.process_asset(db, a, project)
        return a["id"]


def test_regenerate_asset_requeues(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        pid = isstore.list_projects(get_db(), 1)[0]["id"]
    aid = _ready_asset(client, pid)
    resp = client.post(f"/app/pdp-image-set/{pid}/asset/{aid}/regenerate")
    assert resp.status_code == 302
    with client.application.app_context():
        db = get_db()
        a = isstore.get_asset(db, aid, 1)
        assert a["status"] == isstore.STATUS_DRAFT and a["final_path"] is None
        # A queued job exists again and the project is back to generating.
        assert isstore.get_project(db, pid, 1)["status"] == isstore.STATUS_GENERATING
        assert db.execute("SELECT status FROM imageset_jobs WHERE asset_id = ?",
                          (aid,)).fetchone()["status"] == "queued"


def test_keep_toggle_and_zip_excludes_discarded(client, auth, tmp_path, monkeypatch):
    import io
    import zipfile

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        pid = isstore.list_projects(get_db(), 1)[0]["id"]
    aid = _ready_asset(client, pid)
    # Discard it.
    client.post(f"/app/pdp-image-set/{pid}/asset/{aid}/keep")
    with client.application.app_context():
        assert isstore.get_asset(get_db(), aid, 1)["kept"] == 0
    # The only ready image is discarded → ZIP has nothing to bundle (404).
    assert client.get(f"/app/pdp-image-set/{pid}/download.zip").status_code == 404
    # Keep it again → back in the ZIP.
    client.post(f"/app/pdp-image-set/{pid}/asset/{aid}/keep")
    with client.application.app_context():
        assert isstore.get_asset(get_db(), aid, 1)["kept"] == 1
    z = client.get(f"/app/pdp-image-set/{pid}/download.zip")
    assert z.status_code == 200 and zipfile.ZipFile(io.BytesIO(z.data)).namelist()


def test_download_names_use_walmart_item_number(client, auth, tmp_path, monkeypatch):
    """Asset + ZIP downloads are named product-<item>-<type>-<n>, foldered in the ZIP."""
    import io
    import zipfile

    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        db = get_db()
        pid = isstore.list_projects(db, 1)[0]["id"]
        # Give the project a Walmart source URL so the item number drives the name.
        db.execute("UPDATE imageset_projects SET source_url = ? WHERE id = ?",
                   ("https://www.walmart.com/ip/off-deep-woods/10294528", pid))
        db.commit()
    client.post(f"/app/pdp-image-set/{pid}/approve")
    with client.application.app_context():
        db = get_db()
        _run_plan(db)
        project = isstore.get_project(db, pid, 1)
        sc = next(a for a in isstore.assets_for_project(db, pid, 1)
                  if a["asset_type"] == "SIZE_COMPARISON")
        generate.process_asset(db, sc, project)
        aid = sc["id"]
    dl = client.get(f"/app/pdp-image-set/{pid}/asset/{aid}/download")
    assert dl.status_code == 200
    assert "product-10294528-size_comparison-1.png" in dl.headers["Content-Disposition"]
    zresp = client.get(f"/app/pdp-image-set/{pid}/download.zip")
    assert "product-10294528-image-set.zip" in zresp.headers["Content-Disposition"]
    names = zipfile.ZipFile(io.BytesIO(zresp.data)).namelist()
    assert all(n.startswith("product-10294528/") for n in names)


def test_download_name_falls_back_to_project_id(client, auth, tmp_path, monkeypatch):
    """A manually-uploaded product (no Walmart URL) falls back to proj<id> naming."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        db = get_db()
        pid = isstore.list_projects(db, 1)[0]["id"]
    client.post(f"/app/pdp-image-set/{pid}/approve")
    with client.application.app_context():
        db = get_db()
        _run_plan(db)
        project = isstore.get_project(db, pid, 1)
        sc = next(a for a in isstore.assets_for_project(db, pid, 1)
                  if a["asset_type"] == "SIZE_COMPARISON")
        generate.process_asset(db, sc, project)
        aid = sc["id"]
    dl = client.get(f"/app/pdp-image-set/{pid}/asset/{aid}/download")
    assert f"product-proj{pid}-size_comparison-1.png" in dl.headers["Content-Disposition"]


def test_approve_stores_selected_types(client, auth, tmp_path, monkeypatch):
    """Ticked types are persisted and gate which assets get queued."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()
    _submit_intake(client)
    with client.application.app_context():
        db = get_db()
        pid = isstore.list_projects(db, 1)[0]["id"]
    # Tick two specific variations: Feature 1 and Size Comparison.
    client.post(f"/app/pdp-image-set/{pid}/approve",
                data={"types": ["FEATURE_CALLOUT:1", "SIZE_COMPARISON:1"]})
    with client.application.app_context():
        db = get_db()
        _run_plan(db)
        # Only the two selected variations were queued.
        assert db.execute("SELECT COUNT(*) FROM imageset_jobs WHERE project_id = ?",
                          (pid,)).fetchone()[0] == 2


def test_idor_other_user_cannot_access(client, auth, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    auth.register()  # user 1
    _submit_intake(client)
    with client.application.app_context():
        pid = isstore.list_projects(get_db(), 1)[0]["id"]
    auth.logout()
    auth.register(email="other@example.com")  # user 2
    assert client.get(f"/app/pdp-image-set/{pid}/cutout").status_code == 404
    assert client.get(f"/app/pdp-image-set/{pid}").status_code == 404
    assert client.get(f"/app/pdp-image-set/{pid}/cutout-image").status_code == 404
