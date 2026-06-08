"""Pennylane — comptabilité (lecture).

Clé résolue par appel via `access.resolve_api_key("pennylane")` : modèle
clé-per-user (comme Attio), pas de clé plateforme. Chaque utilisateur pose
sa propre clé Pennylane sur `app.oto.ninja/api-keys` — sa compta n'est
visible que par lui.

Surface en lecture + lettrage. Les opérations qui *créent une écriture*
(création facture/client, finalisation, upload) restent côté CLI
`oto pennylane` pour éviter qu'un agent les déclenche par erreur. Le
lettrage (`pennylane_match`) est exposé : c'est un lien de rapprochement
réversible, pas une écriture — nécessaire pour qu'un agent solde les
factures payées et évite les relances à tort.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.pennylane import PennylaneClient

    def _client() -> PennylaneClient:
        key, _is_platform = access.resolve_api_key("pennylane", "PENNYLANE_API_KEY")
        return PennylaneClient(api_key=key)

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
