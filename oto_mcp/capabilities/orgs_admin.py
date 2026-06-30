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
from ._authz import SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class CreateOrgInput(BaseModel):
    name: str


class OrgIdInput(BaseModel):
    org_id: int


def _create_org(ctx: ResolvedCtx, inp: CreateOrgInput) -> dict:
    name = (inp.name or "").strip()
    if not name:
        raise AuthzDenied(400, "missing_name", "Nom d'org requis.")
    org_id = org_store.create_org(name, created_by=ctx.sub)
    return {"id": org_id, "org_id": org_id, "name": name}  # superset REST({id}) + MCP({org_id,name})


def _archive_org(ctx: ResolvedCtx, inp: OrgIdInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    archived = org_store.archive_org(inp.org_id)
    return {"ok": True, "org_id": inp.org_id, "archived": archived}


CAPABILITIES += [
    Capability(
        key="org.admin.create", handler=_create_org, Input=CreateOrgInput,
        authz=SUPER_ADMIN,
        description="[super admin] Create an organization (perimeter). Returns its id.",
        # MCP fusionné dans oto_admin_org(op=create). REST conservé (dashboard).
        rest=RestBinding("POST", "/api/admin/orgs"),
    ),
    Capability(
        key="org.admin.archive", handler=_archive_org, Input=OrgIdInput,
        authz=SUPER_ADMIN,
        description="[super admin] Archive (soft-delete) an org: hidden from all "
                    "listings, reversible in DB. Members fall back to their other orgs.",
        # MCP fusionné dans oto_admin_org(op=archive). REST conservé (dashboard).
        rest=RestBinding("DELETE", "/api/admin/orgs/{id}", _ID),
    ),
]
