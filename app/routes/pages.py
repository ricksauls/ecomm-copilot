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
import os

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
    ci_images,
    ci_jobs,
    copy_jobs,
    fixtures,
    jobs,
    messages,
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
    from datetime import date

    db = get_db()
    uid = g.user["id"]
    month_start = date.today().replace(day=1).isoformat()  # "this month" boundary

    def _kpi(label: str, total: int, month: int) -> dict:
        """A KPI card: a unique-product total with the this-month figure beneath it."""
        return {"label": label, "value": str(total), "footnote": f"{month} this month"}

    view_model = fixtures.get_dashboard()
    # Personalize the demo header and replace the four KPI cards with real,
    # per-user unique-product counts (total + this month). PDP images aren't a
    # built feature yet, so that card reads 0 until it ships.
    view_model["agency"]["name"] = g.user["email"]
    view_model["kpis"] = [
        _kpi("Products managed",
             jobs.count_managed_products(db, uid),
             jobs.count_managed_products(db, uid, since=month_start)),
        _kpi("PDP's scored",
             jobs.count_scored_products(db, uid),
             jobs.count_scored_products(db, uid, since=month_start)),
        _kpi("PDP's copy created",
             copy_jobs.count_copy_products(db, uid),
             copy_jobs.count_copy_products(db, uid, since=month_start)),
        _kpi("PDP's images created", 0, 0),
    ]
    return render_template(
        "app/dashboard.html",
        breadcrumb="",  # no name at the very top (topbar breadcrumb)
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
        url_prefix=pdp.WALMART_IP_PREFIX,
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
        url_prefix=pdp.WALMART_IP_PREFIX,
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


@bp.route("/admin/ci-snapshots")
@admin_required
def admin_ci_snapshots():
    """Admin: table of One-Time Snapshot runs across all users (Central-time)."""
    logger.info("Admin CI snapshots view: admin_user_id=%s", g.user["id"])
    runs = [
        # Shape each run with a Central-time "ran" label (stored timestamps are UTC).
        {**dict(r), "when_cst": _run_when_cst(r)}
        for r in ci_jobs.list_snapshot_runs(get_db())
    ]
    return render_template(
        "app/admin_ci_snapshots.html",
        breadcrumb="Admin · Snapshots run",
        active_nav="admin-ci-snapshots",
        runs=runs,
    )


@bp.route("/admin/ci-monitoring")
@admin_required
def admin_ci_monitoring():
    """Admin: table of Daily Monitoring schedules across all users."""
    logger.info("Admin CI monitoring view: admin_user_id=%s", g.user["id"])
    next_run = ci_analysis.next_monitoring_run()
    groups = []
    for grp in ci_config.list_monitoring_groups_admin(get_db()):
        last_ts = grp["last_started"] or grp["last_created"]
        groups.append({
            **dict(grp),
            # Next sweep is the same wall-clock for all enabled schedules.
            "next_run_cst": next_run.strftime("%a %b %-d, %-I:%M %p") + " CST"
            if grp["monitoring_enabled"] else None,
            "last_run_cst": ci_analysis.format_run_time_cst(last_ts),
        })
    return render_template(
        "app/admin_ci_monitoring.html",
        breadcrumb="Admin · Monitoring scheduled",
        active_nav="admin-ci-monitoring",
        groups=groups,
    )


@bp.route("/admin/activity")
@admin_required
def admin_activity():
    """Admin: one consolidated, read-only view of every activity table.

    Rolls the individual admin screens into collapsible sections (Messages open,
    the rest closed) so an admin can scan everything without clicking through each
    screen. Actions (delete, reply) stay on the dedicated screens.
    """
    logger.info("Admin activity view: admin_user_id=%s", g.user["id"])
    db = get_db()
    snapshot_runs = [
        {**dict(r), "when_cst": _run_when_cst(r)}
        for r in ci_jobs.list_snapshot_runs(db)
    ]
    next_run = ci_analysis.next_monitoring_run()
    monitoring_groups = []
    for grp in ci_config.list_monitoring_groups_admin(db):
        last_ts = grp["last_started"] or grp["last_created"]
        monitoring_groups.append({
            **dict(grp),
            "next_run_cst": next_run.strftime("%a %b %-d, %-I:%M %p") + " CST"
            if grp["monitoring_enabled"] else None,
            "last_run_cst": ci_analysis.format_run_time_cst(last_ts),
        })
    return render_template(
        "app/admin_activity.html",
        breadcrumb="Admin · User Activity",
        active_nav="admin-activity",
        threads=messages.list_all_threads(db),
        category_label=messages.category_label,
        users=users.list_users(),
        items=jobs.list_items(db),
        copy_items=copy_jobs.list_copy_items(db),
        image_sets=[],  # PDP Image Set Creation is not built yet — section shown empty.
        snapshot_runs=snapshot_runs,
        monitoring_groups=monitoring_groups,
    )


@bp.route("/admin/system-activity")
@admin_required
def admin_system_activity():
    """Admin: System Activity — placeholder to be built out later."""
    logger.info("Admin system activity view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_system_activity.html",
        breadcrumb="Admin · System Activity",
        active_nav="admin-system-activity",
    )


# ── Contact Us (user side) ───────────────────────────────────────────────────

@bp.route("/app/contact")
@login_required
def contact_home():
    """The user's Contact Us hub: their threads plus a new-message form."""
    db = get_db()
    return render_template(
        "app/contact_home.html",
        breadcrumb="Contact Us",
        active_nav="contact",
        threads=messages.list_threads_for_user(db, g.user["id"]),
        categories=messages.CATEGORIES,
        category_label=messages.category_label,
    )


@bp.route("/app/contact", methods=["POST"])
@login_required
def contact_create():
    """Open a new thread from the contact form, then jump to the conversation."""
    db = get_db()
    try:
        thread_id = messages.create_thread(
            db, g.user["id"], request.form.get("subject", ""),
            request.form.get("category", ""), request.form.get("body", ""),
        )
    except messages.MessageError as e:
        flash(str(e), "error")
        return redirect(url_for("pages.contact_home"))
    logger.info("Contact thread created id=%s user_id=%s", thread_id, g.user["id"])
    return redirect(url_for("pages.contact_thread", thread_id=thread_id))


@bp.route("/app/contact/threads/<int:thread_id>")
@login_required
def contact_thread(thread_id):
    """View one of the user's own threads (marks it read for the user)."""
    db = get_db()
    thread = messages.get_thread_for_user(db, thread_id, g.user["id"])
    if thread is None:
        abort(404)  # not theirs (or doesn't exist) — IDOR gate
    messages.mark_read(db, thread_id, messages.ROLE_USER)
    return render_template(
        "app/contact_thread.html",
        breadcrumb="Contact Us",
        active_nav="contact",
        thread=thread,
        messages_list=messages.list_messages(db, thread_id),
        category_label=messages.category_label,
        admin_view=False,
    )


@bp.route("/app/contact/threads/<int:thread_id>/reply", methods=["POST"])
@login_required
def contact_reply(thread_id):
    """User replies within their own thread."""
    db = get_db()
    if messages.get_thread_for_user(db, thread_id, g.user["id"]) is None:
        abort(404)
    try:
        messages.post_reply(db, thread_id, g.user["id"], messages.ROLE_USER,
                            request.form.get("body", ""))
    except messages.MessageError as e:
        flash(str(e), "error")
    return redirect(url_for("pages.contact_thread", thread_id=thread_id))


# ── Messages (admin inbox) ───────────────────────────────────────────────────

@bp.route("/admin/messages")
@admin_required
def admin_messages():
    """Admin inbox: every user thread, unread first."""
    logger.info("Admin messages view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_messages.html",
        breadcrumb="Admin · Messages",
        active_nav="admin-messages",
        threads=messages.list_all_threads(get_db()),
        category_label=messages.category_label,
    )


@bp.route("/admin/messages/<int:thread_id>")
@admin_required
def admin_message_thread(thread_id):
    """Admin views a thread (marks it read for the admin side)."""
    db = get_db()
    thread = messages.get_thread(db, thread_id)
    if thread is None:
        abort(404)
    messages.mark_read(db, thread_id, messages.ROLE_ADMIN)
    return render_template(
        "app/contact_thread.html",
        breadcrumb="Admin · Messages",
        active_nav="admin-messages",
        thread=thread,
        messages_list=messages.list_messages(db, thread_id),
        category_label=messages.category_label,
        admin_view=True,
    )


@bp.route("/admin/messages/<int:thread_id>/reply", methods=["POST"])
@admin_required
def admin_message_reply(thread_id):
    """Admin replies within a thread."""
    db = get_db()
    if messages.get_thread(db, thread_id) is None:
        abort(404)
    try:
        messages.post_reply(db, thread_id, g.user["id"], messages.ROLE_ADMIN,
                            request.form.get("body", ""))
    except messages.MessageError as e:
        flash(str(e), "error")
    return redirect(url_for("pages.admin_message_thread", thread_id=thread_id))


@bp.route("/admin/messages/<int:thread_id>/status", methods=["POST"])
@admin_required
def admin_message_status(thread_id):
    """Admin closes or reopens a thread."""
    db = get_db()
    if messages.get_thread(db, thread_id) is None:
        abort(404)
    try:
        messages.set_status(db, thread_id, request.form.get("status", ""))
    except messages.MessageError as e:
        flash(str(e), "error")
    return redirect(url_for("pages.admin_message_thread", thread_id=thread_id))


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


def _run_when_cst(run) -> str | None:
    """Central-time display string for when a run fired (fail-safe).

    Uses the start/enqueue time, not ``finished_at``, so the "Latest run" line
    always reports when the run *fired* — even a failed or reclaimed run, whose
    ``finished_at`` is just when it was marked failed, not when it ran.
    """
    if run is None:
        return None
    ts = run["started_at"] or run["created_at"]
    return ci_analysis.format_run_time_cst(ts)


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
    latest_run = ci_jobs.latest_run(db, group_id)
    return render_template(
        "app/ci_group_config.html",
        breadcrumb=f"Competitive Intelligence · {group['name']}",
        active_nav=_ci_active_nav(group["mode"]),
        group=group,
        brands=ci_config.list_brands(db, group_id, g.user["id"]),
        products=ci_config.list_products(db, group_id, g.user["id"]),
        keywords=ci_config.list_keywords(db, group_id, g.user["id"]),
        latest_run=latest_run,
        latest_run_when=_run_when_cst(latest_run),
        brand_types=ci_config.BRAND_TYPES,
        next_run=ci_analysis.next_monitoring_run(),
        url_prefix=pdp.WALMART_IP_PREFIX,
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
    """Add one or more search keywords to a group.

    Accepts a comma-separated list so the user can add several at once; each
    comma-delimited term is validated and added independently, and per-term
    failures (duplicates, over the cap) are reported without blocking the rest.
    """
    _owned_group_or_404(group_id)  # IDOR: 404 before touching the group's data
    db = get_db()
    terms = [t.strip() for t in request.form.get("keyword", "").split(",") if t.strip()]
    if not terms:
        flash("Enter at least one keyword.", "error")
        return _config_redirect(group_id)

    added = 0
    for term in terms:
        try:
            ci_config.add_keyword(db, group_id, g.user["id"], term)
            added += 1
        except ci_config.ConfigError as e:
            flash(str(e), "error")
    if added:
        flash(f"Added {added} keyword{'' if added == 1 else 's'}.", "ok")
    logger.info("CI add-keyword group_id=%s user_id=%s requested=%d added=%d",
                group_id, g.user["id"], len(terms), added)
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


@bp.route("/app/competitive-intel/groups/<int:group_id>/schedule-monitoring", methods=["POST"])
@login_required
def ci_schedule_from_snapshot(group_id):
    """Clone a snapshot group into a monitoring group, then queue a baseline run.

    The snapshot card's "Schedule for monitoring" action: promote a one-time
    competitive set to ongoing 3x/day tracking without disturbing the snapshot.
    Lands on the new monitoring group's config so the user can confirm the setup.
    """
    _owned_group_or_404(group_id)  # IDOR gate before we touch anything
    db = get_db()
    try:
        new_group_id = ci_config.clone_group_as_monitoring(db, group_id, g.user["id"])
    except ci_config.ConfigError as e:
        flash(str(e), "error")
        return redirect(url_for("pages.ci_snapshot_home"))

    ci_jobs.enqueue_run(db, new_group_id, run_type="one_time")
    flash("Monitoring group created from this snapshot — a baseline run is queued "
          "and automatic checks run 3×/day.", "ok")
    logger.info("CI schedule-from-snapshot src=%s new=%s user_id=%s",
                group_id, new_group_id, g.user["id"])
    return _config_redirect(new_group_id)


def _product_views(products) -> list[dict]:
    """Shape tracked-product rows for the "What this group tracks" section.

    Adds the main-image references both surfaces need: ``image_url`` (the
    same-origin media route the page's <img> hits) and ``image_path`` (the cache
    file the PDF embeds), each present only when an image is actually cached.

    "My" brands are listed first (then whatever order the query returned) so the
    user sees their own items on the left — matching the mine-first convention the
    brand/ranking views already follow.
    """
    products = sorted(products, key=lambda p: 0 if p["brand_type"] == "mine" else 1)
    views = []
    for p in products:
        item_id = p["walmart_item_id"]
        cached = ci_images.has_product_image(item_id)
        views.append({
            "name": p["name"],
            "walmart_item_id": item_id,
            "brand_name": p["brand_name"],
            "brand_type": p["brand_type"],
            "image_url": url_for("pages.ci_product_image", item_id=item_id) if cached else None,
            "image_path": ci_images.product_image_path(item_id) if cached else None,
        })
    return views


@bp.route("/media/ci-product/<item_id>")
@login_required
def ci_product_image(item_id):
    """Serve a cached tracked-product image (same-origin, so CSP img-src 'self')."""
    from flask import send_file

    path = ci_images.product_image_path(item_id)  # None for a non-numeric id
    if not path or not os.path.isfile(path):
        abort(404)
    # Product photos are public; cache a day. login_required keeps it behind auth.
    return send_file(path, mimetype="image/jpeg", max_age=86400)


def _snapshot_data(db, group_id):
    """Gather every section the snapshot results page and its PDF render.

    Returns a dict with the group's config summary plus, once a run is done, the
    overall/per-keyword ranking and share rollups. Shared by the page route and
    the PDF export so the two never drift.
    """
    run = ci_jobs.latest_run(db, group_id)

    # Config summary — always available; user-scoped/IDOR-checked in ci_config.
    brands = ci_config.list_brands(db, group_id, g.user["id"])
    config_summary = {
        "my_brands": [b["name"] for b in brands if b["type"] == "mine"],
        "competitor_brands": [b["name"] for b in brands if b["type"] != "mine"],
        "products": _product_views(ci_config.list_products(db, group_id, g.user["id"])),
        "keywords": [k["keyword"] for k in ci_config.list_keywords(db, group_id, g.user["id"])],
    }

    sos_summary, avg_ranks, rank_rows, share_rows = [], [], [], []
    rank_map = None
    if run and run["status"] == "done":
        rid = run["id"]
        sos_summary = ci_analysis.snapshot_share_of_shelf(db, group_id, rid)
        avg_ranks = ci_analysis.snapshot_brand_avg_rank(db, group_id, rid)
        rank_rows = ci_analysis.snapshot_rank_by_keyword_brand(db, group_id, rid)
        share_rows = ci_analysis.snapshot_share_by_keyword(db, group_id, rid)
        # Placement grid for the Overall Search Ranking section: each brand's
        # average rank mapped onto a page-1 result grid (page + PDF share this).
        depth = ci_analysis.snapshot_page1_depth(db, group_id, rid)
        rank_map = ci_analysis.build_rank_placement_map(avg_ranks, depth)

    return {
        "run": run,
        "config_summary": config_summary,
        "sos_summary": sos_summary,
        "avg_ranks": avg_ranks,
        "rank_rows": rank_rows,
        "share_rows": share_rows,
        "rank_map": rank_map,
    }


@bp.route("/app/competitive-intel/groups/<int:group_id>/results")
@login_required
def ci_snapshot_results(group_id):
    """Current-state snapshot results (no trends); polls while a run is active."""
    group = _owned_group_or_404(group_id)
    db = get_db()
    data = _snapshot_data(db, group_id)

    # Stacked-bar chart scale: bars visualize the table's organic/sponsored share
    # columns (not raw counts), so scale to the tallest organic+sponsored stack.
    sos_scale = max((r["organic_share"] + r["sponsored_share"] for r in data["sos_summary"]),
                    default=0)

    run = data["run"]
    logger.info("CI snapshot results: group_id=%s user_id=%s run_id=%s rank_rows=%d",
                group_id, g.user["id"], run["id"] if run else None,
                len(data["rank_rows"]))
    return render_template(
        "app/ci_snapshot_results.html",
        breadcrumb=f"Competitive Intelligence · {group['name']}",
        active_nav="ci-snapshot",
        group=group,
        run=run,
        sos_summary=data["sos_summary"],
        config_summary=data["config_summary"],
        avg_ranks=data["avg_ranks"],
        rank_rows=data["rank_rows"],
        share_rows=data["share_rows"],
        rank_map=data["rank_map"],
        sos_scale=sos_scale,
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
    data = _snapshot_data(db, group_id)
    if not data["run"] or data["run"]["status"] != "done":
        abort(404)  # nothing to export yet
    # Mirror the page: config summary, both ranking tables, both share tables.
    pdf = build_ci_snapshot_pdf(
        dict(group),
        config_summary=data["config_summary"],
        avg_ranks=data["avg_ranks"],
        rank_rows=data["rank_rows"],
        sos_rows=data["sos_summary"],
        share_rows=data["share_rows"],
        rank_map=data["rank_map"],
    )
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
