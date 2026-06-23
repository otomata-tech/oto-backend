"""Secrets partagés d'un groupe + preset de toolset (ADR 0012).

Autz = `GROUP_ADMIN_OF`. Les secrets de groupe utilisent la MÊME validation
provider/base_url que les secrets d'org (`connectors.org_secret_meta`, source
unique) et le même coffre chiffré (entity_type='group'). Le preset (`default_tools`)
définit la baseline de visibilité de l'équipe — `None` l'efface.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import connectors, credentials_store, group_store
from ._authz import GROUP_ADMIN_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_GID = {"id": "group_id"}


class SetGroupSecretInput(BaseModel):
    group_id: int
    provider: str
    api_key: str = ""                  # connecteurs mono-champ (clé simple)
    fields: Optional[dict[str, str]] = None   # connecteurs multi-champs (zoho/silae…)
    base_url: Optional[str] = None


class DeleteGroupSecretInput(BaseModel):
    group_id: int
    provider: str


class SetPresetInput(BaseModel):
    group_id: int
    tools: Optional[list[str]] = None     # None efface la baseline ; [] = baseline vide


def _set_secret(ctx: ResolvedCtx, inp: SetGroupSecretInput) -> dict:
    base_url = (inp.base_url or "").strip() or None
    meta, code = connectors.org_secret_meta(inp.provider, base_url)
    if code:
        raise AuthzDenied(400, code, f"Provider/base_url invalide : {code}.")
    # Mono-champ (api_key) ou multi-champs (fields packés) — source unique.
    try:
        secret = credentials_store.secret_from_input(inp.provider, inp.api_key, inp.fields)
    except ValueError as e:
        raise AuthzDenied(400, str(e), "Credential incomplet ou vide.")
    group_store.set_group_secret(inp.group_id, inp.provider, secret, set_by=ctx.sub, meta=meta)
    return {"ok": True, "group_id": inp.group_id, "provider": inp.provider}


def _delete_secret(ctx: ResolvedCtx, inp: DeleteGroupSecretInput) -> dict:
    deleted = group_store.delete_group_secret(inp.group_id, inp.provider)
    return {"ok": True, "group_id": inp.group_id, "provider": inp.provider, "deleted": deleted}


def _set_preset(ctx: ResolvedCtx, inp: SetPresetInput) -> dict:
    group_store.set_group_default_tools(inp.group_id, inp.tools)
    return {"ok": True, "group_id": inp.group_id,
            "preset": None if inp.tools is None else len(inp.tools)}


CAPABILITIES += [
    Capability(
        key="group.secret.set", handler=_set_secret, Input=SetGroupSecretInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description=("Set/rotate a group's shared account credential for a provider "
                     "(org-shareable only). Single-key connectors: pass `api_key`. "
                     "Multi-field connectors (zoho/silae…): pass `fields` (all declared "
                     "credential fields). Resolves BEFORE the org secret for members."),
        mcp="oto_set_group_secret",
        rest=RestBinding("PUT", "/api/groups/{id}/secrets/{provider}", _GID),
    ),
    Capability(
        key="group.secret.delete", handler=_delete_secret, Input=DeleteGroupSecretInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description="Remove a group's shared secret for a provider.",
        rest=RestBinding("DELETE", "/api/groups/{id}/secrets/{provider}", _GID),
    ),
    Capability(
        key="group.preset.set", handler=_set_preset, Input=SetPresetInput,
        authz=GROUP_ADMIN_OF("group_id"),
        description=("Set the group's default toolset preset (baseline visibility for "
                     "the team). Pass tools=null to clear it. Never reveals grant-only "
                     "tools (those stay entitlement-gated). Personal toggles still win."),
        mcp="oto_set_group_preset",
        rest=RestBinding("PUT", "/api/groups/{id}/preset", _GID),
    ),
]
