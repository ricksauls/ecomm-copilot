# ecomm-copilot — Session Handoff

_Last updated: 2026-08-21._

A working reference for picking up development. Read this first, then
`CLAUDE.md` (coding standards) and `deploy/DEPLOY.md` (infra).

---

## 1. Snapshot

- **Live:** https://ecomm-copilot.com (HTTPS, auto-renewing cert).
- **Repo:** `github.com/ricksauls/ecomm-copilot` · **local working copy:**
  `~/Desktop/ClaudeStuff/ecomm-copilot-clean`.
- **Stack:** Python / Flask, SQLite, server-rendered Jinja templates, deployed
  by GitHub Actions to a DigitalOcean droplet.
- **Tests:** 40 passing (`ruff` clean, `pip-audit` clean).
- **What works today:** marketing landing, self-service auth (email/password +
  Google SSO), login-guarded workspace, and the **PDP Content Scoring** feature
  end-to-end (intake → queue → background fetch+score → results).

### ✅ Worker installed — scoring is self-serve (2026-08-21)
The scoring **worker is installed and running** on the droplet
(`ecomm-copilot-worker.service`, active; unit installed 2026-08-21). The full
loop works end-to-end: intake enqueues → the worker fetches + scores → the
results screen polls. `deploy/setup-droplet.sh` (idempotent; installs/starts the
worker and expands the sudoers rule) has been applied via the DigitalOcean
Console. Re-run it the same way if the unit ever needs reinstalling; verify with
`sudo systemctl status ecomm-copilot-worker`.

---

## 2. Infrastructure

- **Droplet:** `wm-content-tools`, `142.93.244.23`, Ubuntu 24.04 — **shared**
  with the WM share-of-voice app, so don't disrupt the other services.
- **App dir:** `/home/deploy/apps/ecomm-copilot`, runs as user `deploy`.
- **Web:** `ecomm-copilot.service` → gunicorn on `127.0.0.1:8001` behind nginx
  (site `ecomm-copilot.com` + `www`). Ports 8000/8002 belong to other apps.
- **Worker:** `ecomm-copilot-worker.service` (once installed) runs `worker.py`
  under `DISPLAY=:99`.
- **Xvfb:** `xvfb.service` on `:99` already runs (from the WM scraper) — the
  worker reuses it for headed Chrome.
- **DB:** SQLite at `DATABASE_URL` (`/home/deploy/apps/ecomm-copilot/app.db` on
  the droplet), file chmod 600. Tables: `users`, `scored_items`.
- **Secrets:** in `/home/deploy/apps/ecomm-copilot/.env` (chmod 600) — never
  committed. Includes `SECRET_KEY`, `DATABASE_URL`, `APP_URL`,
  `GOOGLE_CLIENT_ID/SECRET`.
- **SSH from the Mac:** `ssh droplet-deploy` (key `~/.ssh/deploy_wm_ci`). Root is
  only reachable via the DO web Console (the `deploy` sudo password was lost;
  reset with `passwd deploy` while in as root if wanted). The scoped sudoers
  rule lets `deploy` restart the two ecomm services without a password.

### Deploy pipeline (how shipping works)
`git push origin main` → **CI** (ruff, pip-audit, pytest) → on success the
**Deploy** workflow (`workflow_run`) SSHes in, `git pull`, `pip install`,
restarts `ecomm-copilot` (and `ecomm-copilot-worker`, guarded). Everyday loop:

```bash
git -C ~/Desktop/ClaudeStuff/ecomm-copilot-clean add -A
git -C ~/Desktop/ClaudeStuff/ecomm-copilot-clean commit -m "..."
git -C ~/Desktop/ClaudeStuff/ecomm-copilot-clean push origin main
gh run watch -R ricksauls/ecomm-copilot   # optional
```

Actions secrets (already set): `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY`,
`DEPLOY_FINGERPRINT` (**ECDSA** host key — the SSH action negotiates ecdsa, not
ed25519).

---

## 3. Code map

```
app/
  __init__.py        app factory: logging, security headers (strict CSP),
                     session hardening, MAX_CONTENT_LENGTH, blueprints, DB +
                     OAuth init, CSRF + current-user before_request hooks
  db.py              SQLite: schema (users, scored_items), per-request conn
  users.py           user CRUD, Werkzeug password hashing
  security.py        CSRF, login_required, current_user, input validation
  auth.py            /signup /signin /signout + Google SSO (gated)
  oauth.py           Authlib Google client (gated on GOOGLE_CLIENT_ID/SECRET)
  pdp.py             intake parsing: URL validation, CSV parse, item-number
  scoring.py         rule-based scorer: PdpRecord -> ScoreResult (5 dims)
  fetch.py           Playwright fetch + __NEXT_DATA__ parse -> PdpRecord;
                     Pillow image-resolution measurement
  jobs.py            scored_items queue: enqueue / claim / save result
  routes/pages.py    landing, dashboard (guarded), PDP scoring routes
  fixtures.py        demo data for the dashboard
  templates/…        public_base + landing/signin/signup; app/base + rail,
                     topbar, dashboard, pdp_scoring, pdp_results
  static/            tokens.css, public.css, workspace.css, js/pdp_scoring.js
worker.py            standalone background scoring worker (systemd)
deploy/              DEPLOY.md, *.service units, nginx.conf, setup-droplet.sh
tests/               40 tests (auth, jobs, pdp intake, scoring, parser, pages)
```

**Nav (rail):** Dashboard, **PDP Content Scoring** (built), PDP Image Set
Creation, PDP Copy Content Creation, Competitive Intelligence (placeholders,
`href="#"`).

---

## 4. The PDP scoring model

Five weighted dimensions → 0–100 overall (weights in `app/scoring.py:WEIGHTS`,
meant to be recalibrated against real search-rank/conversion data):

| Dimension | Weight | Rule-based signals today | AI-pass (not built) |
|---|---|---|---|
| Imagery | 25 | count, max px (zoom), video | infographic/lifestyle via vision |
| Attributes | 20 | spec count from `data.idml.specifications` | category schema % |
| Title | 18 | length band, ALL-CAPS, word count | keyword/SEO coverage |
| Key features | 18 | count, bullet length | benefit-vs-feature, keywords |
| Description | 19 | word count / depth | structure + SEO depth |

Each dimension returns findings + recommendations that map to a sellable fix.
The overall is computed **only over measurable dimensions** — an unmeasurable one
is excluded, not scored 0.

### Fetch method (reused from the WM scraper)
Walmart blocks plain requests. `fetch.py` drives **headed Chrome via Playwright**
(`channel="chrome"`, `--disable-blink-features=AutomationControlled`) under Xvfb
`:99`, reads `__NEXT_DATA__` →
`props.pageProps.initialData.data.product`, and maps: `name`→title,
`keyFeatures`→bullets, `shortDescription/longDescription`→description,
`imageInfo.allImages`→images, `contentLayout.modules`→video. Attributes come
from the **sibling `data.idml.specifications`** node (a flat name/value list;
`specificationsV2` is the grouped fallback) — *not* `data.product`, where they
aren't, and *not* the on-page spec table, which Walmart A/B-gates off
(`enableSpecificationsTable=false`) and doesn't even render when off. Image
dimensions aren't in the JSON, so Pillow fetches the bytes to measure. Browser
work is slow and serial → it runs in the **worker**, never in a request.

**Proven:** on the droplet, fetching item 10294528 (Tabasco) scores **58/100**
with 13 attributes measured (Attributes dimension 75/100). Note `key_features`
came back 0 for this item — bullets weren't extracted though
`idml.productHighlights` had 6 rows; see gap #7.

---

## 5. Known gaps / next-up roadmap

1. ~~**Install the worker**~~ — **DONE (2026-08-21):** `ecomm-copilot-worker`
   installed and active; the UI scores automatically end-to-end (§1).
2. ~~**Attribute (spec) extraction**~~ — **DONE (2026-08-21).** Specs are read
   from `data.idml.specifications` in `fetch.py`; `attributes_measured` is now
   True on live pages (13 specs on Tabasco). The category-schema **completeness
   %** is still future work — today it's a raw count proxy, so a listing with 5
   filled attributes scores the same regardless of how many its category expects.
3. **AI pass** — the qualitative half: SEO keyword coverage (mine competitor
   PDPs + Walmart autocomplete for the keyword set, score coverage, generate
   rewritten copy) and vision for infographic/lifestyle image quality. This is
   what pulls "mechanically complete but weak" listings down realistically.
   **Use Claude** for this (see the `claude-api` skill for current model IDs).
4. **Competitive benchmarking** — score top-N competitors for the item's head
   terms and show the gap to the category leader (the product's core promise).
5. **Nice-to-haves:** retry `blocked` items, a scoring history view, export.
6. **Other nav screens** — PDP Image Set Creation, PDP Copy Content Creation,
   Competitive Intelligence are still placeholders.
7. **Key-features extraction gap** — `key_features` scored 0 on Tabasco
   (10294528): `fetch.py` only reads `product.keyFeatures`, which was empty,
   even though `data.idml.productHighlights` carried 6 name/value rows. Add
   `idml.productHighlights` (and/or the WM scraper's `highlights` /
   shortDescription-bullet fallbacks) as bullet sources in `_extract_bullets`.

---

## 6. Local development

```bash
cd ~/Desktop/ClaudeStuff/ecomm-copilot-clean
.venv/bin/pip install -r requirements-dev.txt   # includes playwright, Pillow
.venv/bin/ruff check . && .venv/bin/python -m pytest -q
```

- Local `.env` has `SESSION_COOKIE_SECURE=false` so the session cookie works
  over `http://localhost`. `DATABASE_URL` empty → local `app.db` (gitignored).
- Preview server: use the Browser-pane `preview_start` with config
  `ecomm-copilot-preview` (port 5050). **Templates are cached — restart the
  preview server after editing a `.html`.** CSS/JS are static (no restart).
- Live browser fetch can't be tested from the Mac (Walmart blocks it and there's
  no local Xvfb); test parsing with fixtures, and validate live fetch on the
  droplet (`env DISPLAY=:99 venv/bin/python -c "from app.fetch import fetch_pdp…"`).

---

## 7. Gotchas learned (save yourself the debugging)

- **Strict CSP** (`script-src 'self'`): no inline scripts/handlers. Per-screen JS
  must be an external file under `/static/js` loaded via the `scripts` block.
- **CI was red for a reason:** CI installs the **pinned** `requirements-dev.txt`;
  don't let tool versions float. New deps must pass `pip-audit` or CI blocks the
  deploy.
- **Walmart bot block:** direct `requests` → 307 to a block page. Only headed
  Chrome under Xvfb gets through.
- **Deploy host key:** pin the **ECDSA** fingerprint, not ed25519.
- **Root on the droplet:** deploy sudo password is lost — use the DO web Console
  for root steps (`passwd deploy` there to reset if desired).
- **Concurrent sessions:** avoid two chats committing in this folder at once —
  they race (seen once already).
- **`setup-droplet.sh` vs TLS (fixed 2026-08-21):** certbot writes the 443 block
  into `/etc/nginx/sites-available/ecomm-copilot` in place. The script used to
  overwrite that file with the plain-HTTP template every run, so re-running it
  (e.g. to install the worker) silently dropped HTTPS → nginx served the default
  server's `7bcrfp.ricksauls.com` cert → `ERR_CERT_COMMON_NAME_INVALID`. The
  script now skips the overwrite when a 443 block is already present. If you hit
  the cert error, the fix (root, DO Console — the cert still exists) is:
  `sudo certbot --nginx -d ecomm-copilot.com -d www.ecomm-copilot.com`
  then `sudo nginx -t && sudo systemctl reload nginx`.

---

## 8. References

- Memory: `project_ecomm_copilot` (auto-loaded) tracks live status.
- WM scraper (fetch method + Xvfb setup precedent):
  `~/Desktop/ClaudeStuff/WM Dot Com Update` — `pdp_scraper.py`, its `README.md`
  (xvfb.service unit), and `score_content.py`.
