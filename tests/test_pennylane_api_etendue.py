"""Pennylane, l'API v2 élargie : chaque op neuve appelle la méthode du client qui porte
le nom de la fonction décrite (oto-core, `connectors/pennylane/connector.yaml`), avec
ses arguments.

Ce qui casse en silence côté agent : une op qui appelle la mauvaise route (un
détachement envoyé comme un rattachement, une facture fournisseur dérapprochée côté
client), un argument requis remplacé par un défaut, une liste servie en brut.
"""
import asyncio
from unittest.mock import MagicMock

import pytest
from oto_mcp.mcp_errors import McpError


@pytest.fixture
def client(monkeypatch):
    import oto.tools.pennylane as pkg

    inst = MagicMock()
    monkeypatch.setattr(pkg, "PennylaneClient", lambda **kw: inst)
    monkeypatch.setattr("oto_mcp.access.resolve_api_key", lambda *a, **k: ("k", False))
    return inst


def _outil(module: str, nom: str):
    import importlib

    from fastmcp import FastMCP

    m = FastMCP("t")
    importlib.import_module(f"oto_mcp.tools.{module}").register(m)
    return asyncio.run(m.get_tool(nom))


CAS = [
    # (module, outil, arguments, méthode du client, args positionnels, args nommés)
    ("pennylane_banque", "pennylane_bank", {"op": "account", "bank_account_id": 5},
     "get_bank_account", (5,), {}),
    ("pennylane_banque", "pennylane_bank", {"op": "establishments", "bank_ids": [9, 11]},
     "list_bank_establishments", (),
     {"filter": [{"field": "id", "operator": "in", "value": [9, 11]}], "max_pages": None}),
    ("pennylane_banque", "pennylane_bank", {"op": "transaction", "transaction_id": 7},
     "get_transaction", (7,), {}),
    ("pennylane_banque", "pennylane_bank",
     {"op": "transaction_categories", "transaction_id": 7},
     "list_transaction_categories", (7,), {"max_pages": None}),
    ("pennylane_banque", "pennylane_bank", {"op": "matched_invoices", "transaction_id": 7},
     "list_transaction_matched_invoices", (7,), {"max_pages": None}),
    ("pennylane_banque", "pennylane_bank",
     {"op": "create_transaction", "bank_account_id": 5, "label": "Chèque",
      "date": "2026-10-06", "amount": "250.00"},
     "create_transaction", (),
     {"bank_account_id": 5, "label": "Chèque", "date": "2026-10-06", "amount": "250.00",
      "fee": None}),
    ("pennylane_banque", "pennylane_bank",
     {"op": "update_transaction", "transaction_id": 7, "supplier_id": 40},
     "update_transaction", (7,), {"supplier_id": 40}),
    ("pennylane_banque", "pennylane_bank",
     {"op": "update_transaction", "transaction_id": 7, "detach": "customer"},
     "update_transaction", (7,), {"customer_id": None}),
    ("pennylane_banque", "pennylane_bank",
     {"op": "categorize_transaction", "transaction_id": 7,
      "categories": [{"id": 1, "weight": "1"}]},
     "categorize", ("transaction", 7, [{"id": 1, "weight": "1"}]), {}),
    ("pennylane", "pennylane_invoice", {"op": "get", "invoice_id": 50},
     "get_customer_invoice", (50,), {}),
    ("pennylane", "pennylane_invoice", {"op": "payments", "invoice_id": 50},
     "list_customer_invoice_payments", (50,), {"max_pages": None}),
    ("pennylane", "pennylane_invoice", {"op": "installments", "invoice_id": 50},
     "list_customer_invoice_installments", (50,), {"max_pages": None}),
    ("pennylane", "pennylane_invoice", {"op": "matched_transactions", "invoice_id": 50},
     "list_customer_invoice_matched_transactions", (50,), {"max_pages": None}),
    ("pennylane", "pennylane_invoice", {"op": "appendices", "invoice_id": 50},
     "list_customer_invoice_appendices", (50,), {"max_pages": None}),
    ("pennylane", "pennylane_invoice", {"op": "header_fields", "invoice_id": 50},
     "list_customer_invoice_custom_header_fields", (50,), {"max_pages": None}),
    ("pennylane", "pennylane_invoice", {"op": "mark_paid", "invoice_id": 50},
     "mark_customer_invoice_paid", (50,), {}),
    ("pennylane", "pennylane_invoice",
     {"op": "mark_installment_paid", "invoice_id": 50, "installment_id": 61},
     "mark_customer_invoice_installment_paid", (50, 61), {}),
    ("pennylane_achats", "pennylane_supplier_invoice", {"op": "payments", "invoice_id": 80},
     "list_supplier_invoice_payments", (80,), {"max_pages": None}),
    ("pennylane_achats", "pennylane_supplier_invoice",
     {"op": "matched_transactions", "invoice_id": 80},
     "list_supplier_invoice_matched_transactions", (80,), {"max_pages": None}),
    ("pennylane_achats", "pennylane_supplier_invoice",
     {"op": "payment_status", "invoice_id": 80, "payment_status": "paid"},
     "update_supplier_invoice_payment_status", (80, "paid"), {}),
    ("pennylane", "pennylane_invoice",
     {"op": "unmatch_transaction", "invoice_id": 50, "transaction_id": 7},
     "unmatch_customer_invoice_transaction", (50, 7), {}),
    ("pennylane_achats", "pennylane_supplier_invoice",
     {"op": "unmatch_transaction", "invoice_id": 80, "transaction_id": 7},
     "unmatch_supplier_invoice_transaction", (80, 7), {}),
    ("pennylane", "pennylane_customer",
     {"op": "delete_contact", "customer_id": 30, "contact_id": 91},
     "delete_customer_contact", (30, 91), {}),
    ("pennylane", "pennylane_ref", {"kind": "ledger_account", "ledger_account_id": 41},
     "get_ledger_account", (41,), {}),
    ("pennylane", "pennylane_ref", {"kind": "journal", "journal_id": 12},
     "get_journal", (12,), {}),
    ("pennylane", "pennylane_ref", {"kind": "category_group", "category_group_id": 71},
     "get_category_group", (71,), {}),
    ("pennylane", "pennylane_ref", {"kind": "category", "category_id": 7101},
     "get_category", (7101,), {}),
    ("pennylane", "pennylane_ref", {"kind": "numberings"},
     "list_numberings", (), {"max_pages": None}),
    ("pennylane", "pennylane_ref", {"kind": "features"}, "get_company_features", (), {}),
    ("pennylane", "pennylane_customer", {"op": "get", "customer_id": 30},
     "get_customer", (30,), {}),
    ("pennylane", "pennylane_customer", {"op": "contacts", "customer_id": 30},
     "list_customer_contacts", (30,), {"max_pages": None}),
    ("pennylane", "pennylane_customer", {"op": "contact", "customer_id": 30, "contact_id": 91},
     "get_customer_contact", (30, 91), {}),
    ("pennylane", "pennylane_customer",
     {"op": "create_individual", "first_name": "Jane", "last_name": "Doe",
      "address": "1 rue X", "postal_code": "75001", "city": "Paris",
      "emails": ["jane@example.com"], "fields": {"phone": "0600"}},
     "create_individual_customer",
     ("Jane", "Doe", {"address": "1 rue X", "postal_code": "75001", "city": "Paris",
                      "country_alpha2": "FR"}),
     {"emails": ["jane@example.com"], "phone": "0600"}),
    ("pennylane", "pennylane_customer",
     {"op": "update_individual", "customer_id": 31, "fields": {"notes": "x"}},
     "update_individual_customer", (31,), {"notes": "x"}),
    ("pennylane", "pennylane_customer",
     {"op": "create_contact", "customer_id": 30, "first_name": "J", "last_name": "S",
      "email": "j@example.com", "fields": {"role": "CFO"}},
     "create_customer_contact", (30, "J", "S", "j@example.com"), {"role": "CFO"}),
    ("pennylane", "pennylane_customer",
     {"op": "update_contact", "customer_id": 30, "contact_id": 91, "fields": {"role": ""}},
     "update_customer_contact", (30, 91), {"role": ""}),
    ("pennylane", "pennylane_supplier", {"op": "get", "supplier_id": 40},
     "get_supplier", (40,), {}),
    ("pennylane", "pennylane_supplier",
     {"op": "update", "supplier_id": 40, "fields": {"iban": "FR76"}},
     "update_supplier", (40,), {"iban": "FR76"}),
    ("pennylane_devis", "pennylane_quote", {"op": "appendices", "quote_id": 60},
     "list_quote_appendices", (60,), {"max_pages": None}),
    ("pennylane_ledger", "pennylane_ledger_entry",
     {"op": "search_lines", "clauses": [{"field": "ledger_account_id", "operator": "eq",
                                         "value": 41}]},
     "search_ledger_entry_lines", (),
     {"filter": [{"field": "ledger_account_id", "operator": "eq", "value": 41}],
      "max_pages": None}),
    ("pennylane_ledger", "pennylane_ledger_entry", {"op": "line", "line_id": 201},
     "get_ledger_entry_line", (201,), {}),
    ("pennylane_ledger", "pennylane_ledger_entry", {"op": "documents", "entry_id": 101},
     "list_ledger_entry_documents", (101,), {"max_pages": None}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "create_category_group", "label": "Projets", "kind": "profit_and_loss"},
     "create_category_group", ("Projets",), {"kind": "profit_and_loss"}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "update_category_group", "category_group_id": 71, "label": "Projets 2026"},
     "update_category_group", (71, "Projets 2026"), {"kind": None}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "create_category", "label": "Refonte", "category_group_id": 71},
     "create_category", ("Refonte", 71),
     {"direction": None, "analytical_code": None, "ledger_account_id": None}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "update_category", "category_id": 7101, "label": "Refonte 2026"},
     "update_category", (7101,),
     {"label": "Refonte 2026", "direction": None, "analytical_code": None,
      "ledger_account_id": None}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "create_journal", "code": "OD", "label": "Opérations diverses"},
     "create_journal", ("OD", "Opérations diverses"), {}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "create_ledger_account", "number": "6226", "label": "Honoraires"},
     "create_ledger_account", ("6226", "Honoraires"),
     {"vat_rate": None, "country_alpha2": None}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "update_ledger_account", "ledger_account_id": 41, "letterable": True},
     "update_ledger_account", (41,), {"label": None, "letterable": True}),
    ("pennylane_referentiel", "pennylane_ref_edit",
     {"op": "update_product", "product_id": 12, "fields": {"price_before_tax": "750.00"}},
     "update_product", (12,), {"price_before_tax": "750.00"}),
    ("pennylane_exports", "pennylane_export",
     {"op": "start", "kind": "fec", "period_start": "2025-01-01", "period_end": "2025-12-31"},
     "create_fec_export", ("2025-01-01", "2025-12-31"), {}),
    ("pennylane_exports", "pennylane_export",
     {"op": "start", "kind": "analytical_general_ledger", "period_start": "2025-01-01",
      "period_end": "2025-12-31", "mode": "in_column"},
     "create_analytical_general_ledger_export", ("2025-01-01", "2025-12-31"),
     {"mode": "in_column"}),
    ("pennylane_exports", "pennylane_export",
     {"op": "get", "kind": "general_ledger", "export_id": 125},
     "get_general_ledger_export", (125,), {}),
    ("pennylane_documents", "pennylane_records", {"kind": "billing_subscriptions"},
     "list_billing_subscriptions", (), {"filter": None, "max_pages": None}),
    ("pennylane_documents", "pennylane_records",
     {"kind": "sepa_mandates", "clauses": [{"field": "customer_id", "operator": "eq",
                                            "value": 30}]},
     "list_sepa_mandates", (),
     {"filter": [{"field": "customer_id", "operator": "eq", "value": 30}],
      "max_pages": None}),
    ("pennylane_documents", "pennylane_records",
     {"kind": "commercial_document_lines", "record_id": 90},
     "list_commercial_document_lines", (90,), {"max_pages": None}),
    ("pennylane_documents", "pennylane_records", {"kind": "sepa_mandate", "record_id": 3},
     "get_sepa_mandate", (3,), {}),
    ("pennylane_documents", "pennylane_records",
     {"kind": "changes", "resource": "transactions", "start_date": "2026-11-01T00:00:00Z"},
     "list_changes", ("transactions",),
     {"start_date": "2026-11-01T00:00:00Z", "limit": None, "cursor": None}),
]


@pytest.mark.parametrize("module, nom, arguments, methode, args, kwargs", CAS,
                         ids=[f"{c[1]}-{c[2].get('op') or c[2].get('kind')}" for c in CAS])
def test_l_op_appelle_la_fonction_decrite(client, module, nom, arguments, methode, args,
                                          kwargs):
    _outil(module, nom).fn(**arguments)
    getattr(client, methode).assert_called_once_with(*args, **kwargs)


def test_chaque_methode_appelee_existe_dans_le_client():
    from oto.tools.pennylane import PennylaneClient

    assert [c[3] for c in CAS if not hasattr(PennylaneClient, c[3])] == []


@pytest.mark.parametrize("arguments, motif", [
    ({"op": "update_transaction", "transaction_id": 7}, "exactly one"),
    ({"op": "update_transaction", "transaction_id": 7, "customer_id": 1, "supplier_id": 2},
     "exactly one"),
    ({"op": "update_transaction", "transaction_id": 7, "detach": "supplier",
      "supplier_id": 2}, "detach takes no"),
    ({"op": "account"}, "requires bank_account_id"),
])
def test_un_rattachement_ambigu_est_refuse_avant_l_appel(client, arguments, motif):
    with pytest.raises(McpError, match=motif):
        _outil("pennylane_banque", "pennylane_bank").fn(**arguments)
    assert client.update_transaction.call_count == 0


def test_le_mode_n_existe_que_pour_le_grand_livre_analytique(client):
    with pytest.raises(McpError, match="analytical_general_ledger"):
        _outil("pennylane_exports", "pennylane_export").fn(
            op="start", kind="fec", period_start="2025-01-01", period_end="2025-12-31",
            mode="in_line")
    assert client.create_fec_export.call_count == 0


def test_un_refus_local_du_client_devient_une_erreur_d_arguments(client):
    client.update_product.side_effect = ValueError("update_product: unknown field(s) ['price']")
    with pytest.raises(McpError, match="unknown field"):
        _outil("pennylane_referentiel", "pennylane_ref_edit").fn(
            op="update_product", product_id=12, fields={"price": 1})


def test_les_autres_enregistrements_ne_font_que_lire():
    outil = _outil("pennylane_documents", "pennylane_records")
    assert outil.annotations is not None and outil.annotations.readOnlyHint is True


def test_l_acteur_n_envoie_toujours_que_la_facture():
    """`pennylane_act` reprend l'envoi retiré de `pennylane_invoice` (lot des envois du
    21/10/2026) : une liste qui l'autorise n'autorise que cela. L'élargir ferait
    autoriser des envois que personne n'a vus."""
    outil = _outil("pennylane", "pennylane_act")
    op = outil.parameters["properties"]["op"]
    assert op.get("enum", [op.get("const")]) == ["send_invoice"]


def test_une_liste_d_enregistrements_retire_ses_parties_lourdes(client):
    ligne = {"id": 90, "document_type": "proforma", "invoice_lines": [{"x": 1}],
             "pdf_invoice_free_text": "long"}
    client.list_commercial_documents.return_value = [ligne]
    outil = _outil("pennylane_documents", "pennylane_records")
    vue = outil.fn(kind="commercial_documents")
    assert vue["items"] == [{"id": 90, "document_type": "proforma"}]
    assert "invoice_lines" in vue["projection"]["omitted"]
    assert outil.fn(kind="commercial_documents", full=True)["items"] == [ligne]
