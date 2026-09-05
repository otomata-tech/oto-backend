"""Changer de moyen de paiement — la porte qui manquait (#845 ①).

**On perdait un abonné payant en silence.** Il ne part pas, il ne se plaint pas : sa
carte meurt, toutes les relances échouent, et le seul geste qui le sauverait n'existait
pas. La procédure d'impayé chiffrée dans les conditions de vente — trois tentatives puis
quatorze jours de grâce — courait sur quelqu'un qui n'avait **aucun moyen d'agir pendant
ce délai**.

Le `kind='method_change'` était déjà déclaré dans le DDL : une valeur d'opération passée,
sans rien derrière pour la déclencher. Ce module est ce qu'il y a derrière.

## La séquence, et pourquoi celle-là

Mollie n'offre **aucun portail de changement de carte**, et `POST /mandates` refuse les
cartes (« To create mandates for cards, your customers need to perform a first
payment »). La seule voie est donc le checkout hébergé d'un **premier paiement**.

Ce premier paiement est à **0,00 EUR** : la doc Mollie l'autorise pour carte et PayPal
(« you can create a payment with a zero amount. No money will then be debited … Once
completed there will be a customer mandate »), et la page Cards donne « Minimum
transaction amount EUR 0.00 ». **Aucun mouvement d'argent, donc aucun remboursement,
donc aucun avoir à émettre** — c'est ce qui a fait écarter le montant symbolique.

    nouveau first à 0,00  →  mandat au retour  →  bascule  →  révocation de l'ancien

⚠️ **La révocation vient APRÈS la bascule, et elle est best-effort.** Si elle échoue, le
prochain encaissement doit quand même prendre le nouveau mandat : un ancien mandat qui
traîne coûte infiniment moins cher qu'une bascule annulée parce que le ménage a raté. En
faire une condition du succès inverserait le risque.

⚠️ **L'ancienne carte reste active tant que le nouveau mandat n'est pas confirmé**, et la
réponse le dit à l'utilisateur. Sans cette phrase, quelqu'un qui abandonne le checkout à
mi-chemin croit s'être coupé lui-même.

## ⚠️ Ce que le banc de ce module NE prouve PAS

Il n'existe pas de clé Mollie de test ici (décision d'Alexis, 05/09/2026) : **le client
est simulé, et le banc est donc un CONTRAT, pas une preuve**. Il vérifie que notre code
fait la bonne séquence face à un prestataire qui se comporte comme sa documentation le
décrit. Il ne dit rien du comportement réel.

Deux choses que seul le réel tranchera, et qu'aucun banc d'ici ne verra :
- **un premier paiement à 0,00 apparaît-il dans les règlements Mollie** (et donc dans un
  rapprochement comptable) ?
- **qu'arrive-t-il au mandat quand un paiement est remboursé** ?

Le premier vrai changement de carte se fera sous l'œil d'Alexis, en production.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import billing, mollie_client
from .db import billing as db_billing

logger = logging.getLogger(__name__)

#: Le premier paiement d'une rotation ne débite rien. Voir l'en-tête : c'est ce qui
#: évite un remboursement, donc un avoir, à chaque changement de carte.
MONTANT_CENTIMES = 0

#: ⚠️ Carte SEULEMENT. Le zéro-montant n'est documenté que pour carte et PayPal ; un
#: prélèvement SEPA à 0,00 n'est pas une voie connue, et l'ouvrir « au cas où » ferait
#: promettre un chemin qu'on n'a pas mesuré.
METHODE = "creditcard"

#: Ce que l'utilisateur doit lire, parce que la moitié du geste se passe chez Mollie et
#: que l'autre moitié n'arrive qu'au webhook.
ATTENTE = ("Ton moyen de paiement actuel reste actif tant que le nouveau n'est pas "
           "confirmé — rien n'est coupé si tu abandonnes cette page.")


def start(org_id: int, return_url: str) -> dict:
    """Ouvre un changement de moyen : rend l'URL de la page de paiement hébergée.

    `return_url` vient de l'appelant (le dashboard sait où ramener la personne), comme
    pour la souscription — le backend ne connaît pas l'écran d'où part le geste.

    Accepté sur un abonnement `active` ET `past_due` — c'est justement quand la carte
    est morte qu'on vient ici, et refuser un `past_due` fermerait la porte à ceux pour
    qui elle existe."""
    row = db_billing.get_org_subscription(org_id)
    if not row:
        raise ValueError("not_subscribed: aucun abonnement sur cette org")
    if row["status"] == "canceled":
        raise ValueError(
            "already_ended: cet abonnement est clos — il n'y a pas de moyen de "
            "paiement à changer, souscris à nouveau")
    customer_id = row.get("customer_id")
    if not customer_id:
        # Sans client chez le prestataire, aucun mandat ne peut naître : le dire plutôt
        # que d'ouvrir une page qui ne mènera nulle part.
        raise ValueError(
            "no_customer: cet abonnement n'a pas de client de paiement rattaché — "
            "il a été ouvert autrement (plan offert), il n'y a pas de carte à changer")

    payment = mollie_client.create_first_payment(
        MONTANT_CENTIMES,
        customer_id=customer_id,
        redirect_url=return_url,
        description="Mise à jour du moyen de paiement",
        method=METHODE,
        metadata={"org_id": str(org_id), "kind": "method_change"},
        webhook_url=billing.webhook_url(),
    )
    db_billing.insert_billing_payment(
        org_id, "method_change", MONTANT_CENTIMES,
        currency=row.get("currency") or "eur",
        status=str(payment.get("status") or "open"),
        payment_intent_id=payment.get("id"), customer_id=customer_id)
    # La référence est recollée sur l'URL de retour (#493) : sans elle, le navigateur
    # revient sans dire QUEL paiement il vient de conclure, et la confirmation doit
    # deviner — or il peut y en avoir plusieurs ouverts (page rechargée, hésitation).
    if payment.get("id"):
        try:
            mollie_client.update_payment(
                payment["id"],
                redirect_url=billing._return_url_with_ref(return_url, payment["id"]))
        except Exception as e:  # noqa: SILENT — confort de retour, la confirmation marche sans
            # Sans la référence, la confirmation prend le changement le plus récent :
            # correct dans le cas courant, et le webhook, lui, la porte toujours.
            logger.warning("changement de moyen org %s : référence non recollée (%s)",
                           org_id, e)
    return {"checkout_url": mollie_client.checkout_url(payment),
            "payment_id": payment.get("id"), "notice": ATTENTE}


def confirm(org_id: int, payment_ref: Optional[str] = None) -> dict:
    """Constate le retour : bascule sur le nouveau mandat, puis révoque l'ancien.

    Idempotent : rejouer sur un changement déjà bouclé ne rebascule rien."""
    row = db_billing.get_org_subscription(org_id)
    if not row:
        raise ValueError("not_subscribed: aucun abonnement sur cette org")
    candidats = [p for p in db_billing.list_billing_payments(org_id, limit=200)
                 if p["kind"] == "method_change" and p.get("payment_intent_id")]
    if payment_ref:
        ligne = next((p for p in candidats
                      if p["payment_intent_id"] == payment_ref), None)
        if ligne is None:
            # On ne se rabat PAS sur un autre : ce serait basculer un mandat sur la foi
            # d'un retour qui concerne autre chose.
            raise ValueError(
                f"unknown_payment: {payment_ref} n'est pas un changement de moyen "
                "en cours pour cette org")
    elif candidats:
        ligne = candidats[0]
    else:
        raise ValueError("no_pending_change: aucun changement de moyen en cours")

    payment = mollie_client.get_payment(ligne["payment_intent_id"])
    pstatus = str(payment.get("status") or "")
    if pstatus in ("failed", "canceled", "expired"):
        db_billing.update_billing_payment(ligne["id"], status=pstatus)
        # ⚠️ L'ancien moyen est INTACT : le refus d'une autorisation à zéro (une carte
        # qui ne l'accepte pas) ne doit rien coûter à celui qui essayait de bien faire.
        return {"status": "failed", "payment_status": pstatus,
                "notice": "Ton moyen de paiement actuel n'a pas changé."}
    if pstatus != "paid":
        return {"status": "pending", "payment_status": pstatus, "notice": ATTENTE}

    db_billing.update_billing_payment(ligne["id"], status="paid",
                                      payment_id=payment.get("id"))
    mandat = mollie_client.valid_mandate(row["customer_id"])
    if not mandat:
        # ⚠️ Encaissé sans mandat encore visible n'est PAS un échec : chez Mollie le
        # mandat apparaît quelques minutes après. Même régime que la souscription
        # (`pending_mandate`), et l'ancien moyen tient pendant ce temps.
        return {"status": "pending_mandate", "payment_status": "paid", "notice": ATTENTE}

    nouveau = mandat.get("id")
    if nouveau and nouveau == row.get("mandate_id"):
        # Rejeu : le mandat courant est déjà celui-là. Ne rien révoquer — l'ancien
        # d'aujourd'hui EST le nouveau.
        return {"status": "already_current", "mandate_id": nouveau}

    ancien = db_billing.swap_mandate(
        org_id, mandate_id=nouveau,
        mandate_rum=mandat.get("mandateReference"),
        method=mollie_client.method_from_mollie(mandat.get("method")))

    revoque = False
    if ancien and ancien != nouveau:
        try:
            mollie_client.revoke_mandate(row["customer_id"], ancien)
            revoque = True
        except Exception as e:  # noqa: SILENT — ménage best-effort, cf. l'en-tête : la bascule est faite
            # La bascule EST faite ; l'encaissement suivant prendra le nouveau mandat.
            # Un ancien mandat qui traîne coûte moins cher qu'une bascule annulée.
            logger.warning("changement de moyen org %s : ancien mandat %s non révoqué "
                           "(%s) — la bascule tient", org_id, ancien, e)
    return {"status": "changed", "mandate_id": nouveau, "previous_mandate_id": ancien,
            "previous_revoked": revoque,
            "notice": "Ton nouveau moyen de paiement est actif."}
