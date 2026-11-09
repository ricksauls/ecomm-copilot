"""Seed a demo account with a curated, self-consistent story for live demos.

DISCOtech's Insights and Reporting surfaces are time-series: they only read as
meaningful with weeks of accumulated history. This script backfills that history
deterministically for a dedicated demo account, so a live, presenter-driven demo
always tells the same clean story without waiting for — or paying Oxylabs for —
real scrapes.

Two kinds of managed items are seeded under the demo user:

  • SHOWCASE items (``demo = 1``, ``tracked = 1``) — one per brand, each with a
    scripted narrative: a winner climbing the shelf, a steady leader, one losing
    ground, and one with a discovery opportunity (a keyword stuck off page 1).
    Daily rank + share-of-search readings, weekly commerce facts, and weekly
    readiness scores are backfilled across the window. The ``demo`` flag keeps the
    tracking sweeps from ever scraping them (see app.item_rank / app.tracking /
    app.enqueue_item_tracking), so the curated history is never overwritten. Each
    brand also gets a Competitive Tracking group — its own shelf plus competitors —
    with matching share-of-search history, so the per-brand dashboard (which scopes
    to a group's "mine" brands) is populated end to end.

  • SANDBOX items (``demo = 0``, ``tracked = 0``) — real Walmart PDPs with no seeded
    history. They exist so the presenter can demo live Portfolio management (add,
    toggle tracking, edit the AI keywords) on stage against real products.

Idempotent by design: ``--reset`` first wipes the demo user's seeded data (showcase
items, the sandbox items this script owns, the demo CI groups, and the demo
readiness scores — all scoped to the demo user only) and then re-seeds, so you can
restore a known-good demo before every showing. Generation is driven by a fixed RNG
seed, so a re-run reproduces the identical story.

Usage::

    python -m app.seed_demo --email demo@example.com --reset [--days 180]

The demo user must already exist (create it once via the admin "provision user"
screen). Alternatively pass ``--create-user`` with the ``DEMO_ACCOUNT_PASSWORD``
environment variable set, and the script will create a local account; the plaintext
password is hashed before it is stored and is never logged.
"""

import argparse
import json
import logging
import os
import random
import sqlite3
from datetime import date, timedelta

from dotenv import load_dotenv

logger = logging.getLogger("tracking.seed_demo")

# Fixed so a re-run reproduces the exact same story (dates still track "today").
RNG_SEED = 20261103
DEFAULT_DAYS = 180

# Marks every readiness row this script writes, so --reset can remove only the demo
# scores (scored_items has no FK to portfolio_items, so cascade can't reach it).
DEMO_BATCH_ID = "demo-seed"

# A typical count of page-1 result slots a keyword's SERP exposes; the share-of-search
# denominator. Kept in a tight band so shares read as believable percentages.
SHELF_SLOTS_MIN = 34
SHELF_SLOTS_MAX = 38

# The five content-readiness dimensions and their weights, mirroring the live scorer
# (see a real scored_items.result_json). Used to synthesize a plausible breakdown.
DIM_SPECS = (
    ("imagery", "Imagery", 25, -6),
    ("attributes", "Attributes", 20, -10),
    ("title", "Title", 18, +12),
    ("key_features", "Key features", 18, +2),
    ("description", "Description", 19, -4),
)

# One entry per showcase brand. role drives the narrative; group/competitors/ci_keywords
# build that brand's Competitive Tracking shelf. The first entry is the hero (the brand
# the dashboard loads by default).
SHOWCASE_BRANDS = (
    {
        "brand": "Tabasco",
        "role": "winner",
        "url": "https://www.walmart.com/ip/10294528",
        "title": "Tabasco Original Red Pepper Sauce, 2 fl oz",
        "keywords": ["tabasco", "tabasco sauce", "hot sauce", "pepper sauce", "louisiana hot sauce"],
        "group": "Hot Sauce",
        "ci_keywords": ["hot sauce", "pepper sauce", "louisiana hot sauce"],
        "competitors": ["Cholula", "Frank's RedHot", "Louisiana"],
        "ads": True,
        "facts": {"price": 3.98, "rating": 4.8, "reviews0": 5200, "images": 6, "video": True},
    },
    {
        "brand": "Ritz",
        "role": "leader",
        "url": "https://www.walmart.com/ip/690812452",
        "title": "Ritz Original Crackers, 13.7 oz",
        "keywords": ["ritz", "ritz crackers", "crackers", "snack crackers", "buttery crackers"],
        "group": "Crackers",
        "ci_keywords": ["crackers", "snack crackers", "buttery crackers"],
        "competitors": ["Cheez-It", "Club", "Townhouse"],
        "ads": False,
        "facts": {"price": 4.48, "rating": 4.7, "reviews0": 8800, "images": 5, "video": False},
    },
    {
        "brand": "Listerine",
        "role": "losing",
        "url": "https://www.walmart.com/ip/10294064",
        "title": "Listerine Cool Mint Antiseptic Mouthwash, 1 L",
        "keywords": ["listerine", "listerine mouthwash", "mouthwash", "antiseptic mouthwash", "cool mint mouthwash"],
        "group": "Mouthwash",
        "ci_keywords": ["mouthwash", "antiseptic mouthwash", "cool mint mouthwash"],
        "competitors": ["Scope", "Crest", "ACT"],
        "ads": True,
        "facts": {"price": 5.97, "rating": 4.6, "reviews0": 6400, "images": 4, "video": False},
    },
    {
        "brand": "Hawaiian Tropic",
        "role": "opportunity",
        "url": "https://www.walmart.com/ip/15610889",
        "title": "Hawaiian Tropic Sheer Touch Sunscreen Lotion SPF 30, 8 oz",
        # Slot 4 ("tanning oil") is the stuck keyword — never on page 1 (a discovery gap).
        "keywords": ["hawaiian tropic", "sunscreen", "spf 30 sunscreen", "tanning oil", "sunscreen lotion"],
        "opportunity_keyword": "tanning oil",
        "group": "Sunscreen",
        "ci_keywords": ["sunscreen", "spf 30 sunscreen", "sunscreen lotion"],
        "competitors": ["Banana Boat", "Coppertone", "Neutrogena"],
        "ads": False,
        "facts": {"price": 8.97, "rating": 4.5, "reviews0": 2100, "images": 5, "video": False},
    },
)

# Real PDPs with no history — the live Portfolio-management sandbox. Not tracked, not
# demo-flagged, so the presenter can toggle tracking on them on stage.
SANDBOX_ITEMS = (
    {
        "brand": "Hawaiian Tropic",
        "url": "https://www.walmart.com/ip/15610900",
        "title": "Hawaiian Tropic Silk Hydration Sunscreen SPF 30, 6 oz",
        "keywords": ["hawaiian tropic", "sunscreen", "spf 30 sunscreen", "", ""],
    },
    {
        "brand": "Listerine",
        "url": "https://www.walmart.com/ip/17206604",
        "title": "Listerine Total Care Anticavity Mouthwash, 1 L",
        "keywords": ["listerine", "listerine mouthwash", "mouthwash", "", ""],
    },
    {
        "brand": "Ritz",
        "url": "https://www.walmart.com/ip/5059726742",
        "title": "Ritz Fresh Stacks Original Crackers, 11.8 oz",
        "keywords": ["ritz", "ritz crackers", "crackers", "", ""],
    },
    {
        "brand": "Ritz",
        "url": "https://www.walmart.com/ip/9101808244",
        "title": "Ritz Toasted Chips Original, 8.1 oz",
        "keywords": ["ritz", "ritz toasted chips", "snack chips", "", ""],
    },
)


# ── Narrative generators ────────────────────────────────────────────────────
# Each returns believable-but-scripted numbers for a point in the window. f is the
# fraction through the window (0.0 = oldest reading, 1.0 = most recent), so a role's
# trend is a function of f with a little RNG jitter on top.

def _dedupe_key(url: str, item_id: str | None) -> str:
    """Mirror app.portfolio._dedupe_key so a seeded row dedupes like an added one."""
    return item_id if item_id else url.strip().lower()


# How paid-reliant each role's visibility is: the fraction of days a sponsored slot
# shows, and how much of the item's shelf is paid when it does. Gives the demo a real
# story — the winner earns its shelf (mostly organic), the one losing ground buys it
# (paid-heavy) — instead of a flat 25% everywhere. Used for the organic/paid split.
_AD_INTENSITY = {"winner": 0.18, "leader": 0.32, "losing": 0.55, "opportunity": 0.22}

# Per-keyword share multipliers (by slot). The role sets an item's baseline shelf
# presence; these spread it across the item's keywords so their Share of Search
# *differs* (not just their rank), which is what a viewer expects to see in the drill.
_KW_SHARE_FACTORS = (1.0, 0.65, 1.35, 0.8, 1.15)


def _rank_reading(role: str, f: float, is_opportunity_kw: bool,
                  rng: random.Random, kw_factor: float = 1.0) -> dict:
    """One day's rank + slot counts for an (item, keyword), shaped by the role.

    Returns the columns app.item_rank writes: rank / organic_rank / sponsored_rank
    (best page-1 positions; None = off page 1), item_slots (the item's own placements)
    and total_slots (all page-1 placements — the SoS denominator), plus the organic/paid
    split item_sponsored_slots / total_sponsored_slots (how many of the item's / the
    page's slots are sponsored — see app.item_rank.build_reading). ``kw_factor`` scales
    the item's slot count for this keyword so an item's keywords differ in share.
    """
    total = rng.randint(SHELF_SLOTS_MIN, SHELF_SLOTS_MAX)
    intensity = _AD_INTENSITY.get(role, 0.25)

    # A deliberate discovery gap: this keyword never reaches page 1 (so 0% share). The
    # page still carries ads (the paid denominator), the item just holds none of them.
    if is_opportunity_kw:
        return {"rank": None, "organic_rank": None, "sponsored_rank": None,
                "item_slots": 0, "total_slots": total,
                "item_sponsored_slots": 0, "total_sponsored_slots": round(total * 0.2)}

    if role == "winner":
        base, slots = 18 - 14 * f, 1 + round(3 * f)
    elif role == "leader":
        base, slots = 5 - 1 * f, 5
    elif role == "losing":
        base, slots = 6 + 11 * f, max(1, round(4 - 3 * f))
    else:  # opportunity — its other keywords rank moderately
        base, slots = 11 - 3 * f, 1 + round(1.5 * f)

    # Spread the item's shelf presence across its keywords, never exceeding the page.
    slots = max(1, round(slots * kw_factor))
    item_slots = min(slots, total)
    organic_rank = max(1, round(base + rng.uniform(-1.2, 1.2)))

    # Paid presence is a role-driven fraction of the item's own shelf, with light daily
    # jitter — the demo story: the winner is mostly organic (earns its shelf), the brand
    # losing ground leans on ads. Applying it per day (rather than an occasional coin
    # flip) keeps the paid share clearly visible in the composition bar, which is the
    # whole point of the view. sponsored_rank is set exactly when the item holds a paid
    # slot, so the two never contradict; organic is always the remainder.
    paid_frac = min(0.85, max(0.0, intensity + rng.uniform(-0.1, 0.1)))
    item_sponsored = min(item_slots, round(item_slots * paid_frac))
    sponsored_rank = rng.randint(1, 5) if item_sponsored > 0 else None
    rank = min(r for r in (organic_rank, sponsored_rank) if r is not None)
    # Page-wide ad count: at least the item's own paid slots, scaled by intensity.
    total_sponsored = max(item_sponsored, min(total, round(total * (0.15 + 0.2 * intensity))))
    return {"rank": rank, "organic_rank": organic_rank, "sponsored_rank": sponsored_rank,
            "item_slots": item_slots, "total_slots": total,
            "item_sponsored_slots": item_sponsored, "total_sponsored_slots": total_sponsored}


def _mine_share(role: str, f: float) -> float:
    """Target share of the competitive shelf (0..1) for the mine brand at point f."""
    if role == "winner":
        return 0.15 + 0.25 * f          # 15% → 40%
    if role == "leader":
        return 0.45                      # steady leader
    if role == "losing":
        return 0.40 - 0.15 * f          # 40% → 25%
    return 0.10 + 0.12 * f              # opportunity: 10% → 22%


def _readiness_overall(role: str, f: float) -> int:
    """Content-readiness overall score (0..100) for the item at point f."""
    if role == "winner":
        return round(62 + 24 * f)       # 62 → 86
    if role == "leader":
        return 82
    if role == "losing":
        return round(78 - 14 * f)       # 78 → 64
    return round(60 + 12 * f)           # opportunity: 60 → 72


def _result_json(overall: int) -> str:
    """Synthesize a readiness breakdown around an overall score (the scorer's shape)."""
    dims = []
    for key, label, weight, bias in DIM_SPECS:
        score = max(0, min(100, overall + bias))
        dims.append({
            "key": key, "label": label, "score": score, "weight": weight,
            "findings": [f"{label} scored in the demo seed"],
            "recommendations": [f"Improve {label.lower()} to lift the overall score"],
        })
    return json.dumps({"overall": overall, "dimensions": dims})


# ── Date helpers ──────────────────────────────────────────────────────────────

def _window(days: int) -> tuple[date, list[date], list[date]]:
    """Return (start, daily_dates, weekly_dates) for a `days`-long window ending today."""
    today = date.today()
    start = today - timedelta(days=days - 1)
    daily = [start + timedelta(days=i) for i in range(days)]
    weekly = [start + timedelta(days=i) for i in range(0, days, 7)]
    if weekly[-1] != today:
        weekly.append(today)  # always include the most recent point
    return start, daily, weekly


# ── DB writes ───────────────────────────────────────────────────────────────

def _resolve_user(conn: sqlite3.Connection, email: str, create: bool) -> int:
    """Return the demo user's id, optionally creating a local account.

    Creation requires the DEMO_ACCOUNT_PASSWORD env var; the plaintext is hashed by
    werkzeug before storage and never logged. Without --create-user a missing account
    is a hard error (the demo account is provisioned once via the admin UI).
    """
    email = email.strip().lower()
    row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if row:
        return int(row["id"])

    if not create:
        raise SystemExit(
            f"No account for {email!r}. Create it in the admin 'provision user' "
            "screen first, or re-run with --create-user and DEMO_ACCOUNT_PASSWORD set."
        )

    password = os.environ.get("DEMO_ACCOUNT_PASSWORD")
    if not password:
        raise SystemExit("--create-user requires the DEMO_ACCOUNT_PASSWORD env var.")

    # Hash with the same primitive the app's signup path uses; plaintext never persisted.
    from werkzeug.security import generate_password_hash

    cur = conn.execute(
        "INSERT INTO users (email, password_hash, auth_provider, role) "
        "VALUES (?, ?, 'local', 'user')",
        (email, generate_password_hash(password)),
    )
    logger.info("Created demo account user_id=%s", cur.lastrowid)
    return int(cur.lastrowid)


def _reset(conn: sqlite3.Connection, user_id: int) -> None:
    """Remove this script's prior demo data for one user (and only that user).

    Scoped tightly so re-seeding never touches another user's data or any real items
    the presenter added to the demo account: showcase items by the demo flag, sandbox
    items by the exact dedupe keys this script owns, CI groups by the demo group names,
    and readiness scores by the demo batch marker. Cascades handle the child rows.
    """
    from app import pdp

    showcase = conn.execute(
        "DELETE FROM portfolio_items WHERE user_id = ? AND demo = 1", (user_id,)
    ).rowcount

    # Same key the insert path computes, so reset removes exactly the sandbox rows
    # this script owns — never a real item the presenter may have added.
    sandbox_keys = [_dedupe_key(s["url"], pdp.item_number_from_url(s["url"]))
                    for s in SANDBOX_ITEMS]
    placeholders = ",".join("?" for _ in sandbox_keys)
    sandbox = conn.execute(
        f"DELETE FROM portfolio_items WHERE user_id = ? AND demo = 0 "
        f"AND dedupe_key IN ({placeholders})",
        [user_id] + sandbox_keys,
    ).rowcount

    group_names = [b["group"] for b in SHOWCASE_BRANDS]
    gph = ",".join("?" for _ in group_names)
    groups = conn.execute(
        f"DELETE FROM ci_groups WHERE user_id = ? AND name IN ({gph})",
        [user_id] + group_names,
    ).rowcount

    scores = conn.execute(
        "DELETE FROM scored_items WHERE user_id = ? AND batch_id = ?",
        (user_id, DEMO_BATCH_ID),
    ).rowcount
    logger.info("Reset demo data user_id=%s: %d showcase, %d sandbox, %d CI group(s), %d score(s)",
                user_id, showcase, sandbox, groups, scores)


def _insert_item(conn: sqlite3.Connection, user_id: int, spec: dict, demo: bool,
                 tracked: bool) -> int:
    """Insert one portfolio item (title already known, so no title backfill needed)."""
    from app import pdp

    url = spec["url"]
    item_id = pdp.item_number_from_url(url)
    cur = conn.execute(
        "INSERT INTO portfolio_items "
        "(user_id, brand, walmart_url, item_id, dedupe_key, title, title_status, tracked, demo) "
        "VALUES (?, ?, ?, ?, ?, ?, 'done', ?, ?)",
        (user_id, spec["brand"], url, item_id, _dedupe_key(url, item_id),
         spec["title"], 1 if tracked else 0, 1 if demo else 0),
    )
    return int(cur.lastrowid)


def _insert_keywords(conn: sqlite3.Connection, user_id: int, item_pk: int,
                     keywords: list[str]) -> list[int]:
    """Insert the 5 positional keyword slots; return their ids (None keyword = blank)."""
    ids = []
    for slot, kw in enumerate(keywords[:5], start=1):
        cur = conn.execute(
            "INSERT INTO portfolio_keywords (portfolio_item_id, user_id, slot, kind, keyword) "
            "VALUES (?, ?, ?, 'search', ?)",
            (item_pk, user_id, slot, (kw or None)),
        )
        ids.append(int(cur.lastrowid))
    return ids


def _seed_rank_history(conn: sqlite3.Connection, user_id: int, item_pk: int,
                       item_id: str | None, spec: dict, keyword_ids: list[int],
                       daily: list[date], rng: random.Random) -> int:
    """Backfill one showcase item's daily rank readings across every keyword slot."""
    opp = spec.get("opportunity_keyword")
    rows = []
    for slot_idx, kw_id in enumerate(keyword_ids):
        kw = spec["keywords"][slot_idx]
        if not kw:
            continue  # blank slot is never scraped (a cost brake) → no readings
        is_opp = bool(opp) and kw == opp
        # Each keyword slot gets its own share multiplier, so an item's keywords differ
        # in Share of Search (not just rank) — matching what the drill is expected to show.
        kw_factor = _KW_SHARE_FACTORS[slot_idx % len(_KW_SHARE_FACTORS)]
        n = len(daily)
        for i, day in enumerate(daily):
            f = i / (n - 1) if n > 1 else 1.0
            r = _rank_reading(spec["role"], f, is_opp, rng, kw_factor)
            rows.append((item_pk, kw_id, user_id, item_id, day.isoformat(),
                         r["rank"], r["organic_rank"], r["sponsored_rank"],
                         r["item_slots"], r["total_slots"],
                         r["item_sponsored_slots"], r["total_sponsored_slots"]))
    conn.executemany(
        "INSERT INTO portfolio_rank_readings "
        "(portfolio_item_id, keyword_id, user_id, walmart_item_id, captured_at, "
        " rank, organic_rank, sponsored_rank, item_slots, total_slots, "
        " item_sponsored_slots, total_sponsored_slots) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    return len(rows)


def _seed_facts_and_scores(conn: sqlite3.Connection, user_id: int, item_pk: int,
                           item_id: str | None, spec: dict,
                           weekly: list[date], rng: random.Random) -> tuple[int, int]:
    """Backfill weekly commerce-facts snapshots and weekly readiness scores."""
    facts = spec["facts"]
    n = len(weekly)
    fact_rows, score_rows = [], []
    for i, day in enumerate(weekly):
        f = i / (n - 1) if n > 1 else 1.0
        # Reviews accrue over time; price/rating jitter lightly for realism.
        reviews = facts["reviews0"] + round(facts["reviews0"] * 0.25 * f)
        price = round(facts["price"] + rng.uniform(-0.2, 0.2), 2)
        fact_rows.append((item_pk, user_id, item_id, day.isoformat(), price,
                          f"${price:.2f}", 1, facts["rating"], reviews,
                          facts["images"], 1 if facts["video"] else 0, spec["title"]))
        overall = _readiness_overall(spec["role"], f)
        # Midday timestamp so created_at ordering is stable within the day.
        score_rows.append((user_id, item_id, spec["url"], spec["title"], spec["brand"],
                           DEMO_BATCH_ID, overall, _result_json(overall),
                           f"{day.isoformat()} 12:00:00", f"{day.isoformat()} 12:00:00"))
    conn.executemany(
        "INSERT INTO portfolio_item_facts "
        "(portfolio_item_id, user_id, walmart_item_id, captured_at, price, price_string, "
        " in_stock, rating, review_count, image_count, has_video, title) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        fact_rows,
    )
    conn.executemany(
        "INSERT INTO scored_items "
        "(user_id, item_id, url, title, brand, batch_id, status, overall, result_json, "
        " created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'scored', ?, ?, ?, ?)",
        score_rows,
    )
    return len(fact_rows), len(score_rows)


def _seed_ci_group(conn: sqlite3.Connection, user_id: int, spec: dict,
                   item_id: str | None, weekly: list[date],
                   rng: random.Random) -> int:
    """Create a Competitive Tracking group + brands/products/keywords + weekly history.

    monitoring_enabled = 0 so the scheduled CI sweep skips this group (its runs are
    seeded, not scraped), while the competitive screens still read its history.
    Returns the new group id.
    """
    gcur = conn.execute(
        "INSERT INTO ci_groups (user_id, name, description, mode, monitoring_enabled, active) "
        "VALUES (?, ?, ?, 'monitoring', 0, 1)",
        (user_id, spec["group"], f"{spec['brand']} vs. the {spec['group'].lower()} shelf"),
    )
    group_id = int(gcur.lastrowid)

    # mine brand first, then competitors; keep the brand_id for attribution.
    brand_ids: dict[str, int] = {}
    mine_cur = conn.execute(
        "INSERT INTO ci_brands (group_id, name, type, tracked) VALUES (?, ?, 'mine', 1)",
        (group_id, spec["brand"]),
    )
    brand_ids[spec["brand"]] = int(mine_cur.lastrowid)
    # The showcase item is the mine brand's tracked product.
    conn.execute(
        "INSERT INTO ci_products (group_id, brand_id, name, walmart_item_id, walmart_url, active) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (group_id, brand_ids[spec["brand"]], spec["title"], item_id, spec["url"]),
    )
    for comp in spec["competitors"]:
        ccur = conn.execute(
            "INSERT INTO ci_brands (group_id, name, type, tracked) VALUES (?, ?, 'competitor', 1)",
            (group_id, comp),
        )
        brand_ids[comp] = int(ccur.lastrowid)

    keyword_ids: dict[str, int] = {}
    for kw in spec["ci_keywords"]:
        kcur = conn.execute(
            "INSERT INTO ci_keywords (group_id, keyword, active) VALUES (?, ?, 1)",
            (group_id, kw),
        )
        keyword_ids[kw] = int(kcur.lastrowid)

    n = len(weekly)
    ads_start = max(0, n - 8)  # a competitor runs headline ads over the last ~8 weeks
    for i, day in enumerate(weekly):
        f = i / (n - 1) if n > 1 else 1.0
        iso = day.isoformat()
        rcur = conn.execute(
            "INSERT INTO ci_runs (group_id, run_type, slot, status, started_at, finished_at) "
            "VALUES (?, 'monitoring', NULL, 'done', ?, ?)",
            (group_id, f"{iso} 09:00:00", f"{iso} 09:04:00"),
        )
        run_id = int(rcur.lastrowid)
        _seed_ci_run(conn, group_id, run_id, iso, spec, brand_ids, keyword_ids,
                     mine_item_id=item_id, f=f,
                     ads=spec["ads"] and i >= ads_start, rng=rng)
    logger.info("Seeded CI group '%s' (id=%s) with %d weekly run(s)", spec["group"], group_id, n)
    return group_id


def _seed_ci_run(conn: sqlite3.Connection, group_id: int, run_id: int, iso: str,
                 spec: dict, brand_ids: dict[str, int], keyword_ids: dict[str, int],
                 mine_item_id: str | None, f: float, ads: bool,
                 rng: random.Random) -> None:
    """Seed one run's share-of-search rollup AND the raw page-1 cards behind it.

    Both are derived from one per-brand slot allocation so the two readings stay
    consistent: the Share-of-Search table (ci_share_of_search counts) and the
    dashboard Competition card (which counts ci_search_results rows whose item_id
    is one of the user's tracked 'mine' products) report the same share. The mine
    brand's cards therefore carry the real tracked item id; other cards don't.
    """
    mine = spec["brand"]
    mine_share = _mine_share(spec["role"], f)
    for kw, kw_id in keyword_ids.items():
        shelf = rng.randint(SHELF_SLOTS_MIN, SHELF_SLOTS_MAX)
        mine_total = max(1, round(shelf * mine_share))
        remaining = shelf - mine_total

        # Competitors take decreasing shares of what's left; the leftover is "other"
        # (brand_id NULL) so the share denominator is the whole page-1 shelf.
        comp_weights = [0.5, 0.3, 0.2][:len(spec["competitors"])]
        allocations = {mine: mine_total}
        for comp, w in zip(spec["competitors"], comp_weights):
            allocations[comp] = max(0, round(remaining * w * rng.uniform(0.85, 1.15)))
        other = max(0, shelf - sum(allocations.values()))

        # Per-brand share-of-search rows (the rollup the Share-of-Search table reads).
        for brand_name, total in allocations.items():
            if total <= 0:
                continue
            sponsored = round(total * 0.2)
            conn.execute(
                "INSERT INTO ci_share_of_search "
                "(run_id, group_id, keyword_id, date, slot, brand_id, "
                " organic_count, sponsored_count, total_count) "
                "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?)",
                (run_id, group_id, kw_id, iso, brand_ids[brand_name],
                 total - sponsored, sponsored, total),
            )
        if other > 0:
            conn.execute(
                "INSERT INTO ci_share_of_search "
                "(run_id, group_id, keyword_id, date, slot, brand_id, "
                " organic_count, sponsored_count, total_count) "
                "VALUES (?, ?, ?, ?, NULL, NULL, ?, 0, ?)",
                (run_id, group_id, kw_id, iso, other, other),
            )

        # Raw page-1 cards: one per slot, best positions to the biggest shareholder,
        # so the card counts reproduce the shares above. Mine cards carry the tracked
        # item id (the Competition card's numerator); others are attributed by brand.
        ranked = sorted(allocations.items(), key=lambda kv: kv[1], reverse=True)
        cards = []
        position = 1
        for brand_name, total in ranked:
            sponsored = round(total * 0.2)
            for j in range(total):
                cards.append((run_id, group_id, kw_id, iso, position,
                              "sponsored" if j < sponsored else "organic",
                              mine_item_id if brand_name == mine else None,
                              brand_ids[brand_name]))
                position += 1
        for _ in range(other):  # untracked third-party listings — no brand, no tracked id
            cards.append((run_id, group_id, kw_id, iso, position, "organic", None, None))
            position += 1
        conn.executemany(
            "INSERT INTO ci_search_results "
            "(run_id, group_id, keyword_id, scraped_at, position, position_type, "
            " item_id, brand_id, is_new_sku) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
            cards,
        )

    if ads:
        # A competitor amplifying with a Sponsored Brand (headline) ad on the lead keyword.
        lead_kw = next(iter(keyword_ids))
        competitor = spec["competitors"][0]
        conn.execute(
            "INSERT INTO ci_ad_units "
            "(run_id, group_id, keyword_id, scraped_at, ad_type, brand_id, brand_text, image_path) "
            "VALUES (?, ?, ?, ?, 'headline', ?, ?, NULL)",
            (run_id, group_id, keyword_ids[lead_kw], iso, brand_ids[competitor], competitor),
        )


def seed(conn: sqlite3.Connection, user_id: int, days: int) -> dict:
    """Seed all showcase + sandbox content for one user; return a counts summary."""
    from app import pdp

    rng = random.Random(RNG_SEED)
    _, daily, weekly = _window(days)
    counts = {"showcase": 0, "sandbox": 0, "readings": 0, "facts": 0,
              "scores": 0, "groups": 0}
    hero_group_id = None

    for spec in SHOWCASE_BRANDS:
        item_id = pdp.item_number_from_url(spec["url"])
        item_pk = _insert_item(conn, user_id, spec, demo=True, tracked=True)
        kw_ids = _insert_keywords(conn, user_id, item_pk, spec["keywords"])
        counts["readings"] += _seed_rank_history(
            conn, user_id, item_pk, item_id, spec, kw_ids, daily, rng)
        nf, ns = _seed_facts_and_scores(conn, user_id, item_pk, item_id, spec, weekly, rng)
        counts["facts"] += nf
        counts["scores"] += ns
        gid = _seed_ci_group(conn, user_id, spec, item_id, weekly, rng)
        if hero_group_id is None:
            hero_group_id = gid
        counts["showcase"] += 1
        counts["groups"] += 1
        logger.info("Seeded showcase '%s' (%s): item_pk=%s", spec["brand"], spec["role"], item_pk)

    for spec in SANDBOX_ITEMS:
        item_pk = _insert_item(conn, user_id, spec, demo=False, tracked=False)
        _insert_keywords(conn, user_id, item_pk, spec["keywords"])
        counts["sandbox"] += 1

    # Load the hero brand by default on the performance dashboard.
    if hero_group_id is not None:
        conn.execute("UPDATE users SET default_ci_group_id = ? WHERE id = ?",
                     (hero_group_id, user_id))
    return counts


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Seed the DISCOtech demo account.")
    parser.add_argument("--email", required=True, help="demo account email")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"days of history to backfill (default {DEFAULT_DAYS})")
    parser.add_argument("--reset", action="store_true",
                        help="wipe this script's prior demo data for the user, then re-seed")
    parser.add_argument("--create-user", action="store_true",
                        help="create the account if missing (needs DEMO_ACCOUNT_PASSWORD)")
    args = parser.parse_args(argv)

    if args.days < 14:
        parser.error("--days must be at least 14 (needed for week/month roll-ups)")

    load_dotenv()
    from app import db

    database = os.environ.get("DATABASE_URL") or "app.db"
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    db.ensure_schema(conn)

    try:
        user_id = _resolve_user(conn, args.email, create=args.create_user)

        existing = conn.execute(
            "SELECT COUNT(*) FROM portfolio_items WHERE user_id = ? AND demo = 1",
            (user_id,),
        ).fetchone()[0]
        if existing and not args.reset:
            logger.error("Demo data already present for user_id=%s (%d showcase item(s)). "
                         "Re-run with --reset to rebuild.", user_id, existing)
            return 1
        if args.reset:
            _reset(conn, user_id)

        counts = seed(conn, user_id, args.days)
        conn.commit()
    except Exception:
        conn.rollback()
        logger.error("Demo seed failed; rolled back", exc_info=True)
        raise
    finally:
        conn.close()

    logger.info("Demo seed complete for %s: %d showcase + %d sandbox items, "
                "%d rank readings, %d facts, %d scores, %d CI group(s) over %d days",
                args.email, counts["showcase"], counts["sandbox"], counts["readings"],
                counts["facts"], counts["scores"], counts["groups"], args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
