"""Store de l'abonnement par org (ADR 0043) — miroir + machine à états.

Deux tables : `org_subscriptions` (≤1 ligne par org, la vérité du cycle — le
miroir local fait foi, PSP-agnostique par conception) et `billing_payments`
(journal des échéances : audit, UI, file de réconciliation).

Les statuts de `billing_payments` sont les statuts Mollie OBSERVÉS (enum
PaymentStatus) ; les statuts TERMINAUX sont figés ici (`TERMINAL_PAYMENT_
STATUSES`) — la file de réconciliation (`open_billing_payments`) = tout le reste.
"""
from __future__ import annotations

from typing import Any, Optional

from ._conn import _connect

# Statuts Mollie au-delà desquels un paiement ne bouge plus (enum PaymentStatus —
# doit rester aligné avec l'index partiel idx_billing_payments_open de _schema.py).
TERMINAL_PAYMENT_STATUSES = frozenset(
    {"paid", "failed", "canceled", "expired"}
)

# `incomplete` = souscription ouverte non encore activée (jamais entitled — les
# lectures d'entitlement ne regardent que active/past_due). Conservé pour
# compat de schéma ; le flux Mollie unifié pose le miroir directement `active`.
SUBSCRIPTION_STATUSES = ("incomplete", "active", "past_due", "canceled")


# ── org_subscriptions ────────────────────────────────────────────────────────

def get_org_subscription(org_id: int) -> Optional[dict]:
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM org_subscriptions WHERE org_id = %s", (org_id,)
        ).fetchone()


def upsert_org_subscription(
    org_id: int,
    *,
    plan: str,
    method: str = "card",
    provider: str = "mollie",
    customer_id: Optional[str] = None,
    card_id: Optional[str] = None,
    sepa_id: Optional[str] = None,
    mandate_id: Optional[str] = None,
    mandate_rum: Optional[str] = None,
    status: str = "active",
    current_period_end: Optional[str] = None,
    next_billing_at: Optional[str] = None,
) -> None:
    """Crée ou remplace l'abonnement de l'org (souscription / re-souscription).

    Remplacement TOTAL assumé (re-souscrire après résiliation repart propre) —
    les mises à jour ciblées du cycle passent par les setters dédiés ci-dessous.
    """
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_subscriptions
                (org_id, provider, customer_id, card_id, sepa_id, mandate_id,
                 mandate_rum, method, plan, status, current_period_end,
                 next_billing_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (org_id) DO UPDATE SET
                provider = EXCLUDED.provider,
                customer_id = EXCLUDED.customer_id,
                card_id = EXCLUDED.card_id,
                sepa_id = EXCLUDED.sepa_id,
                mandate_id = EXCLUDED.mandate_id,
                mandate_rum = EXCLUDED.mandate_rum,
                method = EXCLUDED.method,
                plan = EXCLUDED.plan,
                status = EXCLUDED.status,
                current_period_end = EXCLUDED.current_period_end,
                next_billing_at = EXCLUDED.next_billing_at,
                grace_until = NULL,
                canceled_at = NULL,
                updated_at = NOW()
            """,
            (org_id, provider, customer_id, card_id, sepa_id, mandate_id,
             mandate_rum, method, plan, status, current_period_end,
             next_billing_at),
        )


def set_subscription_status(
    org_id: int,
    status: str,
    *,
    grace_until: Optional[str] = None,
    canceled: bool = False,
) -> bool:
    """Fait avancer la machine à états. `canceled=True` stampe `canceled_at`."""
    if status not in SUBSCRIPTION_STATUSES:
        raise ValueError(f"statut d'abonnement inconnu : {status!r}")
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET status = %s, grace_until = %s, "
            "canceled_at = CASE WHEN %s THEN NOW() ELSE canceled_at END, "
            "updated_at = NOW() WHERE org_id = %s",
            (status, grace_until, canceled, org_id),
        ).rowcount
    return n > 0


def set_comp_subscription(org_id: int, plan: str, *,
                          granted_by: Optional[str] = None) -> None:
    """Abonnement FORCÉ par un admin, non payé (ADR 0043) : `provider='comp'`,
    `next_billing_at=NULL` → jamais tiré par le billing_runner, jamais de PSP
    derrière. Ouvre l'entitlement exactement comme un abonnement payé (le seam
    has_option ne regarde que le plan). Remplace tout abonnement existant."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO org_subscriptions "
            "  (org_id, provider, method, plan, status, next_billing_at) "
            "VALUES (%s, 'comp', 'comp', %s, 'active', NULL) "
            "ON CONFLICT (org_id) DO UPDATE SET "
            "  provider='comp', method='comp', plan=EXCLUDED.plan, "
            "  status='active', customer_id=NULL, card_id=NULL, sepa_id=NULL, "
            "  mandate_id=NULL, next_billing_at=NULL, grace_until=NULL, "
            "  canceled_at=NULL, block_code=NULL, block_detail=NULL, "
            "  block_since=NULL, block_seen_at=NULL, updated_at=NOW()",
            (org_id, plan),
        )


def is_comp_subscription(org_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM org_subscriptions WHERE org_id=%s AND provider='comp'",
            (org_id,),
        ).fetchone()
    return row is not None


def delete_subscription(org_id: int) -> bool:
    """Retire complètement l'abonnement (retrait d'un comp admin). Pour un
    abonnement PAYÉ, préférer la résiliation à fin de période (mark_cancel…)."""
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM org_subscriptions WHERE org_id=%s", (org_id,)
        ).rowcount
    return n > 0


def mark_cancel_at_period_end(org_id: int) -> bool:
    """Résiliation à fin de période : stampe `canceled_at`, coupe la prochaine
    échéance. Le statut RESTE `active` (entitlement jusqu'à current_period_end) —
    la bascule finale est l'affaire du billing_runner."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET canceled_at = NOW(), "
            "next_billing_at = NULL, updated_at = NOW() "
            "WHERE org_id = %s AND status != 'canceled'",
            (org_id,),
        ).rowcount
    return n > 0


def resume_canceled(org_id: int) -> bool:
    """Annule une résiliation à fin de période. Rend False si rien n'était à reprendre.

    Symétrique exact de `mark_cancel_at_period_end` : on efface `canceled_at` et on
    **restaure** `next_billing_at = current_period_end` — la valeur que la résiliation
    avait mise à NULL. ⚠️ On RESTAURE, on ne recalcule pas : le runner pose toujours les
    deux au même instant (`schedule_next_billing(org_id, nxt, nxt)`), donc recalculer
    ouvrirait une occasion de décaler le cycle là où il n'y a rien à décider.

    ⚠️ **Les deux gardes sont dans le `WHERE`, pas au-dessus** : `status = 'active'`
    (une période déjà échue est passée à `canceled` par le sweep du runner — la
    reprendre ne serait pas une reprise, ce serait un réabonnement silencieux) et
    `canceled_at IS NOT NULL` (rien à annuler). Les mettre dans l'appelant laisserait
    une fenêtre entre la lecture et l'écriture, précisément sur l'objet où deux
    écrivains existent — l'utilisateur et le runner."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET canceled_at = NULL, "
            "next_billing_at = current_period_end, updated_at = NOW() "
            "WHERE org_id = %s AND status = 'active' AND canceled_at IS NOT NULL",
            (org_id,),
        ).rowcount
    return (n or 0) > 0


def schedule_next_billing(
    org_id: int, current_period_end: str, next_billing_at: str
) -> bool:
    """Avance le cycle après une échéance encaissée (retour à `active`)."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET current_period_end = %s, "
            "next_billing_at = %s, status = 'active', grace_until = NULL, "
            # Le blocage s'efface ICI et pas dans l'appelant : une échéance
            # encaissée EST la preuve qu'il n'y en a plus, quel qu'ait été le
            # motif. Effacer côté runner l'aurait laissé traîner sur tout chemin
            # futur qui fait avancer un cycle sans passer par lui.
            "block_code = NULL, block_detail = NULL, block_since = NULL, "
            "block_seen_at = NULL, "
            "updated_at = NOW() WHERE org_id = %s",
            (current_period_end, next_billing_at, org_id),
        ).rowcount
    return n > 0


def retry_billing_at(org_id: int, when) -> bool:
    """Décale la prochaine tentative d'échéance (retry J+3 du runner) sans
    toucher au reste du cycle."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET next_billing_at = %s, updated_at = NOW() "
            "WHERE org_id = %s", (when, org_id),
        ).rowcount
    return n > 0


def flag_subscription_block(org_id: int, code: str, detail: str, *, now) -> bool:
    """Grave POURQUOI l'échéance n'a pas pu être prélevée — l'état visible qui
    manquait (#829).

    ⚠️ **`block_since` ne bouge pas d'un tick à l'autre** (`COALESCE`), et c'est tout
    l'intérêt de la colonne : c'est la date à partir de laquelle on sert sans
    encaisser. La réécrire à chaque passage rendrait un blocage vieux d'un mois
    indiscernable d'un blocage né il y a une heure — soit exactement l'aveuglement
    qu'on répare. `block_seen_at`, lui, avance à chaque constat : il dit que le
    runner tourne encore et voit toujours le problème.

    Le motif change (identité réparée mais mandat perdu) → `block_since` repart, car
    ce n'est plus le même blocage.
    """
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET block_code = %s, block_detail = %s, "
            "block_since = CASE WHEN block_code IS DISTINCT FROM %s "
            "                   THEN %s ELSE COALESCE(block_since, %s) END, "
            "block_seen_at = %s, updated_at = NOW() WHERE org_id = %s",
            (code, detail, code, now, now, now, org_id),
        ).rowcount
    return n > 0


def clear_subscription_block(org_id: int) -> bool:
    """Lève le blocage sans toucher au cycle — pour un chemin qui répare la cause
    sans encaisser (une identité de facturation enfin posée, par exemple)."""
    with _connect() as conn:
        n = conn.execute(
            "UPDATE org_subscriptions SET block_code = NULL, block_detail = NULL, "
            "block_since = NULL, block_seen_at = NULL, updated_at = NOW() "
            "WHERE org_id = %s AND block_code IS NOT NULL", (org_id,),
        ).rowcount
    return n > 0


def blocked_subscriptions(limit: int = 200) -> list[dict]:
    """Les abonnements qu'on SERT SANS POUVOIR ENCAISSER, le plus ancien d'abord.

    La liste qui n'existait pas : sans elle, « combien de clients consomment
    gratuitement, et depuis quand ? » n'avait aucune réponse — ni en base, ni au
    journal (~24 h de rétention), ni ailleurs."""
    with _connect() as conn:
        return list(conn.execute(
            "SELECT org_id, plan, status, block_code, block_detail, block_since, "
            "       block_seen_at, next_billing_at, current_period_end "
            "FROM org_subscriptions WHERE block_code IS NOT NULL "
            "ORDER BY block_since ASC NULLS LAST LIMIT %s", (limit,)))


def count_renewal_attempts(org_id: int, since) -> int:
    """Tentatives de renouvellement déjà jouées pour la période courante
    (`since` = current_period_end) — pilote la politique de retry du runner."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM billing_payments "
            "WHERE org_id = %s AND kind = 'renewal' AND created_at >= %s",
            (org_id, since),
        ).fetchone()
    return int(row["n"])


def sweep_period_end_cancellations() -> list[int]:
    """Bascule `canceled` les abonnements résiliés dont la période est finie.
    Retourne les org_id basculées (log/notification)."""
    with _connect() as conn:
        return [r["org_id"] for r in conn.execute(
            "UPDATE org_subscriptions SET status = 'canceled', updated_at = NOW() "
            "WHERE canceled_at IS NOT NULL AND status != 'canceled' "
            "AND current_period_end <= NOW() RETURNING org_id"
        )]


def sweep_grace_expired() -> list[int]:
    """Bascule `canceled` les impayés dont la grace period est consommée —
    c'est LA fermeture d'entitlement du dunning (ADR 0043)."""
    with _connect() as conn:
        return [r["org_id"] for r in conn.execute(
            "UPDATE org_subscriptions SET status = 'canceled', updated_at = NOW() "
            "WHERE status = 'past_due' AND grace_until IS NOT NULL "
            "AND grace_until <= NOW() RETURNING org_id"
        )]


def due_subscriptions(limit: int = 50) -> list[dict]:
    """Échéances à tirer par le billing_runner (actives ou en retard, dues)."""
    with _connect() as conn:
        return list(conn.execute(
            "SELECT * FROM org_subscriptions "
            "WHERE status IN ('active', 'past_due') AND next_billing_at <= NOW() "
            "ORDER BY next_billing_at ASC LIMIT %s",
            (limit,),
        ))


def active_subscription_plans() -> dict[int, str]:
    """org_id → plan des abonnements OUVRANT l'entitlement (active + grace).

    `past_due` reste entitled tant que la grace court — la fermeture est un acte
    du runner (passage à `canceled`), jamais une lecture qui décide.
    """
    with _connect() as conn:
        return {
            r["org_id"]: r["plan"]
            for r in conn.execute(
                "SELECT org_id, plan FROM org_subscriptions "
                "WHERE status = 'active' "
                "   OR (status = 'past_due' AND grace_until > NOW())"
            )
        }


def subscription_plan_for_org(org_id: int) -> Optional[str]:
    """Plan ouvrant l'entitlement pour CETTE org (même règle que ci-dessus)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT plan FROM org_subscriptions WHERE org_id = %s AND ("
            "status = 'active' OR (status = 'past_due' AND grace_until > NOW()))",
            (org_id,),
        ).fetchone()
    return row["plan"] if row else None


# ── billing_payments (journal) ───────────────────────────────────────────────

def insert_billing_payment(
    org_id: int,
    kind: str,
    amount: int,
    *,
    currency: str = "eur",
    payment_intent_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    status: str = "processing",
    attempt: int = 1,
    customer_id: Optional[str] = None,
    tax: Optional[dict] = None,
) -> int:
    """Journalise une tentative. `amount` = ce qui part RÉELLEMENT au PSP, donc le
    TTC depuis #486 ; `tax` (la sortie de `billing_vat.tax_for`) fige la
    décomposition à côté. `tax=None` n'existe que pour les tests et les chemins
    historiques — une ligne sans décomposition est une ligne qu'on ne saura pas
    facturer."""
    t = tax or {}
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO billing_payments (org_id, kind, amount, currency, "
            "payment_intent_id, payment_id, status, attempt, customer_id, "
            "amount_ht, vat_rate_bps, vat_amount, country_code, vat_scheme) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (org_id, kind, amount, currency, payment_intent_id, payment_id,
             status, attempt, customer_id,
             t.get("amount_ht"), t.get("vat_rate_bps"), t.get("vat_amount"),
             t.get("country_code"), t.get("vat_scheme")),
        ).fetchone()
    return int(row["id"])


# ── billing_identities (l'identité de facturation, #486) ─────────────────────

def get_billing_identity(org_id: int) -> Optional[dict]:
    """L'identité de facturation de l'org, ou `None` — jamais un gabarit vide : une
    identité absente et une identité incomplète se traitent différemment (la seconde
    dit déjà quelque chose du client)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM billing_identities WHERE org_id = %s", (org_id,)
        ).fetchone()


def upsert_billing_identity(
    org_id: int,
    *,
    legal_name: str,
    country_code: str,
    vat_number: Optional[str] = None,
    address_line: Optional[str] = None,
    address_line2: Optional[str] = None,
    postal_code: Optional[str] = None,
    city: Optional[str] = None,
    billing_email: Optional[str] = None,
) -> None:
    """Pose ou REMPLACE l'identité (c'est un formulaire, pas un journal) — un champ
    omis est donc effacé, et l'appelant poste toujours l'identité entière. Ce qui a
    déjà été facturé n'en dépend pas : `billing_payments` a figé sa propre
    décomposition au moment du débit."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO billing_identities
                (org_id, legal_name, country_code, vat_number, address_line,
                 address_line2, postal_code, city, billing_email)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (org_id) DO UPDATE SET
                legal_name = EXCLUDED.legal_name,
                country_code = EXCLUDED.country_code,
                vat_number = EXCLUDED.vat_number,
                address_line = EXCLUDED.address_line,
                address_line2 = EXCLUDED.address_line2,
                postal_code = EXCLUDED.postal_code,
                city = EXCLUDED.city,
                billing_email = EXCLUDED.billing_email,
                updated_at = NOW()
            """,
            (org_id, legal_name, country_code, vat_number, address_line,
             address_line2, postal_code, city, billing_email),
        )


def last_customer_id_for_org(org_id: int) -> Optional[str]:
    """Le dernier customer Mollie sur lequel on a fait payer cette org.

    UN customer par org, pour toujours (#493) : le miroir `org_subscriptions` ne le
    porte qu'APRÈS `confirm`, donc entre l'ouverture d'un checkout et sa conclusion
    il n'y a que le journal pour s'en souvenir — et sans cette lecture, chaque
    tentative de souscription créait un customer de plus chez le PSP (dont les
    mandats survivent à la tentative abandonnée).
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT customer_id FROM billing_payments "
            "WHERE org_id = %s AND customer_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (org_id,),
        ).fetchone()
    return row["customer_id"] if row else None


def pending_initial_payment(org_id: int, *, since) -> Optional[dict]:
    """La souscription DÉJÀ EN VOL de l'org, s'il y en a une depuis `since`.

    « En vol » = tout premier paiement qui n'est pas un ÉCHEC DÉFINITIF : `open`
    (page de checkout encore payable) comme `paid` (encaissé, miroir pas encore
    posé — il manque le mandat). Les deux interdisent d'ouvrir un second checkout :
    c'est exactement l'enchaînement qui a débité deux fois le 25/08 (#493), un
    payeur qui voit un échec sur un paiement réussi et reclique.
    """
    failed = sorted(TERMINAL_PAYMENT_STATUSES - {"paid"})
    placeholders = ",".join(["%s"] * len(failed))
    with _connect() as conn:
        return conn.execute(
            f"SELECT * FROM billing_payments WHERE org_id = %s AND kind = 'initial' "
            f"AND status NOT IN ({placeholders}) AND created_at > %s "
            "ORDER BY created_at DESC LIMIT 1",
            (org_id, *failed, since),
        ).fetchone()


def paid_initials_awaiting_subscription(limit: int = 50, *, since) -> list[dict]:
    """Premiers paiements ENCAISSÉS dont l'abonnement n'est toujours pas ouvert.

    Depuis #493 un encaissement est journalisé `paid` dès qu'il est constaté — donc
    il quitte la file de réconciliation (`open_billing_payments`, qui ne regarde que
    le non-terminal) AVANT que le mandat n'existe. Sans cette seconde file, un payeur
    qui ferme son onglet pendant la course au mandat resterait débité et sans droits,
    et plus rien côté serveur ne reprendrait la main.
    """
    with _connect() as conn:
        return list(conn.execute(
            "SELECT p.* FROM billing_payments p "
            "WHERE p.kind = 'initial' AND p.status = 'paid' AND p.created_at > %s "
            "  AND NOT EXISTS (SELECT 1 FROM org_subscriptions s "
            "                  WHERE s.org_id = p.org_id AND s.status = 'active') "
            "ORDER BY p.created_at ASC LIMIT %s",
            (since, limit),
        ))


def update_billing_payment(
    payment_row_id: int,
    *,
    status: str,
    payment_id: Optional[str] = None,
) -> bool:
    with _connect() as conn:
        n = conn.execute(
            "UPDATE billing_payments SET status = %s, "
            "payment_id = COALESCE(%s, payment_id), updated_at = NOW() "
            "WHERE id = %s",
            (status, payment_id, payment_row_id),
        ).rowcount
    return n > 0


def list_billing_payments(org_id: int, limit: int = 20) -> list[dict]:
    with _connect() as conn:
        return list(conn.execute(
            "SELECT * FROM billing_payments WHERE org_id = %s "
            "ORDER BY created_at DESC LIMIT %s",
            (org_id, limit),
        ))


def get_billing_payment_by_ref(ref: str) -> Optional[dict]:
    """Retrouve une ligne de paiement par son id Mollie (`tr_…`) — que ce soit le
    premier paiement (`payment_intent_id`) ou un rejeu (`payment_id`). Sert le
    webhook Mollie : il ne porte que l'id, on remonte à l'org + la ligne."""
    with _connect() as conn:
        return conn.execute(
            "SELECT * FROM billing_payments "
            "WHERE payment_intent_id = %s OR payment_id = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (ref, ref),
        ).fetchone()


def open_billing_payments(limit: int = 100) -> list[dict]:
    """File de réconciliation : paiements non terminaux à re-poller (rattrape les
    fermetures de checkout post-paiement, les prélèvements SEPA qui se dénouent en
    plusieurs jours et les statuts en vol)."""
    placeholders = ",".join(["%s"] * len(TERMINAL_PAYMENT_STATUSES))
    with _connect() as conn:
        return list(conn.execute(
            f"SELECT * FROM billing_payments WHERE status NOT IN ({placeholders}) "
            "ORDER BY created_at ASC LIMIT %s",
            (*TERMINAL_PAYMENT_STATUSES, limit),
        ))
