"""Capacités du domaine orgs (ADR 0009). Barreau 1 : `org.use_org`.

`oto_use_org` (MCP) et `PUT /api/me/active-org` (REST) étaient câblés séparément
(drift de surface). Une seule `Capability` les co-déclare ; les deux adaptateurs
en dérivent.
"""
from __future__ import annotations

import os

from pydantic import BaseModel, Field

from .. import org_store
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_MAX_ORGS_PER_USER = int(os.environ.get("OTO_MCP_MAX_ORGS_PER_USER", "10"))


class CreateOrgInput(BaseModel):
    name: str = Field(min_length=1, max_length=80)


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    """Self-serve : crée un espace, en fait l'admin, le bascule actif."""
    if org_store.count_orgs_created_by(ctx.sub) >= _MAX_ORGS_PER_USER:
        raise AuthzDenied(429, "org_quota",
                          f"Limite de {_MAX_ORGS_PER_USER} espaces créés atteinte.")
    name = inp.name.strip()
    if not name:
        raise AuthzDenied(400, "invalid_name", "Nom d'espace requis.")
    org_id = org_store.create_org(name, created_by=ctx.sub)
    org_store.add_org_member(org_id, ctx.sub, "org_admin")
    org_store.set_active_org(ctx.sub, org_id)
    return {"org_id": org_id, "name": name, "active_org": org_id, "org_role": "org_admin"}


class UseOrgInput(BaseModel):
    org: str  # id (ex "3") ou nom exact — contrat unifié MCP + REST


def _use_org(ctx: ResolvedCtx, inp: UseOrgInput) -> dict:
    try:
        org_id = org_store.resolve_org_for_user(ctx.sub, inp.org)
    except ValueError as e:
        raise AuthzDenied(404, "unknown_org", str(e))
    org_store.set_active_org(ctx.sub, org_id)  # membre garanti par resolve_org_for_user
    o = org_store.get_org(org_id)
    return {"active_org": org_id, "name": o["name"] if o else None}


CAPABILITIES += [
    Capability(
        key="org.create",
        handler=_create_org,
        Input=CreateOrgInput,
        authz=SUB_ONLY,
        description=(
            "Create your own organization (workspace). You become its org_admin "
            "and it becomes your active org. Self-serve — any authenticated user."
        ),
        mcp="oto_create_org",
        rest=RestBinding("POST", "/api/me/orgs"),
    ),
    Capability(
        key="org.use_org",
        handler=_use_org,
        Input=UseOrgInput,
        authz=SUB_ONLY,
        description=(
            "Switch your active organization (by id or name). The active org "
            "decides which shared secrets resolve for your tool calls. Global to "
            "your account (not per-session)."
        ),
        mcp="oto_use_org",
        rest=RestBinding("PUT", "/api/me/active-org"),
    ),
]
