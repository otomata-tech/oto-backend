"""Capabilities "access level of a connector" — an org, then a team, narrows a consent.

Design validated on 14/10/2026 (connectors repo, `docs/conception/niveaux-d-acces.md`);
the declaration, the resolution and the monotony live in `connectors/access_levels.py`,
where the level bites in `connectors/access_level_gate.py`. Here, the three verbs at the
two scopes: read, set, clear.

- **org** (`/api/orgs/{id}/connectors/{name}/access-level`): `ORG_ADMIN_OF`;
- **team** (`/api/groups/{id}/connectors/{name}/access-level`): `GROUP_ADMIN_OF` (team
  lead, or an admin of its org). A team only NARROWS its org's level — a wider one is
  refused (409 `access_level_wider_than_org`).

The MCP face is the console's `oto_connector_access_level` (`console.py`): no secret here.
`refresh_visibility=True`: the tools the level stops covering leave the caller's session
at once; the other members' at their next session (visibility), and at their NEXT CALL
for the guard, which reads the level at each call.

⚠️ Widening a level does not widen a token: a credential obtained under `read` lacks the
write scopes, and asking for them is a new consent.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ... import org_store, providers
from ...connectors import access_levels
from .._authz import GROUP_ADMIN_OF, ORG_ADMIN_OF
from .._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from ..registry import CAPABILITIES

_ID = {"id": "org_id"}      # placeholder {id} → Input field org_id
_GID = {"id": "group_id"}   # placeholder {id} → Input field group_id


class OrgAccessLevelInput(BaseModel):
    org_id: int
    name: str                  # connector (placeholder {name}, auto-mapped)


class OrgAccessLevelSetInput(OrgAccessLevelInput):
    level: str


class GroupAccessLevelInput(BaseModel):
    group_id: int
    name: str


class GroupAccessLevelSetInput(GroupAccessLevelInput):
    level: str


class AccessLevelDecl(BaseModel):
    """A declared level and the scopes its consent asks for."""
    name: str
    scopes: list[str]


class AccessLevelState(BaseModel):
    """The level of a connector at one scope. `level` = what THIS scope sets (None = it
    sets nothing); `effective` = what applies under it (the org's level for a team that
    sets nothing, else the connector's default level — `full` unless it declares another);
    `source` ∈ group|org|default says who decided.
    `levels` = the declared levels, `full` filtering nothing. `deleted` (clear only):
    `false` = there was nothing to remove (idempotent)."""
    connector: str
    scope: str
    org_id: int
    group_id: Optional[int] = None
    level: Optional[str] = None
    effective: str
    source: str
    levels: list[AccessLevelDecl]
    deleted: Optional[bool] = None


def _decl(name: str) -> access_levels.AccessLevels:
    if name not in providers.REGISTRY:
        raise AuthzDenied(404, "unknown_connector", f"Connector `{name}` unknown.")
    try:
        return access_levels.require_declared(name)
    except access_levels.AccessLevelError as e:
        raise AuthzDenied(400, e.code, str(e))


def _exiger_org(org_id: int) -> None:
    if not org_store.get_org(org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{org_id} unknown.")


def _state(decl, scope: str, org_id: int, group_id: Optional[int]) -> dict:
    valeurs = access_levels.settings(org_id, group_id)
    effective, source = access_levels.resolve(decl.connector, org_id, group_id, valeurs)
    pose = valeurs.get(providers.credential_provider(decl.connector), {}).get(scope)
    return {"connector": decl.connector, "scope": scope, "org_id": org_id,
            "group_id": group_id, "level": pose, "effective": effective,
            "source": source,
            "levels": [{"name": n, "scopes": list(s)} for n, s in decl.levels.items()]}


def _set(scope: str, scope_id: int, org_id: int, name: str, level: str, sub: str) -> None:
    try:
        access_levels.set_level(scope, scope_id, name, level, sub, org_id=org_id)
    except access_levels.AccessLevelError as e:
        if e.code == "access_level_wider_than_org":
            raise AuthzDenied(409, e.code, str(e))
        raise AuthzDenied(400, e.code, str(e))


def _org_get(ctx: ResolvedCtx, inp: OrgAccessLevelInput) -> dict:
    decl = _decl(inp.name)
    _exiger_org(inp.org_id)
    return _state(decl, "org", inp.org_id, None)


def _org_set(ctx: ResolvedCtx, inp: OrgAccessLevelSetInput) -> dict:
    decl = _decl(inp.name)
    _exiger_org(inp.org_id)
    _set("org", inp.org_id, inp.org_id, inp.name, inp.level, ctx.sub)
    return _state(decl, "org", inp.org_id, None)


def _org_clear(ctx: ResolvedCtx, inp: OrgAccessLevelInput) -> dict:
    decl = _decl(inp.name)
    _exiger_org(inp.org_id)
    deleted = access_levels.clear_level("org", inp.org_id, inp.name)
    return {**_state(decl, "org", inp.org_id, None), "deleted": deleted}


def _group_get(ctx: ResolvedCtx, inp: GroupAccessLevelInput) -> dict:
    return _state(_decl(inp.name), "group", ctx.org_id, inp.group_id)


def _group_set(ctx: ResolvedCtx, inp: GroupAccessLevelSetInput) -> dict:
    decl = _decl(inp.name)
    _set("group", inp.group_id, ctx.org_id, inp.name, inp.level, ctx.sub)
    return _state(decl, "group", ctx.org_id, inp.group_id)


def _group_clear(ctx: ResolvedCtx, inp: GroupAccessLevelInput) -> dict:
    decl = _decl(inp.name)
    deleted = access_levels.clear_level("group", inp.group_id, inp.name)
    return {**_state(decl, "group", ctx.org_id, inp.group_id), "deleted": deleted}


_ORG = "/api/orgs/{id}/connectors/{name}/access-level"
_GROUP = "/api/groups/{id}/connectors/{name}/access-level"
_ERREURS = (DeclaredError(404, "unknown_connector", "connecteur absent du registre"),
            DeclaredError(400, "no_access_levels", "le connecteur ne déclare aucun niveau"))
_ERREURS_POSE = _ERREURS + (
    DeclaredError(400, "unknown_access_level", "niveau non déclaré par le connecteur"),)
_ORG_ERR = (DeclaredError(404, "unknown_org", "l'org n'existe pas"),)

CAPABILITIES += [
    Capability(
        key="connectors.access_level.org_get", handler=_org_get,
        Input=OrgAccessLevelInput, Output=AccessLevelState,
        authz=ORG_ADMIN_OF("org_id"), errors=_ERREURS + _ORG_ERR,
        description="[org admin] The access level of a connector in your org: the level "
                    "set (or none), the one in force, and the declared levels with the "
                    "scopes their consent asks for (`full` filters nothing).",
        rest=RestBinding("GET", _ORG, _ID),
    ),
    Capability(
        key="connectors.access_level.set_org", handler=_org_set,
        Input=OrgAccessLevelSetInput, Output=AccessLevelState,
        authz=ORG_ADMIN_OF("org_id"), refresh_visibility=True,
        errors=_ERREURS_POSE + _ORG_ERR,
        description="[org admin] Set the access level of a connector for your org: the "
                    "tools and ops it does not cover are neither served nor callable, and "
                    "the consent asks only for its scopes. Widening needs a new consent.",
        rest=RestBinding("PUT", _ORG, _ID),
    ),
    Capability(
        key="connectors.access_level.clear_org", handler=_org_clear,
        Input=OrgAccessLevelInput, Output=AccessLevelState,
        authz=ORG_ADMIN_OF("org_id"), refresh_visibility=True,
        errors=_ERREURS + _ORG_ERR,
        description="[org admin] Remove your org's access level for a connector: back "
                    "to its default level (`full` unless it declares another).",
        rest=RestBinding("DELETE", _ORG, _ID),
    ),
    Capability(
        key="connectors.access_level.group_get", handler=_group_get,
        Input=GroupAccessLevelInput, Output=AccessLevelState,
        authz=GROUP_ADMIN_OF("group_id"), errors=_ERREURS,
        description="[team lead] The access level of a connector for your team and the "
                    "one in force under it (the org's when the team sets none).",
        rest=RestBinding("GET", _GROUP, _GID),
    ),
    Capability(
        key="connectors.access_level.set_group", handler=_group_set,
        Input=GroupAccessLevelSetInput, Output=AccessLevelState,
        authz=GROUP_ADMIN_OF("group_id"), refresh_visibility=True,
        errors=_ERREURS_POSE + (DeclaredError(
            409, "access_level_wider_than_org",
            "le niveau élargirait celui de l'org : une équipe ne fait que restreindre"),),
        description="[team lead] Narrow the access level of a connector for your team. "
                    "Restrict-only: a level wider than the org's is refused.",
        rest=RestBinding("PUT", _GROUP, _GID),
    ),
    Capability(
        key="connectors.access_level.clear_group", handler=_group_clear,
        Input=GroupAccessLevelInput, Output=AccessLevelState,
        authz=GROUP_ADMIN_OF("group_id"), refresh_visibility=True, errors=_ERREURS,
        description="[team lead] Remove your team's access level for a connector: the "
                    "org's applies.",
        rest=RestBinding("DELETE", _GROUP, _GID),
    ),
]
