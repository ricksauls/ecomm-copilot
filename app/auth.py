"""Authentication routes: sign up, sign in, sign out, and Google SSO.

Design choices:
- Login failures return one generic message ("Email or password is incorrect")
  regardless of which was wrong, so the form can't be used to enumerate which
  emails have accounts.
- Successful sign-up logs the user straight in (core-auth pass; email
  verification is a follow-up).
- Every state-changing route is a POST guarded by the global CSRF check.
"""

import logging

from flask import (
    Blueprint,
    abort,
    current_app,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from app import security, users
from app.oauth import oauth
from app.users import EmailAlreadyRegistered

logger = logging.getLogger(__name__)

bp = Blueprint("auth", __name__)


@bp.route("/signup", methods=["GET", "POST"])
def signup():
    """Self-service account creation with email + password."""
    if g.get("user") is not None:
        return redirect(url_for("pages.dashboard"))

    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")

        error = security.validate_credentials(email, password)
        if error is None:
            try:
                user_id = users.create_local_user(email, password)
            except EmailAlreadyRegistered:
                # Same generic-ish message either way; we don't hide that an
                # email is taken here because sign-up needs to tell the user to
                # sign in instead, but we don't leak anything a user couldn't
                # discover by trying to register.
                error = "That email is already registered. Try signing in."
            else:
                security.login_user(user_id)
                logger.info("Sign-up complete, session started: user_id=%s", user_id)
                return redirect(url_for("pages.dashboard"))

        return render_template("signup.html", error=error, email=email), 400

    return render_template("signup.html", error=None, email="")


@bp.route("/signin", methods=["GET", "POST"])
def signin():
    """Email + password sign-in. Also the redirect target for the login guard."""
    if g.get("user") is not None:
        return redirect(url_for("pages.dashboard"))

    next_url = security.safe_next_url(request.args.get("next"))

    if request.method == "POST":
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        next_url = security.safe_next_url(request.form.get("next")) or next_url

        user = users.get_by_email(email)
        if user is not None and users.verify_password(user, password):
            security.login_user(int(user["id"]))
            logger.info("Login success: user_id=%s", user["id"])
            return redirect(next_url or url_for("pages.dashboard"))

        # Generic failure — do not reveal whether the email exists.
        logger.warning("Login failed for email=%s", users.normalize_email(email))
        return (
            render_template(
                "signin.html",
                error="Email or password is incorrect.",
                email=email,
                next=next_url,
                sso_google=current_app.config.get("SSO_GOOGLE_ENABLED", False),
            ),
            401,
        )

    return render_template(
        "signin.html",
        error=None,
        email="",
        next=next_url,
        sso_google=current_app.config.get("SSO_GOOGLE_ENABLED", False),
    )


@bp.route("/signout", methods=["POST"])
def signout():
    """Sign out and return to the landing page. POST-only + CSRF protected."""
    user_id = g.user["id"] if g.get("user") else None
    security.logout_user()
    logger.info("Logout: user_id=%s", user_id)
    return redirect(url_for("pages.landing"))


# --- Google SSO (gated on configuration) ----------------------------------


@bp.route("/auth/google")
def google_login():
    """Kick off the Google OAuth flow. 404s when SSO isn't configured."""
    if not current_app.config.get("SSO_GOOGLE_ENABLED"):
        abort(404)
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@bp.route("/auth/google/callback")
def google_callback():
    """Complete the Google OAuth flow and start a session.

    Authlib validates the ``state`` parameter and the ID token signature. We
    trust only the verified ``email`` claim to find-or-create the account.
    """
    if not current_app.config.get("SSO_GOOGLE_ENABLED"):
        abort(404)

    try:
        token = oauth.google.authorize_access_token()
    except Exception:
        # Any failure in the exchange (bad state, denied consent, network) is
        # logged server-side and shown to the user as a generic error.
        logger.warning("Google OAuth exchange failed", exc_info=True)
        return render_template("signin.html", error="Single sign-on failed. Try again.",
                               email="", next=None, sso_google=True), 400

    userinfo = token.get("userinfo") or {}
    email = userinfo.get("email")
    email_verified = userinfo.get("email_verified", False)
    if not email or not email_verified:
        logger.warning("Google OAuth returned no verified email")
        return render_template("signin.html", error="Single sign-on failed. Try again.",
                               email="", next=None, sso_google=True), 400

    user_id = users.get_or_create_sso_user(email, "google")
    security.login_user(user_id)
    logger.info("SSO login success: user_id=%s provider=google", user_id)
    return redirect(url_for("pages.dashboard"))
