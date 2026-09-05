"""La facture d'un encaissement (#488), contre un vrai PostgreSQL.

Pourquoi une base RÉELLE plutôt qu'un store simulé : trois des garanties du lot ne
vivent que dans le schéma, et un stub dirait oui à n'importe quoi.

1. **L'idempotence est une CONTRAINTE**, `UNIQUE (payment_row_id, kind)`. C'est elle
   qui empêche un webhook rejoué de produire deux factures — pas une lecture
   préalable, que deux appels simultanés franchiraient tous les deux.
2. **Le PDF est un `BYTEA`** : l'aller-retour d'octets et le fait qu'aucune LISTE ne
   les remonte (le row factory ne normalise que les dates ; des octets dans un dict
   servi en JSON feraient une 500) ne se vérifient que sur une vraie colonne.
3. **La règle (c)** — les deux encaissements du 25/08/2026, sans décomposition
   fiscale, ne sont jamais facturés automatiquement — est un prédicat SQL dans la
   file de reprise.

Le FOURNISSEUR, lui, est simulé (`_faux_pennylane`) : au niveau du CLIENT, pas des
fonctions du seam. Le rapprochement du client, l'ordre brouillon → contrôle →
finalisation, le choix du code de TVA et la traduction d'un refus en exception sont
donc réellement exercés. Le mailer aussi est simulé — rien ne sort sur le réseau.
"""
from __future__ import annotations

from _faux_pennylane import (FauxPennylane, _abonnement,  # noqa: F401
                            _identite, _org, _paiement, brancher, live)


def _appel(faux: FauxPennylane, nom: str):
    return next((c for c in faux.calls if c[0] == nom), None)


# ── le chemin heureux ────────────────────────────────────────────────────────

def test_un_encaissement_produit_une_facture_finalisee(live, monkeypatch):
    from oto_mcp import billing_invoices
    from oto_mcp.db import billing_invoices as db_invoices

    faux = brancher(monkeypatch)
    org = _org()
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org)

    inv = billing_invoices.ensure_invoice_for_payment(paiement, plan="standard")

    assert inv["status"] == "issued", inv.get("error_detail")
    assert inv["number"], "une facture émise porte le numéro DE PENNYLANE"
    assert (inv["amount_ht"], inv["vat_amount"], inv["amount_ttc"]) == (1900, 380, 2280)
    assert inv["vat_scheme"] == "fr_ttc"

    # La ligne dit le palier ET la période couverte — c'est ce que lit le client.
    _, _, code_tva, libelle, brouillon = _appel(faux, "create_invoice")
    assert code_tva == "FR_200"
    assert libelle.startswith("Abonnement Standard — période du ")
    assert brouillon is True, "brouillon d'abord : c'est la seule fenêtre de contrôle"
    assert _appel(faux, "finalize"), "et FINALISÉ — jamais servi en brouillon"

    # Le document est daté du jour de l'encaissement, et déjà payé : pas d'échéance.
    doc = faux.documents[inv["pennylane_invoice_id"]]
    assert doc["date"] == doc["deadline"] == str(paiement["updated_at"])[:10]

    # Le PDF est RANGÉ (l'URL Pennylane, elle, expire en 30 min).
    assert inv["has_pdf"] is True
    assert db_invoices.get_billing_invoice_pdf(inv["id"])["pdf"].startswith(b"%PDF")
    assert inv["pdf_url"].endswith(".pdf")

    # Et le contact de facturation a été prévenu, une fois.
    assert faux.emails and faux.emails[0][0] == "compta@acme.test"
    assert inv["emailed_at"] and inv["email_to"] == "compta@acme.test"


def test_le_client_pennylane_est_rapproche_sur_l_org(live, monkeypatch):
    """Deux paiements de la même org ⟹ UN seul client chez le fournisseur."""
    from oto_mcp import billing_invoices

    faux = brancher(monkeypatch)
    org = _org()
    _identite(org, vat_number=None)
    _abonnement(org)

    billing_invoices.ensure_invoice_for_payment(_paiement(org), plan="standard")
    billing_invoices.ensure_invoice_for_payment(_paiement(org, kind="renewal"),
                                                plan="standard")

    crees = [c for c in faux.calls if c[0] == "create_customer"]
    assert len(crees) == 1, "le second paiement RETROUVE le client, il n'en crée pas"
    assert crees[0][3] == f"oto-org-{org}", "la clé de rapprochement porte l'org"


def test_un_webhook_rejoue_ne_cree_quune_facture(live, monkeypatch):
    from oto_mcp import billing_invoices
    from oto_mcp.db import billing_invoices as db_invoices

    faux = brancher(monkeypatch)
    org = _org()
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org)

    premier = billing_invoices.ensure_invoice_for_payment(paiement, plan="standard")
    second = billing_invoices.ensure_invoice_for_payment(paiement, plan="standard")

    assert premier["id"] == second["id"] and second["status"] == "issued"
    assert len([c for c in faux.calls if c[0] == "create_invoice"]) == 1
    assert len(db_invoices.list_billing_invoices(org)) == 1
    assert len(faux.emails) == 1, "et l'e-mail ne part pas deux fois"


# ── les régimes de TVA portent leur mention ──────────────────────────────────

def test_autoliquidation_porte_sa_mention_et_son_code(live, monkeypatch):
    from oto_mcp import billing_invoices

    faux = brancher(monkeypatch)
    org = _org("ACME BE")
    _identite(org, country_code="BE", vat_number="BE0123456789",
              address_line="2 rue Neuve", postal_code="1000", city="Bruxelles")
    _abonnement(org)

    inv = billing_invoices.ensure_invoice_for_payment(
        _paiement(org, pays="BE", tva="BE0123456789"), plan="standard")

    assert inv["status"] == "issued" and inv["vat_scheme"] == "reverse_charge"
    assert (inv["amount_ht"], inv["vat_amount"], inv["amount_ttc"]) == (1900, 0, 1900)
    assert _appel(faux, "create_invoice")[2] == "crossborder"
    doc = faux.documents[inv["pennylane_invoice_id"]]
    assert "Autoliquidation" in doc["pdf_invoice_free_text"]
    assert "196" in doc["pdf_invoice_free_text"], "l'article de la directive est cité"
    # Le n° de TVA du client est posé sur sa fiche : sans lui, la mention ne vaut rien.
    assert faux.customers[inv["pennylane_customer_id"]]["vat_number"] == "BE0123456789"


def test_export_hors_union_porte_sa_mention(live, monkeypatch):
    from oto_mcp import billing_invoices

    faux = brancher(monkeypatch)
    org = _org("ACME US")
    _identite(org, country_code="US", address_line="1 Main St",
              postal_code="94107", city="San Francisco")
    _abonnement(org)

    inv = billing_invoices.ensure_invoice_for_payment(_paiement(org, pays="US"),
                                                      plan="standard")

    assert inv["vat_scheme"] == "export" and inv["amount_ttc"] == 1900
    assert _appel(faux, "create_invoice")[2] == "extracom"
    assert "259-1" in faux.documents[inv["pennylane_invoice_id"]]["pdf_invoice_free_text"]


# ── ce qui échoue le dit, et rien ne se perd ─────────────────────────────────

def test_sans_cle_plateforme_la_tentative_est_journalisee(live, monkeypatch):
    """Le cas nommé par #488 : pas de credential plateforme ⟹ `invoice_pending`.

    On ne branche PAS le faux fournisseur : c'est `pennylane.client()` lui-même qui
    doit refuser, faute de clé dans l'env du process."""
    from oto_mcp import billing_invoices
    from oto_mcp.db import billing_invoices as db_invoices

    monkeypatch.delenv(billing_invoices.PLATFORM_KEY_ENV, raising=False)
    org = _org()
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org)

    inv = billing_invoices.ensure_invoice_for_payment(paiement, plan="standard")

    assert inv["status"] == "pending", "l'encaissement garde une trace de facture"
    assert inv["error_code"] == "pennylane_unconfigured"
    assert billing_invoices.PLATFORM_KEY_ENV in inv["error_detail"], (
        "le refus NOMME la variable à poser — un code nu ferait chercher")
    assert inv["attempts"] == 1 and inv["number"] is None

    # Et la ligne reste dans la file de reprise : rien n'est abandonné.
    assert inv["id"] in {r["id"] for r in db_invoices.pending_billing_invoices()}


def test_un_refus_du_fournisseur_ne_passe_pas_pour_un_succes(live, monkeypatch):
    """Un refus du fournisseur doit devenir une cause NOMMÉE, pas un document sans
    identifiant. Depuis oto-core#77 le client lève ; le passage obligé de la
    facturation exécute l'appel sous sa garde et traduit — le double échoue de la
    même façon que le vrai, sans quoi cette épreuve ne prouverait rien."""
    from oto_mcp import billing_invoices

    faux = FauxPennylane()
    faux.refus = {"error": "422", "details": "External reference has already been taken",
                  "status_code": 422}
    brancher(monkeypatch, faux)
    org = _org()
    _identite(org)
    _abonnement(org)

    inv = billing_invoices.ensure_invoice_for_payment(_paiement(org), plan="standard")

    assert inv["status"] == "pending" and inv["error_code"] == "pennylane_error"
    assert "422" in inv["error_detail"]


def test_un_ecart_de_montant_refuse_la_finalisation(live, monkeypatch):
    """Le brouillon est la SEULE fenêtre : une facture finalisée ne se supprime plus."""
    from oto_mcp import billing_invoices

    faux = FauxPennylane()
    faux.ttc_faux = 19.00          # le fournisseur n'a pas appliqué la TVA
    brancher(monkeypatch, faux)
    org = _org()
    _identite(org)
    _abonnement(org)

    inv = billing_invoices.ensure_invoice_for_payment(_paiement(org), plan="standard")

    assert inv["status"] == "pending" and inv["error_code"] == "amount_mismatch"
    assert "2280" in inv["error_detail"]
    assert _appel(faux, "finalize") is None, "rien n'a été gravé"


def test_sans_identite_de_facturation_on_nemet_rien(live, monkeypatch):
    from oto_mcp import billing_invoices

    brancher(monkeypatch)
    org = _org("SANS IDENTITE")
    _abonnement(org)

    inv = billing_invoices.ensure_invoice_for_payment(_paiement(org), plan="standard")

    assert inv["status"] == "pending"
    assert inv["error_code"] == "billing_identity_required"
    assert "legal_name" in inv["error_detail"], "le refus nomme les champs manquants"


def test_les_deux_encaissements_davant_la_regle_ne_sont_pas_factures(live, monkeypatch):
    """Règle (c) de #488 : `amount_ht IS NULL` = ligne d'avant la TVA. Sans
    décomposition, aucune facture conforme n'est calculable — et on n'en invente
    pas une. Pas même une ligne en attente : elle sonnerait pour toujours."""
    from oto_mcp import billing_invoices
    from oto_mcp.db import billing_invoices as db_invoices

    brancher(monkeypatch)
    org = _org("ORG DU 25/08")
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org, sans_tva=True)

    assert billing_invoices.ensure_invoice_for_payment(paiement) is None
    assert db_invoices.list_billing_invoices(org) == []
    # Et la file de reprise ne le propose jamais.
    assert paiement["id"] not in {p["id"] for p
                                  in db_invoices.paid_payments_without_invoice(100)}


# ── le remboursement produit un avoir ────────────────────────────────────────

def test_un_remboursement_produit_un_avoir_lie(live, monkeypatch):
    from oto_mcp import billing_invoices

    faux = brancher(monkeypatch)
    org = _org()
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org)
    facture = billing_invoices.ensure_invoice_for_payment(paiement, plan="standard")

    avoir = billing_invoices.ensure_credit_note_for_refund(paiement, 2280)

    assert avoir["kind"] == "credit_note" and avoir["status"] == "issued"
    assert (avoir["amount_ht"], avoir["amount_ttc"]) == (-1900, -2280), (
        "un avoir porte des montants NÉGATIFS")
    assert avoir["credited_invoice_id"] == facture["pennylane_invoice_id"]
    assert ("link_credit_note", facture["pennylane_invoice_id"],
            avoir["pennylane_invoice_id"]) in faux.calls
    assert avoir["number"] and avoir["number"] != facture["number"]


def test_un_remboursement_partiel_ventile_au_prorata(live, monkeypatch):
    from oto_mcp import billing_invoices

    brancher(monkeypatch)
    org = _org()
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org)
    billing_invoices.ensure_invoice_for_payment(paiement, plan="standard")

    avoir = billing_invoices.ensure_credit_note_for_refund(paiement, 1140)  # la moitié

    assert avoir["amount_ttc"] == -1140
    assert avoir["amount_ht"] == -950 and avoir["vat_amount"] == -190
    assert avoir["amount_ht"] + avoir["vat_amount"] == avoir["amount_ttc"], (
        "la TVA est le RESTE, jamais recalculée au taux — sinon la somme dérive")


def test_lavoir_attend_que_la_facture_existe(live, monkeypatch):
    from oto_mcp import billing_invoices

    brancher(monkeypatch)
    org = _org()
    _identite(org)
    _abonnement(org)
    paiement = _paiement(org)

    avoir = billing_invoices.ensure_credit_note_for_refund(paiement, 2280)

    assert avoir["status"] == "pending" and avoir["error_code"] == "invoice_not_issued"
    # Le montant remboursé est GARDÉ : le webhook qui l'a vu ne repassera pas.
    assert avoir["amount_ttc"] == -2280
