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
STATUS_DRAFT = "draft"
STATUS_CUTOUT_PENDING = "cutout_pending"
STATUS_CUTOUT_APPROVED = "cutout_approved"
STATUS_PLANNING = "planning"
STATUS_GENERATING = "generating"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

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
