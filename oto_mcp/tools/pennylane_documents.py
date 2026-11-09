"""Pennylane OTHER RECORDS, read-only — billing subscriptions, commercial documents
(proformas, shipping and purchasing orders), purchase requests, SEPA mandates, and
the changes of any object since a date.

Module of the `pennylane` connector (see `Connector.modules` in the registry): same
key, same client. Read-only (`LECTURE`). Sending a commercial document is not served: it
would widen `pennylane_act`, whose ops the sends lot of 21/10/2026 pins.

Each kind calls the client method of the description's function (`oto-core`,
`connectors/pennylane/connector.yaml`).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .. import output_projection
from .lecture import LECTURE
from .pennylane_socle import _bad, _client, _need

# What the default view of a list leaves out (`full=True` gives everything): the bulky
# nested parts, which the singular kind returns.
_RETIRE = {
    "billing_subscriptions": ("customer_invoice_data", "email_settings"),
    "commercial_documents": ("invoice_lines", "invoice_line_sections", "appendices",
                             "pdf_description", "pdf_invoice_free_text",
                             "pdf_invoice_subject", "special_mention", "public_file_url",
                             "commercial_document_template"),
    "purchase_requests": ("delivery_address", "purchase_order"),
    "sepa_mandates": (),
}


def _vue(kind: str, rows: list, full: bool) -> dict:
    """`{items}` of a list kind, without its bulky fields unless `full`."""
    retire = () if full else _RETIRE[kind]
    out = output_projection.project({"items": rows}, items_path="items", item_drop=retire)
    if retire:
        out["projection"] = {"omitted": list(retire),
                             "full": "pass full=true, or read one record by its kind"}
    return out


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=LECTURE)
    def pennylane_records(
        kind: Literal["billing_subscriptions", "billing_subscription",
                      "billing_subscription_lines", "commercial_documents",
                      "commercial_document", "commercial_document_lines",
                      "commercial_document_appendices", "purchase_requests",
                      "purchase_request", "sepa_mandates", "sepa_mandate", "changes"],
        record_id: Optional[int] = None,
        clauses: Optional[list] = None,
        resource: Optional[Literal["transactions", "customer_invoices", "supplier_invoices",
                                   "customers", "suppliers", "products", "quotes",
                                   "ledger_entries", "ledger_entry_lines",
                                   "ledger_entries_categories",
                                   "ledger_entry_lines_categories"]] = None,
        start_date: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: Optional[int] = None,
        max_pages: Optional[int] = None,
        full: bool = False,
    ) -> dict | list:
        """Pennylane records beyond invoices and quotes, READ-ONLY.

        `kind` (a singular kind takes `record_id`):
        - "billing_subscriptions" / "billing_subscription" / "billing_subscription_lines":
          recurring invoices — customer, status (draft, not_started, in_progress,
          pending, stopped, finished), mode (awaiting_validation: invoices come as
          drafts; finalized; email: finalized AND emailed), recurring_rule,
          next_occurrence; the lines of one. Scope `billing_subscriptions:readonly`.
        - "commercial_documents" / "commercial_document" / "commercial_document_lines" /
          "commercial_document_appendices": proformas, shipping orders, purchasing
          orders (`document_type`). Scope `commercial_documents:readonly`.
        - "purchase_requests" / "purchase_request": supplier, amounts, reason,
          status (to_be_validated, approved, rejected, invoiced), linked_invoices.
          Scope `purchase_requests:readonly`.
        - "sepa_mandates" / "sepa_mandate": the customers' SEPA direct debit
          mandates (iban, bic, signed_at, sequence_type). Scope
          `customer_mandates:readonly`.
        - "changes" (`resource`): what changed on one kind of object, oldest
          first, `{items: [{id, operation, processed_at}], has_more, next_cursor}` —
          ONE page per call. Pennylane keeps FOUR WEEKS: an older `start_date` is
          refused. Pass `start_date` (RFC 3339) for the first page, then ONLY
          `cursor` (`next_cursor`): both together are refused. Scope: the read
          scope of that object.

        Args:
            kind: see above.
            record_id: the id of a singular kind (subscription, document, request,
                mandate).
            clauses: list kinds — server-side filter `[{"field", "operator", "value"}]`.
                Fields: billing_subscriptions id, start, customer_id, status;
                commercial_documents id, customer_id, document_type (proforma,
                shipping_order, purchasing_order); purchase_requests id,
                reviewed_by_id, user_id, supplier_id; sepa_mandates id, customer_id.
            resource / start_date / cursor / limit: kind="changes" (limit ≤ 1000).
            max_pages: list kinds — bounds the pagination.
            full: list kinds — every field. By default a list returns `{items,
                projection}` without the bulky nested parts (lines, PDF texts,
                invoice data, addresses), which the singular kind gives.
        """
        c = _client()
        listes = {  # the keys of `_RETIRE`
            "billing_subscriptions": c.list_billing_subscriptions,
            "commercial_documents": c.list_commercial_documents,
            "purchase_requests": c.list_purchase_requests,
            "sepa_mandates": c.list_sepa_mandates,
        }
        uns = {
            "billing_subscription": c.get_billing_subscription,
            "billing_subscription_lines": c.list_billing_subscription_lines,
            "commercial_document": c.get_commercial_document,
            "commercial_document_lines": c.list_commercial_document_lines,
            "commercial_document_appendices": c.list_commercial_document_appendices,
            "purchase_request": c.get_purchase_request,
            "sepa_mandate": c.get_sepa_mandate,
        }
        try:
            if kind in listes:
                return _vue(kind, listes[kind](filter=clauses, max_pages=max_pages), full)
            if kind in uns:
                ident = _need(record_id, "record_id", kind)
                if kind.endswith(("_lines", "_appendices")):
                    return uns[kind](ident, max_pages=max_pages)
                return uns[kind](ident)
            if kind == "changes":
                return c.list_changes(_need(resource, "resource", kind),
                                      start_date=start_date, limit=limit, cursor=cursor)
        except ValueError as e:      # refused by the client before any call
            raise _bad(f"kind='{kind}': {e}") from e
        raise _bad(f"kind must be one of {', '.join([*listes, *uns, 'changes'])}")
