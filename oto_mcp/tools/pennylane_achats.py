"""Pennylane PURCHASE invoices — import, read, correct, validate.

Module of the `pennylane` connector (see `Connector.modules` in the registry): same
key, same client, distinct domain — like the general ledger and quotes.

Cycle: `pennylane_upload_file` uploads the PDF, `op="import"` creates the invoice
(with `import_as_incomplete=True`, it arrives as `accounting_status =
validation_needed`), `op="update"` corrects it (label, dates, amounts, and the
`vat_rate` of its LINES), `op="validate"` moves it to `complete`.

**Validation is a COMMITTING accounting entry**: never implicit,
never inside an import, and the API does not expose the reverse action.
"""
from __future__ import annotations

from typing import Literal, Optional, Union

from fastmcp import FastMCP

from .pennylane_socle import _analytique, _bad, _client, _ecrit, _need

_CHAMPS_FACTURE = ("label", "date", "deadline", "invoice_number", "supplier_id",
                   "currency", "currency_amount_before_tax", "currency_amount",
                   "currency_tax", "external_reference")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_supplier_invoice(
        op: Literal["list", "get", "lines", "import", "update", "validate",
                    "categories", "categorize", "payments", "matched_transactions",
                    "payment_status", "unmatch_transaction"] = "list",
        invoice_id: Optional[int] = None,
        transaction_id: Optional[int] = None,
        max_pages: Optional[int] = None,
        file_attachment_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        date: Optional[str] = None,
        deadline: Optional[str] = None,
        currency_amount_before_tax: Optional[str] = None,
        currency_amount: Optional[str] = None,
        currency_tax: Optional[str] = None,
        invoice_lines: Optional[Union[list[dict], dict]] = None,
        currency: Optional[str] = None,
        external_reference: Optional[str] = None,
        import_as_incomplete: bool = False,
        invoice_number: Optional[str] = None,
        label: Optional[str] = None,
        categories: Optional[list] = None,
        payment_status: Optional[Literal["paid", "to_be_paid"]] = None,
    ) -> dict | list:
        """PURCHASE invoices (supplier invoices received) — the "expenses" side.

        `op`:
        - "list": the inventory of received invoices, to be checked against disbursements
          (bank reconciliation) or to spot what remains to be paid. ⚠️ Without
          `max_pages`, the WHOLE history comes back — start small.
        - "get" (`invoice_id`): the invoice, including `accounting_status` (draft |
          archived | entry | validation_needed | complete).
        - "lines" (`invoice_id`): its lines, with their `id` and their `vat_rate` —
          the `id`s to pass to `op="update"`.
        - "import": creates the invoice from an already posted PDF
          (`pennylane_upload_file` → `file_attachment_id`). Pennylane does NOT do
          OCR: YOU (having read the PDF) supply the fields, amounts as STRINGs.
          The excl.-VAT amount (`currency_amount_before_tax`) is required at INVOICE level and
          REFUSED inside a line: a line only carries `currency_amount` (incl. VAT)
          and `currency_tax`, plus its `vat_rate`. `import_as_incomplete=True` leaves it
          in `validation_needed`.
        - "update" (`invoice_id`): corrects an invoice not yet validated — the
          invoice fields provided (`label`, `date`, `deadline`,
          `invoice_number`, `supplier_id`, amounts, `external_reference`,
          `currency`) and the lines, via `invoice_lines` = an OBJECT
          `{"update": [{"id": …, "vat_rate": …}], "create": [...], "delete":
          [{"id": …}]}` (not a list here). The reference documents no
          refusal on an already `complete` invoice: if Pennylane refuses, the refusal
          is surfaced as is.
        - "validate" (`invoice_id`): moves the invoice to `complete` — the accounting
          entry is posted. ⚠️ COMMITTING action with no way back through the API: only
          call it on explicit user request, invoice by
          invoice, never right after an import. 422 refusal if the entry
          lines are not balanced.
        - "categories" (`invoice_id`): the ANALYTICAL categories set on the invoice,
          each with its `weight`.
        - "categorize" (`invoice_id`, `categories`): REPLACES all the analytical
          categories of the invoice with `[{"id": …, "weight": "0.5"}]` — weight as a
          STRING between 0 and 1, the weights of ONE axis sum to 1, axes are
          independent; `[]` removes them all. Ids from `pennylane_ref(kind=
          "categories")`. Reversible: re-send the list read with op="categories".
        - "payments" (`invoice_id`): the payments recorded on the invoice.
        - "matched_transactions" (`invoice_id`): the bank transactions matched to it
          (`pennylane_match`; undo one with op="unmatch_transaction").
        - "unmatch_transaction" (`invoice_id`, `transaction_id`): undoes
          `pennylane_match` — the invoice reads unpaid again. Restore it with
          `pennylane_match`.
        - "payment_status" (`invoice_id`, `payment_status`): sets it "paid" or
          "to_be_paid" — e.g. paid with the manager's own card, no transaction to
          match.

        A line's `vat_rate` = Pennylane code: 20 %→"FR_200", 10 %→"FR_100",
        5.5 %→"FR_55", exempt→"exempt". REVERSE-CHARGE codes of the v2
        reference: `intracom_21`, `intracom_55`, `intracom_85`, `intracom_100`,
        `extracom`, `crossborder`, `FR_85_construction`, `FR_100_construction`,
        `FR_200_construction`. The reference does not define them beyond their name
        and knows NO `intracom_200`: for an intra-EU acquisition at 20 %,
        ask the user for the right code (accounting question), do not guess.

        Several Pennylane instances in an org (personal and company): without
        `_instance`, the personal key answers first. For the company's
        purchases, pass `_instance="org:<id>:pennylane"`.

        Args:
            op: "list" (default) | "get" | "lines" | "import" | "update" | "validate"
                | "categories" | "categorize" | "payments" | "matched_transactions" |
                "payment_status" | "unmatch_transaction".
            invoice_id: required for every op but list / import.
            max_pages: list ops — bounds the pagination.
            file_attachment_id: op="import" — id returned by `pennylane_upload_file`.
            supplier_id: op="import" (required) / "update" — existing supplier
                (`pennylane_supplier`).
            date / deadline: op="import" (required) / "update" — ISO dates (invoice /
                due date).
            currency_amount_before_tax / currency_amount / currency_tax:
                op="import" (required) / "update" — excl. VAT / incl. VAT / VAT of the INVOICE, as
                STRING.
            invoice_lines: op="import" — LIST of ≥1 line (`currency_amount`,
                `currency_tax`, `vat_rate`, and `ledger_account_id` if needed);
                op="update" — OBJECT {create|update|delete}.
            currency: default EUR on import; on update, only to change it.
            external_reference: idempotency / trace key.
            import_as_incomplete: op="import" — leaves the invoice in
                `validation_needed`.
            invoice_number / label: supplier number / accounting label.
            categories: op="categorize" — `[{"id", "weight"}]`, replaces everything.
            payment_status: op="payment_status" — "paid" | "to_be_paid".
            transaction_id: op="unmatch_transaction" — from
                op="matched_transactions".
        """
        c = _client()
        if op == "list":
            return c.get_supplier_invoices(max_pages=max_pages)
        if op == "get":
            return c.get_supplier_invoice(_need(invoice_id, "invoice_id", op))
        if op == "lines":
            return c.get_supplier_invoice_lines(_need(invoice_id, "invoice_id", op),
                                                max_pages=max_pages)
        if op == "import":
            lignes = _need(invoice_lines, "invoice_lines", op)
            if not isinstance(lignes, list):
                raise _bad("op='import': invoice_lines is a LIST of lines")
            return _ecrit(lambda: c.import_supplier_invoice(
                file_attachment_id=_need(file_attachment_id, "file_attachment_id", op),
                supplier_id=_need(supplier_id, "supplier_id", op),
                date=_need(date, "date", op), deadline=_need(deadline, "deadline", op),
                currency_amount_before_tax=_need(
                    currency_amount_before_tax, "currency_amount_before_tax", op),
                currency_amount=_need(currency_amount, "currency_amount", op),
                currency_tax=_need(currency_tax, "currency_tax", op),
                invoice_lines=lignes, currency=currency or "EUR",
                external_reference=external_reference,
                import_as_incomplete=import_as_incomplete,
                invoice_number=invoice_number, label=label,
            ), "supplier invoice import")
        if op == "update":
            ident = _need(invoice_id, "invoice_id", op)
            valeurs = {"label": label, "date": date, "deadline": deadline,
                       "invoice_number": invoice_number, "supplier_id": supplier_id,
                       "currency": currency,
                       "currency_amount_before_tax": currency_amount_before_tax,
                       "currency_amount": currency_amount, "currency_tax": currency_tax,
                       "external_reference": external_reference}
            champs = {k: valeurs[k] for k in _CHAMPS_FACTURE if valeurs[k] is not None}
            if invoice_lines is not None and not isinstance(invoice_lines, dict):
                raise _bad("op='update': invoice_lines is an OBJECT "
                           "{create|update|delete: [...]}, not a list — to "
                           "change a line's vat_rate: {\"update\": "
                           "[{\"id\": <id from op='lines'>, \"vat_rate\": …}]}")
            try:
                return _ecrit(lambda: c.update_supplier_invoice(
                    ident, fields=champs, invoice_lines=invoice_lines),
                    "supplier invoice correction")
            except ValueError as e:
                raise _bad(f"op='update': {e}") from e
        if op == "validate":
            return _ecrit(lambda: c.validate_supplier_invoice_accounting(
                _need(invoice_id, "invoice_id", op)),
                "supplier invoice accounting validation")
        if op in ("categories", "categorize"):
            return _analytique(c, op, "supplier_invoice",
                               _need(invoice_id, "invoice_id", op), categories)
        if op == "payments":
            return c.list_supplier_invoice_payments(_need(invoice_id, "invoice_id", op),
                                                    max_pages=max_pages)
        if op == "matched_transactions":
            return c.list_supplier_invoice_matched_transactions(
                _need(invoice_id, "invoice_id", op), max_pages=max_pages)
        if op == "payment_status":
            return _ecrit(lambda: c.update_supplier_invoice_payment_status(
                _need(invoice_id, "invoice_id", op),
                _need(payment_status, "payment_status", op)),
                "supplier invoice payment status")
        if op == "unmatch_transaction":
            return _ecrit(lambda: c.unmatch_supplier_invoice_transaction(
                _need(invoice_id, "invoice_id", op),
                _need(transaction_id, "transaction_id", op)),
                "removing the bank/invoice match")
        raise _bad("op must be 'list', 'get', 'lines', 'import', 'update', 'validate', "
                   "'categories', 'categorize', 'payments', 'matched_transactions', "
                   "'payment_status' or 'unmatch_transaction'")
