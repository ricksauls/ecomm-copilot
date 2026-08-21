"""Background PDP scoring worker.

Runs as its own process (systemd: ecomm-copilot-worker) so the slow, serial
browser work never touches a web request. Polls the ``scored_items`` queue,
claims one item at a time, fetches the PDP in a real browser, scores it, and
writes the result back. It must run with a display available (DISPLAY=:99 via
the droplet's Xvfb) because headed Chrome evades Walmart's bot defense.

Run locally: ``python worker.py`` (needs Playwright + a browser + DISPLAY).
"""

import logging
import os
import random
import sqlite3
import time

from dotenv import load_dotenv

# Load DATABASE_URL (and anything else) before importing app modules that read it.
load_dotenv()

from app import jobs, keywords  # noqa: E402  (import after load_dotenv is intentional)
from app.fetch import FetchBlocked, FetchError, fetch_pdp  # noqa: E402
from app.scoring import result_to_dict, score_pdp  # noqa: E402

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("worker")

# How often to poll when the queue is empty, and the polite delay between
# fetches so we don't hammer Walmart (mirrors the WM scraper's cadence).
POLL_INTERVAL_S = 5
FETCH_DELAY_RANGE_S = (8, 16)


def connect() -> sqlite3.Connection:
    """Open the worker's own SQLite connection (separate from the Flask app)."""
    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def process_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Fetch, score, and persist one claimed item. Never raises."""
    row_id = row["id"]
    try:
        pdp = fetch_pdp(row["url"], row["item_id"])
        # Resolve the target keyword set for keyword-coverage scoring. Prefer the
        # shared cache (same-category items reuse one discovery); on a miss, run
        # discovery and cache the result. Best-effort throughout: any failure
        # scores the item without keywords rather than failing it.
        try:
            key = keywords.cache_key(pdp)
            cached = jobs.get_cached_keywords(conn, key)
            if cached is not None:
                pdp.target_keywords = cached
                log.info("Keyword cache HIT id=%s key=%r (%d kw)", row_id, key, len(cached))
            else:
                found = keywords.discover_keywords(pdp)
                pdp.target_keywords = found
                if found:
                    jobs.put_cached_keywords(conn, key, found)
                log.info("Keyword cache MISS id=%s key=%r discovered=%d", row_id, key, len(found))
        except Exception:  # noqa: BLE001 - keyword resolution must never fail scoring
            log.exception("Keyword resolution failed id=%s; scoring without it", row_id)
        result = score_pdp(pdp)
        jobs.save_result(conn, row_id, result.overall, result_to_dict(result), pdp.title)
        log.info("Scored id=%s item=%s overall=%s", row_id, row["item_id"], result.overall)
    except FetchBlocked as e:
        log.warning("Blocked id=%s: %s", row_id, e)
        jobs.mark_failed(conn, row_id, "blocked", str(e))
    except FetchError as e:
        log.warning("Fetch error id=%s: %s", row_id, e)
        jobs.mark_failed(conn, row_id, "error", str(e))
    except Exception as e:  # noqa: BLE001 - a bad item must not kill the worker
        log.exception("Unexpected error scoring id=%s", row_id)
        jobs.mark_failed(conn, row_id, "error", f"Unexpected error: {e}")


def main() -> None:
    """Claim-and-process loop. Runs until the process is stopped."""
    conn = connect()
    log.info("PDP scoring worker started")
    while True:
        row = jobs.claim_next(conn)
        if row is None:
            time.sleep(POLL_INTERVAL_S)
            continue
        process_one(conn, row)
        time.sleep(random.uniform(*FETCH_DELAY_RANGE_S))


if __name__ == "__main__":
    main()
