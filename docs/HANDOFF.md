# ecomm-copilot — Session Handoff

_Last updated: 2026-08-24._

A working reference for picking up development. Read this first, then
`CLAUDE.md` (coding standards) and `deploy/DEPLOY.md` (infra).

---

## 1. Snapshot

- **Live:** https://ecomm-copilot.com (HTTPS, auto-renewing cert).
- **Repo:** `github.com/ricksauls/ecomm-copilot` · **local working copy:**
  `~/Desktop/ClaudeStuff/ecomm-copilot-clean`.
- **Stack:** Python / Flask, SQLite, server-rendered Jinja templates, deployed
  by GitHub Actions to a DigitalOcean droplet.
- **Tests:** 204 passing (`ruff` clean, `pip-audit` clean).
- **Worker:** installed and running — scoring is self-serve end-to-end (intake →
  queue → background fetch+score → results). One worker only (see §6, parallelism).

**What works today:**
- Marketing **landing** page (dark), self-service **auth** (email/password +
  Google SSO), login-guarded **workspace**.
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
- **Competitive Intelligence** end-to-end, organized into **three rail menus**
  (groups are tagged by `mode`, each menu manages only its own):
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
  2. **Monitoring Setup** — configure, **Schedule & Run** (turns on the 3×/day
     sweep at 7 AM / 3 PM / 11 PM CST **and** runs an immediate baseline), shows
     the **next scheduled run time**.
  3. **View Monitoring** — pick a monitoring group from a **dropdown**, see the
     **trend** dashboard (Search Ranking with Δ + sparkline, filterable by brand;
     Share of Digital Shelf organic/sponsored + trend chart), download a
     **monitoring PDF**.
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
    so users compare standings. Snapshot shows an organic/sponsored best-position
    split; monitoring shows Best + Δ vs prior window + a sparkline.
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
  chmod 600. Tables: `users`, `scored_items`, `keyword_cache`, `copy_items`
  (Copy Content Creation), the CI set `ci_groups` (+`mode`), `ci_brands`,
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
  jobs.py            scored_items queue (enqueue/claim/save), admin list/count,
                     keyword_cache get/put
  copygen.py         AI copy rewrite: PdpRecord + keywords -> Claude (structured
                     output) -> GeneratedCopy. COPYGEN_MODEL, lazy SDK import
  copy_jobs.py       copy_items queue: two-phase fetch->generate lifecycle
                     (enqueue/claim/save_current/save_generated/request_generation)
  messages.py        Contact Us model: threads + messages, per-side unread by
                     *message id* (not timestamp — avoids same-second ties),
                     create/reply/mark_read/set_status, unread counts, IDOR-scoped
                     reads. CATEGORIES allowlist.
  pdf_export.py      reportlab PDFs: build_results_pdf (scored batch),
                     build_copy_pdf (copy batch), build_ci_snapshot_pdf (+ SoS
                     stacked-bar chart, placement-map grid, product thumbnails),
                     build_ci_monitoring_pdf
  routes/pages.py    landing, dashboard, PDP scoring + results + results.pdf,
                     PDP copy (...), scoring create-copy cross-link, CI snapshot/
                     monitoring/view + PDFs + schedule-from-snapshot,
                     /media/ci-product/<id> (cached image), Contact Us
                     (contact_home/create/thread/reply), admin users/items/copy +
                     admin messages (inbox/thread/reply/status) + delete
  fixtures.py        demo data for the dashboard
  templates/…        public_base + landing/signin/signup; app/base + _rail,
                     _topbar, dashboard, pdp_*, ci_* , admin_users/items/copy,
                     admin_messages, contact_home, contact_thread (shared
                     user+admin thread view)
  static/            css/{tokens,public,workspace}.css,
                     js/{intake,admin_users,ci_*}.js, img/{logo,favicon,...}
worker.py            background worker (systemd): drains scoring, copy, and CI
                     queues. process_ci_run also caches each tracked product's
                     main image once (_cache_ci_product_images, best-effort)
deploy/              DEPLOY.md, *.service units, nginx.conf, setup-droplet.sh
tests/               204 tests (auth, jobs, pdp, scoring, fetch, pages, keywords,
                     admin, copygen, copy_jobs, copy, ci_config/jobs/scraper/
                     analysis/worker/monitoring/pages, ci_images, messages,
                     messages_pages)
```

**Nav (rail):** **Dashboard** + **Contact Us** (top-level; Contact Us shows an
unread badge); **Content Studio** section — PDP Content Scoring (built), PDP Image
Set Creation (placeholder), PDP Copy Content Creation (built); **Competitive
Intelligence** section — One-Time Snapshot, Monitoring Setup, View Monitoring (all
built). **Admin** section (below Credits, admins only): Users, Items scored, Copy
created, **Messages** (unread badge), each with a live count. The **topbar** bell
shows unread-message counts (admin inbox for admins, own replies for users) plus
the admins-only new-user badge.

**Competitive Intelligence modules** (`app/`): `ci_config.py` (groups/brands/
products/keywords CRUD, user-scoped/IDOR-checked; groups carry a `mode` =
snapshot|monitoring; `clone_group_as_monitoring` powers snapshot→monitoring),
`ci_jobs.py` (run queue + result writers + SoS rollup), `ci_scraper.py`
(search-page card extraction + pure row builder), `ci_analysis.py` (period
windows, rank/SoS summaries & trends, `next_monitoring_run`, run-scoped
`snapshot_*` aggregations — incl. `snapshot_brand_avg_rank`,
`snapshot_rank_by_keyword_brand`, `snapshot_share_by_keyword`; `snapshot_page1_depth`
+ pure `build_rank_placement_map` for the placement grid; the older `snapshot_rank`
is now test-only), `enqueue_monitoring.py` (timer entry point). The snapshot page +
PDF share `pages._snapshot_data()` (which builds the placement map + thumbnail
`image_url`/`image_path` per product) so the two never drift.
Worker drains CI runs in `worker.process_ci_run`. Routes `pages.ci_*` (three
flows: snapshot/monitoring/view) + templates `templates/app/ci_{snapshot_home,
monitoring_home,group_config,snapshot_results,view}.html` + `_ci_help.html`;
static `js/ci_{config,charts,dashboard}.js`; PDFs via
`pdf_export.build_ci_{snapshot,monitoring}_pdf`. Tables: `ci_groups` (+`mode`),
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

- **Who:** the `ADMIN_EMAILS` allowlist in `.env` (server-side; `security.is_admin`
  / `admin_required` fail closed → 403 / sign-in redirect). Both `ricksauls@cox.net`
  and `ricksauls1@gmail.com` are admins.
- **Rail Admin section** (below Credits, admins only): **Users**, **Items
  scored**, **Copy created**, and **Messages**, each with a live count (via the
  `_inject_admin_context` context processor, which skips all DB work for
  non-admins).
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

**Session 2026-08-24 (all live on main; 7 commits `2759bbe`..`5eb45ff`).**
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
   PR before a runner change breaks the pipeline. Also: the **monitoring PDF**
   (`build_ci_monitoring_pdf`) and the **View-Monitoring page** still use the older
   two-table layout — bring them in line with the rebuilt snapshot page if desired.
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
