---
name: logging-and-error-handling
description: Standards for application logging, diagnostic breadcrumbing, and error handling in all code written or edited in this workspace. Apply these conventions whenever writing, editing, or reviewing Python code, Flask routes, background jobs, API integrations, database operations, or shell scripts — even if the user doesn't mention logging or error handling explicitly. These standards are always in effect alongside commenting-standards.
---

# Logging, Breadcrumbing & Error Handling Standards

These conventions apply to all code produced or modified in this workspace. The goal is that when something goes wrong in production, you can reconstruct what happened, where it happened, and what state the system was in — without having to reproduce the problem or add logging after the fact.

## Core Principles

**Log for the person debugging at 2 AM.** Every log entry should help someone who didn't write the code understand what was happening and why it failed. That person is usually you, weeks later, with no memory of writing it.

**Breadcrumb the happy path, not just failures.** If you only log errors, you know *that* something broke but not *what led up to it*. Trace markers through normal flow are what make errors diagnosable.

**Handle errors at the right level.** Catch exceptions where you can actually do something about them — recover, retry, translate to a user-facing message, or enrich with context before re-raising. Don't catch exceptions just to log and re-raise at every layer; that creates duplicate noise.

**Fail loud, fail safe.** Swallowing exceptions silently is worse than crashing. If you can't handle an error meaningfully, let it propagate. But make sure the top-level handler catches it, logs it, and returns something useful to the user.

---

## Python Logging Setup

### Use the Standard Library

Use Python's built-in `logging` module. Do not use `print()` for any operational output — `print` doesn't give you levels, timestamps, or routing.

### Logger Per Module

Create a module-level logger in every file:

```python
import logging

logger = logging.getLogger(__name__)
```

This gives you hierarchical logger names that match your package structure (e.g., `app.routes.recovery`, `app.services.walmart_api`), which makes filtering easy.

### Application-Level Configuration

Configure logging once at application startup (in your Flask app factory or main entry point). Don't configure it in individual modules.

```python
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
```

For production, also write to a file so logs survive process restarts:

```python
import logging
from logging.handlers import RotatingFileHandler

file_handler = RotatingFileHandler(
    "logs/app.log", maxBytes=10_000_000, backupCount=5
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
)
logging.getLogger().addHandler(file_handler)
```

### Log Levels — Use Them Correctly

- **DEBUG** — Breadcrumb trace detail. Variable values, intermediate state, branch decisions. Turned off in production by default but available when you need to diagnose.
- **INFO** — Normal operational events. "Request received," "Job started," "Export complete." These form the narrative of what the app is doing.
- **WARNING** — Something unexpected that the app handled, but that might indicate a problem. Retries, fallback paths, approaching resource limits.
- **ERROR** — Something failed and the current operation could not complete. The user or calling system got an error response.
- **CRITICAL** — The application itself is in a broken state. Database unreachable, config missing, can't bind to port. Should be rare.

---

## Breadcrumbing

Breadcrumbs are trace-level log entries that record the path through your code. They answer: "What sequence of events led to this state?"

### Where to Drop Breadcrumbs

Place breadcrumbs at decision points and boundaries:

- **Route entry.** Log every inbound request with the key parameters — not the full payload, but enough to identify *which* request this was.
- **External API calls.** Log before calling an external service (what you're requesting) and after (success/failure, response status, timing). This is critical for Walmart API integrations where failures are common and intermittent.
- **Database writes.** Log what you're about to write and the result. For reads, log only when the query is non-trivial or the result is unexpected (zero rows when you expected rows, etc.).
- **Branch decisions.** When code takes path A vs path B based on data, log which path and why. This is the most commonly missing breadcrumb and the one you'll wish you had.
- **Job/task boundaries.** Log start, key milestones, and completion of any background process or batch operation.

### Breadcrumb Formatting

Include enough context to correlate entries. At minimum, include the business identifier (PO number, vendor ID, supplier name — whatever makes sense for the domain).

```python
logger.info("Processing PO %s for vendor %s (%d line items)",
            po_number, vendor_id, len(line_items))

logger.debug("PO %s: line %s unit_cost=%s, ordered_qty=%d, received_qty=%d",
             po_number, line_id, unit_cost, ordered_qty, received_qty)

logger.info("PO %s: recovery amount calculated: $%.2f", po_number, recovery_amt)
```

### What NOT to Breadcrumb

- **Sensitive data.** Never log passwords, API keys, tokens, full credit card numbers, or PII beyond what's needed to identify a transaction. When in doubt, mask it: `logger.info("Authenticating user %s", user_email[:3] + "***")`.
- **High-volume loops at INFO.** If you're processing 10,000 line items, don't log each one at INFO. Use DEBUG for per-item detail and INFO for batch summaries ("Processed 10,000 line items in 4.2s, 3 discrepancies found").
- **Static/deterministic code.** Pure functions with no side effects and no branching don't need breadcrumbs. The input and output tell the story.

---

## Error Handling

### Flask Routes

Every Flask route should handle errors explicitly. Don't let unexpected exceptions bubble up as raw 500s with stack traces.

```python
@app.route("/api/recovery/<vendor_id>")
def get_recovery(vendor_id):
    try:
        results = recovery_service.analyze(vendor_id)
        return jsonify(results)
    except VendorNotFoundError:
        logger.warning("Recovery requested for unknown vendor: %s", vendor_id)
        return jsonify({"error": "Vendor not found"}), 404
    except WalmartAPIError as e:
        logger.error("Walmart API failure during recovery for %s: %s",
                     vendor_id, e, exc_info=True)
        return jsonify({"error": "Upstream data source unavailable"}), 502
    except Exception as e:
        logger.error("Unexpected error in get_recovery for %s: %s",
                     vendor_id, e, exc_info=True)
        return jsonify({"error": "Internal error"}), 500
```

Key patterns:
- Catch specific exceptions first, generic `Exception` last.
- Use `exc_info=True` on ERROR-level logs to capture the stack trace.
- Return structured JSON errors with appropriate HTTP status codes — never leak internal details (class names, file paths, SQL) to the client.
- Log at WARNING for client errors (bad input, not found), ERROR for server-side failures.

### Global Error Handler

Register a global Flask error handler as a safety net. This catches anything that slips through route-level handling.

```python
@app.errorhandler(Exception)
def handle_unhandled(e):
    logger.error("Unhandled exception: %s", e, exc_info=True)
    return jsonify({"error": "Internal error"}), 500

@app.errorhandler(404)
def handle_not_found(e):
    return jsonify({"error": "Not found"}), 404
```

### Custom Exception Classes

Define application-specific exceptions so you can catch them meaningfully. Keep the hierarchy shallow — you don't need a class for every possible failure, just enough to distinguish categories of errors that need different handling.

```python
class AppError(Exception):
    """Base exception for application errors."""

class VendorNotFoundError(AppError):
    """Raised when a vendor ID doesn't exist in the system."""

class WalmartAPIError(AppError):
    """Raised when a Walmart API call fails."""

class DataIntegrityError(AppError):
    """Raised when data violates expected invariants."""
```

### External API Calls

Wrap all external API calls (Walmart, Anthropic, SendGrid, etc.) with:
- **Timeout.** Always set a timeout. No external call should be allowed to hang indefinitely.
- **Retry with backoff.** For transient failures (5xx, timeouts, rate limits), retry 2–3 times with exponential backoff. Don't retry on 4xx — those are client errors.
- **Structured error capture.** Log the request (URL, key params) and the response (status, error body) so you can diagnose without reproducing.

```python
import time

def call_walmart_api(endpoint, params, max_retries=3):
    """Call a Walmart API endpoint with retry and logging."""
    for attempt in range(1, max_retries + 1):
        try:
            logger.info("Walmart API request: %s (attempt %d/%d)",
                        endpoint, attempt, max_retries)
            response = requests.get(
                f"{WALMART_BASE_URL}/{endpoint}",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            logger.info("Walmart API success: %s (%d ms)",
                        endpoint, response.elapsed.total_seconds() * 1000)
            return response.json()
        except requests.exceptions.Timeout:
            logger.warning("Walmart API timeout: %s (attempt %d)", endpoint, attempt)
        except requests.exceptions.HTTPError as e:
            if response.status_code < 500:
                logger.error("Walmart API client error: %s → %d: %s",
                             endpoint, response.status_code, response.text[:200])
                raise WalmartAPIError(f"{endpoint} returned {response.status_code}") from e
            logger.warning("Walmart API server error: %s → %d (attempt %d)",
                           endpoint, response.status_code, attempt)
        if attempt < max_retries:
            sleep_time = 2 ** attempt
            logger.debug("Retrying %s in %ds", endpoint, sleep_time)
            time.sleep(sleep_time)
    raise WalmartAPIError(f"{endpoint} failed after {max_retries} attempts")
```

### Database Operations

For SQLite (and databases generally):
- Wrap write operations in explicit transactions.
- Log the operation and affected row count.
- Catch `IntegrityError` and `OperationalError` specifically — don't just catch `Exception`.

```python
try:
    with db.get_connection() as conn:
        cursor = conn.execute(
            "INSERT INTO discrepancies (po_id, amount) VALUES (?, ?)",
            (po_id, amount),
        )
        logger.info("Inserted discrepancy for PO %s: $%.2f (rowid=%d)",
                     po_id, amount, cursor.lastrowid)
except sqlite3.IntegrityError as e:
    logger.error("Duplicate discrepancy for PO %s: %s", po_id, e)
    raise DataIntegrityError(f"Discrepancy already recorded for PO {po_id}") from e
except sqlite3.OperationalError as e:
    logger.error("Database error writing discrepancy for PO %s: %s",
                 po_id, e, exc_info=True)
    raise
```

### Background Jobs and Scripts

For batch processes, cron jobs, and one-off scripts:
- Log start and end with a summary (records processed, errors encountered, elapsed time).
- Don't let one bad record kill the whole batch. Catch per-record errors, log them, accumulate them, and report a summary at the end.
- Exit with a non-zero code on failure so cron/systemd/GitHub Actions can detect it.

```python
def process_daily_discrepancies():
    """Nightly batch: scan new POs for recoverable discrepancies."""
    logger.info("Daily discrepancy scan starting")
    start = time.time()
    processed = 0
    errors = []

    for po in get_unscanned_pos():
        try:
            analyze_po(po)
            processed += 1
        except Exception as e:
            logger.error("Failed to process PO %s: %s", po.po_number, e,
                         exc_info=True)
            errors.append((po.po_number, str(e)))

    elapsed = time.time() - start
    logger.info("Daily scan complete: %d processed, %d errors, %.1fs elapsed",
                processed, len(errors), elapsed)
    if errors:
        logger.warning("Failed POs: %s", [e[0] for e in errors])
```

---

## Shell Scripts

Use `set -euo pipefail` at the top of every bash script. Log key steps to stderr with a prefix so they're distinguishable from program output.

```bash
#!/bin/bash
set -euo pipefail

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >&2; }

log "Starting deployment to production"
log "Pulling latest code..."
git pull origin main
log "Installing dependencies..."
pip install -r requirements.txt --break-system-packages
log "Restarting service..."
sudo systemctl restart myapp
log "Deployment complete"
```

---

## Anti-Patterns — Don't Do These

- **Bare `except: pass`** — Silent exception swallowing. Always log, always handle or propagate.
- **`print()` for logging** — No levels, no timestamps, no routing. Use `logger`.
- **Logging the full request/response payload at INFO** — Noise that obscures real signals. Use DEBUG for verbose dumps.
- **Catching `Exception` everywhere** — Catch specific exceptions where you can handle them. Use broad `Exception` only at the top-level safety net.
- **String formatting in log calls** — Use `logger.info("x=%s", x)` not `logger.info(f"x={x}")`. The former skips string interpolation if the log level is disabled.
- **Retry without backoff** — Hammering a failing service immediately makes things worse for everyone.
- **Logging API keys or tokens** — Even at DEBUG. Mask or omit them entirely.
