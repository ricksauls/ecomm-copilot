"""Page routes for the public surface and the authenticated workspace.

Three routes in this pass: the marketing landing page and sign-in page (public,
dark surface) and the agency dashboard (workspace, light surface). The remaining
workspace screens (product, analysis, creative, share of shelf) are designed in
the handoff but not yet built.

Auth is not implemented yet: the dashboard is reachable directly. Before this
ships, /app and the other workspace routes need a real session check and route
guard (see the handoff's State Management section) rather than open access.
"""

import logging

from flask import Blueprint, render_template

from app import fixtures

logger = logging.getLogger(__name__)

bp = Blueprint("pages", __name__)


@bp.route("/")
def landing():
    """Marketing landing page. Public, dark surface."""
    logger.info("Serving landing page")
    return render_template("landing.html")


@bp.route("/signin")
def signin():
    """Sign-in page. Public, dark surface.

    The form is presentational only in this pass. Real authentication,
    validation, and error states are a follow-up (see handoff: the fields
    are static, and focus/invalid/loading states are undesigned).
    """
    logger.info("Serving sign-in page")
    return render_template("signin.html")


@bp.route("/app")
def dashboard():
    """Agency dashboard. Authenticated workspace, light surface.

    NOTE: no auth guard yet. Add a session check before launch so this is
    not world-reachable.
    """
    logger.info("Serving dashboard")
    view_model = fixtures.get_dashboard()
    return render_template(
        "app/dashboard.html",
        breadcrumb="Meridian Commerce Group",
        active_nav="dashboard",
        **view_model,
    )
