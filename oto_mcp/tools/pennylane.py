"""Pennylane — comptabilité (lecture).

Clé résolue par appel via `access.resolve_api_key("pennylane")` : modèle
clé-per-user (comme Attio), pas de clé plateforme. Chaque utilisateur pose
sa propre clé Pennylane sur `app.oto.ninja/api-keys` — sa compta n'est
visible que par lui.

Surface en lecture + lettrage + **écriture du flux avoir** (POC chez un client,
doctrine d'org dédiée). Les écritures engageantes sont **brouillon-d'abord** : créer
un avoir produit un draft (supprimable), et **finaliser/envoyer sont des tools
séparés** que l'agent n'appelle qu'après validation humaine (modèle de
supervision validée avec un client). Le lettrage (`pennylane_match`) reste exposé : lien de
rapprochement réversible, pas une écriture.

L'**achat** est désormais couvert pour verser une facture fournisseur depuis un
fichier « côté oto » : `pennylane_upload_file` (poste un PDF désigné par sa source
oto — Drive/Gmail/URL) puis `pennylane_import_supplier_invoice` (brouillon, champs
fournis par l'agent qui a lu le PDF). La création de facture **client** standard
reste côté CLI `oto pennylane`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, file_source


def register(mcp: FastMCP) -> None:
    from oto.tools.pennylane import PennylaneClient

    def _client() -> PennylaneClient:
        key, _is_platform = access.resolve_api_key("pennylane")
        # Rédaction appliquée à la frontière des tools par `FieldRedactionMiddleware`
        # (policy de l'org active), plus au niveau client.
        return PennylaneClient(api_key=key)

    def _bad(msg: str) -> McpError:
        return McpError(ErrorData(code=INVALID_PARAMS, message=msg))

    @mcp.tool()
    def pennylane_upload_file(source: dict, account: Optional[str] = None) -> dict:
        """Upload a file (PDF) to Pennylane from a file that lives "côté oto".

        The agent has no local disk: designate the file by a `source` reference
        that oto resolves to bytes server-side, then uploads to Pennylane.
        `source` (object, `kind` selects the origin):
        - Drive: `{"kind":"drive","file_id":"<id>"}` (id from drive_list/metadata)
        - Gmail attachment: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
        - URL: `{"kind":"url","url":"https://…"}` (e.g. a signed URL from
          drive_download / gmail_get_attachment)
        Optional `account` (email) targets a specific Google account for drive/gmail.

        Returns {file_attachment_id, filename, url}. Feed `file_attachment_id` to
        `pennylane_import_supplier_invoice` to create the supplier invoice.
        """
        try:
            rf = file_source.resolve(source)
        except file_source.FileSourceError as e:
            raise _bad(str(e))
        res = _client().upload_file_bytes(rf.data, rf.filename, rf.mime or "application/pdf")
        if res.get("error") or not res.get("id"):
            raise _bad(f"Pennylane file upload failed: {res.get('details', res)}")
        return {"file_attachment_id": res["id"], "filename": rf.filename, "url": res.get("url")}

    @mcp.tool()
    def pennylane_import_supplier_invoice(
        file_attachment_id: int, supplier_id: int, date: str, deadline: str,
        currency_amount_before_tax: str, currency_amount: str, currency_tax: str,
        invoice_lines: list[dict], currency: str = "EUR",
        external_reference: Optional[str] = None, import_as_incomplete: bool = False,
        invoice_number: Optional[str] = None, label: Optional[str] = None,
    ) -> dict:
        """Create a SUPPLIER (purchase) invoice in Pennylane from an uploaded PDF.

        Two-step flow: first `pennylane_upload_file(...)` → `file_attachment_id`,
        then this. Pennylane does NOT OCR — YOU (having read the PDF) provide the
        fields. Amounts are STRINGS. Creates a draft; reconcile it to a bank
        transaction afterwards with
        `pennylane_match(invoice_id, transaction_id, invoice_type="supplier")`.

        Args:
            file_attachment_id: id returned by pennylane_upload_file.
            supplier_id: Pennylane supplier (company_supplier) id.
            date / deadline: ISO dates (invoice date / payment due date).
            currency_amount_before_tax / currency_amount / currency_tax: amounts as
                strings — HT / TTC / VAT, in the invoice currency.
            invoice_lines: ≥1 line (label, amounts… per Pennylane line schema).
            currency: default EUR. external_reference: your idempotency/trace key.
            import_as_incomplete: mark the draft incomplete if some data is missing.
            invoice_number / label: optional supplier number / accounting label.
        """
        res = _client().import_supplier_invoice(
            file_attachment_id=file_attachment_id, supplier_id=supplier_id,
            date=date, deadline=deadline,
            currency_amount_before_tax=currency_amount_before_tax,
            currency_amount=currency_amount, currency_tax=currency_tax,
            invoice_lines=invoice_lines, currency=currency,
            external_reference=external_reference, import_as_incomplete=import_as_incomplete,
            invoice_number=invoice_number, label=label,
        )
        if isinstance(res, dict) and res.get("error"):
            raise _bad(f"Pennylane supplier invoice import failed: {res.get('details', res)}")
        return res

    @mcp.tool()
    def pennylane_company() -> dict:
        """Informations de l'entreprise du compte Pennylane courant."""
        return _client().get_company_info()

    @mcp.tool()
    def pennylane_fiscal_years() -> list:
        """Liste des exercices fiscaux."""
        return _client().get_fiscal_years()

    @mcp.tool()
    def pennylane_trial_balance(start_date: str, end_date: str) -> list:
        """Balance comptable sur une période.

        Args:
            start_date: début de période (YYYY-MM-DD).
            end_date: fin de période (YYYY-MM-DD).
        """
        return _client().get_trial_balance(start_date, end_date)

    @mcp.tool()
    def pennylane_ledger_accounts() -> list:
        """Plan comptable (comptes du grand livre)."""
        return _client().get_ledger_accounts()

    @mcp.tool()
    def pennylane_customer_invoices(max_pages: Optional[int] = None) -> list:
        """Factures clients (paginé ; max_pages limite le volume)."""
        return _client().get_customer_invoices(max_pages=max_pages)

    @mcp.tool()
    def pennylane_supplier_invoices(max_pages: Optional[int] = None) -> list:
        """Factures fournisseurs (paginé ; max_pages limite le volume)."""
        return _client().get_supplier_invoices(max_pages=max_pages)

    @mcp.tool()
    def pennylane_transactions(max_pages: Optional[int] = None,
                               period_start: Optional[str] = None,
                               period_end: Optional[str] = None,
                               only_outstanding: bool = False,
                               per_page: int = 100) -> list:
        """Transactions bancaires. ⚠️ Sans levier, TOUT l'historique revient
        (des centaines de transactions → dépasse la limite de tokens) : réduire
        le volume à la source avec les filtres, optionnels.

        Args:
            max_pages: limite le nombre de pages ramenées.
            period_start / period_end: bornes de date YYYY-MM-DD (filtre côté
                serveur Pennylane).
            only_outstanding: True → seulement les transactions non soldées
                (outstanding_balance ≠ 0), ex. pour un rapprochement bancaire.
            per_page: taille de page (≤100) — affine la granularité de max_pages.
        """
        return _client().get_transactions(
            max_pages=max_pages, period_start=period_start, period_end=period_end,
            only_outstanding=only_outstanding, per_page=per_page)

    @mcp.tool()
    def pennylane_categories() -> list:
        """Catégories de dépenses."""
        return _client().get_categories()

    @mcp.tool()
    def pennylane_match(
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
    def pennylane_find_invoice_by_reference(external_reference: str) -> dict:
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
    def pennylane_create_credit_note(
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
    def pennylane_finalize_invoice(invoice_id: int) -> dict:
        """Finalise une facture/avoir en brouillon (lui donne sa référence définitive).

        ⚠️ Écriture engageante — n'appeler qu'après validation humaine explicite.

        Args:
            invoice_id: ID du brouillon à finaliser.
        """
        return _client().finalize_invoice(invoice_id)

    @mcp.tool()
    def pennylane_send_invoice(invoice_id: int) -> dict:
        """Envoie une facture/avoir finalisé(e) au client par email (email Pennylane du client).

        ⚠️ Envoi externe — n'appeler qu'après validation humaine explicite. Le
        gabarit/corps précis (modèle « échec des prélèvements » + motif) reste à
        confirmer côté compte ; cette route déclenche l'envoi standard Pennylane.

        Args:
            invoice_id: ID de la facture finalisée à envoyer.
        """
        return _client().send_invoice(invoice_id)

    @mcp.tool()
    def pennylane_update_customer(customer_id: int, fields: dict) -> dict:
        """Met à jour un client (compléter email, vat_number, external_reference…).

        Pour la réconciliation : Pennylane « connaît » un client exporté de MM
        mais sans email ni identifiant — compléter avant de créer l'avoir.

        Args:
            customer_id: ID du client Pennylane.
            fields: champs à mettre à jour (ex. {"emails": ["x@y.fr"], "external_reference": "MM-12345"}).
        """
        return _client().update_customer(customer_id, **fields)
