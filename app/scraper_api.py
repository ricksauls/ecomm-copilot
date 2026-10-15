"""Oxylabs managed-scraping client for bot-blocked Walmart (and Amazon) pages.

Walmart hard-blocks the droplet's datacenter IP — and even a residential proxy —
with a "Robot or human?" press-and-hold captcha, fingerprinting the automation
itself rather than just the IP (verified live 2026-10-14: direct and via the
residential proxy both land on ``walmart.com/blocked``). This module routes page
fetches through Oxylabs' managed scraping products, which solve the
captcha/fingerprint/retry server-side and return the already-rendered HTML:

  1. **Web Scraper API** (primary) — a realtime HTTP endpoint that renders the
     page server-side and returns its HTML in one request/response.
  2. **Web Unblocker** (fallback) — a proxy endpoint that renders + unblocks and
     returns the HTML; used only when the Scraper API is unavailable or fails.

The returned HTML carries Walmart's ``__NEXT_DATA__`` blob, so callers in
:mod:`app.fetch` parse it with the same extractors they already use for the
browser path — the HTML source is the only thing that changes.

**Config-gated.** When no Oxylabs credentials are set the module is inert
(:func:`scraper_enabled` is ``False``) and :mod:`app.fetch` keeps driving a real
browser, so local development needs no account. On the droplet (where the browser
is blocked) the credentials are set and this becomes the live fetch path.

Credentials are read from the environment only — never ``app.config`` (so they
can't leak into a template or error page) and never logged.
"""

import logging
import os
import time
import warnings

from app.fetch import FetchBlocked, FetchError

logger = logging.getLogger(__name__)

# Oxylabs Web Scraper API (primary): realtime endpoint returns the rendered page
# in the response body. Overridable for a different region/endpoint.
_SCRAPER_URL_DEFAULT = "https://realtime.oxylabs.io/v1/queries"
# Oxylabs Web Unblocker (fallback): a rendering proxy. Its TLS is terminated at
# the proxy, so requests through it must skip certificate verification (Oxylabs'
# documented requirement) — scoped to that one call, see _unblocker_html.
_UNBLOCKER_SERVER_DEFAULT = "https://unblock.oxylabs.io:60000"

# Server-side render + unblock can take noticeably longer than a local page load,
# so the default timeout is generous.
_DEFAULT_TIMEOUT_S = 90
# Transient-failure retries (network error / 5xx / 429). 4xx are NOT retried —
# an auth/client error won't fix itself.
_MAX_ATTEMPTS = 3

# Block detection for the returned HTML. We use markers specific to Walmart's
# captcha interstitial rather than app.fetch._BLOCK_MARKERS: those broad markers
# ("robot", "blocked") match the *visible body text* of the tiny challenge page,
# but here we scan the full markup — where a legitimate product (e.g. a "robot
# vacuum") would false-positive. These phrases appear only on the block page.
_API_BLOCK_MARKERS = (
    "robot or human?",
    "activate and hold the button",
    "/blocked?url=",
)


def _scraper_creds() -> tuple[str, str]:
    return (
        (os.environ.get("OXYLABS_SCRAPER_USERNAME") or "").strip(),
        (os.environ.get("OXYLABS_SCRAPER_PASSWORD") or "").strip(),
    )


def _unblocker_creds() -> tuple[str, str]:
    return (
        (os.environ.get("OXYLABS_UNBLOCKER_USERNAME") or "").strip(),
        (os.environ.get("OXYLABS_UNBLOCKER_PASSWORD") or "").strip(),
    )


def _scraper_configured() -> bool:
    user, pw = _scraper_creds()
    return bool(user and pw)


def _unblocker_configured() -> bool:
    user, pw = _unblocker_creds()
    return bool(user and pw)


def scraper_enabled() -> bool:
    """True when any Oxylabs managed fetcher is configured (the API path is live).

    When ``False``, :mod:`app.fetch` falls back to driving a real browser.
    """
    return _scraper_configured() or _unblocker_configured()


def fetch_html(url: str, *, timeout_s: int = _DEFAULT_TIMEOUT_S) -> str:
    """Return the server-rendered HTML of ``url`` via Oxylabs.

    Tries the Web Scraper API first, then the Web Unblocker; each configured
    provider is attempted in turn so one provider's outage or block doesn't sink
    the fetch. Raises :class:`FetchBlocked` when every provider reports a block
    (so the worker records it as "blocked"), or :class:`FetchError` when no
    provider is configured or all fail for other reasons.
    """
    if not scraper_enabled():
        raise FetchError("No Oxylabs scraping credentials configured")

    errors: list[str] = []
    blocked = False
    for name, configured, provider in (
        ("Scraper API", _scraper_configured(), _scraper_api_html),
        ("Web Unblocker", _unblocker_configured(), _unblocker_html),
    ):
        if not configured:
            continue
        try:
            return provider(url, timeout_s=timeout_s)
        except FetchBlocked as e:
            blocked = True
            errors.append(f"{name}: {e}")
            logger.warning("%s reported a block for %s; trying next provider", name, url)
        except FetchError as e:
            errors.append(f"{name}: {e}")
            logger.warning("%s failed for %s: %s; trying next provider", name, url, e)

    detail = "; ".join(errors) or "no provider attempted"
    if blocked:
        raise FetchBlocked(f"All Oxylabs providers blocked for {url}: {detail}")
    raise FetchError(f"All Oxylabs providers failed for {url}: {detail}")


def _scraper_api_html(url: str, *, timeout_s: int) -> str:
    """Fetch rendered HTML via the Web Scraper API realtime endpoint."""
    import requests  # local import keeps module import cheap / dependency-light

    user, pw = _scraper_creds()
    endpoint = (os.environ.get("OXYLABS_SCRAPER_URL") or _SCRAPER_URL_DEFAULT).strip()
    # ``universal`` renders any URL and returns its HTML; ``render=html`` forces a
    # full JS render (Walmart's PDP is client-hydrated).
    payload = {"source": "universal", "url": url, "render": "html"}

    logger.info("Scraper API request: url=%s", url)
    started = time.monotonic()
    resp = _send_with_retry(
        "Scraper API",
        lambda: requests.post(endpoint, json=payload, auth=(user, pw), timeout=timeout_s),
    )
    elapsed = time.monotonic() - started
    if resp.status_code != 200:
        # Body may carry a provider error message; it holds no secrets.
        raise FetchError(f"Scraper API returned {resp.status_code}: {resp.text[:300]}")

    html_text = _scraper_content(resp)
    logger.info("Scraper API done: url=%s bytes=%d elapsed=%.1fs", url, len(html_text), elapsed)
    _raise_if_block(html_text, "Scraper API")
    return html_text


def _scraper_content(resp) -> str:
    """Pull the HTML string out of the Scraper API's ``{results:[{content}]}``."""
    try:
        data = resp.json()
    except ValueError as e:
        raise FetchError(f"Scraper API returned non-JSON: {e}") from e
    results = (data or {}).get("results") or []
    if not results:
        raise FetchError("Scraper API returned no results")
    content = results[0].get("content")
    if not isinstance(content, str) or not content:
        raise FetchError("Scraper API result carried no HTML content")
    return content


def _unblocker_html(url: str, *, timeout_s: int) -> str:
    """Fetch rendered HTML via the Web Unblocker proxy (fallback provider)."""
    import requests
    import urllib3

    user, pw = _unblocker_creds()
    server = (os.environ.get("OXYLABS_UNBLOCKER_SERVER") or _UNBLOCKER_SERVER_DEFAULT).strip()
    proxy_url = _with_credentials(server, user, pw)
    proxies = {"http": proxy_url, "https": proxy_url}
    headers = {"x-oxylabs-render": "html"}

    logger.info("Web Unblocker request: url=%s", url)
    started = time.monotonic()
    # The Unblocker terminates TLS at the proxy, so cert verification is disabled
    # for THIS request only (documented Oxylabs requirement). Suppress the one-off
    # InsecureRequestWarning locally rather than globally, so other HTTPS calls
    # keep full verification.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)
        resp = _send_with_retry(
            "Web Unblocker",
            lambda: requests.get(
                url, proxies=proxies, headers=headers, timeout=timeout_s, verify=False
            ),
        )
    elapsed = time.monotonic() - started
    if resp.status_code != 200:
        raise FetchError(f"Web Unblocker returned {resp.status_code}: {resp.text[:300]}")

    html_text = resp.text or ""
    if not html_text:
        raise FetchError("Web Unblocker returned an empty body")
    logger.info("Web Unblocker done: url=%s bytes=%d elapsed=%.1fs", url, len(html_text), elapsed)
    _raise_if_block(html_text, "Web Unblocker")
    return html_text


def _with_credentials(server: str, user: str, pw: str) -> str:
    """Inject ``user:pass@`` into a proxy URL, URL-encoding the credentials."""
    if not user:
        return server
    from urllib.parse import quote, urlsplit, urlunsplit

    parts = urlsplit(server)
    netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{parts.netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _raise_if_block(html_text: str, where: str) -> None:
    """Raise :class:`FetchBlocked` if the HTML is Walmart's captcha interstitial."""
    body = (html_text or "").lower()
    if any(marker in body for marker in _API_BLOCK_MARKERS):
        raise FetchBlocked(f"{where}: Walmart returned a block page")


def _send_with_retry(label: str, do_request):
    """Run ``do_request()`` (→ a requests ``Response``) with backoff on transients.

    Retries network errors, 5xx and 429 up to :data:`_MAX_ATTEMPTS` with
    exponential backoff; returns immediately on any other status (incl. 4xx,
    which a retry won't fix). Raises :class:`FetchError` when every attempt
    raised a request exception.
    """
    import requests

    last_exc: Exception | None = None
    resp = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            resp = do_request()
        except requests.RequestException as e:
            last_exc = e
            resp = None
            logger.warning("%s attempt %d/%d errored: %s", label, attempt, _MAX_ATTEMPTS, e)
        else:
            if resp.status_code < 500 and resp.status_code != 429:
                return resp
            logger.warning(
                "%s attempt %d/%d → HTTP %d", label, attempt, _MAX_ATTEMPTS, resp.status_code
            )
        if attempt < _MAX_ATTEMPTS:
            time.sleep(2 ** attempt)  # 2s, 4s

    if resp is not None:
        return resp  # last 5xx/429 response; caller surfaces the status
    raise FetchError(f"{label} failed after {_MAX_ATTEMPTS} attempts: {last_exc}") from last_exc
