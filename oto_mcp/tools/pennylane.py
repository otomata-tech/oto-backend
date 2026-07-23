"""Pennylane — comptabilité (lecture).

Clé résolue par appel via `access.resolve_api_key("pennylane")` : modèle
clé-per-user (comme Attio), pas de clé plateforme. Chaque utilisateur pose
sa propre clé Pennylane sur `manage.oto.cx/api-keys` — sa compta n'est
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
    def pennylane_list_suppliers(max_pages: Optional[int] = None) -> list:
        """Liste les fournisseurs (id + nom). Sert à retrouver le `supplier_id` d'un
        fournisseur EXISTANT à réutiliser dans pennylane_import_supplier_invoice (qui
        exige un supplier_id). Paginé ; max_pages limite le volume."""
        return _client().list_suppliers(max_pages=max_pages)

    @mcp.tool()
    def pennylane_create_supplier(name: str, fields: Optional[dict] = None) -> dict:
        """Crée un fournisseur — à faire avant de saisir la facture d'achat d'un
        NOUVEAU fournisseur (pennylane_import_supplier_invoice exige un supplier_id
        existant). Renvoie le fournisseur créé (avec son id).

        Args:
            name: raison sociale du fournisseur (obligatoire).
            fields: autres champs Pennylane optionnels (ex. {"vat_number": "FR…",
                "reg_no": "123456789", "emails": ["compta@exemple.fr"]}).
        """
        return _client().create_supplier(name, **(fields or {}))

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
    def pennylane_create_invoice(
        customer_id: int,
        date: str,
        deadline: str,
        lines: list,
        external_reference: Optional[str] = None,
        free_text: Optional[str] = None,
    ) -> dict:
        """Crée une facture de vente client en **brouillon** dans Pennylane.

        Toujours créée en brouillon : finaliser ensuite avec
        `pennylane_finalize_invoice` (puis `pennylane_send_invoice`) APRÈS validation
        humaine explicite. Le client doit exister (`pennylane_create_customer` pour
        le créer ; `pennylane_update_customer` pour l'e-mail/les coordonnées).

        ⚠️ Schéma de ligne STRICT (tout écart → 400 opaque `NotAnyOf`) — une ligne =
        UNE des 2 formes, aucun champ hors liste :
        - produit : `{product_id: int, quantity: number}` (le produit remplit le
          reste ; overrides possibles : label, raw_currency_unit_price, unit, vat_rate) ;
        - libre : `{label: str, quantity: number, unit: str,
          raw_currency_unit_price: str, vat_rate: str}` — TOUS requis.
        `vat_rate` = code Pennylane, jamais un pourcentage : 20 %→"FR_200",
        10 %→"FR_100", 5,5 %→"FR_55", 2,1 %→"FR_21", exonéré→"exempt".
        `raw_currency_unit_price` = prix unitaire HT en STRING ("700.00") ;
        `quantity` = number (jamais une string).

        Args:
            customer_id: ID du client Pennylane.
            date: date d'émission (YYYY-MM-DD).
            deadline: date d'échéance (YYYY-MM-DD).
            lines: lignes au schéma strict ci-dessus.
            external_reference: référence externe (anti-doublon / trace de la source).
            free_text: texte libre imprimé sur le PDF (commentaire visible client,
                champ API `pdf_invoice_free_text`).
        """
        return _client().create_customer_invoice(
            customer_id=customer_id, date=date, deadline=deadline, lines=lines,
            external_reference=external_reference, pdf_free_text=free_text,
            draft=True)

    @mcp.tool()
    def pennylane_create_credit_note(
        customer_id: int,
        date: str,
        deadline: str,
        lines: list,
        external_reference: Optional[str] = None,
        free_text: Optional[str] = None,
        credited_invoice_id: Optional[int] = None,
    ) -> dict:
        """Crée un avoir **standalone** en brouillon (convention v2 : montants négatifs).

        Fournis les lignes en **POSITIF** (le geste métier : 195 crédits à 1,45) —
        la négativation qui fait de la facture un AVOIR est appliquée côté client,
        jamais par toi. **Pas de facture liée par défaut** (la pratique réelle :
        la référence AUT-… vit en texte libre) ; si `credited_invoice_id` est
        fourni, le lien est posé APRÈS création via l'endpoint dédié
        `link_credit_note` (le champ create-time est cassé côté Pennylane).
        Toujours créé en brouillon : finaliser avec `pennylane_finalize_invoice`
        après validation humaine. Anti-doublon au préalable :
        `pennylane_find_invoice_by_reference` sur la référence externe.

        Args:
            customer_id: ID du client Pennylane (créé au préalable si besoin).
            date: date de l'avoir (YYYY-MM-DD).
            deadline: date d'échéance (YYYY-MM-DD — pratique MM : aujourd'hui).
            lines: lignes au schéma strict de `pennylane_create_invoice`, en
                POSITIF (2 formes : produit `{product_id, quantity}` — résoudre le
                product_id via `pennylane_products` — ou libre `{label, quantity,
                unit, raw_currency_unit_price, vat_rate}` tous requis ; vat_rate =
                code "FR_200"/"FR_100"/…, prix HT en string, quantity en number).
            external_reference: trace de la source (ex. id paiement GoCardless `PM…`) — anti-doublon.
            free_text: texte libre imprimé sur le PDF (commentaire visible client,
                champ API `pdf_invoice_free_text`) — c'est LÀ que vit le
                rapprochement lisible avec la facture d'origine (pratique MM :
                « Avoir sur facture AUT-XXXXX suite prélèvement échoué sur
                GoCardless »), l'avoir n'étant pas lié structurellement.
            credited_invoice_id: optionnel — ID d'une facture à créditer ; le lien
                est posé après création (2ᵉ appel), jamais au create.
        """
        note = _client().create_credit_note(
            customer_id=customer_id, date=date, deadline=deadline, lines=lines,
            external_reference=external_reference, pdf_free_text=free_text,
            draft=True)
        if credited_invoice_id:
            note_id = note.get("id") or (note.get("customer_invoice") or {}).get("id")
            if not note_id:
                return {"credit_note": note,
                        "link": "NON posé : id de l'avoir introuvable dans la réponse"}
            link = _client().link_credit_note(credited_invoice_id, note_id)
            return {"credit_note": note, "link": link}
        return note

    @mcp.tool()
    def pennylane_products(
        op: str = "list",
        product_id: Optional[int] = None,
        max_pages: Optional[int] = None,
    ) -> dict | list:
        """Catalogue produits Pennylane. op="list" → tous les produits (id, label,
        prix, unité, vat_rate…) ; op="get" (`product_id`) → la fiche d'un produit.

        Sert à résoudre le `product_id` d'une ligne de facture ou d'avoir
        (`pennylane_create_invoice` / `pennylane_create_credit_note`) — ne jamais
        deviner un product_id : le lire ici. Attention aux libellés quasi
        homonymes (ex. deux produits « crédit … ») : choisir sur la fiche
        complète (prix, unité), pas sur le début du nom.

        Args:
            op: "list" (défaut) ou "get".
            product_id: requis pour op="get".
            max_pages: op="list" — limite de pagination.
        """
        if op == "list":
            return _client().list_products(max_pages=max_pages)
        if op == "get":
            if not product_id:
                raise ValueError("op='get' requiert product_id")
            return _client().get_product(product_id)
        raise ValueError("op doit être 'list' ou 'get'")

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
    def pennylane_list_customers(max_pages: Optional[int] = None) -> list:
        """Liste les clients (id + nom + coordonnées). Sert à résoudre un `customer_id`
        depuis un NOM (pennylane_customer_invoices ne renvoie que `customer.{id,url}`,
        sans nom ni filtre) — p. ex. retrouver la facture d'origine d'un client donné
        avant de créer un avoir. Paginé ; max_pages limite le volume."""
        return _client().list_customers(max_pages=max_pages)

    @mcp.tool()
    def pennylane_create_customer(
        name: str,
        address: str,
        postal_code: str,
        city: str,
        country_alpha2: str = "FR",
        emails: Optional[list] = None,
        external_reference: Optional[str] = None,
    ) -> dict:
        """Crée un client (entreprise) dans Pennylane.

        L'adresse de facturation complète est OBLIGATOIRE (API v2) — les 4 champs
        address/postal_code/city/country_alpha2. Compléter ensuite les champs
        additionnels (vat_number, reg_no, billing_iban…) via
        `pennylane_update_customer`. Renvoie le client créé avec son `id` (à passer
        en `customer_id` de `pennylane_create_invoice`).

        Args:
            name: raison sociale du client.
            address: adresse de facturation (rue).
            postal_code: code postal.
            city: ville.
            country_alpha2: pays ISO alpha-2 (défaut FR).
            emails: e-mails du client (destinataires des factures).
            external_reference: référence externe (anti-doublon / trace de la source).
        """
        return _client().create_customer(
            name=name, emails=emails, address=address, postal_code=postal_code,
            city=city, country_alpha2=country_alpha2,
            external_reference=external_reference)

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
