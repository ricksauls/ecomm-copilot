"""Page routes for the public surface and the authenticated workspace.

Three routes in this pass: the marketing landing page and sign-in page (public,
dark surface) and the agency dashboard (workspace, light surface). The remaining
workspace screens (product, analysis, creative, share of shelf) are designed in
the handoff but not yet built.

Sign-in / sign-up / sign-out live in the ``auth`` blueprint. The dashboard is
guarded by ``login_required``, so it is no longer world-reachable.
"""

import csv
import io
import json
import logging
import os
import re
from datetime import datetime

from flask import (
    Blueprint,
    abort,
    flash,
    g,
    jsonify,
    make_response,
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
    copygen,
    fixtures,
    image_enhance,
    image_jobs,
    jobs,
    messages,
    pdp,
    portfolio,
    users,
)
from app.db import get_db
from app.imageset import generate as imageset_generate
from app.imageset import jobs as imageset_jobs
from app.imageset import storage as imageset_storage
from app.imageset import store as imageset_store
from app.imageset.providers.background_removal import BackgroundRemovalError
from app.imgimport import sources as import_sources
from app.imgimport import store as import_store
from app.seasonal import jobs as seasonal_jobs
from app.seasonal import storage as seasonal_storage
from app.seasonal import store as seasonal_store
from app.seasonal import themes as seasonal_themes
from app.security import (
    admin_required,
    login_required,
    validate_credentials,
    validate_password,
)

logger = logging.getLogger(__name__)

# Session keys holding the ids of the most recent scoring / copy batches.
_BATCH_KEY = "pdp_batch_ids"
_COPY_BATCH_KEY = "pdp_copy_batch_ids"
# Selected items that had no copy yet when "View copy results" ran — listed on
# the copy results page so the user sees they weren't created. Set by
# `pdp_scoring_view_copy`; cleared whenever a fresh copy batch is created.
_COPY_MISSING_KEY = "pdp_copy_missing"

bp = Blueprint("pages", __name__)


def _format_signup_date(created_at: str | None) -> str:
    """Render a stored UTC ``created_at`` as e.g. "Aug 20, 2026" for the dashboard.

    ``created_at`` is a ``YYYY-MM-DD HH:MM:SS`` string. Fail-safe: an unexpected or
    missing value falls back to the raw date portion rather than 500-ing the
    dashboard over a cosmetic subtitle.
    """
    from datetime import datetime

    if not created_at:
        return "—"
    try:
        return datetime.strptime(created_at[:19], "%Y-%m-%d %H:%M:%S").strftime("%b %-d, %Y")
    except (ValueError, TypeError):
        return created_at[:10]


def _format_activity_date(ts: str | None) -> str:
    """Render a stored UTC timestamp as a short "Aug 26" for the activity tables.

    The tables only cover the current month, so the year is redundant. Fail-safe:
    an unexpected value falls back to its date portion rather than 500-ing the
    dashboard over a cosmetic cell.
    """
    from datetime import datetime

    if not ts:
        return "—"
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").strftime("%b %-d")
    except (ValueError, TypeError):
        return ts[:10]


def _search_blob(*parts) -> str:
    """Lowercased, space-joined searchable text for a history row's ``data-search``.

    The client-side history search (``static/js/dashboard.js``) substring-matches a
    typed query against this, so it can include fields that aren't visible columns
    (an item/SKU number, every item in a grouped run). Blank/None parts are dropped;
    numbers are coerced to strings.
    """
    return " ".join(str(p).strip() for p in parts if p not in (None, "")).lower()


def _item_image_url(item_id):
    """Same-origin URL for an item's cached main image, or ``None`` if uncached.

    Scored/copy items share the item-id-keyed product-image cache with CI (the
    worker fills it on fetch), so the same route serves all three. ``None`` lets
    the template fall back to a placeholder tile.
    """
    if item_id and ci_images.has_product_image(item_id):
        return url_for("pages.ci_product_image", item_id=item_id)
    return None


def _product_activity_view(rows, *, with_score: bool) -> list[dict]:
    """Shape scored/copy rows for a dashboard activity table.

    Common columns are image, date, brand, and title; the scored table adds a
    score. A blank brand renders as an em dash so the column never looks broken.
    """
    views = []
    for r in rows:
        brand = (r["brand"] or "").strip()
        title = r["title"] or r["item_id"] or r["url"]
        view = {
            "id": r["id"],  # scored_items / copy_items row id, for the per-item results link
            "image_url": _item_image_url(r["item_id"]),
            "date": _format_activity_date(r["created_at"]),
            # Raw timestamp for client-side sorting: the display date ("Aug 26")
            # has no year, so sort on the ISO value instead.
            "sort_date": r["created_at"] or "",
            "brand": brand or "—",
            "title": title,
            "item_id": r["item_id"],
            # Lowercased blob the client-side history search matches against. Includes
            # the item number, which isn't a visible column, so a user can find a row
            # by SKU as well as by brand/product.
            "search": _search_blob(brand, title, r["item_id"]),
        }
        if with_score:
            view["score"] = r["overall"]
        views.append(view)
    return views


def _imageset_activity_view(rows) -> list[dict]:
    """Shape image-set project rows for the "images" activity table.

    One row per creative run, matching the shared product-table columns (image,
    date, brand, product). The thumbnail is the project's own uploaded/fetched
    product photo served from ``imageset_original_image`` — the item-id product-
    image cache the scored/copy rows thumbnail from isn't populated for image-set
    projects. A project with no stored photo (shouldn't happen past the cutout
    gate, but guarded) falls back to the placeholder tile. The product column shows
    the project name, since an image-set run isn't tied to a single scored item.
    """
    views = []
    for r in rows:
        brand = (r["brand"] or "").strip()
        name = r["name"] or "Image set"
        # The Walmart item number (when the project was prefilled from a PDP) so the
        # run is searchable by SKU, mirroring the scored/copy rows.
        item = pdp.item_number_from_url(r["source_url"]) if r["source_url"] else None
        views.append({
            "id": r["id"],  # imageset_projects row id, for the gallery link
            "image_url": (url_for("pages.imageset_original_image", pid=r["id"])
                          if r["original_path"] else None),
            "date": _format_activity_date(r["run_at"]),
            # Raw timestamp for client-side sorting (see _product_activity_view).
            "sort_date": r["run_at"] or "",
            "brand": brand or "—",
            "title": name,
            "search": _search_blob(brand, name, item),
        })
    return views


# Human-readable names for the acquire source types stored on import_items.
# source_type (walmart|amazon_pdp|image|unknown); unknown titles fall back to
# the raw value so a new source type still renders something sensible.
_ACQUIRE_SOURCE_LABELS = {
    "walmart": "Walmart",
    "amazon_pdp": "Amazon",
    "image": "Image URL",
    "unknown": "Other",
}


def _acquire_activity_view(rows) -> list[dict]:
    """Shape finished acquire runs for the "Acquired Images" history table.

    One row per run (:func:`import_store.list_acquire_activity`). Columns are
    image, date, source site(s), the product(s) acquired, and the image count.
    The thumbnail hotlinks the run's first ready asset — an external CDN URL, since
    the no-pre-download model stores no bytes — so the history page relaxes its CSP
    the same way the gallery does; if that URL has since rotted the cell falls back
    to the placeholder tile. The product cell shows the first product's label plus
    an "+N more" count when a run pulled several; a run with no labelled items
    (e.g. bare image URLs, which carry no title) shows its source instead so the
    cell is never blank. Search covers every product label and the source, not just
    the shown one.
    """
    views = []
    for r in rows:
        labels = [s for s in (r["labels"] or "").split(import_store._LABEL_SEP) if s.strip()]
        source_types = [s for s in (r["source_types"] or "").split(",") if s]
        # De-dupe source names while preserving order (dict keys are ordered).
        source = ", ".join(dict.fromkeys(
            _ACQUIRE_SOURCE_LABELS.get(s, s.title()) for s in source_types)) or "—"
        if labels:
            product = labels[0]
        else:
            # Bare image URLs carry no title; show the source so the cell isn't blank.
            product = source if source != "—" else "Acquired images"
        views.append({
            "id": r["id"],  # import_runs row id, for the gallery link
            "image_url": r["thumb_url"] or None,
            "date": _format_activity_date(r["run_at"]),
            # Raw timestamp for client-side sorting (see _product_activity_view).
            "sort_date": r["run_at"] or "",
            "source": source,
            "title": product,
            # Extra products beyond the one shown — rendered on its own muted line so
            # it survives the product column's ellipsis truncation.
            "more": max(0, len(labels) - 1),
            "count": r["image_count"],
            "search": _search_blob(source, *labels),
        })
    return views


def _ci_activity_view(db, uid, rows) -> list[dict]:
    """Shape CI snapshot/monitoring activity rows with their brand config.

    Each row gains the group's mine-vs-competitor brand names and tracked-item
    counts (from :func:`ci_config.list_brands`, which carries a per-brand product
    count). ``list_brands`` is ownership-checked, so only the caller's own groups
    resolve. Brand names are comma-joined for the cell; an em dash stands in when a
    side has no brands configured yet.
    """
    views = []
    for r in rows:
        brands = ci_config.list_brands(db, r["group_id"], uid)
        mine = [b for b in brands if b["type"] == "mine"]
        competitors = [b for b in brands if b["type"] != "mine"]
        my_brands = ", ".join(b["name"] for b in mine)
        competitor_brands = ", ".join(b["name"] for b in competitors)
        views.append({
            "group_id": r["group_id"],  # for the per-group results link
            "name": r["group_name"],
            "date": _format_activity_date(r["run_at"]),
            # Raw timestamp for client-side sorting (see _product_activity_view).
            "sort_date": r["run_at"] or "",
            "my_brands": my_brands or "—",
            "my_items": sum(b["product_count"] for b in mine),
            "competitor_brands": competitor_brands or "—",
            "competitor_items": sum(b["product_count"] for b in competitors),
            "search": _search_blob(r["group_name"], my_brands, competitor_brands),
        })
    return views


# Each dashboard activity table + its "View All" screen. Maps the URL kind to its
# display title, layout family ("product" thumbnail table vs "ci" brand table),
# whether it shows a score column, and the empty-state message. The dashboard
# renders all five (month-scoped); a View All renders one (all-time).
_ACTIVITY_META = {
    "scored":        ("Products scored", "product", True,  "Nothing scored yet."),
    "copy":          ("Copy created", "product", False, "No copy created yet."),
    "images":        ("Image sets created", "product", False,
                      "No image sets created yet."),
    "acquired":      ("Images acquired", "acquired", False,
                      "No images acquired yet."),
    "ci-snapshot":   ("Competitive Intelligence — One-Time Snapshot", "ci", False,
                      "No snapshots run yet."),
    "ci-monitoring": ("Competitive Intelligence — Daily Monitoring", "ci", False,
                      "No monitoring groups yet."),
}

# Which rail item the View-All screen highlights, per activity kind. The three
# content histories each map to their own rail sub-item (so "View Scoring
# History" etc. light up); the CI histories map to the Reporting section's
# "View All Competitive Intelligence Activity" item. Unknown kinds never reach
# here (activity_all 404s first), so a plain dict is sufficient.
# History now lives inside each tool's workspace (Create New / History tabs), so
# a history page keeps the *workspace* rail item highlighted rather than its own
# nav slug. CI history is unchanged (CI is not yet on the workspace model).
_ACTIVITY_ACTIVE_NAV = {
    "scored": "pdp-scoring",
    "copy": "pdp-copy",
    "images": "pdp-image-set",
    "acquired": "import-images",
    "ci-snapshot": "ci-activity",
    "ci-monitoring": "ci-activity",
}

# Topbar breadcrumb per activity kind, in the "<workspace> · History" form (the
# content histories are the History tab of their workspace). The CI histories
# (reached from the dashboard) use the closest Insights flow.
_ACTIVITY_BREADCRUMB = {
    "scored": "Content Scoring · History",
    "copy": "Product Copy · History",
    "images": "Product Image Set Creator · History",
    "acquired": "Product Image Import · History",
    "ci-snapshot": "Insights · One-Time Snapshot",
    "ci-monitoring": "Insights · Daily Monitoring",
}

# For the content workspaces, a history page also renders the workspace tab bar.
# Maps the activity `kind` to its paired Create-New endpoint; the History href is
# always ``activity_all`` for that kind. CI kinds are absent (no workspace bar).
_ACTIVITY_WORKSPACE_CREATE = {
    "scored": "pages.pdp_scoring",
    "copy": "pages.pdp_copy",
    "images": "pages.imageset_intake",
    "acquired": "pages.import_images_intake",
}

# The History tab is named after what that workspace produces (e.g. "Copy
# Created"), rather than a generic "History", so the tab reads clearly on its own.
# Must stay in sync with the labels passed on each workspace's Create-New view.
_ACTIVITY_HISTORY_LABEL = {
    "scored": "History - Content Scored",
    "copy": "History - Copy Created",
    "images": "History - Image Sets Created",
    "acquired": "History - Images Acquired",
}


def _activity_workspace(kind):
    """Build the workspace-tab context for a history page, or None for CI kinds.

    Returns a dict of the two tab hrefs plus the history-tab label so the shared
    ``activity_all.html`` / ``activity_scored_runs.html`` can render the Create New
    / History bar only on content workspaces.
    """
    create_endpoint = _ACTIVITY_WORKSPACE_CREATE.get(kind)
    if create_endpoint is None:
        return None
    return {
        "create_href": url_for(create_endpoint),
        "history_href": url_for("pages.activity_all", kind=kind),
        "history_label": _ACTIVITY_HISTORY_LABEL.get(kind, "History"),
    }


def _activity_rows(db, uid, kind, since):
    """Shaped rows for one activity kind — month-scoped (``since`` set) or all-time.

    Shared by the dashboard (``since`` = start of month) and the View All screen
    (``since`` = None). Each row gets a ``result_url`` so it links to that activity's
    results (a scored/copy item's results page, an image-set run's gallery, or a CI
    group's results/monitoring view). Callers validate ``kind`` against
    :data:`_ACTIVITY_META` first.
    """
    if kind == "scored":
        rows = _product_activity_view(jobs.list_scored_activity(db, uid, since), with_score=True)
        for r in rows:
            r["result_url"] = url_for("pages.pdp_scoring_item", sid=r["id"])
        return rows
    if kind == "copy":
        rows = _product_activity_view(copy_jobs.list_copy_activity(db, uid, since), with_score=False)
        for r in rows:
            r["result_url"] = url_for("pages.pdp_copy_item", cid=r["id"])
        return rows
    if kind == "images":
        rows = _imageset_activity_view(imageset_store.list_creative_activity(db, uid, since))
        for r in rows:
            r["result_url"] = url_for("pages.imageset_gallery", pid=r["id"])
        return rows
    if kind == "acquired":
        rows = _acquire_activity_view(import_store.list_acquire_activity(db, uid, since))
        for r in rows:
            r["result_url"] = url_for("pages.import_images_run", run_id=r["id"])
        return rows
    if kind == "ci-snapshot":
        rows = _ci_activity_view(db, uid, ci_jobs.list_snapshot_activity_for_user(db, uid, since))
        for r in rows:
            r["result_url"] = url_for("pages.ci_snapshot_results", group_id=r["group_id"])
        return rows
    if kind == "ci-monitoring":
        rows = _ci_activity_view(db, uid, ci_jobs.list_monitoring_activity_for_user(db, uid, since))
        for r in rows:
            r["result_url"] = url_for("pages.ci_view", group_id=r["group_id"])
        return rows
    return []


def _scored_runs(db, uid) -> list[dict]:
    """Group a user's scored items into one summary row per scoring action.

    Items submitted together share a ``batch_id`` (one scoring run). This rolls
    the all-time scored history up by run: each run shows up to three of its
    items (brand + title + score, mirroring the per-item columns) plus an
    "And N more…" count, its date, the first item's thumbnail, and a link that
    reopens the whole run. Items scored before run tracking have no ``batch_id``
    and each stand alone. Most-recent run first (the query is already newest-first).
    """
    groups: dict[str, dict] = {}
    order: list[str] = []
    for r in jobs.list_scored_activity(db, uid, None):
        # Ungrouped (pre-batch) items each form their own single-item run.
        key = r["batch_id"] or f"item-{r['id']}"
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "first_id": r["id"],          # any sibling reopens the whole run
                "image_item_id": r["item_id"],
                "sort_date": r["created_at"] or "",
                "date": _format_activity_date(r["created_at"]),
                "items": [],
            }
            order.append(key)
        group["items"].append({
            "brand": (r["brand"] or "").strip() or "—",
            "title": r["title"] or r["item_id"] or r["url"],
            "score": r["overall"],
            # Kept per-item so the run's search blob covers every item (incl. SKU),
            # not just the three shown on the row.
            "search": _search_blob(r["brand"], r["title"], r["item_id"]),
        })

    runs = []
    for key in order:
        group = groups[key]
        shown = group["items"][:3]
        runs.append({
            "date": group["date"],
            "sort_date": group["sort_date"],
            "image_url": _item_image_url(group["image_item_id"]),
            "count": len(group["items"]),
            "shown": shown,
            "more": len(group["items"]) - len(shown),  # 0 when nothing hidden
            "result_url": url_for("pages.pdp_scoring_item", sid=group["first_id"]),
            # Search across the whole run's items, so a query finds a run even by an
            # item that isn't among the three displayed.
            "search": " ".join(it["search"] for it in group["items"]),
        })
    return runs


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
    # per-user counts (total + this month): managed/scored/copy products, and
    # image-set runs the user has created.
    view_model["agency"]["name"] = g.user["email"]
    # Portfolio subtitle: distinct brands · distinct products the user has worked
    # on, then their signup date. "Products" reuses the "Products managed" KPI
    # figure below (same helper), and "Walmart" from the demo line is dropped.
    view_model["agency"]["subtitle"] = (
        f"{jobs.count_managed_brands(db, uid)} brands · "
        f"{jobs.count_managed_products(db, uid)} products · "
        f"As of {_format_signup_date(g.user['created_at'])}"
    )
    view_model["kpis"] = [
        _kpi("Products managed",
             jobs.count_managed_products(db, uid),
             jobs.count_managed_products(db, uid, since=month_start)),
        _kpi("Product Detail Pages scored",
             jobs.count_scored_products(db, uid),
             jobs.count_scored_products(db, uid, since=month_start)),
        _kpi("Product Detail Pages copy created",
             copy_jobs.count_copy_products(db, uid),
             copy_jobs.count_copy_products(db, uid, since=month_start)),
        _kpi("Product Detail Pages images created",
             imageset_store.count_creative_runs(db, uid),
             imageset_store.count_creative_runs(db, uid, since=month_start)),
        # Competitive Intelligence activity: snapshots the user has run and the
        # daily-monitoring schedules they have active.
        _kpi("One-Time Snapshot",
             ci_jobs.count_snapshot_runs_for_user(db, uid),
             ci_jobs.count_snapshot_runs_for_user(db, uid, since=month_start)),
        _kpi("Daily Monitoring",
             ci_config.count_monitoring_groups_for_user(db, uid),
             ci_config.count_monitoring_groups_for_user(db, uid, since=month_start)),
    ]

    # "This month" activity tables that replace the old demo "losing ground" table.
    # Each is month-scoped here; its "View all" link opens the all-time screen.
    activity = {
        "scored": _activity_rows(db, uid, "scored", month_start),
        "copy": _activity_rows(db, uid, "copy", month_start),
        "image_sets": _activity_rows(db, uid, "images", month_start),
        "ci_snapshot": _activity_rows(db, uid, "ci-snapshot", month_start),
        "ci_monitoring": _activity_rows(db, uid, "ci-monitoring", month_start),
    }
    # Seasonal campaign planning nudges (launch/expiration/reminder dates coming up
    # or overdue). Nothing publishes automatically, so these are reminders to act.
    seasonal_reminders = seasonal_store.dashboard_reminders(
        db, uid, date.today().isoformat())
    return render_template(
        "app/dashboard.html",
        breadcrumb="Dashboard",
        active_nav="dashboard",
        activity=activity,
        seasonal_reminders=seasonal_reminders,
        **view_model,
    )


@bp.route("/app/activity/<kind>")
@login_required
def activity_all(kind):
    """View All screen for one dashboard activity: every record, all-time.

    The dashboard tables show only the current month; this shows the full history
    for the requested ``kind``. Unknown kinds 404. Reuses the shared table macros
    (`_dash_tables.html`) so the layout matches the dashboard.
    """
    meta = _ACTIVITY_META.get(kind)
    if meta is None:
        abort(404)
    title, layout, with_score, empty = meta
    db = get_db()
    uid = g.user["id"]

    # Scoring history groups by run (one row per scoring action); every other
    # activity stays one row per record.
    if kind == "scored":
        runs = _scored_runs(db, uid)
        logger.info("Serving View All scoring runs user_id=%s runs=%d", uid, len(runs))
        return render_template(
            "app/activity_scored_runs.html",
            breadcrumb=_ACTIVITY_BREADCRUMB.get(kind),
            active_nav=_ACTIVITY_ACTIVE_NAV.get(kind, "dashboard"),
            workspace=_activity_workspace(kind),
            title="Brands/Products Scored",
            runs=runs,
            empty="Nothing scored yet.",
        )

    rows = _activity_rows(db, uid, kind, since=None)  # all-time
    logger.info("Serving View All activity=%s user_id=%s rows=%d", kind, uid, len(rows))
    resp = make_response(render_template(
        "app/activity_all.html",
        breadcrumb=_ACTIVITY_BREADCRUMB.get(kind, "Dashboard · " + title),
        active_nav=_ACTIVITY_ACTIVE_NAV.get(kind, "dashboard"),
        workspace=_activity_workspace(kind),
        title=title,
        layout=layout,
        with_score=with_score,
        empty=empty,
        rows=rows,
    ))
    # The acquired-images history hotlinks external CDN thumbnails (the
    # no-pre-download model stores no bytes), so relax img-src for this page
    # only — the same per-page CSP the acquire gallery uses.
    if kind == "acquired":
        resp.headers["Content-Security-Policy"] = _IMPORT_GALLERY_CSP
    return resp


@bp.route("/app/competitive-intel/activity")
@login_required
def ci_activity():
    """View All Competitive Intelligence Activity: all-time snapshot + monitoring.

    The CI counterpart of View All Content Activity — the two CI tables (One-Time
    Snapshot, Daily Monitoring), all-time, using the same dashboard-style tables
    (collapsible, sortable, 10-row cap, row-click opens the group's results). Not
    month-scoped — ``since=None``.
    """
    db = get_db()
    uid = g.user["id"]
    logger.info("Serving View All Competitive Intelligence Activity user_id=%s", uid)
    return render_template(
        "app/ci_activity.html",
        breadcrumb="Insights · View All Competitive Intelligence Activity",
        active_nav="ci-activity",
        snapshot=_activity_rows(db, uid, "ci-snapshot", since=None),
        monitoring=_activity_rows(db, uid, "ci-monitoring", since=None),
    )


def _portfolio_pick_context() -> dict:
    """Portfolio pick-list context shared by the scoring and copy intakes.

    Both screens offer the user's managed portfolio as a filterable checkbox list
    above manual URL entry; this returns the items and their distinct brands (for
    the brand filter). Scoped to the signed-in user so one user never sees
    another's portfolio.
    """
    db = get_db()
    uid = g.user["id"]
    return {
        "portfolio_items": portfolio.list_items(db, uid),
        "portfolio_brands": portfolio.distinct_brands(db, uid),
    }


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
                    breadcrumb="Product Content Scoring · Score Product Detail Page(s)",
                    active_nav="pdp-scoring",
                    submitted=False,
                    max_items=pdp.MAX_ITEMS,
                    error="No valid item URLs were provided.",
                    **_portfolio_pick_context(),
                ),
                400,
            )

        # Scoring intake doesn't collect a brand from the user; the worker fills it
        # from the PDP during fetch (see jobs.save_result).
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
        breadcrumb="Product Content Scoring · Score Product Detail Page(s)",
        active_nav="pdp-scoring",
        submitted=False,
        max_items=pdp.MAX_ITEMS,
        url_prefix=pdp.WALMART_IP_PREFIX,
        **_portfolio_pick_context(),
    )


def _batch_rows():
    """Fetch the current session batch's rows, scoped to the signed-in user."""
    ids = session.get(_BATCH_KEY, [])
    return jobs.get_items(get_db(), ids, g.user["id"])


# Score bands for the results summary strip, as (name, inclusive floor) ordered
# high → low so the first match wins. Thresholds are deliberately coarse — the
# strip is an at-a-glance health read, not a precise grading scale.
_SCORE_BANDS = (("strong", 80), ("moderate", 60), ("weak", 0))

# Rough wall-clock budget per not-yet-scored item, used only for the progress
# bar's "time left" estimate. ~12s/item ≈ 5 items/minute, which is what the
# concurrent worker sustains and what the in-progress copy promises. Deliberately
# approximate — real per-item time swings with Walmart latency and cache hits.
_SECONDS_PER_ITEM = 12


def _eta_label(remaining: int) -> str | None:
    """A coarse 'time left' for a still-scoring batch, or None when nothing's left.

    Rounded to the minute and prefixed 'about' so it reads as an estimate rather
    than a countdown; under a minute it says so instead of showing seconds.
    """
    if remaining <= 0:
        return None
    seconds = remaining * _SECONDS_PER_ITEM
    if seconds < 60:
        return "less than a minute left"
    return f"about {round(seconds / 60)} min left"


def _score_summary(items: list[dict]) -> dict:
    """Aggregate a scored batch for the results-page summary strip + progress bar.

    Only items that finished scoring contribute to the average and the band
    counts; queued / blocked / errored items are reported separately so the
    strip never implies a score they don't have. ``pending`` (queued + scoring)
    drives the progress bar's remaining-work estimate.
    """
    scored = [
        it for it in items if it["status"] == "scored" and it["overall"] is not None
    ]
    bands = {name: 0 for name, _ in _SCORE_BANDS}
    for it in scored:
        for name, floor in _SCORE_BANDS:
            if it["overall"] >= floor:
                bands[name] += 1
                break
    avg = round(sum(it["overall"] for it in scored) / len(scored)) if scored else None
    pending = sum(1 for it in items if it["status"] in ("queued", "scoring"))
    return {
        "total": len(items),
        "scored": len(scored),
        "avg": avg,
        "bands": bands,
        "pending": pending,
        "eta": _eta_label(pending),
    }


@bp.route("/app/pdp-scoring/results")
@login_required
def pdp_scoring_results():
    """Show the most recent scoring batch and poll until every item finishes."""
    items = [_row_view(r) for r in _batch_rows()]
    # Annotate each flagged image with its async-fix status (button / spinner /
    # inline result / retry). Only when the feature is configured — otherwise the
    # whole enhance UI stays hidden and no job lookup is needed.
    enhance_configured = image_enhance.is_configured()
    enhance_pending = False
    enhance_batch = None
    if enhance_configured and items:
        jobs_map = image_jobs.jobs_for_items(
            get_db(), [it["id"] for it in items], g.user["id"]
        )
        state = _annotate_enhance(items, jobs_map, image_enhance.output_ext())
        enhance_pending = state["pending"]
        enhance_batch = state["batch"]

    # Copy rewrites: annotate each scored row with its existing-copy state (for the
    # per-row badge) and roll the batch up for the copy bar. Always on — copy is a
    # core feature (no provider gate, unlike image fixes); generation just needs
    # ANTHROPIC_API_KEY, which the worker enforces.
    copy_batch = None
    if items:
        scored_keys = [
            (it["item_id"], it["url"]) for it in items if it["status"] == "scored"
        ]
        copy_states = copy_jobs.copy_states_for_items(
            get_db(), g.user["id"], scored_keys
        ) if scored_keys else {}
        copy_batch = _annotate_copy(items, copy_states)
    # Keep the auto-refresh alive while a copy rewrite is still generating, too.
    copy_pending = bool(copy_batch and copy_batch["in_flight_any"])
    return render_template(
        "app/pdp_results.html",
        breadcrumb="Product Content Scoring · Score Product Detail Page(s)",
        active_nav="pdp-scoring",
        items=items,
        summary=_score_summary(items),
        # Gates the per-flagged-image enhance controls — hidden unless an upscaling
        # provider/key is configured (the feature is inert otherwise).
        enhance_configured=enhance_configured,
        # Keeps the page's 5s meta-refresh alive while any fix is still running, so
        # the inline result appears without a manual reload (mirrors scoring).
        enhance_pending=enhance_pending,
        # Batch roll-up (counts across the whole table) driving the batch action
        # bar: fix-all / fix-selected / ZIP / retry / cancel + the progress line.
        enhance_batch=enhance_batch,
        # Shown in the cost-preflight modal; None → modal shows counts only.
        enhance_price=image_enhance.price_per_image() if enhance_configured else None,
        # Copy-rewrite batch roll-up (per-row badge counts + bar state). Always set
        # when there are items — copy has no provider gate.
        copy_batch=copy_batch,
        # Extends the auto-refresh condition so a generating rewrite updates live.
        copy_pending=copy_pending,
    )


def _flagged_image_url(row, index: int) -> str | None:
    """The stored URL of the imagery issue at gallery ``index`` on a scored row.

    Pulled from the row's own ``result_json`` (our scrape output) so the upscaler
    only ever receives a URL we recorded — never arbitrary user input (SSRF guard).
    """
    if not row["result_json"]:
        return None
    try:
        result = json.loads(row["result_json"])
    except (ValueError, TypeError):
        return None
    for d in result.get("dimensions", []):
        if d.get("key") == "imagery":
            for issue in d.get("image_issues", []):
                if issue.get("index") == index:
                    return issue.get("url")
    return None


def _white_bg_image_url(row) -> str | None:
    """Main image URL to fix the white background — from the scored row's own
    result (``imagery.white_bg_url``), falling back to the stored record's main
    image. Both originate from our scrape, never the request (SSRF guard)."""
    if row["result_json"]:
        try:
            result = json.loads(row["result_json"])
        except (ValueError, TypeError):
            result = {}
        for d in result.get("dimensions", []):
            if d.get("key") == "imagery" and d.get("white_bg_url"):
                return d["white_bg_url"]
    if row["record_json"]:
        try:
            return (json.loads(row["record_json"]) or {}).get("main_image_url")
        except (ValueError, TypeError):
            return None
    return None


# Valid enhanced-image slots: a gallery image ("img2") or the main-image white-bg
# fix ("whitebg"). Validated on the serve routes before any filesystem access, on
# top of the path guard already inside ci_images.enhanced_image_path.
_ENHANCE_SLOT_RE = re.compile(r"^(?:img\d+|whitebg)$")


def _enhance_targets(row) -> list[tuple[str, str, str]]:
    """Enhanceable ``(slot, operation, source_url)`` tuples for a scored row.

    The single source of truth for which fixes an item offers, derived from the
    imagery dimension of its stored result:
    - every sub-2000px gallery image → ``("img{index}", "upscale", url)``;
    - the main image when it also fails the white-background check →
      ``("whitebg", "white_bg", url)``, which *supersedes* the plain upscale of
      image 1 (the combined call does white-bg + upscale + resize in one, and you
      can't chain two downloads).

    Every URL originates from our own scrape (``result_json``), never request
    input — the provider fetches it directly (SSRF guard).
    """
    if not row["result_json"]:
        return []
    try:
        result = json.loads(row["result_json"])
    except (ValueError, TypeError):
        return []
    imagery = next(
        (d for d in result.get("dimensions", []) if d.get("key") == "imagery"), None
    )
    if not imagery:
        return []
    white_bg_url = imagery.get("white_bg_url")
    targets: list[tuple[str, str, str]] = []
    for issue in imagery.get("image_issues", []):
        idx, url = issue.get("index"), issue.get("url")
        if idx is None or not url:
            continue
        if white_bg_url and idx == 1:
            continue  # the combined whitebg fix below covers the main image
        targets.append((f"img{idx}", "upscale", url))
    if white_bg_url:
        targets.append(("whitebg", "white_bg", white_bg_url))
    return targets


def _plural(n: int, one: str, many: str | None = None) -> str:
    """'1 fix' / '2 fixes' — the count plus its singular/plural noun."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


def _cost_line(total: int, price: float | None, unit: str) -> str:
    """The '≈ $X (N × $Y per unit)' estimate line, or '' when no price is set.

    Shared by the image-fix and copy-rewrite cost modals so both read identically.
    An estimate only — the real charge is the provider's (see the per-feature price
    helpers). Empty when the operator left the price unset (counts-only preflight).
    """
    if price is None or total <= 0:
        return ""
    return (f"Estimated cost: ≈ ${round(total * price, 2):.2f} "
            f"({total} × ${price:.2f} per {unit})")


def _enhance_slot_state(sid: int, slot: str, jobs_map: dict, ext: str) -> dict:
    """UI state for one enhance slot: done (cached) / queued / processing / error / none.

    The cache file is the source of truth for "done" (a job row may be absent for a
    slot enhanced before this table existed, or pruned). Only when it isn't cached
    do we fall back to the job's status. ``inline_url`` embeds the result on the
    page (same-origin, CSP-allowed); ``download_url`` serves it as a file.
    """
    if ci_images.has_enhanced_image(sid, slot, ext):
        status = "done"
    else:
        job = jobs_map.get((sid, slot))
        status = job["status"] if job else "none"
    return {
        "slot": slot,
        "status": status,
        "inline_url": url_for("pages.pdp_scoring_enhanced", sid=sid, slot=slot),
        "download_url": url_for("pages.pdp_scoring_enhanced_download", sid=sid, slot=slot),
    }


def _annotate_enhance(items: list[dict], jobs_map: dict, ext: str) -> dict:
    """Attach enhance state to each item's imagery issues; return page-level state.

    Mutates each item in place: every flagged gallery image gets ``issue["enhance"]``
    and a white-bg main image gets ``imagery["white_bg_fix"]`` (both the dict from
    :func:`_enhance_slot_state`). Image 1 is left without a per-image control when a
    white-bg fix applies (the combined fix handles it). Also sets
    ``item["enhance_summary"]`` — per-item counts (total / done / queued /
    processing / failed) for the row's "Fix all" / ZIP controls and status badge.

    Returns ``{"pending": bool, "batch": {...}}``: ``pending`` is True when any
    slot is still queued/processing (so the page keeps auto-refreshing), and
    ``batch`` rolls the per-item counts up across the whole table to drive the
    batch action bar (fix-all / ZIP / retry / cancel) and its progress line.
    """
    pending = False
    # Batch roll-up across every item on the page (the "fix everything" scope).
    batch = {"total": 0, "done": 0, "queued": 0, "processing": 0, "failed": 0}
    for it in items:
        if not it["result"]:
            continue
        imagery = next(
            (d for d in it["result"].get("dimensions", []) if d.get("key") == "imagery"),
            None,
        )
        if not imagery:
            continue
        sid = it["id"]
        white_bg_url = imagery.get("white_bg_url")
        # Per-item tally by state, so the row badge can read "M done · K failed".
        counts = {"total": 0, "done": 0, "queued": 0, "processing": 0, "failed": 0}

        def _tally(state):
            counts["total"] += 1
            status = state["status"]
            if status == "done":
                counts["done"] += 1
            elif status == "queued":
                counts["queued"] += 1
            elif status == "processing":
                counts["processing"] += 1
            elif status == "error":
                counts["failed"] += 1

        for issue in imagery.get("image_issues", []):
            idx, url = issue.get("index"), issue.get("url")
            if idx is None or not url or (white_bg_url and idx == 1):
                continue
            state = _enhance_slot_state(sid, f"img{idx}", jobs_map, ext)
            issue["enhance"] = state
            _tally(state)
        if white_bg_url:
            state = _enhance_slot_state(sid, "whitebg", jobs_map, ext)
            imagery["white_bg_fix"] = state
            _tally(state)

        pending = pending or counts["queued"] > 0 or counts["processing"] > 0
        it["enhance_summary"] = {
            **counts,
            "in_flight": counts["queued"] + counts["processing"],
            "all_done": counts["total"] > 0 and counts["done"] == counts["total"],
            "any_done": counts["done"] > 0,
            "zip_url": url_for("pages.pdp_scoring_enhance_zip", sid=sid),
            "fix_all_url": url_for("pages.pdp_scoring_enhance_all", sid=sid),
        }
        for key in batch:
            batch[key] += counts[key]

    batch.update({
        "any": batch["total"] > 0,
        "any_done": batch["done"] > 0,
        "any_failed": batch["failed"] > 0,
        "in_flight": batch["queued"] + batch["processing"],
    })
    return {"pending": pending, "batch": batch}


# Claim priority for batch image fixes: main-image white-background (the hard
# Walmart main-image gate) drains ahead of gallery upscales, so an interrupted
# batch delivers the compliance wins first (see image_jobs.claim_next_image_job).
_WHITEBG_PRIORITY = 10
_UPSCALE_PRIORITY = 0


def _target_priority(operation: str) -> int:
    """Claim priority for an enhance operation (white_bg ahead of upscale)."""
    return _WHITEBG_PRIORITY if operation == "white_bg" else _UPSCALE_PRIORITY


def _batch_enhance_plan(rows: list, jobs_map: dict, ext: str) -> dict:
    """What a batch fix over ``rows`` would newly run, plus its cost estimate.

    Walks every row's :func:`_enhance_targets` and classifies each slot:
    - already cached (``has_enhanced_image``) → counted as ``already_fixed`` and
      skipped (never re-spent);
    - already ``queued``/``processing`` (from ``jobs_map``) → skipped too, since
      it's going to run regardless and re-enqueue is a no-op (no *new* charge);
    - otherwise (no job yet, or a prior ``error`` to retry) → a job to enqueue,
      which is a new metered call.

    So the returned counts reflect the *additional* spend a confirm would incur —
    exactly what the cost-preflight modal shows and what the enqueue route runs
    (both call this, so estimate and action never diverge). ``source_url`` on each
    job originates from our own scrape (SSRF-safe, via ``_enhance_targets``).
    """
    plan_jobs: list[dict] = []
    already_fixed = 0
    whitebg = upscale = 0
    item_ids: set[int] = set()
    for row in rows:
        sid = row["id"]
        for slot, operation, source_url in _enhance_targets(row):
            if ci_images.has_enhanced_image(sid, slot, ext):
                already_fixed += 1
                continue
            job = jobs_map.get((sid, slot))
            if job is not None and job["status"] in ("queued", "processing"):
                continue  # already going to run — not a new charge
            plan_jobs.append({
                "sid": sid, "slot": slot, "operation": operation,
                "source_url": source_url, "priority": _target_priority(operation),
            })
            item_ids.add(sid)
            if operation == "white_bg":
                whitebg += 1
            else:
                upscale += 1
    total = whitebg + upscale
    price = image_enhance.price_per_image()
    return {
        "jobs": plan_jobs,
        "whitebg": whitebg,
        "upscale": upscale,
        "total": total,
        "already_fixed": already_fixed,
        "items": len(item_ids),
        "price_per_image": price,
        "est_cost": round(total * price, 2) if price is not None else None,
    }


def _scope_scored_rows() -> list:
    """Resolve the scored rows a batch bulk action targets, from the POST form.

    Shared by the image-fix and copy-rewrite batch actions (both act on the same
    scoring-results selection). ``all=1`` → the whole current session batch (the
    "everything" scope); otherwise the ticked ``item_ids`` (the selected-subset
    scope), each resolved through :func:`jobs.get_items` so a foreign or unknown id
    is silently dropped (IDOR guard). Returns ``[]`` when nothing is selected.
    """
    if request.form.get("all"):
        return _batch_rows()
    try:
        ids = [int(v) for v in request.form.getlist("item_ids")]
    except ValueError:
        abort(400, description="Invalid item selection.")
    if not ids:
        return []
    return jobs.get_items(get_db(), ids, g.user["id"])


def _owned_scored_row_or_404(sid: int):
    """Return the scored row ``sid`` if the signed-in user owns it, else 404."""
    rows = jobs.get_items(get_db(), [sid], g.user["id"])
    if not rows:
        abort(404)
    return rows[0]


@bp.route("/app/pdp-scoring/enhance/<int:sid>/<int:index>", methods=["POST"])
@login_required
def pdp_scoring_enhance(sid, index):
    """Queue an upscale of a flagged gallery image; the worker runs it (async)."""
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    row = _owned_scored_row_or_404(sid)
    source_url = _flagged_image_url(row, index)
    if not source_url:
        abort(404, description="No image available to enhance.")
    image_jobs.enqueue_image_job(
        get_db(), user_id=g.user["id"], scored_item_id=sid,
        slot=f"img{index}", operation="upscale", source_url=source_url,
        ext=image_enhance.output_ext(),
    )
    return redirect(url_for("pages.pdp_scoring_results"))


@bp.route("/app/pdp-scoring/whitebg/<int:sid>", methods=["POST"])
@login_required
def pdp_scoring_whitebg(sid):
    """Queue the combined main-image fix (white background + upscale), async."""
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    row = _owned_scored_row_or_404(sid)
    source_url = _white_bg_image_url(row)
    if not source_url:
        abort(404, description="No image available to enhance.")
    image_jobs.enqueue_image_job(
        get_db(), user_id=g.user["id"], scored_item_id=sid,
        slot="whitebg", operation="white_bg", source_url=source_url,
        ext=image_enhance.output_ext(),
    )
    return redirect(url_for("pages.pdp_scoring_results"))


@bp.route("/app/pdp-scoring/enhance-all/<int:sid>", methods=["POST"])
@login_required
def pdp_scoring_enhance_all(sid):
    """Queue every available fix for one item in one go ("Fix all images").

    Each is a metered provider call, so the template guards the button with a
    confirm; here we just enqueue (idempotently — already-cached or in-flight slots
    are skipped by :func:`app.image_jobs.enqueue_image_job`).
    """
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    row = _owned_scored_row_or_404(sid)
    targets = _enhance_targets(row)
    ext = image_enhance.output_ext()
    queued = 0
    for slot, operation, source_url in targets:
        if ci_images.has_enhanced_image(sid, slot, ext):
            continue  # already fixed — don't re-spend a metered call
        image_jobs.enqueue_image_job(
            get_db(), user_id=g.user["id"], scored_item_id=sid,
            slot=slot, operation=operation, source_url=source_url, ext=ext,
        )
        queued += 1
    logger.info("Fix-all enqueued %d image job(s) for sid=%s user_id=%s", queued, sid, g.user["id"])
    return redirect(url_for("pages.pdp_scoring_results"))


@bp.route("/app/pdp-scoring/enhance-batch/estimate", methods=["POST"])
@login_required
def pdp_scoring_enhance_batch_estimate():
    """Cost-preflight for a batch fix: JSON counts + a dollar estimate, no enqueue.

    The results page's cost modal POSTs the current scope (``all=1`` or ticked
    ``item_ids``) here and renders the response before the user confirms. Counts
    reflect only the *additional* metered calls a confirm would make — already
    cached and already in-flight slots are excluded (see :func:`_batch_enhance_plan`).
    """
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    rows = _scope_scored_rows()
    ext = image_enhance.output_ext()
    jobs_map = image_jobs.jobs_for_items(
        get_db(), [r["id"] for r in rows], g.user["id"]
    ) if rows else {}
    plan = _batch_enhance_plan(rows, jobs_map, ext)
    # Human-readable strings the (generic) cost modal renders verbatim, alongside
    # the raw counts. Building them here keeps per-feature wording server-side.
    parts = []
    if plan["whitebg"]:
        parts.append(_plural(plan["whitebg"], "white-background fix", "white-background fixes"))
    if plan["upscale"]:
        parts.append(_plural(plan["upscale"], "upscale"))
    summary = (" + ".join(parts) if parts else "0 fixes") + \
        f" across {_plural(plan['items'], 'item')}."
    skipped = f"{plan['already_fixed']} already fixed — skipped." if plan["already_fixed"] else ""
    return jsonify({
        "items": plan["items"],
        "whitebg": plan["whitebg"],
        "upscale": plan["upscale"],
        "total": plan["total"],
        "already_fixed": plan["already_fixed"],
        "price_per_image": plan["price_per_image"],
        "est_cost": plan["est_cost"],
        # Uniform display fields the cost modal renders (see pdp_results.js).
        "summary": summary,
        "skipped": skipped,
        "cost": _cost_line(plan["total"], plan["price_per_image"], "fix"),
        "note": ("Each fix is a metered AI call. Images already fixed or in progress "
                 "are skipped — you’re only charged for new fixes."),
    })


@bp.route("/app/pdp-scoring/enhance-batch", methods=["POST"])
@login_required
def pdp_scoring_enhance_batch():
    """Enqueue every needed fix across a batch scope ("fix all" or selected items).

    Each fix is a metered provider call, so the UI guards this behind the
    cost-preflight modal; here we build the same plan the estimate showed and
    enqueue it. Idempotent/cache-skipping via :func:`_batch_enhance_plan` +
    :func:`app.image_jobs.enqueue_image_job`, so a re-run only fills gaps.
    """
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    rows = _scope_scored_rows()
    if not rows:
        logger.info("Batch fix: nothing selected, user_id=%s", g.user["id"])
        return redirect(url_for("pages.pdp_scoring_results"))
    db = get_db()
    ext = image_enhance.output_ext()
    jobs_map = image_jobs.jobs_for_items(db, [r["id"] for r in rows], g.user["id"])
    plan = _batch_enhance_plan(rows, jobs_map, ext)
    for job in plan["jobs"]:
        image_jobs.enqueue_image_job(
            db, user_id=g.user["id"], scored_item_id=job["sid"], slot=job["slot"],
            operation=job["operation"], source_url=job["source_url"], ext=ext,
            priority=job["priority"],
        )
    scope = "all" if request.form.get("all") else "selected"
    logger.info(
        "Batch fix (%s): enqueued %d image job(s) across %d item(s), %d already fixed, user_id=%s",
        scope, len(plan["jobs"]), plan["items"], plan["already_fixed"], g.user["id"],
    )
    return redirect(url_for("pages.pdp_scoring_results"))


@bp.route("/app/pdp-scoring/enhance-batch/retry-failed", methods=["POST"])
@login_required
def pdp_scoring_enhance_batch_retry():
    """Re-queue every failed image fix in the batch scope ("Retry failed")."""
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    rows = _scope_scored_rows()
    n = image_jobs.requeue_failed_for_items(
        get_db(), [r["id"] for r in rows], g.user["id"]
    )
    logger.info("Batch retry: re-queued %d failed image job(s), user_id=%s", n, g.user["id"])
    return redirect(url_for("pages.pdp_scoring_results"))


@bp.route("/app/pdp-scoring/enhance-batch/cancel", methods=["POST"])
@login_required
def pdp_scoring_enhance_batch_cancel():
    """Cancel still-queued image fixes in the batch scope ("Stop").

    In-flight (processing) calls can't be refunded, so only queued rows are
    dropped (see :func:`app.image_jobs.cancel_queued_for_items`).
    """
    if not image_enhance.is_configured():
        abort(503, description="Image enhancement is not configured.")
    rows = _scope_scored_rows()
    n = image_jobs.cancel_queued_for_items(
        get_db(), [r["id"] for r in rows], g.user["id"]
    )
    logger.info("Batch cancel: dropped %d queued image job(s), user_id=%s", n, g.user["id"])
    return redirect(url_for("pages.pdp_scoring_results"))


def _serve_enhanced_file(sid: int, slot: str, *, as_attachment: bool):
    """Serve a cached enhanced image for an owned item + valid slot, or 404.

    Shared by the inline (``<img>``) and download routes. Ownership is checked
    (IDOR), the slot is validated against :data:`_ENHANCE_SLOT_RE`, and the file
    must already exist in the cache (the worker produces it) — this route never
    calls the provider.
    """
    from flask import send_file

    if not _ENHANCE_SLOT_RE.match(slot):
        abort(404)
    _owned_scored_row_or_404(sid)  # IDOR guard — must own the scored item
    ext = image_enhance.output_ext()
    path = ci_images.enhanced_image_path(sid, slot, ext)
    if not path or not ci_images.has_enhanced_image(sid, slot, ext):
        abort(404)
    download_name = (
        "main-image-fixed" if slot == "whitebg" else f"image-{slot[3:]}-2000px"
    )
    return send_file(
        path, mimetype=image_enhance.output_mime(),
        as_attachment=as_attachment, download_name=f"{download_name}.{ext}", max_age=0,
    )


@bp.route("/app/pdp-scoring/enhanced/<int:sid>/<slot>")
@login_required
def pdp_scoring_enhanced(sid, slot):
    """Serve a cached enhanced image inline (for the results page ``<img>``)."""
    return _serve_enhanced_file(sid, slot, as_attachment=False)


@bp.route("/app/pdp-scoring/enhanced/<int:sid>/<slot>/download")
@login_required
def pdp_scoring_enhanced_download(sid, slot):
    """Serve a cached enhanced image as a file download."""
    return _serve_enhanced_file(sid, slot, as_attachment=True)


@bp.route("/app/pdp-scoring/enhance-all/<int:sid>/download.zip")
@login_required
def pdp_scoring_enhance_zip(sid):
    """Bundle every finished fix for one item into a single ZIP, named by position.

    Includes only slots already cached (``done``); a fix still in flight is simply
    absent from this build. 404 when nothing is ready yet.
    """
    import io
    import zipfile

    from flask import send_file

    row = _owned_scored_row_or_404(sid)
    ext = image_enhance.output_ext()
    item = row["item_id"] or sid
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for slot, _operation, _url in _enhance_targets(row):
            path = ci_images.enhanced_image_path(sid, slot, ext)
            if not path or not ci_images.has_enhanced_image(sid, slot, ext):
                continue
            arcname = (
                f"main-image-fixed.{ext}" if slot == "whitebg"
                else f"image-{slot[3:]}-2000px.{ext}"
            )
            zf.write(path, arcname=arcname)
            written += 1
    if not written:
        abort(404, description="No finished image fixes to download yet.")
    buf.seek(0)
    logger.info("Enhanced-image ZIP: %d file(s) for sid=%s user_id=%s", written, sid, g.user["id"])
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name=f"fixed-images-{item}.zip", max_age=0,
    )


def _enhanced_zip_arcname(slot: str, ext: str) -> str:
    """Filename for a fixed image inside the batch ZIP, by its slot/position."""
    return (
        f"main-image-fixed.{ext}" if slot == "whitebg"
        else f"image-{slot[3:]}-2000px.{ext}"
    )


@bp.route("/app/pdp-scoring/enhance-batch/download.zip")
@login_required
def pdp_scoring_enhance_batch_zip():
    """Bundle every finished fix across the whole batch, organized by item.

    Layout: one folder per item (``item-<item#>/``, or ``item-<sid>/`` when the
    SKU number is unknown), fixed images named by gallery position, plus a
    top-level ``manifest.csv`` mapping each fixed file back to the item and its
    original image URL — so the user knows what to re-upload where (the manifest
    is half the value at batch scale). Only slots already cached (``done``) are
    included; in-flight fixes are simply absent. 404 when nothing is ready.
    """
    import csv
    import io
    import zipfile

    from flask import send_file

    rows = _batch_rows()
    ext = image_enhance.output_ext()
    buf = io.BytesIO()
    manifest_rows: list[list[str]] = []
    written = 0
    # Item-number collisions (a re-scored SKU appearing twice) would otherwise
    # clobber each other's folder — suffix the sid to keep each row's folder unique.
    seen_folders: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in rows:
            sid = row["id"]
            item_no = row["item_id"] or sid
            folder = f"item-{item_no}"
            if folder in seen_folders:
                folder = f"item-{item_no}-{sid}"
            product = row["title"] or ""
            row_has_file = False
            for slot, _operation, source_url in _enhance_targets(row):
                path = ci_images.enhanced_image_path(sid, slot, ext)
                if not path or not ci_images.has_enhanced_image(sid, slot, ext):
                    continue
                arcname = _enhanced_zip_arcname(slot, ext)
                zf.write(path, arcname=f"{folder}/{arcname}")
                manifest_rows.append([
                    str(item_no), product, source_url or "", f"{folder}/{arcname}",
                ])
                written += 1
                row_has_file = True
            if row_has_file:
                seen_folders.add(folder)
        if written:
            # Written last so it sees every folder name chosen above.
            manifest = io.StringIO()
            w = csv.writer(manifest)
            w.writerow(["Item ID", "Product Name", "Original Image URL", "Fixed File"])
            w.writerows(manifest_rows)
            zf.writestr("manifest.csv", manifest.getvalue())
    if not written:
        abort(404, description="No finished image fixes to download yet.")
    buf.seek(0)
    logger.info(
        "Batch enhanced-image ZIP: %d file(s) across %d item(s) user_id=%s",
        written, len(seen_folders), g.user["id"],
    )
    return send_file(
        buf, mimetype="application/zip", as_attachment=True,
        download_name="fixed-images-batch.zip", max_age=0,
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


# Statuses where the worker is actively processing a copy item; everything else
# (fetched / done / blocked / error) has settled for now.
_COPY_ACTIVE = ("queued", "fetching", "gen_queued", "generating")


def _copy_progress(items: list[dict]) -> dict:
    """Progress + time-left for an in-flight copy batch (mirrors _score_summary).

    ``done`` counts items that have settled — the current copy fetched and resting
    ('fetched'), fully generated ('done'), or failed — i.e. everything not still
    being worked. ``pending`` is the active-work count that drives the
    remaining-time estimate (reusing :func:`_eta_label`). A two-step batch shows a
    fresh bar for each wave: fills during fetch, then again during generation.
    """
    total = len(items)
    pending = sum(1 for it in items if it["status"] in _COPY_ACTIVE)
    return {
        "total": total,
        "done": total - pending,
        "pending": pending,
        "eta": _eta_label(pending),
    }


def _fmt_captured(ts: str | None) -> str | None:
    """Format a stored ``YYYY-MM-DD HH:MM:SS`` (UTC) timestamp for display.

    Returns a friendly ``Mon DD, YYYY HH:MM AM/PM UTC`` string, or the raw value
    if it doesn't parse (so a format change never 500s the results page).
    """
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").strftime("%b %d, %Y %I:%M %p UTC")
    except (ValueError, TypeError):
        return ts


def _copy_row_view(row) -> dict:
    """Shape a copy_items row for templates / JSON.

    Drops the internal ``record`` blob (the full PdpRecord kept only for the
    generation phase) so it never reaches the client.
    """
    current = json.loads(row["current_json"]) if row["current_json"] else None
    # When the current copy was reused from a prior score, it carries the capture
    # time — lift it to the view so the page can show its freshness — then drop the
    # internal record/metadata so neither reaches the client.
    captured_at = _fmt_captured(current.pop("captured_at", None)) if current else None
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
        "captured_at": captured_at,
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
        brand = pdp.clean_brand(request.form.get("brand"))

        # Brand is required on copy creation — it anchors the generated copy, so we
        # ask for it rather than scraping. Enforce at the boundary, not only via the
        # browser's `required` (a direct POST bypasses client-side validation).
        error = None
        if not accepted:
            error = "No valid item URLs were provided."
        elif not brand:
            error = "Enter a brand for these products."
        if error:
            return (
                render_template(
                    "app/pdp_copy.html",
                    breadcrumb="Copy Content Studio · Product Detail Page Copy Content Creation",
                    active_nav="pdp-copy",
                    max_items=pdp.MAX_ITEMS,
                    error=error,
                    brand_value=request.form.get("brand", ""),  # keep what they typed
                    **_portfolio_pick_context(),
                ),
                400,
            )

        items = [
            {"url": url, "item": pdp.item_number_from_url(url), "brand": brand}
            for url in accepted
        ]
        ids = copy_jobs.enqueue_copy_items(get_db(), g.user["id"], items)
        session[_COPY_BATCH_KEY] = ids
        session.pop(_COPY_MISSING_KEY, None)  # fresh batch — drop any prior "missing" note
        logger.info(
            "PDP copy: enqueued %d item(s) for current-copy fetch, %d rejected, user_id=%s",
            len(ids), len(rejected), g.user["id"],
        )
        return redirect(url_for("pages.pdp_copy_results"))

    logger.info("Serving PDP Copy Content Creation intake")
    return render_template(
        "app/pdp_copy.html",
        breadcrumb="Copy Content Studio · Product Detail Page Copy Content Creation",
        active_nav="pdp-copy",
        max_items=pdp.MAX_ITEMS,
        url_prefix=pdp.WALMART_IP_PREFIX,
        **_portfolio_pick_context(),
    )


# ── Portfolio Management ─────────────────────────────────────────────────────
# The user's durable, curated catalog of managed products. CSV import / single
# add / delete here; the catalog then surfaces as the first pick-list on the
# Content Scoring and Product Copy intakes.

_PORTFOLIO_NAV = "portfolio"


@bp.route("/app/portfolio")
@login_required
def portfolio_home():
    """Portfolio Management screen: list managed items, with import/add/filter."""
    logger.info("Serving Portfolio Management, user_id=%s", g.user["id"])
    db = get_db()
    uid = g.user["id"]
    return render_template(
        "app/portfolio.html",
        breadcrumb="Portfolio Management · Managed Items",
        active_nav=_PORTFOLIO_NAV,
        items=portfolio.list_items(db, uid),
        brands=portfolio.distinct_brands(db, uid),
    )


@bp.route("/app/portfolio/import", methods=["POST"])
@login_required
def portfolio_import():
    """Import managed products from an uploaded CSV, skipping existing items."""
    csv_file = request.files.get("csv")
    if csv_file is None or not csv_file.filename:
        flash("Choose a CSV file to import.", "error")
        return redirect(url_for("pages.portfolio_home"))

    rows, invalid = portfolio.parse_csv(csv_file)
    if not rows:
        # Nothing usable — tell the user rather than claiming a silent success.
        flash(
            "No valid items found in that CSV. Each row needs a \"Brand\" and a "
            "\"Walmart Item URL\" (a full product URL).",
            "error",
        )
        return redirect(url_for("pages.portfolio_home"))

    result = portfolio.import_rows(get_db(), g.user["id"], rows)
    # Summarize the outcome so the user knows exactly what happened: added vs
    # skipped (already present / duplicate in the file) vs invalid (no URL).
    parts = [f"{result['added']} item{'' if result['added'] == 1 else 's'} added"]
    if result["skipped"]:
        parts.append(f"{result['skipped']} skipped (already in your portfolio)")
    if invalid:
        parts.append(f"{invalid} row{'' if invalid == 1 else 's'} ignored (missing brand or URL)")
    flash(" · ".join(parts), "ok")
    return redirect(url_for("pages.portfolio_home"))


@bp.route("/app/portfolio/add", methods=["POST"])
@login_required
def portfolio_add():
    """Add a single managed product from the inline form."""
    ok, error = portfolio.add_item(
        get_db(),
        g.user["id"],
        brand=request.form.get("brand"),
        walmart_url_raw=request.form.get("walmart_url", ""),
        amazon_url_raw=request.form.get("amazon_url"),
    )
    flash("Item added to your portfolio." if ok else error, "ok" if ok else "error")
    return redirect(url_for("pages.portfolio_home"))


@bp.route("/app/portfolio/delete/<int:item_id>", methods=["POST"])
@login_required
def portfolio_delete(item_id: int):
    """Delete one managed product the user owns."""
    removed = portfolio.delete_item(get_db(), g.user["id"], item_id)
    flash("Item removed." if removed else "That item is no longer in your portfolio.",
          "ok" if removed else "error")
    return redirect(url_for("pages.portfolio_home"))


def _csv_template_response(filename: str, header: list[str], rows: list[list[str]]):
    """Build a downloadable CSV template: one header row + illustrative examples.

    Shared by every screen that accepts a CSV upload, so each "Download a template"
    link produces a correctly-shaped starter file (header the user keeps, example
    rows they replace). ``filename`` sets the download name via Content-Disposition.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "text/csv; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename={filename}"
    return resp


@bp.route("/app/portfolio/template.csv")
@login_required
def portfolio_template():
    """Download a blank CSV with the three expected column headers + one example."""
    # One illustrative row so the expected URL shape is obvious; the user replaces it.
    return _csv_template_response(
        "portfolio-template.csv",
        list(portfolio.TEMPLATE_HEADERS),
        [["Tabasco", "https://www.walmart.com/ip/10294528",
          "https://www.amazon.com/dp/B00004S1C5"]],
    )


# CSV templates for the URL-upload screens. All of these parse via pdp.urls_from_csv
# (any cell that looks like an http(s) URL is picked up, header ignored), so each
# template is a single URL column with example rows matching what that screen accepts.

@bp.route("/app/pdp-scoring/template.csv")
@login_required
def pdp_scoring_template():
    """Download a CSV template for Content Scoring (Walmart item URLs)."""
    return _csv_template_response(
        "content-scoring-template.csv",
        ["Walmart Item URL"],
        [["https://www.walmart.com/ip/10294528"],
         ["https://www.walmart.com/ip/592012115"]],
    )


@bp.route("/app/pdp-copy/template.csv")
@login_required
def pdp_copy_template():
    """Download a CSV template for Product Copy (Walmart item URLs)."""
    return _csv_template_response(
        "product-copy-template.csv",
        ["Walmart Item URL"],
        [["https://www.walmart.com/ip/10294528"],
         ["https://www.walmart.com/ip/592012115"]],
    )


@bp.route("/app/import-images/template.csv")
@login_required
def import_images_template():
    """Download a CSV template for Acquire Product Images (product or image URLs)."""
    return _csv_template_response(
        "acquire-images-template.csv",
        ["Product or Image URL"],
        [["https://www.walmart.com/ip/10294528"],
         ["https://m.media-amazon.com/images/I/71abcdefgh._AC_SL1500_.jpg"]],
    )


@bp.route("/app/seasonal/template.csv")
@login_required
def seasonal_template():
    """Download a CSV template for Seasonal Campaigns (Walmart or Amazon product URLs)."""
    return _csv_template_response(
        "seasonal-products-template.csv",
        ["Product URL"],
        [["https://www.walmart.com/ip/10294528"],
         ["https://www.amazon.com/dp/B00004S1C5"]],
    )


@bp.route("/app/pdp-copy/results")
@login_required
def pdp_copy_results():
    """Show the current copy batch: current copy, and new copy once generated.

    Also lists any selected items that had no copy when "View copy results" ran
    (``_COPY_MISSING_KEY``), so the user sees copy wasn't created for them. Read
    (not popped) so it survives the in-flight auto-refresh; it's cleared when a
    fresh copy batch is created.
    """
    items = [_copy_row_view(r) for r in _copy_batch_rows()]
    return render_template(
        "app/pdp_copy_results.html",
        breadcrumb="Copy Content Studio · Product Detail Page Copy Content Creation",
        active_nav="pdp-copy",
        items=items,
        missing=session.get(_COPY_MISSING_KEY, []),
        progress=_copy_progress(items),
    )


@bp.route("/app/pdp-scoring/item/<int:sid>")
@login_required
def pdp_scoring_item(sid):
    """Open the whole run a scored item belongs to (from a dashboard / View All row).

    Points the session batch at every item submitted in the same run as ``sid``
    (its batch siblings), then reuses the standard scoring results page — so a run
    of several items shows all of them, with polling / PDF / layout unchanged.
    Ownership-checked via :func:`jobs.batch_ids_for_item` — a foreign or missing
    id yields no ids and 404s.
    """
    ids = jobs.batch_ids_for_item(get_db(), sid, g.user["id"])
    if not ids:
        abort(404)
    session[_BATCH_KEY] = ids
    return redirect(url_for("pages.pdp_scoring_results"))


@bp.route("/app/pdp-copy/item/<int:cid>")
@login_required
def pdp_copy_item(cid):
    """Open the whole run a copy item belongs to (from a dashboard / View All row).

    Mirrors :func:`pdp_scoring_item` for the copy queue, via
    :func:`copy_jobs.batch_ids_for_copy_item`.
    """
    ids = copy_jobs.batch_ids_for_copy_item(get_db(), cid, g.user["id"])
    if not ids:
        abort(404)
    session[_COPY_BATCH_KEY] = ids
    session.pop(_COPY_MISSING_KEY, None)  # viewing a specific copy — no "missing" note
    return redirect(url_for("pages.pdp_copy_results"))


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


@bp.route("/app/pdp-copy/results.xlsx")
@login_required
def pdp_copy_results_xlsx():
    """Download the current copy batch's generated copy as an Excel file."""
    from datetime import date

    from flask import Response

    from app.copy_export import build_copy_xlsx

    items = [_copy_row_view(r) for r in _copy_batch_rows()]
    data = build_copy_xlsx(items)
    filename = f"pdp-new-copy-{date.today().isoformat()}.xlsx"
    logger.info("Copy XLSX export: %d item(s), user_id=%s", len(items), g.user["id"])
    return Response(
        data,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/app/pdp-copy/results.csv")
@login_required
def pdp_copy_results_csv():
    """Download the current copy batch's generated copy as CSV (no Excel needed)."""
    from datetime import date

    from flask import Response

    from app.copy_export import build_copy_csv

    items = [_copy_row_view(r) for r in _copy_batch_rows()]
    data = build_copy_csv(items)
    filename = f"pdp-new-copy-{date.today().isoformat()}.csv"
    logger.info("Copy CSV export: %d item(s), user_id=%s", len(items), g.user["id"])
    return Response(
        data,
        mimetype="text/csv",  # Flask appends '; charset=utf-8'
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


def _copy_match_key(row) -> str | None:
    """Identity used to match a scored item to its existing copy: item id, else URL."""
    return (row["item_id"] or row["url"]) or None


def _copy_batch_plan(rows: list, states: dict) -> dict:
    """What a batch copy rewrite over scored ``rows`` would newly generate + its est.

    Only **scored** items are eligible (a rewrite improves already-scored content).
    An item is skipped when it already has a done or in-flight copy (``states`` from
    :func:`copy_jobs.copy_states_for_items`) — mirrors the image cache-skip, so a
    re-run only fills gaps and never silently re-spends; an item whose only prior
    copy attempt failed is included (a retry). Of the included items, ``reused``
    already carry the scored ``record_json`` so generation skips the browser
    re-fetch; ``refetch`` must re-fetch the PDP first. Returns the ready-to-enqueue
    ``prefetched`` / ``to_fetch`` payloads (same shapes ``copy_jobs`` expects) plus
    counts and the (optional, approximate) dollar estimate.
    """
    prefetched: list[dict] = []
    to_fetch: list[dict] = []
    already_copied = 0
    for r in rows:
        if r["status"] != "scored" or not r["url"]:
            continue
        key = _copy_match_key(r)
        st = states.get(key) if key else None
        if st and (st["has_done"] or st["has_inflight"]):
            already_copied += 1  # already covered — don't re-spend
            continue
        if r["record_json"]:
            record = json.loads(r["record_json"])
            current = {
                "title": record.get("title"),
                "bullets": record.get("bullets", []),
                "description": record.get("description", ""),
                "score": json.loads(r["result_json"]) if r["result_json"] else {},
                "record": record,  # full PdpRecord the generation phase rebuilds
                "captured_at": r["updated_at"],  # when the scoring fetch happened
            }
            prefetched.append({
                "url": r["url"], "item_id": r["item_id"], "brand": r["brand"],
                "title": record.get("title"), "current": current,
                "current_overall": r["overall"], "keywords": record.get("target_keywords"),
            })
        else:
            to_fetch.append({"url": r["url"], "item": r["item_id"], "brand": r["brand"]})
    total = len(prefetched) + len(to_fetch)
    price = copygen.price_per_item()
    return {
        "prefetched": prefetched, "to_fetch": to_fetch,
        "reused": len(prefetched), "refetch": len(to_fetch),
        "total": total, "already_copied": already_copied, "items": total,
        "price_per_item": price,
        "est_cost": round(total * price, 2) if price is not None else None,
    }


def _annotate_copy(items: list[dict], states: dict) -> dict:
    """Attach each scored item's copy state (for the row badge) + roll up the batch.

    Mutates each scored item: sets ``it['copy_state']`` to ``{status, current,
    projected}`` where status is ``done`` / ``in_flight`` / ``failed`` / ``none``
    (matched to its copy by item id or URL via ``states``). (The key is
    ``copy_state``, not ``copy`` — ``it.copy`` in Jinja would resolve to the dict's
    built-in ``.copy`` method, not the item.) Returns the page roll-up the copy bar
    reads: counts per bucket, whether the batch has any scored item at all
    (``any``), whether any copy exists to view (``any_copy``), and whether any copy
    is still generating (``in_flight_any``, which keeps the page's auto-refresh on).
    """
    batch = {"scored": 0, "done": 0, "in_flight": 0, "failed": 0, "none": 0}
    for it in items:
        if it["status"] != "scored":
            continue
        batch["scored"] += 1
        key = it["item_id"] or it["url"]
        st = states.get(key) if key else None
        if st and st["has_done"]:
            bucket = "done"
        elif st and st["has_inflight"]:
            bucket = "in_flight"
        elif st and st["has_failed"]:
            bucket = "failed"
        else:
            bucket = "none"
        batch[bucket] += 1
        it["copy_state"] = {
            "status": bucket,
            "current": st["latest_current"] if st else None,
            "projected": st["latest_projected"] if st else None,
        }
    batch["any"] = batch["scored"] > 0
    batch["any_copy"] = batch["done"] + batch["in_flight"] + batch["failed"] > 0
    batch["in_flight_any"] = batch["in_flight"] > 0
    return batch


@bp.route("/app/pdp-scoring/create-copy", methods=["POST"])
@login_required
def pdp_scoring_create_copy():
    """Batch copy rewrite from the scoring screen: create copy jobs for a scope.

    Scope is ``all=1`` (every scored item in the batch) or the ticked ``item_ids``
    (:func:`_scope_scored_rows`). Each metered rewrite is guarded by the cost modal;
    here we build the same plan the estimate showed, skip items already covered
    (done/in-flight copy), and enqueue the rest — prefetched rows (reusing the
    scored PdpRecord) straight to generation, the others via the fetch path. Sets
    the copy session batch and redirects to the copy results page.
    """
    db = get_db()
    rows = _scope_scored_rows()
    states = copy_jobs.copy_states_for_items(
        db, g.user["id"], [(r["item_id"], r["url"]) for r in rows]
    ) if rows else {}
    plan = _copy_batch_plan(rows, states)
    if not plan["total"]:
        # Nothing to do — empty scope, or every eligible item already has copy.
        logger.info(
            "Batch copy: nothing to rewrite (%d already had copy), user_id=%s",
            plan["already_copied"], g.user["id"],
        )
        return redirect(url_for("pages.pdp_scoring_results"))
    ids: list[int] = []
    if plan["prefetched"]:
        ids += copy_jobs.create_prefetched_copy_items(db, g.user["id"], plan["prefetched"])
    if plan["to_fetch"]:
        ids += copy_jobs.enqueue_copy_items(db, g.user["id"], plan["to_fetch"], auto_generate=True)
    session[_COPY_BATCH_KEY] = ids
    session.pop(_COPY_MISSING_KEY, None)  # fresh batch — drop any prior "missing" note
    scope = "all" if request.form.get("all") else "selected"
    logger.info(
        "Batch copy (%s): %d reused + %d re-fetch, %d already had copy, user_id=%s",
        scope, plan["reused"], plan["refetch"], plan["already_copied"], g.user["id"],
    )
    return redirect(url_for("pages.pdp_copy_results"))


@bp.route("/app/pdp-scoring/create-copy/estimate", methods=["POST"])
@login_required
def pdp_scoring_copy_estimate():
    """Cost-preflight for a batch copy rewrite: JSON counts (+ optional $), no enqueue.

    Mirrors the image estimate endpoint and returns the same uniform display fields
    the cost modal renders. Counts reflect only items that would be *newly* rewritten
    — already-covered and in-flight items are excluded (see :func:`_copy_batch_plan`).
    """
    db = get_db()
    rows = _scope_scored_rows()
    states = copy_jobs.copy_states_for_items(
        db, g.user["id"], [(r["item_id"], r["url"]) for r in rows]
    ) if rows else {}
    plan = _copy_batch_plan(rows, states)
    detail = []
    if plan["reused"]:
        detail.append(f"{plan['reused']} reuse the scored content")
    if plan["refetch"]:
        detail.append(f"{plan['refetch']} re-fetch the page first")
    summary = _plural(plan["total"], "item") + " will be rewritten" + \
        ((" (" + ", ".join(detail) + ")") if detail else "") + "."
    skipped = f"{plan['already_copied']} already have copy — skipped." if plan["already_copied"] else ""
    return jsonify({
        "items": plan["items"],
        "reused": plan["reused"],
        "refetch": plan["refetch"],
        "total": plan["total"],
        "already_copied": plan["already_copied"],
        "price_per_item": plan["price_per_item"],
        "est_cost": plan["est_cost"],
        # Uniform display fields the cost modal renders (see pdp_results.js).
        "summary": summary,
        "skipped": skipped,
        "cost": _cost_line(plan["total"], plan["price_per_item"], "item"),
        "note": ("Each rewrite is a metered AI call (Claude). Items that already have "
                 "copy or are in progress are skipped; reused items skip the re-fetch."),
    })


@bp.route("/app/pdp-scoring/view-copy")
@login_required
def pdp_scoring_view_copy():
    """Point the copy session batch at the scored batch's copy, then show it.

    The "View copy results" link from the scoring page's copy bar. Scopes to the
    ticked rows when the link carries a selection (``item_ids`` = scored-item ids,
    kept to the current session batch so a foreign id is ignored); with no
    selection it uses the whole batch. For each product it takes the **latest**
    copy created (see :func:`copy_jobs.copy_item_ids_for_items`). 404-free — an
    empty match simply lands on an empty copy results page.
    """
    db = get_db()
    uid = g.user["id"]
    rows = _batch_rows()
    selected = set(request.args.getlist("item_ids"))
    if selected:
        rows = [r for r in rows if str(r["id"]) in selected]

    keys = [(r["item_id"], r["url"]) for r in rows]
    ids = copy_jobs.copy_item_ids_for_items(db, uid, keys)
    session[_COPY_BATCH_KEY] = ids

    # Selected products that have no copy yet (keyed item-id-else-URL, matching
    # copy_states_for_items): list them on the results page so the user sees copy
    # wasn't created for them rather than them silently dropping out.
    states = copy_jobs.copy_states_for_items(db, uid, keys)
    missing = [
        {"item_id": r["item_id"], "url": r["url"],
         "title": r["title"] or r["item_id"] or r["url"]}
        for r in rows if (r["item_id"] or r["url"]) not in states
    ]
    session[_COPY_MISSING_KEY] = missing

    logger.info("View copy results from scoring: %d with copy, %d missing, user_id=%s",
                len(ids), len(missing), uid)
    return redirect(url_for("pages.pdp_copy_results"))


@bp.route("/admin/users")
@admin_required
def admin_users():
    """Admin: User Management — list every account with role controls."""
    logger.info("Admin users view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_users.html",
        breadcrumb="Admin · User Management",
        active_nav="admin-users",
        users=users.list_users(),
        # Drives the last-admin lockout guard in the template (also enforced below).
        admin_count=users.count_admins(),
    )


@bp.route("/admin/users", methods=["POST"])
@admin_required
def admin_add_user():
    """Admin provisions a local account (email + role + temporary password).

    POST-only + CSRF + admin-guarded. The password is validated for strength and
    the new account is flagged ``must_change_password`` so the person sets their
    own at first sign-in. Duplicate email or bad input re-flashes on the list.
    """
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    role = (request.form.get("role") or users.ROLE_USER).strip()

    error = validate_credentials(email, password)
    if error is None and role not in users.VALID_ROLES:
        error = "Pick a valid role."
    if error is None:
        try:
            new_id = users.admin_create_user(email, password, role)
        except users.EmailAlreadyRegistered:
            error = "That email is already registered."
        else:
            logger.info("Admin user_id=%s provisioned user_id=%s role=%s",
                        g.user["id"], new_id, role)
            flash(
                f"Created {users.normalize_email(email)}. They'll set their own "
                "password the first time they sign in.", "ok",
            )
            return redirect(url_for("pages.admin_users"))

    flash(error, "error")
    return redirect(url_for("pages.admin_users"))


@bp.route("/admin/users/<int:user_id>/role", methods=["POST"])
@admin_required
def admin_set_role(user_id):
    """Change a user's role (admin <-> user). POST-only + CSRF + admin-guarded.

    Refuses an invalid role, and refuses to demote the **last** admin so the app
    can never be left with no one who can administer it (a lockout). Demoting
    yourself is allowed only while another admin remains — the same last-admin
    rule covers it.
    """
    new_role = (request.form.get("role") or "").strip()
    if new_role not in users.VALID_ROLES:
        abort(400, description="Invalid role.")
    target = users.get_by_id(user_id)
    if target is None:
        abort(404)
    current_role = target["role"]
    if (current_role == users.ROLE_ADMIN and new_role != users.ROLE_ADMIN
            and users.count_admins() <= 1):
        logger.warning("Admin user_id=%s blocked from demoting the last admin (user_id=%s)",
                       g.user["id"], user_id)
        abort(400, description="You can't remove the last admin. Promote another admin first.")
    if new_role != current_role:
        users.set_role(user_id, new_role)
        logger.info("Admin user_id=%s changed role of user_id=%s: %s -> %s",
                    g.user["id"], user_id, current_role, new_role)
    return redirect(url_for("pages.admin_users"))


@bp.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    """Delete a user (and their scored items). POST-only + CSRF + admin-guarded.

    Blocks self-deletion so an admin can't remove their own account by accident,
    and blocks deleting the last remaining admin (a lockout).
    """
    if user_id == g.user["id"]:
        logger.warning("Admin user_id=%s tried to delete their own account", g.user["id"])
        abort(400, description="You can't delete your own account.")
    target = users.get_by_id(user_id)
    if (target is not None and target["role"] == users.ROLE_ADMIN
            and users.count_admins() <= 1):
        logger.warning("Admin user_id=%s blocked from deleting the last admin (user_id=%s)",
                       g.user["id"], user_id)
        abort(400, description="You can't delete the last admin.")
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


# Admin message threads live in their own inbox below (``admin_messages``); the
# former consolidated "User Activity" and "System Activity" screens were removed
# in favor of that single Messages screen (reached from the rail and the bell).


# --- PDP Image Set Creation -----------------------------------------------
#
# From one approved product (facts + a photo) the pipeline produces a set of
# marketplace creative assets. The web app handles intake, the human cutout
# approval gate, and the gallery; the background worker does the generation.

# Breadcrumb/nav used by every image-set screen.
_IMGSET_BREADCRUMB = "Creative Content Studio · Product Detail Page Image Set Creation"
_IMGSET_NAV = "pdp-image-set"
# Upload allowlist — product photos only. The 5 MB global MAX_CONTENT_LENGTH caps size.
_IMGSET_UPLOAD_EXT = {"png": "png", "jpg": "jpg", "jpeg": "jpg", "webp": "png"}


def _imageset_project_or_404(pid: int):
    """Return an image-set project owned by the signed-in user, or 404 (IDOR guard)."""
    project = imageset_store.get_project(get_db(), pid, g.user["id"])
    if project is None:
        abort(404)
    return project


def _parse_dimensions(form) -> dict:
    """Pull optional numeric dimensions + unit/weight from the intake form.

    Non-numeric dimension inputs are dropped (left None) rather than rejected, so a
    blank or stray value never blocks the whole submission — the measurement diagram
    simply omits what wasn't supplied.
    """
    dims: dict = {"unit": (form.get("unit") or "in").strip()[:12],
                  "weight": (form.get("weight") or "").strip()[:40]}
    for key in ("width", "height", "depth"):
        raw = (form.get(key) or "").strip()
        try:
            dims[key] = float(raw) if raw else None
        except ValueError:
            dims[key] = None
    return dims


def _validate_upload(file, label: str = "product photo") -> tuple[bytes | None, str | None, str | None]:
    """Validate an uploaded image (``label`` names it in errors). Returns (data, ext, error).

    Treats the upload as untrusted: the extension must be in the allowlist AND the
    bytes must actually decode as that image (Pillow verify), so a renamed
    non-image can't be stored. Size is already capped by MAX_CONTENT_LENGTH.
    """
    if file is None or not file.filename:
        return None, None, f"Please choose a {label} to upload."
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in _IMGSET_UPLOAD_EXT:
        return None, None, f"The {label} must be a PNG, JPG, or WEBP image."
    data = file.read()
    if not data:
        return None, None, "The uploaded file was empty."
    try:
        import io

        from PIL import Image
        Image.open(io.BytesIO(data)).verify()  # decode-check; never trust the extension alone
    except Exception:  # noqa: BLE001 - any decode failure means it isn't a valid image
        return None, None, "That file could not be read as an image."
    return data, _IMGSET_UPLOAD_EXT[ext], None


def _imageset_intake_context(**extra):
    """Shared render context for the intake form."""
    ctx = {"breadcrumb": _IMGSET_BREADCRUMB, "active_nav": _IMGSET_NAV,
           "project": None, "features": [], "fetching": False, "has_image": False,
           "has_logo": False, "pf": {},
           # The URL field autofills with this prefix so the user only types the item id.
           "url_prefix": pdp.WALMART_IP_PREFIX}
    ctx.update(extra)
    return ctx


def _detect_project_brand_color(project) -> str | None:
    """Auto-detect a draft's dominant brand color from its stored product photo.

    Best-effort: a decode/IO failure just yields None (the form shows a blank
    field and the default palette is used), so prefill never blocks on it.
    """
    rel = project["original_path"] if "original_path" in project.keys() else None
    path = imageset_storage.abs_path(rel) if rel else None
    if not path or not os.path.isfile(path):
        return None
    try:
        from app.imageset import compose as _compose
        from PIL import Image as _Image
        with _Image.open(path) as img:
            return _compose.detect_dominant_color(img)
    except Exception:  # noqa: BLE001 - detection is a convenience, never fatal
        logger.debug("Brand-color auto-detect failed for project=%s", project["id"], exc_info=True)
        return None


def _imageset_prefill_values(project) -> dict:
    """Flatten a draft's stored facts into plain form values for prefill."""
    dims = json.loads(project["dimensions_json"]) if project["dimensions_json"] else {}
    envs = json.loads(project["intended_environments"]) if project["intended_environments"] else []
    name = project["name"]
    # Brand colors: the user's saved values win; otherwise suggest the auto-detected
    # dominant color as the primary so the user can confirm or override it.
    try:
        colors = json.loads(project["brand_colors"]) if project["brand_colors"] else []
    except (json.JSONDecodeError, TypeError):
        colors = []
    colors = [c if isinstance(c, str) else "" for c in colors] + ["", "", ""]
    primary, secondary, accent = colors[0], colors[1], colors[2]
    if not primary:
        primary = _detect_project_brand_color(project) or ""
    return {
        "name": "" if name == "(fetching…)" else (name or ""),
        "brand": project["brand"] or "",
        "category": project["category"] or "",
        "description": project["description"] or "",
        "target_audience": project["target_audience"] or "",
        "directions": project["directions"] or "",
        "environments": ", ".join(envs),
        "unit": dims.get("unit") or "in",
        "weight": dims.get("weight") or "",
        "width": dims.get("width") if dims.get("width") is not None else "",
        "height": dims.get("height") if dims.get("height") is not None else "",
        "depth": dims.get("depth") if dims.get("depth") is not None else "",
        "primary_color": primary,
        "secondary_color": secondary,
        "accent_color": accent,
    }


def _parse_intake_fields(form) -> dict:
    """Pull the shared product facts (not the photo) from the intake form."""
    environments = [e.strip() for e in (form.get("environments") or "").split(",") if e.strip()]
    # Normalize brand colors to '#rrggbb', keeping slot order (primary, secondary,
    # accent) — a blank or invalid entry becomes '' so later slots keep their meaning.
    # A typo can't corrupt the palette; bad/blank slots fall back to the default.
    colors = []
    for raw in (form.get("primary_color"), form.get("secondary_color"), form.get("accent_color")):
        v = (raw or "").strip().lstrip("#")
        colors.append("#" + v.lower()
                      if len(v) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in v) else "")
    while colors and not colors[-1]:
        colors.pop()  # trim trailing empties so an all-blank form stores nothing
    return {
        "name": (form.get("name") or "").strip()[:200],
        "brand": (form.get("brand") or "").strip()[:120],
        "category": (form.get("category") or "").strip()[:120],
        "description": (form.get("description") or "").strip()[:2000],
        "target_audience": (form.get("target_audience") or "").strip()[:500],
        "directions": (form.get("directions") or "").strip()[:2000],
        "intended_environments": environments[:8],
        "brand_colors": colors or None,
        "dimensions": _parse_dimensions(form),
    }


def _intake_feature_items(form) -> list[dict]:
    """Zip the repeatable feature title/description inputs into dicts."""
    titles = form.getlist("feature_title")
    descs = form.getlist("feature_desc")
    descs += [""] * (len(titles) - len(descs))
    return [{"title": t, "description": d} for t, d in zip(titles, descs)]


# Draft statuses from which the main intake form may be (re)submitted.
_IMGSET_EDITABLE = {
    imageset_store.STATUS_DRAFT, imageset_store.STATUS_CUTOUT_PENDING, imageset_store.STATUS_FAILED,
}


@bp.route("/app/pdp-image-set", methods=["GET", "POST"])
@login_required
def imageset_intake():
    """Image-set intake: collect product facts + a photo, then cut out.

    GET renders the blank form (with the optional "prefill from a product URL"
    box). A POST creates a new project (manual entry) or updates a prefilled draft
    (when a ``project_id`` is present), stores/keeps the product photo, and runs
    background removal inline so the user lands on the cutout-approval gate.
    """
    if request.method != "POST":
        return render_template("app/pdp_image_set.html", **_imageset_intake_context())

    db, uid, form = get_db(), g.user["id"], request.form
    fields = _parse_intake_fields(form)

    # Editing a prefilled draft? Load it (IDOR + state guard) so a re-render on
    # error keeps the user on the same project.
    project = None
    raw_pid = (form.get("project_id") or "").strip()
    if raw_pid.isdigit():
        project = imageset_store.get_project(db, int(raw_pid), uid)
        if project is None or project["status"] not in _IMGSET_EDITABLE:
            abort(404)

    def _rerender(message, code):
        proj = imageset_store.get_project(db, project["id"], uid) if project else None
        feats = imageset_store.features_for_project(db, proj["id"]) if proj else []
        return render_template("app/pdp_image_set.html", **_imageset_intake_context(
            error=message, form=form, project=proj, features=feats,
            has_image=bool(proj and proj["original_path"]),
            has_logo=bool(proj and proj["logo_path"]))), code

    if not fields["name"] or not fields["brand"] or not fields["category"]:
        return _rerender("Product name, brand, and category are required.", 400)

    # Photo: a new upload always wins; otherwise a prefilled draft may reuse the
    # fetched product image. A brand-new project must supply a photo.
    upload = request.files.get("photo")
    has_upload = bool(upload and upload.filename)
    if has_upload:
        data, ext, err = _validate_upload(upload)
        if err:
            return _rerender(err, 400)
    elif not (project and project["original_path"]):
        return _rerender("Please upload a product photo (or prefill from a product URL).", 400)

    # Optional brand logo (composited top-left on Lifestyle images). Validate up
    # front so a bad file never creates a half-built project; stored below once the
    # pid is known. Absent = keep whatever the draft already had.
    logo_upload = request.files.get("logo")
    logo_data = logo_ext = None
    if logo_upload and logo_upload.filename:
        logo_data, logo_ext, logo_err = _validate_upload(logo_upload, label="brand logo")
        if logo_err:
            return _rerender(logo_err, 400)

    # Persist the facts (create new, or update the draft) + features.
    if project is None:
        pid = imageset_store.create_project(db, user_id=uid, **fields)
    else:
        pid = project["id"]
        imageset_store.update_project_fields(db, pid, **fields)
    imageset_store.set_features(db, pid, _intake_feature_items(form))

    # Store the optional brand logo. A storage failure is non-fatal: the set still
    # generates, just without the logo (logged for follow-up) rather than blocking.
    if logo_data:
        logo_rel = imageset_storage.save(pid, "logo", "logo", logo_data, logo_ext)
        if logo_rel:
            imageset_store.set_logo(db, pid, logo_rel)
            logger.info("Image-set brand logo stored project=%s user_id=%s", pid, uid)
        else:
            logger.warning("Could not store brand logo for project=%s user_id=%s", pid, uid)

    # Store the photo and advance to the cutout step.
    if has_upload:
        rel = imageset_storage.save(pid, "original", "product", data, ext)
        if not rel:
            imageset_store.set_status(db, pid, imageset_store.STATUS_FAILED,
                                      error="Could not store the uploaded photo.")
            return _rerender("We couldn't store the uploaded photo. Please try again.", 500)
        imageset_store.set_original_image(db, pid, rel)
    else:
        # Reuse the prefilled image: just advance the status (path already set).
        imageset_store.set_status(db, pid, imageset_store.STATUS_CUTOUT_PENDING)

    try:
        imageset_generate.run_cutout(db, imageset_store.get_project(db, pid, uid), uid)
    except (BackgroundRemovalError, imageset_generate.GenerationError) as e:
        logger.warning("Image-set cutout failed project=%s: %s", pid, e)
        imageset_store.set_status(db, pid, imageset_store.STATUS_FAILED, error=str(e))
        return _rerender(
            "We couldn't remove the background from that photo. Try a clearer product "
            "shot on a plain background.", 502)

    logger.info("Image-set project ready for cutout id=%s user_id=%s", pid, uid)
    return redirect(url_for("pages.imageset_cutout", pid=pid))


@bp.route("/app/pdp-image-set/fetch", methods=["POST"])
@login_required
def imageset_fetch():
    """Start a Walmart prefill: validate the URL, create a draft, enqueue the fetch."""
    url = pdp.validate_item_url((request.form.get("url") or "").strip())
    # Require an item number so a bare prefix (the autofill, untouched) is rejected.
    if not url or not pdp.item_number_from_url(url):
        return render_template("app/pdp_image_set.html", **_imageset_intake_context(
            error="Enter a valid Walmart product URL — add the item number to the end "
                  "(e.g. https://www.walmart.com/ip/10294528).")), 400
    db, uid = get_db(), g.user["id"]
    pid = imageset_store.create_draft_for_url(db, user_id=uid, url=url)
    logger.info("Image-set prefill queued project=%s user_id=%s", pid, uid)
    return redirect(url_for("pages.imageset_edit", pid=pid))


@bp.route("/app/pdp-image-set/<int:pid>/edit")
@login_required
def imageset_edit(pid):
    """Render the intake form for a draft — a fetching poller, else the prefilled form."""
    project = _imageset_project_or_404(pid)
    fetching = project["status"] in (imageset_store.STATUS_FETCHING,
                                     imageset_store.STATUS_FETCHING_ACTIVE)
    features = [] if fetching else imageset_store.features_for_project(get_db(), pid)
    pf = {} if fetching else _imageset_prefill_values(project)
    # A prefill that failed leaves a note on the draft; surface it (non-blocking).
    err = project["error"] if (not fetching and project["error"]) else None
    return render_template("app/pdp_image_set.html", **_imageset_intake_context(
        project=project, features=features, fetching=fetching, pf=pf,
        has_image=bool(project["original_path"]), has_logo=bool(project["logo_path"]), error=err))


@bp.route("/app/pdp-image-set/<int:pid>/fetch-status")
@login_required
def imageset_fetch_status(pid):
    """JSON prefill state for the edit-page poller."""
    project = _imageset_project_or_404(pid)
    fetching = project["status"] in (imageset_store.STATUS_FETCHING,
                                     imageset_store.STATUS_FETCHING_ACTIVE)
    return jsonify({"fetching": fetching})


@bp.route("/app/pdp-image-set/<int:pid>/original-image")
@login_required
def imageset_original_image(pid):
    """Serve a draft's product photo (fetched or uploaded) for the form preview."""
    project = _imageset_project_or_404(pid)
    return _serve_imageset_file(project["original_path"], download_name=f"product-{pid}.png",
                                as_attachment=False)


@bp.route("/app/pdp-image-set/<int:pid>/cutout")
@login_required
def imageset_cutout(pid):
    """Cutout approval gate: show the generated cutout; Approve starts generation."""
    project = _imageset_project_or_404(pid)
    return render_template(
        "app/pdp_image_set_cutout.html", breadcrumb=_IMGSET_BREADCRUMB,
        active_nav=_IMGSET_NAV, project=project,
        type_choices=imageset_generate.asset_type_choices(),
    )


@bp.route("/app/pdp-image-set/<int:pid>/approve", methods=["POST"])
@login_required
def imageset_approve(pid):
    """Approve the cutout and queue the set for background planning + generation.

    Planning (a slow Claude call) and generation both run on the worker, so this
    request returns immediately and the gallery polls — no web-request timeout.
    """
    project = _imageset_project_or_404(pid)
    if not project["cutout_path"]:
        abort(400, description="No cutout to approve yet.")
    db = get_db()
    # The user ticks which image types to generate on the cutout screen; store the
    # selection so generation builds (and the user pays for) only those. An empty
    # selection is stored as "all implemented" so the flow can't dead-end.
    selected = request.form.getlist("types")
    imageset_store.set_selected_types(db, pid, selected)
    imageset_store.approve_cutout(db, pid)
    imageset_store.queue_plan(db, pid)  # worker plans, creates assets, enqueues them
    logger.info("Image-set approved + queued for planning project=%s user_id=%s types=%s",
                pid, g.user["id"], selected or "all")
    return redirect(url_for("pages.imageset_gallery", pid=pid))


def _imageset_asset_view(asset, job) -> dict:
    """Shape one asset + its job into the gallery's display dict."""
    job_status = job["status"] if job else None
    # The asset is "working" while its job is queued/processing; "ready" once the
    # generation wrote its outputs; else failed/pending. A job that errored (e.g. an
    # orphan reclaimed after a worker restart) shows as failed even if the asset row
    # still says "generating", so the user gets a Retry instead of a stuck spinner.
    status = asset["status"]
    if status != "ready" and job_status == "error":
        status = "failed"
    elif status not in ("ready", "failed") and job_status in ("queued", "processing"):
        status = "generating"
    return {
        "id": asset["id"],
        "type": asset["asset_type"].replace("_", " ").title(),
        "title": asset["title"],
        "status": status,
        "ready": status == "ready" and bool(asset["final_path"]),
        "error": asset["error"] or (job["error"] if job else None),
    }


def _imageset_run_assets(db, pid: int, uid: int) -> list[dict]:
    """The assets actually part of this generation run, as display dicts.

    An asset is in the run once it has a job (enqueued) or has already finished
    (ready/failed). Planned assets for types whose generator isn't built yet exist
    in the DB (for a later phase) but aren't shown, so the gallery never displays a
    tile that can never finish.
    """
    jobs_map = imageset_jobs.jobs_for_project(db, pid, uid)
    views = []
    for asset in imageset_store.assets_for_project(db, pid, uid):
        if asset["id"] in jobs_map or asset["status"] in ("ready", "failed"):
            views.append(_imageset_asset_view(asset, jobs_map.get(asset["id"])))
    return views


# Project statuses that mean "the worker is still building the plan" (no assets yet).
_IMGSET_PLANNING = {imageset_store.STATUS_PLAN_QUEUED, imageset_store.STATUS_PLAN_ACTIVE}


@bp.route("/app/pdp-image-set/<int:pid>")
@login_required
def imageset_gallery(pid):
    """Generation gallery: per-asset status + downloads; polls while work runs."""
    project = _imageset_project_or_404(pid)
    db, uid = get_db(), g.user["id"]
    assets = _imageset_run_assets(db, pid, uid)
    planning = project["status"] in _IMGSET_PLANNING
    pending = planning or any(a["status"] in ("queued", "generating") for a in assets)
    return render_template(
        "app/pdp_image_set_gallery.html", breadcrumb=_IMGSET_BREADCRUMB,
        active_nav=_IMGSET_NAV, project=project, assets=assets, pending=pending,
        planning=planning,
        # "Download All" bundles every finished image.
        ready_count=sum(1 for a in assets if a["ready"]),
    )


@bp.route("/app/pdp-image-set/<int:pid>/asset/<int:aid>/regenerate", methods=["POST"])
@login_required
def imageset_regenerate_asset(pid, aid):
    """Re-run generation for one asset — retry a poor result without redoing the set."""
    _imageset_project_or_404(pid)
    db, uid = get_db(), g.user["id"]
    asset = imageset_store.get_asset(db, aid, uid)
    if asset is None or asset["project_id"] != pid:
        abort(404)
    imageset_store.reset_asset_for_regeneration(db, aid, uid)
    imageset_jobs.enqueue_asset_job(db, user_id=uid, project_id=pid, asset_id=aid)
    imageset_store.set_status(db, pid, imageset_store.STATUS_GENERATING)
    logger.info("Image-set regenerate asset=%s project=%s user_id=%s", aid, pid, uid)
    return redirect(url_for("pages.imageset_gallery", pid=pid))


@bp.route("/app/pdp-image-set/<int:pid>/status")
@login_required
def imageset_status(pid):
    """JSON asset statuses for the gallery poller."""
    project = _imageset_project_or_404(pid)
    db, uid = get_db(), g.user["id"]
    assets = _imageset_run_assets(db, pid, uid)
    planning = project["status"] in _IMGSET_PLANNING
    return jsonify({
        "pending": planning or any(a["status"] in ("queued", "generating") for a in assets),
        "planning": planning,
        "assets": assets,
    })


def _serve_imageset_file(rel: str | None, *, download_name: str, as_attachment: bool):
    """Serve a stored image-set artifact by its relative path (containment-checked)."""
    from flask import send_file

    path = imageset_storage.abs_path(rel)
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/png", as_attachment=as_attachment,
                     download_name=download_name, max_age=0)


@bp.route("/app/pdp-image-set/<int:pid>/cutout-image")
@login_required
def imageset_cutout_image(pid):
    """Serve a project's cutout PNG (same-origin, so CSP img-src 'self')."""
    project = _imageset_project_or_404(pid)
    return _serve_imageset_file(project["cutout_path"], download_name=f"cutout-{pid}.png",
                                as_attachment=False)


@bp.route("/app/pdp-image-set/<int:pid>/asset/<int:aid>/image")
@login_required
def imageset_asset_image(pid, aid):
    """Serve an asset's final (or its thumbnail via ?thumb=1) inline."""
    _imageset_project_or_404(pid)  # IDOR: must own the project
    asset = imageset_store.get_asset(get_db(), aid, g.user["id"])
    if asset is None or asset["project_id"] != pid:
        abort(404)
    rel = asset["thumb_path"] if request.args.get("thumb") else asset["final_path"]
    return _serve_imageset_file(rel, download_name=f"asset-{aid}.png", as_attachment=False)


def _imageset_product_id(project) -> str:
    """The product identifier used in downloaded filenames and folders.

    Prefers the Walmart item number (parsed from the product's source URL) so the
    download ties back to the live listing; falls back to the internal project id
    (``proj<pid>``) for manually-uploaded products that have no Walmart URL.
    """
    url = project["source_url"] if "source_url" in project.keys() else None
    item = pdp.item_number_from_url(url) if url else None
    return item or f"proj{project['id']}"


def _imageset_asset_filename(product_id: str, asset) -> str:
    """Stem a single asset's download as ``product-<id>-<type>-<variation>``."""
    atype = (asset["asset_type"] or "asset").lower()
    return f"product-{product_id}-{atype}-{asset['variation_number']}.png"


@bp.route("/app/pdp-image-set/<int:pid>/asset/<int:aid>/download")
@login_required
def imageset_asset_download(pid, aid):
    """Serve an asset's final as a file download."""
    project = _imageset_project_or_404(pid)
    asset = imageset_store.get_asset(get_db(), aid, g.user["id"])
    if asset is None or asset["project_id"] != pid:
        abort(404)
    name = _imageset_asset_filename(_imageset_product_id(project), asset)
    return _serve_imageset_file(asset["final_path"], download_name=name, as_attachment=True)


@bp.route("/app/pdp-image-set/<int:pid>/download.zip")
@login_required
def imageset_download_zip(pid):
    """Bundle every ready asset final into one ZIP (404 if none ready yet)."""
    import io
    import zipfile

    from flask import send_file

    project = _imageset_project_or_404(pid)
    db, uid = get_db(), g.user["id"]
    product_id = _imageset_product_id(project)
    folder = f"product-{product_id}"  # ZIP root folder, named for the product
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in imageset_store.assets_for_project(db, pid, uid):
            path = imageset_storage.abs_path(asset["final_path"])
            if not path or not os.path.isfile(path):
                continue
            arc = f"{folder}/{_imageset_asset_filename(product_id, asset)}"
            zf.write(path, arcname=arc)
            written += 1
    if not written:
        abort(404, description="No finished images to download yet.")
    buf.seek(0)
    logger.info("Image-set ZIP: %d asset(s) project=%s product_id=%s user_id=%s",
                written, pid, product_id, uid)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"product-{product_id}-image-set.zip", max_age=0)


# ── Import Product Images (Creative Content Studio) ──────────────────────────

_IMPORT_NAV = "import-images"
_IMPORT_BREADCRUMB = "Creative Content Studio · Acquire Product Images From An Existing Site"


def _collect_import_urls(text: str, csv_file) -> tuple[list[str], list[str]]:
    """Merge, validate, and de-duplicate import URLs from the textarea + CSV.

    Permissive (unlike the Walmart-only scoring intake): any ``http(s)`` URL is
    accepted — a product page or a direct image URL; the worker classifies each.
    Returns ``(accepted, rejected)``; ``rejected`` holds non-empty entries that
    weren't valid URLs, for user feedback. Order-preserving, de-duplicated, capped
    at :data:`pdp.MAX_ITEMS`.
    """
    from urllib.parse import urlparse

    accepted: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        url = (candidate or "").strip()
        if not url:
            return
        if len(url) > pdp.MAX_URL_LEN:
            rejected.append(url[:80] + "…")
            return
        try:
            parsed = urlparse(url)
        except ValueError:
            rejected.append(url)
            return
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            rejected.append(url)
            return
        if url in seen:
            return
        seen.add(url)
        if len(accepted) < pdp.MAX_ITEMS:
            accepted.append(url)

    for line in (text or "").splitlines():
        _add(line)
    if csv_file is not None and getattr(csv_file, "filename", ""):
        for value in pdp.urls_from_csv(csv_file):
            _add(value)
    return accepted, rejected


@bp.route("/app/import-images", methods=["GET", "POST"])
@login_required
def import_images_intake():
    """Intake for Import Product Images: paste product/image URLs or upload a CSV.

    A POST validates the URLs, creates a run with one queued item per URL (the
    worker classifies + acquires each), and redirects to the gallery, which polls
    for progress. The web request never downloads or scrapes.
    """
    if request.method == "POST":
        text = request.form.get("urls", "")
        accepted, rejected = _collect_import_urls(text, request.files.get("csv"))
        if not accepted:
            return (
                render_template(
                    "app/import_images.html", breadcrumb=_IMPORT_BREADCRUMB,
                    active_nav=_IMPORT_NAV, max_items=pdp.MAX_ITEMS,
                    error="No valid product or image URLs were provided.",
                    urls_value=text, rejected=rejected,
                ),
                400,
            )
        db, uid = get_db(), g.user["id"]
        run_id = import_store.create_run(db, uid)
        for url in accepted:
            import_store.add_item(
                db, run_id=run_id, user_id=uid, source_url=url,
                source_type=import_sources.classify(url))
        logger.info("Import run=%s created with %d item(s), %d rejected, user_id=%s",
                    run_id, len(accepted), len(rejected), uid)
        return redirect(url_for("pages.import_images_run", run_id=run_id))

    logger.info("Serving Import Product Images intake")
    return render_template(
        "app/import_images.html", breadcrumb=_IMPORT_BREADCRUMB,
        active_nav=_IMPORT_NAV, max_items=pdp.MAX_ITEMS,
    )


_IMPORT_MIME = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp",
                "gif": "image/gif"}
# The gallery hotlinks product images from external CDNs, so it needs a looser
# img-src than the app-wide default (which is 'self' only). Scoped to this one page;
# images only (scripts/styles/fonts stay 'self'), so the relaxation is low-risk.
_IMPORT_GALLERY_CSP = (
    "default-src 'self'; img-src 'self' data: https:; "
    "style-src 'self' 'unsafe-inline'; font-src 'self'; script-src 'self'"
)


def _import_product_id(item) -> str | None:
    """A product identifier for download filenames: Amazon ASIN or Walmart item #."""
    url = item["source_url"] or ""
    stype = item["source_type"]
    if stype == "amazon_pdp":
        m = re.search(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})", url, re.IGNORECASE)
        return m.group(1).upper() if m else None
    if stype == "walmart":
        return pdp.item_number_from_url(url)
    return None


def _import_slug(text: str | None) -> str:
    """A filename-safe lowercase slug of a product title (bounded length)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:40].strip("-")


def _import_download_stem(item, index: int) -> str:
    """Per-image download stem: ``<title-slug>-<product-id>-<n>`` (empties dropped)."""
    parts = [p for p in (_import_slug(item["label"] if item else None),
                         _import_product_id(item) if item else None) if p]
    base = "-".join(parts) if parts else "image"
    return f"{base}-{index}"


def _import_zip_folder(item) -> str:
    """ZIP subfolder for a product: title slug, else product id, else ``item-<id>``."""
    return (_import_slug(item["label"]) or _import_product_id(item)
            or f"item-{item['id']}")


def _maybe_upscale(data: bytes, ext: str, width, height, full_url: str) -> tuple[bytes, str]:
    """Upscale an image to the target longest edge when it's below spec.

    Checked at download time (the first point we know the real size): if the native
    image's longest edge is under ``image_enhance.target_px()`` (the 2000px zoom
    spec) and Claid is configured, return the super-resolved bytes; otherwise the
    native bytes unchanged. Best-effort — a provider failure falls back to native.
    """
    target = image_enhance.target_px()
    if width and height and max(width, height) < target and image_enhance.is_configured():
        try:
            up = image_enhance.enhance(full_url, operation="upscale")
            logger.info("Import: upscaled %s (%dx%d < %d)", full_url, width, height, target)
            return up, image_enhance.output_ext()
        except image_enhance.EnhanceError as e:
            logger.warning("Import: upscale skipped for %s (%s)", full_url, e)
    return data, ext


def _import_gallery_view(db, run_id: int, uid: int) -> list[dict]:
    """Shape a run's items (each with its resolved images) for the gallery/poller.

    No bytes are stored — the gallery hotlinks ``thumb_url`` for display and opens
    ``full_url`` (both external CDN URLs); only Download routes back through us.
    """
    assets_by_item: dict[int, list[dict]] = {}
    for a in import_store.assets_for_run(db, run_id, uid):
        assets_by_item.setdefault(a["item_id"], []).append({
            "id": a["id"],
            "status": a["status"],
            "ready": a["status"] == import_store.STATUS_READY,
            "error": a["error"],
            "width": a["width"],
            "height": a["height"],
            # Hotlinked thumbnail (falls back to the full URL if no thumb was derived).
            "thumb_url": a["thumb_url"] or a["source_image_url"],
            "full_url": a["source_image_url"],
            "download_url": url_for("pages.import_asset_download", run_id=run_id, aid=a["id"]),
        })
    items = []
    for it in import_store.items_for_run(db, run_id, uid):
        items.append({
            "id": it["id"],
            "source_url": it["source_url"],
            "source_type": it["source_type"],
            "label": it["label"],
            "status": it["status"],
            "error": it["error"],
            "assets": assets_by_item.get(it["id"], []),
        })
    return items


def _import_run_or_404(run_id: int):
    run = import_store.get_run(get_db(), run_id, g.user["id"])
    if run is None:
        abort(404)
    return run


@bp.route("/app/import-images/<int:run_id>")
@login_required
def import_images_run(run_id):
    """Gallery for one import run: per-item status + hotlinked images; polls while busy."""
    run = _import_run_or_404(run_id)
    db, uid = get_db(), g.user["id"]
    items = _import_gallery_view(db, run_id, uid)
    ready_count = sum(1 for it in items for a in it["assets"] if a["ready"])
    pending = run["status"] == import_store.STATUS_PROCESSING
    resp = make_response(render_template(
        "app/import_images_gallery.html", breadcrumb=_IMPORT_BREADCRUMB,
        active_nav=_IMPORT_NAV, run=run, items=items, pending=pending,
        ready_count=ready_count,
    ))
    # Per-page CSP so the hotlinked CDN thumbnails load (see _IMPORT_GALLERY_CSP).
    resp.headers["Content-Security-Policy"] = _IMPORT_GALLERY_CSP
    return resp


@bp.route("/app/import-images/<int:run_id>/status")
@login_required
def import_images_status(run_id):
    """JSON run/item status for the gallery poller."""
    run = _import_run_or_404(run_id)
    db, uid = get_db(), g.user["id"]
    return jsonify({
        "pending": run["status"] == import_store.STATUS_PROCESSING,
        "status": run["status"],
        "items": _import_gallery_view(db, run_id, uid),
    })


def _send_image_bytes(data: bytes, *, ext: str, download_name: str):
    """Serve in-memory image bytes as a download (no file on disk)."""
    import io

    from flask import send_file

    mime = _IMPORT_MIME.get(ext, "application/octet-stream")
    return send_file(io.BytesIO(data), mimetype=mime, as_attachment=True,
                     download_name=download_name, max_age=0)


@bp.route("/app/import-images/<int:run_id>/asset/<int:aid>/download")
@login_required
def import_asset_download(run_id, aid):
    """Fetch one image's full-resolution file on demand, upscale if below spec, download."""
    _import_run_or_404(run_id)
    db, uid = get_db(), g.user["id"]
    asset = import_store.get_asset(db, aid, uid)
    if asset is None or asset["run_id"] != run_id:
        abort(404)
    try:
        data, ext, w, h = import_sources.download_image(asset["source_image_url"])
    except import_sources.SourceError as e:
        logger.warning("Import download failed run=%s asset=%s: %s", run_id, aid, e)
        abort(502, description=str(e))
    data, ext = _maybe_upscale(data, ext, w, h, asset["source_image_url"])
    item = import_store.get_item(db, asset["item_id"], uid)
    index = import_store.asset_index_in_item(db, asset["item_id"], aid)
    name = f"{_import_download_stem(item, index)}.{ext}"
    return _send_image_bytes(data, ext=ext, download_name=name)


@bp.route("/app/import-images/<int:run_id>/download.zip")
@login_required
def import_download_zip(run_id):
    """Fetch the selected images' full-resolution files on demand and bundle a ZIP.

    The gallery submits a GET form, so the checked images arrive as ``ids`` query
    params; a hidden ``scope=selected`` marks that a selection was made (so zero
    checked boxes reads as "nothing selected", not "download everything"). A bare
    hit with no ``scope`` (a bookmarked URL) bundles every resolved image. Each file
    is fetched now (not stored at acquire time); an image that fails to download is
    skipped rather than failing the whole ZIP.
    """
    import io
    import zipfile

    from flask import send_file

    _import_run_or_404(run_id)
    db, uid = get_db(), g.user["id"]
    assets = import_store.ready_assets_for_run(db, run_id, uid)

    if request.args.get("scope") == "selected":
        wanted = {i for i in request.args.getlist("ids", type=int)}
        assets = [a for a in assets if a["id"] in wanted]
        if not assets:
            abort(404, description="No images selected.")

    items = {it["id"]: it for it in import_store.items_for_run(db, run_id, uid)}
    counters: dict[int, int] = {}
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for asset in assets:  # ordered by item then id, so per-product numbering is stable
            try:
                data, ext, w, h = import_sources.download_image(asset["source_image_url"])
            except import_sources.SourceError as e:
                logger.warning("Import ZIP: skipped asset=%s (%s)", asset["id"], e)
                continue
            data, ext = _maybe_upscale(data, ext, w, h, asset["source_image_url"])
            item = items.get(asset["item_id"])
            counters[asset["item_id"]] = counters.get(asset["item_id"], 0) + 1
            # Group each product's images in their own folder, numbered per product.
            arc = f"{_import_zip_folder(item)}/{_import_download_stem(item, counters[asset['item_id']])}.{ext}"
            zf.writestr(arc, data)
            written += 1
    if not written:
        abort(404, description="Couldn't download any of the selected images.")
    buf.seek(0)
    # Name the ZIP for the product when it's all one product, else a run-scoped name.
    bundled_items = {a["item_id"] for a in assets}
    if len(bundled_items) == 1:
        zip_name = f"{_import_zip_folder(items.get(next(iter(bundled_items))))}-images.zip"
    else:
        zip_name = f"acquired-images-{len(bundled_items)}-products.zip"
    logger.info("Import ZIP: %d image(s) run=%s user_id=%s", written, run_id, uid)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=zip_name, max_age=0)


# ── Seasonal Content Campaigns ───────────────────────────────────────────────
# Create seasonal creative in bulk: a campaign (season/theme + planning dates) →
# products added by URL (acquire gallery) → the user picks images → each chosen
# image is composited onto a fresh AI seasonal backdrop. Create-and-download only.
# Reuses the import acquire resolver + the image-set compositor/providers.

# The Seasonal Campaigns workspace: one rail slug for both the Create New view
# (home) and the History tab, so the rail stays highlighted across both.
_SEASONAL_NAV = "seasonal"
_SEASONAL_BREADCRUMB = "Creative Studio · Seasonal Campaigns"
_SEASONAL_HISTORY_BREADCRUMB = "Seasonal Campaigns · History"
_SEASONAL_NAME_MAX = 120
_SEASONAL_CUSTOM_MAX = 600
# The detail page hotlinks external CDN thumbnails (the acquired gallery) AND serves
# our own generated finals same-origin, so it needs the same relaxed img-src as the
# Import gallery (images only; scripts/styles/fonts stay 'self'). See _IMPORT_GALLERY_CSP.
_SEASONAL_GALLERY_CSP = _IMPORT_GALLERY_CSP


def _parse_campaign_date(value: str | None) -> tuple[str | None, bool]:
    """Parse a form date into an ISO ``YYYY-MM-DD`` string. Returns ``(value, ok)``.

    Blank is valid and yields ``(None, True)`` — dates are optional planning
    metadata. A malformed date yields ``(None, False)`` so the caller can reject the
    form. ``date.fromisoformat`` accepts exactly the ``<input type=date>`` format.
    """
    from datetime import date

    text = (value or "").strip()
    if not text:
        return None, True
    try:
        return date.fromisoformat(text).isoformat(), True
    except ValueError:
        return None, False


def _seasonal_campaign_or_404(cid: int):
    """Fetch a campaign owned by the current user, or 404 (IDOR guard)."""
    campaign = seasonal_store.get_campaign(get_db(), cid, g.user["id"])
    if campaign is None:
        abort(404)
    return campaign


def _seasonal_detail_view(db, cid: int, uid: int) -> list[dict]:
    """Shape a campaign's products (each with its acquired/generated images) for the UI.

    One dict per product in submission order, each carrying its image list. An image
    carries both the hotlinked source thumbnail (for the acquired gallery) and, once
    generated, the same-origin URLs for its final + thumbnail.
    """
    images_by_product: dict[int, list[dict]] = {}
    for im in seasonal_store.images_for_campaign(db, cid, uid):
        images_by_product.setdefault(im["product_id"], []).append({
            "id": im["id"],
            "gen_status": im["gen_status"],
            "ready": im["gen_status"] == seasonal_store.GEN_READY,
            "failed": im["gen_status"] == seasonal_store.GEN_FAILED,
            "pending": im["gen_status"] in (seasonal_store.GEN_QUEUED,
                                            seasonal_store.GEN_GENERATING),
            "kept": bool(im["kept"]),
            "error": im["error"],
            # Main source image (hotlinked external CDN thumbnail + full URL).
            "source_thumb_url": im["thumb_url"] or im["full_url"],
            # Generated outputs, served same-origin (only meaningful once ready).
            "final_url": url_for("pages.seasonal_image_file", cid=cid, iid=im["id"]),
            "thumb_url": url_for("pages.seasonal_image_file", cid=cid, iid=im["id"], thumb=1),
            "download_url": url_for("pages.seasonal_image_download", cid=cid, iid=im["id"]),
        })
    products = []
    for p in seasonal_store.products_for_campaign(db, cid, uid):
        products.append({
            "id": p["id"],
            "source_url": p["source_url"],
            "source_type": p["source_type"],
            "label": p["label"],
            "status": p["status"],
            "error": p["error"],
            "images": images_by_product.get(p["id"], []),
        })
    return products


@bp.route("/app/seasonal", methods=["GET", "POST"])
@login_required
def seasonal_home():
    """Seasonal campaigns home: list existing campaigns + the new-campaign form.

    A POST validates the name/theme/dates, creates a draft campaign, and redirects
    to its detail page (where products are added). The campaign create never does
    any external work, so it returns immediately.
    """
    db, uid = get_db(), g.user["id"]
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()[:_SEASONAL_NAME_MAX]
        theme = (request.form.get("theme") or "").strip()
        custom_prompt = (request.form.get("custom_prompt") or "").strip()[:_SEASONAL_CUSTOM_MAX]
        launch, launch_ok = _parse_campaign_date(request.form.get("launch_date"))
        expire, expire_ok = _parse_campaign_date(request.form.get("expiration_date"))
        remind, remind_ok = _parse_campaign_date(request.form.get("reminder_date"))

        error = None
        if not name:
            error = "Give the campaign a name."
        elif not seasonal_themes.is_valid_theme(theme):
            error = "Choose a season/theme for the campaign."
        elif not (launch_ok and expire_ok and remind_ok):
            error = "One of the dates isn't a valid calendar date."
        if error is not None:
            flash(error, "error")
            return (render_template(
                "app/seasonal_home.html", breadcrumb=_SEASONAL_BREADCRUMB,
                active_nav=_SEASONAL_NAV, themes=seasonal_themes.theme_choices(),
                form=request.form,
                theme_defaults=seasonal_themes.theme_defaults(),
                default_theme=seasonal_themes.DEFAULT_THEME,
            ), 400)

        cid = seasonal_store.create_campaign(
            db, user_id=uid, name=name, theme=theme, custom_prompt=custom_prompt,
            launch_date=launch, expiration_date=expire, reminder_date=remind)
        logger.info("Seasonal campaign created id=%s theme=%s user_id=%s", cid, theme, uid)
        return redirect(url_for("pages.seasonal_campaign", cid=cid))

    logger.info("Serving Seasonal Campaigns home user_id=%s", uid)
    return render_template(
        "app/seasonal_home.html", breadcrumb=_SEASONAL_BREADCRUMB,
        active_nav=_SEASONAL_NAV, themes=seasonal_themes.theme_choices(),
        form={},
        theme_defaults=seasonal_themes.theme_defaults(),
        default_theme=seasonal_themes.DEFAULT_THEME,
    )


@bp.route("/app/seasonal/history")
@login_required
def seasonal_history():
    """All of the user's campaigns — the single campaign list for the workspace.

    Lists every campaign (drafts included), not just those that produced a finished
    image: this is the only place campaigns are listed now that the Create-New tab's
    inline list was removed, so a draft must remain reachable here.
    """
    db, uid = get_db(), g.user["id"]
    campaigns = seasonal_store.list_campaigns(db, uid)
    logger.info("Serving Seasonal Campaign history user_id=%s (%d)", uid, len(campaigns))
    return render_template(
        "app/seasonal_history.html",
        breadcrumb=_SEASONAL_HISTORY_BREADCRUMB,
        active_nav=_SEASONAL_NAV, campaigns=campaigns,
        theme_label=seasonal_themes.theme_label,
    )


@bp.route("/app/seasonal/<int:cid>")
@login_required
def seasonal_campaign(cid):
    """Campaign detail: add products, pick acquired images, generate, review, download."""
    campaign = _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    products = _seasonal_detail_view(db, cid, uid)
    pending = seasonal_store.has_pending_work(db, cid)
    ready_count = sum(1 for p in products for im in p["images"] if im["ready"] and im["kept"])
    resp = make_response(render_template(
        "app/seasonal_campaign.html", breadcrumb=_SEASONAL_BREADCRUMB,
        active_nav=_SEASONAL_NAV, campaign=campaign, products=products, pending=pending,
        ready_count=ready_count, theme_label=seasonal_themes.theme_label(campaign["theme"]),
        max_items=pdp.MAX_ITEMS,
    ))
    resp.headers["Content-Security-Policy"] = _SEASONAL_GALLERY_CSP
    return resp


@bp.route("/app/seasonal/<int:cid>/products", methods=["POST"])
@login_required
def seasonal_add_products(cid):
    """Add product URLs (textarea and/or CSV) to a campaign; the worker acquires each."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    accepted, rejected = _collect_import_urls(request.form.get("urls", ""),
                                              request.files.get("csv"))
    if not accepted:
        flash("No valid product or image URLs were provided.", "error")
        return redirect(url_for("pages.seasonal_campaign", cid=cid))
    for url in accepted:
        seasonal_store.add_product(db, campaign_id=cid, user_id=uid, source_url=url,
                                   source_type=import_sources.classify(url))
    msg = (f"Added {len(accepted)} product(s). Acquiring each main image and "
           "applying the seasonal treatment…")
    if rejected:
        msg += f" ({len(rejected)} entry(ies) weren't valid URLs and were skipped.)"
    flash(msg, "ok")
    logger.info("Seasonal campaign=%s added %d product(s), %d rejected, user_id=%s",
                cid, len(accepted), len(rejected), uid)
    return redirect(url_for("pages.seasonal_campaign", cid=cid))


@bp.route("/app/seasonal/<int:cid>/product/<int:pid>/retry", methods=["POST"])
@login_required
def seasonal_retry_product(cid, pid):
    """Retry a failed product's acquire (e.g. after a Walmart bot-detection block).

    Re-queues the product in place (no new row) so the worker tries again; only a
    product that actually failed is re-queued.
    """
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    product = seasonal_store.get_product(db, pid, uid)
    if product is None or product["campaign_id"] != cid:
        abort(404)
    if seasonal_store.requeue_product(db, pid, uid):
        flash("Retrying — acquiring the main image again…", "ok")
    else:
        flash("That product isn’t in a failed state.", "ok")
    logger.info("Seasonal retry product=%s campaign=%s user_id=%s", pid, cid, uid)
    return redirect(url_for("pages.seasonal_campaign", cid=cid))


@bp.route("/app/seasonal/<int:cid>/image/<int:iid>/regenerate", methods=["POST"])
@login_required
def seasonal_regenerate_image(cid, iid):
    """Re-queue one image for a fresh seasonal backdrop (idempotent re-enqueue)."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    image = seasonal_store.get_image(db, iid, uid)
    if image is None or image["campaign_id"] != cid:
        abort(404)
    seasonal_jobs.enqueue_image_job(db, user_id=uid, campaign_id=cid, image_id=iid)
    flash("Regenerating that image…", "ok")
    logger.info("Seasonal regenerate campaign=%s image=%s user_id=%s", cid, iid, uid)
    return redirect(url_for("pages.seasonal_campaign", cid=cid))


@bp.route("/app/seasonal/<int:cid>/image/<int:iid>/discard", methods=["POST"])
@login_required
def seasonal_discard_image(cid, iid):
    """Toggle whether a generated image is kept in the set."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    image = seasonal_store.get_image(db, iid, uid)
    if image is None or image["campaign_id"] != cid:
        abort(404)
    seasonal_store.set_image_kept(db, iid, uid, not image["kept"])
    return redirect(url_for("pages.seasonal_campaign", cid=cid))


@bp.route("/app/seasonal/<int:cid>/edit", methods=["POST"])
@login_required
def seasonal_edit(cid):
    """Edit a campaign's name and planning dates (dates can be set or cleared)."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    name = (request.form.get("name") or "").strip()[:_SEASONAL_NAME_MAX]
    launch, launch_ok = _parse_campaign_date(request.form.get("launch_date"))
    expire, expire_ok = _parse_campaign_date(request.form.get("expiration_date"))
    remind, remind_ok = _parse_campaign_date(request.form.get("reminder_date"))
    if not name:
        flash("The campaign needs a name.", "error")
    elif not (launch_ok and expire_ok and remind_ok):
        flash("One of the dates isn't a valid calendar date.", "error")
    else:
        seasonal_store.update_campaign(db, cid, uid, name=name, launch_date=launch,
                                       expiration_date=expire, reminder_date=remind,
                                       clear_dates=True)
        flash("Campaign updated.", "ok")
    return redirect(url_for("pages.seasonal_campaign", cid=cid))


@bp.route("/app/seasonal/<int:cid>/delete", methods=["POST"])
@login_required
def seasonal_delete(cid):
    """Delete a campaign and its products/images (cascade). IDOR-scoped."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    seasonal_store.delete_campaign(db, cid, uid)
    flash("Campaign deleted.", "ok")
    return redirect(url_for("pages.seasonal_home"))


@bp.route("/app/seasonal/<int:cid>/status")
@login_required
def seasonal_status(cid):
    """JSON campaign/product/image status for the detail-page poller."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    return jsonify({
        "pending": seasonal_store.has_pending_work(db, cid),
        "products": _seasonal_detail_view(db, cid, uid),
    })


@bp.route("/app/seasonal/<int:cid>/image/<int:iid>/image")
@login_required
def seasonal_image_file(cid, iid):
    """Serve a generated image's final (or its thumbnail via ?thumb=1) inline.

    Same-origin PNG, so it loads under the default CSP img-src 'self'. IDOR-guarded:
    the image must belong to a campaign the user owns.
    """
    _seasonal_campaign_or_404(cid)
    image = seasonal_store.get_image(get_db(), iid, g.user["id"])
    if image is None or image["campaign_id"] != cid:
        abort(404)
    rel = image["thumb_path"] if request.args.get("thumb") else image["final_path"]
    path = seasonal_storage.abs_path(rel)
    if not path or not os.path.isfile(path):
        abort(404)
    from flask import send_file
    return send_file(path, mimetype="image/png", as_attachment=False,
                     download_name=f"seasonal-{iid}.png", max_age=0)


def _seasonal_download_stem(product, index: int) -> str:
    """Per-image download stem: ``<title-slug>-<product-id>-<n>`` (reuses import helpers)."""
    parts = [p for p in (_import_slug(product["label"] if product else None),
                         _import_product_id(product) if product else None) if p]
    base = "-".join(parts) if parts else "seasonal"
    return f"{base}-{index}"


@bp.route("/app/seasonal/<int:cid>/image/<int:iid>/download")
@login_required
def seasonal_image_download(cid, iid):
    """Download one generated seasonal image (the final PNG)."""
    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    image = seasonal_store.get_image(db, iid, uid)
    if image is None or image["campaign_id"] != cid or image["gen_status"] != seasonal_store.GEN_READY:
        abort(404)
    path = seasonal_storage.abs_path(image["final_path"])
    if not path or not os.path.isfile(path):
        abort(404)
    product = seasonal_store.get_product(db, image["product_id"], uid)
    index = seasonal_store.image_index_in_product(db, image["product_id"], iid)
    from flask import send_file
    return send_file(path, mimetype="image/png", as_attachment=True,
                     download_name=f"{_seasonal_download_stem(product, index)}-seasonal.png",
                     max_age=0)


@bp.route("/app/seasonal/<int:cid>/download.zip")
@login_required
def seasonal_download_zip(cid):
    """Bundle the campaign's kept, ready seasonal images into a ZIP (per-product folders).

    A GET form submits the checked image ids as ``ids`` with ``scope=selected``; an
    empty selection then reads as "nothing selected" (not "everything"). A bare hit
    with no ``scope`` bundles every kept, ready image. Images that fail to read are
    skipped rather than failing the whole ZIP.
    """
    import io
    import zipfile

    from flask import send_file

    _seasonal_campaign_or_404(cid)
    db, uid = get_db(), g.user["id"]
    images = seasonal_store.ready_images_for_campaign(db, cid, uid)
    if request.args.get("scope") == "selected":
        wanted = {i for i in request.args.getlist("ids", type=int)}
        images = [im for im in images if im["id"] in wanted]
        if not images:
            abort(404, description="No images selected.")

    products = {p["id"]: p for p in seasonal_store.products_for_campaign(db, cid, uid)}
    counters: dict[int, int] = {}
    buf = io.BytesIO()
    written = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for im in images:  # ordered by product then id → stable per-product numbering
            data = seasonal_storage.load(im["final_path"])
            if not data:
                logger.warning("Seasonal ZIP: skipped image=%s (missing file)", im["id"])
                continue
            product = products.get(im["product_id"])
            counters[im["product_id"]] = counters.get(im["product_id"], 0) + 1
            folder = _import_zip_folder(product) if product else f"product-{im['product_id']}"
            arc = f"{folder}/{_seasonal_download_stem(product, counters[im['product_id']])}-seasonal.png"
            zf.writestr(arc, data)
            written += 1
    if not written:
        abort(404, description="Couldn't bundle any of the selected images.")
    buf.seek(0)
    logger.info("Seasonal ZIP: %d image(s) campaign=%s user_id=%s", written, cid, uid)
    return send_file(buf, mimetype="application/zip", as_attachment=True,
                     download_name=f"seasonal-campaign-{cid}.zip", max_age=0)


# ── Account: change password ─────────────────────────────────────────────────

@bp.route("/account/password", methods=["GET", "POST"])
@login_required
def change_password():
    """Let a signed-in user set a new password.

    Also the destination of the forced-change gate for admin-provisioned accounts
    (:func:`security.require_password_change`); ``forced`` tailors the copy and
    hides the "cancel" path. Saving clears the ``must_change_password`` flag (in
    :func:`users.set_password`), which releases the gate on the next request.
    """
    forced = bool(g.user["must_change_password"])
    if request.method == "POST":
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        error = validate_password(password)
        if error is None and password != confirm:
            error = "The two passwords don't match."
        if error is None:
            users.set_password(g.user["id"], password)
            logger.info("User user_id=%s set a new password", g.user["id"])
            flash("Password updated.", "ok")
            return redirect(url_for("pages.dashboard"))
        flash(error, "error")

    return render_template(
        "app/change_password.html",
        breadcrumb="Change Password",
        active_nav=None,
        forced=forced,
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


@bp.route("/admin/messages/new")
@admin_required
def admin_message_new():
    """Compose screen: pick one or more recipients and write a single message.

    Sending fans the message out to one private thread per recipient (see
    ``messages.broadcast_from_admin``).
    """
    logger.info("Admin compose view: admin_user_id=%s", g.user["id"])
    return render_template(
        "app/admin_message_new.html",
        breadcrumb="Admin · Messages",
        active_nav="admin-messages",
        recipients=users.list_users(),
    )


@bp.route("/admin/messages/new", methods=["POST"])
@admin_required
def admin_message_send():
    """Fan a composed message out to the selected recipients."""
    db = get_db()
    # Checkbox values arrive as strings; keep only well-formed ids and let the
    # messages layer reject an empty set or an id that no longer exists.
    recipient_ids: list[int] = []
    for raw in request.form.getlist("recipient_ids"):
        try:
            recipient_ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    try:
        thread_ids = messages.broadcast_from_admin(
            db, recipient_ids, g.user["id"],
            request.form.get("subject", ""), request.form.get("body", ""),
        )
    except messages.MessageError as e:
        flash(str(e), "error")
        return redirect(url_for("pages.admin_message_new"))

    count = len(thread_ids)
    logger.info("Admin composed message: admin_user_id=%s recipients=%d", g.user["id"], count)
    if count == 1:
        # Single recipient: drop straight into the conversation we just started.
        flash("Message sent.", "ok")
        return redirect(url_for("pages.admin_message_thread", thread_id=thread_ids[0]))
    flash(f"Message sent to {count} recipients.", "ok")
    return redirect(url_for("pages.admin_messages"))


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
        "brand": row["brand"],
        "status": row["status"],
        "overall": row["overall"],
        "error": row["error"],
        "result": result,
        # Same-origin cached thumbnail (shared item-image cache), or None so the
        # template falls back to a placeholder tile. Lets the dense results table
        # identify items visually without an extra fetch.
        "image_url": _item_image_url(row["item_id"]),
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
        breadcrumb="Insights · One-Time Snapshot",
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
        breadcrumb="Insights · Daily Monitoring",
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
        breadcrumb=f"Insights · {group['name']}",
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
        # No success flash: the results page's own in-progress message ("Extracting
        # data and creating the report…") already confirms the run started, so a
        # separate banner would be redundant (and "worker" is internal jargon).
        ci_jobs.enqueue_run(db, group_id, run_type="one_time")
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


@bp.route("/media/ci-ad/<int:run_id>/<int:keyword_id>/<ad_type>")
@login_required
def ci_ad_image(run_id, keyword_id, ad_type):
    """Serve a captured brand-ad creative (same-origin, so CSP img-src 'self')."""
    from flask import send_file

    path = ci_images.ad_image_abspath(run_id, keyword_id, ad_type)  # None if ad_type invalid
    if not path or not os.path.isfile(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg", max_age=86400)


def _sos_chart_scale(sos_rows) -> float:
    """Tallest organic+sponsored stack among share rows — the stacked-bar chart's
    y-scale. Bars visualize the table's share columns (not raw counts), so both the
    tracked-item and brand-level charts scale to their own tallest stack.
    """
    return max((r["organic_share"] + r["sponsored_share"] for r in sos_rows), default=0)


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

    sos_summary, brand_sos_summary, avg_ranks, rank_rows, share_rows = [], [], [], [], []
    brand_ads = []
    rank_map = None
    if run and run["status"] == "done":
        rid = run["id"]
        sos_summary = ci_analysis.snapshot_share_of_shelf(db, group_id, rid)
        # Brand-level share (all of a brand's SKUs, not just tracked) — the section
        # appended at the end of the report.
        brand_sos_summary = ci_analysis.snapshot_brand_share_of_shelf(db, group_id, rid)
        avg_ranks = ci_analysis.snapshot_brand_avg_rank(db, group_id, rid)
        rank_rows = ci_analysis.snapshot_rank_by_keyword_brand(db, group_id, rid)
        share_rows = ci_analysis.snapshot_share_by_keyword(db, group_id, rid)
        # Brand advertising presence (headline + sponsored-video ad sightings).
        brand_ads = ci_analysis.snapshot_brand_ads(db, group_id, rid)
        # Placement grid for the Overall Search Ranking section: each brand's
        # average rank mapped onto a page-1 result grid (page + PDF share this).
        depth = ci_analysis.snapshot_page1_depth(db, group_id, rid)
        rank_map = ci_analysis.build_rank_placement_map(avg_ranks, depth)

    return {
        "run": run,
        "config_summary": config_summary,
        "sos_summary": sos_summary,
        "brand_sos_summary": brand_sos_summary,
        "avg_ranks": avg_ranks,
        "rank_rows": rank_rows,
        "share_rows": share_rows,
        "brand_ads": brand_ads,
        "rank_map": rank_map,
    }


def _monitoring_data(db, group_id, period):
    """Gather the Daily Monitoring view's sections for a completed calendar period.

    Each results table is aggregated over the last completed ``period`` (week /
    month / quarter / year) with a delta vs the prior period and a per-period trend
    sparkline. The config summary and placement map mirror the snapshot layout.
    """
    # Config summary — always available (it's configuration, not run output).
    brands = ci_config.list_brands(db, group_id, g.user["id"])
    config_summary = {
        "my_brands": [b["name"] for b in brands if b["type"] == "mine"],
        "competitor_brands": [b["name"] for b in brands if b["type"] != "mine"],
        "products": _product_views(ci_config.list_products(db, group_id, g.user["id"])),
        "keywords": [k["keyword"] for k in ci_config.list_keywords(db, group_id, g.user["id"])],
    }

    avg_ranks = ci_analysis.monitoring_avg_rank(db, group_id, period)
    rank_rows = ci_analysis.monitoring_rank_by_keyword(db, group_id, period)
    sos_summary = ci_analysis.monitoring_share_of_shelf(db, group_id, period)
    brand_sos_summary = ci_analysis.monitoring_brand_share_of_shelf(db, group_id, period)
    share_rows = ci_analysis.monitoring_share_by_keyword(db, group_id, period)
    brand_ads = ci_analysis.monitoring_brand_ads(db, group_id, period)
    rank_map = ci_analysis.monitoring_placement_map(db, group_id, period, avg_ranks)

    return {
        "config_summary": config_summary,
        "sos_summary": sos_summary,
        "brand_sos_summary": brand_sos_summary,
        "avg_ranks": avg_ranks,
        "rank_rows": rank_rows,
        "share_rows": share_rows,
        "brand_ads": brand_ads,
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
    # The brand-level chart at the end of the report scales independently.
    sos_scale = _sos_chart_scale(data["sos_summary"])
    brand_sos_scale = _sos_chart_scale(data["brand_sos_summary"])

    run = data["run"]
    logger.info("CI snapshot results: group_id=%s user_id=%s run_id=%s rank_rows=%d",
                group_id, g.user["id"], run["id"] if run else None,
                len(data["rank_rows"]))
    return render_template(
        "app/ci_snapshot_results.html",
        breadcrumb=f"Insights · {group['name']}",
        active_nav="ci-snapshot",
        group=group,
        run=run,
        sos_summary=data["sos_summary"],
        brand_sos_summary=data["brand_sos_summary"],
        config_summary=data["config_summary"],
        avg_ranks=data["avg_ranks"],
        rank_rows=data["rank_rows"],
        share_rows=data["share_rows"],
        brand_ads=data["brand_ads"],
        rank_map=data["rank_map"],
        sos_scale=sos_scale,
        brand_sos_scale=brand_sos_scale,
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
        brand_sos_rows=data["brand_sos_summary"],
        brand_ads=data["brand_ads"],
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
    return raw if raw in ci_analysis.PERIODS else ci_analysis.DEFAULT_PERIOD


@bp.route("/app/competitive-intel/view-snapshot")
@login_required
def ci_view_snapshot():
    """Pick a one-time snapshot group from a dropdown; show its current-state results.

    The snapshot counterpart to :func:`ci_view`: same picker layout, rendering the
    snapshot results sections (no trends, no period window) for the selected group's
    latest completed run.
    """
    db = get_db()
    groups = ci_config.list_groups(db, g.user["id"], mode="snapshot")

    group_id = request.args.get("group_id", type=int)
    selected = None
    if group_id is not None:
        selected = ci_config.get_group(db, group_id, g.user["id"])
        if selected is not None and selected["mode"] != "snapshot":
            selected = None
    elif groups:
        selected = groups[0]  # default to the newest snapshot group

    data = {"run": None, "config_summary": None, "sos_summary": [], "brand_sos_summary": [],
            "avg_ranks": [], "rank_rows": [], "share_rows": [], "brand_ads": [], "rank_map": None}
    if selected is not None:
        data = _snapshot_data(db, selected["id"])

    sos_scale = _sos_chart_scale(data["sos_summary"])
    brand_sos_scale = _sos_chart_scale(data["brand_sos_summary"])
    logger.info("CI view-snapshot user_id=%s group_id=%s run_id=%s",
                g.user["id"], selected["id"] if selected else None,
                data["run"]["id"] if data["run"] else None)
    return render_template(
        "app/ci_view_snapshot.html",
        breadcrumb="Insights · View Snapshot",
        active_nav="ci-view-snapshot",
        groups=groups,
        selected=selected,
        run=data["run"],
        config_summary=data["config_summary"],
        sos_summary=data["sos_summary"],
        brand_sos_summary=data["brand_sos_summary"],
        avg_ranks=data["avg_ranks"],
        rank_rows=data["rank_rows"],
        share_rows=data["share_rows"],
        brand_ads=data["brand_ads"],
        rank_map=data["rank_map"],
        sos_scale=sos_scale,
        brand_sos_scale=brand_sos_scale,
    )


@bp.route("/app/competitive-intel/view")
@login_required
def ci_view():
    """Pick a monitoring group from a dropdown; show its results dashboard.

    Mirrors the One-Time Snapshot results layout (current-state ranking + share
    sections from the latest completed run) with a per-row trend sparkline added,
    over the selected period window.
    """
    db = get_db()
    groups = ci_config.list_groups(db, g.user["id"], mode="monitoring")

    group_id = request.args.get("group_id", type=int)
    selected = None
    if group_id is not None:
        selected = ci_config.get_group(db, group_id, g.user["id"])
        if selected is not None and selected["mode"] != "monitoring":
            selected = None
    elif groups:
        selected = groups[0]  # default to the newest monitoring group

    data = {"config_summary": None, "sos_summary": [], "brand_sos_summary": [],
            "avg_ranks": [], "rank_rows": [], "share_rows": [], "brand_ads": [], "rank_map": None}
    available: list = []
    period = ci_analysis.DEFAULT_PERIOD
    period_label = prior_label = None
    if selected is not None:
        # A period button is offered only once its most recent completed period
        # holds data; fall back to the first available period if the request asks
        # for one that isn't ready yet.
        available = ci_analysis.available_periods(db, selected["id"])
        requested = request.args.get("period")
        period = (requested if requested in available
                  else (available[0] if available else ci_analysis.DEFAULT_PERIOD))
        data = _monitoring_data(db, selected["id"], period)  # config summary always populated
        if available:
            period_label = ci_analysis.period_label(period)
            prior_label = ci_analysis.period_label(period, 1)

    # Stacked-bar chart scale: scale to the tallest organic+sponsored stack, so the
    # bars visualize the table's share columns (same as the snapshot page). The
    # brand-level chart at the end of the report scales independently.
    sos_scale = _sos_chart_scale(data["sos_summary"])
    brand_sos_scale = _sos_chart_scale(data["brand_sos_summary"])
    logger.info("CI view user_id=%s group_id=%s period=%s available=%s",
                g.user["id"], selected["id"] if selected else None, period, available)
    return render_template(
        "app/ci_view.html",
        breadcrumb="Insights · View Monitoring",
        active_nav="ci-view",
        groups=groups,
        selected=selected,
        period=period,
        periods=list(ci_analysis.PERIODS),
        period_labels=ci_analysis.PERIOD_LABELS,
        available=available,
        period_label=period_label,
        prior_label=prior_label,
        config_summary=data["config_summary"],
        sos_summary=data["sos_summary"],
        brand_sos_summary=data["brand_sos_summary"],
        avg_ranks=data["avg_ranks"],
        rank_rows=data["rank_rows"],
        share_rows=data["share_rows"],
        brand_ads=data["brand_ads"],
        rank_map=data["rank_map"],
        sos_scale=sos_scale,
        brand_sos_scale=brand_sos_scale,
    )


@bp.route("/app/competitive-intel/view/<int:group_id>/results.pdf")
@login_required
def ci_view_pdf(group_id):
    """Download the monitoring dashboard as a PDF (snapshot layout + trend lines)."""
    from datetime import date

    from flask import Response

    from app.pdf_export import build_ci_monitoring_pdf

    group = _owned_group_or_404(group_id)
    db = get_db()
    period = _resolve_period(request.args.get("period"))
    # Same data the page renders (see _monitoring_data): aggregated over the last
    # completed calendar period, each row carrying its delta + per-period trend.
    data = _monitoring_data(db, group_id, period)
    pdf = build_ci_monitoring_pdf(
        dict(group), period,
        config_summary=data["config_summary"],
        avg_ranks=data["avg_ranks"],
        rank_rows=data["rank_rows"],
        sos_rows=data["sos_summary"],
        share_rows=data["share_rows"],
        brand_sos_rows=data["brand_sos_summary"],
        brand_ads=data["brand_ads"],
        rank_map=data["rank_map"],
        period_label=ci_analysis.period_label(period),
        prior_label=ci_analysis.period_label(period, 1),
    )
    filename = f"ci-monitoring-{_slug(group['name'])}-{date.today().isoformat()}.pdf"
    logger.info("CI monitoring PDF: group_id=%s period=%s user_id=%s", group_id, period, g.user["id"])
    return Response(pdf, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})
