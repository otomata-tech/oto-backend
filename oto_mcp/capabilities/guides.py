"""Guides ON-DEMAND côté REST (ADR 0042) — surface dashboard des how-to.

Miroir REST de l'outil MCP `oto_guide` (tools/guide.py) : même cœur `guide_store`,
thin adapter d'autz par SCOPE. Le dashboard (REST-only) peut ainsi lister / lire /
rédiger / supprimer les guides on-demand PLATEFORME (platform_admin), d'ORG (org_admin)
et PERSO (self) — tout-DB 2026-07-16, les fichiers `guides/*.md` = seeds de boot.

Distinct des readmes INIT (delivery='init', injectés au handshake — édités par
`me.agent_readme` / `platform.instructions`) et des PROCÉDURES (org_instructions, slots).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import guide_store
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class _NoInput(BaseModel):
    pass


class GuideRefInput(BaseModel):
    scope: str
    slug: str


class GuideSetInput(BaseModel):
    scope: str
    slug: str
    body_md: str = ""
    title: str = ""
    description: str = ""


def _owner_for_write(ctx: ResolvedCtx, scope: str) -> str:
    """Le owner_id d'écriture pour `scope`, avec autz. platform → platform_admin ;
    org → org_admin de l'org active ; user → self. Lève AuthzDenied."""
    from .. import roles
    if scope == "user":
        return ctx.sub
    if scope == "org":
        if ctx.org_id is None:
            raise AuthzDenied(400, "no_active_org", "Aucune org active — vois `oto_use_org`.")
        if not roles.is_org_admin(ctx.sub, ctx.org_id):
            raise AuthzDenied(403, "forbidden", "Réservé à un admin de l'org (guide d'org).")
        return str(ctx.org_id)
    if scope == "platform":
        if not roles.is_platform_admin(ctx.sub):
            raise AuthzDenied(403, "forbidden", "Réservé à l'admin plateforme (guide plateforme).")
        return guide_store.PLATFORM_OWNER
    raise AuthzDenied(400, "bad_scope", "scope éditable = platform | org | user.")


def _list(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    """Catalogue des guides on-demand visibles : plateforme ∪ org active ∪ perso (DB)."""
    return {"guides": guide_store.list_guides_for(ctx.sub, ctx.org_id)}


def _get(ctx: ResolvedCtx, inp: GuideRefInput) -> dict:
    g = guide_store.read_guide_scoped(inp.slug, scope=inp.scope, org_id=ctx.org_id, sub=ctx.sub)
    if g is None:
        raise AuthzDenied(404, "not_found", f"Guide `{inp.slug}` (scope {inp.scope}) introuvable.")
    return g


def _set(ctx: ResolvedCtx, inp: GuideSetInput) -> dict:
    owner_id = _owner_for_write(ctx, inp.scope)
    try:
        return guide_store.set_guide(inp.scope, owner_id, inp.slug, inp.body_md,
                                     inp.title or "", inp.description or "")
    except guide_store.GuideError as e:
        raise AuthzDenied(400, "invalid_guide", str(e))


def _delete(ctx: ResolvedCtx, inp: GuideRefInput) -> dict:
    owner_id = _owner_for_write(ctx, inp.scope)
    deleted = guide_store.delete_guide(inp.scope, owner_id, inp.slug)
    if not deleted:
        raise AuthzDenied(404, "not_found", f"Guide `{inp.slug}` (scope {inp.scope}) absent.")
    return {"scope": inp.scope, "slug": inp.slug, "deleted": True}


# REST-only : la face MCP est déjà servie par l'outil spine `oto_guide` (tools/guide.py).
# Autz = SUB_ONLY (authentifié) + garde par SCOPE inline (platform_admin / org_admin / self)
# — comme l'outil MCP ; le combinateur ne peut pas dériver l'org d'un champ `scope` libre.
CAPABILITIES += [
    Capability(
        key="me.guides.list", handler=_list, Input=_NoInput, authz=SUB_ONLY, mcp=None,
        description="List the on-demand guides you can see (platform ∪ your org ∪ your own).",
        rest=RestBinding("GET", "/api/me/guides"),
    ),
    Capability(
        key="me.guides.get", handler=_get, Input=GuideRefInput, authz=SUB_ONLY, mcp=None,
        description="Read one on-demand guide body by scope+slug.",
        rest=RestBinding("GET", "/api/me/guides/{scope}/{slug}"),
    ),
    Capability(
        key="me.guides.set", handler=_set, Input=GuideSetInput, authz=SUB_ONLY, mcp=None,
        description="Create/update an on-demand guide (scope=platform|org|user).",
        rest=RestBinding("PUT", "/api/me/guides/{scope}/{slug}"),
    ),
    Capability(
        key="me.guides.delete", handler=_delete, Input=GuideRefInput, authz=SUB_ONLY, mcp=None,
        description="Delete an on-demand guide (scope=platform|org|user).",
        rest=RestBinding("DELETE", "/api/me/guides/{scope}/{slug}"),
    ),
]
