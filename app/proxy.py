"""Residential-proxy configuration shared by the browser-based fetchers.

Reads the ``WALMART_PROXY_*`` environment variables (a live Oxylabs residential
endpoint on the droplet) and returns a Playwright ``proxy`` dict, or ``None`` when
unset — in which case the caller runs direct (no proxy). Used by the CI scraper
(every search scrape) and by Import Product Images (product-page fetches only).

Config-gated and metered per-GB, so callers opt in deliberately: routing a request
through the proxy spends bandwidth, and only bot-protected *page* fetches need it —
image-CDN downloads stay direct.
"""

import os


def proxy_from_env() -> dict | None:
    """Playwright proxy config from ``WALMART_PROXY_*``, or ``None`` when unset.

    - ``WALMART_PROXY_SERVER``   e.g. ``http://pr.oxylabs.io:7777`` (required)
    - ``WALMART_PROXY_USERNAME`` provider username (optional)
    - ``WALMART_PROXY_PASSWORD`` provider password (optional)

    An empty/whitespace ``SERVER`` reads as unset (direct). The password is only
    included when a username is set; it is never logged by callers.
    """
    server = os.environ.get("WALMART_PROXY_SERVER", "").strip()
    if not server:
        return None
    proxy = {"server": server}
    username = os.environ.get("WALMART_PROXY_USERNAME", "").strip()
    if username:
        proxy["username"] = username
        proxy["password"] = os.environ.get("WALMART_PROXY_PASSWORD", "")
    return proxy
