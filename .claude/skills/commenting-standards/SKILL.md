---
name: commenting-standards
description: Commenting and documentation standards for all code written or edited in this workspace. Apply these conventions whenever writing, editing, or reviewing Python code, Flask routes, SQL, HTML/Jinja templates, JavaScript, CSS, shell scripts, or configuration files. Trigger on any code generation, code editing, code review, refactoring, or file creation task — even if the user doesn't mention comments explicitly. These standards are always in effect.
---

# Commenting Standards

These conventions apply to all code produced or modified in this workspace. The goal is comments that help a technically literate reader who may not be current on modern frameworks understand *why* something exists and how it fits into the larger system — without restating what the code already says.

## Core Principles

**Comment the why, not the what.** If the code is clear, it doesn't need a comment explaining what it does. Comments should capture intent, business context, constraints, and non-obvious decisions. A reader should be able to remove all comments and still understand the code's mechanics — but the comments should tell them *why* those mechanics were chosen.

**Prefer clarity over brevity in names, brevity over noise in comments.** Good naming reduces the need for comments. When a comment is needed, keep it tight.

**Don't comment to compensate for bad code.** If you need a paragraph to explain a function, the function probably needs to be rewritten or broken up.

## Python / Flask

### File Headers

Every Python file gets a module-level docstring immediately after imports. Keep it to 1–3 sentences covering what this module is responsible for and where it fits in the application.

```python
"""Revenue recovery analysis routes.

Handles the supplier-facing endpoints for viewing and acting on
identified revenue discrepancies against Walmart PO and invoice data.
"""
```

Do not include author, date, version, or license boilerplate. Git handles provenance.

### Functions and Methods

Use a docstring for any function that is non-trivial or part of a public interface. Use Google-style docstrings (Args/Returns/Raises sections) when there are parameters worth documenting. Skip the docstring on small private helpers where the name and signature tell the whole story.

```python
def calculate_recovery_amount(invoice_total, received_qty, ordered_qty, unit_cost):
    """Determine the recoverable amount for a short-ship discrepancy.

    Uses Walmart's standard cost-recovery formula: the delta between
    ordered and received quantities, valued at the PO unit cost, minus
    any tolerance threshold already applied upstream.

    Args:
        invoice_total: Billed amount from the supplier invoice.
        received_qty: Units confirmed at the DC via ASN/receipt.
        ordered_qty: Units on the original PO.
        unit_cost: Per-unit cost from the PO line item.

    Returns:
        Decimal recovery amount, or 0 if within tolerance.
    """
```

For simple functions, a one-liner is fine:

```python
def cents_to_dollars(amount):
    """Convert integer cents to a Decimal dollar amount."""
```

### Inline Comments

Use sparingly. Inline comments are for:
- **Business logic that isn't obvious from the code.** ("Walmart deducts at 2% over the published allowance" is worth a comment; `# increment counter` is not.)
- **Workarounds and known limitations.** Always explain *why* the workaround exists and link to an issue or reference if one exists.
- **Non-obvious control flow.** If an early return, guard clause, or exception path handles a subtle edge case, say what case it handles.

Place inline comments on the line above the code they describe, not at the end of the line (except for very short annotations on dict/config entries).

```python
# Walmart's API returns quantities as strings in the v3 endpoint;
# this was confirmed as intentional by their support team (2024-11).
received_qty = int(raw_data["qty_received"])
```

### TODO / FIXME / HACK Conventions

Use these tags consistently so they're greppable:

- `# TODO:` — Something that needs to be built or improved. Include enough context that the TODO is actionable without re-reading the whole file.
- `# FIXME:` — Known broken behavior that needs a fix.
- `# HACK:` — Intentional shortcut or workaround. Explain what it's working around and when it can be removed.

```python
# TODO: Replace hardcoded tolerance with supplier-level config from the DB
TOLERANCE_THRESHOLD = Decimal("0.02")

# HACK: The Walmart Item 360 API occasionally returns duplicate line items
# on the same PO. Deduplicating here until we add idempotency upstream.
seen_lines = set()
```

### Constants and Configuration

Comment non-obvious constants. If a number, string, or threshold has business meaning, say what it represents. If it came from an external source (Walmart documentation, API behavior, contractual terms), note that.

```python
# Maximum number of items per Walmart Replenishment API batch request.
# Source: Walmart Developer Portal, API v3 rate limit docs.
MAX_BATCH_SIZE = 100
```

## SQL

Comment complex queries at the top with a brief description of what the query answers and why it's structured the way it is. Call out non-obvious JOINs, subqueries, or WHERE clauses with inline comments.

```sql
-- Identify POs with short-ship discrepancies exceeding tolerance.
-- Uses the receipt table as source of truth since ASN data is
-- unreliable for cross-dock items.
SELECT po.po_number, po.vendor_id,
       li.ordered_qty,
       r.received_qty,
       (li.ordered_qty - r.received_qty) * li.unit_cost AS recovery_amt
FROM purchase_orders po
JOIN line_items li ON po.po_id = li.po_id
-- LEFT JOIN because not all POs have receipts yet (in-transit)
LEFT JOIN receipts r ON li.line_id = r.line_id
WHERE (li.ordered_qty - COALESCE(r.received_qty, 0)) > 0
```

## HTML / Jinja Templates

Use Jinja comments (`{# ... #}`) for template logic explanations. Use HTML comments (`<!-- -->`) only for structural landmarks in large templates. Don't comment individual HTML elements.

```html
{# Recovery table: only shown when the supplier has actionable discrepancies.
   Empty state is handled by the else block below. #}
{% if discrepancies %}
```

## JavaScript

Follow the same principles as Python. Use JSDoc (`/** */`) for exported functions. Use `//` inline comments for business logic and workarounds. Avoid `/* */` block comments in JS files.

## CSS

Comment sections/regions, not individual properties. Exception: if a property value is a workaround for a browser bug or framework quirk, comment it.

```css
/* --- Navigation Bar --- */

.nav-primary {
  position: sticky;
  top: 0;
  z-index: 100; /* Must sit above the sidebar overlay (z-index: 90) */
}
```

## Shell Scripts / Bash

Start every shell script with a comment block explaining what it does, what it expects (environment, arguments), and any prerequisites.

```bash
#!/bin/bash
# Deploy the Flask application to the DO droplet.
# Expects: SSH key configured for root@142.93.244.23
# Usage: ./deploy.sh [--skip-build]
```

## Configuration Files

Comment non-obvious settings in nginx configs, .env files, systemd units, etc. Don't comment self-explanatory settings like `port = 5002`.

```nginx
# Rate limit the recovery API to prevent runaway batch jobs from
# overwhelming the SQLite database (no WAL mode yet).
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
```

## What NOT to Comment

- **Obvious code.** `x += 1  # increment x` adds nothing.
- **Changelog entries.** That's what git log is for.
- **Commented-out code.** Delete it. Git has it if you need it back.
- **Apologetic comments.** "Sorry this is messy" — clean it up instead.
- **Closing-brace labels.** `# end if`, `# end for` — if your nesting is that deep, refactor.
