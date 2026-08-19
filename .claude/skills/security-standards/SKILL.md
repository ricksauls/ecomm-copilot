---
name: security-standards
description: Security best-practice standards for all code written or edited in this workspace. Apply these conventions whenever writing, editing, or reviewing Python code, Flask routes, database access, authentication, API integrations, dependency management, deployment configuration (GitHub Actions, systemd, nginx), or anything that handles secrets, user input, or external data — even if the user doesn't mention security explicitly. These standards are always in effect alongside commenting-standards and logging-and-error-handling.
---

# Security Standards

These conventions apply to all code produced or modified in this workspace. The goal is that security is designed in from the first commit, not retrofitted after a breach. This skill assumes a Python/Flask/SQLite application deployed via GitHub Actions, and it deliberately spells out modern conventions rather than assuming they're already known — flag the reasoning when a practice is non-obvious.

## Core Principles

**Never trust input, never trust the environment.** Every value that crosses a boundary — HTTP request, external API response, file upload, environment variable, database row — is untrusted until validated. The attacker controls anything you didn't generate yourself.

**Secrets never touch the repo.** Not in code, not in comments, not in config files, not in commit history, not in log output. If a secret is ever committed, it is compromised and must be rotated — removing it in a later commit does not help, because git history is permanent.

**Fail closed, not open.** When an auth check, permission check, or validation step errors out, the safe default is to deny. A bug in a security check should lock people out, not let them in.

**Least privilege everywhere.** Every credential, token, database user, and CI job should have the minimum access it needs and nothing more. When something is compromised, least privilege is what limits the blast radius.

**Defense in depth.** No single control is sufficient. Validate input *and* parameterize queries *and* escape output. Assume any one layer can fail.

---

## Secrets and Credentials

### Never Hardcode Secrets

API keys, database passwords, tokens, signing keys, and connection strings live in environment variables, loaded at runtime — never as literals in source.

```python
import os

# Correct: read from the environment, fail loudly if missing.
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
```

Reading a required secret with `os.environ["KEY"]` (which raises `KeyError` if absent) is preferable to `os.environ.get("KEY")` (which silently returns `None` and defers the failure to a confusing spot later). Fail at startup, not mid-request.

### .env Files Are Local-Only

- `.env` holds real secrets and is **never** committed. Confirm it's in `.gitignore`.
- `.env.example` is committed, lists every variable name the app needs, and has **empty or placeholder values** — never real ones. It documents the required configuration surface.
- Do not `print` or `logger.info` the contents of environment variables, even at DEBUG. (See logging-and-error-handling: never log API keys or tokens, even masked, unless masking is genuinely irreversible.)

### Secrets in GitHub Actions

- Store secrets in the repository's **Actions secrets** (Settings → Secrets and variables → Actions), referenced in workflows as `${{ secrets.NAME }}`.
- Never `echo` a secret in a workflow step — Actions masks known secret values in logs, but only the exact stored string, so a transformed or partial secret can leak.
- Pin third-party actions to a full commit SHA, not a mutable tag, so a compromised action release can't silently run in your pipeline:

```yaml
# Pinned to an immutable commit, not @v4 which can be re-pointed.
- uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11  # v4.1.1
```

- Scope the workflow token: add `permissions:` with the minimum needed (often `contents: read`). The default token is broader than most jobs require.

### If a Secret Leaks

Rotate it immediately at the source (regenerate the API key, change the DB password). Do not attempt to "scrub" it from history as the fix — assume anything ever pushed to a public repo was scraped within minutes.

---

## Input Validation

**Validate at the boundary, before the value is used.** Check type, range, format, and length as soon as untrusted data arrives, and reject clearly.

```python
# Flask route: validate before touching the service layer.
vendor_id = request.args.get("vendor_id", "")
if not vendor_id.isdigit() or len(vendor_id) > 12:
    logger.warning("Rejected malformed vendor_id: %r", vendor_id[:32])
    return jsonify({"error": "Invalid vendor_id"}), 400
```

- **Allowlist over blocklist.** Define what's *valid* and reject everything else. Trying to enumerate every bad input is a losing game.
- **Validate structure explicitly** for JSON bodies — check that required keys exist and have the expected types before use. Consider a schema validator (e.g. `pydantic`) for anything non-trivial.
- **Bound everything.** Cap string lengths, list sizes, numeric ranges, and file sizes. Unbounded input is a denial-of-service vector.

---

## SQL Injection

**Always parameterize. Never build SQL with string formatting or f-strings.** This is the single most important rule for SQLite and every other database.

```python
# CORRECT — parameterized. The driver handles escaping.
cursor.execute(
    "SELECT * FROM vendors WHERE vendor_id = ?",
    (vendor_id,),
)

# WRONG — string interpolation. This is an injection hole,
# even if vendor_id "looks like" it was validated upstream.
cursor.execute(f"SELECT * FROM vendors WHERE vendor_id = {vendor_id}")
```

Parameterization is non-negotiable **even when you've already validated the input** — validation and parameterization are separate layers, and defense in depth means keeping both. Table and column names can't be parameterized; when they must be dynamic, select them from a hardcoded allowlist, never from user input directly.

---

## Output Encoding and XSS

- Jinja2 autoescapes by default — leave it on. Only use `| safe` or `Markup` on content you generated and fully control, never on user-supplied or externally-sourced data.
- When returning JSON, use `jsonify` (sets the correct content type and escapes properly) rather than hand-building response strings.
- Set security headers on responses: at minimum `X-Content-Type-Options: nosniff` and a `Content-Security-Policy`. A small `after_request` hook or `flask-talisman` handles this centrally.

---

## Authentication and Sessions

- **Never store passwords in plaintext or with fast hashes (MD5, SHA-1, SHA-256).** Use a purpose-built password hash — `bcrypt`, `argon2`, or `scrypt` via a maintained library (e.g. `werkzeug.security.generate_password_hash`, which uses a suitable algorithm).
- Set Flask's `SECRET_KEY` from the environment — a long random value, never a hardcoded default. Session integrity depends on it.
- Session cookies must be `HttpOnly` (blocks JS access), `Secure` (HTTPS-only), and `SameSite=Lax` or `Strict` (CSRF mitigation). Configure via `SESSION_COOKIE_*` settings.
- **Enforce authorization on every protected route, server-side.** Hiding a button in the UI is not access control — the endpoint itself must check that the caller is allowed to do what they're asking. Fail closed: if the check errors, deny.
- Add CSRF protection for any state-changing form (`flask-wtf` provides it). APIs authenticated by token rather than cookie are less exposed but still validate the token on every request.

---

## External API Calls

Building on the external-API patterns in logging-and-error-handling, the security additions:

- **Always set a timeout** (already required for reliability; it's also a DoS defense — no external call should hang a worker indefinitely).
- **Verify TLS.** Never disable certificate verification (`verify=False` in `requests`). If a cert fails, that's the control working.
- **Treat API responses as untrusted input.** A compromised or misbehaving upstream can return anything. Validate response structure before trusting it — don't assume the shape.
- **Store the API's credentials as secrets** (above), and scope them to least privilege on the provider side where possible.

---

## Dependencies

- **Pin dependencies** with a lockfile or pinned `requirements.txt` so builds are reproducible and a compromised new release can't silently enter your app.
- **Keep Dependabot enabled** for the `pip` ecosystem so known-vulnerable dependencies surface as PRs. (The template ships a Dependabot config — make sure it targets `pip`, not `npm`, for this project.)
- Add a vulnerability scan step to CI (`pip-audit`) so a dependency with a published CVE fails the build rather than shipping.
- Prefer well-maintained, widely-used libraries over obscure ones. Every dependency is attack surface and a supply-chain risk.

---

## Error Handling and Information Disclosure

Building on logging-and-error-handling:

- **Never leak internals to the client.** Stack traces, exception class names, file paths, SQL, and library versions all help an attacker map your system. Return generic messages (`{"error": "Internal error"}`) to the caller; put the detail in the server-side log.
- **Run Flask with `debug=False` in any environment reachable by others.** The Werkzeug debugger allows arbitrary code execution and must never be exposed.
- Log security-relevant events (failed auth, rejected input, permission denials) at WARNING so they're auditable — but never log the sensitive values themselves.

---

## Deployment (GitHub Actions, systemd, nginx)

Flagged explicitly because deployment/infra security is easy to miss when it used to be someone else's job:

- **Terminate TLS at nginx** and redirect all HTTP to HTTPS. Use a real certificate (Let's Encrypt via certbot); never serve the app over plain HTTP.
- **Never run the app as root.** Create a dedicated low-privilege service user; run the systemd unit as that user. If the process is compromised, least privilege limits what the attacker gets.
- **Don't expose the Flask dev server to the internet.** Use a production WSGI server (`gunicorn`, `uwsgi`) behind nginx. The built-in server is single-threaded and not hardened.
- **Lock down the SQLite file's permissions** — readable/writable only by the service user, never world-readable, and never inside the web-served directory.
- In GitHub Actions, scope the workflow `permissions:` block, pin actions to SHAs, and keep deployment credentials in Actions secrets (above). A deploy workflow is a direct path to production — treat it as production-sensitive.

---

## Anti-Patterns — Don't Do These

- **Secrets in source or committed config** — the number one real-world breach cause. Environment variables only.
- **String-built SQL** — `f"... {value}"` in a query is an injection hole regardless of upstream validation. Parameterize, always.
- **`verify=False` on requests** — disables TLS verification; never do it, not even "temporarily" in dev.
- **`debug=True` anywhere reachable** — remote code execution via the debugger.
- **Fast/plaintext password storage** — MD5/SHA are not password hashes. Use bcrypt/argon2.
- **Blocklist validation** — enumerating bad input never covers everything. Allowlist what's valid.
- **Trusting client-side checks** — UI validation is UX, not security. Re-validate and re-authorize on the server.
- **Leaking errors to clients** — generic message out, detail to the log.
- **Unpinned dependencies and actions** — a mutable tag or floating version is a supply-chain foothold.
- **Running as root / exposing the dev server** — least privilege and a real WSGI server behind nginx, always.
