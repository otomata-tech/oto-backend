"""Client HTTP mince vers l'API Mollie v2 (ADR 0043 — PSP, bascule Stancer→Mollie).

Périmètre = ce que le billing par org consomme : customers, payments (first =
page de checkout hébergée / recurring = rejeu MIT off-session), mandates
(card OU SEPA, créés par le premier paiement). Surface calquée sur le spec
officiel (https://docs.mollie.com/reference).

Points structurants (ADR 0043, delta vs Stancer) :
- **Mandat unifié card + SEPA** : le PREMIER paiement (`sequenceType=first`) sur
  la page de checkout crée un mandat réutilisable, quel que soit le moyen (carte
  ou prélèvement) — plus de flux SEPA séparé (IBAN tokenisé + signature OTP + ICS
  créancier). Le rejeu (`sequenceType=recurring`) tire sur `customerId`+`mandateId`.
- **Webhooks natifs** : chaque paiement peut porter un `webhookUrl` (Mollie POST
  l'id → on GET le paiement). Le billing_runner garde le POLLING comme socle ;
  le webhook (barreau ultérieur) l'enrichit sans le remplacer.
- **Idempotence par HEADER** `Idempotency-Key` (≠ champ `unique_id` Stancer) : un
  rejeu de la même clé renvoie le MÊME paiement (HTTP 200), jamais un 409 ni un
  double débit — la fenêtre Mollie couvre le retry d'un tick.
- **Montants en décimal-string** `{"currency":"EUR","value":"49.00"}` (≠ centiers
  entiers Stancer). Le reste du billing raisonne en CENTIMES (`PLANS`) ;
  `amount_field` convertit à la frontière.
- Auth **Bearer** (clés `test_…` sandbox / `live_…` prod ; le KYB du profil OTOMATA
  est déjà `verified` → pas de blocage go-live comme chez Stancer).

Config (env de process, jamais oto.config/SOPS) : `MOLLIE_API_KEY`.
Clé absente → RuntimeError actionnable à l'APPEL (le module s'importe sans, le
serveur boote sans billing configuré).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_BASE = "https://api.mollie.com"
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# Statuts au-delà desquels un paiement Mollie ne bouge plus (enum PaymentStatus).
# `open`/`pending`/`authorized` NE SONT PAS terminaux (checkout en cours, ou
# prélèvement SEPA soumis qui met des jours à se dénouer).
TERMINAL_PAYMENT_STATUSES = frozenset({"paid", "failed", "canceled", "expired"})

_client: Optional[httpx.Client] = None


class MollieError(RuntimeError):
    """Erreur API Mollie, message actionnable (statut HTTP + `detail` serveur)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Mollie HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def is_configured() -> bool:
    return bool(os.environ.get("MOLLIE_API_KEY"))


def _c() -> httpx.Client:
    global _client
    key = os.environ.get("MOLLIE_API_KEY")
    if not key:
        raise RuntimeError(
            "Mollie non configuré (MOLLIE_API_KEY absent de l'env). "
            "Le billing par org (ADR 0043) exige une clé API Mollie "
            "(test_… sandbox / live_… prod)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
    return _client


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        if isinstance(body, dict):
            # Mollie: {status, title, detail, field?}
            return str(body.get("detail") or body.get("title") or body)[:300]
        return str(body)[:300]
    except Exception:
        return (r.text or "")[:300]


def _req(method: str, path: str, *, json: Optional[dict] = None,
         params: Optional[dict] = None,
         idempotency_key: Optional[str] = None) -> Any:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    r = _c().request(method, path, json=json, params=params, headers=headers)
    if r.status_code >= 400:
        raise MollieError(r.status_code, _detail(r))
    if not r.content:
        return None
    return r.json()


# ── montants ─────────────────────────────────────────────────────────────────

def amount_field(cents: int, currency: str = "eur") -> dict:
    """Centimes (format interne du billing) → objet montant Mollie. Mollie exige
    une string décimale à 2 chiffres et un code devise MAJUSCULE ISO 4217."""
    return {"currency": currency.upper(), "value": f"{cents / 100:.2f}"}


# ── sonde ────────────────────────────────────────────────────────────────────

def ping() -> bool:
    """Sonde de configuration : GET /v2/methods avec la clé posée. Lève sur échec."""
    _req("GET", "/v2/methods")
    return True


# ── customers ────────────────────────────────────────────────────────────────

def create_customer(*, name: Optional[str] = None, email: Optional[str] = None,
                    metadata: Optional[dict] = None) -> dict:
    """Crée le customer Mollie d'une org. Mollie n'a pas d'`external_id` avec
    contrainte d'unicité (contrairement à Stancer) → on trace notre `org_id`
    dans `metadata` ; l'anti-doublon est côté miroir (`customer_id` réutilisé)."""
    body: dict[str, Any] = {}
    for k, v in (("name", name), ("email", email), ("metadata", metadata)):
        if v is not None:
            body[k] = v
    return _req("POST", "/v2/customers", json=body)


def get_customer(customer_id: str) -> dict:
    return _req("GET", f"/v2/customers/{customer_id}")


# ── payments : premier (checkout hébergé) ────────────────────────────────────

def create_first_payment(
    amount_cents: int,
    *,
    customer_id: str,
    redirect_url: str,
    currency: str = "eur",
    description: Optional[str] = None,
    method: Optional[str] = None,
    metadata: Optional[dict] = None,
    webhook_url: Optional[str] = None,
) -> dict:
    """Premier paiement (`sequenceType=first`) : le `_links.checkout.href` de la
    réponse = la page de paiement hébergée Mollie (3DS carte / collecte IBAN +
    mandat SEPA gérés par eux). Un mandat réutilisable naît à l'encaissement.
    `method` restreint la page ('creditcard' | 'directdebit' ; None = choix libre).
    `amount_cents` en CENTIMES (converti à la frontière)."""
    body: dict[str, Any] = {
        "amount": amount_field(amount_cents, currency),
        "customerId": customer_id,
        "sequenceType": "first",
        "redirectUrl": redirect_url,
    }
    for k, v in (("description", description), ("method", method),
                 ("metadata", metadata), ("webhookUrl", webhook_url)):
        if v is not None:
            body[k] = v
    return _req("POST", "/v2/payments", json=body)


def create_recurring_payment(
    amount_cents: int,
    *,
    customer_id: str,
    mandate_id: Optional[str] = None,
    currency: str = "eur",
    description: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    webhook_url: Optional[str] = None,
) -> dict:
    """Rejeu MIT off-session (`sequenceType=recurring`) — l'échéance du runner.
    Tire sur le mandat du customer (`mandate_id` explicite, sinon Mollie prend le
    mandat valide courant). `idempotency_key` DÉTERMINISTE côté runner : un rejeu
    renvoie le même paiement (jamais de double débit)."""
    body: dict[str, Any] = {
        "amount": amount_field(amount_cents, currency),
        "customerId": customer_id,
        "sequenceType": "recurring",
    }
    for k, v in (("mandateId", mandate_id), ("description", description),
                 ("webhookUrl", webhook_url)):
        if v is not None:
            body[k] = v
    return _req("POST", "/v2/payments", json=body,
                idempotency_key=idempotency_key)


def get_payment(payment_id: str) -> dict:
    return _req("GET", f"/v2/payments/{payment_id}")


def checkout_url(payment: dict) -> Optional[str]:
    """URL de la page de checkout hébergée portée par un premier paiement."""
    return (payment.get("_links", {}) or {}).get("checkout", {}).get("href")


def payment_is_terminal(status: str) -> bool:
    return status in TERMINAL_PAYMENT_STATUSES


# ── mandates (card OU SEPA — créés par le premier paiement) ───────────────────

def list_mandates(customer_id: str) -> list[dict]:
    resp = _req("GET", f"/v2/customers/{customer_id}/mandates")
    embedded = (resp or {}).get("_embedded", {}) if isinstance(resp, dict) else {}
    return embedded.get("mandates", []) or []


def get_mandate(customer_id: str, mandate_id: str) -> dict:
    return _req("GET", f"/v2/customers/{customer_id}/mandates/{mandate_id}")


def valid_mandate(customer_id: str) -> Optional[dict]:
    """Le mandat `valid` le plus utilisable du customer (celui né du premier
    paiement encaissé). None si aucun — récurrence impossible."""
    for m in list_mandates(customer_id):
        if m.get("status") == "valid":
            return m
    return None


def revoke_mandate(customer_id: str, mandate_id: str) -> None:
    """Révoque un mandat (rotation de moyen de paiement : l'ancien est purgé
    après bascule)."""
    _req("DELETE", f"/v2/customers/{customer_id}/mandates/{mandate_id}")


# ── mapping vocabulaire méthode (miroir 'card'|'sepa' ↔ Mollie) ───────────────

# Le miroir garde le vocabulaire produit 'card'|'sepa' ; Mollie parle
# 'creditcard'|'directdebit'.
_METHOD_TO_MOLLIE = {"card": "creditcard", "sepa": "directdebit"}
_MOLLIE_TO_METHOD = {"creditcard": "card", "directdebit": "sepa"}


def mollie_method(method: str) -> Optional[str]:
    """'card'|'sepa' → restriction de page Mollie ('creditcard'|'directdebit')."""
    return _METHOD_TO_MOLLIE.get(method)


def method_from_mollie(mollie_method_value: Optional[str]) -> str:
    """Méthode Mollie observée sur un paiement → vocabulaire miroir ('card' défaut)."""
    return _MOLLIE_TO_METHOD.get(mollie_method_value or "", "card")
