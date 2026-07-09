"""Acceptation des documents légaux — face de `me.legal` (gate frontend LegalGate).

`GET /api/me/legal` rend le `LegalStatus` (docs + reste-à-accepter par contexte) ;
`POST /api/me/legal/accept {context}` enregistre l'acceptation des docs requis du
contexte à leur version COURANTE. SUB_ONLY (self-service, `/api/me/*`). Source des
docs = `legal_docs.py` ; trace = table `legal_acceptances` (`db.*`).

REST-only : le consentement est un acte de l'utilisateur dans le dashboard, pas un
canal agent → pas de binding MCP.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import db, legal_docs
from ._authz import SUB_ONLY
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class _NoInput(BaseModel):
    pass


class AcceptInput(BaseModel):
    context: str


def _is_current(acc: dict, slug: str) -> bool:
    a = acc.get(slug)
    return a is not None and a["version"] == legal_docs.CURRENT_DOCS[slug]["version"]


def _status(sub: str) -> dict:
    """Compose le LegalStatus attendu par le front (documents + contexts)."""
    acc = db.get_legal_acceptances(sub)
    documents = []
    for slug, meta in legal_docs.CURRENT_DOCS.items():
        a = acc.get(slug)
        documents.append({
            "slug": slug,
            "version": meta["version"],
            "url": meta["url"],
            "label": meta["label"],
            "accepted": _is_current(acc, slug),
            "accepted_version": a["version"] if a else None,
            "accepted_at": a["accepted_at"] if a else None,
        })
    contexts = {}
    for ctx, required in legal_docs.CONTEXTS.items():
        outstanding = [s for s in required if not _is_current(acc, s)]
        contexts[ctx] = {"required": required, "outstanding": outstanding}
    return {"documents": documents, "contexts": contexts}


def _get(ctx: ResolvedCtx, inp: _NoInput) -> dict:
    return _status(ctx.sub)


def _accept(ctx: ResolvedCtx, inp: AcceptInput) -> dict:
    required = legal_docs.CONTEXTS.get(inp.context)
    if required is None:
        raise AuthzDenied(400, "unknown_context", f"Contexte légal inconnu : {inp.context!r}.")
    db.record_legal_acceptances(
        ctx.sub, [(slug, legal_docs.CURRENT_DOCS[slug]["version"]) for slug in required])
    return _status(ctx.sub)


CAPABILITIES += [
    Capability(
        key="me.legal.get", handler=_get, Input=_NoInput,
        authz=SUB_ONLY,
        description="The user's legal acceptance status: current documents "
                    "(slug/version/url/label + whether accepted) and, per context "
                    "('access'|'purchase'), the required docs and those still outstanding.",
        rest=RestBinding("GET", "/api/me/legal"),
    ),
    Capability(
        key="me.legal.accept", handler=_accept, Input=AcceptInput,
        authz=SUB_ONLY,
        description="Record the user's acceptance of the documents required by a "
                    "context ('access' at signup, 'purchase' at checkout) at their "
                    "current version. Returns the refreshed legal status.",
        rest=RestBinding("POST", "/api/me/legal/accept"),
    ),
]
