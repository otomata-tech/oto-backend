"""RBAC connecteur INTERNE à l'org (ADR 0025) — surface org_admin.

L'org_admin réserve un connecteur à un sous-ensemble de son org : des **départements**
(groupes) et/ou des **membres** nommés. La présence de ≥1 principal sur un connecteur le
rend RESTREINT (deny-by-default) ; sans principal il reste ouvert à toute l'org. L'accès
est enforced DUR ailleurs (visibilité `session_visibility` + call-time `access.require_connector_access`).
Cette capacité ne fait que gouverner l'ACL (table `org_connector_access`).

autz `ORG_ADMIN_OF` : l'org_admin gouverne SON org (super_admin escalade via roles).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from .. import db, group_store, org_store, providers
from ._authz import GROUP_ADMIN_OF, GROUP_MEMBER_OF, ORG_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}
_ID_CONN = {"id": "org_id", "connector": "connector"}
_GID = {"id": "group_id"}
_GID_CONN = {"id": "group_id", "connector": "connector"}


class AclListInput(BaseModel):
    org_id: int


class AclSetInput(BaseModel):
    org_id: int
    connector: str
    principal_type: Literal["group", "user"]
    principal_id: str


def _validate(inp: AclSetInput) -> None:
    """Connecteur réel + principal appartenant à l'org (anti-typo / anti-IDOR)."""
    if providers.connector_for_provider(inp.connector) is None:
        raise AuthzDenied(400, "unknown_connector", f"Connecteur `{inp.connector}` inconnu.")
    if inp.principal_type == "group":
        try:
            g = group_store.get_group(int(inp.principal_id))
        except (ValueError, TypeError):
            raise AuthzDenied(400, "invalid_principal", "principal_id de groupe doit être un entier.")
        if not g or g.get("org_id") != inp.org_id:
            raise AuthzDenied(400, "group_not_in_org",
                              f"Le groupe #{inp.principal_id} n'appartient pas à l'org #{inp.org_id}.")
    else:  # user
        if org_store.get_org_role(inp.org_id, inp.principal_id) is None:
            raise AuthzDenied(400, "user_not_in_org",
                              f"`{inp.principal_id}` n'est pas membre de l'org #{inp.org_id}.")


def _list_acl(ctx: ResolvedCtx, inp: AclListInput) -> dict:
    return {
        "org_id": inp.org_id,
        "access": db.list_connector_access(inp.org_id),
        "restricted": sorted(db.org_restricted_connectors(inp.org_id)),
    }


def _grant(ctx: ResolvedCtx, inp: AclSetInput) -> dict:
    _validate(inp)
    db.set_connector_access(inp.org_id, inp.connector, inp.principal_type,
                            inp.principal_id, granted_by=ctx.sub)
    return {"ok": True, "org_id": inp.org_id, "connector": inp.connector,
            "principal_type": inp.principal_type, "principal_id": inp.principal_id,
            "restricted": True}


def _revoke(ctx: ResolvedCtx, inp: AclSetInput) -> dict:
    db.clear_connector_access(inp.org_id, inp.connector, inp.principal_type, inp.principal_id)
    still = inp.connector in db.org_restricted_connectors(inp.org_id)
    return {"ok": True, "org_id": inp.org_id, "connector": inp.connector,
            "principal_type": inp.principal_type, "principal_id": inp.principal_id,
            "restricted": still}  # False = dernier principal retiré → connecteur réouvert


# ── ACL au grain ÉQUIPE (ADR 0012 B2, restrict-only) ─────────────────────────
# Un chef d'équipe (`GROUP_ADMIN_OF`) réserve un connecteur à un sous-ensemble de
# MEMBRES de SON équipe. Intersection avec l'ACL d'org (narrowing pur — l'équipe ne
# peut que restreindre davantage, jamais débloquer ce que l'org autorise). Enforced
# DUR ailleurs (visibilité + `require_connector_access`, verdict `_connector_blocked`).

class GroupAclListInput(BaseModel):
    group_id: int


class GroupAclSetInput(BaseModel):
    group_id: int
    connector: str
    member: str          # sub d'un membre de l'équipe (le seul type de principal ici)


def _validate_group(inp: GroupAclSetInput) -> None:
    """Connecteur réel + membre appartenant à l'ÉQUIPE (anti-typo / anti-IDOR)."""
    if providers.connector_for_provider(inp.connector) is None:
        raise AuthzDenied(400, "unknown_connector", f"Connecteur `{inp.connector}` inconnu.")
    if not group_store.is_group_member(inp.member, inp.group_id):
        raise AuthzDenied(400, "user_not_in_group",
                          f"`{inp.member}` n'est pas membre de l'équipe #{inp.group_id}.")


def _group_list_acl(ctx: ResolvedCtx, inp: GroupAclListInput) -> dict:
    return {
        "group_id": inp.group_id,
        "access": db.list_group_connector_access(inp.group_id),
        "restricted": sorted(db.group_restricted_connectors(inp.group_id)),
    }


def _group_grant(ctx: ResolvedCtx, inp: GroupAclSetInput) -> dict:
    _validate_group(inp)
    db.set_group_connector_access(inp.group_id, inp.connector, inp.member, granted_by=ctx.sub)
    return {"ok": True, "group_id": inp.group_id, "connector": inp.connector,
            "member": inp.member, "restricted": True}


def _group_revoke(ctx: ResolvedCtx, inp: GroupAclSetInput) -> dict:
    db.clear_group_connector_access(inp.group_id, inp.connector, inp.member)
    still = inp.connector in db.group_restricted_connectors(inp.group_id)
    return {"ok": True, "group_id": inp.group_id, "connector": inp.connector,
            "member": inp.member, "restricted": still}  # False = dernier retiré → réouvert


CAPABILITIES += [
    Capability(
        key="connectors.acl.group_list", handler=_group_list_acl, Input=GroupAclListInput,
        authz=GROUP_MEMBER_OF("group_id"),
        description="[team] List the connector access rules of a team (which connectors are reserved, "
                    "and to which members). Team-level RBAC narrows the org's — it can only further restrict.",
        rest=RestBinding("GET", "/api/groups/{id}/connectors/acl", _GID),
    ),
    Capability(
        key="connectors.acl.group_grant", handler=_group_grant, Input=GroupAclSetInput,
        authz=GROUP_ADMIN_OF("group_id"), refresh_visibility=True,
        description="[team lead] Reserve a connector to a member of your team (member=<sub>). Adding the "
                    "first member makes it restricted within the team (deny-by-default) — a further "
                    "narrowing of the org, never an expansion.",
        rest=RestBinding("POST", "/api/groups/{id}/connectors/{connector}/access", _GID_CONN),
    ),
    Capability(
        key="connectors.acl.group_revoke", handler=_group_revoke, Input=GroupAclSetInput,
        authz=GROUP_ADMIN_OF("group_id"), refresh_visibility=True,
        description="[team lead] Remove a member from a connector's team access list. Removing the last "
                    "member reopens the connector to the whole team.",
        rest=RestBinding("DELETE", "/api/groups/{id}/connectors/{connector}/access", _GID_CONN),
    ),
    Capability(
        key="connectors.acl.list", handler=_list_acl, Input=AclListInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] List the connector access rules of an org (which connectors are "
                    "restricted, and to which departments/members).",
        rest=RestBinding("GET", "/api/orgs/{id}/connectors/acl", _ID),
    ),
    Capability(
        key="connectors.acl.grant", handler=_grant, Input=AclSetInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Restrict a connector to a department (principal_type='group', "
                    "principal_id=<group_id>) or a member (principal_type='user', principal_id=<sub>). "
                    "Adding the first principal makes the connector restricted (deny-by-default) for the org.",
        rest=RestBinding("POST", "/api/orgs/{id}/connectors/{connector}/access", _ID_CONN),
    ),
    Capability(
        key="connectors.acl.revoke", handler=_revoke, Input=AclSetInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="[org admin] Remove a department/member from a connector's access list. Removing "
                    "the last principal reopens the connector to the whole org.",
        rest=RestBinding("DELETE", "/api/orgs/{id}/connectors/{connector}/access", _ID_CONN),
    ),
]
