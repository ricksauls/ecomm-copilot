# DISCOtech (ecomm-copilot) — Session Handoff

_Last updated: 2026-10-07 (session 12 — left-rail menu restructure + collapsible icon/tree-line rail redesign; breadcrumbs aligned to "<main group> · <sub-item>"; View Scoring History grouped by run ("Brands/Products Scored", up to 3 items + "And N more…"); Available Credits hidden; bot account confirmed removed. Session 11 — batch copy rewrites; cost modal generalized; wording/UI polish; signup honeypot; "Re-export"→"Re-create" data fix)._

> **Session 12 — View Scoring History grouped by run (2026-10-07).** The scoring
> history (`/app/activity/scored`) now shows **one row per scoring action**
> (`batch_id`), retitled **"Brands/Products Scored"**: each run lists up to three
> of its items (Brand + Product columns kept, stacked; Score on the right) with an
> **"And N more…"** line under Product, and the row reopens the whole run
> (`pdp_scoring_item`). History screen only — the dashboard's monthly scored table
> is unchanged. Code: `pages._scored_runs` + a `kind == "scored"` branch in
> `activity_all` → new `activity_scored_runs.html` + `_dash_tables.scored_runs_table`
> macro (`.dash-row-runs`/`.run-stack` CSS); `jobs.list_scored_activity` now also
> selects `batch_id`. **358 tests**, `ruff` clean.

> **Session 12 rail redesign (2026-10-07) — collapsible icon menu with tree
> lines.** The rail was reworked to a collapsible design (reference-matched):
> each main option has an **outline icon**; the five groups (Scoring, Copy,
> Creative, Reporting, Admin) are native **`<details>`** with a CSS-drawn **+/−**
> toggle, **collapsed by default**, revealing children joined by **rounded
> tree-connector lines** (trunk + elbow, `--rail-line` color token on `.rail`);
> Dashboard + Contact Us are leaf links (no toggle). The group holding the current
> page auto-opens (`active` slug ∈ group), and **`app/static/js/rail.js`**
> (external, CSP-safe, loaded in `base.html`) remembers manual open/close per tab
> via sessionStorage. Icons are inline SVG via a `rail_icon()` Jinja macro; groups
> via a `rail_group()` macro. Replaced the prior `.nav-item*`/`.rail-group-label`/
> `.rail-section` rail CSS with `.rail-leaf`/`.rail-group-head`/`.rail-ico`/
> `.rail-toggle`/`.rail-children`/`.nav-sub-item` (old classes removed as dead).
> Browser-verified (collapsed default, auto-open, +/− toggle, tree lines, JS
> persistence); **357 tests**, `ruff` clean.


> **Session 12 (2026-10-07) — left-rail navigation restructure.** The rail
> (`app/templates/app/_rail.html`) now has six top-level options in order:
> **Dashboard**; **Product Detail Page Content Scoring** (→ Score Product Detail
> Page(s) = scoring intake, View Scoring History = `/app/activity/scored`); **Copy
> Content Studio** (→ PDP Copy Content Creation, View Copy Content Creation History
> = `/app/activity/copy`); **Creative Content Studio** (→ PDP Creative Content
> Creation = placeholder `#`, not built; View Creative Content Creation History =
> `/app/activity/images`); **Reporting · Insights · Analytics** (the former
> Competitive Intelligence section, same three items); **Contact Us** (moved last).
> The three Studio groups + Reporting use the labelled-section pattern (new generic
> `.rail-section` CSS class). **Available Credits is hidden** (block removed from the
> rail). `activity_all` now sets `active_nav` per kind (`_ACTIVITY_ACTIVE_NAV`) so
> the history sub-items highlight. The old combined **View All Content Activity**
> page (`/app/content-activity`) still works by URL but is no longer linked in the
> nav. **Rail typography/alignment** (follow-up in the same session): main options
> are the larger/brighter tier (14px semibold, new `.rail-group-label`) and
> sub-items the smaller muted tier (12px); Dashboard + Contact Us sit flush with
> the group labels on one left edge (`.nav-item-flush`, mark dropped, active shown
> as a flush inset accent stripe). **Admin section unified** with the rest of the
> rail (same `.rail-section` + `.rail-group-label` as the content groups; the small
> uppercase eyebrow `.rail-section-label` and the legacy `.rail-studio`/`.rail-ci`/
> `.rail-admin` classes were removed as dead). **Signed-in account block moved** to
> sit directly above Sign Out (Sign Out top margin 66px→14px so they read as one
> bottom cluster). **Rail spacing tweaks:** extra space above Contact Us
> (`.rail-contact`, 30px) to set it off from the content groups, and a 28px top
> margin on the account block so the signed-in user name is clearly separated from
> the Admin section (and the rail body for non-admins). Browser-verified; **357
> tests**, `ruff` clean.


A working reference for picking up development. Read this first, then
`CLAUDE.md` (coding standards) and `deploy/DEPLOY.md` (infra).

> **Next session — start here (last worked 2026-10-07, session 11).**
> **Everything below is SHIPPED + DEPLOYED and verified live.** `main` at **273e5bb**;
> last Deploy green; site 200; both services active. Tests **357 passing**, `ruff` +
> `pip-audit` clean. No open/half-built work; tree clean.
>
> **What session 11 shipped (newest first, with commits):**
> 1. **Signup honeypot** (`273e5bb`) — a bot registered via the public signup form
>    (`etdhogdk@formtests.info`), so signup now has a hidden decoy `website` field
>    (off-screen, not focusable, aria-hidden, autocomplete off). The server rejects a
>    non-empty honeypot before creating any account (generic 400 + a WARNING log with
>    ip/ua, no secrets). See `app/auth.py` + `signup.html` + `.hp-field` in
>    `public.css`.
> 2. **Flagged-image lines de-reddened** (`a5c406e`) — the `.pdp-img-issues` lines now
>    use the regular `--mid-gray` (they were signal-red, which read as errors).
>    Genuine error states (Enhancement failed, failed badges, blocked/error notes,
>    error status, "needs work" count) are still red by design.
> 3. **Green fix-and-download labels + ZIP rename + indented fix lines** (`984f417`) —
>    per-item ZIP "Download All As ZIP" → **"Fix And Download All As Zip"**; both it
>    and the per-image **"Fix and Download"** link are green (new `--signal-green`
>    token; `a.pdp-enhance-link` specificity beats the red `.pdp-img-issues a`); the
>    image-fix lines are indented with a left hairline rule to set them apart from the
>    generic recommendations.
> 4. **Wording/UI polish** (`93d192f`) — batch button **"Fix All Flagged In Batch" →
>    "Fix Images For All Items"** (+ modal title); expanded-row finished fix shows a
>    download link only, **no inline thumbnail** (`.pdp-enhance-thumb` removed);
>    per-image **"Download" → "Fix and Download"**; scorer recs **"Re-export …" →
>    "Re-create …"**. *(NB: historical session-10/11 text further below still shows the
>    OLD "Fix All Flagged In Batch" label — the live button is the new one.)*
> 5. **Batch copy rewrites** (`1d9c43e`) — the headline feature; full detail in
>    "## Session 2026-10-07 (session 11)" below. Scoring results page now has a unified
>    **"Bulk actions"** section with two sibling bars: **Image fixes** (session 10) and
>    **Copy rewrites** (new), both behind the shared cost-preflight modal.
>
> **One-time data fix (not a commit).** Recommendations are stored in
> `scored_items.result_json` at scoring time, so the 11 items scored before `93d192f`
> still said "Re-export". Normalized them on the droplet with a backed-up
> `UPDATE … REPLACE('Re-export','Re-create')` (0 remaining). The code produces
> "Re-create" for all new scores.
>
> **Open housekeeping (nothing blocking):**
> - **Bot account removed** — `etdhogdk@formtests.info` (was users.id 8) is no longer
>   in the prod DB (confirmed 2026-10-07: live `app.db` holds only ids 2/3/6/7). Done;
>   nothing further needed.
> - **DB backup on the droplet** — `app.db.bak-20261007-181537` (2.7M, from the
>   Re-export fix). Safe to `rm` once the user confirms results look right.
>
> **Likely next-ups (user's stated focus was "image fixes, edits, and creation"):**
> - **PDP Image Set Creation** — still a nav placeholder (the "creation" piece never
>   built). The natural next feature in the image track.
> - Carried-over from sessions 10/11: **cross-image-batch dedup** (a re-scored SKU's
>   image slots still pay per row), **per-item copy Retry** on the scoring page.
> - If signup spam continues past the honeypot: **email verification** or an
>   **allowlist** signup model (these are client tools, so an allowlist may fit).
>
> **Infra reminders:** optionally set `COPYGEN_PRICE_PER_ITEM` on the droplet to show
> a $ estimate for copy rewrites (blank = counts only); `IMAGE_UPSCALE_PRICE_PER_IMAGE`
> defaults to $0.04. The image-fix feature is ON (Claid key live). Deploy caution
> (worker restart orphans in-flight CI scrapes) is unchanged — see §2/§9.
>
> ---
>
> **Session 10 (still accurate, shipped + deployed).** **Batch image
> fixing is built and verified** (browser-tested on a seeded batch): the results
> table now has a **batch bar** to fix flagged images across the **whole batch** or
> just the **ticked items**, each gated by a **reusable cost-preflight modal** that
> shows the real count of *new* fixes and a dollar estimate before spending. Also
> shipped: **priority ordering** (white-bg fixes drain first), a **by-item batch ZIP
> + manifest.csv**, **Retry Failed** / **Cancel Queued**, per-row fix badges, and a
> JS-controlled auto-refresh so the modal can't be wiped mid-decision. The image-fix
> feature is **ON + verified live** (Claid key set on the droplet). Tests **338
> passing**, `ruff` + `pip-audit` clean.
>
> **SHIPPED + DEPLOYED (2026-10-07).** `main` at **7504be1**; Deploy workflow green;
> site 200; both services active. The per-fix price defaults to **$0.04** in code, so
> no droplet `.env` change was needed (set `IMAGE_UPSCALE_PRICE_PER_IMAGE` only to
> override).
>
> **Deploy hotfix landed (commit 7504be1) — read this.** The first deploy of session
> 10 (1110f72) **crashed the web service (502)**: the `image_jobs` claim-order index
> (`idx_image_jobs_claim`, on the new `priority` column) was defined in `db._SCHEMA`,
> which `ensure_schema` runs **before** `_migrate` adds the column. Fresh DBs were
> fine (CREATE TABLE includes `priority`) so tests/local passed, but the droplet's
> pre-existing `image_jobs` had no `priority` yet → `OperationalError: no such column:
> priority` on startup. Fixed by moving the index creation into `_migrate` (after the
> column is ensured) + a regression test (`test_db.test_migrate_adds_priority_to_
> preexisting_image_jobs`). **Lesson: never index a migrated-in column from `_SCHEMA`
> — it runs before `_migrate`.** Service was restored mid-incident with a manual
> additive `ALTER`/`CREATE INDEX` on the droplet, then the code hotfix deployed clean.
>
> **Possible next-ups (not started):** (1) **dedup across the batch** — a re-scored
> SKU in two rows pays twice today (cache is keyed per `scored_item_id`); dedup by
> item-id + source URL before enqueue (design decision #7 below; deliberately scoped
> OUT of session 10 to keep cost semantics simple). (2) per-item **Retry** on a
> failed single fix (batch Retry exists; per-item doesn't). (3) the older
> re-score / consistency items carried over from session 9 (below).
>
> **What session 9 shipped (all live on main + deployed):**
> 1. Image fixes are **async** on the background worker (new `image_jobs` queue,
>    mirrors `copy_jobs`). Click → enqueue → "Enhancing… ⏳" → the **fixed image shows
>    inline** when done (+ Download); the 5s meta-refresh carries it, no web-worker
>    tie-up. The original CDN image stays a link ("before").
> 2. **"Fix All Images For This Item" → one ZIP** (per item), JS-confirmed (metered),
>    cache-skip on already-fixed slots.
> 3. Main image shows **both its issues (resolution + white bg) on one line** with the
>    single combined fix; the standalone line is kept only for the white-bg-only case.
> 4. **Detail row stays open across reloads** (sessionStorage, keyed on a new per-row
>    `data-id`) — fixes the collapse under the auto-refresh + the fix-action redirect.
> 5. Old sync GET download routes replaced by **POST-enqueue** + inline/download serve
>    + ZIP routes.
>
> **Open items / decisions (nothing half-built; tree clean):**
> - **Batch image fixing** — the next-up design work; see the section below.
> - **Claid feature is ON + verified.** Key set in droplet `.env`; both `ecomm-copilot`
>   and `ecomm-copilot-worker` carry it (restart **both** to pick up any future key
>   change — session 9 moved the work to the worker). One real metered enhance was
>   confirmed working end-to-end on 2026-10-01. Claid is **metered per image**.
> - **Re-score older items:** items scored before ~02:04 CT 2026-10-02 lack
>   `white_bg_url` / `record_json`, so they won't show the combined main-image fix or
>   copy-reuse. Re-score (Score More, same URL) to populate them. Possible follow-up:
>   an in-place "Re-score" action so users don't re-paste URLs.
> - **Minor consistency:** CI group-config section *headings* still read "Run
>   snapshot" (lowercase — they're headings, not buttons); the CI snapshot/monitoring
>   screens still use the **old flashing subtitle** (only the two Content Studio
>   result screens were de-flashed). Both deferred pending user sign-off.
>
> **Still live from session 8** (unchanged): worker **concurrency** (WAL,
> `SCORING_CONCURRENCY=3` on the 8 GB droplet), the **results-table redesign**, copy
> **reuse + Excel/CSV exports**, and the **PDP → Product Detail Page** renaming. Full
> detail in the session-8 section below.
>
> **Deploy caution unchanged:** `deploy.yml` restarts the worker on every push,
> which orphans any in-flight CI scrape (marked `error`). Check for an active run
> before pushing while scrapes may be happening — see §2/§9. A docs-only push still
> deploys + restarts.

---

## Session 2026-10-07 (session 11)

**Batch copy rewrites, as a sibling to batch image fixes — and the cost modal made
truly reusable.** Prompted by the user noticing the copy and image flows felt
disconnected. **Browser-verified** on a seeded batch; **354 tests** (`ruff` +
`pip-audit` clean). **Local only — not yet committed/pushed.** No DB migration.

### What shipped
- **Unified "Bulk actions" section** (`pdp_results.html`): one card holding two
  sibling bars — **Image fixes** (unchanged from session 10) and **Copy rewrites**
  (new). Both act on the **same** row selection (the existing `item_ids` checkboxes)
  or the whole batch. The old header **"Create New Copy Content"** button is removed
  — copy entry now lives entirely in the bar.
- **Copy rewrites bar**: **Rewrite Copy For All Items** / **Rewrite Copy For Selected**
  (JS-driven, open the cost modal) + **View Copy Results** (cross-link) + a progress
  line (`N of M have new copy · K generating · J failed`). A `<noscript>` fallback
  submit keeps a no-JS path.
- **Cost modal generalized** (the reusable primitive the user asked for): the
  estimate endpoints now return **server-formatted display strings** — `summary`,
  `skipped`, `cost`, `note` — plus raw counts, and the modal just renders them. One
  modal serves image fixes **and** copy rewrites; a `data-confirm-label` sets the
  button text ("Confirm & Fix" / "Confirm & Rewrite"). `_cost_line()` + `_plural()`
  are the shared formatters. The image estimate was updated to the same shape (raw
  fields kept, so its tests are unchanged).
- **Dedup / cost-skip for copy** (`copy_jobs.copy_states_for_items`): a scored item
  is matched to its existing copy by **item id, else URL** (copy has no FK to
  scored_items). A batch rewrite **skips** items that already have a **done or
  in-flight** copy (never re-spends); an item whose only prior copy **failed** is
  eligible (a retry). Mirrors the image cache-skip. `_copy_batch_plan` is the single
  source of truth the estimate and the enqueue share; `reused` (carries
  `record_json` → generation skips the re-fetch) vs `refetch` is surfaced in the
  estimate.
- **Per-row copy badge** (`_annotate_copy` → `it['copy_state']`; **NB** the key is
  `copy_state`, not `copy` — `it.copy` in Jinja resolves to the dict's `.copy`
  method, a bug caught in testing): `✓ copy 64→88` (done, current→projected),
  `⏳ copy generating` (in-flight), `copy failed`. Hidden when the item has no copy.
- **View-copy cross-link** (`pdp_scoring_view_copy` + `copy_jobs.copy_item_ids_for_items`):
  points the copy session batch at all copy for the scored batch's products, then
  opens the copy results page.
- **Cost config** (`copygen.price_per_item`): `COPYGEN_PRICE_PER_ITEM`, USD per
  rewrite, **no default** (a rewrite's cost is token-variable + two-phase) — blank →
  counts only; set → `≈ $N` in the modal. In `.env.example` + DEPLOY.md.
- **Auto-refresh** now also stays alive while a copy rewrite is **generating**
  (`copy_pending`), reusing the pausable JS refresh from session 10.

### Deliberately kept different (not over-unified)
The **copy results stay on their dedicated comparison page** (side-by-side current
vs new text + score delta + Excel/CSV/PDF) — an image result is a thumbnail that
fits inline; a copy result is paragraphs that don't. So the *controls* unified,
the *results view* stayed specialized. And copy is **not provider-gated** (always
shown; generation needs `ANTHROPIC_API_KEY`, which the worker enforces), unlike the
image bar which gates on `IMAGE_UPSCALE_API_KEY`.

### Verified end to end (seeded batch)
Copy bar progress + per-row badges render; the copy modal showed "1 item will be
rewritten (1 re-fetch the page first)" + "2 already have copy — skipped" + counts-only
(no price set) + "Confirm & Rewrite"; confirming enqueued **only** the eligible item
and landed on the copy results page (the done + in-flight items were skipped). The
image modal still renders correctly through the shared path ("≈ $0.16 (4 × $0.04)").
No console errors.

### Tests (+16 → 354)
`test_copygen.py` (price contract), `test_copy_jobs.py` (copy_states matching by
id/URL + dedup buckets + IDOR; copy_item_ids), `test_pages.py` (copy bar visibility,
estimate counts/reused-refetch/skip/price/selected, enqueue all/selected/skip/
nothing-to-do, per-row badge, view-copy).

### Still open (next-ups)
Unchanged from session 10: cross-**image**-batch dedup (a re-scored SKU's image
slots still pay per row). New minor: per-item **Retry** on a single failed copy
(batch has none on the scoring page; the copy results page has its own re-run).

---

## Session 2026-10-07 (session 10)

**Batch image fixing (the full roadmap) + a reusable cost-preflight modal.** Built
on session 9's per-item async fixes. **Browser-verified** on a seeded batch; **337
tests** (`ruff` + `pip-audit` clean). **Local only — not yet committed/pushed.**

### What shipped
- **Batch action bar** (`pdp_results.html`, shown only when the feature's configured
  and the batch has flagged images): **"Fix All Flagged In Batch"** (whole batch) and
  **"Fix Images For Selected"** (reuses the table's existing `item_ids` checkboxes;
  enabled by JS only when ≥1 row is ticked), plus **Download Fixed Images (ZIP)**,
  **Retry Failed (N)**, **Cancel Queued (N)**, and a progress line (`done / total ·
  running · failed`). A compact per-row fix badge in the Status cell mirrors it.
- **Reusable cost-preflight modal** (`cost-modal` in the template + `pdp_results.js`
  + `.cost-modal*` CSS). A `[data-cost-action]` trigger carries `data-scope` /
  `data-estimate-url` / `data-action-url`; JS POSTs the scope to the estimate route,
  renders *"X white-bg fixes + Y upscales across Z items; N already fixed — skipped"*
  + *"≈ $NN (total × $price per fix)"*, and on confirm builds+submits the real POST.
  Kept action-agnostic so other metered features can reuse it. **Verified: a scope
  with 4 new fixes showed "≈ $0.16 (4 × $0.04)" and enqueued exactly those 4.**
- **Routes** (`pages.py`): `POST .../enhance-batch` (enqueue), `POST
  .../enhance-batch/estimate` (JSON counts+cost, no enqueue), `POST
  .../enhance-batch/retry-failed`, `POST .../enhance-batch/cancel`, `GET
  .../enhance-batch/download.zip`. `_enhance_batch_rows` resolves scope (`all=1` →
  session batch; else owned `item_ids`, IDOR-guarded). `_batch_enhance_plan` is the
  single source of truth the estimate **and** the enqueue both use (so they never
  diverge): it skips already-cached **and** already-in-flight slots, counting only
  *new* metered calls. SSRF guard unchanged (URLs come from our own scrape).
- **Priority ordering** (`image_jobs` + `db.py`): new additive `priority` column;
  `claim_next_image_job` orders `priority DESC, id`. Batch enqueue tags white-bg
  fixes priority 10, upscales 0 — an interrupted run delivers the hard Walmart
  main-image gate first. `cancel_queued_for_items` (deletes queued rows, leaves
  in-flight) and `requeue_failed_for_items` (error → queued) back the two buttons.
- **By-item batch ZIP + manifest** (`pages.pdp_scoring_enhance_batch_zip`): one folder
  per SKU (`item-<n>/`, suffixed by sid on collision), files named by position, plus
  a top-level `manifest.csv` (Item ID / Product Name / Original Image URL / Fixed
  File) so the user knows what to re-upload where.
- **Cost config** (`image_enhance.price_per_image`): reads
  `IMAGE_UPSCALE_PRICE_PER_IMAGE` — unset → default **$0.04** (Claid's per-action
  price), explicit empty → counts only, invalid/negative → default (logged). Estimate
  only; never meters/bills/blocks. In `.env.example` + DEPLOY.md.
- **Auto-refresh made pausable** (`pdp_results.js` + `head_extra`): the 5s results
  reload is now JS-driven (from a `<meta name="pdp-refresh-seconds">` marker) with a
  `<noscript>` meta-refresh fallback, so the cost modal pauses it and can't be wiped
  mid-decision. (This was a real bug caught in browser verification.)

### Tests (22 added → 337)
`test_image_enhance.py`: price contract (default/override/opt-out/bad-value).
`test_image_jobs.py`: priority claim order, cancel-queued (queued-only, IDOR),
retry-failed (errors-only). `test_pages.py`: batch bar visibility, estimate counts
(whole-batch / selected / cache-skip / in-flight-skip / $ math), enqueue (all +
selected + priority + 503 + nothing-selected + foreign-id IDOR), retry, cancel, and
the by-item ZIP + manifest (+ 404-when-empty).

### Deliberately scoped OUT (noted for next session)
Cross-batch **dedup** (design decision #7 below): a re-scored SKU in multiple rows
still pays per row, because the enhanced-image cache is keyed per `scored_item_id`.
Doing it right means paying once and fanning the result to every matching slot —
left out to keep session 10's cost semantics simple and reviewable.

### Shipped
Commits `1110f72` (feature) + `7504be1` (migration-order hotfix — see the start-here
block). Deployed to the droplet; site verified 200; both services active after a
clean startup. No droplet `.env` change was required (price defaults to $0.04).

---

## Session 2026-10-01 (session 9)

**Async image fixes + ZIP bundle (Phases 1 & 2), plus two UX refinements.** All
**shipped + deployed** (`main` at `78131ec`) and the feature is **ON + verified live**.
Tests **315** (`ruff` + `pip-audit` clean). Plan file:
`~/.claude/plans/mossy-strolling-boole.md`. Commits: `6a762ae` (Phases 1 & 2),
`78131ec` (UX refinements).

### Why
Session 8's image fixes ran **synchronously in the web request** — a click tied up a
gunicorn worker for up to ~1 min calling Claid, with no feedback and the result only
reachable by opening the downloaded file. On the shared droplet that risks starving
the web pool. This moves the work to the background worker and reworks the UX.

### What shipped
- **New `image_jobs` queue** (`app/image_jobs.py`, table in `app/db.py`). Single-phase
  lifecycle `queued → processing → done|error`, one row per `(scored_item_id, slot)`
  (`slot` = `img{N}` | `whitebg`), `UNIQUE(scored_item_id, slot)` → idempotent enqueue.
  Mirrors `copy_jobs` exactly (claim/mark/reclaim). The disk cache
  (`ci_images.enhanced_image_path`, keyed `(sid, slot, ext)`) stays the artifact store;
  the DB row only tracks status.
- **Worker** (`worker.py`): `process_image_one` (calls `image_enhance.enhance` →
  `ci_images.save_enhanced_image` → mark done; fail-loud, no secrets), `drain_image_jobs`
  (thread pool bounded by `SCORING_CONCURRENCY`, no browser/Walmart pause — it's a plain
  Claid HTTP call), `reclaim_orphaned_image_jobs` on startup, and a drain branch in
  `main()` **after copy, before CI** (lightest work).
- **Routes** (`app/routes/pages.py`): the two synchronous GET routes are **replaced** by
  `POST /app/pdp-scoring/enhance/<sid>/<index>`, `POST .../whitebg/<sid>`,
  `POST .../enhance-all/<sid>` (Phase 2 fix-all), `GET .../enhanced/<sid>/<slot>` (inline),
  `GET .../enhanced/<sid>/<slot>/download`, and `GET .../enhance-all/<sid>/download.zip`
  (stdlib zip of finished slots, named by position). `_enhance_targets` is the single
  source of truth for which fixes an item offers (image-1 upscale is suppressed when the
  combined whitebg fix applies). `_annotate_enhance` attaches per-slot state
  (done/queued/processing/error/none) + an `enhance_summary` for the fix-all/ZIP bar, and
  returns `enhance_pending` (OR'd into the existing 5s `<meta refresh>` so results appear
  live). SSRF guard unchanged — the provider only ever gets URLs from our own scrape.
- **UI** (`pdp_results.html` macro `enhance_control` + `pdp_results.js` + `.pdp-enhance-*`
  CSS in `workspace.css`): per-image button → "Enhancing…" spinner → inline image +
  Download → Retry; per-item "Fix All Images For This Item" (JS `confirm`, metered) +
  "Download All As ZIP" + "N of M fixed". Before/after = **after inline** (same-origin,
  CSP-ok), **before as a CDN link** (CSP `img-src 'self'` blocks embedding the original).
- **Tests:** new `tests/test_image_jobs.py` (queue), image-fix cases added to
  `tests/test_worker.py`, and the enhance route tests in `tests/test_pages.py` rewritten
  from sync-download to enqueue/serve/zip/fix-all + IDOR.
- **DEPLOY.md:** the enable-note now says restart **both** web + worker (worker loads the
  same `.env`, so no infra change — just needs a restart to see a newly set key).

### Follow-up UX refinements (same session, commit `78131ec`, shipped)
Two tweaks after the user tried the feature live:
- **Detail row stays open across reloads.** Previously the expanded row snapped shut on
  every 5s auto-refresh and on the POST→redirect after a fix. Now open rows are saved in
  `sessionStorage` (keyed on a new per-row `data-id`) and restored on load
  (`restoreOpen`/`persistOpen` in `pdp_results.js`). Clicks on the enhance/retry/download
  **buttons** no longer toggle the row. Browser-verified: expand → reload → still open.
- **Main image: both issues on one line.** When the main image needs both a resolution
  upscale and a white background, its flagged-image line now reads both issues and offers
  the single combined fix inline (`_annotate_enhance` + the `pdp_results.html` list). The
  separate paragraph is kept (and labelled "Main image —") only for the white-bg-only case
  (resolution fine, so it isn't in the flagged list). New test
  `test_whitebg_only_uses_standalone_line` + assertions on the consolidated line.

### Verification done
315 tests, `ruff`, `pip-audit` all green; app boot + route-registration + schema smoke
check passed. **Browser-verified** the two UX refinements on a seeded scratch DB (expand
persists across reload; combined main-image line renders). **Real Claid call verified live**
on 2026-10-01 — a genuine round-trip returned a 2000px JPEG (the previously-open
end-to-end risk is now closed).

### Next steps if resumed
- **Batch image fixing** — the main next-up; see "## Batch image fixes — design ideas" below.
- Minor nicety still open: nothing — expanded-row persistence (the plan's optional nicety)
  was done in `78131ec`.

---

## Batch image fixes — design ideas (next up)

_Design discussion captured 2026-10-01 for the user to think through; **nothing decided
or built**. This is the deferred "Phase 3," reframed. Read before building._

**The problem to manage:** fix the flagged images across a **whole scored batch** (up to
100 items, each with several flagged images), not one item at a time. The plumbing is
mostly in place — the real design challenge is **informed, controllable spend**, because
**every fix is a metered Claid call**, so one batch button could fire hundreds of paid
calls. Everything below serves that.

**What we already have to build on** (makes a batch version largely additive):
- `image_jobs` queue is **idempotent and cache-skips** any slot already fixed, so batch
  runs are naturally **resumable** (re-run only fills the gaps).
- The worker drains image jobs **concurrently** and **after** scoring/copy, so a big image
  batch yields to new scoring waves.
- Per-item **"Fix All" + ZIP** and the results **table with row checkboxes** (currently used
  for copy rewrite) already exist — reuse both.

**Key design decisions:**
1. **Scope — don't make "all" the only option.** Reuse the table's row checkboxes: a
   top-of-table bar with **"Fix images for selected"** and **"Fix all flagged in batch."**
   Add quick filters to target spend: *main-image white-bg only* (a hard Walmart gate —
   highest ROI), *sub-2000px upscales only*, or *items below score X*.
2. **Cost preflight before anything runs.** A confirm stating the real count:
   *"142 white-bg fixes + 238 upscales across 72 items; already-fixed skipped."* If we get
   Claid's per-image price, show *≈ $NN*. Add a cap/guardrail for very large runs.
3. **Prioritize high-impact fixes first** — queue main-image white-bg ahead of gallery
   upscales so a partial/interrupted run delivers the compliance wins first (add a priority
   ordering to `image_jobs`, currently FIFO by id).
4. **Batch progress + per-row badges** — a batch progress bar (reuse `_eta_label`) and a
   per-row badge "N fixes · M done · K failed," clickable to expand. All status already in
   the queue; this is aggregation queries.
5. **Failure handling + cancel** — a **"Retry failed"** batch action; the deliverable
   includes whatever succeeded; a **"Stop"** that cancels still-*queued* jobs (in-flight
   can't be refunded). Cache-skip already makes re-running safe/resumable.
6. **Delivery (differs most from per-item)** — one **batch ZIP organized by item** (a folder
   per SKU, files named by gallery position) **plus a manifest CSV** (item → original image →
   fixed filename) so the user knows what to re-upload where. The manifest is half the value
   at batch scale.
7. **Dedup across the batch** — a re-scored product can appear in multiple rows; the cache
   is keyed per `scored_item_id`, so today a duplicate pays twice. Dedup by item-id + image
   URL before enqueue.

**Recommended phased cut (if greenlit):**
1. **Batch bar "Fix flagged for selected"** (reuse checkboxes) + **cost-preflight confirm**
   + cache-skip + priority ordering (folds in cheaply). → the capability, safely.
2. **Per-row badges + batch progress bar.** → makes a long run legible.
3. **Batch ZIP (by-item folders) + manifest CSV.** → makes the output usable.
4. **Retry-failed + cancel-queued.** → makes it robust.

**Open questions for the user (decide next session):**
- **Scope default:** selected-items (safer, deliberate) vs. a true whole-batch "fix
  everything"? (Lean: selected-first.)
- **Cost:** is Claid's per-image price known? If so we can show a $ estimate; if not,
  counts only.
- **Delivery end-goal:** "ZIP + manifest to re-upload manually," or eventually **push fixed
  images directly to Walmart via their API** (a much larger, separate effort)?

---

## Session 2026-10-02 (session 8)

All shipped to `main` + deployed. Commits are listed per area. Tests: **291**.

### Results page — Product Detail Page Content Scores (`pdp_results.html`)
- Rebuilt from one-tall-card-per-item into a **dense, sortable table** that scales
  to hundreds of items. One row per item: select checkbox · thumbnail · **Brand** ·
  Product · **big Overall + bar** · per-dimension **mini-bars** · Status · expand
  caret. Click a row to expand its recommendations.
- Progressive enhancement in `app/static/js/pdp_results.js`: column-sort, text
  filter, row expand/collapse, **bulk select-all** (header checkbox, indeterminate
  state) and **Expand/Collapse all**. CSS lives in the `.pdp-*` section of
  `workspace.css`. Works with JS off (panels render open; filter inert).
- **Summary strip** (avg score + strong/moderate/needs-work bands) + a **progress
  bar with ETA** ("N of M scored · about X left") while pending — the subtitle no
  longer flashes (that cue moved to the bar). Route helpers: `_score_summary`,
  `_eta_label` (~12s/item), `_row_view` now returns `image_url`, `brand`, `record`.
- **Columns:** removed **Attributes** (still contributes to the overall score —
  just not shown), added **Brand** (left of Product; from `scored_items.brand`).

### Naming / labels
- All **user-facing "PDP" → "Product Detail Page"** (routes `/app/pdp-scoring`,
  Python identifiers, CSS classes, logs, the LLM prompt — all left unchanged).
- **All button labels Title-Cased** app-wide (buttons only; help text, section
  headings, page `<title>`s, inline nav links left as written).
- Copy results heading → "Product Detail Page Copy Content Creation"; "New batch" →
  "Create More"; "Score more" → "Score More".

### Concurrency (worker + DB)
- Scoring and copy now process **up to `SCORING_CONCURRENCY` items at once** via a
  **thread pool inside the single worker process** (`worker.drain_scoring()` /
  `worker.drain_copy()`). Default **3**, hard-capped 10, env-tunable. One process,
  so the startup orphan-reclaim stays correct (no lease/migration).
- `db.tune_connection()` applies **WAL + a 5s busy-timeout** to every connection so
  the pool's concurrent writes don't collide with web reads. `jobs.has_queued_items`
  / `copy_jobs.has_claimable_items` gate the pools. Scoring and copy never drain at
  the same time (shared memory budget). **Live at concurrency=3** on the 8 GB droplet.
- Throughput ≈ **3–5 items/min** (~15–20s/item). The ETA assumes ~12s/item; it reads
  a touch pessimistic at the very start of a batch.

### Copy flow — reuse scored content, progress, exports
- **Cross-link reuse:** "Create New Copy Content" from the scoring screen now reuses
  the content captured at scoring time instead of re-fetching the PDP. Scoring
  persists the full `PdpRecord` in new **`scored_items.record_json`** (additive
  migration), and `pages.pdp_scoring_create_copy` builds copy rows **pre-populated**
  (status `gen_queued`) via `copy_jobs.create_prefetched_copy_items` → straight to
  generation. Items without a record (older scores) fall back to the fetch path. The
  copy results page shows "Current copy captured <time> — reused from scoring".
- Copy results also got the **progress bar + ETA + de-flashed subtitle**
  (`_copy_progress`, reuses `_eta_label`).
- **Exports** (`app/copy_export.py`): "Download To Excel" (`.xlsx`, openpyxl) and
  "Download CSV" (stdlib) of the generated copy — columns Item ID / Product URL /
  Product Name / Site Description / Key Feature 1..N, one row per completed item.
  Routes `/app/pdp-copy/results.xlsx` and `.csv`.

### Imagery scoring — check all images
- Resolution now grades **every** gallery image (not the single largest edge):
  `fetch._measure_image_dims` → `PdpRecord.image_dims` (`[{url, px}]`, up to 12).
  Each image is scored on its own tier; every sub-2000px image is recorded on
  `DimensionScore.image_issues` (`{index, url, px, severity}`). Falls back to the
  old single-max tier when per-image data is absent (older results).
- White-background failure now also sets `DimensionScore.white_bg_url` (the main
  image URL) so the UI can act on it.

### AI image fixes (`app/image_enhance.py`) — config-gated, Claid.ai
- **Inert until `IMAGE_UPSCALE_API_KEY` is set** (mirrors the residential-proxy
  plumbing). Provider = **Claid.ai** (`POST /v1/image/edit`, Bearer auth). Claid
  fetches the source URL itself; we pass the flagged image's URL **from our own
  stored result** (SSRF-safe) and download the result.
- Operations: `upscale` (restorations.upscale `smart_enhance` + resize 2000) and
  `white_bg` (background→#FFFFFF **+** upscale + resize — one call fixes a low-res
  off-white main image completely, since you can't chain two downloads).
- UI (results detail, only when configured): **"Enhance to 2000px"** per sub-2000px
  gallery image; the **main image** (gallery index 1) shows ONE **"Fix & enhance
  main image"** when it also needs white-bg (its standalone enhance link is
  suppressed to avoid two half-fixes).
- Routes: `GET /app/pdp-scoring/enhance/<sid>/<index>`, `GET /app/pdp-scoring/whitebg/<sid>`
  (user-scoped/IDOR-guarded, cached under `media/enhanced/` via
  `ci_images.enhanced_image_path/has/save`, slot-keyed `img{N}` / `whitebg`).
  **First call is synchronous** (ties up a web worker up to ~1 min), cached after.
  **Metered per image.** Config: `IMAGE_UPSCALE_*` (see `.env.example` + DEPLOY.md).

### Infra / dependencies
- **Droplet resized to 8 GB** (user). `SCORING_CONCURRENCY=3` default, live.
- New deps: **`openpyxl==3.1.5`** (Excel export). **`urllib3==2.8.0`** pinned up to
  clear PYSEC-2026-4175/76/77 (published 2026-10; it comes transitively via
  `requests` and was about to fail the CI `pip-audit` for any commit).
- New column: `scored_items.record_json` (additive migration). New optional env:
  `IMAGE_UPSCALE_PROVIDER/API_KEY/MODE/TARGET_PX/OUTPUT_FORMAT`.

### Image fixes — next steps (ON HOLD, user deciding)
Proposed phased plan for a better fix-management UX (see the end-of-session
discussion):
1. **Async** — move enhancement to the background worker (like scoring/copy) with
   per-image status + a progress bar, and show the **fixed image inline** (it's
   cached same-origin, so CSP allows displaying it — unlike the original CDN image).
2. **"Fix all images for this item" → one ZIP** (user's idea) — fixes every flagged
   image + the main image and bundles them, named by position, to re-upload.
3. Batch-wide "fix all" + per-row "N fixes available" badges.
Guardrail: each fix is a metered Claid call, so add a confirm before a bulk fix.

---

## Earlier sessions (historical)

> **Session 7 (2026-09-30) — DISCOtech rebrand.** Session 7 did a **visual-only rebrand**
> to "DISCOtech" — new logo, color palette, and copy. **Read §12 "Session
> 2026-09-30 — DISCOtech rebrand" first**; it's the freshest and most detailed.
>
> **Session 7 is committed locally, not yet pushed.** Pushing to `main`
> auto-deploys (see §2/§9 "Deploy caution") — decide when ready, check for an
> active CI run first if deploying. Until pushed, none of this has touched CI
> or the droplet. Tests: **252** passing.
>
> Everything from Session 6 (2026-08-26) below is still accurate and **is** live
> on main/deployed — that work hasn't changed. Headlines from session 6:
> 1. **Brand-level Share of Digital Shelf** — a new section at the END of both the
>    snapshot and monitoring reports showing a brand's WHOLE page-1 presence (all
>    SKUs, tracked + untracked) ÷ the shelf, complementing the existing tracked-only
>    share. Page + PDF (its own page). No new scraping — reads `ci_search_results`.
> 2. **Brand Advertising Presence** — headline (Sponsored Brand / "Brand Amplifier")
>    and sponsored-video ad **detection + creative-image capture + per-brand counts**
>    on the search page. New `ci_ad_units` table, images under `MEDIA_DIR/ci_ads/`,
>    section at the end of snapshot + monitoring (page + PDF). **Requires a
>    residential proxy** (see below) — Walmart serves these ads only to residential
>    IPs, so the datacenter droplet got zero fill without it.
> 3. Small UI: Contact Us rail sub-label; PDP scoring/copy flashing message now sets
>    a time expectation ("up to 1 minute per item"); snapshot in-progress copy is now
>    "Extracting data and creating the report… / This can take up to 5 minutes."
>
> Tests: **252** passing (`ruff` + `pip-audit` clean).
>
> **NEW infra — residential proxy (metered).** A live **Oxylabs** residential proxy
> is configured in the droplet `.env` (`WALMART_PROXY_*`, see §2) and the CI scraper
> routes **every** search scrape through it (config-gated — inert if unset). This is
> what makes Walmart serve the headline/video ads. **It costs per-GB and now runs on
> every scrape, incl. the 3×/day monitoring sweeps.** A "Press & Hold" bot-challenge
> fires intermittently during ad capture; the scraper detects it (iframe-aware) and
> skips the creative screenshot (count kept, no marred image) — see §9.
>
> **Deploy caution (worker restarts):** `deploy.yml` restarts the worker, which
> **orphans any in-flight CI run** (the reclaim logic marks it `error`). Before
> pushing while runs may be happening, check for an active run first:
> `ssh droplet-deploy 'cd /home/deploy/apps/ecomm-copilot && venv/bin/python -c "import sqlite3,os;c=sqlite3.connect(os.environ.get(\"DATABASE_URL\") or \"app.db\");print([r[0] for r in c.execute(\"SELECT id FROM ci_runs WHERE status IN (\x27queued\x27,\x27running\x27)\")])"'`
>
> **No open operational items.** Open enhancement (optional): raise the ad-creative
> image hit-rate (retry a challenged keyword's capture with a fresh session/IP, or
> tune Oxylabs rotation). Video-ad brand attribution is best-effort + not yet seen
> live. See §7 session 6.

---

## 1. Snapshot

- **Live:** https://ecomm-copilot.com (HTTPS, auto-renewing cert).
- **Repo:** `github.com/ricksauls/ecomm-copilot` · **local working copy:**
  `~/Desktop/ClaudeStuff/ecomm-copilot-clean`.
- **Stack:** Python / Flask, SQLite, server-rendered Jinja templates, deployed
  by GitHub Actions to a DigitalOcean droplet.
- **Tests:** 252 passing (`ruff` clean, `pip-audit` clean).
- **Worker:** installed and running — scoring is self-serve end-to-end (intake →
  queue → background fetch+score → results). One worker only (see §6, parallelism).
  On startup it now **reclaims orphaned in-flight work in all three queues**
  (scoring, copy, CI) so a mid-fetch restart never strands a row — see §7 session 5.

**What works today:**
- Marketing **landing** page (dark), self-service **auth** (email/password +
  Google SSO), login-guarded **workspace**.
- **Dashboard** (rebuilt session 5): a 6-card KPI row over **five "this month"
  activity tables** — PDP Scored / Copy / Image Sets, then CI Snapshot / Monitoring.
  Each table is **collapsible** (native `<details>`, open by default), **column-
  sortable**, **capped at 10 rows** (rest scroll under a sticky header), shows a
  product **thumbnail (hover to enlarge)**, and its **rows are clickable → open
  that run's results**. Each has a **View all** link to its all-time screen. Two
  dedicated all-time screens also exist: **View All Content Activity**
  (`/app/content-activity`, the 3 PDP tables) and **View All Competitive
  Intelligence Activity** (`/app/competitive-intel/activity`, the 2 CI tables). See
  §7 session 5.
- **Contact Us messaging** (in-app, two-way) — users open threads (subject +
  category: question/issue/customization/other) and see replies as a
  conversation; admins get a **Messages** inbox (unread first) and reply as the
  team, with close/reopen. Unread counts show in the topbar bell + rail badges
  for both sides (shared admin inbox). See §5, §3 (`app/messages.py`).
- **PDP Content Scoring** end-to-end: intake (multi-URL or CSV, up to 100), queue,
  background fetch+score, results page (flashes while scoring, shows product
  title), and a **PDF export** of a batch.
- **PDP Copy Content Creation** end-to-end: intake (same URL/CSV, up to 100) →
  "Get Current Copy Content" (worker fetches current Title/Description/Key
  Features) → "Create new copy content" (AI rewrite via Claude) → results screen
  showing current vs new copy **side by side** with a current→projected score
  delta, plus a **PDF export** (Download PDF, `/app/pdp-copy/results.pdf`). Also
  reachable by ticking items on the scoring results and clicking "Create new copy
  content" (that path fetches **and** generates in one pass). **Live + verified**
  on the droplet (Tabasco Chipotle: current 82 → projected 96).
- **Competitive Intelligence** end-to-end, organized into **four rail menus**
  (order: One-Time Snapshot → View Snapshot, Daily Monitoring → View Monitoring;
  groups are tagged by `mode`, each setup menu manages only its own):
  1. **One-Time Snapshot** — configure a group (Brands mine/competitor →
     Products → Keywords), **Run**, see **current-state results only (no
     trends)**, download a **snapshot PDF**. The results page (rebuilt 2026-08-23)
     stacks five sections in this order: **What this group tracks** (config
     summary — now with a **main-image thumbnail grid** for the tracked products,
     mine-first; see §3 image cache), **Overall Search Ranking** (avg page-1
     ranking per brand, over the terms it appears on, **+ a page-1 placement-map
     grid**), **Search Ranking** (keyword → brand → avg ranking, ordered keyword
     then avg asc), **Overall Share of Digital Shelf** (table + a CSS/HTML
     **stacked bar chart** of each brand's organic/sponsored *share %* — not raw
     counts), and **Share of Digital Shelf** (per-keyword table, ordered keyword
     then total-share desc). The **snapshot PDF mirrors the page** (all sections
     incl. the summary with thumbnails, the SoS **stacked-bar chart**, and the
     **placement-map grid** — all drawn with reportlab shapes/images). While a run
     is queued/running the subtitle **flashes** and `ci_config.js` reloads on
     completion.
     - **Placement map** (2026-08-24): a 4-col page-1 result grid, blank except
       where each brand's *overall* average rank lands — my brand in signal red,
       competitors in ink, ties split one tile into a chip per brand, exact
       average on the tile. Grid depth = the run's deepest page-1 slot. Pure model
       in `ci_analysis.build_rank_placement_map` (shared by page + PDF).
     - **Schedule for monitoring** (2026-08-24): a button on each snapshot card
       *clones* the set into a new `mode='monitoring'` group (leaving the snapshot
       intact), enables the sweep, and queues a baseline —
       `ci_config.clone_group_as_monitoring` + `pages.ci_schedule_from_snapshot`.
  2. **View Snapshot** — pick a snapshot group from a **dropdown** and see its
     current-state results (the same five sections as the snapshot results page,
     no trends). Route `pages.ci_view_snapshot`, template `ci_view_snapshot.html`;
     the five sections come from the shared partial `_ci_results_sections.html`
     with `show_trend=False`.
  3. **Daily Monitoring** (setup; was "Monitoring Setup") — configure, **Schedule &
     Run** (turns on the 3×/day sweep at 7 AM / 3 PM / 11 PM CST **and** runs an
     immediate baseline), shows the **next scheduled run time**.
  4. **View Monitoring** — pick a monitoring group from a **dropdown**; results are
     **aggregated over the last completed calendar period** (Week Mon–Sun / Month /
     Quarter / Year), each of the five sections mirroring the snapshot layout **plus
     a "vs prior" delta column and a per-period trend sparkline** (one point per
     completed period; hover shows "period · value"). Period buttons **disable until
     their most recent completed period has data**. Download a **monitoring PDF**
     that mirrors the page. Same shared partial with `show_trend=True`. See the §7
     session note for the full model.
  The worker scrapes page-1 Walmart search per keyword, records each card's
  position + organic/sponsored type, attributes each card to a brand, and rolls up
  per-brand share-of-search. The config screen carries a help panel. Monitoring
  systemd timers are **installed and active** on the droplet (§2).
  - **Brand attribution (important, learned the hard way):** Walmart search cards
    expose an **opaque `data-item-id`** (e.g. `3K2RMCS1KI5D`), *not* the numeric
    item number. So matching parses the **numeric id from the card's `/ip/<slug>/
    <number>` URL** and matches that to tracked products (and stores it as the row
    `item_id`, so the ranking join lines up). **Sponsored** slots get a *different*
    id **and** a tracking URL with no `/ip/<number>`, so they can only be matched
    by a **brand-name fallback** (the brand name found in the card title). See
    `app/ci_scraper.py:build_result_rows` — order is numeric-id → raw-id → URL →
    brand-name. This also counts a brand's untracked SKUs toward its share.
  - **Search Ranking is brand-level and includes competitors** (mine + competitor)
    so users compare standings. Snapshot/View Snapshot show the run's average
    ranking; View Monitoring shows the **average over the completed period + a
    "vs prior" delta + a per-period trend sparkline** (see §7 session note).
  - **Share % denominator = all page-1 placements** (branded + "Other"); the
    placement count is shown on-screen. **PDF export** works for both snapshot and
    monitoring (`.../results.pdf`, `.../view/<id>/results.pdf`).
  - **Verified live** (Tabasco Original Hot Sauce group, run 3, 284 page-1 cards):
    sponsored attribution now works — 24 of 50 sponsored slots attribute to a
    tracked brand (was 0 before the fix). Share of shelf: Tabasco 67 (55 organic +
    12 sponsored), Frank's 32, Louisiana 20, Cholula 17, Other 148. Brand-level
    ranking populated for all four brands across all five keywords with
    organic/sponsored splits (e.g. Tabasco "tabasco" best #1 via sponsored). Note
    brand-name matching counts a brand's *untracked* SKUs too, so organic counts
    jumped vs run 2 (Tabasco 3→55) — that's correct share-of-shelf semantics.
- **Admin screens** (Users, Items scored, **Copy created**) for the two admin
  emails, with a new-user notification and per-user delete.

---

## 2. Infrastructure

- **Droplet:** `wm-content-tools`, `142.93.244.23`, Ubuntu 24.04 — **shared**
  with the WM share-of-voice app. **Only ~2 GB RAM** — this constrains worker
  parallelism (see §6). Don't disrupt the other services.
- **App dir:** `/home/deploy/apps/ecomm-copilot`, runs as user `deploy`.
- **Web:** `ecomm-copilot.service` → gunicorn on `127.0.0.1:8001` behind nginx
  (site `ecomm-copilot.com` + `www`). Ports 8000/8002 belong to other apps.
- **Worker:** `ecomm-copilot-worker.service` runs `worker.py` under `DISPLAY=:99`
  (headed Chrome). Installed and active. Drains three queues now: scoring, copy,
  and **Competitive Intelligence runs** (a CI run scrapes a group's keywords).
- **CI monitoring timers (installed + active):** three systemd timers
  (`ecomm-copilot-ci-{morning,afternoon,night}.timer`) enqueue monitoring runs at
  7 AM / 3 PM / 11 PM CST via `ecomm-copilot-ci-monitor@.service`
  (`python -m app.enqueue_monitoring <slot>`). Installed manually as root on
  2026-08-22 (DO Console; deploy sudo password is lost) and enabled — verified via
  `systemctl list-timers 'ecomm-copilot-ci-*'` (next fires 12:00/20:00/04:00 UTC =
  7 AM/3 PM/11 PM CST). A group only gets swept when its owner turns **monitoring
  on** for it. Re-installing after a fresh `setup-droplet.sh` is idempotent; the
  copy-paste block is in `deploy/DEPLOY.md` ("Competitive Intelligence monitoring
  timers") if the droplet is ever rebuilt (a normal git-pull deploy does not run
  setup-droplet.sh, so the timers persist untouched across deploys).
- **Xvfb:** `xvfb.service` on `:99` (from the WM scraper) — the worker reuses it.
- **DB:** SQLite at `DATABASE_URL` (`/home/deploy/apps/ecomm-copilot/app.db`),
  chmod 600. Tables: `users`, `scored_items` (+`batch_id`), `keyword_cache`,
  `copy_items` (Copy Content Creation, +`batch_id`), the CI set `ci_groups`
  (+`mode`), `ci_brands`,
  `ci_products`, `ci_keywords`, `ci_runs`, `ci_search_results`,
  `ci_share_of_search`, **`ci_ad_units`** (session 6 — brand ad sightings:
  run/group/keyword/ad_type[headline|video]/brand_id/brand_text/image_path), and the
  messaging set `message_threads`, `messages`
  (Contact Us). Schema is created + migrated idempotently at web **and** worker
  startup (`db.ensure_schema` = `_SCHEMA` + `_migrate`).
- **Media dir (cached images):** `$MEDIA_DIR` (default `media/` next to `app.db`).
  Two subdirs: `media/ci_products/<item_id>.jpg` (tracked-product mains) and, new in
  session 6, `media/ci_ads/<run>_<kw>_<type>.jpg` (captured ad creatives). Worker-
  populated, outside the repo (survives git-pull deploys), created on first write.
  Delete a file to force a re-fetch. See §3 + DEPLOY.md.
- **Secrets / config** in `/home/deploy/apps/ecomm-copilot/.env` (chmod 600,
  never committed): `SECRET_KEY`, `DATABASE_URL`, `APP_URL`,
  `GOOGLE_CLIENT_ID/SECRET`, **`ADMIN_EMAILS`** (comma-separated allowlist =
  `ricksauls@cox.net,ricksauls1@gmail.com`), — for Copy Content Creation —
  **`ANTHROPIC_API_KEY`** (the AI copy generator; the worker fails those items
  loudly if it's unset) and optional **`COPYGEN_MODEL`** (defaults to
  `claude-opus-5`), — and, new in session 6, the **residential proxy** used by the
  CI scraper so Walmart serves the headline/video ads: **`WALMART_PROXY_SERVER`**
  (live Oxylabs endpoint `http://pr.oxylabs.io:7777`), **`WALMART_PROXY_USERNAME`**,
  **`WALMART_PROXY_PASSWORD`**. Config-gated: unset → scrape direct (no ads). It's
  **metered per-GB and runs on every CI scrape** (see §9 for the proxy + captcha
  notes).
- **SSH from the Mac:** `ssh droplet-deploy` (key `~/.ssh/deploy_wm_ci`, **no
  passphrase**). Passwordless — that's the way in; you can drive the droplet
  directly. Root is only via the DO web Console (the `deploy` **sudo** password
  was lost — `passwd deploy` there to reset). The scoped sudoers rule lets
  `deploy` restart the two ecomm services without a password.

### Deploy pipeline (how shipping works)
`git push origin main` → **CI** (ruff, pip-audit, pytest) → on success the
**Deploy** workflow (`workflow_run`) SSHes in, `git pull`, `pip install -r
requirements.txt`, restarts `ecomm-copilot` (and `ecomm-copilot-worker`, guarded).
Everyday loop:

```bash
git -C ~/Desktop/ClaudeStuff/ecomm-copilot-clean add -A
git -C ~/Desktop/ClaudeStuff/ecomm-copilot-clean commit -m "..."
git -C ~/Desktop/ClaudeStuff/ecomm-copilot-clean push origin main
gh run watch -R ricksauls/ecomm-copilot   # optional
```

Actions secrets (already set): `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`,
`DEPLOY_FINGERPRINT` (**ECDSA** host key — the SSH action negotiates ecdsa).

---

## 3. Code map

```
app/
  __init__.py        app factory: logging, strict-CSP headers, session
                     hardening, MAX_CONTENT_LENGTH, ADMIN_EMAILS config,
                     blueprints, DB + OAuth init, before_request hooks, and three
                     context processors (static_url + admin nav counts + message
                     unread counts)
  db.py              SQLite schema + idempotent _migrate; per-request conn
  users.py           user CRUD, password hashing, record_login, list/count,
                     delete_user, count_created_since (new-user notification)
  security.py        CSRF, login_required, current_user, input validation,
                     is_admin + admin_required (ADMIN_EMAILS allowlist)
  auth.py            /signup /signin /signout + Google SSO; records login times
  oauth.py           Authlib Google client (gated on GOOGLE_CLIENT_ID/SECRET)
  pdp.py             intake parsing: URL validation, CSV parse, item-number
  scoring.py         rule-based scorer: PdpRecord -> ScoreResult (see §4)
  fetch.py           Playwright fetch + __NEXT_DATA__ parse -> PdpRecord; Pillow
                     resolution + main-image white-background check; idml specs
                     + longDescription bullets. _load_pdp_data (shared browser
                     load) + fetch_main_image_url (image URL only, for CI cache)
  ci_images.py       cached tracked-product main images: path guard (digits only,
                     no traversal), download+downscale to JPEG, has/save/from_url
  keywords.py        keyword discovery: Walmart autocomplete + competitor SERP
                     mining -> ranked target set; cache_key (category-level)
  jobs.py            scored_items queue (enqueue w/ batch_id/claim/save),
                     reclaim_orphaned_items, batch_ids_for_item, list_scored_activity
                     (optional since), dashboard counts, keyword_cache get/put
  copygen.py         AI copy rewrite: PdpRecord + keywords -> Claude (structured
                     output) -> GeneratedCopy. COPYGEN_MODEL, lazy SDK import
  copy_jobs.py       copy_items queue: two-phase fetch->generate lifecycle
                     (enqueue w/ batch_id/claim/save_current/save_generated/
                     request_generation), reclaim_orphaned_copy_items,
                     batch_ids_for_copy_item, list_copy_activity (optional since)
  messages.py        Contact Us model: threads + messages, per-side unread by
                     *message id* (not timestamp — avoids same-second ties),
                     create/reply/mark_read/set_status, unread counts, IDOR-scoped
                     reads. CATEGORIES allowlist.
  pdf_export.py      reportlab PDFs: build_results_pdf (scored batch),
                     build_copy_pdf (copy batch), build_ci_snapshot_pdf (+ SoS
                     stacked-bar chart, placement-map grid, product thumbnails),
                     build_ci_monitoring_pdf
  routes/pages.py    landing, dashboard (5 "this month" activity tables +
                     _activity_rows/_ACTIVITY_META shaping), View All screens
                     (activity_all/<kind>, content_activity, ci_activity), PDP
                     scoring + results + results.pdf + per-item results
                     (pdp_scoring_item), PDP copy (... + pdp_copy_item), scoring
                     create-copy cross-link, CI snapshot/monitoring/view + PDFs +
                     schedule-from-snapshot, /media/ci-product/<id> (cached image),
                     Contact Us (contact_home/create/thread/reply), admin
                     users/items/copy + admin messages + delete
  fixtures.py        demo data (KPI/agency scaffold; the demo table/sidebar it
                     also carried are no longer rendered — dashboard body rebuilt)
  templates/…        public_base + landing/signin/signup; app/base + _rail,
                     _topbar, dashboard, _dash_tables (shared activity-table
                     macros), activity_all, content_activity, ci_activity, pdp_*,
                     ci_*, admin_*, contact_* (shared user+admin thread view)
  static/            css/{tokens,public,workspace}.css,
                     js/{intake,admin_users,ci_*,dashboard}.js, img/{logo,...}
                     (dashboard.js: sort + 10-row cap + thumbnail hover-preview)
worker.py            background worker (systemd): drains scoring, copy, and CI
                     queues; reclaims orphaned in-flight work in all three on
                     startup. Caches each fetched item's main image
                     (_cache_item_image for scoring/copy; _cache_ci_product_images
                     for CI), best-effort
deploy/              DEPLOY.md, *.service units, nginx.conf, setup-droplet.sh
tests/               237 tests (auth, jobs, pdp, scoring, fetch, pages, keywords,
                     admin, copygen, copy_jobs, copy, ci_config/jobs/scraper/
                     analysis/worker/monitoring/pages, ci_images, messages,
                     messages_pages)
```

**Nav (rail):** **Dashboard** + **Contact Us** (top-level; Contact Us shows an
unread badge); **Content Studio** section — PDP Content Scoring (built), PDP Image
Set Creation (placeholder), PDP Copy Content Creation (built), **View All Content
Activity** (new session 5); **Competitive Intelligence** section — One-Time
Snapshot, Daily Monitoring, **View All Competitive Intelligence Activity** (new
session 5). _Session 5 removed the old **View Snapshot** / **View Monitoring** rail
items — routes `ci_view_snapshot` / `ci_view` still exist; `ci_view` is still
linked from the monitoring setup/config "View results" buttons, `ci_view_snapshot`
is now unlinked but intact._ **Admin** section (below Credits, admins only): Users, Items scored, Copy
created, **Messages** (unread badge), each with a live count. The **topbar** bell
shows unread-message counts (admin inbox for admins, own replies for users) plus
the admins-only new-user badge.

**Competitive Intelligence modules** (`app/`): `ci_config.py` (groups/brands/
products/keywords CRUD, user-scoped/IDOR-checked; groups carry a `mode` =
snapshot|monitoring; `clone_group_as_monitoring` powers snapshot→monitoring),
`ci_jobs.py` (run queue + result writers + SoS rollup), `ci_scraper.py`
(search-page card extraction + pure row builder), `ci_analysis.py` — two families:
**run-scoped `snapshot_*`** for the snapshot views (`snapshot_brand_avg_rank`,
`snapshot_rank_by_keyword_brand`, `snapshot_share_of_shelf`, `snapshot_share_by_keyword`,
`snapshot_page1_depth`, pure `build_rank_placement_map`; `snapshot_rank` is test-only)
and **calendar-period monitoring** (`PERIODS`/`PERIOD_LABELS`, `period_bounds` /
`period_label` / `period_has_data` / `available_periods`, date-scoped `_*_range`
aggregations, and `monitoring_{avg_rank, rank_by_keyword, share_of_shelf,
share_by_keyword, placement_map}` returning current + `delta` + per-period `trend`);
plus `next_monitoring_run` / `format_run_time_cst`. `enqueue_monitoring.py` (timer
entry point). **Snapshot** page + PDF share `pages._snapshot_data()`; **monitoring**
page + PDF share `pages._monitoring_data()` — so each pair never drifts.
Worker drains CI runs in `worker.process_ci_run`. Routes `pages.ci_*` (four flows:
snapshot setup + **view-snapshot**, monitoring setup + **view**) + templates
`templates/app/ci_{snapshot_home,monitoring_home,group_config,snapshot_results,
view,view_snapshot}.html` + `_ci_help.html` + the shared `_ci_results_sections.html`
partial (the 5 sections, `show_trend` toggles the delta/trend columns);
static `js/ci_{config,charts,dashboard}.js` (the `.ci-spark` sparkline + hover
tooltip live in `ci_charts.js`); PDFs via `pdf_export.build_ci_{snapshot,monitoring}_pdf`
(both go through the shared `_ci_results_flow`). Tables: `ci_groups` (+`mode`),
`ci_brands`, `ci_products`, `ci_keywords`, `ci_runs`, `ci_search_results`,
`ci_share_of_search`, `ci_ad_units`. Deploy: `deploy/ecomm-copilot-ci-*.{service,timer}`.
**Session 6 additions** (see §7): brand-level share (`ci_analysis.{snapshot,
monitoring}_brand_share_of_shelf`); ad detection (`ci_scraper.{scrape_keyword_page,
build_ad_rows,_capture_ads,_challenge_overlay_present,_proxy_from_env}`,
`ci_images.{save_ad_image,ad_image_relpath,ad_image_abspath}`,
`ci_jobs.write_ad_units`, `ci_analysis.{snapshot,monitoring}_brand_ads`,
`worker._write_keyword_ads`, route `pages.ci_ad_image`, macro
`templates/app/_ci_ads.html`, `pdf_export._brand_ads_table`). Both new sections
append to `_ci_results_sections.html` + `ci_snapshot_results.html` + `_ci_results_flow`.

---

## 4. The PDP scoring model

Weighted dimensions → 0–100 overall (weights in `app/scoring.py:WEIGHTS`). The
overall is computed **only over available dimensions** — an unmeasured/paused one
is excluded (weights renormalize), not scored 0. Each dimension returns findings
+ recommendations that map to a sellable fix.

| Dimension | Weight | Scored today | Deferred (AI/vision pass) |
|---|---|---|---|
| Imagery | 25 | count, max px (zoom), **main-image white background (blended 20%)**. Video **paused**. | infographic/lifestyle quality via vision |
| Attributes | 20 | **scoring paused** (still extracted onto the record, just not in `score_pdp`) | category-schema completeness % |
| Title | 18 | length band, ALL-CAPS, word count, **+ keyword coverage (blended 30%)** | keyword *quality*/placement via LLM |
| Key features | 18 | bullet count + length | benefit-vs-feature, keywords |
| Description | 19 | word count / depth, **+ keyword coverage (blended 30%)** | structure + SEO depth via LLM |

Currently **four** dimensions score (Attributes paused). "Blended X%" = the
signal is mixed into the dimension only when measured, so the dimension still
spans 0–100 when it isn't (same pattern for keyword coverage and white-bg).
Paused signals (video, attributes) are commented/guarded, not deleted — easy to
re-enable (search `WHITE_BG_BLEND`, `KEYWORD_BLEND`, "paused" in `scoring.py`).

### Fetch method (`fetch.py`, reused from the WM scraper)
Walmart blocks plain requests, so `fetch.py` drives **headed Chrome via
Playwright** (`channel="chrome"`, `--disable-blink-features=AutomationControlled`)
under Xvfb `:99`, reads `__NEXT_DATA__` →
`props.pageProps.initialData.data`, and maps:
- `product.name`→title; `contentLayout.modules`→video; `imageInfo.allImages`→images.
- **Attributes** ← the sibling **`data.idml.specifications`** (flat name/value;
  `specificationsV2` fallback) — *not* `data.product` (absent there) and *not*
  the on-page spec table (Walmart A/B-gates it off via
  `enableSpecificationsTable=false`).
- **Key-feature bullets** ← `product.keyFeatures`, falling back to the `<li>`s in
  **`data.idml.longDescription`** (usually where they live).
- **Resolution** ← Pillow measures image bytes (dims aren't in the JSON).
- **Main-image white background** ← `_is_white_background`: downsize the first
  image, sample the outer border band (product is centered), pass if ≥90%
  near-white (transparency flattened onto white). Deterministic, no vision.

Browser work is slow + serial → runs in the **worker**, never in a request.

### Keyword coverage (`keywords.py`, the AI-pass Phase 1, rule-based)
Per item the worker builds a **target keyword set** = Walmart **autocomplete**
(typeahead API, HTTP) + **competitor SERP title mining** (headed Chrome) →
n-gram merge/rank (ported from the WM tool's `discover_keywords.py`, generalized
to derive seeds from the title). Title/Description then score how much of that set
the copy covers. **Cached** in `keyword_cache` keyed on the item's generic SERP
terms (category-level, 7-day TTL) — discovery is ~54 s/item cold vs ~0 ms warm,
so same-category batches are much faster (first item warms it, the rest fly).

**Proven live:** Tabasco (10294528) — main image white (imagery 100), 13
attributes extracted (not scored), 6 key-feature bullets, keyword coverage on
title/description. All-in overall ≈ 80s (varies as competitor SERPs drift).

---

## 5. Admin

> **Nav changed (session #2):** the Admin rail now shows only **User Activity** and
> **System Activity** (see §7). The per-table screens below still exist as routes
> (and User Activity rolls them all into one read-only page) but are no longer in the
> nav. The bullets below describe those still-live screens.

- **Who:** the `ADMIN_EMAILS` allowlist in `.env` (server-side; `security.is_admin`
  / `admin_required` fail closed → 403 / sign-in redirect). Both `ricksauls@cox.net`
  and `ricksauls1@gmail.com` are admins.
- **Rail Admin section** (below Credits, admins only): **User Activity**
  (`/admin/activity`) + **System Activity** (`/admin/system-activity`, stub). The
  per-table admin routes (Users, Items scored, Copy created, CI Snapshots/Monitoring,
  Messages) are unlinked but intact. `_inject_admin_context` now provides only
  `is_admin` + `admin_new_user_count` (the topbar badges); the per-table rail counts
  were retired.
- **Users screen** (`/admin/users`): table of all users; per-row **Delete**
  (POST + CSRF, blocks self-delete, cascades the user's scored items; confirm
  dialog via `static/js/admin_users.js`).
- **Items scored** (`/admin/items`): recent items across all users (item, product
  title, submitter email, status, score, "Ran" time).
- **Copy created** (`/admin/copy`): recent copy items across all users (item,
  product title, submitter email, status, current + projected score, "Ran"
  time). Backed by `copy_jobs.count_copy_items` / `list_copy_items`.
- **Messages** (`/admin/messages`): the Contact Us inbox — every user thread,
  unread first, with owner email + category + status. Open one (`/admin/messages/
  <id>`) to reply as the team or close/reopen. Backed by `messages.list_all_threads`
  / `count_unread_for_admin`; the two admins share one inbox (read state is shared,
  keyed per-side by message id). See §9 gotcha on read tracking.
- **New-user notification** (topbar, admins only): counts users who signed up
  since the admin's **previous login** (`users.last_login_at` / `prev_login_at`,
  stamped on every sign-in). First-ever login falls back to account-creation time.
- **Message notification** (topbar, everyone): unread-message count — the shared
  inbox count for admins, the user's own unread replies otherwise. Injected by the
  `_inject_message_context` processor for every signed-in user.

---

## 6. Parallelism (measured; important)

The droplet has **~2 GB RAM total, ~1.1 GB free**, shared with the WM app. Each
headed-Chrome worker uses ~300–500 MB. **Measured 2026-08-21:** 2 workers dropped
free RAM to ~89 MB; **5 would OOM** the shared services — do **not** run 5 here.
The 2-worker throughput gain was modest (~1.3–1.5×) because the per-worker 8–16 s
fetch delay caps the rate. **Decision: keep the single worker + the keyword
cache** (the cache is the real, safe speedup). For genuine parallelism, **resize
the droplet to ~4 GB first**, then wire a systemd worker-pool (template unit) sized
to the RAM. The queue claim (`jobs.claim_next`) is already concurrency-safe.

---

## 7. Known gaps / next-up roadmap

**Session 2026-08-26 (session 6 — all live on main + deployed).** _Focus: two new
Competitive Intelligence report sections — brand-level Share of Digital Shelf, and
Brand Advertising Presence (headline + sponsored-video ad detection via a residential
proxy) — plus UI polish._ Read this note first. Commits `edef727`..`67ded5b`.

- **Brand-level Share of Digital Shelf (new section, end of snapshot + monitoring).**
  The existing "Overall Share of Digital Shelf" is **tracked-item-only** (a brand's
  tracked SKUs' slots ÷ all placements). The new **"Brand Share of Digital Shelf"**
  counts **all** of a brand's page-1 cards (tracked + untracked SKUs) ÷ the whole
  shelf — the whole-brand view, so competitors whose SKUs aren't individually tracked
  finally show their real presence. **No new scraping/schema**: the scraper already
  brand-attributes every card, so this reads `ci_search_results` directly.
  `ci_analysis.snapshot_brand_share_of_shelf` / `monitoring_brand_share_of_shelf`
  (+ shared `_shape_share_rows`, `_brand_level_counts`, `_assemble_share_over_period`
  — the old tracked-only functions were refactored onto the same helpers so they
  can't drift). Rendered on the page (`ci_snapshot_results.html` inline + the shared
  `_ci_results_sections.html` partial with delta/trend for monitoring) and in the PDF
  (`_ci_results_flow`, its **own page** via a PageBreak). `pages._snapshot_data` /
  `_monitoring_data` expose `brand_sos_summary`; routes pass an independent chart scale.
- **Brand Advertising Presence (new section + whole new capability).** Detects the
  search page's **headline ad** (Sponsored Brand Ad, `div[data-testid="sba-container"]`;
  brand from its "Sponsored by <brand>" line / logo alt) and **sponsored video ad**
  (`div[data-testid="VideoPlayerWrapper"]`), screenshots each creative, attributes it
  to a tracked brand by name, and reports per brand a **count** (one per keyword-
  appearance) + the latest creative thumbnail. Flow: `ci_scraper.scrape_keyword_page()`
  returns `{cards, ads}` from ONE page load (`scrape_keyword_cards` is now a thin
  wrapper); pure `build_ad_rows()` attributes brand (shared `_brand_index`);
  `worker._write_keyword_ads` saves each creative via `ci_images.save_ad_image()`
  (→ `media/ci_ads/`, served same-origin at `/media/ci-ad/<run>/<kw>/<type>`,
  path-guarded) and records rows via `ci_jobs.write_ad_units`; `ci_analysis.
  {snapshot,monitoring}_brand_ads` aggregate; report section via the shared
  `templates/app/_ci_ads.html` macro (page) + `pdf_export._brand_ads_table`
  (PDF, own page, creatives embedded). New table `ci_ad_units` (§2). **Live-validated**
  (runs 17/18): real headline ads captured + attributed (Frank's RedHot/Tabasco →
  tracked; Pace/Ragu/V8 Red → `brand_id` NULL, recorded but not shown).
  - **Requires the residential proxy (§2, §9).** Walmart's ad exchange serves these
    creatives only to residential IPs; the datacenter droplet got **zero** ad fill
    until the Oxylabs proxy was wired in. Ads are client-served, so parse the rendered
    DOM (NOT `__NEXT_DATA__`, whose `adsContext.brand` stays empty).
  - **"Press & Hold" captcha (handled).** A PerimeterX bot-challenge fires
    intermittently during ad capture (it renders in an **iframe**). `ci_scraper.
    _challenge_overlay_present` is iframe-aware (scans frame URLs + text); when a
    challenge is present the creative screenshot is **skipped** (count/brand still
    recorded, no marred image) and the captcha is never interacted with. So clean
    creatives land on un-challenged sessions (accumulating over monitoring's 3×/day
    runs); counts always work. "Capture early" does NOT help — the ad lazy-loads only
    after scroll. See §9.
  - **Video ads:** none seen live yet (campaign-dependent); video brand attribution
    is best-effort (the `<video>` has no brand text; falls back to a "Sponsored by"
    line on an ancestor). Refine when one appears.
- **UI polish.** Contact Us rail item gained a muted sub-label "(questions, issues,
  customizations etc.)"; PDP scoring/copy flashing subtitles now set a time
  expectation ("… this can take up to 1 minute per item"); the One-Time Snapshot
  in-progress subtitle + card now read "Extracting data and creating the report… /
  This can take up to 5 minutes."; and the redundant "Snapshot queued — results appear
  as the worker finishes" flash was removed ("worker" is internal jargon; the page's
  own in-progress message already confirms the run started).
- **Next-ups (carried + new):** the deprecated `actions/checkout` + `actions/
  setup-python` Node-20 bump is still open; PDP Image Set Creation is still the only
  placeholder; **new optional CI enhancement** — raise the ad-creative image hit-rate
  (retry a challenged keyword's capture with a fresh session/IP, or tune Oxylabs
  rotation), and validate video-ad brand attribution once one appears live.

**Session 2026-08-26 (session 5 — all live on main + deployed).** _Focus: the
dashboard body rebuilt into activity tables, the "View All … Activity" navigation,
run grouping, and closing the worker orphan-reclaim gap._ Read this note first.

- **Worker orphan-reclaim — CLOSED (the prior session's open item).** The scoring
  and copy queues now self-heal on startup, matching the CI queue. New
  `jobs.reclaim_orphaned_items` (fails rows stuck `scoring`) and
  `copy_jobs.reclaim_orphaned_copy_items` (fails rows stuck `fetching`/
  `generating`) run at `worker.main` alongside `ci_jobs.reclaim_orphaned_runs`.
  Orphans are marked `error` (not requeued) so a poison-pill row can't re-crash the
  worker every restart; the user re-runs deliberately. On the very first deploy it
  caught 1 real stranded scoring item on the droplet.
- **Dashboard body rebuilt → five "this month" activity tables.** Replaced the demo
  "Products losing ground" table + demo sidebar (and the "Export report" button)
  with five per-user, current-month tables: **PDP Scored** (image·date·brand·
  product·score), **Copy Created** and **Image Sets Created** (same, no score;
  Image Sets is an empty-state placeholder until that feature ships), **CI One-Time
  Snapshot** and **CI Daily Monitoring** (name·date·my-brand·my-items·competitor-
  brands·competitor-items). The KPI card **"PDP's Scored → N this month" counts
  distinct *products*** while the table counts *rows* (one per scoring run) — a
  re-scored product piles up as multiple rows, so the two legitimately differ; this
  is expected, not a bug.
- **Table UX (all in `static/js/dashboard.js` + `_dash_tables.html` macros):**
  **collapsible** (native `<details open>`, no JS), **column-sortable** (click a
  header; ▲/▼ toggles direction; numeric-aware via a whole-string `Number()` test
  so ISO dates sort as text; date cells carry the raw ISO in `data-sort` since the
  visible "Aug 26" has no year), **10-row cap** with the rest scrolling under a
  sticky header (JS sets `max-height` from the first 10 rows on bodies flagged
  `.dash-body-capped`), **single-line bold black titles**, **uniform 13px row
  font**, and a **thumbnail hover-preview** (a `position:fixed` 240px image on
  `<body>` so table overflow can't clip it).
- **Product-image cache extended to scored/copy.** `PdpRecord.main_image_url` now
  carries the PDP main-image URL through `fetch_pdp`; the worker caches it on fetch
  via the **item-id-keyed** `ci_images` cache (shared with CI — a scored item and a
  CI product with the same Walmart id share one file), served same-origin at
  `/media/ci-product/<id>`. Backfilled the 6 existing prod scored/copy items on the
  droplet (see §9 for the one-off script pattern). Older rows show a placeholder
  until re-run or backfilled.
- **Row click → open that run's results.** Each table row is an `<a>` linking to
  the activity's results. Scored/copy rows go through new per-item routes
  `pdp_scoring_item` / `pdp_copy_item` (`/app/pdp-scoring/item/<id>`,
  `/app/pdp-copy/item/<id>`) which point the **session batch** at the run and reuse
  the existing results page (polling/PDF/layout unchanged); CI snapshot rows →
  `ci_snapshot_results(group_id)`, monitoring rows → `ci_view(group_id)`.
  Ownership-checked → a foreign/missing id 404s.
- **A click opens the *whole run*, not just the one item** — new nullable
  **`batch_id`** column on `scored_items` + `copy_items` (in `_SCHEMA` + additive
  `db._migrate`). `enqueue_items` / `enqueue_copy_items` stamp one `uuid4` per
  submission; `jobs.batch_ids_for_item` / `copy_jobs.batch_ids_for_copy_item`
  return a run's sibling ids. **Backfill:** existing prod rows were grouped by
  identical `created_at` second (items enqueued together share it; separate runs
  are minutes apart) — 7 multi-item runs reconstructed for `ricksauls1@gmail.com`.
  Only fills NULL `batch_id`; new runs group natively.
- **Per-table "View all" → all-time screens.** Each dashboard table carries a
  **View all** link (shared `.wbtn.wbtn-secondary` grey button) to
  `/app/activity/<kind>` (`activity_all.html`) showing that one activity all-time
  (uncapped). Kinds: `scored`, `copy`, `images`, `ci-snapshot`, `ci-monitoring`;
  unknown kinds 404. The dashboard vs all-time split is driven by an optional
  `since` on the four list helpers (renamed `jobs.list_scored_activity` /
  `copy_jobs.list_copy_activity`; the two `ci_jobs.list_*_activity_for_user` gained
  optional `since`). `_activity_rows(kind, since)` is the single shaping entry point
  (used by dashboard + both View-All screens + the two combined screens below).
- **Two combined "View All … Activity" screens.** **View All Content Activity**
  (`/app/content-activity`, rail item under Content Studio) shows the 3 PDP tables
  all-time; **View All Competitive Intelligence Activity**
  (`/app/competitive-intel/activity`, rail item under CI) shows the 2 CI tables
  all-time. Both are dashboard-style and **capped at 10 rows**. Adding the CI screen
  **removed the old View Snapshot / View Monitoring rail items** (routes intact —
  see the §3 Nav note).
- **Shared markup + files.** Table macros live in
  `templates/app/_dash_tables.html` (`product_table`, `ci_table`, `_summary`;
  `all_url`/`cap`/optional `sub` params; rows render as `<a>` when a `result_url`
  is present). New templates: `activity_all.html`, `content_activity.html`,
  `ci_activity.html`. New static: `static/js/dashboard.js`.
- **Infra note:** a GitHub Actions **major outage** stalled deploys for hours
  mid-session (queued runs, dropped `workflow_run` events). It recovered and
  everything shipped. See the top-of-file heads-up.
- **Next-ups still open (carried):** the deprecated `actions/checkout` +
  `actions/setup-python` Node-20 bump (§7 item 6); PDP Image Set Creation is still
  the only placeholder; the AI qualitative pass, attribute completeness %, and CI
  next-ups below are unchanged.

**Session 2026-08-26 (session 4 — all live on main + deployed).** _Focus: brand
capture, dashboard, a full rebuild of the Daily Monitoring results into calendar
period-over-period, and a new View Snapshot menu._ Read this note first.

- **Brand capture (both sources) + brand backfill.** Scored/copy items now store a
  `brand` (nullable `TEXT` on `scored_items` + `copy_items`, in `_SCHEMA` + additive
  `db._migrate`). Two sources: (a) the worker reads `product.brand` from the PDP
  (`fetch._extract_brand`; `PdpRecord.brand`), and (b) the **Copy** intake form has
  an optional Brand field (`pdp.clean_brand`; trim + 120-char cap). **The Scoring
  intake has no Brand field** (removed at the user's request) — scored items get
  their brand only from the PDP. **Reconciliation: the user-entered brand wins** —
  the worker fills the scraped brand only where the column is blank
  (`brand = COALESCE(NULLIF(brand,''), ?)` in `jobs.save_result` +
  `copy_jobs.save_current_copy`). The scoring→copy cross-link carries the brand
  forward. **Backfill:** the 3 existing prod products were hand-set on the droplet
  (10294528=Tabasco, 20857711518=PLERISE, 2165321927=F.U. Larry's) via a
  parameterized `UPDATE`; older rows without a brand stay NULL until re-run.
- **Dashboard.** Now **6 KPI cards** (added **One-Time Snapshot** =
  `ci_jobs.count_snapshot_runs_for_user`, **Daily Monitoring** =
  `ci_config.count_monitoring_groups_for_user`; both with a this-month figure);
  the KPI grid is `repeat(6,1fr)` with 2-line-reserved card titles (steps to 3 then
  2 cols responsively). Portfolio **subtitle** = `"<N> brands · <M> products · As of
  <signup date>"` (brands via `jobs.count_managed_brands`, case-insensitive distinct
  across both tables; `_format_signup_date`). Breadcrumb now shows **"Dashboard"**;
  the **Add product** button was removed.
- **View Snapshot** (new menu + screen). The snapshot counterpart to View
  Monitoring: a group dropdown → the 5 snapshot sections (no trends). The 5-section
  markup now lives in one shared partial **`templates/app/_ci_results_sections.html`**
  (param `show_trend`), included by `ci_view_snapshot.html` (False) **and**
  `ci_view.html` (True). Route `pages.ci_view_snapshot`; rail order is now the
  symmetric **One-Time Snapshot → View Snapshot, Daily Monitoring → View Monitoring**.
- **Daily Monitoring view rebuilt into calendar period-over-period (the big one).**
  The View Monitoring tables no longer show a single run — they **aggregate over the
  last *completed* calendar period** and compare to the one before:
  - **Periods** (`ci_analysis.PERIODS` = `wow/mom/qoq/yoy`, labels Week/Month/
    Quarter/Year): **Week = Mon–Sun**; Month/Quarter/Year are calendar; always the
    last *completed* one (never the in-progress current). `period_bounds(period,
    index, today)` (index 0 = last completed, 1 = prior), `period_label`
    ("Aug 17–23", "Jul 2026", "Q2 2026", "2025").
  - **Availability gating:** a period button is a link only once its most recent
    completed period holds data — `period_has_data` / `available_periods`; the route
    falls back to the first available period, disables the rest (dashed/faded
    `.ci-periods .disabled`), and shows a "no completed period yet" empty state when
    none qualify.
  - **Each table = aggregate + "vs prior" delta + per-period trend.** Assemblers
    `ci_analysis.monitoring_{avg_rank, rank_by_keyword, share_of_shelf,
    share_by_keyword}` return the current-period rows (via `_*_range` date-scoped
    aggregations that mirror the run-scoped `snapshot_*`), plus `delta` vs the prior
    period (rank: prior−current so **+ = improved/moved up**; share: current−prior
    pts so **+ = gained**), plus `trend`/`trend_dates` = **one point per completed
    period** (`_period_trend`, last `TREND_PERIODS`=6, oldest→newest; `trend_dates`
    are the period labels shown in the hover tooltip). Placement map from the
    period's avg via `monitoring_placement_map`. Subtitle names the window
    ("Aggregated over Jul 2026 · vs Jun 2026").
  - **Sparkline hover tooltip** (all `.ci-spark` in `ci_charts.js`): each point has a
    transparent hit-circle + a shared `.ci-spark-tip` div; shows **"label · value"**
    — `#n` for rank (`data-unit="rank"`), `n%` for share (`data-unit="share"` +
    `data-better="high"` so a rising share renders up). Point labels come from
    `data-dates` (period labels; a raw ISO date is formatted `Mon D`, anything else
    shown as-is).
  - **Monitoring PDF matches** (`pdf_export.build_ci_monitoring_pdf`): same 5
    sections via the shared `_ci_results_flow(with_trend=True)`, now with a "vs
    prior" delta column (`_delta_cell`) and a reportlab per-period sparkline
    (`_sparkline_drawing`); header names the period. Route `ci_view_pdf` passes
    `period_label`/`prior_label`.
  - **Removed dead code** in this rework: the old rolling-window functions
    (`share_of_shelf_summary`, `rank_summary`, `share_of_shelf_trend`,
    `get_date_range`/`get_prior_date_range`, `PERIOD_DAYS`, the daily `*_trend_*`
    helpers, `_daily_total_share_by_brand`, `rank_trend`) and `ci_jobs.latest_done_run`.
    The dependency-free multi-line trend **chart** (`drawChart`/`[data-ci-chart]` in
    `ci_charts.js`, and `share_of_shelf_trend`) is gone — the sparklines replaced it.
    Note `snapshot_rank` remains (test-only, left as-is).
- **Breadcrumbs / labels:** Content Studio screens lead with **"Content Studio · …"**;
  Daily Monitoring setup breadcrumb is **"Competitive Intelligence · Daily Monitoring"**.
- **Ranking-semantics recap (unchanged, worth knowing):** ranking counts only the
  group's **tracked** items (`ci_analysis._TRACKED_ITEMS_FILTER`); share of shelf
  counts every SKU (brand-level). A tracked item's *sponsored* slot has an opaque id
  that can't be tied back, so only its organic placements count toward ranking.
- **OPEN — worker orphan-reclaim (operational, not a feature).** The scoring/copy
  queues have no startup reclaim, so a deploy (or OOM) that kills the worker
  mid-fetch strands that row in `scoring`/`fetching`/`generating` forever; the CI
  queue self-heals (`ci_jobs.reclaim_orphaned_runs` at `worker.main`). Add the
  equivalent for `scored_items`/`copy_items` at worker startup. We hit this once this
  session (a deploy restarted the worker mid-fetch of a scored item; the user re-ran
  it). Root cause is the ~2 GB RAM; the reclaim is a resilience fix.

**Session 2026-08-24 #2 (all live on main; 16 commits `fba5d6f`..`a4b4185`).**
_Focus: CI ranking/share semantics, admin consolidation, dashboard._
- **CI ranking + share are now TRACKED-ITEM-ONLY (important semantic change).**
  Search Ranking, Overall Search Ranking, and Share of Digital Shelf used to count
  every SKU the brand-name matcher swept onto the page; they now count only the
  group's **tracked products** (by Walmart item id). A brand's untracked SKUs no
  longer drag its ranking down or inflate its share. Shared SQL fragment
  `ci_analysis._TRACKED_ITEMS_FILTER`; applied in `snapshot_rank_by_keyword_brand`,
  `snapshot_brand_avg_rank`, `rank_summary`. Share rollup now buckets a placement
  under its brand only if it's the tracked item, else "Other" — so the denominator
  stays the whole page-1 shelf (share = tracked item's slots ÷ all placements).
  `ci_jobs.write_share_of_search` takes a new `tracked_item_ids` arg (worker passes
  `set(item_map)`). **Only affects new runs;** run 5's share was hand-recomputed on
  the droplet. `snapshot_rank` (best-position split) is untouched and test-only.
- **Sponsored slots now attribute to the tracked item (title matching).** Walmart
  gives a product a *different opaque id* in a sponsored slot, so id/URL matching
  never reached it. `ci_scraper.build_result_rows` now learns each tracked product's
  title from its id-matched organic card, then ties a sponsored card with the
  identical title back to that tracked item (storing the tracked numeric id so the
  ranking join reaches it). Only the tracked SKU's own sponsored slots are tied back.
  Ported from the WM SOV tool's sponsored-variant name matching. **New runs only.**
- **Orphaned-run recovery.** A worker killed mid-run (OOM on the ~2 GB droplet) left
  its run stuck `running`, which made `enqueue_monitoring` skip the group every slot
  (so a scheduled run silently never fired). `ci_jobs.reclaim_orphaned_runs` (called
  at `worker.main` startup) marks any leftover `running` run as `error`, unblocking
  the queue. Assumes one worker (see §6). This is a *resilience* fix — the root cause
  is RAM; resize to ~4 GB for reliability.
- **"Latest run" shows Central fire-time even on failure.** New
  `ci_analysis.format_run_time_cst` (UTC→CST, DST-correct); `_run_when_cst` uses the
  start/enqueue time, not `finished_at`.
- **CI "Daily Monitoring" rename** (was "Monitoring Setup") — nav + page + cross-link.
  Route names unchanged (`ci_monitoring_home`).
- **User-facing "scraping" → "extracting data"** across CI templates (running-run
  status, config hint, help). Internal names (`ci_scraper`, `scraped_at`) unchanged.
- **CI snapshot PDF polish:** page breaks after the config summary and after Search
  Ranking; more spacing around the Overall Search Ranking table; product grid in
  "What this group tracks" left-aligned with breathing room above.
- **Admin consolidated.** New read-only **User Activity** screen (`/admin/activity`,
  `admin_activity.html`) rolls every admin table into collapsible native `<details>`
  sections (CSP-safe, no JS): Messages (open by default, even when empty), Users,
  Items Scored, Copy Created, Image Sets Created (empty — feature not built), CI
  Snapshots Ran, CI Monitoring Scheduled. The **Admin rail is now just User Activity
  + System Activity** (`/admin/system-activity`, a placeholder stub to build out
  later); the individual per-table screens still exist as routes (User Activity links
  to the message thread view) but are unlinked from the nav. Retired the per-table
  rail count context vars (topbar message + new-user badges unaffected). New admin
  data helpers: `ci_jobs.{count,list}_snapshot_runs`,
  `ci_config.{count_monitoring_groups,list_monitoring_groups_admin}`.
- **Dashboard personalized + real KPIs.** Topbar breadcrumb name removed on the
  dashboard; the Portfolio header shows the signed-in user (was the demo agency
  name). The four KPI cards are now real per-user unique-product counts, each with a
  this-month figure: **Products managed** (scored ∪ copy, item-id deduped), **PDP's
  scored**, **PDP's copy created**, **PDP's images created** (0 until built). Helpers
  `jobs.count_managed_products` / `jobs.count_scored_products` /
  `copy_jobs.count_copy_products`, all with an optional `since` (ISO date) for the
  monthly figure. **Note:** "scored/copy created" counts a PDP once it's been
  *submitted* to that pipeline (any status), consistent with "products managed"; the
  user may later want *completed*-only — a one-line status filter per helper.
- **Still pending (carried to next session):** the dashboard **"brands · products"
  subtitle line** — see §11.

**Session 2026-08-24 #1 (all live on main; 7 commits `2759bbe`..`5eb45ff`).**
- **Contact Us in-app messaging** — two-way threaded support (user threads +
  category, admin inbox, replies, close/reopen), unread badges in topbar + rail
  for both sides. New `app/messages.py`, tables `message_threads`/`messages`,
  templates `contact_home`/`contact_thread`/`admin_messages`, `_inject_message_context`.
- **CI snapshot PDF** now includes the **SoS stacked-bar chart** and, under Overall
  Search Ranking, the **placement-map grid** (both reportlab shapes).
- **Placement map** on the snapshot page + PDF: each brand's overall avg rank lit
  on a page-1 result grid (mine red, competitors ink, ties split, exact avg on
  tile). `ci_analysis.build_rank_placement_map`.
- **Tracked-product main images** in "What this group tracks" (page grid + PDF
  thumbnails), mine-first, with breathing room. Worker caches each product image
  once (`ci_images.py`, `worker._cache_ci_product_images`), served same-origin from
  `/media/ci-product/<id>` (CSP `img-src 'self'`). **Note:** existing groups show
  placeholders until their next run caches images (I hand-cached the "Tabasco
  Original Hot Sauce" group's 4 items live on 2026-08-24).
- **Schedule for monitoring** button on snapshot cards (clones the set into a
  monitoring group + baseline run).

**Session 2026-08-23 (all live on main).** Focused on the CI **One-Time Snapshot**
results page + UX polish:
- **Rebuilt the snapshot results page** into five sections (see §1 for the order
  and semantics). New aggregations `snapshot_brand_avg_rank`,
  `snapshot_rank_by_keyword_brand`, `snapshot_share_by_keyword` in `ci_analysis.py`;
  page + PDF share `pages._snapshot_data()`.
- **Share-of-shelf stacked bar chart** (CSS/HTML, CSP-safe, no library): segments
  are the table's **organic/sponsored share %** (raw counts are organic-heavy and
  mislead); bars fill the plot width, are labeled with total-share %, and
  bottom-align via a fixed-height `.sos-col-plot` (so a wrapping brand name can't
  lift a bar). Styles: `.sos-*`, `.ci-summary`, `.header-actions` in `workspace.css`.
- **CI snapshot PDF now mirrors the page** (`build_ci_snapshot_pdf`, keyword-only
  args) — summary + both ranking tables + both share tables; chart omitted.
- **Button-contrast fixes:** `wbtn-primary` buttons were being restyled to
  low-contrast text by `.ci-periods a`; moved them to the new **`.header-actions`**
  wrapper (Download PDF on snapshot + View results on the config header; View
  Monitoring header de-inlined). See §9.
- **UX:** subtitle **flashes while scraping** (same cue as PDP scoring); **item-URL
  fields autofill** `pdp.WALMART_IP_PREFIX` (`https://www.walmart.com/ip/`) on PDP
  scoring/copy + CI product so the user only appends the number (`collect_items`
  now requires an item number and silently skips a bare prefix; see §9); CI
  **keywords accept a comma-separated list** (add several at once, IDOR-guarded
  route, note in the form).

Done earlier (kept for context): worker install; attribute extraction;
key-feature extraction; keyword coverage Phase 1 + category cache; main-image
white-bg check; admin screens; PDF export; HTTPS/cache-busting fixes;
**PDP Copy Content Creation** (AI copy rewrite — the generation half of the AI
pass; `app/copygen.py`, `app/copy_jobs.py`, `copy_items` table, worker two-phase
fetch→generate, results with projected-score delta, scoring cross-link).
Also this cycle (2026-08-22, all live on main): **copy CSV cap 200→100** +
Imagery card copy; **worker schema-race fix** (`db.ensure_schema`, see §9); **copy
results PDF export**; **admin "Copy created" screen** (`/admin/copy`).
**Competitive Intelligence** shipped end-to-end this cycle: built the whole
feature (7 `ci_*` tables, config CRUD, search scraper, worker CI queue, 3×/day
monitoring timers, analysis + dashboards); then **restructured into 3 menus**
(One-Time Snapshot / Monitoring Setup / View Monitoring) with per-mode groups and
two CI PDF exports; **reorganized the rail** (Dashboard / Content Studio / CI);
and fixed brand attribution — **numeric item id parsed from the card URL** (the
`data-item-id` is opaque) plus a **brand-name fallback so sponsored slots attribute**,
with **brand-level Search Ranking incl. competitors** and the page-1 placement
count shown. Also tweaked URL-field hint copy ("…with item number at the end").

1. **AI pass — remaining qualitative half (needs Claude + `ANTHROPIC_API_KEY`).**
   The **copy rewrite** half now ships (see above). Still open: keyword
   *quality*/placement judgment folded back into *scoring*, and **vision** for
   infographic/lifestyle image quality. Use Claude (see the `claude-api` skill for
   current model IDs; note `temperature` is removed on Opus 5/4.8 — get
   determinism from output caching, not temp). Also: a human "approved" gate on
   the discovered keyword set (today top-N auto-approved); smarter seed derivation.
   **Copy-gen Phase 2 next-ups:** cache generated copy by content hash +
   "Regenerate"; prompt-cache the stable prefix; PDF/export of the rewrite;
   optional inline editing of the generated copy.
2. **Attribute completeness %** — today it's a raw count proxy; wire a
   per-category expected-attribute schema to make it a true % (and re-enable the
   Attributes dimension when ready — it's paused, not removed).
3. **Competitive benchmarking** — score top-N competitors for the item's head
   terms and show the gap to the category leader.
4. **Other nav screens** — PDP Image Set Creation is the only remaining
   placeholder (`href="#"`) under Content Studio. (PDP Copy Content Creation and
   all three Competitive Intelligence flows are built; PDF export is done for CI.)
   **CI next-ups:** surface the `is_new_sku` flag (already captured) as a
   new-competitor-SKU alert; competitive benchmarking on the CI data (gap to the
   category leader per keyword); optional CSV export; richer rank trends as
   monitoring accumulates multi-day history; consider a per-keyword page-1 depth
   cap (today the scraper takes all cards the item-stack yields, ~50/keyword).
5. **Nice-to-haves:** retry `blocked` items, a scoring history view.
6. **CI/infra maintenance:** the Actions runs warn that `actions/checkout` and
   `actions/setup-python` still target the **deprecated Node 20** (GitHub is
   force-running them on Node 24 for now). Bump those action versions in a small
   PR before a runner change breaks the pipeline. (The **View-Monitoring page** and
   the **monitoring PDF** were rebuilt 2026-08-25 to mirror the snapshot layout plus
   trend lines — see the session note below.)
7. **Snapshot ranking semantics to keep in mind:** "Overall Search Ranking" is a
   two-stage average (avg per keyword, then across the terms a brand placed on) so
   it reconciles with the per-keyword "Search Ranking" table; absent terms are
   excluded, not penalized. Per-keyword "Share of Digital Shelf" uses **each
   keyword's own slots** as the denominator (rows sum to ~100% incl. "Other").

---

## 8. Local development

```bash
cd ~/Desktop/ClaudeStuff/ecomm-copilot-clean
.venv/bin/pip install -r requirements-dev.txt   # playwright, Pillow, reportlab
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

- Local `.env`: `SESSION_COOKIE_SECURE=false` (session cookie over
  `http://localhost`); `DATABASE_URL` empty → local `app.db` (gitignored);
  `ADMIN_EMAILS` unset locally → no admins (set it in a test to exercise admin).
- Preview server: Browser-pane `preview_start` with config `ecomm-copilot-preview`
  (port 5050). **Templates are cached — restart the preview server after editing
  a `.html`.** CSS/JS are static. (Note: the public surface is near-black, so
  screenshots of dark sections can look blank — verify via `read_page`/text.)
- Live browser fetch / keyword mining / white-bg can't run from the Mac (Walmart
  blocks it, no local Xvfb). Test parsing/scoring with fixtures; validate live on
  the droplet over SSH, e.g.:
  `ssh droplet-deploy 'cd /home/deploy/apps/ecomm-copilot && env DISPLAY=:99 venv/bin/python -c "from app.fetch import fetch_pdp; r=fetch_pdp(\"https://www.walmart.com/ip/10294528\",\"10294528\"); print(r.title, r.main_image_white_bg)"'`

---

## 9. Gotchas learned (save yourself the debugging)

- **Strict CSP** (`script-src 'self'`): no inline scripts/handlers. Per-screen JS
  is an external file under `/static/js`, loaded via the `scripts` block.
- **Static cache-busting:** nginx serves `/static` with `expires 30d`. Reference
  assets in templates via `static_url('…')` (context-processor helper), **never**
  raw `url_for('static', …)` — `static_url` appends `?v=<mtime>` so edits reach
  users. Symptom if you forget: a deployed CSS change needs a hard refresh.
  (Favicons are cached even more aggressively — expect a hard refresh there.)
- **CI installs pinned `requirements-dev.txt`** — don't let tool versions float;
  new deps must pass `pip-audit` or CI blocks the deploy.
- **Walmart bot block:** direct `requests` → block page; only headed Chrome under
  Xvfb gets through. Autocomplete (typeahead API) is plain HTTP and works.
- **Deploy host key:** pin the **ECDSA** fingerprint, not ed25519.
- **Root on the droplet:** deploy sudo password is lost — DO web Console for root
  steps (`passwd deploy` there to reset if desired). SSH as `deploy` is
  passwordless (key), so day-to-day droplet work doesn't need root.
- **`setup-droplet.sh` vs TLS (fixed):** certbot writes the 443 block into the
  nginx site file in place; the script used to overwrite it every run and drop
  HTTPS (→ `ERR_CERT_COMMON_NAME_INVALID`, serving the default `7bcrfp` cert). It
  now skips the overwrite when a 443 block exists. If you hit the cert error, fix
  (root, DO Console — cert still exists):
  `certbot install --cert-name ecomm-copilot.com --nginx` then
  `nginx -t && systemctl reload nginx`.
- **DB migrations:** `CREATE TABLE IF NOT EXISTS` won't alter an existing table
  and SQLite has no `ADD COLUMN IF NOT EXISTS` — add new columns in `db._migrate`
  (checks `PRAGMA table_info`). New *tables* can go straight in `_SCHEMA`.
- **Worker ensures its own schema:** `db.ensure_schema(conn)` (idempotent
  `_SCHEMA` + `_migrate`) is called by BOTH `init_db` (web) and `worker.connect()`.
  This exists because on the copy_items deploy the worker restarted **before** the
  web app created the table and crashed with `no such table: copy_items` (it
  self-healed after one systemd restart). Don't reintroduce a dependence on the
  web app initializing the DB first — new tables are safe for the worker now.
- **`_row_view` in `routes/pages.py`** shapes scored_items rows for the
  results template/JSON — add any new column there too, or it won't render
  (bit us with `title`).
- **Concurrent sessions:** avoid two chats committing in this folder at once —
  they race.
- **`.ci-periods a` restyles anchors:** the period-pill rule (`.ci-periods a`,
  specificity 0,1,1) beats `.wbtn-primary` (0,1,0) and forces low-contrast text
  onto a primary button's dark fill. Put `.wbtn` header buttons in **`.header-actions`**,
  not `.ci-periods` (which is only for the View-Monitoring period pills). This bit
  us three times (Download PDF, View results, View-Monitoring header).
- **Item-URL fields autofill a prefix:** `pdp.WALMART_IP_PREFIX` prefills the URL
  inputs and `intake.js` keeps a matching `URL_PREFIX` copy (keep them in sync).
  `pdp.collect_items` now **requires an item number** — a URL that is just the
  prefix (an untouched autofill row) is skipped silently; an edited-but-numberless
  URL is a reject. So item-less URLs are no longer accepted for scoring/copy.
- **CI snapshot page ↔ PDF:** both render from `pages._snapshot_data()`. When you
  add or reshape a snapshot section, update that helper (and `build_ci_snapshot_pdf`)
  or the page and PDF drift.
- **Message read tracking is by *message id*, not timestamp:** `message_threads`
  stores `user_last_read_msg_id` / `admin_last_read_msg_id`; a thread is unread for
  a side when a message from the *other* side has a larger id. This was a
  deliberate fix — a timestamp compare (`created_at > last_read_at`) misses a reply
  created in the *same second* as a read (`datetime('now')` is 1-second resolution),
  so unread badges would silently not appear. Don't switch it back to timestamps.
- **Product images are worker-cached, so they lag first use:** a group shows image
  placeholders until a run caches them (`worker._cache_ci_product_images` fetches
  each uncached product's PDP once). To backfill without a full re-run, cache
  directly on the droplet, e.g.:
  `ssh droplet-deploy 'cd /home/deploy/apps/ecomm-copilot && env DISPLAY=:99 venv/bin/python -c "from app.fetch import fetch_main_image_url; from app import ci_images; u=fetch_main_image_url(\"https://www.walmart.com/ip/<ITEM>\",\"<ITEM>\"); print(ci_images.cache_product_image_from_url(\"<ITEM>\", u))"'`
  Served from `/media/ci-product/<id>` (same-origin; CSP already allows `img-src 'self'`).
  The `sqlite3` CLI is **not** installed on the droplet — inspect the DB with
  `venv/bin/python -c "import sqlite3; ..."` instead.
- **CI ad capture needs the residential proxy, and it's metered (session 6):**
  Walmart serves headline/video ads only to residential IPs. `WALMART_PROXY_*` in the
  droplet `.env` (live Oxylabs) makes `ci_scraper.scrape_keyword_page` route through
  it (config-gated: unset → direct scrape, no ads). It bills per-GB and runs on
  **every** CI scrape, incl. the 3×/day monitoring timers. To check the exit is
  residential: `ssh droplet-deploy 'set -a; . /home/deploy/apps/ecomm-copilot/.env; set +a; curl -s -x "$WALMART_PROXY_SERVER" -U "$WALMART_PROXY_USERNAME:$WALMART_PROXY_PASSWORD" https://ip.oxylabs.io/location'`.
- **"Press & Hold" captcha renders in an IFRAME:** a PerimeterX bot-challenge fires
  intermittently *during ad capture* (after the load-time block check). Its text
  isn't in the main-page body (it's in an iframe), so `ci_scraper.
  _challenge_overlay_present` scans **frame URLs + frame text** too; a challenged
  capture **skips the creative screenshot** (count/brand kept, no marred image) and
  **never touches the captcha** (per the no-CAPTCHA rule). Marred creatives already
  stored can be cleaned by nulling `ci_ad_units.image_path` for the run + deleting
  `media/ci_ads/<run>_*.jpg`.
- **Deploy restarts the worker → orphans an in-flight CI run** (reclaim marks it
  `error`). Before pushing while runs may be happening, check for an active run first
  (see the top-of-file "Deploy caution" one-liner).
- **Recoloring a logo's text when the ring's own shading is nearly the same
  color (session 7):** don't try to protect the ring by *position* (a bounding
  box or circle) — any fixed-radius margin either leaves a decorative
  accent-shape un-recolored (looks like a hole punched in the letters) or, if
  tightened, eats into the letterform where it intentionally sits close to the
  ring. Classify by **color** instead: sample real pixels to find where the
  letter's dark fill and the ring's darkest shadow actually diverge in HLS
  lightness (here: letters ≤0.13, ring shadow ≥0.15 — a real but narrow gap),
  threshold on that, and use **zero dilation** on the protected zone (any
  margin re-opens a notch where the letterform intentionally sits close to the
  ring) — clean up stray antialiased pixels with a small closing on the
  recolor mask instead. Reusable script: `scripts/recolor_wordmark.py`. See
  §12 for the full story.
- **Templates/CSS edits need a dev-server restart to appear** (`python
  app.run()` doesn't set `TEMPLATES_AUTO_RELOAD`, so Jinja's compiled-template
  cache goes stale) — a browser refresh alone won't show the change. Swapping a
  static *file* (an image, unchanged filename) does not need a restart —
  `static_url()`'s mtime-based cache-buster handles that.

---

## 10. References

- Memory: `project_ecomm_copilot` (auto-loaded) tracks live status.
- WM scraper (fetch method + Xvfb precedent + keyword discovery source):
  `~/Desktop/ClaudeStuff/WM Dot Com Update` — `pdp_scraper.py`,
  `discover_keywords.py`, `score_content.py`, and its `README.md` (xvfb unit).

---

## 11. Brand capture + dashboard "brands · products" subtitle — DONE + DEPLOYED

**Status: live on main and deployed (2026-08-25/26).** The §7 "Session 2026-08-26"
note has the current summary (incl. the Scoring-form field removal and the prod
backfill); the detail below is the original build for reference. Both halves shipped:

**1. Capture the brand when scraping.** `fetch._extract_brand` reads
`product.brand` from `__NEXT_DATA__` (handles the plain-string and nested
`{"name": …}` shapes; empty when absent). `PdpRecord` gained a `brand` field
(`scoring.py`); the worker passes `pdp.brand` into `jobs.save_result` and
`copy_jobs.save_current_copy`.

**2. Capture the brand the user enters at intake.** The **Copy** intake form
(`pdp_copy.html`) has an optional batch-level **Brand** field (above step 1;
`.brand-field`/`.brand-input` in `workspace.css`); the route reads it via
`pdp.clean_brand` (trim + 120-char bound, blank→None) and stores it on every
enqueued row. **The Scoring intake has no brand field** (removed 2026-08-25 at the
user's request) — scored items get their brand only from the PDP on fetch. The
scoring→copy cross-link still carries any brand forward from the scored row.

**Reconciliation rule (important):** the **user-entered brand wins**. The worker
fills the scraped brand only where the column is still blank —
`brand = COALESCE(NULLIF(brand, ''), ?)` in both save functions — so a deliberate
user label is never overwritten by Walmart's PDP value.

**Storage:** nullable `brand TEXT` on **both** `scored_items` and `copy_items`
(added to `_SCHEMA` and idempotently in `db._migrate`; existing rows stay NULL until
re-run). Not added to `_row_view` — brand doesn't render on the results screens.

**Count + subtitle:** `jobs.count_managed_brands(conn, uid, since=None)` =
`COUNT(DISTINCT LOWER(TRIM(brand)))` across `scored_items ∪ copy_items` (case-insensitive
so "Tabasco"/"TABASCO" don't double-count; NULL/blank excluded). The dashboard route
overrides `view_model["agency"]["subtitle"]` to
`f"{brands} brands · {products} products · As of {signup}"` where products reuses
`count_managed_products` and `signup` is `g.user["created_at"]` formatted by
`_format_signup_date` (→ e.g. "Aug 24, 2026"). "Walmart" is dropped. Verified live in
preview reading `0 brands · 0 products · As of Aug 24, 2026`.

**Follow-ups (optional):** existing rows have `brand = NULL` until re-scored, so the
count starts low and grows (accepted). A one-off droplet re-fetch backfill is
possible but not required. The `fixtures.get_dashboard()` demo subtitle string is now
dead for the real page (route always overrides it) but left in place.

---

### Original spec (kept for reference)

Context: prior session personalized the dashboard header and made the four KPI cards
real (see §7 session #2). This was the *remaining* change to the line **above** those
cards — the `<p class="subtitle">` under the Portfolio header, which used to render
demo text: `15 brands · 148 products · Walmart · week of Aug 10`
(`fixtures.get_dashboard()["agency"]["subtitle"]`).

**What the user asked for, exactly:**
1. **Brands** = the number of distinct brands the signed-in user has *scored, created
   copy for, or created creative image sets for*.
2. **Products** = the number of distinct products across the same three activities —
   this is the same figure as the "Products managed" KPI
   (`jobs.count_managed_products(db, uid)`), so reuse it.
3. **Remove "Walmart"** from the line.
4. **Date** → `"As of <signup date>"` where the date is when the user signed up
   (`g.user["created_at"]`, a UTC `YYYY-MM-DD HH:MM:SS` string — format it, e.g.
   `As of Aug 20, 2026`).
   So the line becomes roughly: `<N> brands · <M> products · As of Aug 20, 2026`.

**Agreed approach for brands (user chose "capture the real brand going forward"):**
Scored/copy items do **not** store a brand today (the tables keep only item id, url,
title; `result_json` has no brand). So:
- **Extract the brand during fetch.** Walmart's PDP `__NEXT_DATA__` exposes the brand
  at `props.pageProps.initialData.data.product.brand` (verify the exact key against a
  live PDP — `fetch.py` already reads `product.name` from the same object). Add a
  `brand` field to `PdpRecord` (`app/scoring.py`) and populate it in
  `fetch._load_pdp_data` / `fetch_pdp`.
- **Store it.** Add a nullable `brand TEXT` column to **both** `scored_items` and
  `copy_items` via `db._migrate` (PRAGMA table_info guard — SQLite has no ADD COLUMN
  IF NOT EXISTS; see §9). Write it where the worker saves the fetched record
  (`jobs.save_result` for scoring; `copy_jobs.save_current_copy` for copy). Also add
  it to `_row_view` in `routes/pages.py` if it needs to render anywhere (§9 gotcha).
- **Count it.** Add `jobs.count_managed_brands(conn, user_id)` mirroring
  `count_managed_products` but `COUNT(DISTINCT brand)` across `scored_items` ∪
  `copy_items` (union creative/image-set table in once that ships), excluding NULL/''.
- **Expectation:** existing rows have `brand = NULL` until re-scored, so the brand
  count starts low and grows — this is understood/accepted. (A one-off re-fetch
  backfill on the droplet is possible but not required.)

**Where to wire it:** the dashboard route (`pages.dashboard`) already overrides
`view_model["agency"]["name"]` and rebuilds `view_model["kpis"]`. Override
`view_model["agency"]["subtitle"]` there too, e.g.
`f"{brands} brands · {products} products · As of {signup_str}"`.

**Also still open from earlier (unchanged):** PDP Image Set Creation is still a
placeholder (`href="#"`); the "PDP's images created" KPI and the creative side of
these brand/product counts stay 0 until that feature is built. The dashboard's
"Products losing ground" table and the remaining fixture bits are still demo data.

---

## 12. Session 2026-09-30 — DISCOtech rebrand (committed, not yet pushed)

**Status: visual rebrand complete, verified locally (252 tests pass),
committed to `main` locally.** Touched ~30 templates/CSS/Python files, deleted
2 (old logo/favicon PNGs), added 3 (the final logo/favicon assets) + the
`scripts/` directory. **Not yet pushed** — decide on that first (see the
top-of-file banner).

**Scope agreed with the user:** full rebrand of name/logo/colors/typography/copy
*in the app and docs only*. The GitHub repo name, the droplet's systemd units
(`ecomm-copilot.service`, `ecomm-copilot-worker.service`), the nginx site, env
var names, and the domain (`ecomm-copilot.com`) are all **deliberately
untouched** — that's a separate, later pass to do together with the domain
swap, since it touches shared production infra (see §2/§9). `deploy/DEPLOY.md`
and the systemd-unit comments in `worker.py`/`enqueue_monitoring.py` still say
"ecomm-copilot" on purpose — they describe what's actually running.

### What changed

**1. Text rename.** "ecomm-copilot" → "DISCOtech" across every template
`<title>`, alt text, and visible copy (bulk sed on the common
`— ecomm-copilot{% endblock %}` title suffix, plus a few one-off strings:
`signin.html` "New to...", `contact_thread.html` "...team"). Old tagline "Your
eCommerce Team, Amplified." → "Your eCommerce CoPilot." (landing `<h1>` +
`_rail.html` tagline + footer). `README.md`/`CLAUDE.md` headers → "DISCOtech
(ecomm-copilot)" (keeps the real repo/folder name visible since that hasn't
changed yet). Two Python docstrings (`app/__init__.py`, `app/fixtures.py`)
renamed; the systemd-referencing comments were **not** touched (see above).

**2. Color palette (`tokens.css`).** Every CSS custom-property **name** is
unchanged — only the hex **values** moved — so none of the ~400 `var(--x)` call
sites elsewhere in the CSS needed to change. New values: `--black`→Midnight
`#0B1220`, `--graphite`→Navy `#132A52`, `--mid-gray`→Slate `#64748B`,
`--canvas`→Light Gray `#EAEFF7`, plus a few derived neutrals to fill gaps the
named palette didn't cover (`--faint-gray`, `--cool-gray`, `--border`,
`--divider`, `--placeholder`, `--control-border`, `--row-hover`). One **new**
token, `--accent` (Electric Blue `#2E6DFF`), was split out for the handful of
spots that were always brand-identity, not a warning: `.red-rule`, the
active-nav-item dot (`.nav-item.active .mark`), the landing page's step-number
accent (`.step-num.accent`), and "this is my brand" highlighting in the CI rank
map (`.rankmap-*.mine`). **`--signal-red` was deliberately left red** and kept
every other job — validation errors, `status-blocked`/`status-error` badges,
decline deltas, unread/alert dots, destructive-hover, and generic interactive
hover (sortable headers, "add" links, etc.). Rationale: red is the one hue
users already read as "needs attention"; swapping it for a brand color would
quietly make error states less recognizable. `app/static/js/ci_charts.js`'s
hardcoded hex (`MINE`, grid stroke, axis-label fill) were synced to match so
the dependency-free SVG charts don't drift from the CSS tokens.

**3. Logo — the long way round.** The user went through several rounds of
asset deliveries; what's actually in the repo now is the end state. Read this
if the logo ever needs to be regenerated or a defect shows up again:

- **Only one logo asset ships in the app:** `app/static/img/discotech-wordmark.png`
  (2400×655, white text, transparent bg) — used via plain `<img>` in
  `_rail.html`, `landing.html` (header + footer), `signin.html`, `signup.html`,
  all sized by CSS (`.brand-logo { height: 48–52px; width: auto; }`). No SVG
  ships in the repo — the earlier vector approach (outlined `<text>` paths,
  `inline_svg()` Jinja helper) was tried and then abandoned in favor of this
  raster file; `inline_svg()` was removed from `app/__init__.py` again when
  that happened. Don't reintroduce it without reason.
- **Why raster, not vector:** the user's actual source of truth is an
  AI-rendered master image with a richer glossy/glow 3D "disco ball" look than
  hand-coded SVG gradients can reproduce. The clean master lives at
  `~/Desktop/Work - Income/DISCOtech (ecomm-copilot)/Branding/Web-Ready
  Creative/logos/discotech-wordmark-2400.png` (dark-navy text, meant for a
  *light* background — the opposite of what this app's chrome needs).
- **The recolor problem (the hard part).** The app needs white text on dark
  surfaces, but the source only has dark-navy text. A naive "recolor dark
  pixels to white" pass is unsafe because **the ring's own internal shadow
  band is almost the same dark navy as the letters** — several failed
  attempts before landing on the right method:
  - A rectangular column cut to protect the ring left a visible dark triangular
    notch (a decorative accent flanking the ring) biting into the "C" and "t"
    once their surroundings turned white.
  - A padded circle had the same problem — the accent sat inside the padding.
  - Flood-filling connected components (to tell "ring" from "letter" by
    touching-vs-separate) failed because the ring's soft outer **glow**
    (desaturated, low-opacity) forms a continuous alpha>40 bridge all the way
    to the nearby letters, merging them into one component.
  - **What worked:** classify every pixel by its own HLS color, not its
    position. Sampled real pixels to find the dividing line: letter-navy fill
    tops out at **lightness 0.13**; the ring's own darkest shadow starts at
    **lightness 0.15** — a real, if narrow, gap. A pixel is "ring, protect it"
    when `saturation > 0.30 and lightness > 0.14`; everything else opaque gets
    recolored to white. **No dilation** on that protected zone — even 1px
    re-opens a visible notch at the two spots where the letterform
    intentionally almost touches the ring. A small closing (7×7) on the
    *recolor* mask afterward mops up the last handful of antialiased stray
    pixels without that risk. The working script (verified byte-identical to
    the installed asset) is saved at `scripts/recolor_wordmark.py` — re-run it
    against an updated source master if the logo ever changes again, don't
    re-derive this from scratch. It needs numpy/scipy, which aren't app
    dependencies — see the script's docstring.
  - Verify any future regeneration by scanning for stray navy pixels **outside
    a generous ring-centered exclusion circle** (this bit us once — too tight
    an exclusion radius makes the scan blind to the exact defect near the
    ring). Check all four letter-tip contact points: top and bottom horns of
    "C", left edge and top of "t".
- **The favicon's delivered asset was also broken.** The user's
  `favicons/favicon.ico` was cropped from the full lockup+tagline composite,
  not an isolated icon — it showed fragments of "tech" and "...merce Co..."
  bleeding in at the edges. `favicons/favicon-512.png`, however, **was**
  clean. Fix: built a fresh multi-size `.ico` (16/32/48/64) with Pillow
  directly from `favicon-512.png`, not from the delivered `.ico`. Both files
  now live in `app/static/img/` (`favicon.ico`, `favicon-512.png`); linked via
  two `<link rel="icon">` tags in `base.html`/`public_base.html`.
- **Three more asset folders surfaced in `~/Downloads` mid-session that are
  NOT used and should not be used:**
  - `DISCOtech_vector_logo_rebuild/` — byte-identical to a vector file already
    evaluated and abandoned earlier in the session. Not a new fix.
  - `DISCOtech_web_assets_fixed_v2 2/` and `.../fixed_v2 3/` — attempt to fix
    the favicon/crop issues but introduce **new** problems: visible
    noise/fringing around the glow (same bad-cutout pattern as one of the
    earlier flawed ChatGPT exports), washed-out low-contrast letters, and a
    sliver of the tagline text bleeding into the bottom of the wordmark crop.
    Confirmed by direct inspection — don't switch to these without asking the
    user to regenerate them properly first.

**4. Landing page copy + spacing.** `.hero` top padding `96px → 48px`
(`public.css`) to tighten the gap under the sticky header. The descriptive
sub-paragraph ("See which listings are losing ground…") was replaced with a
short brand tagline, styled to match the actual gradient treatment from the
logo lockup's SVG source (new `.hero-tagline` class: bold, uppercase,
`letter-spacing: 0.14em`, text filled with the same 4-stop
cyan→blue→violet→magenta `linear-gradient` via `background-clip: text`, not
plain gray body text).

**5. Cleanup.** `git rm`'d the old `ecomm-copilot-logo.png` and `favicon.png`
(confirmed unreferenced first). Removed several now-superseded intermediate
SVG files from earlier in the session (never committed, so plain `rm`).

### Verification

All done against the local dev server (`.claude/launch.json` → `ecomm-copilot`,
port 5001; **note:** `ecomm-copilot-preview` on port 5050 may be in use by
another session — use the `ecomm-copilot` entry or free the port). Checked the
landing page, sign-in/sign-up, and the authenticated dashboard/rail at mobile
(375px), narrow desktop (560–900px), and full desktop (1280px) widths. A
throwaway local account exists in the dev DB from testing:
`rebrand-check@example.com` / `TempPass!2345` — fine to leave or delete.

**Static-asset note:** template/CSS/Python edits need a dev-server **restart**
to show up (`TEMPLATES_AUTO_RELOAD` isn't forced on, so Jinja caches
compiled templates) — a plain browser refresh is not enough after editing a
`.html` or `.py` file. Swapping a static image file (like the logo PNG) does
**not** need a restart; `static_url()`'s `?v=<mtime>` cache-buster handles that
on its own.

### Commit message used

```
Rebrand to DISCOtech: logo, color palette, and landing copy

Visual-only rebrand — name/logo/colors/typography/copy change in the app
and docs. Repo name, droplet services, domain, and env vars are untouched
on purpose; that's a separate pass alongside the eventual domain swap.
```

Single commit on `main`, local only — see `git log -1` for the hash; not yet
pushed.

### Next session should probably

1. **Decide on pushing/deploying.** Pushing to `main` auto-deploys (see §2/§9
   "Deploy caution") — check for an active CI run first if deploying.
2. If the result still isn't quite right anywhere, the exact recolor
   parameters are documented above — don't start over from scratch.
3. Whenever the domain/repo rename happens: update `deploy/DEPLOY.md`, the
   systemd unit names, nginx site config, GitHub repo name, and the
   `worker.py`/`enqueue_monitoring.py` comments together, in one coordinated
   pass (see the "Scope agreed" note above for why these were held back).
4. Nothing else from §7's roadmap changed this session — it's all still open.
