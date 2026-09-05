"""Finkare — recouvrement de créances piloté par IA (factures, débiteurs, relances).

Quatre verbes, un par ressource de l'API v1 : la facture à recouvrer, le débiteur,
l'encaissement, et le workflow de relance qui les relie. Clé résolue par appel
(membre → org), jamais de clé plateforme : une clé Finkare ouvre les créances d'UNE
entreprise, la mutualiser n'aurait aucun sens.

⚠️ **Les montants sont en CENTIMES**, partout, à l'entrée comme à la sortie. Un
`amountCents: 150000` est une facture de 1 500 €. C'est le piège le plus coûteux de
cette API : la même valeur lue en euros passe inaperçue et fausse toute relance.

⚠️ **La clé porte son environnement** : `fk_test_…` travaille sur la sandbox,
`fk_live_…` sur les vraies créances. Rien à choisir à l'appel — le client dérive
l'adresse de la clé posée. Une clé de test ne peut donc pas relancer un vrai débiteur.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET invoices` (déjà dans le client — `list_invoices`), Finkare n'exposant
    ni `/me` ni solde. Lecture sans effet de bord ; une liste VIDE (aucune
    créance) est un état normal, jamais un refus.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope —
    une clé Finkare porte le périmètre entier des créances d'UNE entreprise.
    """
    from oto.tools.finkare import FinkareClient

    FinkareClient(api_key=fields["key"]).list_invoices()


def register(mcp: FastMCP) -> None:
    from oto.tools.finkare import FinkareClient

    connector_verify.register("finkare", _verify)

    def _client() -> FinkareClient:
        key, _ = access.resolve_api_key("finkare")
        return FinkareClient(api_key=key)

    @mcp.tool()
    def finkare_invoice(
        op: Literal["list", "create", "bulk", "cancel"] = "list",
        invoice: Optional[dict] = None,
        invoices: Optional[list] = None,
        invoice_id: Optional[str] = None,
        status: Optional[str] = None,
        reason: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Les créances à recouvrer — les déposer, les lister, en arrêter une.

        `op` :
        - "list" : l'inventaire, filtrable par `status`, paginé (`limit` ≤ 100).
        - "create" (`invoice`) : UNE créance. Requis : `invoiceNumber`,
          `amountCents` (⚠️ **centimes**), `dueDate` (ISO 8601), et un objet
          `debtor` avec au moins `name` et `email` — `siret`, `address`, `city`,
          `postalCode` améliorent l'identification et les courriers.
        - "bulk" (`invoices`) : import par lot. L'API refuse au-delà de **100
          créances** ou 10 Mo par requête — découper en amont, pas après le refus.
        - "cancel" (`invoice_id`, `reason`) : **arrête le workflow de relance** de
          cette créance. Ce n'est pas une suppression : la facture reste, les
          relances cessent. `reason` part dans l'historique — le remplir, c'est ce
          qui rendra l'arrêt compréhensible dans trois mois.

        ⚠️ Les écritures portent une clé d'idempotence : un même appel rejoué après
        une coupure réseau ne crée pas une seconde facture.
        """
        c = _client()
        if op == "create":
            if not invoice:
                raise ValueError("op=create demande `invoice` (l'objet créance).")
            return c.create_invoice(invoice)
        if op == "bulk":
            if not invoices:
                raise ValueError("op=bulk demande `invoices` (la liste des créances).")
            return c.create_invoices_bulk(invoices)
        if op == "cancel":
            if not invoice_id:
                raise ValueError("op=cancel demande `invoice_id`.")
            return c.cancel_invoice(invoice_id, reason=reason)
        return c.list_invoices(status=status, page=page, limit=limit)

    @mcp.tool()
    def finkare_debtor(
        op: Literal["list", "get", "create", "update", "invoices", "score"] = "list",
        debtor_id: Optional[str] = None,
        debtor: Optional[dict] = None,
        search: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Les débiteurs — qui doit, quoi, et avec quel comportement de paiement.

        `op` :
        - "list" : recherche textuelle par `search`, paginée (`limit` ≤ 100).
        - "get" / "create" / "update" (`debtor_id`, `debtor`) : la fiche. `name` et
          `email` sont requis ; `siret` fait 14 chiffres ; `country` est un code
          ISO à deux lettres (FR par défaut côté serveur). `notes` reste **interne**
          — rien de ce qu'on y écrit ne part au débiteur.
        - "invoices" : toutes les créances de ce débiteur, d'un coup.
        - "score" : le score de comportement de paiement calculé par Finkare.
          ⚠️ C'est une lecture de LEUR modèle, pas un jugement d'oto : à citer comme
          tel si on le rend à quelqu'un.
        """
        c = _client()
        if op in ("get", "invoices", "score", "update") and not debtor_id:
            raise ValueError(f"op={op} demande `debtor_id`.")
        if op == "get":
            return c.get_debtor(debtor_id)
        if op == "create":
            if not debtor:
                raise ValueError("op=create demande `debtor` (la fiche).")
            return c.create_debtor(debtor)
        if op == "update":
            if not debtor:
                raise ValueError("op=update demande `debtor` (les champs à écrire).")
            return c.update_debtor(debtor_id, debtor)
        if op == "invoices":
            return c.debtor_invoices(debtor_id)
        if op == "score":
            return c.debtor_score(debtor_id)
        return c.list_debtors(search=search, page=page, limit=limit)

    @mcp.tool()
    def finkare_payment(
        op: Literal["list", "get", "by_invoice", "stats"] = "list",
        payment_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
        status: Optional[str] = None,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        period: Optional[str] = None,
        page: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Les encaissements — en LECTURE seule : Finkare les constate, oto ne les pose pas.

        `op` :
        - "list" : filtrable par `status` (pending|completed|failed|refunded), par
          fenêtre (`from_date` / `to_date`, ISO 8601) ou par `invoice_id`.
        - "get" (`payment_id`) : un encaissement précis.
        - "by_invoice" (`invoice_id`) : tout ce qui a été encaissé sur une créance —
          c'est ce qui dit s'il reste un solde, pas le statut de la facture.
        - "stats" (`period` = day|week|month|year) : les agrégats.
          ⚠️ Cet appel-là exige la portée `reports:read`, que les autres n'ont pas :
          une clé qui lit les paiements peut échouer ici, et ce n'est pas une panne.
        """
        c = _client()
        if op == "get":
            if not payment_id:
                raise ValueError("op=get demande `payment_id`.")
            return c.get_payment(payment_id)
        if op == "by_invoice":
            if not invoice_id:
                raise ValueError("op=by_invoice demande `invoice_id`.")
            return c.invoice_payments(invoice_id)
        if op == "stats":
            return c.payment_stats(period=period)
        return c.list_payments(status=status, from_date=from_date, to_date=to_date,
                               invoice_id=invoice_id, page=page, limit=limit)

    @mcp.tool()
    def finkare_workflow(
        op: Literal["status", "history", "next_action", "trigger", "stats"] = "status",
        invoice_id: Optional[str] = None,
        action: Optional[Literal["start", "pause", "resume", "cancel",
                                 "escalate"]] = None,
        reason: Optional[str] = None,
    ) -> dict:
        """La relance d'une créance : où elle en est, ce qui vient, et comment l'infléchir.

        `op` :
        - "status" / "history" / "next_action" (`invoice_id`) : l'état courant, ce
          qui a déjà été tenté, et la prochaine action prévue avec sa date.
        - "trigger" (`invoice_id`, `action`, `reason`) : **agit sur un vrai
          débiteur**. `start` lance la cascade, `pause` la suspend, `resume` la
          reprend, `cancel` l'arrête, `escalate` passe au cran supérieur.
        - "stats" : les agrégats de tous les workflows.

        ⚠️ **`trigger` produit des effets EXTERNES et irréversibles** — un courrier
        parti ne se rappelle pas, une escalade vue par le débiteur ne s'annule pas.
        À n'appeler que sur demande explicite, et `reason` mérite d'être rempli :
        c'est ce que quelqu'un lira pour comprendre pourquoi la relance s'est
        arrêtée là.
        """
        c = _client()
        if op == "stats":
            return c.workflow_stats()
        if not invoice_id:
            raise ValueError(f"op={op} demande `invoice_id`.")
        if op == "history":
            return c.workflow_history(invoice_id)
        if op == "next_action":
            return c.workflow_next_action(invoice_id)
        if op == "trigger":
            if not action:
                raise ValueError(
                    "op=trigger demande `action` : start|pause|resume|cancel|escalate.")
            return c.workflow_trigger(invoice_id, action, reason=reason)
        return c.workflow_status(invoice_id)
