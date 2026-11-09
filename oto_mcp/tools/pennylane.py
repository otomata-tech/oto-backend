"""Pennylane — accounting (read + supervised invoice/credit-note flow).

Key resolved per call via `access.resolve_api_key("pennylane")`: per-user
key model (like Attio), no platform key. Each user sets
their own Pennylane key on `manage.oto.cx/api-keys` — their accounting is
visible only to them.

**Consolidated surface (ADR 0047 applied to a connector)**: one tool per business
OBJECT, the verb as an `op` parameter — `pennylane_customer`, `pennylane_invoice`
(sales), `pennylane_supplier` (purchases; supplier invoices live
in `pennylane_achats.py`, `pennylane_supplier_invoice`), plus a single
`pennylane_ref` for read-only reference data (company, fiscal years,
chart of accounts, analytical axes and categories, invoice templates, products). The heterogeneous verbs that
merging would not factor out stay named: `pennylane_transactions`,
`pennylane_bank_accounts`,
`pennylane_trial_balance`, `pennylane_match`, `pennylane_upload_file`. Families of their
own live in sibling modules: the bank (`pennylane_bank`, `pennylane_banque.py`), reference
data written (`pennylane_ref_edit`, `pennylane_referentiel.py`), exports
(`pennylane_export`, `pennylane_exports.py`), other records read (`pennylane_records`,
`pennylane_documents.py`).

Committing writes are **draft-first**: creating an invoice or a
credit note produces a draft, and **finalizing/sending are separate gestures** that
the agent only calls after human validation (supervision model validated with
a customer). Sending a document to the customer reaches a third party: it is not an
`op` of `pennylane_invoice` but of `pennylane_act` (`op="send_invoice"`), the tool an MCP
client asks to confirm. Lettering (`pennylane_match`) remains exposed: a reversible
reconciliation link, not an entry.

**Purchasing** is covered to file a supplier invoice from a file
"on the oto side": `pennylane_upload_file` (posts a PDF designated by its oto source —
Drive/Gmail/URL) then `pennylane_supplier_invoice(op="import")` (draft,
fields supplied by the agent that read the PDF).
"""
from __future__ import annotations

import json
from typing import Literal, Optional

from fastmcp import FastMCP

from ..connectors import verify as connector_verify

from .. import file_source
from .lecture import ACTE
from .pennylane_socle import _analytique, _bad, _client, _ecrit, _need


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe — otomata-tech/oto#69. Covers `auth` + PERMISSIONS.

    `GET /api/external/v2/me`. What Pennylane's docs establish, quoted:

    - **authenticated** — "Authentication Required: OAuth2", and a documented 401
      "Access token is missing or invalid";
    - **no side effect** — a GET that "Returns the user and the company";
      the docs mention none;
    - **the cost** — "Cost/Billing/Quotas: No information provided". ⚠️ As with
      Folk, this is NOT a line saying "free": it is the absence of any credit
      counter in the documentation. A strong argument, not proof.

    **Authenticated ≠ usable** (class named on oto#69, with attio): this
    probe goes further than authentication, and that is deliberate. The response
    carries `scopes` — the exact list of the key's permissions. A Pennylane key
    can perfectly well authenticate and be able to do nothing: the model is one key
    per person or per team, each with its own scope, and Pennylane split
    its scopes (`journals:*`, `ledger_accounts:*`, `ledger_entries:*`…). Returning
    "connected" on a key with zero permissions would be the hollow verdict the probe
    exists to prevent — same lesson as the Zoho probe (auth OK, zero CRM scope)
    and Stripe's (key restricted to Balance:read only).

    Does NOT read a quota: Pennylane exposes none on this call.
    """
    from oto.tools.pennylane import PennylaneClient

    infos = PennylaneClient(api_key=fields["key"]).get_company_info()
    if not (infos or {}).get("company"):
        raise RuntimeError(
            "Pennylane answered without naming a company for this key — "
            f"unexpected response: {str(infos)[:200]}")
    scopes = infos.get("scopes")
    if isinstance(scopes, list) and not scopes:
        raise RuntimeError(
            "The key does authenticate (company "
            f"« {(infos.get('company') or {}).get('name') or '?'} » recognized) but carries "
            "NO permission: it will not be able to read or write anything. "
            "Regenerate it at Pennylane, ticking the desired scopes.")


def register(mcp: FastMCP) -> None:
    connector_verify.register("pennylane", _verify)
    # --- reference data (read-only) -----------------------------------------

    @mcp.tool()
    def pennylane_ref(kind: Literal["company", "fiscal_years", "ledger_accounts",
                                    "ledger_account", "journals", "journal",
                                    "category_groups", "category_group", "categories",
                                    "category", "invoice_templates", "products",
                                    "numberings", "features"],
                      product_id: Optional[int] = None,
                      category_group_id: Optional[int] = None,
                      category_id: Optional[int] = None,
                      journal_id: Optional[int] = None,
                      ledger_account_id: Optional[int] = None,
                      max_pages: Optional[int] = None) -> dict | list:
        """Pennylane reference data, READ-ONLY — what must be resolved BEFORE
        writing (product ids, invoice templates, accounts, categories).

        `kind`:
        - "company": information about the current account's company.
        - "fiscal_years": fiscal years.
        - "ledger_accounts": chart of accounts (general ledger accounts);
          "ledger_account" (`ledger_account_id`): ONE account.
        - "journals": journals (`{id, code, label, type}` — "HA" purchases, "VT"
          sales, "BQ" bank…). Prerequisite of a general-ledger entry, which
          requires a `journal_id`: these ids are SPECIFIC TO THE COMPANY, resolve them
          here rather than hard-coding them. Scope `journals:*`, distinct from that
          of entries — a key can read one without the other. "journal"
          (`journal_id`): ONE journal.
        - "category_groups": the ANALYTICAL axes `{id, label}` ("Projects",
          "Cost centres"…). Scope `categories:readonly`. "category_group"
          (`category_group_id`): ONE axis, with its kind and categories.
        - "categories": the analytical categories, ALL of them (every page) —
          `{id, label, category_group, analytical_code, ledger_account}`; with
          `category_group_id`, those of ONE axis only. The ids to pass to
          `op="categorize"` of `pennylane_invoice`, `pennylane_supplier_invoice`,
          `pennylane_ledger_entry` and `pennylane_bank`. "category" (`category_id`):
          ONE category. Creating or changing axes, categories, journals, accounts
          and products is `pennylane_ref_edit`.
        - "invoice_templates": invoicing templates. The template drives the PDF
          RENDERING — notably the "Avoir" (credit note) template (without payment/IBAN block), the only API
          way to get the credit-note rendering: pass its id as
          `customer_invoice_template_id` to `pennylane_invoice`. Required scope:
          customer_invoice_templates:readonly.
        - "products": product catalog (id, label, price, unit, vat_rate…).
          With `product_id` → the record of ONE product. Used to resolve the
          `product_id` of an invoice or credit-note line — never guess a
          product_id: read it here. Beware of near-homonymous labels (e.g.
          two "credit …" products): choose on the full record (price,
          unit), not on the start of the name.
        - "numberings": the document numbering per type (`kind`, `number_prefix`,
          `next_number`); a type never set up is absent — finalizing an invoice
          FAILS without the invoice numbering. Set up in the Pennylane app only.
        - "features": which gated features the company can use NOW
          (installments, custom_fields, products_and_services_catalog): read it
          before an action a feature gates, rather than reading a 403 as a scope.

        Args:
            kind: the desired reference data (see above).
            product_id: kind="products" — the record of a single product.
            category_group_id: kind="categories" — the categories of ONE axis;
                kind="category_group" — that axis.
            category_id / journal_id / ledger_account_id: kind="category" /
                "journal" / "ledger_account".
            max_pages: pagination limit (paginated kinds: products,
                invoice_templates, category_groups, categories, numberings).
        """
        c = _client()
        if kind == "company":
            return c.get_company_info()
        if kind == "fiscal_years":
            return c.get_fiscal_years()
        if kind == "ledger_accounts":
            return c.get_ledger_accounts()
        if kind == "ledger_account":
            return c.get_ledger_account(_need(ledger_account_id, "ledger_account_id", kind))
        if kind == "journal":
            return c.get_journal(_need(journal_id, "journal_id", kind))
        if kind == "category_group":
            return c.get_category_group(_need(category_group_id, "category_group_id", kind))
        if kind == "category":
            return c.get_category(_need(category_id, "category_id", kind))
        if kind == "numberings":
            return c.list_numberings(max_pages=max_pages)
        if kind == "features":
            return c.get_company_features()
        if kind == "journals":
            return c.get_journals(max_pages=max_pages)
        if kind == "category_groups":
            return c.list_category_groups(max_pages=max_pages)
        if kind == "categories":
            return c.get_categories(category_group_id, max_pages=max_pages)
        if kind == "invoice_templates":
            return c.list_invoice_templates(max_pages=max_pages)
        if kind == "products":
            if product_id is not None:
                return c.get_product(product_id)
            return c.list_products(max_pages=max_pages)
        raise _bad("kind must be 'company', 'fiscal_years', 'ledger_accounts', "
                   "'ledger_account', 'journals', 'journal', 'category_groups', "
                   "'category_group', 'categories', 'category', 'invoice_templates', "
                   "'products', 'numberings' or 'features'")

    # --- customers -----------------------------------------------------------

    @mcp.tool()
    def pennylane_customer(
        op: Literal["list", "find", "get", "create", "update", "create_individual",
                    "update_individual", "contacts", "contact", "create_contact",
                    "update_contact", "delete_contact"] = "list",
        customer_id: Optional[int] = None,
        contact_id: Optional[int] = None,
        external_reference: Optional[str] = None,
        name: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        address: Optional[str] = None,
        postal_code: Optional[str] = None,
        city: Optional[str] = None,
        country_alpha2: str = "FR",
        emails: Optional[list] = None,
        fields: Optional[dict] = None,
        max_pages: Optional[int] = None,
    ) -> dict | list:
        """Pennylane customers — companies and individuals — and their contacts.

        `op`:
        - "list": id + name + contact details. Used to resolve a `customer_id` from
          a NAME (`pennylane_invoice(op="list")` only returns `customer.{id,url}`,
          with no name or filter). Paginated (`max_pages`).
        - "find" (`external_reference`): native server-side filter, a single call.
          **Anti-duplicate**: an already created customer (e.g. back-office companyId as
          external_reference) makes `op="create"` fail with 422 "External reference
          has already been taken". Call BEFORE creating: if it exists,
          reuse its `id`. Returns the customer or `{"found": false}`.
        - "get" (`customer_id`): ONE customer, company or individual (the
          `customer.id` of an invoice) — name, emails, addresses, payment
          conditions, ledger account.
        - "create": the full billing address is MANDATORY (API v2) —
          `name`, `address`, `postal_code`, `city`, `country_alpha2`. Returns the
          created customer with its `id` (to pass as `customer_id` of
          `pennylane_invoice(op="create")`).
        - "update" (`customer_id`, `fields`): complete email, vat_number,
          reg_no, billing_iban, external_reference… Typical case: Pennylane "knows"
          an imported customer but with no email or identifier — complete before
          creating the credit note.
        - "create_individual": a PERSON, not a company — `first_name`,
          `last_name` and the full billing address (`address`, `postal_code`,
          `city`, `country_alpha2`) are required; `emails`, `external_reference`
          (look it up first with op="find"), and any other field in `fields`
          (phone, payment_conditions, billing_iban, billing_language, notes…).
        - "update_individual" (`customer_id`, `fields`): same fields, only those
          given change.
        - "contacts" (`customer_id`) / "contact" (`customer_id`, `contact_id`): the
          contacts of a customer. ⚠️ A contact is NOT an invoice recipient: invoices
          go to the customer's `emails` (op="update"), which do not follow the
          contacts.
        - "create_contact" (`customer_id`, `first_name`, `last_name`, `email`, and
          in `fields`: role, telephone_number, mobile_number).
        - "update_contact" (`customer_id`, `contact_id`, `fields`): only the fields
          given change; "" clears role or a phone number.
        - "delete_contact" (`customer_id`, `contact_id`): deletes a contact — no way
          back but recreating it: read it first (op="contact"). Its address stays
          among the invoice recipients if it is there.

        Args:
            op: "list" (default) | "find" | "get" | "create" | "update" |
                "create_individual" | "update_individual" | "contacts" | "contact" |
                "create_contact" | "update_contact" | "delete_contact".
            customer_id: required for every op but list / find / create /
                create_individual.
            contact_id: op="contact" / "update_contact" / "delete_contact".
            first_name / last_name: op="create_individual" / "create_contact".
            email: op="create_contact".
            external_reference: op="find" (search) or op="create" (source
                trace / anti-duplicate).
            name / address / postal_code / city / country_alpha2: op="create"
                (country_alpha2 = ISO alpha-2, default FR); the address also for
                op="create_individual".
            emails: op="create" / "create_individual" — invoice recipients.
            fields: op="update" / "update_individual" / "update_contact" — fields
                to update, e.g. {"emails": ["x@y.fr"], "external_reference":
                "MM-12345"}; op="create_individual" / "create_contact" — the
                optional fields.
            max_pages: op="list" — pagination limit.
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
                external_reference=external_reference), "customer creation")
        if op == "update":
            return _ecrit(lambda: c.update_customer(_need(customer_id, "customer_id", op),
                                            **(fields or {})),
                          "customer update")
        if op == "get":
            return c.get_customer(_need(customer_id, "customer_id", op))
        if op == "contacts":
            return c.list_customer_contacts(_need(customer_id, "customer_id", op),
                                            max_pages=max_pages)
        if op == "contact":
            return c.get_customer_contact(_need(customer_id, "customer_id", op),
                                          _need(contact_id, "contact_id", op))
        if op == "delete_contact":
            return _ecrit(lambda: c.delete_customer_contact(
                _need(customer_id, "customer_id", op), _need(contact_id, "contact_id", op)),
                "contact deletion")
        try:
            if op == "create_individual":
                adresse = {"address": _need(address, "address", op),
                           "postal_code": _need(postal_code, "postal_code", op),
                           "city": _need(city, "city", op), "country_alpha2": country_alpha2}
                extra = {k: v for k, v in (("emails", emails),
                                           ("external_reference", external_reference))
                         if v is not None}
                return _ecrit(lambda: c.create_individual_customer(
                    _need(first_name, "first_name", op), _need(last_name, "last_name", op),
                    adresse, **extra, **(fields or {})), "individual customer creation")
            if op == "update_individual":
                return _ecrit(lambda: c.update_individual_customer(
                    _need(customer_id, "customer_id", op), **_need(fields, "fields", op)),
                    "individual customer update")
            if op == "create_contact":
                return _ecrit(lambda: c.create_customer_contact(
                    _need(customer_id, "customer_id", op),
                    _need(first_name, "first_name", op), _need(last_name, "last_name", op),
                    _need(email, "email", op), **(fields or {})), "contact creation")
            if op == "update_contact":
                return _ecrit(lambda: c.update_customer_contact(
                    _need(customer_id, "customer_id", op), _need(contact_id, "contact_id", op),
                    **_need(fields, "fields", op)), "contact update")
        except ValueError as e:      # refused by the client before any call
            raise _bad(f"op='{op}': {e}") from e
        raise _bad("op must be 'list', 'find', 'get', 'create', 'update', "
                   "'create_individual', 'update_individual', 'contacts', 'contact', "
                   "'create_contact', 'update_contact' or 'delete_contact'")

    # --- SALES invoices + credit notes (draft-first, supervision) ------------

    @mcp.tool()
    def pennylane_invoice(
        op: Literal["list", "find", "get", "create", "credit_note", "update",
                    "finalize", "delete", "categories", "categorize", "payments",
                    "installments", "matched_transactions", "appendices",
                    "header_fields", "mark_paid", "mark_installment_paid",
                    "unmatch_transaction"] = "list",
        invoice_id: Optional[int] = None,
        installment_id: Optional[int] = None,
        transaction_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        date: Optional[str] = None,
        deadline: Optional[str] = None,
        lines: Optional[list] = None,
        external_reference: Optional[str] = None,
        free_text: Optional[str] = None,
        customer_invoice_template_id: Optional[int] = None,
        credited_invoice_id: Optional[int] = None,
        fields: Optional[dict] = None,
        categories: Optional[list] = None,
        max_pages: Optional[int] = None,
    ) -> dict | list:
        """SALES invoices (customer invoices) and CREDIT NOTES — the "revenue" side.

        `op`:
        - "list": the inventory of issued invoices, to be checked against receipts
          for a bank reconciliation (receipt ↔ customer invoice direction) or
          to spot unpaid invoices / the remaining balance due. ⚠️ Without `max_pages`, the WHOLE
          history comes back (may exceed the token limit) — start small
          then widen. Does NOT look for a specific invoice: see op="find".
        - "find" (`external_reference`): anti-duplicate before creating a credit note for
          a failed payment — if an invoice already carries this reference (e.g. the
          GoCardless id `PM…`), the credit note exists, do not recreate it. Search by server-side
          filter: EXHAUSTIVE (an invoice's age or archiving does not
          hide it) and reliable — an upstream outage raises instead of reading as
          "none". Returns the invoice or `{"found": false}`.
        - "get" (`invoice_id`): ONE invoice or credit note — status, `paid`,
          `remaining_amount_with_tax`, amounts, customer, lines, `public_file_url`.
        - "payments" / "installments" / "matched_transactions" / "appendices" /
          "header_fields" (`invoice_id`): the payments recorded on it, its
          installments by deadline (their `id`s for op="mark_installment_paid"),
          the bank transactions matched to it (undo one with
          op="unmatch_transaction"), its appended files, its custom header fields.
        - "unmatch_transaction" (`invoice_id`, `transaction_id`): undoes
          `pennylane_match` — the invoice may read unpaid or late again, and
          Pennylane may remind the customer. Restore it with `pennylane_match`.
        - "mark_paid" (`invoice_id`) / "mark_installment_paid" (`invoice_id`,
          `installment_id`): marks the invoice, or one installment, PAID — once
          every installment is paid, the invoice is. ⚠️ No transaction is matched
          and the API offers NO way back: when the money is on the bank account,
          match the transaction instead (`pennylane_match`). Installments need a
          plan that has them (`pennylane_ref(kind="features")`), else 403.
        - "create": sales invoice as a **draft**. The customer must exist
          (`pennylane_customer`).
        - "credit_note": **standalone** credit note as a draft (v2 convention:
          negative amounts). Provide the lines as **POSITIVE** (the business action:
          195 credits at 1.45) — the negation that makes it a CREDIT NOTE is applied
          server-side, never by you. **No linked invoice by default** (the
          reference lives in free text); if `credited_invoice_id` is provided, the
          link is set AFTER creation via the dedicated endpoint (the create-time
          field is broken on Pennylane's side).
        - "update" (`invoice_id`, `fields`): on a DRAFT (date, deadline,
          customer_invoice_template_id, pdf_invoice_free_text, external_reference…).
          On a FINALIZED document the API only allows label /
          transaction_reference / external_reference — do not use it to
          "correct" an issued document. Typical use: switch a draft credit note
          to the "Avoir" template before finalization.
        - "finalize" (`invoice_id`): gives the draft its definitive reference.
          ⚠️ Committing write — after explicit human validation ONLY.
        - Sending the finalized document to the customer is
          `pennylane_act(op="send_invoice", invoice_id=…)` — another tool.
        - "delete" (`invoice_id`): deletes a DRAFT — cleanup of an obsolete draft
          (replaced, never finalized) before it pollutes the view or is
          finalized by mistake. Pennylane refuses an already FINALIZED document (it must be
          cancelled by credit note instead) — the refusal is surfaced as is, never a
          silent fallback.
        - "categories" (`invoice_id`): the ANALYTICAL categories set on the invoice,
          each with its `weight`.
        - "categorize" (`invoice_id`, `categories`): REPLACES all the analytical
          categories of the invoice with `[{"id": …, "weight": "0.5"}]` — weight as a
          STRING between 0 and 1, the weights of ONE axis sum to 1, axes are
          independent; `[]` removes them all. Ids from `pennylane_ref(kind=
          "categories")`. A DRAFT cannot be categorized (finalize first). Reversible:
          re-send the list read with op="categories".

        ⚠️ Strict LINE schema for create/credit_note (any deviation → opaque 400
        `NotAnyOf`) — a line = ONE of 2 forms, no field outside the list:
        - product: `{product_id: int, quantity: number}` (the product fills the
          rest; possible overrides: label, raw_currency_unit_price, unit,
          vat_rate) — resolve the product_id via `pennylane_ref(kind="products")`;
        - free: `{label: str, quantity: number, unit: str,
          raw_currency_unit_price: str, vat_rate: str}` — ALL required.
        `vat_rate` = Pennylane code, never a percentage: 20 %→"FR_200",
        10 %→"FR_100", 5.5 %→"FR_55", 2.1 %→"FR_21", exempt→"exempt".
        `raw_currency_unit_price` = unit price excl. VAT as a STRING ("700.00");
        `quantity` = number (never a string).

        Args:
            op: "list" (default) | "find" | "get" | "create" | "credit_note" |
                "update" | "finalize" | "delete" | "categories" | "categorize" |
                "payments" | "installments" | "matched_transactions" | "appendices" |
                "header_fields" | "mark_paid" | "mark_installment_paid" |
                "unmatch_transaction".
            invoice_id: required for every op but list / find / create /
                credit_note.
            installment_id: op="mark_installment_paid" — from op="installments".
            transaction_id: op="unmatch_transaction" — from
                op="matched_transactions".
            customer_id: required for create / credit_note.
            date: issue date of the invoice / credit note (YYYY-MM-DD).
            deadline: due date (YYYY-MM-DD).
            lines: lines in the strict schema above (create / credit_note).
            external_reference: op="find" (search); create/credit_note (source
                trace, e.g. GoCardless payment id — anti-duplicate).
            free_text: free text printed on the PDF (API field
                `pdf_invoice_free_text`) — THAT is where the readable link
                to the original invoice of a credit note not structurally linked lives (e.g.
                "Credit note on invoice AUT-XXXXX following failed direct debit").
            customer_invoice_template_id: PDF rendering template
                (`pennylane_ref(kind="invoice_templates")`).
            credited_invoice_id: op="credit_note" — invoice to credit; the link is
                set after creation (2nd call), never at create.
            fields: op="update" — fields to update.
            categories: op="categorize" — `[{"id", "weight"}]`, replaces everything.
            max_pages: op="list" — bounds the pagination (⚠️ see above); also the
                other list ops.
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
                return _ecrit(lambda: c.create_customer_invoice(**kwargs), "invoice creation")
            note = _ecrit(lambda: c.create_credit_note(**kwargs), "credit note creation")
            if credited_invoice_id:
                note_id = note.get("id") or (note.get("customer_invoice") or {}).get("id")
                if not note_id:
                    return {"credit_note": note,
                            "link": "NOT set: credit note id not found in the response"}
                return {"credit_note": note,
                        "link": _ecrit(lambda: c.link_credit_note(credited_invoice_id, note_id),
                                       "linking the credit note to its invoice")}
            return note
        if op == "update":
            return _ecrit(lambda: c.update_invoice(_need(invoice_id, "invoice_id", op),
                                           **(fields or {})), "invoice update")
        if op == "finalize":
            return _ecrit(lambda: c.finalize_invoice(_need(invoice_id, "invoice_id", op)),
                          "invoice finalization")
        if op == "delete":
            return _ecrit(lambda: c.delete_invoice(_need(invoice_id, "invoice_id", op)),
                          "invoice deletion")
        if op in ("categories", "categorize"):
            return _analytique(c, op, "customer_invoice",
                               _need(invoice_id, "invoice_id", op), categories)
        listes = {"payments": c.list_customer_invoice_payments,
                  "installments": c.list_customer_invoice_installments,
                  "matched_transactions": c.list_customer_invoice_matched_transactions,
                  "appendices": c.list_customer_invoice_appendices,
                  "header_fields": c.list_customer_invoice_custom_header_fields}
        if op in listes:
            return listes[op](_need(invoice_id, "invoice_id", op), max_pages=max_pages)
        if op == "get":
            return c.get_customer_invoice(_need(invoice_id, "invoice_id", op))
        if op == "mark_paid":
            return _ecrit(lambda: c.mark_customer_invoice_paid(
                _need(invoice_id, "invoice_id", op)), "marking the invoice paid")
        if op == "mark_installment_paid":
            return _ecrit(lambda: c.mark_customer_invoice_installment_paid(
                _need(invoice_id, "invoice_id", op),
                _need(installment_id, "installment_id", op)),
                "marking the installment paid")
        if op == "unmatch_transaction":
            return _ecrit(lambda: c.unmatch_customer_invoice_transaction(
                _need(invoice_id, "invoice_id", op),
                _need(transaction_id, "transaction_id", op)),
                "removing the bank/invoice match")
        raise _bad("op must be 'list', 'find', 'get', 'create', 'credit_note', 'update', "
                   "'finalize', 'delete', 'categories', 'categorize', 'payments', "
                   "'installments', 'matched_transactions', 'appendices', "
                   "'header_fields', 'mark_paid', 'mark_installment_paid' or "
                   "'unmatch_transaction' — sending "
                   "is `pennylane_act`")

    @mcp.tool(annotations=ACTE)
    def pennylane_act(op: Literal["send_invoice"], invoice_id: int) -> dict:
        """⚠️ ACTS toward a third party from Pennylane: `send_invoice` emails a
        FINALIZED sales invoice or credit note to the customer (the customer's Pennylane
        email). External send — after explicit human validation ONLY. Creating,
        finalizing and reading invoices is `pennylane_invoice`.

        Args:
            op: "send_invoice".
            invoice_id: the finalized document.
        """
        if op != "send_invoice":
            raise _bad("op must be 'send_invoice'")
        c = _client()
        return _ecrit(lambda: c.send_invoice(_need(invoice_id, "invoice_id", op)),
                      "invoice sending")

    # --- purchases: suppliers + supplier invoices ----------------------------

    @mcp.tool()
    def pennylane_supplier(op: Literal["list", "get", "create", "update"] = "list",
                           supplier_id: Optional[int] = None,
                           name: Optional[str] = None,
                           fields: Optional[dict] = None,
                           max_pages: Optional[int] = None) -> dict | list:
        """Pennylane suppliers.

        `op`:
        - "get" (`supplier_id`): ONE supplier.
        - "update" (`supplier_id`, `fields`): name, reg_no (SIREN), establishment_no
          (SIRET), vat_number, postal_address, emails, iban, external_reference,
          supplier_payment_method, supplier_due_date_delay, supplier_due_date_rule;
          only the fields given change, null clears establishment_no and the
          payment settings.
        - "list": id + name. Used to find the `supplier_id` of an EXISTING
          supplier to reuse in `pennylane_supplier_invoice(op="import")` (which
          requires a supplier_id). Paginated.
        - "create": do this before entering the supplier invoice of a NEW
          supplier. Returns the created supplier (with its id).

        Args:
            op: "list" (default) | "get" | "create" | "update".
            supplier_id: op="get" / "update".
            name: op="create" — company name (mandatory).
            fields: op="create" / "update" — other Pennylane fields (e.g.
                {"vat_number": "FR…", "reg_no": "123456789",
                "emails": ["compta@exemple.fr"]}).
            max_pages: op="list" — pagination limit.
        """
        c = _client()
        if op == "list":
            return c.list_suppliers(max_pages=max_pages)
        if op == "create":
            return _ecrit(lambda: c.create_supplier(_need(name, "name", op), **(fields or {})),
                          "supplier creation")
        if op == "get":
            return c.get_supplier(_need(supplier_id, "supplier_id", op))
        if op == "update":
            try:
                return _ecrit(lambda: c.update_supplier(
                    _need(supplier_id, "supplier_id", op), **_need(fields, "fields", op)),
                    "supplier update")
            except (ValueError, TypeError) as e:   # unknown field, refused before the call
                raise _bad(f"op='update': {e}") from e
        raise _bad("op must be 'list', 'get', 'create' or 'update'")

    @mcp.tool()
    def pennylane_upload_file(source: dict, account: Optional[str] = None) -> dict:
        """Upload a file (PDF) to Pennylane from a file that lives "oto-side".

        The agent has no local disk: designate the file by a `source` reference
        that oto resolves to bytes server-side, then uploads to Pennylane.
        `source` (object, `kind` selects the origin):
        - Drive: `{"kind":"drive","file_id":"<id>"}` (id from drive_file op=list/metadata)
        - Gmail attachment: `{"kind":"gmail","message_id":"<id>","filename":"<name>"}`
        - URL: `{"kind":"url","url":"https://…"}` (e.g. a signed URL from
          drive_file op="download" / gmail op="attachment")
        - Project file: `{"kind":"project_file","project_id":<id>,"file_id":<id>}`
          (ids from oto_project_files op=list)
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
            "file upload")
        if not res.get("id"):
            raise _bad(f"Pennylane accepted the file upload but returned no "
                       f"`id` — nothing to attach to an invoice. Response: {str(res)[:300]}")
        return {"file_attachment_id": res["id"], "filename": rf.filename, "url": res.get("url")}

    # --- bank & lettering ----------------------------------------------------

    @mcp.tool()
    def pennylane_transactions(max_pages: Optional[int] = None,
                               period_start: Optional[str] = None,
                               period_end: Optional[str] = None,
                               only_outstanding: bool = False,
                               per_page: int = 100) -> list:
        """Bank transactions. ⚠️ Without a lever, the WHOLE history comes back
        (hundreds of transactions → exceeds the token limit): reduce
        the volume at the source with the optional filters.

        Args:
            max_pages: limits the number of pages fetched.
            period_start / period_end: date bounds YYYY-MM-DD (filtered on the
                Pennylane server side).
            only_outstanding: True → only unsettled transactions
                (outstanding_balance ≠ 0), e.g. for a bank reconciliation.
            per_page: page size (≤100) — refines the granularity of max_pages.
        """
        return _client().get_transactions(
            max_pages=max_pages, period_start=period_start, period_end=period_end,
            only_outstanding=only_outstanding, per_page=per_page)

    @mcp.tool()
    def pennylane_bank_accounts(bank_names: bool = True) -> dict:
        """The company's bank accounts with their CURRENT balance, read-only.

        One call = one Pennylane company (one key). For several companies, call
        once per key (`_instance`) and add up on your side.

        Returns `{"bank_accounts": [...]}`, each account:
        - `id`, `name`, `currency` (ISO code);
        - `balance`: the balance Pennylane holds for the account, a decimal STRING
          as Pennylane sends it (e.g. "12345.67") — parse it with a decimal type,
          not a float, before adding up;
        - `bank`: `{"id", "name"}` — the bank establishment;
        - `updated_at`: when Pennylane last changed the account record. Pennylane's
          API exposes NO bank-sync date: this is the closest timestamp, not a
          guarantee that the bank feed ran then.

        Not available through Pennylane's API: the IBAN (not exposed on the
        account), the sync date (see `updated_at`).

        An account Pennylane returns without a balance raises an error: a
        missing balance is never read as zero.

        Args:
            bank_names: True (default) names each bank, which needs the key's
                `bank_establishments:readonly` scope — without it the call
                fails and says so. False skips that read: `bank.name` is then
                null, by request, and only `bank_accounts:readonly` is needed.
        """
        from oto.tools.common.errors import UpstreamHTTPError

        c = _client()
        comptes = c.fetch_all_pages("bank_accounts")
        sans_solde = [a.get("id") for a in comptes if a.get("balance") in (None, "")]
        if sans_solde:
            raise _bad(
                "Pennylane returned bank account(s) WITHOUT a balance (ids "
                f"{sans_solde}): the balance cannot be read for them, and is not "
                "reported as zero. Check the account's bank feed in Pennylane.")
        ids = sorted({(a.get("bank_establishment") or {}).get("id")
                      for a in comptes} - {None})
        noms: dict = {}
        if bank_names and ids:
            try:
                etablissements = c.fetch_all_pages(
                    "bank_establishments",
                    {"filter": json.dumps([{"field": "id", "operator": "in",
                                            "value": ids}])})
            except UpstreamHTTPError as e:
                if e.status_code != 403:
                    raise
                raise _bad(
                    "Pennylane refused to name the banks (HTTP 403): the key lacks "
                    "the `bank_establishments:readonly` scope. Add it to the key "
                    "at Pennylane, or call again with bank_names=False to get the "
                    "balances without the bank names.") from e
            noms = {b.get("id"): b.get("name") for b in etablissements}
        return {"bank_accounts": [{
            "id": a.get("id"),
            "name": a.get("name"),
            "currency": a.get("currency"),
            "balance": a["balance"],
            "bank": {"id": (a.get("bank_establishment") or {}).get("id"),
                     "name": noms.get((a.get("bank_establishment") or {}).get("id"))},
            "updated_at": a.get("updated_at"),
        } for a in comptes]}

    @mcp.tool()
    def pennylane_trial_balance(start_date: str, end_date: str) -> list:
        """Trial balance over a period.

        Args:
            start_date: start of period (YYYY-MM-DD).
            end_date: end of period (YYYY-MM-DD).
        """
        return _client().get_trial_balance(start_date, end_date)

    @mcp.tool()
    def pennylane_match(
        invoice_id: int,
        transaction_id: int,
        invoice_type: str = "customer",
    ) -> dict:
        """Reconciles a bank transaction with an invoice.

        A reversible reconciliation link, not an accounting entry. To
        be used to settle a paid invoice whose incoming transfer
        is not lettered (otherwise Pennylane keeps it `late` and chases the
        customer wrongly).

        ⚠️ **Do not confuse with general-ledger lettering.** The word
        "lettering" covers two actions on two objects: here a bank
        transaction and an invoice; associating entry LINES with each other in the
        general ledger is `pennylane_ledger_lettering`. Picking the wrong tool does not
        produce an error, only an action performed in the wrong place.

        Args:
            invoice_id: invoice ID (customer or supplier).
            transaction_id: bank transaction ID.
            invoice_type: "customer" (sales) or "supplier" (purchases).
        """
        return _ecrit(lambda: _client().match_transaction(invoice_id, transaction_id, invoice_type),
                      "bank/invoice reconciliation")
