"""`admin.org_suspension` — suspendre une ORG, sans rien détruire (`org_suspension`).

Le pendant, au palier de l'org, de `admin.account` : l'org ne peut plus agir —
capacités, outils, travaux de fond — et rien de ce qui lui appartient n'est touché.

**Qui suspend et qui lève** (décision du 04/10/2026) : le geste est de FACTURATION
(un essai fini sans abonnement), donc il appartient au commerce, qui écrit des droits
(ADR 0070 §7) — par l'identité de service `commerce` (`service.org.suspension`, REST
seule, sous `/api/service/`), la même qui pose déjà les droits déclarés. Un super
admin garde la main (`admin.org_suspension`). Les deux suspendent ET lèvent, par le
même handler. Personne d'autre : ni un admin de l'org (elle se rouvrirait seule), ni
un admin de tenant, ni un compte de service d'usage — et aucun compte n'a besoin
d'être super admin pour que le commerce coupe une org.

**Le tenant désactivé passe avant** (oto-backend#1165) : sa désactivation suspend
toutes ses orgs (`admin.tenant_disablement`). Tant qu'il l'est, `resume` est refusé
(`409 tenant_disabled`) ; `suspend` sur une org qu'il a suspendue REPREND la suspension
à son compte, pour que la réactivation du tenant ne la lève pas.

**La fin d'essai** (`service.org.trial`, `admin.org_trial`, 16/10/2026) : l'essai n'est
pas un droit déclaré, il appartient au commerce, qui suspend l'org ici à sa fin. Il
pousse aussi la DATE de fin (`orgs.trial_ends_at`, `null` l'efface quand l'org paie),
pour que la plateforme, les admins et les agents la lisent (`oto_admin_org op=get`).
Le cœur ne fait que la servir.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

from .. import org_store, org_suspension
from ..db.tenants import TenantDesactive
from ._authz import COMMERCE_SERVICE, SUPER_ADMIN
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
        try:
            change = org_store.resume_org(inp.org_id)
        except TenantDesactive as e:
            # Son tenant est désactivé : la désactivation a suspendu toutes ses orgs, la
            # lever ici rouvrirait celle-ci à ses membres venus d'autres tenants. Elle se
            # lève par `op=enable` du tenant (celles qu'il a suspendues), puis ici.
            logger.warning("levée refusée org=%s par=%s : tenant %s désactivé",
                           inp.org_id, ctx.sub, e.slug)
            raise AuthzDenied(409, "tenant_disabled", str(e)) from e
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


_ERREURS = (
    DeclaredError(404, "unknown_org", "aucune org ne porte cet id"),
    DeclaredError(400, "missing_reason", "op=suspend sans `reason`"),
    DeclaredError(400, "reason_too_long", f"`reason` dépasse {_MOTIF_MAX} caractères"),
    DeclaredError(409, "tenant_disabled",
                  "op=resume sur une org dont le tenant est désactivé : elle rouvre avec "
                  "lui (`admin.tenant_disablement` op=enable)"),
)

CAPABILITIES += [
    Capability(
        key="admin.org_suspension", handler=_org_suspension, Input=OrgSuspensionInput,
        Output=OrgSuspensionOut, authz=SUPER_ADMIN,
        errors=_ERREURS,
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
    # Le même geste pour le COMMERCE : c'est lui qui facture, donc lui qui suspend à
    # la fin d'un essai et lève à l'abonnement. REST seule, comme toute la face de
    # service (`service_commerce`) — un tuyau de service, pas un outil d'agent.
    Capability(
        key="service.org.suspension", handler=_org_suspension, Input=OrgSuspensionInput,
        Output=OrgSuspensionOut, authz=COMMERCE_SERVICE, mcp=None,
        errors=_ERREURS,
        description=(
            "[service commerce] Suspend or resume an org (trial ended without a plan, "
            "plan taken): op=suspend (`reason` required) stops the org from acting — "
            "capabilities, tools, its agents' jobs, webhooks and schedules — without "
            "deleting anything; op=resume lifts it. Idempotent both ways. Same effect "
            "as the super admin's `admin.org_suspension`."),
        rest=RestBinding("POST", "/api/service/orgs/{id}/suspension", {"id": "org_id"}),
    ),
]


# ── la fin d'essai, poussée par le commerce ─────────────────────────────────

class OrgTrialInput(BaseModel):
    org_id: int
    # `null` efface : l'org a payé, plus d'essai en cours.
    ends_at: Optional[datetime] = None


class OrgTrialOut(BaseModel):
    org_id: int
    trial_ends_at: Optional[str] = None
    changed: bool


def _org_trial(ctx: ResolvedCtx, inp: OrgTrialInput) -> dict:
    if not org_store.get_org(inp.org_id):
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    change = org_store.set_trial_ends_at(inp.org_id, inp.ends_at)
    # Relue plutôt que recopiée : la forme servie est celle de la fiche d'org.
    fin = org_store.get_trial_ends_at(inp.org_id)
    logger.info("fin d'essai org=%s par=%s : %s (change=%s)", inp.org_id, ctx.sub, fin, change)
    return {"org_id": inp.org_id, "trial_ends_at": fin, "changed": change}


_ERREURS_ESSAI = (DeclaredError(404, "unknown_org", "aucune org ne porte cet id"),)

_DESCRIPTION_ESSAI = (
    "Set an org's trial end date (`ends_at`, ISO 8601), or clear it with `null` (the "
    "org paid). Read-only for the platform: it is served in the org detail "
    "(`trial_ends_at`) and nothing in the core acts on it — the commerce service "
    "suspends the org itself when the trial ends (`service.org.suspension`). "
    "Idempotent: `changed` says whether the value moved.")

CAPABILITIES += [
    # REST seule, comme `service.org.suspension` : un tuyau de service.
    Capability(
        key="service.org.trial", handler=_org_trial, Input=OrgTrialInput,
        Output=OrgTrialOut, authz=COMMERCE_SERVICE, mcp=None,
        errors=_ERREURS_ESSAI,
        description="[service commerce] " + _DESCRIPTION_ESSAI,
        rest=RestBinding("POST", "/api/service/orgs/{id}/trial", {"id": "org_id"}),
    ),
    # Le même geste pour un super admin — et le repli du commerce tant que son client
    # M2M n'existe pas.
    Capability(
        key="admin.org_trial", handler=_org_trial, Input=OrgTrialInput,
        Output=OrgTrialOut, authz=SUPER_ADMIN, mcp=None,
        errors=_ERREURS_ESSAI,
        description="[super admin] " + _DESCRIPTION_ESSAI,
        rest=RestBinding("POST", "/api/admin/orgs/{id}/trial", {"id": "org_id"}),
    ),
]
