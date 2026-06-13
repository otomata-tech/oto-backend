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

from .. import connectors, org_store
from ._authz import ORG_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding

from .registry import CAPABILITIES

_ID = {"id": "org_id"}


class SetSecretInput(BaseModel):
    org_id: int
    provider: str
    api_key: str
    base_url: Optional[str] = None     # connecteurs remote uniquement (endpoint du bridge)


class DeleteSecretInput(BaseModel):
    org_id: int
    provider: str


def _set_secret(ctx: ResolvedCtx, inp: SetSecretInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if not (inp.api_key or "").strip():
        raise AuthzDenied(400, "empty_api_key", "api_key vide.")
    base_url = (inp.base_url or "").strip() or None
    meta, code = connectors.org_secret_meta(inp.provider, base_url)
    if code:
        raise AuthzDenied(400, code, f"Provider/base_url invalide : {code}.")
    org_store.set_org_secret(inp.org_id, inp.provider, inp.api_key, set_by=ctx.sub, meta=meta)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider}


def _delete_secret(ctx: ResolvedCtx, inp: DeleteSecretInput) -> dict:
    deleted = org_store.delete_org_secret(inp.org_id, inp.provider)
    return {"ok": True, "org_id": inp.org_id, "provider": inp.provider, "deleted": deleted}


CAPABILITIES += [
    Capability(
        key="org.secret.set", handler=_set_secret, Input=SetSecretInput,
        authz=ORG_ADMIN_OF("org_id"),
        description=("Set/rotate an org's shared account credential for a provider "
                     "(org-shareable only ; base_url required for remote connectors)."),
        mcp="oto_admin_set_org_secret",
        rest=(RestBinding("PUT", "/api/orgs/{id}/secrets/{provider}", _ID),
              RestBinding("PUT", "/api/admin/orgs/{id}/secrets/{provider}", _ID)),
    ),
    Capability(
        key="org.secret.delete", handler=_delete_secret, Input=DeleteSecretInput,
        authz=ORG_ADMIN_OF("org_id"),
        description="Remove an org's shared secret for a provider.",
        mcp="oto_admin_delete_org_secret",
        rest=(RestBinding("DELETE", "/api/orgs/{id}/secrets/{provider}", _ID),
              RestBinding("DELETE", "/api/admin/orgs/{id}/secrets/{provider}", _ID)),
    ),
]
