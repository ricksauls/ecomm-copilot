"""Persistence for the PDP Image Set Creation feature.

CRUD over ``imageset_projects`` / ``imageset_features`` / ``imageset_assets``
(the queue lives in :mod:`app.imageset.jobs`). Like the other data modules, every
function takes an explicit ``sqlite3.Connection`` so it works inside a Flask
request and in the worker process, every query is parameterized, and every read
that a request can reach is scoped by ``user_id`` as an IDOR guard.

Image bytes are never stored here — rows hold only filesystem paths (relative to
MEDIA_DIR) and status, the same split the enhanced-image cache uses.
"""

import json
import logging
import sqlite3

from app.imageset import plan as planmod

logger = logging.getLogger(__name__)

# Project lifecycle statuses (see the imageset_projects schema in app.db).
# ``fetching`` → a Walmart prefill is queued; ``fetching_active`` → a worker holds
# it mid-fetch (used as a one-shot, per-project claim lock); both resolve to
# ``draft`` (the editable, possibly prefilled, intake form).
STATUS_FETCHING = "fetching"
STATUS_FETCHING_ACTIVE = "fetching_active"
STATUS_DRAFT = "draft"
STATUS_CUTOUT_PENDING = "cutout_pending"
STATUS_CUTOUT_APPROVED = "cutout_approved"
# ``plan_queued`` → the worker should build the creative plan; ``plan_active`` → a
# worker holds it mid-plan (a one-shot, per-project claim lock). Planning runs on
# the worker (a slow Claude call) so it never blocks/times-out a web request.
STATUS_PLAN_QUEUED = "plan_queued"
STATUS_PLAN_ACTIVE = "plan_active"
STATUS_PLANNING = "planning"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

# Max product bullets to seed as features when prefilling from a PDP.
_MAX_PREFILL_FEATURES = 6

# Asset output-path columns an update may set — a hardcoded allowlist so a column
# name can never come from caller input (defense in depth alongside parameters).
_ASSET_UPDATABLE = frozenset(
    {"status", "final_path", "thumb_path", "scene_path", "review_json", "error", "title"}
)


def _json_or_none(value) -> str | None:
    """Serialize a list/dict to JSON, or return None for an empty/None value."""
    return json.dumps(value) if value else None


def _loads(raw: str | None, default):
    """Parse a stored JSON column, tolerating NULL/garbage by returning default."""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


# --- projects ---------------------------------------------------------------

def create_project(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    name: str,
    brand: str = "",
    category: str = "",
    description: str = "",
    target_audience: str = "",
    directions: str = "",
    caution: str = "",
    intended_environments: list[str] | None = None,
    brand_colors: list[str] | None = None,
    dimensions: dict | None = None,
) -> int:
    """Insert a draft project and return its id."""
    cur = conn.execute(
        "INSERT INTO imageset_projects "
        "(user_id, name, brand, category, description, target_audience, directions, caution, "
        " intended_environments, brand_colors, dimensions_json, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            user_id, name, brand, category, description, target_audience, directions, caution,
            _json_or_none(intended_environments), _json_or_none(brand_colors),
            _json_or_none(dimensions), STATUS_DRAFT,
        ),
    )
    conn.commit()
    logger.info("Created image-set project id=%s user_id=%s name=%r", cur.lastrowid, user_id, name)
    return int(cur.lastrowid)


def get_project(conn: sqlite3.Connection, project_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one project owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM imageset_projects WHERE id = ? AND user_id = ?",
        (project_id, user_id),
    ).fetchone()


def update_project_fields(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    name: str,
    brand: str = "",
    category: str = "",
    description: str = "",
    target_audience: str = "",
    directions: str = "",
    intended_environments: list[str] | None = None,
    brand_colors: list[str] | None = None,
    dimensions: dict | None = None,
) -> None:
    """Update a draft project's editable facts (used when the user submits the
    prefilled form). Mirrors :func:`create_project`'s fields; leaves image/status
    untouched."""
    conn.execute(
        "UPDATE imageset_projects SET name = ?, brand = ?, category = ?, description = ?, "
        "target_audience = ?, directions = ?, intended_environments = ?, brand_colors = ?, "
        "dimensions_json = ?, updated_at = datetime('now') WHERE id = ?",
        (name, brand, category, description, target_audience, directions,
         _json_or_none(intended_environments), _json_or_none(brand_colors),
         _json_or_none(dimensions), project_id),
    )
    conn.commit()


def list_projects(conn: sqlite3.Connection, user_id: int, limit: int = 100) -> list[sqlite3.Row]:
    """Return a user's projects, newest first (bounded)."""
    return conn.execute(
        "SELECT * FROM imageset_projects WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, max(1, min(limit, 500))),
    ).fetchall()


def set_status(
    conn: sqlite3.Connection, project_id: int, status: str, *, error: str | None = None
) -> None:
    """Update a project's status (and optional short error), stamping updated_at."""
    conn.execute(
        "UPDATE imageset_projects SET status = ?, error = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (status, error[:500] if error else None, project_id),
    )
    conn.commit()


def set_original_image(conn: sqlite3.Connection, project_id: int, path: str) -> None:
    """Record the uploaded product photo and move the project to cutout_pending."""
    conn.execute(
        "UPDATE imageset_projects SET original_path = ?, status = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (path, STATUS_CUTOUT_PENDING, project_id),
    )
    conn.commit()


def set_original_path(conn: sqlite3.Connection, project_id: int, path: str) -> None:
    """Record an original photo path WITHOUT advancing the status.

    Used when prefilling from a fetched product image: the project stays a
    ``draft`` so the user reviews the prefilled form (and can replace the photo)
    before anything advances to the cutout step.
    """
    conn.execute(
        "UPDATE imageset_projects SET original_path = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (path, project_id),
    )
    conn.commit()


# --- Walmart prefill (fetch) -----------------------------------------------

def create_draft_for_url(conn: sqlite3.Connection, *, user_id: int, url: str) -> int:
    """Create a ``fetching`` draft tied to a product URL; return its id.

    The name/brand/description are filled by the worker once the fetch completes
    (:func:`apply_fetched_record`); until then the row carries a placeholder name.
    """
    cur = conn.execute(
        "INSERT INTO imageset_projects (user_id, name, source_url, status) "
        "VALUES (?, ?, ?, ?)",
        (user_id, "(fetching…)", url, STATUS_FETCHING),
    )
    conn.commit()
    logger.info("Created fetching draft id=%s user_id=%s", cur.lastrowid, user_id)
    return int(cur.lastrowid)


def has_claimable_fetch(conn: sqlite3.Connection) -> bool:
    """True if any project is waiting for a Walmart prefill (status ``fetching``)."""
    return conn.execute(
        "SELECT 1 FROM imageset_projects WHERE status = ? LIMIT 1", (STATUS_FETCHING,)
    ).fetchone() is not None


def claim_next_fetch(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest project awaiting prefill (-> fetching_active).

    Uses a conditional UPDATE so a restart/second worker can't double-fetch; the
    project row itself is the one-shot queue (a prefill happens once per project).
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM imageset_projects WHERE status = ? ORDER BY id LIMIT 1",
            (STATUS_FETCHING,),
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE imageset_projects SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (STATUS_FETCHING_ACTIVE, candidate["id"], STATUS_FETCHING),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM imageset_projects WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next waiting project.


def apply_fetched_record(
    conn: sqlite3.Connection, project_id: int, *, name: str, brand: str, description: str
) -> None:
    """Fill a draft's name/brand/description from a fetched PDP record."""
    conn.execute(
        "UPDATE imageset_projects SET name = ?, brand = ?, description = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (name[:200] or "(unnamed product)", (brand or "")[:120],
         (description or "")[:2000], project_id),
    )
    conn.commit()


def replace_features_from_bullets(
    conn: sqlite3.Connection, project_id: int, bullets: list[str]
) -> int:
    """Seed a project's features from PDP bullets (replacing any). Returns the count.

    Each bullet becomes an editable feature the user can trim/rewrite; capped so a
    long PDP doesn't flood the form.
    """
    conn.execute("DELETE FROM imageset_features WHERE project_id = ?", (project_id,))
    count = 0
    for bullet in bullets:
        text = (bullet or "").strip()
        if not text:
            continue
        count += 1
        conn.execute(
            "INSERT INTO imageset_features (project_id, feature_key, title, position) "
            "VALUES (?, ?, ?, ?)",
            (project_id, f"f{count}", text[:120], count),
        )
        if count >= _MAX_PREFILL_FEATURES:
            break
    conn.commit()
    return count


def finish_fetch(conn: sqlite3.Connection, project_id: int) -> None:
    """Mark a prefill complete — the draft is now the editable intake form."""
    conn.execute(
        "UPDATE imageset_projects SET status = ?, error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (STATUS_DRAFT, project_id),
    )
    conn.commit()


def fail_fetch(conn: sqlite3.Connection, project_id: int, message: str) -> None:
    """Record a prefill failure but leave the draft usable (user fills it manually)."""
    conn.execute(
        "UPDATE imageset_projects SET status = ?, error = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (STATUS_DRAFT, message[:500], project_id),
    )
    conn.commit()
    logger.warning("Image-set prefill failed project=%s: %s", project_id, message)


def reclaim_orphaned_fetches(conn: sqlite3.Connection) -> int:
    """Reset any project stuck ``fetching_active`` to a usable draft — on worker startup.

    A project still mid-fetch at startup was orphaned by a restart; dropping it to
    ``draft`` lets the user fill it manually rather than wait forever. Relies on one
    worker process (like the other queues).
    """
    updated = conn.execute(
        "UPDATE imageset_projects SET status = ?, "
        "error = 'Prefill was interrupted — please fill in the details or try again.', "
        "updated_at = datetime('now') WHERE status = ?",
        (STATUS_DRAFT, STATUS_FETCHING_ACTIVE),
    )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned image-set prefill(s) on startup", updated.rowcount)
    return updated.rowcount


# --- Creative-plan queue (async planning on the worker) ---------------------

def queue_plan(conn: sqlite3.Connection, project_id: int) -> None:
    """Mark an approved project for background planning (status ``plan_queued``)."""
    conn.execute(
        "UPDATE imageset_projects SET status = ?, error = NULL, "
        "updated_at = datetime('now') WHERE id = ?",
        (STATUS_PLAN_QUEUED, project_id),
    )
    conn.commit()


def has_claimable_plan(conn: sqlite3.Connection) -> bool:
    """True if any project is waiting to be planned (status ``plan_queued``)."""
    return conn.execute(
        "SELECT 1 FROM imageset_projects WHERE status = ? LIMIT 1", (STATUS_PLAN_QUEUED,)
    ).fetchone() is not None


def claim_next_plan(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """Atomically claim the oldest project awaiting planning (-> plan_active), or None.

    Conditional UPDATE so a restart/second worker can't double-plan; the project
    row is the one-shot queue (a project is planned once per approval).
    """
    while True:
        candidate = conn.execute(
            "SELECT id FROM imageset_projects WHERE status = ? ORDER BY id LIMIT 1",
            (STATUS_PLAN_QUEUED,),
        ).fetchone()
        if candidate is None:
            return None
        updated = conn.execute(
            "UPDATE imageset_projects SET status = ?, updated_at = datetime('now') "
            "WHERE id = ? AND status = ?",
            (STATUS_PLAN_ACTIVE, candidate["id"], STATUS_PLAN_QUEUED),
        )
        conn.commit()
        if updated.rowcount == 1:
            return conn.execute(
                "SELECT * FROM imageset_projects WHERE id = ?", (candidate["id"],)
            ).fetchone()
        # Lost the race; try the next queued project.


def reclaim_orphaned_plans(conn: sqlite3.Connection) -> int:
    """Fail any project stuck ``plan_active`` — on worker startup.

    A project still mid-plan at startup was orphaned by a restart; mark it failed so
    the user can re-approve rather than wait forever. Relies on one worker process.
    """
    updated = conn.execute(
        "UPDATE imageset_projects SET status = ?, "
        "error = 'Planning was interrupted — please approve again to retry.', "
        "updated_at = datetime('now') WHERE status = ?",
        (STATUS_FAILED, STATUS_PLAN_ACTIVE),
    )
    conn.commit()
    if updated.rowcount:
        logger.warning("Reclaimed %d orphaned image-set plan(s) on startup", updated.rowcount)
    return updated.rowcount


def set_cutout(conn: sqlite3.Connection, project_id: int, path: str) -> None:
    """Record the generated transparent-PNG cutout (awaiting human approval)."""
    conn.execute(
        "UPDATE imageset_projects SET cutout_path = ?, updated_at = datetime('now') WHERE id = ?",
        (path, project_id),
    )
    conn.commit()


def approve_cutout(conn: sqlite3.Connection, project_id: int) -> None:
    """Mark the cutout human-approved — the gate before any generation runs."""
    conn.execute(
        "UPDATE imageset_projects SET cutout_approved_at = datetime('now'), status = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (STATUS_CUTOUT_APPROVED, project_id),
    )
    conn.commit()
    logger.info("Cutout approved for image-set project id=%s", project_id)


def set_selected_types(conn: sqlite3.Connection, project_id: int, types: list[str]) -> None:
    """Persist the asset types the user chose on the cutout screen.

    Stored as a JSON array; only known plan types are kept (defense against a
    tampered form). An empty list is stored as NULL, which generation reads as
    "all implemented types" — the prior default — so the flow never dead-ends.
    """
    valid = [t for t in types if t in planmod.ASSET_TYPES]
    payload = json.dumps(valid) if valid else None
    conn.execute(
        "UPDATE imageset_projects SET selected_types = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (payload, project_id),
    )
    conn.commit()
    logger.info("Image-set project id=%s selected_types=%s", project_id, valid or "all")


def set_logo(conn: sqlite3.Connection, project_id: int, path: str) -> None:
    """Record an optional brand logo for compositing."""
    conn.execute(
        "UPDATE imageset_projects SET logo_path = ?, updated_at = datetime('now') WHERE id = ?",
        (path, project_id),
    )
    conn.commit()


# --- features ---------------------------------------------------------------

def add_feature(
    conn: sqlite3.Connection,
    project_id: int,
    *,
    feature_key: str,
    title: str,
    description: str = "",
    feature_type: str = "benefit",
    icon: str = "",
    position: int = 0,
) -> int:
    """Insert one approved feature for a project and return its id."""
    cur = conn.execute(
        "INSERT INTO imageset_features "
        "(project_id, feature_key, title, description, feature_type, icon, position) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project_id, feature_key, title, description, feature_type, icon, position),
    )
    conn.commit()
    return int(cur.lastrowid)


def features_for_project(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    """Return a project's approved features in display order."""
    return conn.execute(
        "SELECT * FROM imageset_features WHERE project_id = ? ORDER BY position, id",
        (project_id,),
    ).fetchall()


def set_features(conn: sqlite3.Connection, project_id: int, items: list[dict]) -> int:
    """Replace a project's features from form rows ``[{title, description}]``.

    Blank titles are skipped; keys are assigned f1..fN in order. Returns the count.
    """
    conn.execute("DELETE FROM imageset_features WHERE project_id = ?", (project_id,))
    count = 0
    for item in items:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        count += 1
        conn.execute(
            "INSERT INTO imageset_features (project_id, feature_key, title, description, position) "
            "VALUES (?, ?, ?, ?, ?)",
            (project_id, f"f{count}", title[:120], (item.get("description") or "").strip()[:120], count),
        )
    conn.commit()
    return count


# --- plan + assets ----------------------------------------------------------

def plan_context(conn: sqlite3.Connection, project: sqlite3.Row) -> planmod.PlanProductContext:
    """Build the planner's product context from a project row + its features."""
    features = [
        planmod.Feature(id=f["feature_key"], title=f["title"], feature_type=f["feature_type"])
        for f in features_for_project(conn, project["id"])
    ]
    dims = _loads(project["dimensions_json"], {})
    return planmod.PlanProductContext(
        name=project["name"],
        brand=project["brand"],
        category=project["category"],
        description=project["description"],
        target_audience=project["target_audience"],
        intended_environments=_loads(project["intended_environments"], []),
        directions=project["directions"],
        has_dimensions=bool(dims),
        features=features,
    )


def save_plan_and_create_assets(
    conn: sqlite3.Connection, project_id: int, user_id: int, plan: planmod.CreativePlan
) -> list[int]:
    """Persist the plan JSON and create one draft asset row per plan item.

    Replaces any prior assets for the project (a re-plan starts clean) and stores
    the raw plan so the run is reproducible. Returns the new asset ids in plan
    order. The project is left in ``planning`` — the caller enqueues the assets.
    """
    conn.execute("DELETE FROM imageset_assets WHERE project_id = ?", (project_id,))
    plan_json = json.dumps(
        {"items": [_item_to_dict(i) for i in plan.items]}
    )
    conn.execute(
        "UPDATE imageset_projects SET plan_json = ?, status = ?, updated_at = datetime('now') "
        "WHERE id = ?",
        (plan_json, STATUS_PLANNING, project_id),
    )
    asset_ids: list[int] = []
    for item in plan.items:
        cur = conn.execute(
            "INSERT INTO imageset_assets "
            "(project_id, user_id, asset_type, variation_number, title, layout_style, "
            " scene_description, usage_scenario, assigned_feature_ids, generation_instructions, "
            " status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft')",
            (
                project_id, user_id, item.asset_type, item.variation_number, item.title,
                item.layout_style, item.scene_description, item.usage_scenario,
                _json_or_none(item.assigned_feature_ids), item.generation_instructions,
            ),
        )
        asset_ids.append(int(cur.lastrowid))
    conn.commit()
    logger.info("Saved plan + %d asset(s) for image-set project id=%s", len(asset_ids), project_id)
    return asset_ids


def _item_to_dict(item: planmod.PlanItem) -> dict:
    """Serialize a PlanItem for storage in plan_json (round-trips the run)."""
    return {
        "assetType": item.asset_type,
        "variationNumber": item.variation_number,
        "title": item.title,
        "sceneDescription": item.scene_description,
        "usageScenario": item.usage_scenario,
        "assignedFeatureIds": item.assigned_feature_ids,
        "layoutStyle": item.layout_style,
        "generationInstructions": item.generation_instructions,
    }


def assets_for_project(
    conn: sqlite3.Connection, project_id: int, user_id: int
) -> list[sqlite3.Row]:
    """Return a project's assets (IDOR-scoped), in stable creation order."""
    return conn.execute(
        "SELECT * FROM imageset_assets WHERE project_id = ? AND user_id = ? ORDER BY id",
        (project_id, user_id),
    ).fetchall()


def get_asset(conn: sqlite3.Connection, asset_id: int, user_id: int) -> sqlite3.Row | None:
    """Return one asset owned by ``user_id`` (IDOR guard), or None."""
    return conn.execute(
        "SELECT * FROM imageset_assets WHERE id = ? AND user_id = ?",
        (asset_id, user_id),
    ).fetchone()


def update_asset(conn: sqlite3.Connection, asset_id: int, **fields) -> None:
    """Update whitelisted columns on one asset, stamping updated_at.

    Column names are checked against :data:`_ASSET_UPDATABLE` (a hardcoded
    allowlist) so a dynamic SET clause can never be built from caller-controlled
    keys; values are always bound as parameters.
    """
    cols = [c for c in fields if c in _ASSET_UPDATABLE]
    if not cols:
        return
    assignments = ", ".join(f"{c} = ?" for c in cols)  # names from the allowlist only
    values = [fields[c] for c in cols]
    conn.execute(
        f"UPDATE imageset_assets SET {assignments}, updated_at = datetime('now') WHERE id = ?",
        (*values, asset_id),
    )
    conn.commit()


def asset_feature_ids(asset: sqlite3.Row) -> list[str]:
    """Return an asset's assigned feature_key list (parsed from JSON)."""
    return _loads(asset["assigned_feature_ids"], [])
