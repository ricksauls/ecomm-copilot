"""Background PDP scoring worker.

Runs as its own process (systemd: ecomm-copilot-worker) so the slow browser work
never touches a web request. Polls the scoring and copy queues and processes up to
``SCORING_CONCURRENCY`` items at once (default 3) — a thread pool *inside this one
process*, each thread doing its own headed-Chrome fetch. Scoring drains first,
then copy; the two never run at the same time, so they share one memory budget.
Competitive-Intelligence runs stay serial and run between waves. It must run with
a display available (DISPLAY=:99 via the droplet's Xvfb) because headed Chrome
evades Walmart's bot defense.

Concurrency is capped at 10 because each concurrent fetch is a full browser:
on the ~2 GB shared droplet even 3 is memory-significant, so raise
``SCORING_CONCURRENCY`` only after giving the box more RAM.

Run locally: ``python worker.py`` (needs Playwright + a browser + DISPLAY).
"""

import json
import logging
import os
import random
import sqlite3
import threading
import time

from dotenv import load_dotenv

# Load DATABASE_URL (and anything else) before importing app modules that read it.
load_dotenv()

from dataclasses import asdict, replace  # noqa: E402
from datetime import datetime  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from app import (  # noqa: E402  (after load_dotenv is intentional)
    ci_config, ci_images, ci_jobs, ci_scraper, copy_jobs, copygen, db,
    image_enhance, image_jobs, jobs, keywords,
)
from app.imageset import generate as imageset_generate  # noqa: E402
from app.imageset import jobs as imageset_jobs  # noqa: E402
from app.imageset import plan as imageset_plan  # noqa: E402
from app.imageset import store as imageset_store  # noqa: E402
from app.fetch import FetchBlocked, FetchError, fetch_main_image_url, fetch_pdp  # noqa: E402
from app.scoring import PdpRecord, result_to_dict, score_pdp  # noqa: E402

# Scrape dates are stamped in Central time so a monitoring run near midnight lands
# on the day the user expects (the droplet clock is UTC).
CST = ZoneInfo("America/Chicago")

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("worker")

# How often to poll when the queue is empty, and the polite delay between
# fetches so we don't hammer Walmart (mirrors the WM scraper's cadence). The
# delay is now per *thread*: each pool thread waits it out before claiming again.
POLL_INTERVAL_S = 5
FETCH_DELAY_RANGE_S = (8, 16)

# Scoring concurrency: how many items score at once (each in its own thread +
# headed Chrome). Capped at 10 — a full browser per concurrent fetch is heavy on
# the shared ~2 GB droplet; raise only after adding RAM. Default 3.
_MAX_CONCURRENCY = 10
_DEFAULT_CONCURRENCY = 3

# Stagger each pool thread's first claim so N browsers don't launch — and hit
# Walmart — at the same instant. (thread i waits up to i × this before starting;
# the per-item FETCH_DELAY_RANGE_S paces each thread thereafter.)
SUBMIT_STAGGER_S = (1.0, 4.0)


def resolve_concurrency(raw: str | None) -> int:
    """Parse ``SCORING_CONCURRENCY`` into a sane 1..10 int (default 3).

    Garbage or out-of-range values clamp rather than crash the worker: a typo in
    the droplet ``.env`` must not take scoring down, and an over-eager value must
    not OOM the box.
    """
    if raw is None or raw.strip() == "":
        return _DEFAULT_CONCURRENCY
    try:
        n = int(raw)
    except ValueError:
        log.warning("Invalid SCORING_CONCURRENCY=%r; falling back to %d",
                    raw, _DEFAULT_CONCURRENCY)
        return _DEFAULT_CONCURRENCY
    clamped = max(1, min(n, _MAX_CONCURRENCY))
    if clamped != n:
        log.warning("SCORING_CONCURRENCY=%d out of range; clamped to %d", n, clamped)
    return clamped


SCORING_CONCURRENCY = resolve_concurrency(os.environ.get("SCORING_CONCURRENCY"))


def connect(*, ensure: bool = True) -> sqlite3.Connection:
    """Open a worker SQLite connection (separate from the Flask app's).

    Applies the shared connection pragmas (FK enforcement, busy timeout, WAL) via
    :func:`db.tune_connection` — WAL and the busy timeout are what let the pool's
    threads and the web app write concurrently without "database is locked".

    ``ensure`` controls the one-time schema guard. The main loop opens with
    ``ensure=True`` so the schema exists before any work — the worker never
    depends on the web app having initialized the DB first (on a deploy that adds
    a table, the worker can restart ahead of the web app and would otherwise crash
    on the missing table). Pool threads pass ``ensure=False``: the schema is
    already guaranteed, so re-running it on every thread is wasted work.
    """
    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    db.tune_connection(conn)
    if ensure:
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


def _cache_item_image(item_id: str | None, image_url: str | None) -> None:
    """Best-effort: cache a scored/copy item's main image for the dashboard tables.

    Reuses the item-id-keyed product-image cache shared with Competitive
    Intelligence, so an item scored here and tracked in a CI group share one file.
    Skips when there's no item id/URL or the image is already cached; never raises
    — a missing thumbnail must not fail the scoring/copy job that produced it.
    """
    if not item_id or not image_url or ci_images.has_product_image(item_id):
        return
    try:
        ci_images.cache_product_image_from_url(item_id, image_url)
    except Exception:  # noqa: BLE001 - image caching must never fail the job
        log.exception("Failed to cache item image item_id=%s", item_id)


def process_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Fetch, score, and persist one claimed item. Never raises."""
    row_id = row["id"]
    try:
        pdp = fetch_pdp(row["url"], row["item_id"])
        resolve_keywords(conn, row_id, pdp)
        result = score_pdp(pdp)
        # Persist the full record too, so the copy cross-link can reuse this fetch
        # instead of re-fetching the PDP (resolve_keywords has already set
        # pdp.target_keywords, which the copy generation phase needs).
        jobs.save_result(conn, row_id, result.overall, result_to_dict(result),
                         pdp.title, brand=pdp.brand, record=asdict(pdp))
        _cache_item_image(row["item_id"], pdp.main_image_url)
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
            brand=pdp.brand,
        )
        _cache_item_image(row["item_id"], pdp.main_image_url)
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


def _cache_ci_product_images(run_id: int, item_map: dict) -> None:
    """Best-effort: cache the main image for each tracked product missing one.

    One headed-Chrome PDP fetch per *uncached* product to discover the image URL,
    then a plain download of the bytes. Images rarely change, so a product is
    fetched once and skipped on every later run. Entirely non-fatal — an image
    that won't fetch just leaves that product without a thumbnail; the keyword
    sweep (the actual point of the run) is never affected.
    """
    todo = [(iid, p) for iid, p in item_map.items() if not ci_images.has_product_image(iid)]
    if not todo:
        return
    log.info("CI run id=%s caching %d product image(s)", run_id, len(todo))
    for i, (item_id, product) in enumerate(todo):
        try:
            img_url = fetch_main_image_url(product["walmart_url"], item_id)
            if img_url:
                ci_images.cache_product_image_from_url(item_id, img_url)
            else:
                log.info("CI run id=%s no main image for item_id=%s", run_id, item_id)
        except (FetchBlocked, FetchError) as e:
            log.warning("CI run id=%s product image fetch failed item_id=%s: %s",
                        run_id, item_id, e)
        except Exception:  # noqa: BLE001 - image caching must never sink the run
            log.exception("CI run id=%s unexpected error caching image item_id=%s",
                          run_id, item_id)
        # Polite pause between PDP fetches (same courtesy as the keyword sweep).
        if i < len(todo) - 1:
            time.sleep(random.uniform(*ci_scraper.INTER_KEYWORD_DELAY_S))


def _write_keyword_ads(conn, ads, *, run_id, group_id, keyword_id, brand_map, scrape_date):
    """Save each captured ad creative, then record the sightings. Best-effort.

    ``ads`` are ``{ad_type, brand_text, image_bytes}`` from the scrape. The image is
    stored (its relative path goes on the row); the pure builder attributes each ad
    to a tracked brand by name. Never raises — ad capture is a bonus on top of the
    keyword's share/ranking data, which is already committed by this point.
    """
    if not ads:
        return
    try:
        for ad in ads:
            rel = None
            if ad.get("image_bytes"):
                rel = ci_images.save_ad_image(run_id, keyword_id, ad["ad_type"],
                                              ad["image_bytes"])
            ad["image_path"] = rel
        rows = ci_scraper.build_ad_rows(
            ads, run_id=run_id, group_id=group_id, keyword_id=keyword_id,
            brand_map=brand_map, scrape_date=scrape_date,
        )
        ci_jobs.write_ad_units(conn, rows)
    except Exception:  # noqa: BLE001 - never let ad bookkeeping sink the run
        log.exception("CI run id=%s failed writing ad units keyword_id=%s",
                      run_id, keyword_id)


def process_ci_run(conn: sqlite3.Connection, run: sqlite3.Row) -> None:
    """Scrape every active keyword for a claimed Competitive Intelligence run.

    Each keyword is scraped in its own browser (anti-detection) with a polite
    delay between keywords. A per-keyword failure (block/layout change) is logged
    and skipped so one bad keyword doesn't sink the whole sweep; the run is marked
    ``done`` if any keyword succeeded, else ``error``. Never raises.
    """
    run_id = run["id"]
    group_id = run["group_id"]
    slot = run["slot"]
    try:
        keyword_rows, item_map, brand_map = ci_config.load_group_config(conn, group_id)
        if not keyword_rows:
            log.info("CI run id=%s group=%s has no active keywords — nothing to scrape",
                     run_id, group_id)
            ci_jobs.finish_run(conn, run_id)
            return

        scrape_date = datetime.now(CST).date().isoformat()
        seen = ci_jobs.seen_item_ids_by_brand(conn, group_id)
        log.info("CI run id=%s group=%s starting — %d keyword(s)",
                 run_id, group_id, len(keyword_rows))

        # Cache each tracked product's main image (once) for the results page + PDF.
        _cache_ci_product_images(run_id, item_map)

        succeeded = failed = 0
        for i, kw in enumerate(keyword_rows):
            try:
                page = ci_scraper.scrape_keyword_page(kw["keyword"])
                cards = page["cards"]
                rows = ci_scraper.build_result_rows(
                    cards, run_id=run_id, group_id=group_id, keyword_id=kw["id"],
                    item_map=item_map, brand_map=brand_map,
                    seen_ids_by_brand=seen, scrape_date=scrape_date,
                )
                ci_jobs.write_search_results(conn, rows)
                # Share of shelf is tracked-item-only; item_map keys are the group's
                # tracked walmart_item_ids.
                ci_jobs.write_share_of_search(conn, run_id, group_id, kw["id"],
                                              scrape_date, slot, rows, set(item_map))
                # Brand ad units (headline / sponsored video) captured on the same
                # page load: save each creative screenshot, then record the sighting.
                _write_keyword_ads(conn, page["ads"], run_id=run_id, group_id=group_id,
                                   keyword_id=kw["id"], brand_map=brand_map,
                                   scrape_date=scrape_date)
                succeeded += 1
                log.info("CI run id=%s keyword=%r ok — %d cards, %d ad(s)",
                         run_id, kw["keyword"], len(rows), len(page["ads"]))
            except (FetchBlocked, FetchError) as e:
                failed += 1
                log.warning("CI run id=%s keyword=%r failed: %s", run_id, kw["keyword"], e)
            except Exception:  # noqa: BLE001 - one bad keyword must not kill the run
                failed += 1
                log.exception("CI run id=%s keyword=%r unexpected error", run_id, kw["keyword"])
            # Polite pause before the next keyword (skip after the last one).
            if i < len(keyword_rows) - 1:
                time.sleep(random.uniform(*ci_scraper.INTER_KEYWORD_DELAY_S))

        if succeeded == 0:
            ci_jobs.fail_run(conn, run_id, f"All {failed} keyword(s) failed to scrape")
        else:
            ci_jobs.finish_run(conn, run_id)
            log.info("CI run id=%s complete — %d ok, %d failed", run_id, succeeded, failed)
    except Exception as e:  # noqa: BLE001 - a bad run must not kill the worker
        log.exception("Unexpected error on CI run id=%s", run_id)
        ci_jobs.fail_run(conn, run_id, f"Unexpected error: {e}")


def drain_scoring() -> int:
    """Score every queued item concurrently; return how many were processed.

    Spins up to ``SCORING_CONCURRENCY`` threads. Each opens its own DB connection
    and loops — atomically claim the next queued item, fetch + score it, then pause
    for Walmart politeness — until the queue drains, at which point the thread
    exits. Blocks until all threads finish, so the caller resumes single-threaded
    for copy / CI work. Call only when there's queued work (see
    :func:`jobs.has_queued_items`) so the pool isn't spun up on idle polls.

    Threads are daemons: on shutdown (a systemd stop or deploy restart) the process
    can exit without blocking on an in-flight fetch; whatever was mid-flight is
    reclaimed on the next startup (:func:`jobs.reclaim_orphaned_items`), which is
    correct precisely because this is a single process (see that function's note).
    """
    processed = 0
    counter_lock = threading.Lock()

    def claim_loop(stagger: float) -> None:
        nonlocal processed
        # Each thread must use its own connection — SQLite connections are not
        # safe to share across threads, and separate connections are how the
        # busy-timeout/WAL contention handling actually kicks in.
        conn = connect(ensure=False)
        try:
            if stagger:
                time.sleep(stagger)
            while True:
                row = jobs.claim_next(conn)
                if row is None:
                    return  # queue drained — this thread is done
                process_one(conn, row)
                with counter_lock:
                    processed += 1
                # Politeness pause before this thread claims its next item.
                time.sleep(random.uniform(*FETCH_DELAY_RANGE_S))
        finally:
            conn.close()

    threads = [
        threading.Thread(
            target=claim_loop,
            # Ramp launches apart: thread i waits up to i × the stagger window.
            args=(random.uniform(*SUBMIT_STAGGER_S) * i,),
            name=f"score-{i}",
            daemon=True,
        )
        for i in range(SCORING_CONCURRENCY)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return processed


def drain_copy() -> int:
    """Process every claimable copy item concurrently; return phases processed.

    Same single-process thread pool as :func:`drain_scoring` (bounded by
    ``SCORING_CONCURRENCY`` — scoring and copy never drain at the same time, so
    they share the same memory budget). Each thread claims copy rows
    (``claim_next_copy`` — a row's fetch phase, then its generate phase) until the
    queue drains. The Walmart-politeness pause applies only after a real fetch
    phase (``process_copy_one`` returns ``did_fetch``); the AI generation phase
    needs no pause.
    """
    processed = 0
    counter_lock = threading.Lock()

    def claim_loop(stagger: float) -> None:
        nonlocal processed
        conn = connect(ensure=False)
        try:
            if stagger:
                time.sleep(stagger)
            while True:
                row = copy_jobs.claim_next_copy(conn)
                if row is None:
                    return
                did_fetch = process_copy_one(conn, row)
                with counter_lock:
                    processed += 1
                if did_fetch:
                    time.sleep(random.uniform(*FETCH_DELAY_RANGE_S))
        finally:
            conn.close()

    threads = [
        threading.Thread(
            target=claim_loop,
            args=(random.uniform(*SUBMIT_STAGGER_S) * i,),
            name=f"copy-{i}",
            daemon=True,
        )
        for i in range(SCORING_CONCURRENCY)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return processed


def process_image_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Process one claimed image-fix row: enhance the source, cache it, mark done.

    The upscaling provider (Claid) is a plain HTTP call — no browser, no Xvfb, no
    Walmart politeness delay — so this is cheap and RAM-light next to a scoring
    fetch. ``source_url`` was resolved from our own stored scrape at enqueue time
    (SSRF-safe). The output is written to the shared enhanced-image cache keyed by
    (scored_item_id, slot); the web app serves it same-origin from there.
    """
    row_id = row["id"]
    sid, slot = row["scored_item_id"], row["slot"]
    try:
        data = image_enhance.enhance(row["source_url"], operation=row["operation"])
        saved = ci_images.save_enhanced_image(sid, slot, data, row["ext"])
        if not saved:
            # The provider returned bytes but we couldn't cache them (disk/decode);
            # surface it so the user retries rather than seeing a stuck spinner.
            image_jobs.mark_image_failed(conn, row_id, "Could not store the enhanced image.")
            return
        image_jobs.mark_image_done(conn, row_id)
        log.info("Enhanced image job id=%s item=%s slot=%s op=%s", row_id, sid, slot, row["operation"])
    except image_enhance.EnhanceNotConfigured as e:
        # Key removed after the job was queued — a config state, not a provider
        # failure. Fail loud so the row doesn't hang; no secret is in the message.
        log.warning("Image job id=%s not processed — enhancement not configured: %s", row_id, e)
        image_jobs.mark_image_failed(conn, row_id, "Image enhancement is not configured.")
    except image_enhance.EnhanceError as e:
        log.warning("Image job id=%s failed item=%s slot=%s: %s", row_id, sid, slot, e)
        image_jobs.mark_image_failed(conn, row_id, "Image enhancement failed — try again shortly.")
    except Exception as e:  # noqa: BLE001 - a bad item must not kill the worker
        log.exception("Unexpected error enhancing image job id=%s", row_id)
        image_jobs.mark_image_failed(conn, row_id, f"Unexpected error: {e}")


def drain_image_jobs() -> int:
    """Process every queued image fix concurrently; return the count processed.

    Same single-process thread pool as :func:`drain_copy` (bounded by
    ``SCORING_CONCURRENCY``), minus the Walmart-politeness pause — these are
    provider HTTP calls, not browser fetches. Runs only when scoring and copy are
    idle, so it never competes with them for the memory budget. Each thread claims
    rows (``claim_next_image_job``) until the queue drains.
    """
    processed = 0
    counter_lock = threading.Lock()

    def claim_loop(stagger: float) -> None:
        nonlocal processed
        conn = connect(ensure=False)
        try:
            if stagger:
                time.sleep(stagger)
            while True:
                row = image_jobs.claim_next_image_job(conn)
                if row is None:
                    return  # queue drained — this thread is done
                process_image_one(conn, row)
                with counter_lock:
                    processed += 1
        finally:
            conn.close()

    threads = [
        threading.Thread(
            target=claim_loop,
            args=(random.uniform(*SUBMIT_STAGGER_S) * i,),
            name=f"image-{i}",
            daemon=True,
        )
        for i in range(SCORING_CONCURRENCY)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return processed


def process_imageset_one(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """Process one claimed image-set asset job: generate the asset, mark it done.

    Like the image-fix path these are provider HTTP calls + local compositing — no
    browser, no Xvfb, no Walmart politeness delay. Loads the asset + project
    (scoped to the job's user), runs the generation orchestrator, and marks the job
    done. Any failure marks both the job and the asset failed so the gallery can
    offer a retry, and never raises (a bad asset must not kill the worker).
    """
    row_id = row["id"]
    uid, pid, aid = row["user_id"], row["project_id"], row["asset_id"]
    try:
        asset = imageset_store.get_asset(conn, aid, uid)
        project = imageset_store.get_project(conn, pid, uid)
        if asset is None or project is None:
            # The asset/project was deleted after the job was queued — nothing to do.
            imageset_jobs.mark_failed(conn, row_id, "The asset or project no longer exists.")
            return
        imageset_generate.process_asset(conn, asset, project)
        imageset_jobs.mark_done(conn, row_id)
        log.info("Generated image-set asset job id=%s project=%s asset=%s", row_id, pid, aid)
    except imageset_generate.GenerationError as e:
        log.warning("Image-set asset job id=%s failed project=%s asset=%s: %s", row_id, pid, aid, e)
        imageset_jobs.mark_failed(conn, row_id, str(e))
        imageset_store.update_asset(conn, aid, status="failed", error=str(e)[:500])
    except Exception as e:  # noqa: BLE001 - a bad asset must not kill the worker
        log.exception("Unexpected error generating image-set asset job id=%s", row_id)
        imageset_jobs.mark_failed(conn, row_id, f"Unexpected error: {e}")
        imageset_store.update_asset(conn, aid, status="failed", error=f"Unexpected error: {e}"[:500])


def drain_imageset_jobs() -> int:
    """Process every queued image-set asset job concurrently; return the count.

    Same single-process thread pool as :func:`drain_image_jobs` (bounded by
    ``SCORING_CONCURRENCY``), no Walmart-politeness pause — generation is provider
    HTTP + local Pillow compositing. Runs only when scoring/copy/image-fix are
    idle, so it never competes with them for the memory budget.
    """
    processed = 0
    counter_lock = threading.Lock()

    def claim_loop(stagger: float) -> None:
        nonlocal processed
        conn = connect(ensure=False)
        try:
            if stagger:
                time.sleep(stagger)
            while True:
                row = imageset_jobs.claim_next_job(conn)
                if row is None:
                    return  # queue drained — this thread is done
                process_imageset_one(conn, row)
                with counter_lock:
                    processed += 1
        finally:
            conn.close()

    threads = [
        threading.Thread(
            target=claim_loop,
            args=(random.uniform(*SUBMIT_STAGGER_S) * i,),
            name=f"imageset-{i}",
            daemon=True,
        )
        for i in range(SCORING_CONCURRENCY)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return processed


def process_imageset_fetch_one(conn: sqlite3.Connection, project: sqlite3.Row) -> None:
    """Prefill one claimed image-set draft from its Walmart product URL.

    Browser work (headed Chrome via ``fetch_pdp``), so it runs with the worker's
    DISPLAY like scoring/copy — never in the web process. A fetch failure is
    non-fatal: the draft drops back to an editable state with a note so the user
    can fill it in manually. Never raises (a bad URL must not kill the worker).
    """
    pid = project["id"]
    try:
        imageset_generate.run_prefill(conn, project)
        imageset_store.finish_fetch(conn, pid)
        log.info("Prefilled image-set draft project=%s", pid)
    except FetchBlocked:
        imageset_store.fail_fetch(
            conn, pid, "Walmart blocked the fetch — please fill in the product details manually.")
    except FetchError:
        imageset_store.fail_fetch(
            conn, pid, "Couldn't read that product page — please fill in the details manually.")
    except imageset_generate.GenerationError as e:
        imageset_store.fail_fetch(conn, pid, str(e))
    except Exception as e:  # noqa: BLE001 - a bad prefill must not kill the worker
        log.exception("Unexpected error prefilling image-set draft project=%s", pid)
        imageset_store.fail_fetch(conn, pid, f"Unexpected error: {e}")


def process_imageset_plan_one(conn: sqlite3.Connection, project: sqlite3.Row) -> None:
    """Build one claimed project's creative plan, create its assets, and enqueue them.

    Planning is a slow Claude call, so it runs here on the worker rather than in the
    web request (which would time out). ``generate_plan`` falls back to the built-in
    plan if Claude fails, so this rarely errors; any unexpected failure marks the
    project failed so the user can re-approve. Never raises.
    """
    pid, uid = project["id"], project["user_id"]
    try:
        plan = imageset_plan.generate_plan(imageset_store.plan_context(conn, project))
        imageset_store.save_plan_and_create_assets(conn, pid, uid, plan)
        imageset_generate.enqueue_project_assets(
            conn, imageset_store.get_project(conn, pid, uid), uid)
        log.info("Planned image-set project=%s and enqueued its assets", pid)
    except Exception as e:  # noqa: BLE001 - a bad plan must not kill the worker
        log.exception("Unexpected error planning image-set project=%s", pid)
        imageset_store.set_status(
            conn, pid, imageset_store.STATUS_FAILED,
            error=f"Could not plan the image set: {e}"[:500])


def main() -> None:
    """Claim-and-process loop. Runs until the process is stopped.

    Drains the scoring queue first, then the copy queue, each concurrently (see
    :func:`drain_scoring` / :func:`drain_copy`), then Competitive Intelligence
    runs, then idles. Scoring takes priority; the two queues never drain at once,
    so their pools share one memory budget. CI stays serial — a run is a whole
    keyword sweep that does its own inter-keyword pacing.
    """
    conn = connect()
    log.info("PDP worker started — scoring concurrency=%d (+ copy + competitive intelligence)",
             SCORING_CONCURRENCY)
    # A previous worker may have died mid-flight (a deploy restart or an OOM kill on
    # the ~2 GB droplet). Reclaim anything it left stuck in an in-flight status so a
    # scoring/copy item doesn't flash in-progress forever and a CI run doesn't block
    # its group's monitoring schedule. All three queues self-heal on startup.
    reclaimed_items = jobs.reclaim_orphaned_items(conn)
    reclaimed_copy = copy_jobs.reclaim_orphaned_copy_items(conn)
    reclaimed_runs = ci_jobs.reclaim_orphaned_runs(conn)
    reclaimed_images = image_jobs.reclaim_orphaned_image_jobs(conn)
    reclaimed_sets = imageset_jobs.reclaim_orphaned_jobs(conn)
    reclaimed_fetches = imageset_store.reclaim_orphaned_fetches(conn)
    reclaimed_plans = imageset_store.reclaim_orphaned_plans(conn)
    if (reclaimed_items or reclaimed_copy or reclaimed_runs or reclaimed_images
            or reclaimed_sets or reclaimed_fetches or reclaimed_plans):
        log.warning(
            "Startup: reclaimed %d scoring item(s), %d copy item(s), %d CI run(s), "
            "%d image job(s), %d image-set job(s), %d image-set prefill(s), %d plan(s)",
            reclaimed_items, reclaimed_copy, reclaimed_runs, reclaimed_images,
            reclaimed_sets, reclaimed_fetches, reclaimed_plans,
        )
    while True:
        # Scoring first and concurrently: drain the whole queue with the thread
        # pool, then fall through to the serial copy / CI work.
        if jobs.has_queued_items(conn):
            n = drain_scoring()
            log.info("Scoring wave complete — processed %d item(s)", n)
            continue

        # Copy next, also concurrently: drain the fetch/generate queue with the
        # pool (runs only when scoring is idle, so the memory budget is shared).
        if copy_jobs.has_claimable_items(conn):
            n = drain_copy()
            log.info("Copy wave complete — processed %d phase(s)", n)
            continue

        # Image-set prefill next — browser work (headed Chrome) like scoring/copy,
        # so it's grouped with them and ahead of the light HTTP/compositing queues.
        # Processed one project per loop so multiple prefills are naturally paced.
        if imageset_store.has_claimable_fetch(conn):
            fetch_project = imageset_store.claim_next_fetch(conn)
            if fetch_project is not None:
                process_imageset_fetch_one(conn, fetch_project)
                continue

        # Image-set planning next — a slow Claude call moved off the web request.
        # One project per loop; it creates the assets and enqueues them, which the
        # image-set generation drain below then processes concurrently.
        if imageset_store.has_claimable_plan(conn):
            plan_project = imageset_store.claim_next_plan(conn)
            if plan_project is not None:
                process_imageset_plan_one(conn, plan_project)
                continue

        # Image fixes next — the lightest work (provider HTTP calls, no browser),
        # so clear them before the serial CI sweep. Runs only when scoring/copy are
        # idle, so it never competes with them for RAM.
        if image_jobs.has_claimable_image_jobs(conn):
            n = drain_image_jobs()
            log.info("Image-fix wave complete — processed %d job(s)", n)
            continue

        # Image-set asset generation next — also light (provider HTTP + local
        # compositing, no browser). Drained before the serial CI sweep, only when
        # the heavier queues are idle so it never competes with them for RAM.
        if imageset_jobs.has_claimable_jobs(conn):
            n = drain_imageset_jobs()
            log.info("Image-set wave complete — processed %d asset(s)", n)
            continue

        ci_run = ci_jobs.claim_next_run(conn)
        if ci_run is not None:
            process_ci_run(conn, ci_run)
            continue

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
