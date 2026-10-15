"""Tests for the Business Report feature: data assembly, storage, renderers, routes.

Covers app.report (assembly + grading + rule-based highlights/recs), app.jobs's
new readiness_dimension_averages, app.report_store (file + row CRUD, IDOR, path
confinement), the two renderers (app.report_pdf / app.report_pptx build valid
bytes), and the /app/reports routes (auth, IDOR, format/period validation,
generate → stream + record, download, delete).
"""

import io
import json
import os
import zipfile

import pytest

from app import ci_analysis, ci_config, ci_jobs, jobs, report, report_store
from app.db import get_db
from app.users import create_local_user


# ── Seeding helpers ──────────────────────────────────────────────────────────

def _uid(db, email="user@example.com"):
    """The id of a user created via the auth flow (lookup by email)."""
    return db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def _scored(db, uid, item_id, title, brand, t, f, d, img):
    """Insert a scored product with primary-dimension detail."""
    overall = round((t + f + d + img) / 4)
    result = {"overall": overall, "dimensions": [
        {"key": "imagery", "label": "Imagery", "score": img, "weight": 25, "available": True},
        {"key": "title", "label": "Title", "score": t, "weight": 25, "available": True},
        {"key": "key_features", "label": "Key Features", "score": f, "weight": 30, "available": True},
        {"key": "description", "label": "Description", "score": d, "weight": 20, "available": True},
    ]}
    db.execute(
        "INSERT INTO scored_items (user_id, item_id, url, title, brand, status, overall, result_json) "
        "VALUES (?,?,?,?,?,'scored',?,?)",
        (uid, item_id, f"https://www.walmart.com/ip/x/{item_id}", title, brand, overall,
         json.dumps(result)))


def _seed_monitoring(db, uid, *, with_ci=True, with_scores=True):
    """A monitoring group with (optionally) a completed period of CI data + scores.

    Returns the group id. CI data lands in the last completed WEEK relative to the
    real today, so assemble_business_report (which uses today=None) sees it.
    """
    gid = ci_config.create_group(db, uid, "Tabasco", mode="monitoring")
    mine = ci_config.add_brand(db, gid, uid, "Tabasco", "mine")
    comp = ci_config.add_brand(db, gid, uid, "Frank's RedHot", "competitor")
    items = [("AAA111", "Tabasco Original 5 oz"), ("BBB222", "Tabasco Chipotle 5 oz")]
    for iid, name in items:
        db.execute(
            "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url) "
            "VALUES (?,?,?,?,?)", (gid, mine, name, iid, f"https://www.walmart.com/ip/x/{iid}"))
    kids = [ci_config.add_keyword(db, gid, uid, k) for k in ("hot sauce", "pepper sauce")]
    rid = ci_jobs.enqueue_run(db, gid)

    if with_ci:
        cur_s, _ = ci_analysis.period_bounds("wow", 0)
        pri_s, _ = ci_analysis.period_bounds("wow", 1)
        # keyword 0: mine ranks + holds shelf; keyword 1: mine absent, competitor leads.
        db.execute("INSERT INTO ci_search_results (run_id, group_id, keyword_id, scraped_at, "
                   "position, position_type, item_id, brand_id, is_new_sku) VALUES (?,?,?,?,?,?,?,?,0)",
                   (rid, gid, kids[0], cur_s, 3, "organic", "AAA111", mine))
        db.execute("INSERT INTO ci_search_results (run_id, group_id, keyword_id, scraped_at, "
                   "position, position_type, item_id, brand_id, is_new_sku) VALUES (?,?,?,?,?,?,?,?,0)",
                   (rid, gid, kids[0], pri_s, 6, "organic", "AAA111", mine))
        for kid in kids:
            db.execute("INSERT INTO ci_search_results (run_id, group_id, keyword_id, scraped_at, "
                       "position, position_type, item_id, brand_id, is_new_sku) VALUES (?,?,?,?,?,?,?,?,0)",
                       (rid, gid, kid, cur_s, 2, "organic", "COMP1", comp))
        for kid, (mo, ms) in zip(kids, [(4, 2), (0, 0)]):
            db.execute("INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
                       "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
                       (rid, gid, kid, cur_s, None, mine, mo, ms, mo + ms))
            db.execute("INSERT INTO ci_share_of_search (run_id, group_id, keyword_id, date, slot, "
                       "brand_id, organic_count, sponsored_count, total_count) VALUES (?,?,?,?,?,?,?,?,?)",
                       (rid, gid, kid, cur_s, None, comp, 5, 1, 6))

    if with_scores:
        _scored(db, uid, "AAA111", "Tabasco Original 5 oz", "Tabasco", 92, 88, 95, 90)
        _scored(db, uid, "BBB222", "Tabasco Chipotle 5 oz", "Tabasco", 70, 60, 48, 85)
    db.commit()
    return gid


# ── grade() ──────────────────────────────────────────────────────────────────

def test_grade_bands():
    assert report.grade(95)["label"] == "Excellent"
    assert report.grade(80)["label"] == "Good"       # pinned to READINESS_STRONG_MIN
    assert report.grade(79)["label"] == "Needs Attention"
    assert report.grade(40)["label"] == "Critical"
    assert report.grade(None) == {"label": "n/a", "tone": "muted"}


# ── readiness_dimension_averages ─────────────────────────────────────────────

def test_dimension_averages(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("dim@example.com", "password123")
        _scored(db, uid, "X1", "A", "Tabasco", 90, 80, 60, 100)
        _scored(db, uid, "X2", "B", "Tabasco", 80, 60, 40, 80)
        db.commit()
        dims = jobs.readiness_dimension_averages(db, uid, ["Tabasco"])
        by = {d["key"]: d["avg"] for d in dims}
        assert by == {"title": 85, "key_features": 70, "description": 50, "imagery": 90}
        # Order is the fixed primary order; secondary content never appears.
        assert [d["key"] for d in dims] == list(jobs.PRIMARY_DIMENSION_ORDER)
        assert jobs.readiness_dimension_averages(db, uid, []) == []


# ── assemble_business_report ─────────────────────────────────────────────────

def test_assemble_no_data(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("nd@example.com", "password123")
        gid = ci_config.create_group(db, uid, "Empty", mode="monitoring")
        data = report.assemble_business_report(db, uid, ci_config.get_group(db, gid, uid), "wow")
        assert data["meta"]["has_data"] is False
        assert all(not c["available"] for c in data["cards"].values())
        assert data["readiness"]["available"] is False
        # Always builds a sensible fallback rather than empty lists.
        assert data["highlights"]["wins"] and data["highlights"]["attention"]
        assert data["recommendations"]
        # Secondary content / reviews never present.
        assert "reviews" not in data and "secondary" not in data


def test_assemble_populated(app):
    with app.app_context():
        db = get_db()
        uid = create_local_user("pop@example.com", "password123")
        gid = _seed_monitoring(db, uid)
        data = report.assemble_business_report(db, uid, ci_config.get_group(db, gid, uid), "wow")
        assert data["meta"]["has_data"] is True
        assert data["cards"]["avg_rank"]["available"]
        assert data["cards"]["shelf_share"]["available"]
        assert data["cards"]["content_readiness"]["available"]
        # Readiness scorecard is primary dimensions only.
        assert [d["key"] for d in data["readiness"]["dimensions"]] == list(jobs.PRIMARY_DIMENSION_ORDER)
        # The weak product (Chipotle: description 48) drives a priority fix + recs.
        assert any(f["impact"] == "High" for f in data["priority_fixes"])
        assert any(r["priority"] == "High" for r in data["recommendations"])
        # "pepper sauce" has no mine ranking → flagged as a no-rank keyword.
        assert "pepper sauce" in data["discovery"]["no_rank_keywords"]


# ── Renderers ────────────────────────────────────────────────────────────────

def _data(app, **kw):
    with app.app_context():
        db = get_db()
        uid = create_local_user(kw.pop("email", "r@example.com"), "password123")
        gid = _seed_monitoring(db, uid, **kw)
        return report.assemble_business_report(db, uid, ci_config.get_group(db, gid, uid), "wow")


def test_pdf_builds(app):
    from app.report_pdf import build_business_report_pdf
    pdf = build_business_report_pdf(_data(app))
    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 5000


def test_pdf_builds_no_data(app):
    from app.report_pdf import build_business_report_pdf
    with app.app_context():
        db = get_db()
        uid = create_local_user("pdfnd@example.com", "password123")
        gid = ci_config.create_group(db, uid, "Empty", mode="monitoring")
        data = report.assemble_business_report(db, uid, ci_config.get_group(db, gid, uid), "wow")
    assert build_business_report_pdf(data)[:5] == b"%PDF-"


def test_pptx_builds(app):
    from app.report_pptx import build_business_report_pptx
    pptx = build_business_report_pptx(_data(app, email="rp@example.com"))
    # A .pptx is a zip; it must contain the presentation part.
    assert zipfile.is_zipfile(io.BytesIO(pptx))
    with zipfile.ZipFile(io.BytesIO(pptx)) as z:
        assert "ppt/presentation.xml" in z.namelist()


# ── report_store ─────────────────────────────────────────────────────────────

@pytest.fixture
def media(tmp_path, monkeypatch):
    """Point the media root at a clean temp dir for storage tests."""
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    return tmp_path


def test_store_save_get_list_delete(app, media):
    with app.app_context():
        db = get_db()
        uid = create_local_user("st@example.com", "password123")
        rid = report_store.save_report(
            db, user_id=uid, group_id=None, brand_label="Tabasco", period="wow",
            period_label="This week", fmt="pdf", data=b"%PDF-xyz", filename="r.pdf")
        row = report_store.get_report(db, rid, uid)
        assert row["brand_label"] == "Tabasco" and row["size_bytes"] == 8
        # File exists on disk under the media root.
        assert os.path.isfile(report_store.abspath(row["rel_path"]))
        assert len(report_store.list_reports(db, uid)) == 1
        # Delete removes both row and file.
        assert report_store.delete_report(db, rid, uid) is True
        assert report_store.get_report(db, rid, uid) is None


def test_store_idor(app, media):
    with app.app_context():
        db = get_db()
        owner = create_local_user("owner@example.com", "password123")
        other = create_local_user("other@example.com", "password123")
        rid = report_store.save_report(
            db, user_id=owner, group_id=None, brand_label="B", period="wow",
            period_label=None, fmt="pdf", data=b"x", filename="r.pdf")
        # Another user can neither read nor delete it.
        assert report_store.get_report(db, rid, other) is None
        assert report_store.delete_report(db, rid, other) is False
        assert report_store.get_report(db, rid, owner) is not None


def test_store_rejects_bad_format(app, media):
    with app.app_context():
        db = get_db()
        uid = create_local_user("bad@example.com", "password123")
        with pytest.raises(ValueError):
            report_store.save_report(db, user_id=uid, group_id=None, brand_label="B",
                                     period="wow", period_label=None, fmt="exe",
                                     data=b"x", filename="r.exe")


def test_store_abspath_confinement():
    # A tampered relative path escaping the media root resolves to None.
    assert report_store.abspath("../../etc/passwd") is None
    assert report_store.abspath("reports/1/../../../../etc/passwd") is None


# ── Routes ───────────────────────────────────────────────────────────────────

def test_reports_requires_login(client):
    resp = client.get("/app/reports")
    assert resp.status_code in (301, 302)
    assert "/signin" in resp.headers["Location"]


def test_reports_home_tabs(client, auth):
    auth.register()
    assert client.get("/app/reports").status_code == 200
    assert client.get("/app/reports?tab=history").status_code == 200
    # An unknown tab falls back to create, not an error.
    assert client.get("/app/reports?tab=bogus").status_code == 200


def test_generate_and_history(client, auth, app, media):
    auth.register()
    with app.app_context():
        gid = _seed_monitoring(get_db(), _uid(get_db()))
    # Generate a PDF — streamed as a download and recorded in History.
    resp = client.post("/app/reports/generate",
                       data={"group_id": gid, "period": "wow", "fmt": "pdf"})
    assert resp.status_code == 200
    assert resp.mimetype == "application/pdf"
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.data[:5] == b"%PDF-"
    # History now lists it with a working download link.
    with app.app_context():
        reports = report_store.list_reports(get_db(), _uid(get_db()))
        assert len(reports) == 1 and reports[0]["fmt"] == "pdf"
        rid = reports[0]["id"]
    dl = client.get(f"/app/reports/download/{rid}")
    assert dl.status_code == 200 and dl.data[:5] == b"%PDF-"


def test_generate_pptx(client, auth, app, media):
    auth.register()
    with app.app_context():
        gid = _seed_monitoring(get_db(), _uid(get_db()))
    resp = client.post("/app/reports/generate",
                       data={"group_id": gid, "period": "wow", "fmt": "pptx"})
    assert resp.status_code == 200
    assert "presentationml" in resp.mimetype
    assert zipfile.is_zipfile(io.BytesIO(resp.data))


def test_generate_idor_and_bad_format(client, auth, app, media):
    auth.register()
    # A forged group id (belongs to no one here) → 404, not a leak.
    assert client.post("/app/reports/generate",
                       data={"group_id": 9999, "period": "wow", "fmt": "pdf"}).status_code == 404
    with app.app_context():
        gid = _seed_monitoring(get_db(), _uid(get_db()))
    # A bad format is rejected before any work.
    assert client.post("/app/reports/generate",
                       data={"group_id": gid, "period": "wow", "fmt": "exe"}).status_code == 400


def test_generate_rejects_other_users_group(client, auth, app, media):
    # A group owned by someone else must 404 for the logged-in user (IDOR).
    with app.app_context():
        other = create_local_user("stranger@example.com", "password123")
        gid = _seed_monitoring(get_db(), other)
    auth.register()  # logs in as user@example.com
    assert client.post("/app/reports/generate",
                       data={"group_id": gid, "period": "wow", "fmt": "pdf"}).status_code == 404


def test_download_delete_idor(client, auth, app, media):
    auth.register()
    with app.app_context():
        db = get_db()
        mine = report_store.save_report(db, user_id=_uid(db), group_id=None, brand_label="B",
                                        period="wow", period_label=None, fmt="pdf",
                                        data=b"%PDF-x", filename="r.pdf")
        stranger = create_local_user("x@example.com", "password123")
        theirs = report_store.save_report(db, user_id=stranger, group_id=None, brand_label="B",
                                          period="wow", period_label=None, fmt="pdf",
                                          data=b"%PDF-y", filename="r.pdf")
    # Can't download another user's report.
    assert client.get(f"/app/reports/download/{theirs}").status_code == 404
    assert client.get(f"/app/reports/download/{mine}").status_code == 200
    # Deleting another user's report is a no-op (redirect, row survives).
    client.post(f"/app/reports/{theirs}/delete")
    with app.app_context():
        assert report_store.get_report(get_db(), theirs, _uid(get_db(), "x@example.com")) is not None
    # Deleting own report works.
    client.post(f"/app/reports/{mine}/delete")
    with app.app_context():
        assert report_store.get_report(get_db(), mine, _uid(get_db())) is None
