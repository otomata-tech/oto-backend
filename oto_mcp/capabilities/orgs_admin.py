"""Capacités orgs super-admin (ADR 0009, barreau 2c).

Écritures sur les orgs tierces : créer une org, accorder/révoquer un
entitlement de namespace gouverné. Agir sur une org tierce = escalade en masse
→ réservé au **SUPER_ADMIN** (pas à l'admin opérationnel). Les réponses sont des
**supersets** des deux contrats historiques (mêmes clés qu'avant côté MCP ET
côté REST) pour ne casser aucun consommateur.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import org_store
from ..tool_visibility import ADMIN_GRANT_ONLY_NAMESPACES
from ._authz import SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class CreateOrgInput(BaseModel):
    name: str


class EntitlementInput(BaseModel):
    org_id: int
    namespace: str


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    name = (inp.name or "").strip()
    if not name:
        raise AuthzDenied(400, "missing_name", "Nom d'org requis.")
    org_id = org_store.create_org(name, created_by=ctx.sub)
    return {"id": org_id, "org_id": org_id, "name": name}  # superset REST({id}) + MCP({org_id,name})


def _grant_entitlement(ctx: ResolvedCtx, inp: EntitlementInput) -> dict:
    if inp.namespace not in ADMIN_GRANT_ONLY_NAMESPACES:
        raise AuthzDenied(400, "namespace_not_controlled",
                          f"`{inp.namespace}` n'est pas un namespace gouverné.")
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    org_store.grant_org_entitlement(inp.org_id, inp.namespace, granted_by=ctx.sub)
    return {"ok": True, "org_id": inp.org_id, "namespace": inp.namespace, "granted": True}


def _revoke_entitlement(ctx: ResolvedCtx, inp: EntitlementInput) -> dict:
    existed = org_store.revoke_org_entitlement(inp.org_id, inp.namespace)
    return {"ok": True, "org_id": inp.org_id, "namespace": inp.namespace,
            "revoked": existed, "existed": existed}


CAPABILITIES += [
    Capability(
        key="org.admin.create", handler=_create_org, Input=CreateOrgInput,
        authz=SUPER_ADMIN,
        description="[super admin] Create an organization (perimeter). Returns its id.",
        mcp="oto_admin_create_org",
        rest=RestBinding("POST", "/api/admin/orgs"),
    ),
    Capability(
        key="org.entitlement.grant", handler=_grant_entitlement, Input=EntitlementInput,
        authz=SUPER_ADMIN,
        description="[super admin] Entitle an org to a controlled (grant-only) namespace.",
        mcp="oto_admin_grant_org_entitlement",
        rest=RestBinding("POST", "/api/admin/orgs/{id}/entitlements/{namespace}", _ID),
    ),
    Capability(
        key="org.entitlement.revoke", handler=_revoke_entitlement, Input=EntitlementInput,
        authz=SUPER_ADMIN,
        description="[super admin] Revoke an org's entitlement to a controlled namespace.",
        mcp="oto_admin_revoke_org_entitlement",
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}/entitlements/{namespace}", _ID),
    ),
]
