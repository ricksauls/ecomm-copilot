"""Google SSO via OpenID Connect (Authlib).

This is optional and fully gated on configuration: if ``GOOGLE_CLIENT_ID`` and
``GOOGLE_CLIENT_SECRET`` are not set, SSO stays disabled, the routes 404, and
the sign-in page hides the button. That keeps the app running with email +
password alone until real OAuth credentials are provisioned.

To enable in production: create an OAuth 2.0 Client (type "Web application") in
Google Cloud Console, add the redirect URI ``https://<host>/auth/google/callback``,
and set the two env vars in the app's ``.env``.
"""

import logging

from authlib.integrations.flask_client import OAuth
from flask import Flask

logger = logging.getLogger(__name__)

# Google's OIDC discovery document; Authlib reads endpoints and JWKS from it.
_GOOGLE_METADATA_URL = "https://accounts.google.com/.well-known/openid-configuration"

oauth = OAuth()


def init_oauth(app: Flask) -> None:
    """Register the Google client if credentials are configured.

    Sets ``app.config['SSO_GOOGLE_ENABLED']`` so routes and templates can gate
    on a single flag. Secrets are read from config (populated from env), never
    hardcoded.
    """
    oauth.init_app(app)

    client_id = app.config.get("GOOGLE_CLIENT_ID")
    client_secret = app.config.get("GOOGLE_CLIENT_SECRET")
    enabled = bool(client_id and client_secret)
    app.config["SSO_GOOGLE_ENABLED"] = enabled

    if not enabled:
        logger.info("Google SSO disabled (GOOGLE_CLIENT_ID/SECRET not set)")
        return

    oauth.register(
        name="google",
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=_GOOGLE_METADATA_URL,
        # openid+email is all we need to identify the account.
        client_kwargs={"scope": "openid email"},
    )
    logger.info("Google SSO enabled")
