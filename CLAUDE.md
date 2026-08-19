# ecomm-copilot

A Python / Flask web application, deployed via GitHub Actions.

## Coding Standards — Always Apply

This repo carries three project skills in `.claude/skills/`. Apply them to **all**
code you write, edit, or review in this repo, whether or not the request mentions
them. They are always in effect, together:

- **commenting-standards** — how to comment and document code (the *why*, not the
  *what*) across Python, Flask, SQL, Jinja, JavaScript, CSS, shell, and config.
- **logging-and-error-handling** — application logging, breadcrumbing, and error
  handling: logger-per-module, correct log levels, breadcrumbs on the happy path,
  fail-loud/fail-safe error handling, retry-with-backoff on external calls.
- **security-standards** — secrets management, input validation, SQL-injection
  prevention, auth/session hardening, dependency and deployment security.

Before writing or modifying code, read the relevant skill file(s) and follow them.
When a security or deployment best practice is non-obvious, call it out explicitly
rather than applying it silently.

## GitHub Workflow

- Prefer feature branches. Direct commits to main are acceptable while the
  project is solo and nothing auto-deploys from main. Switch to strict
  branch-and-PR once a deploy pipeline runs off main or a second person joins —
  at that point branch protection should require CI to pass before merge.
- Keep changes small and focused, whether on a branch or on main.
- Run lint, tests, and build before suggesting merge.
- Explain all meaningful file changes.
- Do not modify secrets, billing, auth, or production config without explicit approval.

## Stack

- Language: Python
- Framework: Flask
- Database: SQLite
- CI/CD: GitHub Actions
- Front-end: server-rendered templates (design started in Claude Design)
