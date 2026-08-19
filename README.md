# ecomm-copilot

An agency-facing retail intelligence and creative production platform. Agencies
manage a portfolio of client brands and their Walmart product listings, run
competitive PDP analyses that produce a 0–100 score, and buy the specific
creative deliverables that close the gaps the analysis identifies.

**Your eCommerce Team, Amplified.**

## Stack

- Python / Flask (server-rendered Jinja templates)
- Plain CSS with custom-property design tokens (no build step, no CDN)
- SQLite (planned; not yet wired up)
- GitHub Actions for CI

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Generate a session key and create your local env file.
cp .env.example .env
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32))" >> .env

# Run locally (development server; do not use in production).
# .env is loaded automatically, so no SECRET_KEY prefix is needed.
python wsgi.py
# → http://127.0.0.1:5000
```

For production, serve with gunicorn behind nginx over HTTPS:

```bash
gunicorn wsgi:app
```

## Structure

```
app/
  __init__.py        app factory: logging, security headers, error handlers
  routes/pages.py    landing, sign-in, dashboard routes
  fixtures.py        stand-in demo data (swap for real data access later)
  static/            design tokens, per-surface CSS, self-hosted Inter, fonts
  templates/         Jinja templates (public + workspace surfaces)
tests/               pytest smoke tests
wsgi.py              production entry point
.claude/skills/      coding standards Claude Code applies to this repo
```

## Coding standards

Three skills in `.claude/skills/` govern all code in this repo — commenting,
logging/error-handling, and security. `CLAUDE.md` tells Claude Code to apply
them. See `CLAUDE.md` for the workflow rules.

## What's built vs. designed

Built: landing page, sign-in page (presentational), agency dashboard. The
design handoff also specifies the product workspace, competitive analysis,
creative workspace, and share-of-shelf screens, plus a Client View mode and
real authentication — all still to build.
