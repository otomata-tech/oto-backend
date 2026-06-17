"""Capacités billing (ADR 0009) : solde de credits, historique, packs, recharge Stripe.

Le porte-monnaie est PAR ORGANISATION : `ORG_MEMBER` injecte l'org active depuis
l'état serveur (jamais un id client → verrou IDOR). L'achat de credits recharge le
wallet partagé → ouvert à tout membre de l'org (bénin ; pour réserver à l'org_admin,
introduire un combinateur `ORG_ADMIN_OF_ACTIVE` dans `_authz`).

Montage auto (MCP + REST) via les adaptateurs qui bouclent sur `registry.CAPABILITIES`.
"""
from __future__ import annotations

from pydantic import BaseModel

from .. import billing, credits_store
from ._authz import ORG_MEMBER, SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES


class NoInput(BaseModel):
    pass


class TxInput(BaseModel):
    limit: int = 50


class CheckoutInput(BaseModel):
    pack_id: str


def _balance(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return credits_store.get_balance(ctx.org_id)


def _transactions(ctx: ResolvedCtx, inp: TxInput) -> dict:
    return {"org_id": ctx.org_id,
            "transactions": credits_store.list_transactions(ctx.org_id, inp.limit)}


def _packs(ctx: ResolvedCtx, inp: NoInput) -> dict:
    return {"packs": billing.packs()}


def _checkout(ctx: ResolvedCtx, inp: CheckoutInput) -> dict:
    return billing.create_checkout_session(ctx.org_id, inp.pack_id, ctx.sub)


CAPABILITIES += [
    Capability(
        key="billing.balance", handler=_balance, Input=NoInput, authz=ORG_MEMBER,
        description="Remaining MCP call credits for your active organization "
                    "(balance, low flag, whether the free base stock was granted).",
        mcp="billing_balance", rest=RestBinding("GET", "/api/me/billing"),
    ),
    Capability(
        key="billing.transactions", handler=_transactions, Input=TxInput, authz=ORG_MEMBER,
        rest=RestBinding("GET", "/api/me/billing/transactions"),
    ),
    Capability(
        key="billing.packs", handler=_packs, Input=NoInput, authz=SUB_ONLY,
        rest=RestBinding("GET", "/api/billing/packs"),
    ),
    Capability(
        key="billing.checkout", handler=_checkout, Input=CheckoutInput, authz=ORG_MEMBER,
        rest=RestBinding("POST", "/api/me/billing/checkout"),
    ),
]
