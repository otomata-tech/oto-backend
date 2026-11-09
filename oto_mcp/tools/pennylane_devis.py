"""Pennylane quotes — create a quote, read it, list it, its PDF, invoice it.

Third module of the `pennylane` connector (see `Connector.modules` in the
registry): same key, same client, distinct domain. A quote precedes the
invoice when the customer requires it (purchasing tool, purchase order); a pro forma
does not stand in for it.

**A quote has no draft.** Pennylane creates it with status `pending` (awaiting
acceptance); nothing goes to the customer until it is sent to them.
The status only moves through `op="set_status"`.

**Several Pennylane instances in the same org** (for example a personal
instance and the company's): without `_instance`, the cascade
chooses — the personal key first. To quote or invoice on behalf of the
company, pass `_instance="org:<id>:pennylane"`.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .pennylane_socle import _bad, _client, _ecrit, _need

_STATUTS = ("pending", "accepted", "denied", "invoiced", "expired")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_quote(
        op: Literal["list", "get", "lines", "pdf", "appendices", "create", "set_status",
                    "to_invoice"] = "list",
        quote_id: Optional[int] = None,
        customer_id: Optional[int] = None,
        date: Optional[str] = None,
        deadline: Optional[str] = None,
        lines: Optional[list] = None,
        free_text: Optional[str] = None,
        external_reference: Optional[str] = None,
        quote_template_id: Optional[int] = None,
        customer_invoice_template_id: Optional[int] = None,
        status: Optional[str] = None,
        max_pages: Optional[int] = None,
    ) -> dict:
        """Sales QUOTES — the document that precedes the invoice.

        `op`:
        - "list": returns `{quotes: [...]}`, the quotes, filterable by `status` and
          `customer_id` (server-side filter). ⚠️ Without `max_pages`, the WHOLE history comes back — start small.
        - "get" (`quote_id`): the full quote, including `status`, `quote_number`,
          `public_file_url` (the PDF) and `linked_invoices`.
        - "lines" (`quote_id`): returns `{quote_id, lines: [...]}`, its lines.
        - "appendices" (`quote_id`): the files appended to it, `{id, filename, url}`.
        - "pdf" (`quote_id`): returns `{quote_id, quote_number, public_file_url,
          filename}` — the PDF link to attach to an email. ⚠️ The link EXPIRES
          (30 minutes): re-read it just before using it, never store it.
        - "create": creates the quote (`customer_id`, `date`, `deadline` = end of
          validity, `lines`). No draft: it is born `pending`. The customer must
          exist (`pennylane_customer`). Announce the customer, lines
          and validity to the user before calling.
        - "set_status" (`quote_id`, `status`): pending | accepted | denied |
          invoiced | expired — for example `accepted` when the customer has signed.
        - "to_invoice" (`quote_id`): creates a customer invoice that takes over the quote
          (customer, lines), always as a **draft**; finalizing it (`pennylane_invoice`)
          and sending it (`pennylane_act`) remain separate gestures, after human
          validation.
          Requires the `customer_invoices:all` scope in addition to `quotes:all`.

        The lines have the invoices' STRICT schema (any deviation → opaque 400) —
        a line = ONE of 2 forms: product `{product_id: int, quantity:
        number}` (possible overrides: label, raw_currency_unit_price, unit,
        vat_rate), or free `{label: str, quantity: number, unit: str,
        raw_currency_unit_price: str, vat_rate: str}`, all required. A DISCOUNT
        is a free line with a negative price (`raw_currency_unit_price: "-100.00"`).
        `vat_rate` = Pennylane code: 20 %→"FR_200", 10 %→"FR_100", 5.5 %→"FR_55",
        exempt→"exempt".

        Several Pennylane instances in an org (personal and company): without
        `_instance`, the personal key answers first. For a quote on behalf of the
        company, pass `_instance="org:<id>:pennylane"`.

        Args:
            op: "list" (default) | "get" | "lines" | "pdf" | "appendices" | "create" |
                "set_status" | "to_invoice".
            quote_id: required for get / lines / pdf / appendices / set_status /
                to_invoice.
            customer_id: op="create" (required); op="list" (filter).
            date: op="create" — quote date (YYYY-MM-DD).
            deadline: op="create" — end of validity (YYYY-MM-DD).
            lines: op="create" — lines in the strict schema above.
            free_text: op="create" — free text printed on the PDF (terms,
                customer's order reference…).
            external_reference: op="create" — source trace; op="to_invoice" —
                reference of the created invoice.
            quote_template_id: op="create" — rendering template of the quote.
            customer_invoice_template_id: op="to_invoice" — template of the invoice.
            status: op="set_status" (required); op="list" (filter).
            max_pages: op="list" / "appendices" — bounds the pagination.
        """
        if status is not None and status not in _STATUTS:
            raise _bad(f"unknown status: {status!r} — expected: {', '.join(_STATUTS)}")
        c = _client()
        if op == "list":
            return {"quotes": c.list_quotes(max_pages=max_pages, status=status,
                                            customer_id=customer_id)}
        if op == "get":
            return c.get_quote(_need(quote_id, "quote_id", op))
        if op == "lines":
            return {"quote_id": quote_id,
                    "lines": c.get_quote_lines(_need(quote_id, "quote_id", op))}
        if op == "appendices":
            return c.list_quote_appendices(_need(quote_id, "quote_id", op),
                                           max_pages=max_pages)
        if op == "pdf":
            devis = c.get_quote(_need(quote_id, "quote_id", op))
            url = (devis or {}).get("public_file_url")
            if not url:
                raise _bad(f"Pennylane returned no PDF link for quote {quote_id}.")
            return {"quote_id": quote_id, "quote_number": devis.get("quote_number"),
                    "public_file_url": url, "filename": devis.get("filename")}
        if op == "create":
            return _ecrit(lambda: c.create_quote(
                customer_id=_need(customer_id, "customer_id", op),
                date=_need(date, "date", op),
                deadline=_need(deadline, "deadline", op),
                lines=_need(lines, "lines", op),
                external_reference=external_reference, pdf_free_text=free_text,
                quote_template_id=quote_template_id), "quote creation")
        if op == "set_status":
            return _ecrit(lambda: c.update_quote_status(
                _need(quote_id, "quote_id", op), _need(status, "status", op)),
                "quote status change")
        if op == "to_invoice":
            return _ecrit(lambda: c.create_invoice_from_quote(
                _need(quote_id, "quote_id", op), draft=True,
                external_reference=external_reference,
                customer_invoice_template_id=customer_invoice_template_id),
                "quote invoicing")
        raise _bad("op must be 'list', 'get', 'lines', 'pdf', 'appendices', 'create', "
                   "'set_status' or 'to_invoice'")
