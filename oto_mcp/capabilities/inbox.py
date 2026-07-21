"""Inbox d'accueil (lot 3, Ship 3) — `GET /api/me/inbox`, agrège des tables
EXISTANTES en DEUX voies :

- **À traiter** (attend une décision de moi) : propositions en attente sur les
  projets où j'ai l'ÉCRITURE (`ownership.accessible_project_ids(want='write')`) +
  invitations en attente pour mon email (cross-org, voie dédiée G1).
- **Récent** (info qui vieillit) : mes propositions résolues (retour au proposeur,
  indépendant de l'org active — H1) + les projets récemment partagés à moi.

**Sans org active → 200 listes vides, jamais 400** (l'accueil charge l'inbox
automatiquement ; un 400 casserait la home — ≠ `oto_search`, invocation délibérée).
REST-only (surface dashboard) ; zéro table neuve.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import db, org_store, ownership
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class InboxInput(BaseModel):
    pass


def _inbox(ctx: ResolvedCtx, inp: InboxInput) -> dict:
    sub = ctx.sub
    to_review: list[dict] = []
    invitations: list[dict] = []
    recent: list[dict] = []

    # À traiter — propositions sur mes projets EN ÉCRITURE (jamais 400 sans org).
    if ctx.org_id is not None:
        writable = ownership.accessible_project_ids(sub, ctx.org_id, want="write")
        for cr in db.list_change_requests_by_project(writable):
            to_review.append({
                "request_id": cr["id"],
                "kind": "create" if not cr.get("doc_id") else "modif",
                "project_id": cr.get("eff_project_id") or cr.get("project_id"),
                "project_name": cr.get("project_name"),
                "doc_id": cr.get("doc_id"), "doc_title": cr.get("doc_title"),
                "proposed_title": cr.get("proposed_title"),
                # Corps proposé → le diff avant/après de la revue (sinon after=before,
                # « aucun changement détecté » sur toute modification).
                "proposed_body_md": cr.get("proposed_body_md"),
                "requested_by": cr.get("requested_by"),
                "message": cr.get("message"), "created_at": cr.get("created_at")})

    # Invitations pour mon email (cross-org, indépendant de l'org active).
    user = db.get_user(sub) or {}
    for inv in org_store.list_pending_invitations_for_email(user.get("email") or ""):
        invitations.append({
            "code": inv.get("code"), "org_id": inv.get("org_id"),
            "org_name": inv.get("org_name"), "group_id": inv.get("group_id"),
            "invited_by": inv.get("invited_by"), "created_at": inv.get("created_at")})

    # Récent — mes propositions résolues (retour proposeur, cross-org).
    for cr in db.list_change_requests_by_requester(sub):
        recent.append({
            "type": "proposal_resolved", "request_id": cr["id"],
            "status": cr.get("status"),
            "project_id": cr.get("project_id"), "project_name": cr.get("project_name"),
            "doc_id": cr.get("doc_id"), "doc_title": cr.get("doc_title"),
            "proposed_title": cr.get("proposed_title"),
            "resolved_by": cr.get("resolved_by"), "resolved_at": cr.get("resolved_at")})
    # Récent — projets partagés à moi (grants aux principals du contexte).
    if ctx.org_id is not None:
        principals = ownership.active_org_principals(sub, ctx.org_id)
        for p in db.list_projects_granted_to(principals):
            recent.append({
                "type": "project_shared", "project_id": p["id"],
                "project_name": p.get("name"), "permission": p.get("permission"),
                "granted_at": p.get("granted_at")})
    recent.sort(key=lambda r: str(r.get("resolved_at") or r.get("granted_at") or ""),
                reverse=True)

    return {
        "to_review": to_review,
        "invitations": invitations,
        "recent": recent[:30],
        # Badge = ce qui attend une décision déterministe (états pending). Zéro « lu/vu ».
        "count": len(to_review) + len(invitations),
    }


CAPABILITIES += [
    Capability(
        key="me.inbox", handler=_inbox, Input=InboxInput, authz=SUB_ONLY,
        description="Home inbox: proposals awaiting your review + pending invitations "
                    "(À traiter) and recent activity (Récent). REST-only.",
        rest=RestBinding("GET", "/api/me/inbox"),
    ),
]
