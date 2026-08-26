# ecomm-copilot — Session Handoff

_Last updated: 2026-08-26 (session 5)._

A working reference for picking up development. Read this first, then
`CLAUDE.md` (coding standards) and `deploy/DEPLOY.md` (infra).

> **Next session — start here.** Everything below is **live on main and deployed**
> (each change ships via `git push` → CI → auto-deploy; site returns 200). Session 5
> (2026-08-26) was a big **dashboard + activity-navigation** pass — read the **§7
> "Session 2026-08-26 (session 5)" note first**; it's the freshest and most
> detailed. Headlines: the dashboard body was **rebuilt into five "this month"
> activity tables** (PDP scored/copy/image-sets + CI snapshot/monitoring), each
> **collapsible, sortable, capped at 10 rows, with a thumbnail (hover to enlarge)
> and clickable rows that open that run's results**; two new **"View All …
> Activity"** screens (Content + Competitive Intelligence) show the same tables
> all-time; **run grouping** via a new `batch_id` so a row click opens the whole
> run; and the **worker orphan-reclaim gap is now CLOSED** (scoring + copy queues
> self-heal on startup, like CI). Tests: **237** passing (`ruff` + `pip-audit`
> clean).
>
> **Heads-up on GitHub Actions:** a multi-hour GitHub Actions **major outage**
> mid-session stalled the CI→Deploy pipeline (runs stuck `queued`, `workflow_run`
> chaining dropped). It fully recovered and everything deployed; nothing is wrong
> with our pipeline. If deploys ever hang again, check githubstatus.com first — and
> remember `deploy.yml` triggers **only** on `workflow_run` (no manual dispatch),
> so a clean re-trigger is a fresh push once Actions is healthy, or a manual SSH
> deploy (`git pull` + restart) if urgent.
>
> **No open operational items.** (The prior session's scoring/copy orphan-reclaim
> gap was fixed this session.)

---

## 1. Snapshot

- **Live:** https://ecomm-copilot.com (HTTPS, auto-renewing cert).
- **Repo:** `github.com/ricksauls/ecomm-copilot` · **local working copy:**
  `~/Desktop/ClaudeStuff/ecomm-copilot-clean`.
- **Stack:** Python / Flask, SQLite, server-rendered Jinja templates, deployed
  by GitHub Actions to a DigitalOcean droplet.
- **Tests:** 237 passing (`ruff` clean, `pip-audit` clean).
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
  `ci_share_of_search`, and the messaging set `message_threads`, `messages`
  (Contact Us). Schema is created + migrated idempotently at web **and** worker
  startup (`db.ensure_schema` = `_SCHEMA` + `_migrate`).
- **Media dir (cached product images):** `$MEDIA_DIR` (default `media/` next to
  `app.db` → `/home/deploy/apps/ecomm-copilot/media/ci_products/<item_id>.jpg`).
  Worker-populated, outside the repo (survives git-pull deploys), created on
  first write. Zero-config; delete a file to force a re-fetch. See §3 + DEPLOY.md.
- **Secrets / config** in `/home/deploy/apps/ecomm-copilot/.env` (chmod 600,
  never committed): `SECRET_KEY`, `DATABASE_URL`, `APP_URL`,
  `GOOGLE_CLIENT_ID/SECRET`, **`ADMIN_EMAILS`** (comma-separated allowlist =
  `ricksauls@cox.net,ricksauls1@gmail.com`), and — for Copy Content Creation —
  **`ANTHROPIC_API_KEY`** (the AI copy generator; the worker fails those items
  loudly if it's unset) and optional **`COPYGEN_MODEL`** (defaults to
  `claude-opus-5`; set it to switch models, e.g. a cheaper one for big batches).
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
`ci_share_of_search`. Deploy: `deploy/ecomm-copilot-ci-*.{service,timer}`.

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
