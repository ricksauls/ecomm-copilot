"""Portfolio access resolution.

Turns "who is viewing" into "whose portfolio, and which slice of it" for a
request. An account manager (an ``admin`` role) owns every portfolio item they
add; a helper (a ``user`` role) only ever sees what the manager granted them via
the ``portfolio_grants`` table. This module is the single place grant rows are
turned into concrete filters, so every surface — Portfolio, Insights, Reports,
Competitive — scopes identically and **fails closed** (no grant, an anonymous
caller, or an unexpected state all resolve to "see nothing").

Phase 0 ships this resolver and its tests only; no route calls it yet, so the
app's behavior is unchanged. Later phases route their reads/writes through
:func:`resolve_item_scope` and :func:`resolve_ci_scope`.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from . import security

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ItemScope:
    """The portfolio items a viewer may see and manage on this request.

    Attributes:
        owner_id: The account manager who owns the items in view, or ``None``
            when the viewer has no access at all (an ungranted helper or an
            anonymous caller).
        item_ids: ``None`` means "every item the owner has" — the owner
            themselves, or a helper with a whole-portfolio grant. A frozenset
            names exactly the accessible item ids. An empty frozenset means the
            viewer has an owner but no items currently resolve into scope.
    """

    owner_id: int | None
    item_ids: frozenset[int] | None

    @property
    def denied(self) -> bool:
        """True when the viewer may see no items at all."""
        return self.owner_id is None or self.item_ids == frozenset()

    @property
    def unrestricted(self) -> bool:
        """True when every one of the owner's items is in scope (no id filter)."""
        return self.owner_id is not None and self.item_ids is None


@dataclass(frozen=True)
class CiScope:
    """The Competitive Intelligence monitoring groups a viewer may see.

    Attributes:
        owner_id: The account manager who owns the groups, or ``None`` for no
            access.
        group_ids: ``None`` means "every group the owner has" (owner, or a
            helper with a whole-portfolio grant). A frozenset names exactly the
            brand-matched groups. An empty frozenset means no group is in scope.
    """

    owner_id: int | None
    group_ids: frozenset[int] | None

    @property
    def denied(self) -> bool:
        """True when the viewer may see no competitive groups at all."""
        return self.owner_id is None or self.group_ids == frozenset()

    @property
    def unrestricted(self) -> bool:
        """True when every one of the owner's groups is in scope."""
        return self.owner_id is not None and self.group_ids is None


# A viewer with no owner and no items/groups: the fail-closed default returned
# whenever resolution can't establish access. Kept as module constants so every
# deny path returns the same shape.
_ITEM_DENY = ItemScope(owner_id=None, item_ids=frozenset())
_CI_DENY = CiScope(owner_id=None, group_ids=frozenset())


def resolve_item_scope(conn: sqlite3.Connection,
                       viewer: sqlite3.Row | None) -> ItemScope:
    """Resolve which portfolio items ``viewer`` may see and manage.

    Admins get their own whole portfolio (unrestricted). Helpers get the union
    of their grants resolved against the owner's *current* items, so a brand
    grant automatically picks up items the owner adds to that brand later.
    Anonymous or ungranted callers are denied.

    Args:
        conn: Open DB connection (``sqlite3.Row`` row factory expected).
        viewer: The current user row, or ``None`` for an anonymous caller.

    Returns:
        An :class:`ItemScope`. ``denied`` / ``unrestricted`` summarize it.
    """
    if viewer is None:
        logger.warning("Item-scope resolution for anonymous caller; denying")
        return _ITEM_DENY

    viewer_id = viewer["id"]

    # The account manager owns their portfolio outright: no grant lookup, and no
    # id filter — every item they own is in scope.
    if security.is_admin(viewer):
        return ItemScope(owner_id=viewer_id, item_ids=None)

    grants = _load_grants(conn, viewer_id)
    if not grants:
        logger.info("Helper user_id=%s has no portfolio grants; item scope empty",
                    viewer_id)
        return _ITEM_DENY

    owner_id = _single_owner(grants, viewer_id)

    # A whole-portfolio grant subsumes any narrower brand/item grants.
    if _has_all_grant(grants):
        logger.debug("Helper user_id=%s has an 'all' grant from owner %s",
                     viewer_id, owner_id)
        return ItemScope(owner_id=owner_id, item_ids=None)

    item_ids: set[int] = set()
    item_ids.update(_items_for_brands(conn, owner_id, _granted_brands(grants)))
    item_ids.update(_explicit_items(conn, owner_id, grants))

    logger.info("Helper user_id=%s item scope: owner=%s items=%d",
                viewer_id, owner_id, len(item_ids))
    return ItemScope(owner_id=owner_id, item_ids=frozenset(item_ids))


def resolve_ci_scope(conn: sqlite3.Connection,
                     viewer: sqlite3.Row | None) -> CiScope:
    """Resolve which Competitive monitoring groups ``viewer`` may see.

    Competitive access is **brand-matched**: a helper sees a monitoring group
    only when the group's own ("mine") brand matches a brand they were granted.
    A whole-portfolio grant unlocks every group; an item-only grant unlocks
    nothing competitive (a group is brand-level, and the helper wasn't given the
    brand). Admins get all of their own groups.

    Args:
        conn: Open DB connection.
        viewer: The current user row, or ``None``.

    Returns:
        A :class:`CiScope`.
    """
    if viewer is None:
        logger.warning("CI-scope resolution for anonymous caller; denying")
        return _CI_DENY

    viewer_id = viewer["id"]

    if security.is_admin(viewer):
        return CiScope(owner_id=viewer_id, group_ids=None)

    grants = _load_grants(conn, viewer_id)
    if not grants:
        logger.info("Helper user_id=%s has no grants; CI scope empty", viewer_id)
        return _CI_DENY

    owner_id = _single_owner(grants, viewer_id)

    if _has_all_grant(grants):
        return CiScope(owner_id=owner_id, group_ids=None)

    # Only brand grants unlock competitive data; item-only grants do not.
    brands = _granted_brands(grants)
    if not brands:
        logger.info("Helper user_id=%s has only item grants; no CI access", viewer_id)
        return CiScope(owner_id=owner_id, group_ids=frozenset())

    group_ids = _groups_for_brands(conn, owner_id, brands)
    logger.info("Helper user_id=%s CI scope: owner=%s groups=%d",
                viewer_id, owner_id, len(group_ids))
    return CiScope(owner_id=owner_id, group_ids=frozenset(group_ids))


# --- internals -------------------------------------------------------------

def _load_grants(conn: sqlite3.Connection, grantee_user_id: int) -> list[sqlite3.Row]:
    """Return every grant row handed to ``grantee_user_id``."""
    return conn.execute(
        "SELECT owner_user_id, scope_type, scope_ref FROM portfolio_grants "
        "WHERE grantee_user_id = ?",
        (grantee_user_id,),
    ).fetchall()


def _single_owner(grants: list[sqlite3.Row], viewer_id: int) -> int:
    """Return the owning account manager for a helper's grants.

    The current model is single-manager, so every grant a helper holds should
    reference the same owner. If somehow they don't (an unsupported multi-manager
    state we'd handle in the SaaS phase), pick the lowest id deterministically
    and warn rather than silently mixing two portfolios.
    """
    owners = {g["owner_user_id"] for g in grants}
    if len(owners) > 1:
        logger.warning(
            "Helper user_id=%s holds grants from multiple owners %s; "
            "scoping to lowest id (single-manager model expected)",
            viewer_id, sorted(owners),
        )
    return min(owners)


def _has_all_grant(grants: list[sqlite3.Row]) -> bool:
    """True if any grant opens the owner's whole portfolio."""
    return any(g["scope_type"] == "all" for g in grants)


def _granted_brands(grants: list[sqlite3.Row]) -> list[str]:
    """Lowercased brand names from the helper's brand grants (deduped)."""
    seen = {
        g["scope_ref"].strip().lower()
        for g in grants
        if g["scope_type"] == "brand" and g["scope_ref"] and g["scope_ref"].strip()
    }
    return sorted(seen)


def _items_for_brands(conn: sqlite3.Connection, owner_id: int,
                      brands: list[str]) -> set[int]:
    """Owner's current item ids whose brand matches any granted brand.

    Case-insensitive, matching how brands are stored free-text on items. Items
    with no brand never match a brand grant.
    """
    if not brands:
        return set()
    # Only the placeholder COUNT is interpolated (never user data); the brand
    # values are bound parameters, so this stays injection-safe.
    placeholders = ", ".join("?" for _ in brands)
    rows = conn.execute(
        f"SELECT id FROM portfolio_items "
        f"WHERE user_id = ? AND LOWER(brand) IN ({placeholders})",
        (owner_id, *brands),
    ).fetchall()
    return {r["id"] for r in rows}


def _explicit_items(conn: sqlite3.Connection, owner_id: int,
                    grants: list[sqlite3.Row]) -> set[int]:
    """Item ids from 'item' grants, kept only if the owner actually owns them.

    The owner-id filter is the authorization check: a grant can never leak an
    item the named owner doesn't own, even if the stored ref is stale or forged.
    Non-numeric refs are ignored defensively (the column is free text).
    """
    ids = [
        int(g["scope_ref"])
        for g in grants
        if g["scope_type"] == "item" and g["scope_ref"] and str(g["scope_ref"]).isdigit()
    ]
    if not ids:
        return set()
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT id FROM portfolio_items "
        f"WHERE user_id = ? AND id IN ({placeholders})",
        (owner_id, *ids),
    ).fetchall()
    return {r["id"] for r in rows}


def _groups_for_brands(conn: sqlite3.Connection, owner_id: int,
                       brands: list[str]) -> set[int]:
    """Owner's monitoring groups whose own ('mine') brand matches a granted brand."""
    if not brands:
        return set()
    placeholders = ", ".join("?" for _ in brands)
    rows = conn.execute(
        f"SELECT DISTINCT g.id FROM ci_groups g "
        f"JOIN ci_brands b ON b.group_id = g.id "
        f"WHERE g.user_id = ? AND b.type = 'mine' "
        f"AND LOWER(b.name) IN ({placeholders})",
        (owner_id, *brands),
    ).fetchall()
    return {r["id"] for r in rows}
