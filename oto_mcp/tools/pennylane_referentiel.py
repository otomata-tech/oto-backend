"""Pennylane REFERENCE DATA, written — analytical axes and categories, journals,
ledger accounts, products.

Module of the `pennylane` connector (see `Connector.modules` in the registry): same
key, same client. Reading this data stays `pennylane_ref`, read-only; this tool only
creates and changes it. The API deletes none of these objects: a creation stays, so
each op says to look the object up first.

Each op calls the client method of the same name, which is the function of the
connector's description (`oto-core`, `connectors/pennylane/connector.yaml`).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP

from .pennylane_socle import _bad, _client, _ecrit, _need

_OPS = ("create_category_group", "update_category_group", "create_category",
        "update_category", "create_journal", "create_ledger_account",
        "update_ledger_account", "update_product")


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def pennylane_ref_edit(
        op: Literal["create_category_group", "update_category_group", "create_category",
                    "update_category", "create_journal", "create_ledger_account",
                    "update_ledger_account", "update_product"],
        label: Optional[str] = None,
        category_group_id: Optional[int] = None,
        category_id: Optional[int] = None,
        kind: Optional[Literal["profit_and_loss", "treasury", "building"]] = None,
        direction: Optional[Literal["cash_in", "cash_out"]] = None,
        analytical_code: Optional[str] = None,
        ledger_account_id: Optional[int] = None,
        code: Optional[str] = None,
        number: Optional[str] = None,
        vat_rate: Optional[str] = None,
        country_alpha2: Optional[str] = None,
        letterable: Optional[bool] = None,
        product_id: Optional[int] = None,
        fields: Optional[dict] = None,
    ) -> dict:
        """Create or change Pennylane REFERENCE data. Reading it is `pennylane_ref`:
        look the object up there first — the API deletes none of these.

        `op`:
        - "create_category_group" (`label`, optional `kind`: profit_and_loss,
          treasury, building): a new analytical axis.
        - "update_category_group" (`category_group_id`, `label` — required by
          Pennylane even unchanged —, optional `kind`).
        - "create_category" (`label`, `category_group_id`, optional `direction`
          — treasury axes only, cash_out by default —, `analytical_code`,
          `ledger_account_id` — only in an axis with self-service accounting, never
          a bank, pending or cash account).
        - "update_category" (`category_id` and what changes: `label`, `direction`,
          `analytical_code`, `ledger_account_id`).
        - "create_journal" (`code` of 2 to 5 letters, `label`).
        - "create_ledger_account" (`number`, `label`, optional `vat_rate` — a
          Pennylane code such as FR_200 —, `country_alpha2`). ⚠️ A number starting
          with 401 also creates a SUPPLIER, 411 a company CUSTOMER: look both up
          first (`pennylane_supplier`, `pennylane_customer`).
        - "update_ledger_account" (`ledger_account_id`, `label` and/or
          `letterable`); the number cannot change.
        - "update_product" (`product_id`, `fields`): label, description,
          external_reference, price_before_tax (decimal STRING), vat_rate (FR_200
          for 20 %), unit, currency, reference, ledger_account_id, substance
          (goods | services). Invoices already issued keep their lines.

        Scopes: `categories:all`, `journals:all`, `ledger_accounts:all`,
        `products:all`.

        Args:
            op: see above.
            label: the label of the axis, category, journal or ledger account.
            category_group_id: the axis (create_category, update_category_group).
            category_id: op="update_category".
            kind: the kind of an axis.
            direction: a treasury category's direction.
            analytical_code: a category's analytical code.
            ledger_account_id: a category's account, or op="update_ledger_account".
            code: op="create_journal".
            number / vat_rate / country_alpha2: op="create_ledger_account".
            letterable: op="update_ledger_account".
            product_id / fields: op="update_product".
        """
        c = _client()
        try:
            if op == "create_category_group":
                return _ecrit(lambda: c.create_category_group(
                    _need(label, "label", op), kind=kind), "analytical axis creation")
            if op == "update_category_group":
                return _ecrit(lambda: c.update_category_group(
                    _need(category_group_id, "category_group_id", op),
                    _need(label, "label", op), kind=kind), "analytical axis update")
            if op == "create_category":
                return _ecrit(lambda: c.create_category(
                    _need(label, "label", op),
                    _need(category_group_id, "category_group_id", op),
                    direction=direction, analytical_code=analytical_code,
                    ledger_account_id=ledger_account_id), "category creation")
            if op == "update_category":
                return _ecrit(lambda: c.update_category(
                    _need(category_id, "category_id", op), label=label,
                    direction=direction, analytical_code=analytical_code,
                    ledger_account_id=ledger_account_id), "category update")
            if op == "create_journal":
                return _ecrit(lambda: c.create_journal(_need(code, "code", op),
                                                       _need(label, "label", op)),
                              "journal creation")
            if op == "create_ledger_account":
                return _ecrit(lambda: c.create_ledger_account(
                    _need(number, "number", op), _need(label, "label", op),
                    vat_rate=vat_rate, country_alpha2=country_alpha2),
                    "ledger account creation")
            if op == "update_ledger_account":
                return _ecrit(lambda: c.update_ledger_account(
                    _need(ledger_account_id, "ledger_account_id", op), label=label,
                    letterable=letterable), "ledger account update")
            if op == "update_product":
                return _ecrit(lambda: c.update_product(
                    _need(product_id, "product_id", op), **_need(fields, "fields", op)),
                    "product update")
        except ValueError as e:      # refused by the client before any call
            raise _bad(f"op='{op}': {e}") from e
        raise _bad(f"op must be one of {', '.join(_OPS)}")
