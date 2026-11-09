"""Pennylane BANK — one bank account, the banks, one transaction: read, record,
attach, categorize.

Module of the `pennylane` connector (see `Connector.modules` in the registry): same
key, same client. The LISTS stay where they were: the accounts and their balance are
`pennylane_bank_accounts`, the transactions `pennylane_transactions`; matching a
transaction to an invoice is `pennylane_match`, and undoing it
`pennylane_invoice` / `pennylane_supplier_invoice(op="unmatch_transaction")`.

Each op calls the client method of the same name, which is the function of the
connector's description (`oto-core`, `connectors/pennylane/connector.yaml`).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .pennylane_socle import _analytique, _bad, _client, _ecrit, _need

_OPS = ("account", "establishments", "transaction", "transaction_categories",
        "matched_invoices", "create_transaction", "update_transaction",
        "categorize_transaction")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_bank(
        op: Literal["account", "establishments", "transaction", "transaction_categories",
                    "matched_invoices", "create_transaction", "update_transaction",
                    "categorize_transaction"],
        bank_account_id: Optional[int] = None,
        bank_ids: Optional[list[int]] = None,
        transaction_id: Optional[int] = None,
        label: Optional[str] = None,
        date: Optional[str] = None,
        amount: Optional[str] = None,
        fee: Optional[str] = None,
        customer_id: Optional[int] = None,
        supplier_id: Optional[int] = None,
        detach: Optional[Literal["customer", "supplier"]] = None,
        categories: Optional[list] = None,
        max_pages: Optional[int] = None,
    ) -> dict:
        """Pennylane bank: one account, the banks, one transaction. The accounts and
        their balance are `pennylane_bank_accounts`, the transaction list
        `pennylane_transactions`. A list op returns `{items: [...]}`.

        `op`:
        - "account" (`bank_account_id`): one bank account and its CURRENT balance,
          a decimal STRING ("12345.67") — parse it as a decimal, never a float.
          Scope `bank_accounts:readonly`.
        - "establishments": the banks `{id, name}`; `bank_ids` narrows to some.
          Scope `bank_establishments:readonly`, distinct from the accounts' one.
        - "transaction" (`transaction_id`): one transaction — amount, date, label,
          `outstanding_balance` (non-zero while not fully matched), customer or
          supplier, categories, `matched_invoices`.
        - "transaction_categories" (`transaction_id`): its analytical categories,
          each with its `weight`.
        - "matched_invoices" (`transaction_id`): the invoices matched to it, each
          `{id, type, url}` (`type` tells customer from supplier invoice).
        - "create_transaction" (`bank_account_id`, `label`, `date`, `amount`,
          optional `fee`): records a transaction BY HAND, for an account without a
          bank feed. `amount` a decimal string, negative for money out. ⚠️ A
          transaction the bank feed also brings would be counted twice: check
          `pennylane_transactions` on that date first. Scope `transactions:all`.
        - "update_transaction" (`transaction_id`): attaches it to ONE `customer_id`
          or ONE `supplier_id`; `detach="customer"` or `"supplier"` (without an id)
          detaches that side instead. Matching an invoice is `pennylane_match`.
        - "categorize_transaction" (`transaction_id`, `categories`): REPLACES its
          analytical categories with `[{"id": …, "weight": "0.5"}]` — weight as a
          STRING between 0 and 1, the weights of ONE axis sum to 1; `[]` removes
          them all. Ids from `pennylane_ref(kind="categories")`. Reversible:
          re-send the list read with op="transaction_categories".

        Args:
            op: see above.
            bank_account_id: op="account" / "create_transaction".
            bank_ids: op="establishments" — the bank ids (`bank.id` of an account).
            transaction_id: every transaction op but "create_transaction".
            label / date / amount / fee: op="create_transaction" (date YYYY-MM-DD,
                amounts as decimal strings).
            customer_id / supplier_id: op="update_transaction" — exactly one.
            detach: op="update_transaction" — "customer" or "supplier", detached.
            categories: op="categorize_transaction" — `[{"id", "weight"}]`.
            max_pages: list ops — bounds the pagination.
        """
        c = _client()
        if op == "account":
            return c.get_bank_account(_need(bank_account_id, "bank_account_id", op))
        if op == "establishments":
            clauses = ([{"field": "id", "operator": "in", "value": bank_ids}]
                       if bank_ids else None)
            return {"items": c.list_bank_establishments(filter=clauses, max_pages=max_pages)}
        if op == "transaction":
            return c.get_transaction(_need(transaction_id, "transaction_id", op))
        if op == "transaction_categories":
            return {"items": c.list_transaction_categories(
                _need(transaction_id, "transaction_id", op), max_pages=max_pages)}
        if op == "matched_invoices":
            return {"items": c.list_transaction_matched_invoices(
                _need(transaction_id, "transaction_id", op), max_pages=max_pages)}
        if op == "create_transaction":
            return _ecrit(lambda: c.create_transaction(
                bank_account_id=_need(bank_account_id, "bank_account_id", op),
                label=_need(label, "label", op), date=_need(date, "date", op),
                amount=_need(amount, "amount", op), fee=fee), "transaction recording")
        if op == "update_transaction":
            ident = _need(transaction_id, "transaction_id", op)
            given = {k: v for k, v in (("customer_id", customer_id),
                                       ("supplier_id", supplier_id)) if v is not None}
            if detach is not None and given:
                raise _bad("op='update_transaction': detach takes no customer_id nor "
                           "supplier_id")
            if detach is None and len(given) != 1:
                raise _bad("op='update_transaction' takes exactly one of customer_id, "
                           "supplier_id, or detach")
            fields = {f"{detach}_id": None} if detach is not None else given
            return _ecrit(lambda: c.update_transaction(ident, **fields),
                          "transaction update")
        if op == "categorize_transaction":
            r = _analytique(c, "categorize", "transaction",
                            _need(transaction_id, "transaction_id", op), categories)
            return {"items": r} if isinstance(r, list) else r
        raise _bad(f"op must be one of {', '.join(_OPS)}")
