"""Background PDP scoring worker.

Runs as its own process (systemd: ecomm-copilot-worker) so the slow, serial
browser work never touches a web request. Polls the ``scored_items`` queue,
claims one item at a time, fetches the PDP in a real browser, scores it, and
writes the result back. It must run with a display available (DISPLAY=:99 via
the droplet's Xvfb) because headed Chrome evades Walmart's bot defense.

Run locally: ``python worker.py`` (needs Playwright + a browser + DISPLAY).
"""

import json
import logging
import os
import random
import sqlite3
import time

from dotenv import load_dotenv

# Load DATABASE_URL (and anything else) before importing app modules that read it.
load_dotenv()

from dataclasses import replace  # noqa: E402

from app import copy_jobs, copygen, db, jobs, keywords  # noqa: E402  (after load_dotenv is intentional)
from app.fetch import FetchBlocked, FetchError, fetch_pdp  # noqa: E402
from app.scoring import PdpRecord, result_to_dict, score_pdp  # noqa: E402

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
    """Open the worker's own SQLite connection (separate from the Flask app).

    Ensures the schema exists before returning, so the worker never depends on
    the web app having initialized the DB first — on a deploy that adds a table,
    the worker can restart ahead of the web app and would otherwise crash on the
    missing table (see ``db.ensure_schema``).
    """
    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)
    return conn


def resolve_keywords(conn: sqlite3.Connection, row_id: int, pdp) -> list[str] | None:
    """Resolve the target keyword set for a fetched PDP, cache-first.

    Prefer the shared cache (same-category items reuse one discovery); on a miss,
    run discovery and cache the result. Best-effort: any failure returns None so
    the caller proceeds without keywords rather than failing the item. Sets
    ``pdp.target_keywords`` as a side effect and returns the same value.
    """
    try:
        key = keywords.cache_key(pdp)
        cached = jobs.get_cached_keywords(conn, key)
        if cached is not None:
            pdp.target_keywords = cached
            log.info("Keyword cache HIT id=%s key=%r (%d kw)", row_id, key, len(cached))
            return cached
        found = keywords.discover_keywords(pdp)
        pdp.target_keywords = found
        if found:
            jobs.put_cached_keywords(conn, key, found)
        log.info("Keyword cache MISS id=%s key=%r discovered=%d", row_id, key, len(found))
        return found
    except Exception:  # noqa: BLE001 - keyword resolution must never fail the job
        log.exception("Keyword resolution failed id=%s; continuing without it", row_id)
        return None


def process_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Fetch, score, and persist one claimed item. Never raises."""
    row_id = row["id"]
    try:
        pdp = fetch_pdp(row["url"], row["item_id"])
        resolve_keywords(conn, row_id, pdp)
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


def _copy_fetch(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Phase 1 of a copy job: fetch the current copy, score it, and persist it.

    Stores the full fetched record (so the generation phase can rebuild it for a
    projected score without re-fetching) alongside the display copy. Advances the
    row to ``gen_queued`` when it should auto-generate, else to ``fetched``.
    """
    row_id = row["id"]
    try:
        pdp = fetch_pdp(row["url"], row["item_id"])
        found = resolve_keywords(conn, row_id, pdp)
        current_score = score_pdp(pdp)
        current = {
            "title": pdp.title,
            "bullets": pdp.bullets,
            "description": pdp.description,
            "score": result_to_dict(current_score),
            # Full record for the generation phase to rebuild and re-score.
            "record": pdp.__dict__,
        }
        next_status = "gen_queued" if row["auto_generate"] else "fetched"
        copy_jobs.save_current_copy(
            conn, row_id, title=pdp.title, current=current,
            current_overall=current_score.overall, keywords=found, next_status=next_status,
        )
        log.info("Fetched current copy id=%s overall=%s next=%s",
                 row_id, current_score.overall, next_status)
    except FetchBlocked as e:
        log.warning("Copy fetch blocked id=%s: %s", row_id, e)
        copy_jobs.mark_copy_failed(conn, row_id, "blocked", str(e))
    except FetchError as e:
        log.warning("Copy fetch error id=%s: %s", row_id, e)
        copy_jobs.mark_copy_failed(conn, row_id, "error", str(e))
    except Exception as e:  # noqa: BLE001 - a bad item must not kill the worker
        log.exception("Unexpected error fetching copy id=%s", row_id)
        copy_jobs.mark_copy_failed(conn, row_id, "error", f"Unexpected error: {e}")


def _copy_generate(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Phase 2 of a copy job: generate new copy and score the projected result."""
    row_id = row["id"]
    try:
        current = json.loads(row["current_json"]) if row["current_json"] else {}
        base = PdpRecord(**current["record"])
        generated = copygen.generate_copy(base, base.target_keywords)
        # Project the score: swap in the new copy, keep the imagery/attribute
        # signals unchanged (copy edits don't change them), and re-score.
        projected = replace(
            base, title=generated.title, bullets=generated.bullets,
            description=generated.description,
        )
        proj_score = score_pdp(projected)
        new = {
            "title": generated.title,
            "bullets": generated.bullets,
            "description": generated.description,
            "score": result_to_dict(proj_score),
        }
        copy_jobs.save_generated_copy(conn, row_id, new=new, projected_overall=proj_score.overall)
        log.info("Generated copy id=%s projected_overall=%s", row_id, proj_score.overall)
    except copygen.CopyGenError as e:
        log.warning("Copy generation failed id=%s: %s", row_id, e)
        copy_jobs.mark_copy_failed(conn, row_id, "error", str(e))
    except Exception as e:  # noqa: BLE001 - a bad item must not kill the worker
        log.exception("Unexpected error generating copy id=%s", row_id)
        copy_jobs.mark_copy_failed(conn, row_id, "error", f"Unexpected error: {e}")


def process_copy_one(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Process one claimed copy row. Returns True if it did browser work.

    A ``fetching`` row runs the (slow, browser) fetch phase; a ``generating`` row
    runs the (network-only) AI generation phase. The return value lets the loop
    apply the Walmart-politeness delay only after a real fetch.
    """
    if row["status"] == "fetching":
        _copy_fetch(conn, row)
        return True
    _copy_generate(conn, row)
    return False


def main() -> None:
    """Claim-and-process loop. Runs until the process is stopped.

    Drains the scoring queue first, then the copy queue, then idles. Scoring is
    given priority simply because it's the older, higher-volume feature; both
    queues share this single worker (the droplet's RAM caps parallelism — see the
    handoff).
    """
    conn = connect()
    log.info("PDP worker started (scoring + copy)")
    while True:
        row = jobs.claim_next(conn)
        if row is not None:
            process_one(conn, row)
            time.sleep(random.uniform(*FETCH_DELAY_RANGE_S))
            continue

        copy_row = copy_jobs.claim_next_copy(conn)
        if copy_row is not None:
            did_fetch = process_copy_one(conn, copy_row)
            if did_fetch:
                time.sleep(random.uniform(*FETCH_DELAY_RANGE_S))
            continue

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
