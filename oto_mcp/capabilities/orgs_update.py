"""Capacité d'écriture des métadonnées d'org (ADR 0009).

Renommer / re-décrire son org était impossible : `org.create` posait le nom une
fois, et aucune capacité ne l'éditait ensuite. On comble le trou en miroir de
`group.update` (groups.py) : un handler core + Input pydantic + autz
`ORG_ADMIN_OF` (org_admin de cette org, ou escalade platform_admin). Multi-binding
REST (self `/api/orgs/{id}` + admin `/api/admin/orgs/{id}`), comme membres/secrets.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .. import org_store
from ._authz import ORG_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class UpdateOrgInput(BaseModel):
    org_id: int
    name: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=2000)
    # Profil d'entreprise (2026-07-02). `domain` = domaine de marque (acme.com),
    # normalisé org_store.normalize_domain ; dérive le logo via logo.dev quand
    # aucun logo n'est uploadé. Chaîne vide = effacer le champ.
    domain: Optional[str] = Field(None, max_length=253)
    industry: Optional[str] = Field(None, max_length=120)
    location: Optional[str] = Field(None, max_length=120)


def _update_org(ctx: ResolvedCtx, inp: UpdateOrgInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if inp.name is not None and not inp.name.strip():
        raise AuthzDenied(400, "invalid_name", "Nom d'org vide.")
    try:
        org_store.update_org(inp.org_id, name=inp.name, description=inp.description,
                             domain=inp.domain, industry=inp.industry,
                             location=inp.location)
    except ValueError as e:  # domaine non-normalisable (saisie libre org_admin)
        raise AuthzDenied(400, "invalid_domain", str(e))
    o = org_store.get_org(inp.org_id) or {}
    return {"ok": True, "org_id": inp.org_id,
            "name": o.get("name"), "description": o.get("description"),
            "domain": o.get("domain"), "industry": o.get("industry"),
            "location": o.get("location"),
            "logo_url": org_store.effective_logo_url(o)}


CAPABILITIES += [
    Capability(
        key="org.update", handler=_update_org, Input=UpdateOrgInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Update an organization's profile (name, description, brand "
                     "domain like acme.com, industry, location). The domain also "
                     "drives the org logo when none is uploaded. "
                     "You must be org_admin of this org."),
        mcp="oto_update_org",
        rest=(RestBinding("PATCH", "/api/orgs/{id}", _ID),
              RestBinding("PATCH", "/api/admin/orgs/{id}", _ID)),
    ),
]
