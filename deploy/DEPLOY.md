# Deploying ecomm-copilot

The app runs on the shared **wm-content-tools** droplet (`142.93.244.23`,
Ubuntu 24.04), following the same convention as the other apps there:

- App directory: **`/home/deploy/apps/ecomm-copilot`**, owned by `deploy`
- systemd service runs as **`User=deploy`**, gunicorn bound to **`127.0.0.1:8001`**
  (8000/8002 are used by other apps)
- nginx reverse proxy for **ecomm-copilot.com / www.ecomm-copilot.com**, TLS via
  Let's Encrypt (certbot)

Because `deploy` owns its home directory, the clone / venv / `.env` / git-pull
steps need **no sudo**. Only four steps need root, collected in
`deploy/setup-droplet.sh`.

Config files in this `deploy/` directory:
- `ecomm-copilot.service` — the systemd unit
- `nginx.conf` — the reverse-proxy server block
- `setup-droplet.sh` — the four root-only steps, run once with sudo

---

## One-time setup

### Unprivileged steps (as the `deploy` user)

```bash
# Read-only git deploy key, added to the repo under Settings -> Deploy keys
ssh-keygen -t ed25519 -C "ecomm-copilot-droplet" -f ~/.ssh/ecomm_deploy -N ""

# Clone, venv, deps
mkdir -p ~/apps
GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" \
  git clone git@github.com:ricksauls/ecomm-copilot.git ~/apps/ecomm-copilot
cd ~/apps/ecomm-copilot
python3 -m venv venv
venv/bin/pip install -r requirements.txt

# Secrets — generated locally, never committed. chmod 600.
printf 'SECRET_KEY=%s\nDATABASE_URL=%s/app.db\nAPP_URL=https://ecomm-copilot.com\n' \
  "$(python3 -c 'import secrets; print(secrets.token_hex(32))')" "$PWD" > .env
chmod 600 .env
```

### Privileged steps (once, with sudo)

`deploy/setup-droplet.sh` installs the systemd unit, the nginx site, a scoped
sudoers line (passwordless restart of *only* this service, for CI deploys), and
starts the service:

```bash
sudo bash ~/apps/ecomm-copilot/deploy/setup-droplet.sh
```

### DNS + HTTPS

Point the domain at the droplet, then issue the certificate:

1. At your DNS provider, create **A** records:
   - `ecomm-copilot.com` -> `142.93.244.23`
   - `www.ecomm-copilot.com` -> `142.93.244.23`
2. Wait for them to resolve (`dig +short ecomm-copilot.com` returns the IP).
3. Issue the cert (rewrites nginx to add 443 + HTTP->HTTPS redirect, auto-renews):

```bash
sudo certbot --nginx -d ecomm-copilot.com -d www.ecomm-copilot.com
```

---

## Auto-deploy (GitHub Actions)

`.github/workflows/deploy.yml` deploys over SSH **only after the CI workflow
passes on `main`**. It reuses the droplet's `deploy` user and restarts the
service via the scoped sudoers line above.

Required repository **Actions secrets** (Settings -> Secrets and variables ->
Actions):

| Secret | Value |
| --- | --- |
| `DEPLOY_HOST` | `142.93.244.23` |
| `DEPLOY_USER` | `deploy` |
| `DEPLOY_SSH_KEY` | private half of a CI-only keypair whose public half is in `deploy`'s `~/.ssh/authorized_keys` |
| `DEPLOY_FINGERPRINT` | `ssh-keyscan -t ecdsa 142.93.244.23 \| ssh-keygen -lf - \| awk '{print $2}'` (the SSH client in the deploy action negotiates the ECDSA host key, so pin that one, not ed25519) |

After the secrets exist, every push to `main` that passes CI runs the deploy.

---

## PDP scoring worker

PDP scoring runs in a **separate background service** (`ecomm-copilot-worker`),
not in the web workers, because it drives a real browser (~seconds per item).
`setup-droplet.sh` installs and starts it alongside the web service.

**Concurrency.** The worker processes several items at once — a thread pool
*inside the one process*, set by `SCORING_CONCURRENCY` (default **3**, hard-capped
at 10). It governs **both** the PDP-scoring pool and the Copy-Content pool (which
drain one at a time, never together, so they share one memory budget). Each
concurrent item is a full headed Chrome, so this is **memory-bound**: on a ~2 GB
droplet keep it at **2–3**. Going higher (up to 10) needs more RAM first — resize
the droplet, then set the value in `.env` and restart the worker:

```bash
# in /home/deploy/apps/ecomm-copilot/.env
SCORING_CONCURRENCY=3
```

```bash
sudo systemctl restart ecomm-copilot-worker
```

Higher concurrency also means more simultaneous requests to Walmart from one
datacenter IP; watch the worker log for a rise in `blocked` results and dial back
if needed. The SQLite DB runs in **WAL mode** with a busy timeout (set in
`db.tune_connection`) so the pool's threads and the web app can read/write
concurrently without "database is locked".

**Managed scraping API (unblocks Walmart).** Walmart now serves a press-and-hold
captcha to our browser even through the residential proxy — it fingerprints the
automation, not just the IP — which blocks scoring, item-tracking facts, and CI
image caching. Set the Oxylabs **Web Scraper API** credentials (primary) and/or
**Web Unblocker** (fallback) in `.env`; `app.fetch` then fetches the rendered HTML
from the API instead of launching Chrome (see `.env.example` for the
`OXYLABS_SCRAPER_*` / `OXYLABS_UNBLOCKER_*` block). These are a separate Oxylabs
product from `WALMART_PROXY_*` and supersede it for page fetches. No new systemd
units — the web + worker read the same `.env`, so set the vars and restart both:

```bash
# in /home/deploy/apps/ecomm-copilot/.env
OXYLABS_SCRAPER_USERNAME=...
OXYLABS_SCRAPER_PASSWORD=...
```

```bash
sudo systemctl restart ecomm-copilot ecomm-copilot-worker
```

Leave these blank and the fetchers fall back to the local browser (the dev
default). Note the CI **search** scrapers (`ci_scraper`/`keywords`) still use the
browser + `WALMART_PROXY_*` for now — migrating them is a later phase.

Requirements on the droplet:
- **Playwright** (installed into the venv by `pip install -r requirements.txt`
  on deploy) plus a real Chrome — it uses `channel="chrome"`, the same system
  Chrome the WM scraper uses.
- The **Xvfb `:99` virtual display** (from the WM scraper setup): the worker
  runs headed Chrome via `Environment=DISPLAY=:99` and `Wants=xvfb.service`.

Manage / inspect it:

```bash
sudo systemctl status ecomm-copilot-worker
sudo journalctl -u ecomm-copilot-worker -n 50 --no-pager
```

The GitHub Actions deploy restarts the worker too (guarded, so it's a no-op
until `setup-droplet.sh` has installed the unit + sudoers rule).

### Cached product images (media dir)

The worker caches each tracked CI product's main image as a small JPEG so the
snapshot page and PDF can show a thumbnail. Files live in a **`media/`** directory
next to the SQLite DB (`$MEDIA_DIR`, defaulting to
`/home/deploy/apps/ecomm-copilot/media/ci_products/<item_id>.jpg`). It's created
automatically on first write, sits **outside** the git checkout (so `git pull`
deploys never wipe it), and needs no setup. Zero-config; both the web app and the
worker resolve it from `DATABASE_URL`. To force a re-fetch, delete a file (or the
directory) — the next run re-caches any that are missing. No secrets live here.

### Generated business reports (media dir)

The Reports area (rail → **Reports**) builds a brand's performance deck as a PDF
or PowerPoint and stores the file under the same `media/` tree, at
`media/reports/<user_id>/<token>.<pdf|pptx>`, with a `generated_reports` row
pointing at it. Same lifecycle as the image cache: created on first write,
outside the git checkout, resolved from `DATABASE_URL`/`$MEDIA_DIR`, no setup and
no secrets. The History tab re-downloads past reports; deleting one removes both
the row and the file. Generation is pure-Python and synchronous (no browser, no
apt packages) — the only new dependency is **`python-pptx`** (for the editable
PowerPoint), already pinned in `requirements.txt`, so a normal
`venv/bin/pip install -r requirements.txt` on deploy picks it up. Restart the web
service after deploying:

```bash
sudo systemctl restart ecomm-copilot
```

Scheduled (weekly/monthly) reports are a planned follow-on; they will reuse this
same `generated_reports` table + `media/reports/` store and add a timer + worker
drain, documented here when they land.

## AI image upscaling (optional)

The content scorer flags product images below Walmart's 2000px zoom spec and main
images that aren't on a pure white background. When a provider is configured, the
results page offers per-item fixes — **"Enhance to 2000px"** (conservative
super-resolution of a flagged gallery image) and **"Fix & enhance main image"**
(composite the main image on pure white + upscale + resize, in one call) — plus a
per-item **"Fix all images"** that queues every flagged fix and a **"Download all
as ZIP"** of the results, all via Claid.ai. Each finished fix shows inline on the
page and is downloadable as a file to re-upload to Walmart.

**Batch fixing** (results table): a batch bar fixes flagged images across the
**whole batch** or the **ticked items**, drains main-image white-bg fixes first
(priority), and bundles every finished fix into one **by-item ZIP + `manifest.csv`**
(item → original URL → fixed file). Each "Fix" button first shows a **cost-preflight
modal** — real counts of new fixes and a dollar estimate — before spending.
**Retry Failed** re-queues errored fixes; **Cancel Queued** drops still-queued
jobs (in-flight calls finish). The estimate uses `IMAGE_UPSCALE_PRICE_PER_IMAGE`
(USD per fix, default `0.04` = Claid's per-action price); set it empty to show
counts only. It only estimates — it never meters, bills, or blocks.

**Batch copy rewrites** (same results table, in the "Bulk actions" section beside
image fixes): **Rewrite Copy For All Items** / **Rewrite Copy For Selected** generate
new Title/Description/Key Features for the scored batch, each behind the **same
cost-preflight modal** (a rewrite is a metered Claude call). Items that already have
copy or are in progress are skipped (matched by item id / URL). Results open on the
dedicated copy comparison page; a per-row badge shows each item's copy state. Copy is
**not provider-gated** (always available; generation needs `ANTHROPIC_API_KEY`, which
the worker enforces). The cost estimate uses `COPYGEN_PRICE_PER_ITEM` (approximate USD
per rewrite) — **no default** since a rewrite's cost is token-variable, so leave it
blank for counts-only or set your own rough figure.

**Config-gated and inert by default** — nothing shows or runs until you set
`IMAGE_UPSCALE_API_KEY` in `.env` (see `.env.example` for `IMAGE_UPSCALE_*`). To
enable, set the key and restart **both** services:

```bash
sudo systemctl restart ecomm-copilot          # web — reveals the controls
sudo systemctl restart ecomm-copilot-worker   # worker — actually runs the fixes
```

The worker loads the same `.env`, so no extra config is needed — it just needs a
restart to pick up a newly set key. Enhanced files cache under `media/enhanced/`
next to the DB (same media dir as product images; created on first write, outside
the git checkout).

Notes: fixes run **asynchronously on the background worker** (not in the web
request) — a click enqueues a job (`image_jobs` queue, mirrors scoring/copy), the
results page shows "Enhancing…" and auto-refreshes, and the fixed image appears
inline when done. Claid is **metered per image** — a real cost once enabled; the
"Fix all" button confirms first and never re-spends on an already-cached slot. The
API key is read from the environment and never logged.

## Competitive Intelligence monitoring timers

CI "monitoring" runs are enqueued **3×/day at 7:00 AM / 3:00 PM / 11:00 PM CST**
by three systemd timers driving a templated oneshot
(`ecomm-copilot-ci-monitor@<slot>.service`). The timers only enqueue `queued`
runs; the existing `ecomm-copilot-worker` does the scraping, so there's no extra
long-running process. `OnCalendar` carries an explicit `America/Chicago`
timezone, so systemd tracks CST/CDT automatically (the droplet clock is UTC).

`setup-droplet.sh` installs and enables all of this. If you're adding it to an
already-provisioned droplet **without** re-running the full script, do the
privileged install once (root via the DO web Console — the `deploy` sudo password
is lost; see the handoff):

```bash
cd /home/deploy/apps/ecomm-copilot
install -m 644 deploy/ecomm-copilot-ci-monitor@.service \
  /etc/systemd/system/ecomm-copilot-ci-monitor@.service
for slot in morning afternoon night; do
  install -m 644 "deploy/ecomm-copilot-ci-$slot.timer" \
    "/etc/systemd/system/ecomm-copilot-ci-$slot.timer"
done
systemctl daemon-reload
for slot in morning afternoon night; do
  systemctl enable --now "ecomm-copilot-ci-$slot.timer"
done
```

Inspect / verify:

```bash
systemctl list-timers 'ecomm-copilot-ci-*' --all       # next fire times
journalctl -u 'ecomm-copilot-ci-monitor@*' -n 50 --no-pager
# Trigger one immediately for a smoke test (enqueues a run for opted-in groups):
sudo systemctl start ecomm-copilot-ci-monitor@morning.service
```

A group is only swept when its owner has toggled **monitoring on** for it in the
app (`ci_groups.monitoring_enabled`).

## Item-tracking timers

Individual portfolio-item tracking is enqueued by two systemd timers driving a
templated oneshot (`ecomm-copilot-track@<cadence>.service`):

- **daily** — 6:00 AM CST — a cheap commerce-facts fetch (price / stock / rating /
  reviews) per tracked item, via the `portfolio_fact_jobs` queue.
- **weekly** — Monday 5:00 AM CST — a full content-readiness re-score per tracked
  item, reusing the `scored_items` queue.

Same model as the CI timers: they only write `queued` rows; the existing
`ecomm-copilot-worker` does the browser work, and `OnCalendar` carries an explicit
`America/Chicago` timezone. `setup-droplet.sh` installs and enables them. To add to
an already-provisioned droplet without re-running the full script (root via the DO
Console):

```bash
cd /home/deploy/apps/ecomm-copilot
install -m 644 deploy/ecomm-copilot-track@.service \
  /etc/systemd/system/ecomm-copilot-track@.service
for cadence in daily weekly; do
  install -m 644 "deploy/ecomm-copilot-track-$cadence.timer" \
    "/etc/systemd/system/ecomm-copilot-track-$cadence.timer"
done
systemctl daemon-reload
for cadence in daily weekly; do
  systemctl enable --now "ecomm-copilot-track-$cadence.timer"
done
```

Inspect / verify:

```bash
systemctl list-timers 'ecomm-copilot-track-*' --all
journalctl -u 'ecomm-copilot-track@*' -n 50 --no-pager
# Trigger one immediately for a smoke test (enqueues jobs for all tracked items):
sudo systemctl start ecomm-copilot-track@daily.service
```

An item is only picked up when its owner has toggled **Track** on for it in the
app (`portfolio_items.tracked`).

## Scheduled-reports timer

The Reports area lets a user stand up a **weekly or monthly** auto-build of a
brand's deck (rail → Reports → Schedules). A single daily timer drives it:

- **daily** — 6:30 AM CST (`ecomm-copilot-reports.timer` →
  `ecomm-copilot-reports.service` → `python -m app.enqueue_reports`).

The enqueue job decides which schedules are *due today* (by weekday / day-of-
month), writes one `queued` `generated_reports` row per schedule × format, and
**prunes** generated reports older than `REPORT_RETENTION_DAYS` (default 90;
optional env). The `ecomm-copilot-worker` builds the queued decks (pure-Python,
no browser) and writes them under `media/reports/`. Same model as the other
timers — only `queued` rows are written, and `OnCalendar` carries an explicit
`America/Chicago` timezone. `setup-droplet.sh` installs and enables it. To add to
an already-provisioned droplet without re-running the full script (root via the DO
Console):

```bash
cd /home/deploy/apps/ecomm-copilot
install -m 644 deploy/ecomm-copilot-reports.service \
  /etc/systemd/system/ecomm-copilot-reports.service
install -m 644 deploy/ecomm-copilot-reports.timer \
  /etc/systemd/system/ecomm-copilot-reports.timer
systemctl daemon-reload
systemctl enable --now ecomm-copilot-reports.timer
```

Inspect / verify:

```bash
systemctl list-timers 'ecomm-copilot-reports.timer' --all
journalctl -u ecomm-copilot-reports.service -n 50 --no-pager
# Trigger one immediately for a smoke test (enqueues today's due schedules):
sudo systemctl start ecomm-copilot-reports.service
```

A schedule only fires when its owner created it in the app (`report_schedules`)
and left it enabled; completed runs land in Reports → History with a "new" badge.

## Manual deploy / rollback

```bash
cd ~/apps/ecomm-copilot
GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" git pull --ff-only   # or: git checkout <sha>
venv/bin/pip install -r requirements.txt
sudo systemctl restart ecomm-copilot
```

## Troubleshooting

- **App won't start:** `sudo journalctl -u ecomm-copilot -n 50 --no-pager`
- **502 from nginx:** the service is down or not on `127.0.0.1:8001`.
- **Static files 404:** confirm the nginx `alias` matches
  `/home/deploy/apps/ecomm-copilot/app/static/`.
- **Never** run gunicorn/Flask with `debug=True` on the droplet — the debugger
  allows remote code execution.
