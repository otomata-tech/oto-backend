"""Capacité fact générique (ADR 0008/0009) — substrat typé exposé sur 2 faces.

Le graphe de facts (fact/edge typés, `factgraph/`) est le **substrat typé canonique**
(le datastore reste libre). Cette capacité l'expose **génériquement** (aucune logique
prospection) : écrire/lire des facts typés validés contre le registre `schemas.py`,
les relier, et **décrire les schémas** (`fact_kinds`) pour que la vue dashboard
« Fact graph » rende des fiches lisibles, schema-aware.

Un « thème » = un `kind` (ex. `lead`) ; son `domain` (= `kind` du workspace, scopé
org) est résolu automatiquement → la doctrine/agent n'écrit qu'un `kind` + `fields`.
Le chemin d'écriture prospection-spécifique (scout scoring/claim/cockpit) est retiré
par ailleurs (ADR 0018) ; ici on ne garde que le générique.

Org-scopé via `ORG_MEMBER` (org active injectée, jamais d'un param client → IDOR
verrouillé). Handlers SYNC (I/O psycopg bloquant), comme les autres capacités DB.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ..factgraph import schemas, store
from ._authz import ORG_MEMBER, SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


# ── Inputs ───────────────────────────────────────────────────────────────────
class FactKindsInput(BaseModel):
    pass


class FactWriteInput(BaseModel):
    kind: str
    fields: dict
    id: Optional[int] = None          # présent = mise à jour (merge) du fact existant


class FactListInput(BaseModel):
    kind: str
    limit: int = 200


class FactGetInput(BaseModel):
    id: int


class FactLinkInput(BaseModel):
    from_id: int
    to_id: int
    role: str


def _domain_for(kind: str) -> str:
    domain = schemas.KIND_DOMAIN.get(kind)
    if domain is None:
        raise AuthzDenied(400, "unknown_kind",
                          f"kind inconnu: {kind!r} (connus: {sorted(schemas.KIND_DOMAIN)}).")
    return domain


# ── Handlers (core, sync — org_id injecté par l'autz) ────────────────────────
def _kinds(ctx: ResolvedCtx, inp: FactKindsInput) -> dict:
    """Décrit les kinds disponibles (champs + rôle de rendu + label) = le contrat
    que la vue dashboard lit pour rendre des fiches sans schéma codé en dur."""
    return {"kinds": schemas.describe_kinds()}


def _write(ctx: ResolvedCtx, inp: FactWriteInput) -> dict:
    """Crée un fact typé (ou met à jour par `id`, merge des champs). Validé contre
    le schéma du kind ; refus net si le payload est malformé."""
    if inp.id is not None:
        existing = store.get_fact_for_org(ctx.org_id, inp.id)
        if not existing:
            raise AuthzDenied(404, "not_found", f"fact #{inp.id} introuvable dans ton org.")
        merged = {**(existing["data"] or {}), **inp.fields}
        try:
            clean = store.update_fact(inp.id, existing["kind"], merged)
        except schemas.SchemaError as e:
            raise AuthzDenied(400, "schema_error", str(e))
        return {"id": inp.id, "kind": existing["kind"], "data": clean, "updated": True}

    domain = _domain_for(inp.kind)
    ws = store.get_or_create_workspace(ctx.org_id, domain)
    try:
        fid = store.add_fact(ws, inp.kind, inp.fields, created_by=ctx.sub)
    except schemas.SchemaError as e:
        raise AuthzDenied(400, "schema_error", str(e))
    return {"id": fid, "kind": inp.kind, "data": store.get_fact(fid)["data"], "created": True}


def _list(ctx: ResolvedCtx, inp: FactListInput) -> dict:
    """Facts d'un kind dans l'org active (workspace org × domaine)."""
    domain = _domain_for(inp.kind)
    rows = store.list_facts_for_org(ctx.org_id, domain, inp.kind, max(1, min(inp.limit, 1000)))
    return {
        "kind": inp.kind,
        "facts": [{"id": r["id"], "data": r["data"], "created_at": r["created_at"]} for r in rows],
        "count": len(rows),
    }


def _get(ctx: ResolvedCtx, inp: FactGetInput) -> dict:
    """Un fact + ses arêtes entrantes (contacts/actions qui le concernent)."""
    f = store.get_fact_for_org(ctx.org_id, inp.id)
    if not f:
        raise AuthzDenied(404, "not_found", f"fact #{inp.id} introuvable dans ton org.")
    inc = store.incoming(inp.id)
    return {
        "id": f["id"], "kind": f["kind"], "data": f["data"], "created_at": f["created_at"],
        "incoming": [{"id": r["id"], "kind": r["kind"], "role": r["role"], "data": r["data"]}
                     for r in inc],
    }


def _link(ctx: ResolvedCtx, inp: FactLinkInput) -> dict:
    """Relie deux facts par une arête typée (les deux doivent être dans ton org)."""
    if not store.get_fact_for_org(ctx.org_id, inp.from_id) or \
       not store.get_fact_for_org(ctx.org_id, inp.to_id):
        raise AuthzDenied(404, "not_found", "fact source ou cible hors de ton org.")
    try:
        store.link(inp.from_id, inp.to_id, inp.role)
    except (ValueError, schemas.SchemaError) as e:
        raise AuthzDenied(400, "link_error", str(e))
    return {"ok": True, "from_id": inp.from_id, "to_id": inp.to_id, "role": inp.role}


CAPABILITIES += [
    Capability(
        key="facts.kinds", handler=_kinds, Input=FactKindsInput, authz=SUB_ONLY,
        description=("List typed fact kinds + their schema (fields, render role, label). "
                     "A 'kind' (e.g. `lead`) is a structured record type rendered as a "
                     "readable card in the Fact graph view."),
        mcp="fact_kinds",
        rest=RestBinding("GET", "/api/facts/kinds"),
    ),
    Capability(
        key="facts.write", handler=_write, Input=FactWriteInput, authz=ORG_MEMBER,
        description=("Write a typed fact in your active org (validated against the kind's "
                     "schema). `kind` (e.g. `lead`) + `fields` dict; pass `id` to update "
                     "an existing fact (fields merged). For prospection, ALWAYS fill the "
                     "free-text qualification fields (pourquoi_lead, accroche, next_step, "
                     "notes), not just the data fields."),
        mcp="fact_write",
        rest=RestBinding("POST", "/api/facts"),
    ),
    Capability(
        key="facts.list", handler=_list, Input=FactListInput, authz=ORG_MEMBER,
        description="List facts of a `kind` in your active org (most recent first).",
        mcp="fact_list",
        rest=RestBinding("GET", "/api/facts"),
    ),
    Capability(
        key="facts.get", handler=_get, Input=FactGetInput, authz=ORG_MEMBER,
        description="Get one fact by id + its incoming edges (linked contacts/actions).",
        mcp="fact_get",
        rest=RestBinding("GET", "/api/facts/item/{id}"),
    ),
    Capability(
        key="facts.link", handler=_link, Input=FactLinkInput, authz=ORG_MEMBER,
        description="Link two facts with a typed edge (role: concerns | derived-from | ...).",
        mcp="fact_link",
        rest=RestBinding("POST", "/api/facts/item/{id}/links", {"id": "from_id"}),
    ),
]
