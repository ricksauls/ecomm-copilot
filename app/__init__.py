"""Application factory for the ecomm-copilot web app.

Wires up logging, configuration, security-relevant response headers, error
handlers, and the page blueprint. Import ``create_app`` and call it to get a
configured Flask instance; ``wsgi.py`` at the repo root does exactly that for
production servers.
"""

import logging
import os

from flask import Flask, render_template


def _configure_logging(app: Flask) -> None:
    """Configure application-wide logging once, at startup.

    Per the logging standard: a single configuration point, module-level
    loggers everywhere else. Level is INFO by default and can be lifted to
    DEBUG via the LOG_LEVEL env var when diagnosing an issue in place.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app.logger.setLevel(level)


def _register_security_headers(app: Flask) -> None:
    """Attach conservative security headers to every response.

    Centralised here rather than per-route so no endpoint can forget them.
    CSP is deliberately strict: this app self-hosts everything (fonts, CSS,
    JS), so no external origins need to be allowed. Revisit the CSP if a
    third-party script or asset is ever introduced.
    """

    @app.after_request
    def _apply_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "font-src 'self'; "
            "script-src 'self'"
        )
        return response


def _register_error_handlers(app: Flask) -> None:
    """Register app-wide error handlers as a safety net.

    Client-facing pages get a friendly template; nothing leaks stack traces
    or internals to the browser. The 500 handler logs with a full traceback
    so the failure is diagnosable server-side.
    """
    logger = logging.getLogger(__name__)

    @app.errorhandler(404)
    def not_found(error):
        return render_template("errors/404.html"), 404

    @app.errorhandler(500)
    def internal_error(error):
        logger.error("Unhandled 500 serving a request: %s", error, exc_info=True)
        return render_template("errors/500.html"), 500


def create_app() -> Flask:
    """Build and return a configured Flask application instance."""
    app = Flask(__name__)

    _configure_logging(app)
    logger = logging.getLogger(__name__)

    # SECRET_KEY signs the session cookie. It must come from the environment
    # and never be hardcoded. Fail loudly at startup if it's missing rather
    # than silently falling back to an insecure default.
    secret_key = os.environ.get("SECRET_KEY")
    if not secret_key:
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_hex(32))\"` "
            "and set it in your environment (.env locally, Actions secret in CI)."
        )
    app.config["SECRET_KEY"] = secret_key

    # Session cookie hardening. Secure=True requires HTTPS; it can be relaxed
    # for local http development via SESSION_COOKIE_SECURE=false.
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = (
        os.environ.get("SESSION_COOKIE_SECURE", "true").lower() != "false"
    )

    # Google SSO credentials (optional). Read into config so oauth.init_oauth
    # can gate on their presence; absent -> SSO stays disabled.
    app.config["GOOGLE_CLIENT_ID"] = os.environ.get("GOOGLE_CLIENT_ID")
    app.config["GOOGLE_CLIENT_SECRET"] = os.environ.get("GOOGLE_CLIENT_SECRET")

    _register_security_headers(app)
    _register_error_handlers(app)

    # Database: resolve path, create schema, register teardown.
    from app import db

    db.init_app(app)

    # Optional Google SSO (no-op when unconfigured).
    from app.oauth import init_oauth

    init_oauth(app)

    # Auth request lifecycle: load the current user, then enforce CSRF on unsafe
    # methods. Registered before blueprints so they run for every route.
    from app import security

    app.before_request(security.load_current_user)
    app.before_request(security.verify_csrf)

    # Expose the CSRF token and current user to every template.
    @app.context_processor
    def _inject_auth_context():
        from flask import g

        return {"csrf_token": security.csrf_token, "current_user": g.get("user")}

    # Blueprints: public/workspace pages and the auth routes.
    from app import auth
    from app.routes import pages

    app.register_blueprint(pages.bp)
    app.register_blueprint(auth.bp)

    logger.info("Application initialised (log level %s)", logging.getLevelName(app.logger.level))
    return app
