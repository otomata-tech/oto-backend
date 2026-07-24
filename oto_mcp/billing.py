"""Billing par org (ADR 0043) — abonnement unique, PSP Mollie.

Le cycle est piloté ICI (miroir local `org_subscriptions` = source de vérité,
PSP-agnostique par conception ADR 0043) :
- `subscribe` ouvre le PREMIER paiement sur la page de checkout hébergée Mollie
  (`sequenceType=first` — 3DS carte ou collecte IBAN + mandat SEPA gérés par eux,
  UN seul flux) et journalise le paiement ;
- `confirm` LIT le paiement au retour du payeur (et en réconciliation) : encaissé
  (`paid`) → récupère le mandat réutilisable né du checkout et pose le miroir
  `active` — c'est LUI qui ouvre l'entitlement, jamais le redirect brut ;
- `cancel` marque la résiliation à fin de période (l'entitlement court jusqu'à
  `current_period_end` ; le billing_runner fera la bascule).

Bascule Stancer→Mollie (ADR 0043, amende 2026-07-24) : Mollie **unifie carte et
SEPA** derrière un customer + un mandat créé au premier paiement → plus de chemin
SEPA séparé (IBAN tokenisé + signature OTP + ICS créancier). Le rejeu MIT tire sur
`customerId`+`mandateId`. Webhooks natifs (barreau ultérieur) ; polling = socle.

Le plan (prix, options débloquées) vit dans `PLANS` — mapping en CODE (pas de
table) : la vérité produit est versionnée et relue par l'entitlement (has_option,
2e source). ⚠️ Valeurs actuelles = prix actés Alexis 2026-07-06.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

from . import mollie_client
from . import db
from .db import billing as db_billing

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """Feature flag global (ADR 0043, dark launch) : la surface billing (capacités
    REST/MCP + nav dashboard + runner) n'est exposée QUE si `OTO_BILLING_ENABLED=1`.
    Absent/0 = dormant. Piloté par-déploiement (prod off tant que le PSP n'est pas
    live, canari on) sans divergence de branche ni revert."""
    return os.environ.get("OTO_BILLING_ENABLED", "0") == "1"

# plan → prix (centimes), intervalle, options de connecteur débloquées (couche 3,
# lues par access.has_option). Prix HT mensuels (Alexis 2026-07-06). Chaque
# plan CONFIGURE l'org à l'activation (options débloquées + plafond de comptes
# messagerie) → une seule action admin, plus de grants/quotas par connecteur.
# `amount=None` = palier sur devis (pas de checkout self-serve ; posé par un
# admin en abonnement `comp`). `unipile_accounts` alimente orgs.unipile_account_
# limit ; `unmetered=True` = clés plateforme sans quota (fin des credits d'appel).
# Plafonds de comptes messagerie actés (Alexis 2026-07-09) : 1 / 10 / 50.
PLANS: dict[str, dict] = {
    "solo": {
        "label": "Solo", "amount": 4900, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": 1, "unmetered": True,
    },
    "team": {
        "label": "Team", "amount": 25000, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": 10, "unmetered": True,
    },
    "business": {
        "label": "Business", "amount": 50000, "currency": "eur", "interval": "month",
        "options": ("unipile",), "unipile_accounts": 50, "unmetered": True,
    },
    "enterprise": {
        "label": "Entreprise (sur devis)", "amount": None, "currency": "eur",
        "interval": "month", "options": ("unipile",), "unipile_accounts": None,
        "unmetered": True, "custom": True,
    },
}


def plans() -> list[dict]:
    """Catalogue public (l'UI billing du dashboard boucle dessus)."""
    return [{"plan": k, "custom": v.get("custom", False),
             **{f: v[f] for f in ("label", "amount", "currency", "interval",
                                  "unipile_accounts")}}
            for k, v in PLANS.items()]


def plan_options(plan: str) -> frozenset[str]:
    """Options de connecteur débloquées par `plan` (consommé par access.has_option)."""
    meta = PLANS.get(plan)
    return frozenset(meta["options"]) if meta else frozenset()


def plan_is_unmetered(plan: str) -> bool:
    """Le plan lève-t-il les quotas des clés plateforme ? (fin des credits d'appel)."""
    meta = PLANS.get(plan)
    return bool(meta and meta.get("unmetered"))


def apply_plan_entitlements(org_id: int, plan: str) -> None:
    """Configure l'org d'après son plan à l'ACTIVATION — le geste qui remplace
    le micro-management admin (options + plafond messagerie posés d'un coup).
    Idempotent. `unipile_accounts=None` (devis) = plafond levé."""
    meta = PLANS.get(plan)
    if meta is None:
        return
    db.set_org_unipile_limit(org_id, meta.get("unipile_accounts"))


def _add_period(dt: datetime, interval: str) -> datetime:
    """Échéance suivante au mois/an CALENDAIRE (pas d'approximation 30 j) —
    borné au dernier jour du mois cible (31/01 + 1 mois → 28/02)."""
    if interval == "year":
        return _safe_replace(dt, year=dt.year + 1, month=dt.month)
    month = dt.month + 1
    year = dt.year + (1 if month > 12 else 0)
    return _safe_replace(dt, year=year, month=((month - 1) % 12) + 1)


def _safe_replace(dt: datetime, *, year: int, month: int) -> datetime:
    for day in (dt.day, 30, 29, 28):
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            continue
    raise AssertionError("unreachable")


def webhook_url() -> str:
    """URL publique que Mollie rappelle à chaque changement d'état d'un paiement
    (base = `OTO_MCP_PUBLIC_URL`, cf. Logto/Google OAuth). Portée par chaque
    paiement créé → réconciliation événementielle en complément du polling."""
    base = os.environ.get("OTO_MCP_PUBLIC_URL", "https://mcp.oto.ninja").rstrip("/")
    return f"{base}/api/billing/webhook"


# ── souscription ─────────────────────────────────────────────────────────────

def subscribe(org_id: int, plan: str, return_url: str, *,
              method: str = "card") -> dict:
    """Ouvre la souscription. UN seul flux (Mollie unifie carte et SEPA) : premier
    paiement `sequenceType=first` → l'URL renvoyée = la page de checkout hébergée
    Mollie où le payeur finit le geste (3DS carte, ou saisie IBAN + acceptation du
    mandat SEPA). `method` ∈ {card, sepa} restreint la page ; le mandat réutilisable
    naît à l'encaissement. Le miroir n'est PAS posé ici — il naît à `confirm`
    (paiement constaté), qui relit le plan de la `metadata` du paiement."""
    meta = PLANS.get(plan)
    if meta is None:
        raise ValueError(f"unknown_plan: {plan!r} (plans : {', '.join(PLANS)})")
    if meta.get("custom"):
        raise ValueError("custom_plan: ce palier est sur devis — contacter "
                         "Otomata (un admin l'active en abonnement comp)")
    if method not in ("card", "sepa"):
        raise ValueError(f"unknown_method: {method!r} (card | sepa)")
    existing = db_billing.get_org_subscription(org_id)
    if existing and existing["status"] == "active" and not existing.get("canceled_at"):
        raise ValueError("already_subscribed: l'org a déjà un abonnement actif")

    customer_id = existing["customer_id"] if existing and existing.get("customer_id") else None
    if not customer_id:
        cust = mollie_client.create_customer(
            name=f"Otomata org {org_id}", metadata={"org_id": str(org_id)})
        customer_id = cust["id"]

    payment = mollie_client.create_first_payment(
        meta["amount"], customer_id=customer_id, currency=meta["currency"],
        redirect_url=return_url, description=f"Abonnement {meta['label']}",
        method=mollie_client.mollie_method(method), webhook_url=webhook_url(),
        # le plan voyage dans la metadata du paiement (pas d'état serveur pendant
        # le checkout : confirm le relit → survit à un restart).
        metadata={"org_id": str(org_id), "plan": plan})
    db_billing.insert_billing_payment(
        org_id, "initial", meta["amount"], currency=meta["currency"],
        payment_intent_id=payment["id"], status=payment.get("status", "open"))
    return {"checkout_url": mollie_client.checkout_url(payment),
            "payment_intent_id": payment["id"], "plan": plan, "method": method}


def confirm(org_id: int) -> dict:
    """Fait avancer la souscription en cours (POLLING) : lit le premier paiement ;
    encaissé (`paid`) → récupère le mandat réutilisable né du checkout, pose le
    miroir `active` (carte comme SEPA — même chemin). Idempotent : re-confirmer un
    abonnement déjà actif est un no-op informatif."""
    sub_row = db_billing.get_org_subscription(org_id)
    open_initial = [
        p for p in db_billing.list_billing_payments(org_id)
        if p["kind"] == "initial"
        and p["status"] not in db_billing.TERMINAL_PAYMENT_STATUSES
        and p.get("payment_intent_id")
    ]
    if not open_initial:
        if sub_row and sub_row["status"] == "active":
            return {"status": "active", "plan": sub_row["plan"]}
        raise ValueError("no_pending_subscription: aucun paiement initial en cours")

    row = open_initial[0]  # le plus récent (list_billing_payments trie DESC)
    payment = mollie_client.get_payment(row["payment_intent_id"])
    pstatus = str(payment.get("status") or "")

    if pstatus in ("failed", "canceled", "expired"):
        db_billing.update_billing_payment(row["id"], status=pstatus)
        return {"status": "failed", "payment_status": pstatus}
    if pstatus != "paid":
        # pas encaissé : le payeur est peut-être encore sur la page de checkout.
        return {"status": "pending", "payment_status": pstatus}

    # encaissé → le mandat réutilisable existe désormais sur le customer.
    customer_id = payment.get("customerId")
    mandate = mollie_client.valid_mandate(customer_id) if customer_id else None
    if not mandate:
        # payé mais pas de mandat valide → pas de récurrence possible : on ne pose
        # PAS un abonnement qu'on ne saura pas renouveler (ADR : jamais de fallback
        # silencieux). Cas à investiguer (méthode non récurrente sur la page).
        raise RuntimeError(
            "no_mandate: premier paiement encaissé sans mandat valide — récurrence "
            "impossible, vérifier le moyen de paiement de la page de checkout")

    plan = (payment.get("metadata") or {}).get("plan")
    if plan not in PLANS:
        raise RuntimeError(f"bad_metadata: plan illisible sur le paiement ({plan!r})")
    meta = PLANS[plan]
    method = mollie_client.method_from_mollie(payment.get("method"))

    now = datetime.now(timezone.utc)
    period_end = _add_period(now, meta["interval"])
    db_billing.update_billing_payment(row["id"], status="paid",
                                      payment_id=payment["id"])
    db_billing.upsert_org_subscription(
        org_id, plan=plan, method=method, provider="mollie",
        customer_id=customer_id, mandate_id=mandate["id"],
        mandate_rum=mandate.get("mandateReference"),
        status="active", current_period_end=period_end, next_billing_at=period_end)
    apply_plan_entitlements(org_id, plan)
    logger.info("billing: org %s abonnée (plan %s, méthode %s, échéance %s)",
                org_id, plan, method, period_end.date())
    return {"status": "active", "plan": plan, "method": method,
            "current_period_end": period_end.isoformat()}


# ── état & résiliation ───────────────────────────────────────────────────────

def status(org_id: int) -> dict:
    row = db_billing.get_org_subscription(org_id)
    if not row:
        return {"subscribed": False, "plans": plans()}
    meta = PLANS.get(row["plan"], {})
    return {
        "subscribed": row["status"] in ("active", "past_due"),
        "plan": row["plan"], "label": meta.get("label"),
        "amount": meta.get("amount"), "currency": meta.get("currency"),
        "interval": meta.get("interval"),
        "status": row["status"], "method": row["method"],
        "comp": row["provider"] == "comp",   # abonnement forcé par un admin (non payé)
        "current_period_end": row.get("current_period_end"),
        "next_billing_at": row.get("next_billing_at"),
        "grace_until": row.get("grace_until"),
        "canceled_at": row.get("canceled_at"),
    }


def cancel(org_id: int) -> dict:
    """Résiliation à fin de période : l'entitlement court jusqu'à
    `current_period_end`, plus aucune échéance n'est tirée (next_billing_at
    nettoyé) ; le billing_runner basculera le statut à l'échéance."""
    row = db_billing.get_org_subscription(org_id)
    if not row or row["status"] == "canceled":
        raise ValueError("not_subscribed: aucun abonnement à résilier")
    db_billing.mark_cancel_at_period_end(org_id)
    return status(org_id)


# ── admin : forcer / retirer un plan (non payé) ──────────────────────────────

def admin_set_plan(org_id: int, plan: str, *, granted_by: str) -> dict:
    """Force un plan sur une org SANS paiement (abonnement `comp`) — ADR 0043.
    Ouvre l'entitlement immédiatement (options + plafond messagerie du plan),
    jamais de PSP derrière, jamais d'échéance tirée. Sert les pilotes,
    partenaires et le palier « sur devis ». Écrase l'abonnement existant."""
    if plan not in PLANS:
        raise ValueError(f"unknown_plan: {plan!r} (plans : {', '.join(PLANS)})")
    db_billing.set_comp_subscription(org_id, plan, granted_by=granted_by)
    apply_plan_entitlements(org_id, plan)
    logger.info("billing: plan %s FORCÉ (comp) sur l'org %s par %s",
                plan, org_id, granted_by)
    return status(org_id)


def admin_clear_plan(org_id: int) -> dict:
    """Retire un abonnement `comp` (forcé). Refuse de toucher un abonnement PAYÉ
    (passer par la résiliation) — anti-bévue admin."""
    row = db_billing.get_org_subscription(org_id)
    if not row:
        raise ValueError("not_subscribed: aucun abonnement sur cette org")
    if row["provider"] != "comp":
        raise ValueError("paid_subscription: abonnement payant — résilier via "
                         "cancel, pas admin_clear_plan")
    db_billing.delete_subscription(org_id)
    db.set_org_unipile_limit(org_id, None)   # retire le plafond posé par le plan
    logger.info("billing: plan comp retiré de l'org %s", org_id)
    return {"subscribed": False, "org_id": org_id}


# ── webhook Mollie (réconciliation événementielle) ───────────────────────────

def process_webhook(payment_id: str) -> str:
    """Traite un rappel webhook Mollie (le corps ne porte QUE l'id du paiement —
    on re-fetch l'objet avec NOTRE clé, jamais de confiance dans le POST). Retourne
    l'issue (log) : 'ignored' | 'confirmed' | 'updated' | 'unchanged'.

    Sécurité : un id inconnu de notre journal est ignoré (un POST forgé ne
    déclenche rien) ; un premier paiement `paid` rejoue `confirm` (idempotent) ;
    sinon on aligne le statut journalisé. Complément du polling (billing_runner),
    pas un remplacement."""
    row = db_billing.get_billing_payment_by_ref(payment_id)
    if not row:
        return "ignored"
    payment = mollie_client.get_payment(payment_id)
    status = str(payment.get("status") or "")
    if row["kind"] == "initial" and status == "paid":
        confirm(row["org_id"])   # pose le miroir si pas déjà fait (idempotent)
        return "confirmed"
    if status and status != row["status"]:
        db_billing.update_billing_payment(row["id"], status=status)
        return "updated"
    return "unchanged"
