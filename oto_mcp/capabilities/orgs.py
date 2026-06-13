"""Capacités du domaine orgs (ADR 0009). Barreau 1 : `org.use_org`.

`oto_use_org` (MCP) et `PUT /api/me/active-org` (REST) étaient câblés séparément
(drift de surface). Une seule `Capability` les co-déclare ; les deux adaptateurs
en dérivent.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import org_store
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


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
