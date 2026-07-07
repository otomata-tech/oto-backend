"""Acceptation des documents légaux (CGU/CGV/DPA) — REST-only.

Source de vérité du CONTENU = oto-websites ; ici on TRACE l'acceptation
(`db.legal_acceptances`, append-only) et on calcule le reste-à-accepter par
contexte (`access`, `purchase`). Le catalogue + versions courantes vivent dans
`oto_mcp.legal_docs`.

- `me.legal.status` (GET /api/me/legal) : pour chaque document, sa version courante,
  son URL, et si l'utilisateur (∪ org active) l'a acceptée à cette version ; + le
  reste-à-accepter par contexte (pour re-solliciter au changement de version).
- `me.legal.accept` (POST /api/me/legal/accept) : enregistre l'acceptation des
  documents requis d'un contexte (ou d'une liste explicite) à leur version courante.

Le gate d'ACHAT (exiger CGU+CGV+DPA) est appliqué dans `capabilities.billing`
(`billing.subscribe`), qui enregistre l'acceptation au moment de la souscription.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from .. import legal_docs
from ..db import legal_acceptance as dbla
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class NoInput(BaseModel):
    pass


class AcceptInput(BaseModel):
    context: str = "access"          # access | purchase
    slugs: Optional[list[str]] = None  # défaut = les documents requis du contexte


def status_payload(sub: str, org_id: Optional[int]) -> dict:
    """Vue « où en est cet utilisateur » — partagée avec le gate d'achat."""
    latest = dbla.latest_acceptances(sub, org_id)
    documents = []
    for slug, version in legal_docs.CURRENT_VERSIONS.items():
        acc = latest.get(slug)
        documents.append({
            "slug": slug,
            "version": version,
            "url": legal_docs.doc_url(slug),
            "label": legal_docs.LABELS.get(slug, slug),
            "accepted": acc is not None and acc["version"] == version,
            "accepted_version": acc["version"] if acc else None,
            "accepted_at": acc["accepted_at"] if acc else None,
        })
    contexts = {}
    for ctx_name, req in legal_docs.REQUIRED_BY_CONTEXT.items():
        contexts[ctx_name] = {
            "required": list(req),
            "outstanding": outstanding_slugs(latest, ctx_name),
        }
    return {"documents": documents, "contexts": contexts}


def outstanding_slugs(latest: dict, context: str) -> list[str]:
    """Slugs requis du contexte dont la version courante n'est PAS acceptée."""
    out = []
    for slug in legal_docs.required_slugs(context):
        acc = latest.get(slug)
        if not (acc and acc["version"] == legal_docs.CURRENT_VERSIONS.get(slug)):
            out.append(slug)
    return out


def _status(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return status_payload(ctx.sub, ctx.org_id)


def _accept(ctx: ResolvedCtx, inp: AcceptInput) -> dict:
    slugs = inp.slugs if inp.slugs is not None else list(legal_docs.required_slugs(inp.context))
    if not slugs:
        raise AuthzDenied(400, "nothing_to_accept",
                          f"aucun document à accepter pour le contexte '{inp.context}'")
    for slug in slugs:
        version = legal_docs.current_version(slug)
        if version is None:
            raise AuthzDenied(400, "unknown_document", f"document inconnu : '{slug}'")
        dbla.record_legal_acceptance(
            ctx.sub, slug, version, inp.context, org_id=ctx.org_id)
    return status_payload(ctx.sub, ctx.org_id)


CAPABILITIES += [
    Capability(
        key="me.legal.status", handler=_status, Input=NoInput, authz=ORG_MEMBER,
        rest=RestBinding("GET", "/api/me/legal"),
    ),
    Capability(
        key="me.legal.accept", handler=_accept, Input=AcceptInput, authz=ORG_MEMBER,
        rest=RestBinding("POST", "/api/me/legal/accept"),
    ),
]
