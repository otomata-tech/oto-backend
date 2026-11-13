"""`org.erasure` — effacer une org, entière, sur confirmation de son titulaire (RGPD art. 17).

Le geste en quatre temps (`docs/effacement-d-org.md`) :

1. **demander** (`op=request`) — un admin de l'org, ou un super admin pour le compte du
   client (`reason` exigé) ; le périmètre est vérifié (refus nommés), un lien à usage
   unique part chez le TITULAIRE (le créateur de l'org s'il en est admin, sinon l'admin
   le plus ancien). Rien n'est encore suspendu.
2. **confirmer** (`org.erasure_confirm`) — le titulaire, connecté, ouvre le lien : il voit
   ce qui va partir (`op=preview`), puis confirme (`op=confirm`). L'org est suspendue
   sur-le-champ, la purge est fixée au terme du délai de grâce.
3. **annuler** (`op=cancel`) — tant que la purge n'a pas commencé, un admin de l'org ou un
   super admin ; la suspension posée par la confirmation est levée.
4. **purger** — le travail de fond `oto-mcp maintenance effacements`, jamais une requête.

Le commerce suit par sa face de service (`service.org.erasures`) : il cesse de facturer
dès la confirmation, efface sa part après la purge et l'acquitte.

⚠️ Le jeton du lien est un SECRET porteur : aucune face MCP ne le reçoit (règle « secret
brut jamais en argument MCP »), et seule son empreinte est gardée.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel

from .. import config, org_store, org_suspension
from ..db import _connect
from ..effacement import demandes, purge, travail
from ..effacement.courriels import send_org_erasure_confirm_email
from ..effacement.perimetre import EffacementRefuse, Instantane, figer, titulaire
from ._authz import COMMERCE_SERVICE, ORG_ADMIN_OF, SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_PLAFOND_DU_PLAN = 100_000
_MOTIF_MAX = 500
_MOTIF_DE_SUSPENSION = org_suspension.MOTIF_EFFACEMENT + " (demande #{id})"


class OrgErasureInput(BaseModel):
    op: Literal["plan", "request", "cancel", "status"]
    org_id: int
    # Exigé quand un super admin demande pour le compte du client.
    reason: Optional[str] = None


class ErasureView(BaseModel):
    """Une demande d'effacement, telle que l'org la voit."""
    id: int
    org_id: int
    state: str
    via: str
    requested_at: Optional[str] = None
    token_expires_at: Optional[str] = None
    confirm_to: Optional[str] = None
    confirmed_at: Optional[str] = None
    scheduled_for: Optional[str] = None
    cancelled_at: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    last_error: Optional[str] = None
    plan: Optional[dict] = None
    receipt: Optional[dict] = None


class OrgErasureOut(BaseModel):
    org_id: int
    # op=plan : ce que l'effacement ferait (comptes par action et par table, bornés).
    plan: Optional[dict] = None
    # op=request|cancel|status : la demande (la dernière, pour status ; None = aucune).
    erasure: Optional[ErasureView] = None
    emailed: Optional[bool] = None


def _masquer(email: Optional[str]) -> Optional[str]:
    """`j***@exemple.fr` : l'org sait OÙ le lien est parti sans que la réponse serve
    d'annuaire."""
    if not email or "@" not in email:
        return None
    nom, dom = email.split("@", 1)
    return f"{nom[:1]}***@{dom}"


def _vue(d: Optional[dict]) -> Optional[dict]:
    if not d:
        return None
    return {k: d.get(k) for k in ErasureView.model_fields if k != "confirm_to"} | {
        "confirm_to": _masquer(d.get("confirm_email"))}


def _refus(e: EffacementRefuse) -> AuthzDenied:
    """Le refus du périmètre, servi tel quel : son code, sous son statut."""
    if e.code == "unknown_org":
        return AuthzDenied(404, e.code, str(e))
    return AuthzDenied(409, e.code, str(e))


def _plan(inst: Instantane) -> dict:
    with _connect() as conn:
        return purge.plan(conn, inst, plafond=_PLAFOND_DU_PLAN)


def _figer(org_id: int) -> Instantane:
    try:
        with _connect() as conn:
            return figer(conn, org_id)
    except EffacementRefuse as e:
        raise _refus(e) from e


def _lien(front_base: Optional[str], jeton: str) -> str:
    base = front_base.rstrip("/") if front_base else config.invite_base_url()
    return f"{base}/erasure/{jeton}"


def _ouvert() -> None:
    """Une instance qui ne purge pas (`OTO_EFFACEMENTS` absente) n'accepte ni demande ni
    confirmation : l'org serait suspendue sans jamais être effacée."""
    if not travail.actif():
        raise AuthzDenied(409, "erasure_disabled",
                          "L'effacement d'org n'est pas ouvert sur cette instance.")


def _est_membre_admin(sub: str, org_id: int) -> bool:
    return org_store.get_org_role(org_id, sub) == "org_admin"


def _org_erasure(ctx: ResolvedCtx, inp: OrgErasureInput) -> dict:
    if inp.op == "status":
        return {"org_id": inp.org_id, "erasure": _vue(demandes.derniere(inp.org_id))}
    if inp.op == "plan":
        return {"org_id": inp.org_id, "plan": _plan(_figer(inp.org_id))}
    if inp.op == "cancel":
        d = demandes.vivante(inp.org_id)
        if d is None:
            raise AuthzDenied(404, "no_pending_erasure", "Aucun effacement en cours pour cette org.")
        if d["state"] == "running":
            raise AuthzDenied(409, "erasure_running",
                              "La purge a commencé : elle ne s'annule plus.")
        annulee = demandes.annuler(d["id"], by=ctx.sub)
        if annulee is None:
            raise AuthzDenied(409, "erasure_running", "La purge vient de commencer.")
        etat = org_store.get_org_suspension(inp.org_id)
        if etat and etat.get("suspended_reason") == _MOTIF_DE_SUSPENSION.format(id=d["id"]):
            org_store.resume_org(inp.org_id)
            org_suspension.invalider()
        logger.warning("effacement %s annulé org=%s par=%s", d["id"], inp.org_id, ctx.sub)
        return {"org_id": inp.org_id, "erasure": _vue(annulee)}
    # op=request
    _ouvert()
    via = "owner" if _est_membre_admin(ctx.sub, inp.org_id) else "operator"
    motif = (inp.reason or "").strip() or None
    if via == "operator" and not motif:
        raise AuthzDenied(400, "missing_reason",
                          "`reason` requis : un super admin efface pour le compte d'un client, "
                          "et le dit (la référence de sa demande, par exemple).")
    if motif and len(motif) > _MOTIF_MAX:
        raise AuthzDenied(400, "reason_too_long", f"`reason` dépasse {_MOTIF_MAX} caractères.")
    inst = _figer(inp.org_id)
    try:
        with _connect() as conn:
            qui = titulaire(conn, inp.org_id)
    except EffacementRefuse as e:
        raise _refus(e) from e
    jeton, empreinte = demandes.nouveau_jeton()
    try:
        d = demandes.creer(org_id=inp.org_id, via=via, reason=motif, requested_by=ctx.sub,
                           confirm_sub=qui["sub"], confirm_email=qui["email"],
                           org_name=qui["org_name"], token_hash=empreinte, plan=_plan(inst))
    except demandes.DemandeEnConflit as e:
        raise AuthzDenied(409, e.code, str(e)) from e
    envoye = send_org_erasure_confirm_email(
        qui["email"], org_name=qui["org_name"], confirm_url=_lien(qui["front_base_url"], jeton),
        delai_jours=demandes.DELAI_DE_GRACE.days, par_operateur=via == "operator",
        brand=qui["front_brand"], locale=qui["locale"])
    logger.warning("effacement %s demandé org=%s par=%s via=%s (mail=%s)", d["id"],
                   inp.org_id, ctx.sub, via, envoye)
    return {"org_id": inp.org_id, "erasure": _vue(d), "emailed": envoye}


_ERREURS = (
    DeclaredError(404, "unknown_org", "aucune org ne porte cet id"),
    DeclaredError(404, "no_pending_erasure", "op=cancel sans effacement en cours"),
    DeclaredError(400, "missing_reason", "op=request par un super admin non membre, sans `reason`"),
    DeclaredError(400, "reason_too_long", f"`reason` dépasse {_MOTIF_MAX} caractères"),
    DeclaredError(409, "erasure_pending", "op=request alors qu'une demande vit déjà"),
    DeclaredError(409, "erasure_running", "op=cancel alors que la purge a commencé"),
    DeclaredError(409, "already_erased", "l'org est déjà effacée"),
    DeclaredError(409, "subscription_active", "l'org porte un abonnement qui prélève"),
    DeclaredError(409, "operator_account", "un compte d'opérateur n'a que cette org"),
    DeclaredError(409, "tenant_admin_account", "un compte d'admin de tenant n'a que cette org"),
    DeclaredError(409, "no_owner", "aucun admin de l'org n'est joignable par mail"),
    DeclaredError(409, "erasure_disabled", "l'instance ne purge pas (`OTO_EFFACEMENTS`)"),
)

CAPABILITIES += [
    Capability(
        key="org.erasure", handler=_org_erasure, Input=OrgErasureInput, Output=OrgErasureOut,
        # Un admin de l'org ; un super admin par l'escalade de `roles` (ADR 0012).
        authz=ORG_ADMIN_OF("org_id"), mcp=None, errors=_ERREURS,
        description=(
            "Permanently erase an org (GDPR art. 17). op=plan → what would be erased, kept "
            "(accounting records) or anonymised, counted per table (bounded). op=request → "
            "emails a single-use confirmation link (72 h) to the org's owner; nothing is "
            "suspended yet. A super admin acting for the customer must give `reason`. "
            "op=cancel → cancels a pending request until the purge starts (lifts the "
            "suspension it set). op=status → the latest request, with its receipt."),
        rest=RestBinding("POST", "/api/orgs/{id}/erasure", {"id": "org_id"}),
    ),
]


# ── la confirmation, par le titulaire, depuis le lien ──────────────────────────

class ErasureConfirmInput(BaseModel):
    op: Literal["preview", "confirm"]
    token: str


class ErasureConfirmOut(BaseModel):
    org_id: int
    org_name: Optional[str] = None
    # Le compte connecté est-il le titulaire à qui le lien a été envoyé ?
    is_recipient: bool
    grace_days: int
    erasure: ErasureView


def _par_jeton(jeton: str) -> dict:
    d = demandes.par_jeton(demandes.empreinte(jeton.strip()))
    if d is None or d["state"] != "requested":
        raise AuthzDenied(404, "invalid_link", "Ce lien n'est pas (ou plus) valide.")
    return d


def _org_erasure_confirm(ctx: ResolvedCtx, inp: ErasureConfirmInput) -> dict:
    d = _par_jeton(inp.token)
    destinataire = d["confirm_sub"] == ctx.sub
    out = {"org_id": d["org_id"], "org_name": d["org_name"], "is_recipient": destinataire,
           "grace_days": demandes.DELAI_DE_GRACE.days, "erasure": _vue(d)}
    if inp.op == "preview":
        return out
    _ouvert()
    if not destinataire:
        raise AuthzDenied(403, "not_recipient",
                          "Seul le titulaire à qui le lien a été envoyé peut confirmer : se "
                          "connecter avec son compte.")
    inst = _figer(d["org_id"])          # les refus se rejouent à la confirmation
    fait = demandes.confirmer(d["id"], sub=ctx.sub, snapshot=inst.vers_json(),
                              plan=_plan(inst))
    if fait is None:
        raise AuthzDenied(404, "invalid_link", "Ce lien n'est plus valide.")
    org_store.suspend_org(d["org_id"], by=ctx.sub,
                          reason=_MOTIF_DE_SUSPENSION.format(id=d["id"]))
    org_suspension.invalider()
    logger.warning("effacement %s confirmé org=%s par=%s, purge le %s", d["id"], d["org_id"],
                   ctx.sub, fait["scheduled_for"])
    return out | {"erasure": _vue(fait)}


CAPABILITIES += [
    Capability(
        key="org.erasure_confirm", handler=_org_erasure_confirm, Input=ErasureConfirmInput,
        Output=ErasureConfirmOut, authz=SUB_ONLY, mcp=None,
        errors=(
            DeclaredError(404, "invalid_link", "jeton inconnu, consommé ou échu"),
            DeclaredError(403, "not_recipient", "op=confirm par un autre compte que le titulaire"),
            *(e for e in _ERREURS if e.code in ("already_erased", "subscription_active",
                                                 "operator_account", "tenant_admin_account",
                                                 "erasure_disabled")),
        ),
        description=(
            "The owner's side of an org erasure, from the emailed link. op=preview → the "
            "org, what will be erased, and whether the signed-in account is the recipient. "
            "op=confirm (recipient only) → the org is suspended now and erased after the "
            "grace period; an org admin can still cancel until the purge starts."),
        # POST, jeton dans le CORPS : jamais dans une URL (journaux, en-tête Referer).
        rest=RestBinding("POST", "/api/org-erasures/confirmation"),
    ),
]


# ── le commerce : cesser de facturer, effacer sa part, acquitter ──────────────

class CommerceErasuresInput(BaseModel):
    limit: int = 100


class CommerceErasure(BaseModel):
    id: int
    org_id: int
    state: str
    scheduled_for: Optional[str] = None
    finished_at: Optional[str] = None


class CommerceErasuresOut(BaseModel):
    erasures: list[CommerceErasure]


def _pour_le_commerce(ctx: ResolvedCtx, inp: CommerceErasuresInput) -> dict:
    return {"erasures": demandes.pour_le_commerce(max(1, min(inp.limit, 500)))}


class CommerceAckInput(BaseModel):
    erasure_id: int


class CommerceAckOut(BaseModel):
    erasure_id: int
    acknowledged: bool


def _acquitter(ctx: ResolvedCtx, inp: CommerceAckInput) -> dict:
    ok = demandes.commerce_a_efface(inp.erasure_id)
    if not ok:
        raise AuthzDenied(409, "erasure_not_done",
                          "Seul un effacement terminé (`done`) s'acquitte.")
    return {"erasure_id": inp.erasure_id, "acknowledged": True}


CAPABILITIES += [
    Capability(
        key="service.org.erasures", handler=_pour_le_commerce, Input=CommerceErasuresInput,
        Output=CommerceErasuresOut, authz=COMMERCE_SERVICE, mcp=None,
        description=(
            "[service commerce] Org erasures commerce has not acknowledged yet: "
            "`scheduled`/`running` → stop billing the org (cancel its plan); `done` → erase "
            "commerce's own data for the org, then acknowledge it."),
        rest=RestBinding("GET", "/api/service/org-erasures"),
    ),
    Capability(
        key="service.org.erasure_ack", handler=_acquitter, Input=CommerceAckInput,
        Output=CommerceAckOut, authz=COMMERCE_SERVICE, mcp=None,
        errors=(DeclaredError(409, "erasure_not_done", "l'effacement n'est pas terminé"),),
        description=("[service commerce] Acknowledge that commerce erased its own data for a "
                     "`done` org erasure."),
        rest=RestBinding("POST", "/api/service/org-erasures/{id}/commerce-erased",
                         {"id": "erasure_id"}),
    ),
]
