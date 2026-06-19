"""Pennylane — comptabilité (lecture).

Clé résolue par appel via `access.resolve_api_key("pennylane")` : modèle
clé-per-user (comme Attio), pas de clé plateforme. Chaque utilisateur pose
sa propre clé Pennylane sur `app.oto.ninja/api-keys` — sa compta n'est
visible que par lui.

Surface en lecture + lettrage + **écriture du flux avoir** (POC Movinmotion,
doctrine org 35). Les écritures engageantes sont **brouillon-d'abord** : créer
un avoir produit un draft (supprimable), et **finaliser/envoyer sont des tools
séparés** que l'agent n'appelle qu'après validation humaine (modèle de
supervision Celeste). Le lettrage (`pennylane_match`) reste exposé : lien de
rapprochement réversible, pas une écriture.

Les autres mutations larges (création de facture standard, upload de PDF)
restent côté CLI `oto pennylane`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.pennylane import PennylaneClient

    def _client() -> PennylaneClient:
        key, _is_platform = access.resolve_api_key("pennylane", "PENNYLANE_API_KEY")
        return PennylaneClient(api_key=key, field_filter=access.resolve_field_filter("pennylane"))

    @mcp.tool()
    async def pennylane_company() -> dict:
        """Informations de l'entreprise du compte Pennylane courant."""
        return _client().get_company_info()

    @mcp.tool()
    async def pennylane_fiscal_years() -> list:
        """Liste des exercices fiscaux."""
        return _client().get_fiscal_years()

    @mcp.tool()
    async def pennylane_trial_balance(start_date: str, end_date: str) -> list:
        """Balance comptable sur une période.

        Args:
            start_date: début de période (YYYY-MM-DD).
            end_date: fin de période (YYYY-MM-DD).
        """
        return _client().get_trial_balance(start_date, end_date)

    @mcp.tool()
    async def pennylane_ledger_accounts() -> list:
        """Plan comptable (comptes du grand livre)."""
        return _client().get_ledger_accounts()

    @mcp.tool()
    async def pennylane_customer_invoices(max_pages: Optional[int] = None) -> list:
        """Factures clients (paginé ; max_pages limite le volume)."""
        return _client().get_customer_invoices(max_pages=max_pages)

    @mcp.tool()
    async def pennylane_supplier_invoices(max_pages: Optional[int] = None) -> list:
        """Factures fournisseurs (paginé ; max_pages limite le volume)."""
        return _client().get_supplier_invoices(max_pages=max_pages)

    @mcp.tool()
    async def pennylane_transactions(max_pages: Optional[int] = None) -> list:
        """Transactions bancaires (paginé ; max_pages limite le volume)."""
        return _client().get_transactions(max_pages=max_pages)

    @mcp.tool()
    async def pennylane_categories() -> list:
        """Catégories de dépenses."""
        return _client().get_categories()

    @mcp.tool()
    async def pennylane_match(
        invoice_id: int,
        transaction_id: int,
        invoice_type: str = "customer",
    ) -> dict:
        """Lettre (rapproche) une transaction bancaire avec une facture.

        Lien de rapprochement réversible, pas une écriture comptable. À
        utiliser pour solder une facture payée dont le virement entrant
        n'est pas lettré (sinon Pennylane la garde `late` et relance le
        client à tort).

        Args:
            invoice_id: ID de la facture (client ou fournisseur).
            transaction_id: ID de la transaction bancaire.
            invoice_type: "customer" (ventes) ou "supplier" (achats).
        """
        return _client().match_transaction(invoice_id, transaction_id, invoice_type)

    # --- écriture du flux avoir (brouillon-d'abord, supervision) -------------

    @mcp.tool()
    async def pennylane_find_invoice_by_reference(external_reference: str) -> dict:
        """Cherche une facture client par son `external_reference` (anti-doublon).

        À appeler AVANT de créer un avoir pour un paiement échoué : si une
        facture porte déjà cet `external_reference` (ex. l'id GoCardless `PM…`),
        l'avoir existe — ne pas le recréer. Renvoie la facture trouvée ou
        `{"found": false}`.

        Args:
            external_reference: référence externe recherchée (ex. id paiement GoCardless).
        """
        inv = _client().find_invoice_by_external_reference(external_reference)
        return inv if inv else {"found": False}

    @mcp.tool()
    async def pennylane_create_credit_note(
        customer_id: int,
        date: str,
        lines: list,
        credited_invoice_id: int,
        external_reference: Optional[str] = None,
    ) -> dict:
        """Crée un avoir (facture d'avoir) en **brouillon**, crédité sur une facture.

        L'avoir est lié à la facture d'origine via `credited_invoice_id`. Toujours
        créé en brouillon : finaliser ensuite avec `pennylane_finalize_invoice`
        après validation humaine. Vérifier l'anti-doublon avec
        `pennylane_find_invoice_by_reference` au préalable.

        Args:
            customer_id: ID du client Pennylane (complété au préalable si besoin).
            date: date de l'avoir (YYYY-MM-DD).
            lines: lignes [{product_id, quantity, label?, raw_currency_unit_price?, unit?, vat_rate?}].
            credited_invoice_id: ID de la facture d'origine créditée.
            external_reference: trace de la source (ex. id paiement GoCardless `PM…`) — anti-doublon.
        """
        return _client().create_credit_note(
            customer_id=customer_id, date=date, lines=lines,
            credited_invoice_id=credited_invoice_id,
            external_reference=external_reference, draft=True)

    @mcp.tool()
    async def pennylane_finalize_invoice(invoice_id: int) -> dict:
        """Finalise une facture/avoir en brouillon (lui donne sa référence définitive).

        ⚠️ Écriture engageante — n'appeler qu'après validation humaine explicite.

        Args:
            invoice_id: ID du brouillon à finaliser.
        """
        return _client().finalize_invoice(invoice_id)

    @mcp.tool()
    async def pennylane_send_invoice(invoice_id: int) -> dict:
        """Envoie une facture/avoir finalisé(e) au client par email (email Pennylane du client).

        ⚠️ Envoi externe — n'appeler qu'après validation humaine explicite. Le
        gabarit/corps précis (modèle « échec des prélèvements » + motif) reste à
        confirmer côté compte ; cette route déclenche l'envoi standard Pennylane.

        Args:
            invoice_id: ID de la facture finalisée à envoyer.
        """
        return _client().send_invoice(invoice_id)

    @mcp.tool()
    async def pennylane_update_customer(customer_id: int, fields: dict) -> dict:
        """Met à jour un client (compléter email, vat_number, external_reference…).

        Pour la réconciliation : Pennylane « connaît » un client exporté de MM
        mais sans email ni identifiant — compléter avant de créer l'avoir.

        Args:
            customer_id: ID du client Pennylane.
            fields: champs à mettre à jour (ex. {"emails": ["x@y.fr"], "external_reference": "MM-12345"}).
        """
        return _client().update_customer(customer_id, **fields)
