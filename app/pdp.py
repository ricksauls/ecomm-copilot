"""Helpers for the PDP Content Scoring intake screen.

Collects item URLs from either the repeatable URL fields or an uploaded CSV,
validating every value at the boundary. Nothing here trusts its input: URLs are
scheme-checked and length-bounded, the number of items is capped, and the CSV is
parsed defensively with a size limit (see security-standards).
"""

import csv
import io
import logging
from urllib.parse import urlparse

from werkzeug.datastructures import FileStorage

logger = logging.getLogger(__name__)

# Bounds. Unbounded input is a DoS vector, so cap both the per-URL length and
# the total number of items accepted in one submission.
MAX_URL_LEN = 2048
MAX_ITEMS = 100
# Read at most this many bytes from an uploaded CSV, regardless of the app-wide
# MAX_CONTENT_LENGTH, as a second belt-and-braces cap.
MAX_CSV_BYTES = 2 * 1024 * 1024
_ALLOWED_SCHEMES = {"http", "https"}

# The stable part of a Walmart product URL up to (but not including) the item
# number. Intake fields autofill this so the user only appends the number; the
# templates and intake.js reuse it. Keep in sync with the JS copy in intake.js.
WALMART_IP_PREFIX = "https://www.walmart.com/ip/"


def validate_item_url(raw: str) -> str | None:
    """Return a cleaned URL if valid, else None.

    Allowlist the http/https schemes (rejecting javascript:, data:, file:, …),
    require a host, and bound the length.
    """
    if not raw:
        return None
    url = raw.strip()
    if not url or len(url) > MAX_URL_LEN:
        return None
    parsed = urlparse(url)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        return None
    return url


def item_number_from_url(url: str) -> str | None:
    """Return the Walmart item number — the trailing numeric path segment.

    Walmart product URLs look like ``https://www.walmart.com/ip/<slug>/10294528``
    (or ``/ip/10294528``); the item number is the last path segment. Returns
    None when the last segment isn't numeric, so callers can flag URLs that
    don't match the expected shape.
    """
    path = urlparse(url).path
    segments = [s for s in path.split("/") if s]
    if segments and segments[-1].isdigit():
        return segments[-1]
    return None


def urls_from_csv(file: FileStorage) -> list[str]:
    """Extract candidate URLs from an uploaded CSV.

    Scans every cell and keeps the ones that look like http(s) URLs, so the
    column layout and any header row don't matter (headers aren't URLs). Reads
    at most MAX_CSV_BYTES and stops once MAX_ITEMS candidates are found.
    """
    raw = file.read(MAX_CSV_BYTES + 1)
    if len(raw) > MAX_CSV_BYTES:
        logger.warning("Uploaded CSV exceeded %d bytes; truncating", MAX_CSV_BYTES)
        raw = raw[:MAX_CSV_BYTES]

    # Decode leniently — a stray non-UTF-8 byte shouldn't sink the whole file.
    text = raw.decode("utf-8", errors="replace")

    found: list[str] = []
    try:
        for row in csv.reader(io.StringIO(text)):
            for cell in row:
                cleaned = validate_item_url(cell)
                if cleaned:
                    found.append(cleaned)
                    if len(found) >= MAX_ITEMS:
                        return found
    except csv.Error as e:
        # Malformed CSV: keep whatever we parsed before the error rather than
        # failing the whole submission.
        logger.warning("CSV parse error, using %d rows parsed so far: %s", len(found), e)
    return found


def collect_items(form_urls: list[str], csv_file: FileStorage | None) -> tuple[list[str], list[str]]:
    """Merge, validate, and de-duplicate items from the form fields and CSV.

    Returns ``(accepted, rejected)`` where ``accepted`` is the de-duplicated,
    order-preserving list of valid URLs (capped at MAX_ITEMS) and ``rejected``
    is the list of non-empty entries that failed validation, for user feedback.
    """
    accepted: list[str] = []
    rejected: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str, *, from_form: bool) -> None:
        cleaned = validate_item_url(candidate)
        if cleaned is None:
            # Only surface non-empty rejects from the form; empty rows are noise.
            if from_form and candidate.strip():
                rejected.append(candidate.strip()[:MAX_URL_LEN])
            return
        if item_number_from_url(cleaned) is None:
            # A URL with no item number is unusable. Silently skip an untouched
            # autofill prefix (the field pre-populates WALMART_IP_PREFIX and the
            # user may leave spare rows unfinished); flag anything else as a reject.
            stripped = candidate.strip().rstrip("/")
            if from_form and stripped and stripped != WALMART_IP_PREFIX.rstrip("/"):
                rejected.append(candidate.strip()[:MAX_URL_LEN])
            return
        if cleaned in seen:
            return
        seen.add(cleaned)
        if len(accepted) < MAX_ITEMS:
            accepted.append(cleaned)

    for value in form_urls:
        _add(value, from_form=True)

    if csv_file is not None and csv_file.filename:
        for value in urls_from_csv(csv_file):
            _add(value, from_form=False)

    return accepted, rejected
