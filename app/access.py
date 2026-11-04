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

    def allows(self, item_id: int) -> bool:
        """Whether one specific item is in scope — the per-item manage gate.

        A route about to act on ``item_id`` (toggle tracking, edit keywords)
        calls this before touching it. Denied viewers allow nothing; an
        unrestricted scope allows any of the owner's items; otherwise the id
        must be in the resolved set. Pair it with passing ``owner_id`` to the
        data layer, so the write both targets the owner's row and is authorized.
        """
        if self.owner_id is None:
            return False
        if self.item_ids is None:
            return True
        return item_id in self.item_ids


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

    # Helpers are the only accounts scoped to *someone else's* portfolio. A
    # helper is a non-admin holding at least one grant. Everyone else — an admin
    # account manager, or a plain self-service account with no grants — owns
    # their own items and sees all of them (owner_id == viewer_id).
    if not security.is_admin(viewer):
        grants = _load_grants(conn, viewer_id)
        if grants:
            owner_id = _single_owner(grants, viewer_id)
            # A whole-portfolio grant subsumes any narrower brand/item grants.
            if _has_all_grant(grants):
                logger.debug("Helper user_id=%s has an 'all' grant from owner %s",
                             viewer_id, owner_id)
                return ItemScope(owner_id=owner_id, item_ids=None)
            # A helper who also owned items (none do today) would be scoped to the
            # granting owner only, not their own — revisit for a multi-owner model.
            item_ids: set[int] = set()
            item_ids.update(_items_for_brands(conn, owner_id, _granted_brands(grants)))
            item_ids.update(_explicit_items(conn, owner_id, grants))
            logger.info("Helper user_id=%s item scope: owner=%s items=%d",
                        viewer_id, owner_id, len(item_ids))
            return ItemScope(owner_id=owner_id, item_ids=frozenset(item_ids))

    # Owner path (admin, or any account with no grants): their own whole portfolio.
    return ItemScope(owner_id=viewer_id, item_ids=None)


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

    # Same shape as item scope: only a non-admin with grants is a helper scoped
    # to someone else's groups; everyone else sees their own.
    if not security.is_admin(viewer):
        grants = _load_grants(conn, viewer_id)
        if grants:
            owner_id = _single_owner(grants, viewer_id)
            if _has_all_grant(grants):
                return CiScope(owner_id=owner_id, group_ids=None)
            # Only brand grants unlock competitive data; item-only grants do not.
            brands = _granted_brands(grants)
            if not brands:
                logger.info("Helper user_id=%s has only item grants; no CI access",
                            viewer_id)
                return CiScope(owner_id=owner_id, group_ids=frozenset())
            group_ids = _groups_for_brands(conn, owner_id, brands)
            logger.info("Helper user_id=%s CI scope: owner=%s groups=%d",
                        viewer_id, owner_id, len(group_ids))
            return CiScope(owner_id=owner_id, group_ids=frozenset(group_ids))

    return CiScope(owner_id=viewer_id, group_ids=None)


def is_helper(conn: sqlite3.Connection, viewer: sqlite3.Row | None) -> bool:
    """True when the viewer is a helper: a non-admin holding at least one grant.

    Helpers manage a slice of an account manager's portfolio and never own
    items, so routes use this to block adding, importing, and deleting. Admins
    and plain self-service accounts (no grants) own their own portfolio and keep
    full control of it — so they are *not* helpers.
    """
    if viewer is None or security.is_admin(viewer):
        return False
    return bool(_load_grants(conn, viewer["id"]))


# --- grant administration --------------------------------------------------
# Resolvers above turn stored grants into filters; the functions here are the
# write side — how an account manager creates and revokes those grants. Both
# are strictly scoped to ``owner_id`` (the acting manager) so one manager can
# never read, create, or revoke a grant over another manager's portfolio, and
# every brand/item reference is validated against the owner's *current*
# portfolio before a row is written (fail-closed: an unknown brand or an item
# the owner doesn't hold is rejected, never silently stored).

# Scope types accepted from the admin UI, mirrored from the DB CHECK constraint
# on portfolio_grants.scope_type. Kept here as the single allowlist the write
# path validates against.
VALID_SCOPE_TYPES = frozenset({"all", "brand", "item"})


class GrantError(ValueError):
    """A grant could not be created because the input was invalid or unauthorized.

    Carries a human-readable message safe to surface to the admin (no internal
    details); the route flashes it back on the User Management page.
    """


def list_grants(conn: sqlite3.Connection, owner_id: int,
                grantee_id: int) -> list[sqlite3.Row]:
    """Grants ``owner_id`` has handed ``grantee_id``, newest first.

    Scoped to the owning manager so the admin panel shows (and can revoke) only
    the grants that manager issued. Includes the row ``id`` the revoke route
    needs.
    """
    return conn.execute(
        "SELECT id, scope_type, scope_ref, created_at FROM portfolio_grants "
        "WHERE owner_user_id = ? AND grantee_user_id = ? "
        "ORDER BY created_at DESC, id DESC",
        (owner_id, grantee_id),
    ).fetchall()


def grant_access(conn: sqlite3.Connection, owner_id: int, grantee_id: int,
                 scope_type: str, scope_ref: str | None) -> int:
    """Create one access grant from ``owner_id`` to ``grantee_id``; return its id.

    Validates everything the DB can't: the scope type is allowlisted, a brand
    grant names a brand the owner actually has, and an item grant names an item
    the owner actually owns. ``all`` grants ignore ``scope_ref``. The caller is
    responsible for confirming ``grantee_id`` is a real non-admin account.

    Args:
        conn: Open DB connection.
        owner_id: The account manager issuing the grant (owns the items).
        grantee_id: The helper account receiving access.
        scope_type: One of ``all`` / ``brand`` / ``item``.
        scope_ref: Brand name (brand), portfolio item id as text (item), or
            ignored (all).

    Returns:
        The new ``portfolio_grants`` row id.

    Raises:
        GrantError: Invalid scope type, a brand/item that doesn't belong to the
            owner, or a grant that already exists (duplicate).
    """
    scope_type = (scope_type or "").strip()
    if scope_type not in VALID_SCOPE_TYPES:
        raise GrantError("Pick a valid access scope.")

    # Normalize the stored ref per scope type, validating against the owner's
    # own portfolio so a forged/stale form value can never create a grant over
    # something the owner doesn't hold.
    ref: str | None
    if scope_type == "all":
        ref = None
    elif scope_type == "brand":
        ref = _canonical_owner_brand(conn, owner_id, scope_ref)
        if ref is None:
            raise GrantError("That brand isn't in your portfolio.")
    else:  # item
        if not scope_ref or not str(scope_ref).strip().isdigit():
            raise GrantError("Pick an item to grant.")
        item_id = int(str(scope_ref).strip())
        if not _owner_has_item(conn, owner_id, item_id):
            raise GrantError("That item isn't in your portfolio.")
        ref = str(item_id)

    try:
        cur = conn.execute(
            "INSERT INTO portfolio_grants "
            "(owner_user_id, grantee_user_id, scope_type, scope_ref) "
            "VALUES (?, ?, ?, ?)",
            (owner_id, grantee_id, scope_type, ref),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        # The unique dedupe index rejects an identical grant; surface it plainly
        # rather than letting the write blow up the request.
        conn.rollback()
        logger.info("Duplicate grant skipped: owner=%s grantee=%s %s/%s",
                    owner_id, grantee_id, scope_type, ref)
        raise GrantError("That access is already granted.") from None

    grant_id = cur.lastrowid
    logger.info("Grant created id=%s owner=%s grantee=%s scope=%s ref=%s",
                grant_id, owner_id, grantee_id, scope_type, ref)
    return grant_id


def revoke_grant(conn: sqlite3.Connection, owner_id: int, grant_id: int) -> bool:
    """Revoke one grant ``owner_id`` issued; return True if a row was removed.

    Scoped to ``owner_id`` so a manager can only ever revoke their own grants,
    even if a tampered form supplies someone else's grant id (the owner filter
    simply matches nothing then).
    """
    cur = conn.execute(
        "DELETE FROM portfolio_grants WHERE id = ? AND owner_user_id = ?",
        (grant_id, owner_id),
    )
    conn.commit()
    removed = cur.rowcount > 0
    if removed:
        logger.info("Grant revoked id=%s owner=%s", grant_id, owner_id)
    else:
        # Not an error — a stale link or a grant already gone — but worth a
        # breadcrumb since it can also mean a cross-owner revoke attempt.
        logger.warning("Revoke matched no grant id=%s for owner=%s", grant_id, owner_id)
    return removed


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


def _canonical_owner_brand(conn: sqlite3.Connection, owner_id: int,
                           brand: str | None) -> str | None:
    """Return the owner's stored brand matching ``brand`` (case-insensitively), or None.

    A brand grant is validated against the owner's *own* items, so an admin can
    only ever grant a brand that exists in their portfolio. The stored casing is
    returned so the grant row — and the label the UI shows — reads the way the
    owner entered the brand, while matching stays case-insensitive everywhere.
    """
    if not brand or not brand.strip():
        return None
    row = conn.execute(
        "SELECT brand FROM portfolio_items "
        "WHERE user_id = ? AND LOWER(TRIM(brand)) = ? LIMIT 1",
        (owner_id, brand.strip().lower()),
    ).fetchone()
    return row["brand"] if row else None


def _owner_has_item(conn: sqlite3.Connection, owner_id: int, item_id: int) -> bool:
    """True when ``item_id`` is a portfolio item the owner actually holds.

    The authorization check behind an item grant: it guarantees a grant can
    never reference an item outside the granting manager's own portfolio.
    """
    return conn.execute(
        "SELECT 1 FROM portfolio_items WHERE id = ? AND user_id = ? LIMIT 1",
        (item_id, owner_id),
    ).fetchone() is not None
