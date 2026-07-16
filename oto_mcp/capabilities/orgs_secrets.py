"""Capacités d'écriture des secrets d'org (ADR 0009, barreau 2b).

Même réconciliation que les membres : MCP platform-admin-only vs REST org_admin
self-service → unifié sur **`ORG_ADMIN_OF`**. Multi-binding (self + admin). La
validation provider/base_url passe par **`connectors.org_secret_meta`** (source
unique — le REST l'utilisait déjà ; le MCP avait une validation à la main,
supprimée). Les secrets ne sont jamais renvoyés en clair.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import connectors, credentials_store, org_store
from ._authz import ORG_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class SetSecretInput(BaseModel):
    org_id: int
    provider: str
    api_key: str = ""                  # connecteurs mono-champ (clé simple)
    fields: Optional[dict[str, str]] = None   # connecteurs multi-champs (zoho/silae…)
    base_url: Optional[str] = None     # connecteurs remote uniquement (endpoint du bridge)


class DeleteSecretInput(BaseModel):
    org_id: int
    provider: str


def _set_secret(ctx: ResolvedCtx, inp: SetSecretInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    base_url = (inp.base_url or "").strip() or None
    meta, code = connectors.org_secret_meta(inp.provider, base_url)
    if code:
        raise AuthzDenied(400, code, f"Provider/base_url invalide : {code}.")
    # Mono-champ (api_key) ou multi-champs (fields packés) — source unique.
    try:
        secret = credentials_store.secret_from_input(inp.provider, inp.api_key, inp.fields)
    except ValueError as e:
        raise AuthzDenied(400, str(e), "Credential incomplet ou vide.")
    org_store.set_org_secret(inp.org_id, inp.provider, secret, set_by=ctx.sub, meta=meta)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider}


def _delete_secret(ctx: ResolvedCtx, inp: DeleteSecretInput) -> dict:
    deleted = org_store.delete_org_secret(inp.org_id, inp.provider)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider, "deleted": deleted}


CAPABILITIES += [
    Capability(
        # MCP retiré (2026-06-25) : pose de secret brut = dashboard-only. REST conservé.
        key="org.secret.set", handler=_set_secret, Input=SetSecretInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Set/rotate an org's shared account credential for a provider "
                     "(org-shareable only). Single-key connectors: pass `api_key`. "
                     "Multi-field connectors (zoho/silae…): pass `fields` "
                     "(all declared credential fields). base_url for remote bridges."),
        rest=(RestBinding("PUT", "/api/orgs/{id}/secrets/{provider}", _ID),
              RestBinding("PUT", "/api/admin/orgs/{id}/secrets/{provider}", _ID)),
    ),
    Capability(
        key="org.secret.delete", handler=_delete_secret, Input=DeleteSecretInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Remove an org's shared secret for a provider.",
        rest=(RestBinding("DELETE", "/api/orgs/{id}/secrets/{provider}", _ID),
              RestBinding("DELETE", "/api/admin/orgs/{id}/secrets/{provider}", _ID)),
    ),
]
