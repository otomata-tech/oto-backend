"""Porte-monnaie de credits d'appel PAR ORGANISATION (soft billing).

`balance` = compteur entier d'appels restants ; **peut devenir négatif** — on ne
bloque jamais un appel (soft enforcement). Don de base unique de OTO_MCP_FREE_CALLS
crédité paresseusement (`ensure_wallet`, au 1er débit OU 1re lecture). Top-ups Stripe
via `credit()`, **idempotent** sur `stripe_event_id`. Le débit par appel ne fait
qu'un `UPDATE balance` (pas de ligne ledger — cf. db._SCHEMA, volumétrie).

Couche backend-core (ADR 0004) : réutilise les primitives `db` (`_connect`), aucun
import d'adaptateur (MCP/REST). Appelé par le hook de calllog (`server._calllog_sink`)
et par les capacités `capabilities/billing.py`.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from .db import _connect

logger = logging.getLogger(__name__)


def _free_calls() -> int:
    try:
        return int(os.environ.get("OTO_MCP_FREE_CALLS", "1000"))
    except ValueError:
        return 1000


def _low_threshold() -> int:
    try:
        return int(os.environ.get("OTO_MCP_LOW_BALANCE_THRESHOLD", "50"))
    except ValueError:
        return 50


def ensure_wallet(org_id: int) -> dict:
    """Matérialise le wallet de l'org si absent, en créditant le don de base unique.

    Idempotent (PK `org_id` + `ON CONFLICT DO NOTHING`). Renvoie `{balance,
    base_granted}`. Le don de base et sa ligne ledger sont posés dans la MÊME
    transaction que l'insert — soit les deux, soit rien.
    """
    free = _free_calls()
    with _connect() as conn:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO org_credits (org_id, balance, base_granted) "
                "VALUES (%s, %s, TRUE) ON CONFLICT (org_id) DO NOTHING "
                "RETURNING balance, base_granted",
                (org_id, free),
            ).fetchone()
            if row is not None:
                # Première matérialisation → trace le don de base au ledger.
                conn.execute(
                    "INSERT INTO credit_transactions (org_id, delta, reason) "
                    "VALUES (%s, %s, 'base_grant')",
                    (org_id, free),
                )
                return {"balance": int(row["balance"]), "base_granted": True}
        # Existait déjà : relire hors de la transaction d'insert.
        cur = conn.execute(
            "SELECT balance, base_granted FROM org_credits WHERE org_id = %s",
            (org_id,),
        ).fetchone()
        return {"balance": int(cur["balance"]), "base_granted": bool(cur["base_granted"])}


def get_balance(org_id: int) -> dict:
    """`{org_id, balance, base_granted, low}`. Matérialise le wallet (don de base) au besoin."""
    w = ensure_wallet(org_id)
    return {
        "org_id": org_id,
        "balance": w["balance"],
        "base_granted": w["base_granted"],
        "low": w["balance"] <= _low_threshold(),
    }


def debit(org_id: int, n: int = 1, reason: str = "call") -> Optional[int]:
    """Décrémente la balance de `n` (autorise le négatif). Renvoie la nouvelle
    balance, ou `None` si le wallet n'existe pas ENCORE (cf. `debit_for_call` hot-path).
    NE pose PAS de ligne ledger (volumétrie — cf. db._SCHEMA)."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE org_credits SET balance = balance - %s, updated_at = NOW() "
            "WHERE org_id = %s RETURNING balance",
            (n, org_id),
        ).fetchone()
        return int(row["balance"]) if row else None


def credit(
    org_id: int, n: int, reason: str, stripe_event_id: Optional[str] = None
) -> dict:
    """Ajoute `n` credits (top-up Stripe / ajustement admin). **Idempotent** sur
    `stripe_event_id` : un rejeu du webhook ne crédite qu'une fois. Renvoie
    `{org_id, balance, applied}` (`applied=False` = rejeu détecté, no-op).

    Le `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id` est le
    verrou : si la ligne existe déjà, on saute l'`UPDATE` de balance. Les deux
    tournent dans une seule transaction → atomicité sur crash.
    """
    ensure_wallet(org_id)
    with _connect() as conn:
        with conn.transaction():
            if stripe_event_id is not None:
                ins = conn.execute(
                    "INSERT INTO credit_transactions (org_id, delta, reason, stripe_event_id) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (stripe_event_id) DO NOTHING "
                    "RETURNING id",
                    (org_id, n, reason, stripe_event_id),
                ).fetchone()
                if ins is None:
                    cur = conn.execute(
                        "SELECT balance FROM org_credits WHERE org_id = %s", (org_id,)
                    ).fetchone()
                    return {"org_id": org_id, "balance": int(cur["balance"]), "applied": False}
            else:
                conn.execute(
                    "INSERT INTO credit_transactions (org_id, delta, reason) "
                    "VALUES (%s, %s, %s)",
                    (org_id, n, reason),
                )
            row = conn.execute(
                "UPDATE org_credits SET balance = balance + %s, updated_at = NOW() "
                "WHERE org_id = %s RETURNING balance",
                (n, org_id),
            ).fetchone()
            return {"org_id": org_id, "balance": int(row["balance"]), "applied": True}


def _unipile_monthly_credits() -> int:
    """Coût mensuel en credits d'UN compte LinkedIn connecté (refacturation du
    ~5 €/compte/mois d'Unipile). Configurable ; défaut 500 credits."""
    try:
        return int(os.environ.get("OTO_MCP_UNIPILE_MONTHLY_CREDITS", "500"))
    except ValueError:
        return 500


def charge_unipile_monthly(period: str) -> dict:
    """Débite chaque org de `_unipile_monthly_credits()` par compte LinkedIn connecté
    porté par son abonnement, pour le mois `period` ("YYYY-MM"). **Idempotent** par
    (org, compte, mois) via la clé `stripe_event_id="unipile:{org}:{account}:{period}"`
    (UNIQUE du ledger) → rejouable sans double-débit. Renvoie un récap.

    Soft : le débit passe par `credit()` (delta négatif, ligne ledger auditable), le
    solde peut devenir négatif — on ne bloque rien, on facture.
    """
    from . import db

    per = _unipile_monthly_credits()
    charged = 0
    skipped = 0  # déjà facturé ce mois (idempotency)
    orgs: set[int] = set()
    for acc in db.list_unipile_accounts_by_org():
        org_id, account_id = acc["org_id"], acc["account_id"]
        key = f"unipile:{org_id}:{account_id}:{period}"
        res = credit(org_id, -per, "unipile_monthly", stripe_event_id=key)
        if res.get("applied"):
            charged += 1
            orgs.add(org_id)
        else:
            skipped += 1
    return {
        "period": period, "per_account_credits": per,
        "accounts_charged": charged, "accounts_skipped": skipped,
        "orgs_charged": len(orgs),
    }


def list_transactions(org_id: int, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, delta, reason, stripe_event_id, created_at "
            "FROM credit_transactions WHERE org_id = %s "
            "ORDER BY created_at DESC, id DESC LIMIT %s",
            (org_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def debit_for_call(sub: Optional[str]) -> None:
    """Best-effort : débite 1 credit du wallet de l'org ACTIVE de `sub`.

    No-op si `sub` absent (stdio/dev) ou sans org active (pré-onboarding =
    untracked/gratuit). **Avale toute exception** : la facturation ne doit JAMAIS
    faire échouer un appel (et de toute façon l'appel a déjà produit son résultat
    quand ce hook tourne, post-exécution). Hot-path : 1 seul UPDATE en régime établi.
    """
    if not sub:
        return
    try:
        from . import org_store

        org_id = org_store.get_active_org(sub)
        if org_id is None:
            return
        if debit(org_id, 1, "call") is None:
            # Wallet pas encore matérialisé → pose le don de base puis redébite.
            ensure_wallet(org_id)
            debit(org_id, 1, "call")
    except Exception:
        logger.debug("credit debit skipped (best-effort)", exc_info=True)
