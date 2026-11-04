# DISCOtech Demo — Guide & Runbook

The seeded demo on **`demo.ecomm-copilot.com`**: what it is, how it's built, and
the two commands that keep it current. The demo is a **separate instance** of the
app from production — its own clone, its own database, its own login — so you can
show the product against a stable, curated story without touching production data.

- **URL:** https://demo.ecomm-copilot.com
- **Login:** `demo@ecomm-copilot.com`
- **On the droplet:** `/home/deploy/apps/ecomm-copilot-demo` (services
  `ecomm-copilot-demo` + `ecomm-copilot-demo-worker`, gunicorn on `127.0.0.1:8003`)

---

## Quick reference

Both commands run on the droplet, from the demo clone (`ssh` in first).

**Update the demo to the latest code (`main`):**

```bash
cd /home/deploy/apps/ecomm-copilot-demo && GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" git pull --ff-only && venv/bin/pip install -q -r requirements.txt && sudo -n systemctl restart ecomm-copilot-demo ecomm-copilot-demo-worker
```

**Re-seed the demo data (re-anchor the dates to today):**

```bash
cd /home/deploy/apps/ecomm-copilot-demo && venv/bin/python -m app.seed_demo --email demo@ecomm-copilot.com --reset
```

---

## 1. How the demo works

Eight managed items live under the demo account, split into two kinds that do very
different jobs. The split is the whole design: one set carries the data-rich
Insights/Reporting story, the other is for showing live portfolio management.

### Showcase items (4) — the frozen story

One per brand — **Tabasco, Ritz, Listerine, Hawaiian Tropic** — each flagged
`tracked = 1` **and** `demo = 1`. They show as "Tracking on" and appear across the
Dashboard, Performance, Competitive, and Reports with ~180 days of seeded history.

The `demo = 1` flag is what makes them safe to rely on: every tracking sweep skips
`demo = 1` items, so they are **never scraped**. Their numbers are the seeded story
and never change — which is exactly why the demo looks identical every time you
present it, and why it costs zero Oxylabs requests.

Each item tells a deliberate narrative:

| Brand | Role in the story |
|---|---|
| **Tabasco** | Winner — rank climbing, shelf share rising (the hero / default brand) |
| **Ritz** | Steady leader — strong and flat |
| **Listerine** | Losing ground — drives the "needs attention" signals |
| **Hawaiian Tropic** | Opportunity — a keyword stuck off page 1 (a discovery gap) |

### Sandbox items (4) — live portfolio management

Real Walmart PDPs flagged `tracked = 0`, `demo = 0`, with **no seeded history**.
They exist so you can demo the *act* of managing a portfolio: add an item, toggle
tracking, edit the AI-chosen keywords. Adding an item triggers the live
setup-progress pills (Queued → Fetching → Ready) as the worker fetches the title
and picks keywords — a nice on-screen beat.

### What is NOT running (and why that's intentional)

The demo instance has **no scheduled tracking timers** installed, so nothing
scrapes on a schedule — there is **no ongoing tracking for any item**, showcase or
sandbox. The worker runs only on-demand work (like the title/keyword setup when you
add an item). So the demo is a stable, self-contained snapshot: safe to click
through, and nothing changes under you mid-presentation.

---

## 2. Update the demo to the latest code

```bash
cd /home/deploy/apps/ecomm-copilot-demo && GIT_SSH_COMMAND="ssh -i ~/.ssh/ecomm_deploy" git pull --ff-only && venv/bin/pip install -q -r requirements.txt && sudo -n systemctl restart ecomm-copilot-demo ecomm-copilot-demo-worker
```

- Pulls the latest commits for the branch the demo clone is on — currently `main`,
  the same branch production deploys from. So this brings the demo up to the same
  code prod runs.
- **Independent of prod's auto-deploy.** Pushing to `main` auto-deploys *production*
  only; the demo is a separate clone the deploy workflow never touches. Run this
  command whenever you want the demo to catch up — on your schedule, not prod's.
- `pip install` picks up any new dependencies; the restart runs the app's additive
  schema migrations automatically.
- **Untouched by an update:** your seeded data (in the demo's own `app.db`) and the
  demo `.env`. A code update does not reset the demo story.
- `--ff-only` is a safety net. If it ever errors with *"Not possible to
  fast-forward,"* stop — the clone has drifted (stray local commits/edits); don't
  force it, investigate first.

---

## 3. Re-seed the data (keep it date-current)

```bash
cd /home/deploy/apps/ecomm-copilot-demo && venv/bin/python -m app.seed_demo --email demo@ecomm-copilot.com --reset
```

**Why this is needed over time.** The Dashboard and Insights always compute "last
completed **week** / month / quarter" relative to *today's* date — but the seeded
history is frozen, ending on the day you last seeded. As real time moves forward,
those windows slide past the data and the cards flip to the "gathering…" empty
state. Roughly:

| Default view | Stays populated until ~ |
|---|---|
| **Week** (the dashboard default) | ~1–2 weeks after the seed date |
| **Month** | ~1–2 months |
| **Quarter** | through roughly year-end |

The **Week view goes stale first** — within a couple of weeks. Re-seeding rebuilds
the *identical* story (the generator is deterministic) but ends it at the current
date, so every view is full again.

- **No password needed.** The account already exists, so there's no
  `--create-user` / `DEMO_ACCOUNT_PASSWORD`. The demo login is unchanged by
  `--reset`.
- `--reset` only wipes the seeded content (the 4 showcase + 4 sandbox items, the 4
  demo CI groups, and the demo readiness scores) and rebuilds it — nothing else on
  the account is affected.

**When to run it:** before a demo if it's been a week or more since the last seed,
or on a regular cadence if you demo often. (This can be automated with a small
weekly `systemd` timer on the demo instance so the demo is always current — ask if
you want that set up.)
