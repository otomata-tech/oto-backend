"""Capacités billing (ADR 0043, B2) — abonnement par org, REST-only.

Pas de face MCP par choix d'ADR : payer est un acte humain (dashboard), on ne
fait pas transiter d'URL de paiement dans un contexte LLM. Souscrire/confirmer/
résilier = org_admin ; consulter = tout membre de l'org active.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import billing
from ..stancer_client import StancerError
from ._authz import ORG_ADMIN, ORG_MEMBER, SUB_ONLY, SUPER_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class NoInput(BaseModel):
    pass


class SubscribeInput(BaseModel):
    plan: str
    return_url: str          # URL de retour du dashboard (page billing)
    method: str = "card"     # 'card' | 'sepa' (prélèvement)
    # champs SEPA (exigés ensemble si method='sepa') — le mobile reçoit l'OTP
    # de signature du mandat sur la page hébergée Stancer.
    iban: str | None = None
    holder_name: str | None = None
    mobile: str | None = None


class PaymentsInput(BaseModel):
    limit: int = 20


class AdminPlanInput(BaseModel):
    org_id: int
    plan: str | None = None   # None / omis = retirer le plan comp forcé


def _domain(fn, *args):
    """Traduit les erreurs domaine/PSP en refus neutres (jamais un 500 nu) :
    ValueError = état/entrée (`code: détail`), StancerError = amont PSP (502),
    RuntimeError = config/invariant (STANCER_API_KEY absente, token manquant)."""
    try:
        return fn(*args)
    except ValueError as e:
        msg = str(e)
        code = msg.split(":", 1)[0].strip() if ":" in msg else "billing_error"
        raise AuthzDenied(409 if code in ("already_subscribed",) else 400, code, msg)
    except StancerError as e:
        raise AuthzDenied(502, "psp_error", e.detail)
    except RuntimeError as e:
        raise AuthzDenied(503, "billing_unavailable", str(e))


def _plans(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return {"plans": billing.plans()}


def _status(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return _domain(billing.status, ctx.org_id)


def _subscribe(ctx: ResolvedCtx, inp: SubscribeInput) -> dict:
    def call():
        return billing.subscribe(ctx.org_id, inp.plan, inp.return_url,
                                 method=inp.method, iban=inp.iban,
                                 holder_name=inp.holder_name, mobile=inp.mobile)

    return _domain(call)


def _confirm(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return _domain(billing.confirm, ctx.org_id)


def _cancel(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return _domain(billing.cancel, ctx.org_id)


def _admin_set_plan(ctx: ResolvedCtx, inp: AdminPlanInput) -> dict:
    if inp.plan:
        return _domain(lambda: billing.admin_set_plan(
            inp.org_id, inp.plan, granted_by=ctx.sub))
    return _domain(lambda: billing.admin_clear_plan(inp.org_id))


def _payments(ctx: ResolvedCtx, inp: PaymentsInput) -> dict:
    from ..db import billing as db_billing

    rows = db_billing.list_billing_payments(ctx.org_id, inp.limit)
    return {"payments": [
        {k: r.get(k) for k in ("id", "kind", "amount", "currency", "status",
                               "attempt", "created_at")}
        for r in rows
    ]}


CAPABILITIES += [
    Capability(
        key="billing.plans", handler=_plans, Input=NoInput, authz=SUB_ONLY,
        rest=RestBinding("GET", "/api/billing/plans"),
    ),
    Capability(
        key="billing.status", handler=_status, Input=NoInput, authz=ORG_MEMBER,
        rest=RestBinding("GET", "/api/me/billing"),
    ),
    Capability(
        key="billing.subscribe", handler=_subscribe, Input=SubscribeInput,
        authz=ORG_ADMIN, rest=RestBinding("POST", "/api/me/billing/subscribe"),
    ),
    Capability(
        key="billing.confirm", handler=_confirm, Input=NoInput,
        authz=ORG_ADMIN, rest=RestBinding("POST", "/api/me/billing/confirm"),
    ),
    Capability(
        key="billing.cancel", handler=_cancel, Input=NoInput,
        authz=ORG_ADMIN, rest=RestBinding("POST", "/api/me/billing/cancel"),
    ),
    Capability(
        key="billing.payments", handler=_payments, Input=PaymentsInput,
        authz=ORG_MEMBER, rest=RestBinding("GET", "/api/me/billing/payments"),
    ),
    # Admin : forcer un plan sur une org SANS paiement (abonnement comp) ou le
    # retirer (plan=null). Ouvre l'entitlement (options + plafond messagerie du
    # plan) immédiatement. Sert pilotes/partenaires + palier « sur devis ».
    Capability(
        key="billing.admin_set_plan", handler=_admin_set_plan, Input=AdminPlanInput,
        authz=SUPER_ADMIN,
        description="[super admin] Force a plan on an org WITHOUT payment (comp "
                    "subscription): unlocks the plan's options + messaging seat cap "
                    "immediately, no PSP, never charged. Pass plan=null to remove a "
                    "comp plan (refuses to touch a PAID subscription). For pilots, "
                    "partners and the custom 'enterprise' tier.",
        mcp="oto_admin_set_plan",
        rest=RestBinding("POST", "/api/admin/orgs/{org_id}/plan", {"org_id": "org_id"}),
    ),
]
