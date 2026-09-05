"""Pennylane — comptabilité (lecture + flux facture/avoir supervisé).

Clé résolue par appel via `access.resolve_api_key("pennylane")` : modèle
clé-per-user (comme Attio), pas de clé plateforme. Chaque utilisateur pose
sa propre clé Pennylane sur `manage.oto.cx/api-keys` — sa compta n'est
visible que par lui.

**Surface consolidée (ADR 0047 appliqué à un connecteur)** : un tool par OBJET
métier, le verbe en paramètre `op` — `pennylane_customer`, `pennylane_invoice`
(ventes), `pennylane_supplier` / `pennylane_supplier_invoice` (achats), plus un
`pennylane_ref` unique pour les référentiels en lecture seule (société, exercices,
plan comptable, catégories, modèles de facture, produits). Restent nommés les
verbes hétérogènes que la fusion ne factoriserait pas : `pennylane_transactions`,
`pennylane_trial_balance`, `pennylane_match`, `pennylane_upload_file`.

Les écritures engageantes sont **brouillon-d'abord** : créer une facture ou un
avoir produit un draft, et **finaliser/envoyer sont des `op` distincts** que
l'agent n'appelle qu'après validation humaine (modèle de supervision validé avec
un client). Le lettrage (`pennylane_match`) reste exposé : lien de rapprochement
réversible, pas une écriture.

L'**achat** est couvert pour verser une facture fournisseur depuis un fichier
« côté oto » : `pennylane_upload_file` (poste un PDF désigné par sa source oto —
Drive/Gmail/URL) puis `pennylane_supplier_invoice(op="import")` (brouillon,
champs fournis par l'agent qui a lu le PDF).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify

from .. import file_source
from .pennylane_socle import _bad, _client, _ecrit, _need


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` + DROITS.

    `GET /api/external/v2/me`. Ce que la doc de Pennylane établit, cité :

    - **authentifié** — « Authentication Required: OAuth2 », et un 401 documenté
      « Access token is missing or invalid » ;
    - **sans effet de bord** — un GET qui « Returns the user and the company » ;
      la doc n'en mentionne aucun ;
    - **le coût** — « Cost/Billing/Quotas: No information provided ». ⚠️ Comme pour
      Folk, ce n'est PAS une ligne qui dit « gratuit » : c'est l'absence de tout
      compteur de crédits dans la documentation. Argument fort, pas preuve.

    **Authentifié ≠ utilisable** (classe nommée sur oto#69, avec attio) : cette
    sonde va plus loin que l'authentification, et c'est délibéré. La réponse
    porte `scopes` — la liste exacte des droits de la clé. Une clé Pennylane
    peut parfaitement authentifier et ne rien pouvoir faire : le modèle est une clé
    par personne ou par équipe, chacune avec son périmètre, et Pennylane a éclaté
    ses scopes (`journals:*`, `ledger_accounts:*`, `ledger_entries:*`…). Rendre
    « connecté » sur une clé à zéro droit serait le verdict creux que la sonde
    existe pour empêcher — même leçon que la sonde Zoho (auth OK, zéro scope CRM)
    et celle de Stripe (clé restreinte au seul Balance:read).

    Ne lit PAS de quota : Pennylane n'en expose pas sur cet appel.
    """
    from oto.tools.pennylane import PennylaneClient

    infos = PennylaneClient(api_key=fields["key"]).get_company_info()
    if not (infos or {}).get("company"):
        raise RuntimeError(
            "Pennylane a répondu sans désigner de société pour cette clé — "
            f"réponse inattendue : {str(infos)[:200]}")
    scopes = infos.get("scopes")
    if isinstance(scopes, list) and not scopes:
        raise RuntimeError(
            "La clé authentifie bien (société "
            f"« {(infos.get('company') or {}).get('name') or '?'} » reconnue) mais ne "
            "porte AUCUN droit : elle ne pourra lire ni écrire quoi que ce soit. "
            "Régénère-la chez Pennylane en cochant les périmètres voulus.")


def register(mcp: FastMCP) -> None:
    connector_verify.register("pennylane", _verify)
    # --- référentiels (lecture seule) ---------------------------------------

    @mcp.tool()
    def pennylane_ref(kind: Literal["company", "fiscal_years", "ledger_accounts",
                                    "journals", "categories", "invoice_templates",
                                    "products"],
                      product_id: Optional[int] = None,
                      max_pages: Optional[int] = None) -> dict | list:
        """Référentiels Pennylane en LECTURE SEULE — ce qu'il faut résoudre AVANT
        d'écrire (ids de produits, modèles de facture, comptes, catégories).

        `kind` :
        - "company" : informations de l'entreprise du compte courant.
        - "fiscal_years" : exercices fiscaux.
        - "ledger_accounts" : plan comptable (comptes du grand livre).
        - "journals" : journaux (`{id, code, label, type}` — « HA » achats, « VT »
          ventes, « BQ » banque…). Prérequis d'une écriture au grand livre, qui
          exige un `journal_id` : ces ids sont PROPRES À LA SOCIÉTÉ, les résoudre
          ici plutôt que les coder en dur. Scope `journals:*`, distinct de celui
          des écritures — une clé peut lire les unes sans les autres.
        - "categories" : catégories de dépenses.
        - "invoice_templates" : modèles de facturation. Le modèle pilote le RENDU
          PDF — notamment le modèle « Avoir » (sans bloc paiement/IBAN), seul moyen
          API d'obtenir le rendu avoir : passer son id en
          `customer_invoice_template_id` à `pennylane_invoice`. Scope requis :
          customer_invoice_templates:readonly.
        - "products" : catalogue produits (id, label, prix, unité, vat_rate…).
          Avec `product_id` → la fiche d'UN produit. Sert à résoudre le
          `product_id` d'une ligne de facture ou d'avoir — ne jamais deviner un
          product_id : le lire ici. Attention aux libellés quasi homonymes (ex.
          deux produits « crédit … ») : choisir sur la fiche complète (prix,
          unité), pas sur le début du nom.

        Args:
            kind: le référentiel voulu (voir ci-dessus).
            product_id: kind="products" — la fiche d'un seul produit.
            max_pages: limite de pagination (kinds paginés : products,
                invoice_templates).
        """
        c = _client()
        if kind == "company":
            return c.get_company_info()
        if kind == "fiscal_years":
            return c.get_fiscal_years()
        if kind == "ledger_accounts":
            return c.get_ledger_accounts()
        if kind == "journals":
            return c.get_journals(max_pages=max_pages)
        if kind == "categories":
            return c.get_categories()
        if kind == "invoice_templates":
            return c.list_invoice_templates(max_pages=max_pages)
        if kind == "products":
            if product_id is not None:
                return c.get_product(product_id)
            return c.list_products(max_pages=max_pages)
        raise _bad("kind doit être 'company', 'fiscal_years', 'ledger_accounts', "
                   "'journals', 'categories', 'invoice_templates' ou 'products'")

    # --- clients -------------------------------------------------------------

    @mcp.tool()
    def pennylane_customer(
        op: Literal["list", "find", "create", "update"] = "list",
        customer_id: Optional[int] = None,
        external_reference: Optional[str] = None,
        name: Optional[str] = None,
        address: Optional[str] = None,
        postal_code: Optional[str] = None,
        city: Optional[str] = None,
        country_alpha2: str = "FR",
        emails: Optional[list] = None,
        fields: Optional[dict] = None,
        max_pages: Optional[int] = None,
    ) -> dict | list:
        """Clients (entreprises) Pennylane — lister, retrouver, créer, compléter.

        `op` :
        - "list" : id + nom + coordonnées. Sert à résoudre un `customer_id` depuis
          un NOM (`pennylane_invoice(op="list")` ne renvoie que `customer.{id,url}`,
          sans nom ni filtre). Paginé (`max_pages`).
        - "find" (`external_reference`) : filtre serveur natif, un seul appel.
          **Anti-doublon** : un client déjà créé (ex. companyId back-office en
          external_reference) fait échouer `op="create"` en 422 « External reference
          has already been taken ». À appeler AVANT de créer : s'il existe,
          réutiliser son `id`. Renvoie le client ou `{"found": false}`.
        - "create" : l'adresse de facturation complète est OBLIGATOIRE (API v2) —
          `name`, `address`, `postal_code`, `city`, `country_alpha2`. Renvoie le
          client créé avec son `id` (à passer en `customer_id` de
          `pennylane_invoice(op="create")`).
        - "update" (`customer_id`, `fields`) : compléter email, vat_number,
          reg_no, billing_iban, external_reference… Cas type : Pennylane « connaît »
          un client importé mais sans email ni identifiant — compléter avant de
          créer l'avoir.

        Args:
            op: "list" (défaut) | "find" | "create" | "update".
            customer_id: requis pour op="update".
            external_reference: op="find" (recherche) ou op="create" (trace de la
                source / anti-doublon).
            name / address / postal_code / city / country_alpha2: op="create"
                (country_alpha2 = ISO alpha-2, défaut FR).
            emails: op="create" — destinataires des factures.
            fields: op="update" — champs à mettre à jour, ex.
                {"emails": ["x@y.fr"], "external_reference": "MM-12345"}.
            max_pages: op="list" — limite de pagination.
        """
        c = _client()
        if op == "list":
            return c.list_customers(max_pages=max_pages)
        if op == "find":
            found = c.find_customer_by_external_reference(
                _need(external_reference, "external_reference", op))
            return found if found else {"found": False}
        if op == "create":
            return _ecrit(lambda: c.create_customer(
                name=_need(name, "name", op), emails=emails,
                address=_need(address, "address", op),
                postal_code=_need(postal_code, "postal_code", op),
                city=_need(city, "city", op), country_alpha2=country_alpha2,
                external_reference=external_reference), "la création de client")
        if op == "update":
            return _ecrit(lambda: c.update_customer(_need(customer_id, "customer_id", op),
                                            **(fields or {})),
                          "la mise à jour de client")
        raise _bad("op doit être 'list', 'find', 'create' ou 'update'")

    # --- factures de VENTE + avoirs (brouillon-d'abord, supervision) ---------

    @mcp.tool()
    def pennylane_invoice(
        op: Literal["list", "find", "create", "credit_note", "update",
                    "finalize", "send", "delete"] = "list",
        invoice_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        date: Optional[str] = None,
        deadline: Optional[str] = None,
        lines: Optional[list] = None,
        external_reference: Optional[str] = None,
        free_text: Optional[str] = None,
        customer_invoice_template_id: Optional[int] = None,
        credited_invoice_id: Optional[int] = None,
        fields: Optional[dict] = None,
        max_pages: Optional[int] = None,
    ) -> dict | list:
        """Factures de VENTE (factures clients) et AVOIRS — le côté « recettes ».

        `op` :
        - "list" : l'inventaire des factures émises, à confronter aux encaissements
          pour un rapprochement bancaire (sens encaissement ↔ facture client) ou
          pour repérer les impayés / le reste à devoir. ⚠️ Sans `max_pages`, TOUT
          l'historique revient (peut dépasser la limite de tokens) — commencer petit
          puis élargir. Ne cherche PAS une facture précise : voir op="find".
        - "find" (`external_reference`) : anti-doublon avant de créer un avoir pour
          un paiement échoué — si une facture porte déjà cette référence (ex. l'id
          GoCardless `PM…`), l'avoir existe, ne pas le recréer. Recherche par filtre
          serveur : EXHAUSTIVE (l'ancienneté ou l'archivage d'une facture ne la
          cachent pas) et fiable — une panne amont lève au lieu de se lire
          « aucune ». Renvoie la facture ou `{"found": false}`.
        - "create" : facture de vente en **brouillon**. Le client doit exister
          (`pennylane_customer`).
        - "credit_note" : avoir **standalone** en brouillon (convention v2 :
          montants négatifs). Fournis les lignes en **POSITIF** (le geste métier :
          195 crédits à 1,45) — la négativation qui en fait un AVOIR est appliquée
          côté serveur, jamais par toi. **Pas de facture liée par défaut** (la
          référence vit en texte libre) ; si `credited_invoice_id` est fourni, le
          lien est posé APRÈS création via l'endpoint dédié (le champ create-time
          est cassé côté Pennylane).
        - "update" (`invoice_id`, `fields`) : sur un BROUILLON (date, deadline,
          customer_invoice_template_id, pdf_invoice_free_text, external_reference…).
          Sur un document FINALISÉ l'API ne permet plus que label /
          transaction_reference / external_reference — ne pas s'en servir pour
          « corriger » un document émis. Usage type : basculer un brouillon d'avoir
          sur le modèle « Avoir » avant finalisation.
        - "finalize" (`invoice_id`) : donne sa référence définitive au brouillon.
          ⚠️ Écriture engageante — après validation humaine explicite SEULEMENT.
        - "send" (`invoice_id`) : envoie le document finalisé au client par email
          (email Pennylane du client). ⚠️ Envoi externe — après validation humaine
          explicite SEULEMENT.
        - "delete" (`invoice_id`) : supprime un BROUILLON — ménage d'un brouillon
          obsolète (remplacé, jamais finalisé) avant qu'il ne pollue la vue ou soit
          finalisé par erreur. Pennylane refuse un document déjà FINALISÉ (il faut
          l'annuler par avoir à la place) — le refus remonte tel quel, jamais un
          fallback silencieux.

        ⚠️ Schéma de LIGNE strict pour create/credit_note (tout écart → 400 opaque
        `NotAnyOf`) — une ligne = UNE des 2 formes, aucun champ hors liste :
        - produit : `{product_id: int, quantity: number}` (le produit remplit le
          reste ; overrides possibles : label, raw_currency_unit_price, unit,
          vat_rate) — résoudre le product_id via `pennylane_ref(kind="products")` ;
        - libre : `{label: str, quantity: number, unit: str,
          raw_currency_unit_price: str, vat_rate: str}` — TOUS requis.
        `vat_rate` = code Pennylane, jamais un pourcentage : 20 %→"FR_200",
        10 %→"FR_100", 5,5 %→"FR_55", 2,1 %→"FR_21", exonéré→"exempt".
        `raw_currency_unit_price` = prix unitaire HT en STRING ("700.00") ;
        `quantity` = number (jamais une string).

        Args:
            op: "list" (défaut) | "find" | "create" | "credit_note" | "update" |
                "finalize" | "send" | "delete".
            invoice_id: requis pour update / finalize / send / delete.
            customer_id: requis pour create / credit_note.
            date: date d'émission / de l'avoir (YYYY-MM-DD).
            deadline: date d'échéance (YYYY-MM-DD).
            lines: lignes au schéma strict ci-dessus (create / credit_note).
            external_reference: op="find" (recherche) ; create/credit_note (trace de
                la source, ex. id paiement GoCardless — anti-doublon).
            free_text: texte libre imprimé sur le PDF (champ API
                `pdf_invoice_free_text`) — c'est LÀ que vit le rapprochement lisible
                avec la facture d'origine d'un avoir non lié structurellement (ex.
                « Avoir sur facture AUT-XXXXX suite prélèvement échoué »).
            customer_invoice_template_id: modèle de rendu PDF
                (`pennylane_ref(kind="invoice_templates")`).
            credited_invoice_id: op="credit_note" — facture à créditer ; le lien est
                posé après création (2ᵉ appel), jamais au create.
            fields: op="update" — champs à mettre à jour.
            max_pages: op="list" — borne la pagination (⚠️ voir ci-dessus).
        """
        c = _client()
        if op == "list":
            return c.get_customer_invoices(max_pages=max_pages)
        if op == "find":
            inv = c.find_invoice_by_external_reference(
                _need(external_reference, "external_reference", op))
            return inv if inv else {"found": False}
        if op in ("create", "credit_note"):
            kwargs = dict(
                customer_id=_need(customer_id, "customer_id", op),
                date=_need(date, "date", op),
                deadline=_need(deadline, "deadline", op),
                lines=_need(lines, "lines", op),
                external_reference=external_reference, pdf_free_text=free_text,
                customer_invoice_template_id=customer_invoice_template_id,
                draft=True)
            if op == "create":
                return _ecrit(lambda: c.create_customer_invoice(**kwargs), "la création de facture")
            note = _ecrit(lambda: c.create_credit_note(**kwargs), "la création d'avoir")
            if credited_invoice_id:
                note_id = note.get("id") or (note.get("customer_invoice") or {}).get("id")
                if not note_id:
                    return {"credit_note": note,
                            "link": "NON posé : id de l'avoir introuvable dans la réponse"}
                return {"credit_note": note,
                        "link": _ecrit(lambda: c.link_credit_note(credited_invoice_id, note_id),
                                       "le rattachement de l'avoir à sa facture")}
            return note
        if op == "update":
            return _ecrit(lambda: c.update_invoice(_need(invoice_id, "invoice_id", op),
                                           **(fields or {})), "la mise à jour de facture")
        if op == "finalize":
            return _ecrit(lambda: c.finalize_invoice(_need(invoice_id, "invoice_id", op)),
                          "la finalisation de facture")
        if op == "send":
            return _ecrit(lambda: c.send_invoice(_need(invoice_id, "invoice_id", op)),
                          "l'envoi de facture")
        if op == "delete":
            return _ecrit(lambda: c.delete_invoice(_need(invoice_id, "invoice_id", op)),
                          "la suppression de facture")
        raise _bad("op doit être 'list', 'find', 'create', 'credit_note', 'update', "
                   "'finalize', 'send' ou 'delete'")

    # --- achats : fournisseurs + factures d'achat ----------------------------

    @mcp.tool()
    def pennylane_supplier(op: Literal["list", "create"] = "list",
                           name: Optional[str] = None,
                           fields: Optional[dict] = None,
                           max_pages: Optional[int] = None) -> dict | list:
        """Fournisseurs Pennylane.

        `op` :
        - "list" : id + nom. Sert à retrouver le `supplier_id` d'un fournisseur
          EXISTANT à réutiliser dans `pennylane_supplier_invoice(op="import")` (qui
          exige un supplier_id). Paginé.
        - "create" : à faire avant de saisir la facture d'achat d'un NOUVEAU
          fournisseur. Renvoie le fournisseur créé (avec son id).

        Args:
            op: "list" (défaut) | "create".
            name: op="create" — raison sociale (obligatoire).
            fields: op="create" — autres champs Pennylane optionnels (ex.
                {"vat_number": "FR…", "reg_no": "123456789",
                "emails": ["compta@exemple.fr"]}).
            max_pages: op="list" — limite de pagination.
        """
        c = _client()
        if op == "list":
            return c.list_suppliers(max_pages=max_pages)
        if op == "create":
            return _ecrit(lambda: c.create_supplier(_need(name, "name", op), **(fields or {})),
                          "la création de fournisseur")
        raise _bad("op doit être 'list' ou 'create'")

    @mcp.tool()
    def pennylane_supplier_invoice(
        op: Literal["list", "import"] = "list",
        max_pages: Optional[int] = None,
        file_attachment_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        date: Optional[str] = None,
        deadline: Optional[str] = None,
        currency_amount_before_tax: Optional[str] = None,
        currency_amount: Optional[str] = None,
        currency_tax: Optional[str] = None,
        invoice_lines: Optional[list[dict]] = None,
        currency: str = "EUR",
        external_reference: Optional[str] = None,
        import_as_incomplete: bool = False,
        invoice_number: Optional[str] = None,
        label: Optional[str] = None,
    ) -> dict | list:
        """Factures d'ACHAT (factures fournisseurs reçues) — le côté « dépenses ».

        `op` :
        - "list" : l'inventaire des factures reçues, à confronter aux décaissements
          pour un rapprochement bancaire (sens dépense ↔ facture fournisseur) ou
          pour repérer ce qui reste à payer. ⚠️ Sans `max_pages`, TOUT l'historique
          d'achats revient (peut dépasser la limite de tokens) — commencer petit
          puis élargir. Ne liste pas les fournisseurs eux-mêmes
          (`pennylane_supplier`).
        - "import" : crée la facture d'achat depuis un PDF déjà posté. Flux en deux
          temps : `pennylane_upload_file(...)` → `file_attachment_id`, puis ceci.
          Pennylane ne fait PAS d'OCR — YOU (ayant lu le PDF) fournis les champs.
          Les montants sont des STRINGS. Crée un brouillon ; le rapprocher ensuite
          à une transaction bancaire avec
          `pennylane_match(invoice_id, transaction_id, invoice_type="supplier")`.

        Args:
            op: "list" (défaut) | "import".
            max_pages: op="list" — borne la pagination (⚠️ voir ci-dessus).
            file_attachment_id: op="import" — id renvoyé par `pennylane_upload_file`.
            supplier_id: op="import" — fournisseur (company_supplier) existant
                (`pennylane_supplier`).
            date / deadline: op="import" — dates ISO (facture / échéance de paiement).
            currency_amount_before_tax / currency_amount / currency_tax:
                op="import" — montants en STRING : HT / TTC / TVA, dans la devise.
            invoice_lines: op="import" — ≥1 ligne (label, montants… au schéma de
                ligne Pennylane).
            currency: défaut EUR. external_reference: clé d'idempotence / de trace.
            import_as_incomplete: marquer le brouillon incomplet si des données
                manquent.
            invoice_number / label: numéro fournisseur / libellé comptable.
        """
        c = _client()
        if op == "list":
            return c.get_supplier_invoices(max_pages=max_pages)
        if op == "import":
            return _ecrit(lambda: c.import_supplier_invoice(
                file_attachment_id=_need(file_attachment_id, "file_attachment_id", op),
                supplier_id=_need(supplier_id, "supplier_id", op),
                date=_need(date, "date", op), deadline=_need(deadline, "deadline", op),
                currency_amount_before_tax=_need(
                    currency_amount_before_tax, "currency_amount_before_tax", op),
                currency_amount=_need(currency_amount, "currency_amount", op),
                currency_tax=_need(currency_tax, "currency_tax", op),
                invoice_lines=_need(invoice_lines, "invoice_lines", op),
                currency=currency, external_reference=external_reference,
                import_as_incomplete=import_as_incomplete,
                invoice_number=invoice_number, label=label,
            ), "l'import de facture d'achat")
        raise _bad("op doit être 'list' ou 'import'")

    @mcp.tool()
    def pennylane_upload_file(source: dict, account: Optional[str] = None) -> dict:
        """Upload a file (PDF) to Pennylane from a file that lives "côté oto".

        The agent has no local disk: designate the file by a `source` reference
        that oto resolves to bytes server-side, then uploads to Pennylane.
        `source` (object, `kind` selects the origin):
        - Drive: `{"kind":"drive","file_id":"<id>"}` (id from drive_file op=list/metadata)
        - Gmail attachment: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
        - URL: `{"kind":"url","url":"https://…"}` (e.g. a signed URL from
          drive_file op="download" / gmail_message op="attachment")
        Optional `account` (email) targets a specific Google account for drive/gmail.

        Returns {file_attachment_id, filename, url}. Feed `file_attachment_id` to
        `pennylane_supplier_invoice(op="import")` to create the supplier invoice.
        """
        try:
            rf = file_source.resolve(source)
        except file_source.FileSourceError as e:
            raise _bad(str(e))
        res = _ecrit(lambda: 
            _client().upload_file_bytes(rf.data, rf.filename, rf.mime or "application/pdf"),
            "le dépôt de fichier")
        if not res.get("id"):
            raise _bad(f"Pennylane a accepté le dépôt de fichier mais n'a rendu aucun "
                       f"`id` — rien à rattacher à une facture. Réponse : {str(res)[:300]}")
        return {"file_attachment_id": res["id"], "filename": rf.filename, "url": res.get("url")}

    # --- banque & lettrage ---------------------------------------------------

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
    def pennylane_trial_balance(start_date: str, end_date: str) -> list:
        """Balance comptable sur une période.

        Args:
            start_date: début de période (YYYY-MM-DD).
            end_date: fin de période (YYYY-MM-DD).
        """
        return _client().get_trial_balance(start_date, end_date)

    @mcp.tool()
    def pennylane_match(
        invoice_id: int,
        transaction_id: int,
        invoice_type: str = "customer",
    ) -> dict:
        """Rapproche une transaction bancaire d'une facture.

        Lien de rapprochement réversible, pas une écriture comptable. À
        utiliser pour solder une facture payée dont le virement entrant
        n'est pas lettré (sinon Pennylane la garde `late` et relance le
        client à tort).

        ⚠️ **Ne pas confondre avec le lettrage du grand livre.** Le mot
        « lettrage » recouvre deux gestes sur deux objets : ici une transaction
        bancaire et une facture ; associer entre elles des LIGNES d'écriture au
        grand livre, c'est `pennylane_ledger_lettering`. Se tromper d'outil ne
        produit pas d'erreur, seulement un geste posé au mauvais endroit.

        Args:
            invoice_id: ID de la facture (client ou fournisseur).
            transaction_id: ID de la transaction bancaire.
            invoice_type: "customer" (ventes) ou "supplier" (achats).
        """
        return _ecrit(lambda: _client().match_transaction(invoice_id, transaction_id, invoice_type),
                      "le rapprochement banque/facture")
