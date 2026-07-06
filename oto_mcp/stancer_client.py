"""Client HTTP mince vers l'API Stancer v2 (ADR 0043, B1).

Périmètre = ce que le billing par org consomme : customers, payment intents
(page de paiement hébergée — le `url` de l'intent est la page Stancer, 3DS
géré par eux), payments (rejeu MIT sur token `card_xxx` par le billing_runner),
cards. Surface calquée sur le spec OpenAPI officiel
(https://docs.stancer.com/api/openapi.json, version v2).

Points structurants (ADR 0043) :
- **Pas de webhooks chez Stancer** → le suivi d'état est du POLLING :
  `get_payment_intent`/`get_payment` re-lus jusqu'à statut terminal
  (`TERMINAL_INTENT_STATUSES` / db.billing.TERMINAL_PAYMENT_STATUSES).
- **Idempotence** : tout rejeu MIT passe `unique_id` (unicité vérifiée par
  Stancer) — un retry runner ne double-débite jamais.
- Auth HTTP Basic : la clé API en username, mot de passe vide
  (clés `stest_…` sandbox / `sprod_…` live).

Config (env de process, jamais oto.config/SOPS) : `STANCER_API_KEY`.
Clé absente → RuntimeError actionnable à l'APPEL (le module s'importe sans, le
serveur boote sans billing configuré).
"""
from __future__ import annotations

import os
from typing import Any, Optional

import httpx

_BASE = "https://api.stancer.com"
_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# Statuts au-delà desquels un payment intent ne bouge plus (enum
# PaymentIntentStatus du spec). `authorized` n'est PAS terminal : capture=true
# par défaut → on attend `captured`.
TERMINAL_INTENT_STATUSES = frozenset({"captured", "canceled", "unpaid"})

_client: Optional[httpx.Client] = None


class StancerError(RuntimeError):
    """Erreur API Stancer, message actionnable (statut HTTP + détail serveur)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Stancer HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


def is_configured() -> bool:
    return bool(os.environ.get("STANCER_API_KEY"))


def _c() -> httpx.Client:
    global _client
    key = os.environ.get("STANCER_API_KEY")
    if not key:
        raise RuntimeError(
            "Stancer non configuré (STANCER_API_KEY absent de l'env). "
            "Le billing par org (ADR 0043) exige une clé API Stancer "
            "(stest_… sandbox / sprod_… live)."
        )
    if _client is None:
        _client = httpx.Client(
            base_url=_BASE,
            auth=(key, ""),  # HTTP Basic : clé en username, password vide
            timeout=_TIMEOUT,
        )
    return _client


def _detail(r: httpx.Response) -> str:
    try:
        body = r.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("error") or body)[:300]
        return str(body)[:300]
    except Exception:
        return (r.text or "")[:300]


def _req(method: str, path: str, *, json: Optional[dict] = None,
         params: Optional[dict] = None) -> Any:
    r = _c().request(method, path, json=json, params=params)
    if r.status_code >= 400:
        raise StancerError(r.status_code, _detail(r))
    if not r.content:
        return None
    return r.json()


# ── sonde ────────────────────────────────────────────────────────────────────

def ping() -> bool:
    """Sonde de configuration : GET /v2/ping avec la clé posée. Lève sur échec."""
    _req("GET", "/v2/ping")
    return True


# ── customers ────────────────────────────────────────────────────────────────

def create_customer(*, email: Optional[str] = None, name: Optional[str] = None,
                    external_id: Optional[str] = None) -> dict:
    """Crée le customer Stancer d'une org (`external_id` = notre org_id, unicité
    vérifiée par Stancer → une re-souscription retombe en erreur explicite
    plutôt qu'en doublon silencieux)."""
    body = {k: v for k, v in
            (("email", email), ("name", name), ("external_id", external_id))
            if v is not None}
    return _req("POST", "/v2/customers/", json=body)


def get_customer(customer_id: str) -> dict:
    return _req("GET", f"/v2/customers/{customer_id}")


# ── payment intents (page hébergée) ──────────────────────────────────────────

def create_payment_intent(
    amount: int,
    *,
    currency: str = "eur",
    customer: Optional[str] = None,
    return_url: Optional[str] = None,
    methods_allowed: tuple[str, ...] = ("card",),
    description: Optional[str] = None,
    order_id: Optional[str] = None,
) -> dict:
    """Crée un payment intent ; le champ `url` de la réponse = la page de
    paiement hébergée Stancer (3DS + tokenisation gérés par eux). `amount` en
    centimes (format Stancer)."""
    body: dict[str, Any] = {
        "amount": amount,
        "currency": currency,
        "methods_allowed": list(methods_allowed),
    }
    for k, v in (("customer", customer), ("return_url", return_url),
                 ("description", description), ("order_id", order_id)):
        if v is not None:
            body[k] = v
    return _req("POST", "/v2/payment_intents/", json=body)


def get_payment_intent(intent_id: str) -> dict:
    return _req("GET", f"/v2/payment_intents/{intent_id}")


def payment_intent_payments(intent_id: str) -> Any:
    """Payments portés par l'intent (pour extraire paym_xxx + card_xxx après
    encaissement sur la page hébergée)."""
    return _req("GET", f"/v2/payment_intents/{intent_id}/payments")


def intent_is_terminal(status: str) -> bool:
    return status in TERMINAL_INTENT_STATUSES


# ── payments (rejeu MIT par le billing_runner) ───────────────────────────────

def create_payment(
    amount: int,
    *,
    currency: str = "eur",
    card: Optional[str] = None,
    sepa: Optional[str] = None,
    customer: Optional[str] = None,
    unique_id: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Rejoue un paiement sur un moyen tokenisé (`card_xxx` ou `sepa_xxx`) —
    l'échéance MIT du runner. `unique_id` OBLIGATOIRE côté appelant runner
    (idempotence : un retry ne double-débite pas) ; optionnel ici pour ne pas
    contraindre les autres usages."""
    if not card and not sepa:
        raise ValueError("create_payment : `card` ou `sepa` requis (rejeu sur token)")
    body: dict[str, Any] = {"amount": amount, "currency": currency}
    for k, v in (("card", card), ("sepa", sepa), ("customer", customer),
                 ("unique_id", unique_id), ("description", description)):
        if v is not None:
            body[k] = v
    return _req("POST", "/v2/payments/", json=body)


def get_payment(payment_id: str) -> dict:
    return _req("GET", f"/v2/payments/{payment_id}")


# ── sepa & mandats (phase 2 — prélèvement) ───────────────────────────────────

def create_sepa(*, iban: str, name: str, customer: str) -> dict:
    """Tokenise un IBAN (`sepa_xxx`). Le prélèvement exige ENSUITE un mandat
    signé (create_mandate → sign_url) — sans lui, tout paiement est refusé
    `no valid mandate` (vérifié sandbox 2026-07-06)."""
    return _req("POST", "/v2/sepa/", json={"iban": iban, "name": name,
                                           "customer": customer})


def create_mandate(sepa_id: str) -> dict:
    """Crée le mandat du `sepa_xxx` et renvoie notamment `sign_url` (page de
    signature hébergée Stancer, OTP SMS — le customer DOIT porter un mobile)
    et `upload_url` (voie mandat papier). RUM générée par Stancer à la
    signature ; `signed_at`/`rum` restent null tant que non signé."""
    return _req("POST", "/v2/mandates/", json={"sepa": sepa_id})


def get_mandate(mandate_id: str) -> dict:
    return _req("GET", f"/v2/mandates/{mandate_id}")


def mandate_is_signed(mandate: dict) -> bool:
    return bool(mandate.get("signed_at"))


# ── cards ────────────────────────────────────────────────────────────────────

def get_card(card_id: str) -> dict:
    """Fiche carte tokenisée — porte l'expiration (Stancer notifie l'approche
    d'expiration ; l'écran billing la surface)."""
    return _req("GET", f"/v2/cards/{card_id}")


def delete_card(card_id: str) -> None:
    """Supprime un token carte (rotation de moyen de paiement : l'ancienne
    carte est purgée après bascule)."""
    _req("DELETE", f"/v2/cards/{card_id}")
