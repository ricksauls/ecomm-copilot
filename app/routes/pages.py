"""Page routes for the public surface and the authenticated workspace.

Three routes in this pass: the marketing landing page and sign-in page (public,
dark surface) and the agency dashboard (workspace, light surface). The remaining
workspace screens (product, analysis, creative, share of shelf) are designed in
the handoff but not yet built.

Sign-in / sign-up / sign-out live in the ``auth`` blueprint. The dashboard is
guarded by ``login_required``, so it is no longer world-reachable.
"""

import json
import logging

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from app import (
    ci_analysis,
    ci_config,
    ci_jobs,
    copy_jobs,
    fixtures,
    jobs,
    pdp,
    users,
)
from app.db import get_db
from app.security import admin_required, login_required

logger = logging.getLogger(__name__)

# Session keys holding the ids of the most recent scoring / copy batches.
_BATCH_KEY = "pdp_batch_ids"
_COPY_BATCH_KEY = "pdp_copy_batch_ids"

bp = Blueprint("pages", __name__)


@bp.route("/")
def landing():
    """Marketing landing page. Public, dark surface."""
    logger.info("Serving landing page")
    return render_template("landing.html")


@bp.route("/app")
@login_required
def dashboard():
    """Agency dashboard. Authenticated workspace, light surface.

    Guarded by ``login_required``: an unauthenticated request is redirected to
    the sign-in page rather than served.
    """
    logger.info("Serving dashboard")
    view_model = fixtures.get_dashboard()
    return render_template(
        "app/dashboard.html",
        breadcrumb="Meridian Commerce Group",
        active_nav="dashboard",
        **view_model,
    )


@bp.route("/app/pdp-scoring", methods=["GET", "POST"])
@login_required
def pdp_scoring():
    """PDP Content Scoring intake: collect item URLs and enqueue them to score.

    A POST validates the submitted URLs / CSV, enqueues the accepted items as
    scoring jobs, and redirects to the results page (which polls for progress).
    The background worker does the actual fetch + score.
    """
    if request.method == "POST":
        form_urls = request.form.getlist("urls")
        csv_file = request.files.get("csv")
        accepted, rejected = pdp.collect_items(form_urls, csv_file)

        if not accepted:
            # Nothing usable — re-render the form with a message instead of
            # enqueuing an empty batch.
            return (
                render_template(
                    "app/pdp_scoring.html",
                    breadcrumb="PDP Content Scoring",
                    active_nav="pdp-scoring",
                    submitted=False,
                    max_items=pdp.MAX_ITEMS,
                    error="No valid item URLs were provided.",
                ),
                400,
            )

        items = [{"url": url, "item": pdp.item_number_from_url(url)} for url in accepted]
        ids = jobs.enqueue_items(get_db(), g.user["id"], items)
        session[_BATCH_KEY] = ids
        logger.info(
            "PDP scoring: enqueued %d item(s), %d rejected, user_id=%s",
            len(ids),
            len(rejected),
            g.user["id"],
        )
        return redirect(url_for("pages.pdp_scoring_results"))

    logger.info("Serving PDP Content Scoring intake")
    return render_template(
        "app/pdp_scoring.html",
        breadcrumb="PDP Content Scoring",
        active_nav="pdp-scoring",
        submitted=False,
        max_items=pdp.MAX_ITEMS,
    )


def _batch_rows():
    """Fetch the current session batch's rows, scoped to the signed-in user."""
    ids = session.get(_BATCH_KEY, [])
    return jobs.get_items(get_db(), ids, g.user["id"])


@bp.route("/app/pdp-scoring/results")
@login_required
def pdp_scoring_results():
    """Show the most recent scoring batch and poll until every item finishes."""
    rows = _batch_rows()
    return render_template(
        "app/pdp_results.html",
        breadcrumb="PDP Content Scoring",
        active_nav="pdp-scoring",
        items=[_row_view(r) for r in rows],
    )


@bp.route("/app/pdp-scoring/results.pdf")
@login_required
def pdp_scoring_results_pdf():
    """Download the current batch's scores as a PDF."""
    from datetime import date

    from flask import Response

    from app.pdf_export import build_results_pdf

    items = [_row_view(r) for r in _batch_rows()]
    pdf = build_results_pdf(items)
    filename = f"pdp-scores-{date.today().isoformat()}.pdf"
    logger.info("PDF export: %d item(s), user_id=%s", len(items), g.user["id"])
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/app/pdp-scoring/status")
@login_required
def pdp_scoring_status():
    """JSON status for the current batch, polled by the results page."""
    rows = _batch_rows()
    items = [_row_view(r) for r in rows]
    pending = any(r["status"] in ("queued", "scoring") for r in items)
    return jsonify({"pending": pending, "items": items})


# --- PDP Copy Content Creation --------------------------------------------


def _copy_batch_rows():
    """Fetch the current session's copy batch rows, scoped to the signed-in user."""
    ids = session.get(_COPY_BATCH_KEY, [])
    return copy_jobs.get_copy_items(get_db(), ids, g.user["id"])


def _copy_row_view(row) -> dict:
    """Shape a copy_items row for templates / JSON.

    Drops the internal ``record`` blob (the full PdpRecord kept only for the
    generation phase) so it never reaches the client.
    """
    current = json.loads(row["current_json"]) if row["current_json"] else None
    if current:
        current.pop("record", None)
    new = json.loads(row["new_json"]) if row["new_json"] else None
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "url": row["url"],
        "title": row["title"],
        "status": row["status"],
        "current": current,
        "current_overall": row["current_overall"],
        "new": new,
        "projected_overall": row["projected_overall"],
        "error": row["error"],
    }


@bp.route("/app/pdp-copy", methods=["GET", "POST"])
@login_required
def pdp_copy():
    """Copy Content Creation intake: collect item URLs, then fetch current copy.

    A POST enqueues the accepted items as copy jobs (fetch-only to start) and
    redirects to the results page, where the user can review the current copy and
    then request new copy. The background worker does the fetch.
    """
    if request.method == "POST":
        form_urls = request.form.getlist("urls")
        csv_file = request.files.get("csv")
        accepted, rejected = pdp.collect_items(form_urls, csv_file)

        if not accepted:
            return (
                render_template(
                    "app/pdp_copy.html",
                    breadcrumb="PDP Copy Content Creation",
                    active_nav="pdp-copy",
                    max_items=pdp.MAX_ITEMS,
                    error="No valid item URLs were provided.",
                ),
                400,
            )

        items = [{"url": url, "item": pdp.item_number_from_url(url)} for url in accepted]
        ids = copy_jobs.enqueue_copy_items(get_db(), g.user["id"], items)
        session[_COPY_BATCH_KEY] = ids
        logger.info(
            "PDP copy: enqueued %d item(s) for current-copy fetch, %d rejected, user_id=%s",
            len(ids), len(rejected), g.user["id"],
        )
        return redirect(url_for("pages.pdp_copy_results"))

    logger.info("Serving PDP Copy Content Creation intake")
    return render_template(
        "app/pdp_copy.html",
        breadcrumb="PDP Copy Content Creation",
        active_nav="pdp-copy",
        max_items=pdp.MAX_ITEMS,
    )


@bp.route("/app/pdp-copy/results")
@login_required
def pdp_copy_results():
    """Show the current copy batch: current copy, and new copy once generated."""
    items = [_copy_row_view(r) for r in _copy_batch_rows()]
    return render_template(
        "app/pdp_copy_results.html",
        breadcrumb="PDP Copy Content Creation",
        active_nav="pdp-copy",
        items=items,
    )


@bp.route("/app/pdp-copy/results.pdf")
@login_required
def pdp_copy_results_pdf():
    """Download the current copy batch's generated copy as a PDF."""
    from datetime import date

    from flask import Response

    from app.pdf_export import build_copy_pdf

    items = [_copy_row_view(r) for r in _copy_batch_rows()]
    pdf = build_copy_pdf(items)
    filename = f"pdp-copy-{date.today().isoformat()}.pdf"
    logger.info("Copy PDF export: %d item(s), user_id=%s", len(items), g.user["id"])
    return Response(
        pdf,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/app/pdp-copy/generate", methods=["POST"])
@login_required
def pdp_copy_generate():
    """Advance the batch's fetched items to generation ("Create new copy content")."""
    ids = session.get(_COPY_BATCH_KEY, [])
    count = copy_jobs.request_generation(get_db(), ids, g.user["id"])
    logger.info("PDP copy: requested generation for %d item(s), user_id=%s", count, g.user["id"])
    return redirect(url_for("pages.pdp_copy_results"))


@bp.route("/app/pdp-copy/status")
@login_required
def pdp_copy_status():
    """JSON status for the current copy batch, for polling."""
    items = [_copy_row_view(r) for r in _copy_batch_rows()]
    pending = any(
        it["status"] in ("queued", "fetching", "gen_queued", "generating") for it in items
    )
    return jsonify({"pending": pending, "items": items})


@bp.route("/app/pdp-scoring/create-copy", methods=["POST"])
@login_required
def pdp_scoring_create_copy():
    """Cross-link from the scoring screen: create copy jobs for selected items.

    Takes the item ids checked on the scoring results page, resolves them to URLs
    (scoped to the user — IDOR guard), and enqueues copy jobs that fetch the
    current copy AND generate new copy in one pass (``auto_generate``). Redirects
    to the copy results page.
    """
    try:
        selected = [int(v) for v in request.form.getlist("item_ids")]
    except ValueError:
        abort(400, description="Invalid item selection.")
    rows = jobs.get_items(get_db(), selected, g.user["id"])
    items = [{"url": r["url"], "item": r["item_id"]} for r in rows if r["url"]]
    if not items:
        # Nothing valid selected — send them back to the scoring results.
        logger.info("PDP copy cross-link: no valid items selected, user_id=%s", g.user["id"])
        return redirect(url_for("pages.pdp_scoring_results"))

    ids = copy_jobs.enqueue_copy_items(get_db(), g.user["id"], items, auto_generate=True)
    session[_COPY_BATCH_KEY] = ids
    logger.info(
        "PDP copy cross-link: enqueued %d item(s) from scoring, user_id=%s",
        len(ids), g.user["id"],
    )
    return redirect(url_for("pages.pdp_copy_results"))


@bp.route("/admin/users")
@admin_required
def admin_users():
    """Admin: table of all registered users."""
    logger.info("Admin users view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_users.html",
        breadcrumb="Admin · Users",
        active_nav="admin-users",
        users=users.list_users(),
    )


@bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    """Delete a user (and their scored items). POST-only + CSRF + admin-guarded.

    Blocks self-deletion so an admin can't remove their own account by accident.
    """
    if user_id == g.user["id"]:
        logger.warning("Admin user_id=%s tried to delete their own account", g.user["id"])
        abort(400, description="You can't delete your own account.")
    users.delete_user(user_id)
    logger.info("Admin user_id=%s deleted user_id=%s", g.user["id"], user_id)
    return redirect(url_for("pages.admin_users"))


@bp.route("/admin/items")
@admin_required
def admin_items():
    """Admin: table of recent scored items across all users."""
    logger.info("Admin items view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_items.html",
        breadcrumb="Admin · Items scored",
        active_nav="admin-items",
        items=jobs.list_items(get_db()),
    )


@bp.route("/admin/copy")
@admin_required
def admin_copy():
    """Admin: table of recent copy-content items across all users."""
    logger.info("Admin copy view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_copy.html",
        breadcrumb="Admin · Copy created",
        active_nav="admin-copy",
        items=copy_jobs.list_copy_items(get_db()),
    )


def _row_view(row) -> dict:
    """Shape a scored_items row for templates / JSON (parses the result blob)."""
    result = json.loads(row["result_json"]) if row["result_json"] else None
    return {
        "id": row["id"],
        "item_id": row["item_id"],
        "url": row["url"],
        "title": row["title"],
        "status": row["status"],
        "overall": row["overall"],
        "error": row["error"],
        "result": result,
    }


# --- Competitive Intelligence ---------------------------------------------
#
# Search Ranking + Share of Digital Shelf. A user manages one or more groups
# (brands -> products, plus keywords), triggers a one-time scrape or opts a group
# into the 3x/day monitoring sweep, and views the dashboards. Every route resolves
# ownership through ci_config (which raises ConfigError / returns None for another
# user's ids), so IDOR is enforced server-side regardless of the ids posted.


def _owned_group_or_404(group_id: int):
    """Return the group row if the signed-in user owns it, else 404."""
    group = ci_config.get_group(get_db(), group_id, g.user["id"])
    if group is None:
        abort(404)
    return group


def _run_status_view(run) -> dict:
    """Shape a ci_runs row for the status JSON polled by the config page."""
    if run is None:
        return {"status": None}
    return {
        "id": run["id"],
        "run_type": run["run_type"],
        "status": run["status"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "error": run["error"],
    }


def _ci_active_nav(mode: str) -> str:
    """Rail slug for a group's mode (config screen highlights its parent menu)."""
    return "ci-monitoring" if mode == "monitoring" else "ci-snapshot"


def _slug(name: str) -> str:
    """Filesystem-safe slug for PDF filenames (alnum kept, else '-')."""
    return ("".join(c if c.isalnum() else "-" for c in (name or "ci")).strip("-").lower() or "ci")[:40]


@bp.route("/app/competitive-intel")
@login_required
def ci_home():
    """Back-compat entry point: send the old CI link to the Snapshot home."""
    return redirect(url_for("pages.ci_snapshot_home"))


# ── One-Time Snapshot ────────────────────────────────────────────────────────

@bp.route("/app/competitive-intel/snapshot")
@login_required
def ci_snapshot_home():
    """Home for one-time snapshot groups: list + create."""
    logger.info("Serving CI snapshot home user_id=%s", g.user["id"])
    return render_template(
        "app/ci_snapshot_home.html",
        breadcrumb="Competitive Intelligence · One-Time Snapshot",
        active_nav="ci-snapshot",
        groups=ci_config.list_groups(get_db(), g.user["id"], mode="snapshot"),
    )


@bp.route("/app/competitive-intel/snapshot/groups", methods=["POST"])
@login_required
def ci_create_snapshot_group():
    """Create a snapshot group and jump to its config screen."""
    try:
        gid = ci_config.create_group(
            get_db(), g.user["id"], request.form.get("name", ""),
            request.form.get("description"), mode="snapshot",
        )
    except ci_config.ConfigError as e:
        flash(str(e), "error")
        return redirect(url_for("pages.ci_snapshot_home"))
    return redirect(url_for("pages.ci_group_config", group_id=gid))


# ── Monitoring Setup ─────────────────────────────────────────────────────────

@bp.route("/app/competitive-intel/monitoring")
@login_required
def ci_monitoring_home():
    """Home for monitoring groups: list (with status) + create + next-run time."""
    logger.info("Serving CI monitoring home user_id=%s", g.user["id"])
    db = get_db()
    return render_template(
        "app/ci_monitoring_home.html",
        breadcrumb="Competitive Intelligence · Monitoring",
        active_nav="ci-monitoring",
        groups=ci_config.list_groups(db, g.user["id"], mode="monitoring"),
        next_run=ci_analysis.next_monitoring_run(),
    )


@bp.route("/app/competitive-intel/monitoring/groups", methods=["POST"])
@login_required
def ci_create_monitoring_group():
    """Create a monitoring group and jump to its config screen."""
    try:
        gid = ci_config.create_group(
            get_db(), g.user["id"], request.form.get("name", ""),
            request.form.get("description"), mode="monitoring",
        )
    except ci_config.ConfigError as e:
        flash(str(e), "error")
        return redirect(url_for("pages.ci_monitoring_home"))
    return redirect(url_for("pages.ci_group_config", group_id=gid))


# ── Shared config screen + group delete ──────────────────────────────────────

@bp.route("/app/competitive-intel/groups/<int:group_id>/delete", methods=["POST"])
@login_required
def ci_delete_group(group_id):
    """Delete a group and all its children; return to its mode's home."""
    db = get_db()
    group = ci_config.get_group(db, group_id, g.user["id"])
    if group is None:
        abort(404)
    mode = group["mode"]
    ci_config.delete_group(db, group_id, g.user["id"])
    home = "pages.ci_monitoring_home" if mode == "monitoring" else "pages.ci_snapshot_home"
    return redirect(url_for(home))


@bp.route("/app/competitive-intel/groups/<int:group_id>")
@login_required
def ci_group_config(group_id):
    """Config screen (shared): brands, products, keywords + a mode-aware action.

    Snapshot groups show "Run snapshot"; monitoring groups show "Schedule & Run"
    plus the next scheduled run time. A help panel explains the setup flow.
    """
    group = _owned_group_or_404(group_id)
    db = get_db()
    return render_template(
        "app/ci_group_config.html",
        breadcrumb=f"Competitive Intelligence · {group['name']}",
        active_nav=_ci_active_nav(group["mode"]),
        group=group,
        brands=ci_config.list_brands(db, group_id, g.user["id"]),
        products=ci_config.list_products(db, group_id, g.user["id"]),
        keywords=ci_config.list_keywords(db, group_id, g.user["id"]),
        latest_run=ci_jobs.latest_run(db, group_id),
        brand_types=ci_config.BRAND_TYPES,
        next_run=ci_analysis.next_monitoring_run(),
    )


def _config_redirect(group_id):
    """Redirect back to a group's config screen (the common post-mutation target)."""
    return redirect(url_for("pages.ci_group_config", group_id=group_id))


@bp.route("/app/competitive-intel/groups/<int:group_id>/brands", methods=["POST"])
@login_required
def ci_add_brand(group_id):
    """Add a brand (mine|competitor) to a group."""
    try:
        ci_config.add_brand(get_db(), group_id, g.user["id"],
                            request.form.get("name", ""), request.form.get("type", ""))
    except ci_config.ConfigError as e:
        flash(str(e), "error")
    return _config_redirect(group_id)


@bp.route("/app/competitive-intel/brands/<int:brand_id>/delete", methods=["POST"])
@login_required
def ci_delete_brand(brand_id):
    """Delete a brand (and its products). group_id posted for the redirect target."""
    try:
        ci_config.delete_brand(get_db(), brand_id, g.user["id"])
    except ci_config.ConfigError as e:
        flash(str(e), "error")
    return _config_redirect(request.form.get("group_id", type=int))


@bp.route("/app/competitive-intel/groups/<int:group_id>/products", methods=["POST"])
@login_required
def ci_add_product(group_id):
    """Add a product under a brand (validates the Walmart URL)."""
    try:
        ci_config.add_product(
            get_db(), group_id, request.form.get("brand_id", type=int), g.user["id"],
            request.form.get("url", ""), request.form.get("name"),
        )
    except ci_config.ConfigError as e:
        flash(str(e), "error")
    return _config_redirect(group_id)


@bp.route("/app/competitive-intel/products/<int:product_id>/delete", methods=["POST"])
@login_required
def ci_delete_product(product_id):
    """Delete a product. group_id posted for the redirect target."""
    try:
        ci_config.delete_product(get_db(), product_id, g.user["id"])
    except ci_config.ConfigError as e:
        flash(str(e), "error")
    return _config_redirect(request.form.get("group_id", type=int))


@bp.route("/app/competitive-intel/groups/<int:group_id>/keywords", methods=["POST"])
@login_required
def ci_add_keyword(group_id):
    """Add a search keyword to a group."""
    try:
        ci_config.add_keyword(get_db(), group_id, g.user["id"], request.form.get("keyword", ""))
    except ci_config.ConfigError as e:
        flash(str(e), "error")
    return _config_redirect(group_id)


@bp.route("/app/competitive-intel/keywords/<int:keyword_id>/delete", methods=["POST"])
@login_required
def ci_delete_keyword(keyword_id):
    """Delete a keyword. group_id posted for the redirect target."""
    try:
        ci_config.delete_keyword(get_db(), keyword_id, g.user["id"])
    except ci_config.ConfigError as e:
        flash(str(e), "error")
    return _config_redirect(request.form.get("group_id", type=int))


@bp.route("/app/competitive-intel/groups/<int:group_id>/status")
@login_required
def ci_run_status(group_id):
    """JSON status of the group's latest run, polled by both run flows."""
    _owned_group_or_404(group_id)
    return jsonify(_run_status_view(ci_jobs.latest_run(get_db(), group_id)))


# ── Snapshot run + results + PDF ─────────────────────────────────────────────

@bp.route("/app/competitive-intel/groups/<int:group_id>/run", methods=["POST"])
@login_required
def ci_run_snapshot(group_id):
    """Enqueue a one-time snapshot scrape and go to the (polling) results page."""
    _owned_group_or_404(group_id)
    db = get_db()
    if ci_jobs.has_active_run(db, group_id):
        flash("A run is already in progress for this group.", "error")
    else:
        ci_jobs.enqueue_run(db, group_id, run_type="one_time")
        flash("Snapshot queued — results appear as the worker finishes.", "ok")
    return redirect(url_for("pages.ci_snapshot_results", group_id=group_id))


def _snapshot_view(db, group_id):
    """Return (run, sos_rows, rank_rows) for a group's latest completed run."""
    run = ci_jobs.latest_run(db, group_id)
    if run and run["status"] == "done":
        return (run,
                ci_analysis.snapshot_share_of_shelf(db, group_id, run["id"]),
                ci_analysis.snapshot_rank(db, group_id, run["id"]))
    return run, [], []


@bp.route("/app/competitive-intel/groups/<int:group_id>/results")
@login_required
def ci_snapshot_results(group_id):
    """Current-state snapshot results (no trends); polls while a run is active."""
    group = _owned_group_or_404(group_id)
    db = get_db()
    run, sos_summary, ranks = _snapshot_view(db, group_id)
    return render_template(
        "app/ci_snapshot_results.html",
        breadcrumb=f"Competitive Intelligence · {group['name']}",
        active_nav="ci-snapshot",
        group=group,
        run=run,
        sos_summary=sos_summary,
        ranks=ranks,
    )


@bp.route("/app/competitive-intel/groups/<int:group_id>/results.pdf")
@login_required
def ci_snapshot_results_pdf(group_id):
    """Download the snapshot's current-state results as a PDF."""
    from datetime import date

    from flask import Response

    from app.pdf_export import build_ci_snapshot_pdf

    group = _owned_group_or_404(group_id)
    db = get_db()
    run, sos_summary, ranks = _snapshot_view(db, group_id)
    if not run or run["status"] != "done":
        abort(404)  # nothing to export yet
    pdf = build_ci_snapshot_pdf(dict(group), sos_summary, ranks)
    filename = f"ci-snapshot-{_slug(group['name'])}-{date.today().isoformat()}.pdf"
    logger.info("CI snapshot PDF: group_id=%s user_id=%s", group_id, g.user["id"])
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ── Monitoring: schedule & run ───────────────────────────────────────────────

@bp.route("/app/competitive-intel/groups/<int:group_id>/schedule-run", methods=["POST"])
@login_required
def ci_schedule_run(group_id):
    """Turn monitoring on AND enqueue an immediate baseline run."""
    _owned_group_or_404(group_id)
    db = get_db()
    ci_config.set_monitoring(db, group_id, g.user["id"], True)
    if ci_jobs.has_active_run(db, group_id):
        flash("Monitoring is on. A run is already in progress.", "ok")
    else:
        ci_jobs.enqueue_run(db, group_id, run_type="one_time")
        flash("Monitoring scheduled and a baseline run has been queued.", "ok")
    logger.info("CI schedule-run group_id=%s user_id=%s", group_id, g.user["id"])
    return _config_redirect(group_id)


# ── View Monitoring (dropdown + trends + PDF) ────────────────────────────────

def _resolve_period(raw: str | None) -> str:
    return raw if raw in ci_analysis.PERIOD_DAYS else ci_analysis.DEFAULT_PERIOD


@bp.route("/app/competitive-intel/view")
@login_required
def ci_view():
    """Pick a monitoring group from a dropdown; show its trend dashboard."""
    db = get_db()
    groups = ci_config.list_groups(db, g.user["id"], mode="monitoring")
    period = _resolve_period(request.args.get("period"))

    group_id = request.args.get("group_id", type=int)
    selected = None
    if group_id is not None:
        selected = ci_config.get_group(db, group_id, g.user["id"])
        if selected is not None and selected["mode"] != "monitoring":
            selected = None
    elif groups:
        selected = groups[0]  # default to the newest monitoring group

    sos_summary, ranks, sos_trend = [], [], {"dates": [], "brands": []}
    if selected is not None:
        sos_summary = ci_analysis.share_of_shelf_summary(db, selected["id"], period)
        ranks = ci_analysis.rank_summary(db, selected["id"], period)
        sos_trend = ci_analysis.share_of_shelf_trend(db, selected["id"], period)
    logger.info("CI view user_id=%s group_id=%s period=%s",
                g.user["id"], selected["id"] if selected else None, period)
    return render_template(
        "app/ci_view.html",
        breadcrumb="Competitive Intelligence · View Monitoring",
        active_nav="ci-view",
        groups=groups,
        selected=selected,
        period=period,
        periods=list(ci_analysis.PERIOD_DAYS.keys()),
        sos_summary=sos_summary,
        ranks=ranks,
        # Serialized for the external chart script (strict CSP: no inline data).
        sos_trend_json=json.dumps(sos_trend),
    )


@bp.route("/app/competitive-intel/view/<int:group_id>/results.pdf")
@login_required
def ci_view_pdf(group_id):
    """Download the monitoring dashboard (with deltas) as a PDF."""
    from datetime import date

    from flask import Response

    from app.pdf_export import build_ci_monitoring_pdf

    group = _owned_group_or_404(group_id)
    db = get_db()
    period = _resolve_period(request.args.get("period"))
    sos_summary = ci_analysis.share_of_shelf_summary(db, group_id, period)
    ranks = ci_analysis.rank_summary(db, group_id, period)
    pdf = build_ci_monitoring_pdf(dict(group), period, sos_summary, ranks)
    filename = f"ci-monitoring-{_slug(group['name'])}-{date.today().isoformat()}.pdf"
    logger.info("CI monitoring PDF: group_id=%s period=%s user_id=%s", group_id, period, g.user["id"])
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
