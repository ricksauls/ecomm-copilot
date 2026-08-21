"""Page routes for the public surface and the authenticated workspace.

Three routes in this pass: the marketing landing page and sign-in page (public,
dark surface) and the agency dashboard (workspace, light surface). The remaining
workspace screens (product, analysis, creative, share of shelf) are designed in
the handoff but not yet built.

Sign-in / sign-up / sign-out live in the ``auth`` blueprint. The dashboard is
guarded by ``login_required``, so it is no longer world-reachable.
"""

import logging

from flask import Blueprint, render_template, request

from app import fixtures, pdp
from app.security import login_required

logger = logging.getLogger(__name__)

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
    """PDP Content Scoring intake: collect item URLs to score.

    Users add one or more Walmart item URLs via the repeatable fields and/or
    upload a CSV of them. This screen validates and collects the items; the
    scoring engine that consumes them is a follow-up, so a POST currently echoes
    the parsed/validated list back as confirmation.
    """
    if request.method == "POST":
        form_urls = request.form.getlist("urls")
        csv_file = request.files.get("csv")
        accepted, rejected = pdp.collect_items(form_urls, csv_file)
        # Pair each accepted URL with its parsed Walmart item number (the
        # trailing path segment) for display.
        accepted_items = [
            {"url": url, "item": pdp.item_number_from_url(url)} for url in accepted
        ]
        logger.info(
            "PDP scoring intake submitted: %d accepted, %d rejected",
            len(accepted),
            len(rejected),
        )
        return render_template(
            "app/pdp_scoring.html",
            breadcrumb="PDP Content Scoring",
            active_nav="pdp-scoring",
            submitted=True,
            accepted_items=accepted_items,
            rejected=rejected,
        )

    logger.info("Serving PDP Content Scoring intake")
    return render_template(
        "app/pdp_scoring.html",
        breadcrumb="PDP Content Scoring",
        active_nav="pdp-scoring",
        submitted=False,
        max_items=pdp.MAX_ITEMS,
    )
