"""`admin.org_suspension` — suspendre une ORG, sans rien détruire (`org_suspension`).

Le pendant, au palier de l'org, de `admin.account` : l'org ne peut plus agir —
capacités, outils de connecteur, travaux de fond — et rien de ce qui lui appartient
n'est touché. Posé par le service d'usage d'un tenant (essai fini sans
abonnement), levé par lui à l'abonnement ; un super admin peut le faire à la main.

Super admin de plateforme seulement : suspendre un espace coupe tous ses membres, et
c'est un geste de facturation, pas d'administration de l'org.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from .. import org_store, org_suspension
from ._authz import SUPER_ADMIN
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_MOTIF_MAX = 500


class OrgSuspensionInput(BaseModel):
    op: Literal["suspend", "resume"]
    org_id: int
    # Exigé pour `suspend` — même règle que la pause de compte.
    reason: Optional[str] = None


class OrgSuspensionOut(BaseModel):
    org_id: int
    suspended: bool
    changed: bool
    suspended_at: Optional[str] = None
    suspended_by: Optional[str] = None
    suspended_reason: Optional[str] = None


def _vue(org_id: int, etat: Optional[dict], *, changed: bool) -> dict:
    return {
        "org_id": org_id,
        "suspended": bool(etat),
        "changed": changed,
        "suspended_at": (etat or {}).get("suspended_at"),
        "suspended_by": (etat or {}).get("suspended_by"),
        "suspended_reason": (etat or {}).get("suspended_reason"),
    }


def _org_suspension(ctx: ResolvedCtx, inp: OrgSuspensionInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    if inp.op == "resume":
        change = org_store.resume_org(inp.org_id)
        org_suspension.invalider()
        logger.warning("org réactivée org=%s par=%s (change=%s)", inp.org_id, ctx.sub, change)
        return _vue(inp.org_id, None, changed=change)
    motif = (inp.reason or "").strip()
    if not motif:
        raise AuthzDenied(400, "missing_reason",
                          "`reason` requis : une suspension sans motif écrit devient une "
                          "suspension que personne ne saura expliquer ni lever.")
    if len(motif) > _MOTIF_MAX:
        raise AuthzDenied(400, "reason_too_long",
                          f"`reason` fait {len(motif)} caractères pour {_MOTIF_MAX} au plus.")
    deja = org_store.get_org_suspension(inp.org_id)
    etat = org_store.suspend_org(inp.org_id, by=ctx.sub, reason=motif)
    org_suspension.invalider()
    logger.warning("org suspendue org=%s par=%s motif=%r", inp.org_id, ctx.sub, motif)
    return _vue(inp.org_id, etat, changed=deja is None)


CAPABILITIES += [
    Capability(
        key="admin.org_suspension", handler=_org_suspension, Input=OrgSuspensionInput,
        Output=OrgSuspensionOut, authz=SUPER_ADMIN,
        errors=(
            DeclaredError(404, "unknown_org", "aucune org ne porte cet id"),
            DeclaredError(400, "missing_reason", "op=suspend sans `reason`"),
        ),
        description=(
            "[super admin] Suspend an org without deleting anything. op=suspend "
            "(`reason` required) → from the next call on, the org can no longer act: "
            "its capabilities and connector tools are refused with `org_suspended` "
            "(listing/reading orgs and switching org stay open), its agents' jobs are "
            "not claimed, its webhooks are refused and its schedules enqueue nothing. "
            "Members keep their other orgs. op=resume → lifts it. Idempotent both ways."),
        # POST seul, comme `admin.account` : les paramètres de requête fusionnent dans
        # l'`Input`, donc un GET pourrait muter.
        rest=RestBinding("POST", "/api/admin/orgs/{id}/suspension", {"id": "org_id"}),
    ),
]
