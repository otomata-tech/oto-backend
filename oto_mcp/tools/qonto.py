"""Qonto tool — French business banking, read-only.

Wraps `oto.tools.qonto.QontoClient` (Business API v2, `Authorization:
login:secret-key`), credential resolved per call via `access.resolve_credential("qonto")`
(member → team → org, never a platform key: it opens ONE company's bank account).
The form asks for the login and the secret key in two boxes; `_authorization` joins
them (and still reads a key saved before the split, `login:secret-key` in one box).

The connector is read-only, so it serves ONE tool, `qonto` (LECTURE), the read verb
in `op` — no `qonto_act` (`tests/test_convention_act.py`). An API key reaches no
payment route; cards and cash-flow categories answer 401 "OAuth2 required" to it,
so they are not served.

Dates: one `date_from` / `date_to` pair, applied to the field each list filters on
by default (`settled_at` for transactions, `created_at` for invoices, quotes, credit
notes and requests, `scheduled_date` for transfers, the month for statements),
`date_field` to pick another. Transactions without an account read the MAIN account
(one `get_organization` call first) — the common case, an organization with one
account.

Client calls are written out (`_client().list_transactions(…)`) so the
version-skew probe (`test_tools_client_methods_exist`) can check them.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify
from ..mcp_errors import McpError
from .lecture import LECTURE

_NAME = "qonto"

_OPS = ("accounts", "transactions", "transaction", "attachments", "attachment",
        "statements", "statement", "supplier_invoices", "supplier_invoice",
        "client_invoices", "client_invoice", "credit_notes", "quotes", "clients",
        "client", "transfers", "beneficiaries", "requests", "members", "labels")

#: Ops that read one object by `id`, and the argument name the client gives it.
_BY_ID = {"transaction": "transaction_id", "attachments": "transaction_id",
          "attachment": "attachment_id", "statement": "statement_id",
          "supplier_invoice": "supplier_invoice_id",
          "client_invoice": "client_invoice_id", "client": "client_id"}

#: The list key of each list op's answer, for `all_pages` and the trimmed view.
_ITEMS = {"transactions": "transactions", "attachments": "attachments",
          "statements": "statements", "supplier_invoices": "supplier_invoices",
          "client_invoices": "client_invoices", "credit_notes": "credit_notes",
          "quotes": "quotes", "clients": "clients", "transfers": "transfers",
          "beneficiaries": "beneficiaries", "requests": "requests",
          "members": "memberships", "labels": "labels"}

#: Date field each op filters on by default, and the ones it accepts.
_DATE_FIELDS = {
    "transactions": ("settled_at", ("settled_at", "emitted_at", "updated_at")),
    "supplier_invoices": ("created_at", ("created_at", "updated_at")),
    "client_invoices": ("created_at", ("created_at", "updated_at", "due_date")),
    "credit_notes": ("created_at", ("created_at",)),
    "quotes": ("created_at", ("created_at",)),
    "clients": ("updated_at", ("updated_at",)),
    "transfers": ("scheduled_date", ("scheduled_date", "updated_at")),
    "requests": ("created_at", ("created_at", "processed_at")),
    "statements": ("period", ("period",)),
}

#: What the default (non-`full`) view drops from each row: display-only and
#: bookkeeping keys nobody reads at triage. `full=True` returns the raw rows.
_ROW_DROP = {
    "transactions": ("logo", "amount_cents", "local_amount_cents", "settled_balance_cents",
                     "vat_amount_cents", "attachment_lost", "subject_type"),
    "supplier_invoices": ("available_actions", "einvoicing_lifecycle_events",
                          "approval_workflow", "matched_transactions", "meta",
                          "related_invoices", "has_suggested_credit_notes",
                          "display_attachment_id", "organization_id"),
}

#: Pages walked by `all_pages` before stopping (100 rows each on most lists).
_MAX_PAGES = 20


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, **given: Any) -> None:
    """No argument is silently dropped: one that `op` does not read is refused."""
    stray = sorted(k for k, v in given.items() if v is not None)
    if stray:
        raise _bad(f"op='{op}' does not take {', '.join(f'`{k}`' for k in stray)}. "
                   "Nothing was done.")


def _upstream_message(e) -> str:
    """Turn a Qonto refusal into an actionable message, by status."""
    status = e.status_code
    detail = e.body if isinstance(e.body, (str, dict)) else ""
    if status == 401 and "oauth" in str(detail).lower():
        return ("Qonto serves this read only to an OAuth connection, not to an API "
                f"key (401): {detail}")
    if status == 401:
        return ("Qonto rejected the login and secret key (401). Both come from Settings → "
                "Integrations and partnerships → API key: the Login (like acme-1234, not "
                "an email) and the latest Secret key — regenerating it revokes the old "
                "one. Update them on the Qonto connector; retrying unchanged fails "
                "the same way.")
    if status == 403:
        return f"Qonto refused this read (403): the key's member lacks the right. {detail}"
    if status == 404:
        return f"Qonto: not found (404) — check the id. {detail}"
    if status in (400, 422):
        return f"Qonto rejected the request ({status}): {detail}"
    if status == 429:
        return "Qonto: too many requests (429) — retry in a few seconds."
    if status in (500, 502, 503, 504):
        return f"Qonto is temporarily unavailable (HTTP {status}) — retry later."
    return f"Qonto refused the request (HTTP {status}): {detail}"


def _month(value: Optional[str]) -> Optional[str]:
    """A statement period: `MM-YYYY` as is, or the month of an ISO date."""
    if value is None or len(value) == 7 and value[2] == "-":
        return value
    if len(value) >= 7 and value[4] == "-":
        return f"{value[5:7]}-{value[:4]}"
    return value


def _authorization(key: Optional[str], login: Optional[str] = None) -> str:
    """The `Authorization` value Qonto wants, `login:secret-key`, from the two boxes.

    The secret key is what follows the last colon of `key` (a key saved before the
    split, or the pair pasted into the secret box out of habit, carries the login in
    front of it); the login is the `login` field, else that prefix. Raises
    `ValueError` (actionable) when a half is missing."""
    key, login = (key or "").strip(), (login or "").strip()
    prefix, _, secret = key.rpartition(":")
    login = login or prefix.strip()
    secret = secret.strip()
    if not secret:
        raise ValueError("The Qonto Secret key is empty: copy it from Settings → "
                         "Integrations and partnerships → API key (click Generate if "
                         "it is empty).")
    if not login:
        raise ValueError("The Qonto Login is missing: it is shown above the Secret key "
                         "in Settings → Integrations and partnerships → API key (like "
                         "acme-1234, not your email).")
    return f"{login}:{secret}"


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test connection" probe: reads the organization (`get_organization`) — no
    side effect; 401 on a wrong key. The login comes with the fields at save time,
    from `config` (the row's `meta`) otherwise."""
    from oto.tools.qonto import QontoClient
    from oto.tools.common.errors import UpstreamHTTPError

    try:
        login = fields.get("login") or (config or {}).get("login")
        QontoClient(api_key=_authorization(fields.get("key"), login)).probe()
    except UpstreamHTTPError as e:
        if e.status_code in (401, 403):
            raise connector_verify.NonAutorise(_upstream_message(e))
        raise RuntimeError(f"Qonto HTTP {e.status_code}: {e.body}")
    except ValueError as e:
        raise connector_verify.NonAutorise(str(e))


def register(mcp: FastMCP) -> None:
    from oto.tools.qonto import QontoClient
    from oto.tools.common.errors import UpstreamHTTPError

    connector_verify.register(_NAME, _verify)

    def _client() -> QontoClient:
        rc = access.resolve_credential(_NAME, want="auto")
        # The login lives in the row's `meta`; it wins over a login carried inside a
        # key saved before the split, as in `_verify` (`_authorization`).
        return QontoClient(api_key=_authorization(rc.key, rc.config.get("login")))

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    def _main_account() -> str:
        org = _run(lambda: _client().get_organization())
        accounts = (org.get("organization") or {}).get("bank_accounts") or []
        main = [a for a in accounts if a.get("main")] or accounts
        if not main:
            raise _bad("Qonto lists no bank account for this organization.")
        return main[0]["id"]

    def _pages(op: str, fetch, page: Optional[int], all_pages: bool) -> Any:
        """One page, or every page from `page` on (at most `_MAX_PAGES`), merged."""
        first = _run(lambda: fetch(page))
        if not all_pages or not isinstance(first, dict):
            return first
        key, rows, meta, read = _ITEMS[op], list(first.get(_ITEMS[op]) or []), \
            first.get("meta") or {}, 1
        while meta.get("next_page") and read < _MAX_PAGES:
            nxt = meta["next_page"]
            more = _run(lambda: fetch(nxt))
            rows.extend(more.get(key) or [])
            meta, read = more.get("meta") or {}, read + 1
        return {key: rows, "meta": {"pages_read": read,
                                    "total_count": meta.get("total_count"),
                                    "truncated": bool(meta.get("next_page")),
                                    "next_page": meta.get("next_page")}}

    def _view(op: str, result: Any, full: bool) -> Any:
        if full or op not in _ROW_DROP:
            return result
        return output_projection.project(result, items_path=_ITEMS[op],
                                         item_drop=_ROW_DROP[op])

    @mcp.tool(name="qonto", annotations=LECTURE)
    def qonto(
        op: Literal[_OPS] = "accounts",
        id: Optional[str] = None,
        bank_account_id: Optional[str] = None,
        iban: Optional[str] = None,
        status: Optional[list[str]] = None,
        side: Optional[Literal["credit", "debit"]] = None,
        operation_type: Optional[list[str]] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        date_field: Optional[str] = None,
        query: Optional[str] = None,
        with_attachments: Optional[bool] = None,
        includes: Optional[list[Literal["vat_details", "labels", "attachments"]]] = None,
        request_type: Optional[list[str]] = None,
        trusted: Optional[bool] = None,
        sort_by: Optional[str] = None,
        page: Optional[int] = None,
        per_page: Optional[int] = None,
        all_pages: bool = False,
        full: bool = False,
    ) -> Any:
        """Qonto business banking, read-only: balances, transactions, receipts,
        statements, supplier and client invoices of the organization's Qonto account.

        `op`:
        - `accounts` (default) — the organization and every bank account with
          `balance`, `authorized_balance` (minus pending debits), `iban`, `id`.
        - `transactions` — the transactions of ONE account (`bank_account_id` or
          `iban`; neither = the main account), newest settled first: `amount`,
          `side` credit|debit, `operation_type`, `label` (counterparty), `status`,
          `settled_at`, `vat_amount`, `vat_rate`, `category`, `attachment_ids`.
          Filters: `status` (pending, declined, completed, reversed), `side`,
          `operation_type` (card, transfer, income, direct_debit, qonto_fee, ...),
          `with_attachments`; `includes` embeds vat_details, labels, attachments.
          ⚠️ A `settled_at` window leaves out pending transactions.
        - `transaction` (`id`) — one transaction by its UUID `id`.
        - `attachments` (`id` = transaction id) — its receipts; `attachment`
          (`id`) — one file. Their `url` is signed and expires after 30 minutes.
        - `statements` — monthly statements (`date_from`/`date_to` as MM-YYYY or a
          date); `statement` (`id`).
        - `supplier_invoices` — invoices and receipts received, with supplier,
          amounts (decimal strings), dates and matched transactions; `status` one of
          to_review, to_approve, awaiting_payment, pending, scheduled, paid,
          archived, rejected, discarded; `query` searches supplier, amount, file
          name, number. `supplier_invoice` (`id`).
        - `client_invoices` (`status` one of draft, unpaid, paid, canceled),
          `client_invoice` (`id`), `credit_notes`, `quotes` (`status` one of
          pending_approval, approved, canceled), `clients` (`query` = part of the
          name), `client` (`id`) — the invoicing side, if the company invoices
          from Qonto.
        - `transfers` — outgoing SEPA transfers (`status` pending, processing,
          canceled, declined, settled); `beneficiaries` — saved payees (`trusted`);
          `requests` — spend requests (`status`, `request_type` transfer,
          multi_transfer, flash_card, virtual_card).
        - `members`, `labels`.

        Dates: `date_from`/`date_to` (ISO 8601) filter on the op's natural field —
        settled_at for transactions, created_at for invoices, quotes, credit notes
        and requests, scheduled_date for transfers, updated_at for clients;
        `date_field` picks another (transactions: emitted_at, updated_at; client
        invoices: updated_at, due_date; ...).

        Lists answer `{<items>: [...], meta: {current_page, next_page, total_count,
        ...}}`, 100 per page at most (500 for client invoices and quotes);
        `all_pages=true` walks up to 20 pages and merges them (`meta.truncated`
        says if more remain). Transactions and supplier invoices drop display-only
        keys by default (logo, duplicate *_cents, available actions...);
        `full=true` returns raw rows. Not served: cards, cash-flow categories and
        any payment or write — Qonto keeps them behind an OAuth connection.

        Args:
            op: what to read (see above).
            id: the object id, for transaction | attachments | attachment |
                statement | supplier_invoice | client_invoice | client.
            bank_account_id: transactions / statements — the account (from op=accounts).
            iban: transactions / statements — or its IBAN.
            status: statuses to keep (one only for supplier_invoices,
                client_invoices, quotes).
            side: transactions — credit (in) | debit (out).
            operation_type: transactions — card, transfer, income, direct_debit, ...
            date_from: lower bound, ISO 8601 (statements: MM-YYYY or a date).
            date_to: upper bound, ISO 8601.
            date_field: which date the bounds apply to (default per op).
            query: supplier_invoices — full-text search; clients — part of the name.
            with_attachments: transactions — only with (true) / without (false) a receipt.
            includes: transactions / transaction — vat_details, labels, attachments.
            request_type: requests — transfer, multi_transfer, flash_card, virtual_card.
            trusted: beneficiaries — trusted or not.
            sort_by: property:asc|desc (transactions default settled_at:desc).
            page: page number, from 1.
            per_page: rows per page.
            all_pages: lists — read every page (20 at most) and merge.
            full: transactions / supplier_invoices — raw rows.
        """
        if op == "accounts":
            _refuse_ignored(op, id=id, bank_account_id=bank_account_id, iban=iban,
                            status=status, side=side, operation_type=operation_type,
                            date_from=date_from, date_to=date_to, date_field=date_field,
                            query=query, with_attachments=with_attachments,
                            includes=includes, request_type=request_type,
                            trusted=trusted, sort_by=sort_by, page=page,
                            per_page=per_page, all_pages=all_pages or None)
            return _run(lambda: _client().get_organization())

        if op in _BY_ID:
            if not id:
                raise _bad(f"`id` is required for op='{op}'. Nothing was done.")
            _refuse_ignored(op, bank_account_id=bank_account_id, iban=iban,
                            status=status, side=side, operation_type=operation_type,
                            date_from=date_from, date_to=date_to, date_field=date_field,
                            query=query, with_attachments=with_attachments,
                            request_type=request_type, trusted=trusted,
                            sort_by=sort_by, all_pages=all_pages or None,
                            includes=None if op == "transaction" else includes,
                            page=page if op != "attachments" else None,
                            per_page=per_page if op != "attachments" else None)
            if op == "transaction":
                return _run(lambda: _client().get_transaction(id, includes=includes))
            if op == "attachments":
                return _run(lambda: _client().list_transaction_attachments(
                    id, page=page, per_page=per_page))
            if op == "attachment":
                return _run(lambda: _client().get_attachment(id))
            if op == "statement":
                return _run(lambda: _client().get_statement(id))
            if op == "supplier_invoice":
                return _run(lambda: _client().get_supplier_invoice(id))
            if op == "client_invoice":
                return _run(lambda: _client().get_client_invoice(id))
            return _run(lambda: _client().get_client(id))

        _refuse_ignored(op, id=id)
        field = None
        if op in _DATE_FIELDS:
            default, allowed = _DATE_FIELDS[op]
            field = date_field or default
            if field not in allowed:
                raise _bad(f"op='{op}' filters dates on {', '.join(allowed)}, not "
                           f"`{field}`. Nothing was done.")
        else:
            _refuse_ignored(op, date_from=date_from, date_to=date_to,
                            date_field=date_field)

        def one() -> Optional[str]:
            if not status:
                return None
            if len(status) > 1:
                raise _bad(f"op='{op}' filters on ONE status; got {status}. "
                           "Nothing was done.")
            return status[0]

        def dates(names: tuple) -> dict:
            out = {f"{field}_from": date_from, f"{field}_to": date_to}
            return {k: v for k, v in out.items() if k in names}

        if op != "transactions":
            _refuse_ignored(op, side=side, operation_type=operation_type,
                            with_attachments=with_attachments, includes=includes)
        if op not in ("transactions", "statements"):
            _refuse_ignored(op, bank_account_id=bank_account_id, iban=iban)
        if op not in ("supplier_invoices", "clients"):
            _refuse_ignored(op, query=query)
        if op != "requests":
            _refuse_ignored(op, request_type=request_type)
        if op != "beneficiaries":
            _refuse_ignored(op, trusted=trusted)

        if op == "transactions":
            account = bank_account_id
            if account is None and iban is None:
                account = _main_account()
            when = dates(("settled_at_from", "settled_at_to", "emitted_at_from",
                          "emitted_at_to", "updated_at_from", "updated_at_to"))
            fetch = lambda p: _client().list_transactions(  # noqa: E731
                account, iban, status=status, side=side, operation_type=operation_type,
                with_attachments=with_attachments, includes=includes, sort_by=sort_by,
                page=p, per_page=per_page, **when)
        elif op == "statements":
            _refuse_ignored(op, status=status)
            fetch = lambda p: _client().list_statements(  # noqa: E731
                [bank_account_id] if bank_account_id else None,
                [iban] if iban else None, period_from=_month(date_from),
                period_to=_month(date_to), sort_by=sort_by, page=p, per_page=per_page)
        elif op == "supplier_invoices":
            st = one()
            when = dates(("created_at_from", "created_at_to", "updated_at_from",
                          "updated_at_to"))
            _refuse_ignored(op, sort_by=sort_by)
            fetch = lambda p: _client().list_supplier_invoices(  # noqa: E731
                st, query=query, page=p, per_page=per_page, **when)
        elif op == "client_invoices":
            st = one()
            when = dates(("created_at_from", "created_at_to", "updated_at_from",
                          "updated_at_to", "due_date_from", "due_date_to"))
            fetch = lambda p: _client().list_client_invoices(  # noqa: E731
                st, sort_by=sort_by, page=p, per_page=per_page, **when)
        elif op == "credit_notes":
            _refuse_ignored(op, status=status)
            when = dates(("created_at_from", "created_at_to"))
            fetch = lambda p: _client().list_credit_notes(  # noqa: E731
                sort_by=sort_by, page=p, per_page=per_page, **when)
        elif op == "quotes":
            st = one()
            when = dates(("created_at_from", "created_at_to"))
            fetch = lambda p: _client().list_quotes(  # noqa: E731
                st, sort_by=sort_by, page=p, per_page=per_page, **when)
        elif op == "clients":
            _refuse_ignored(op, status=status)
            when = dates(("updated_at_from", "updated_at_to"))
            fetch = lambda p: _client().list_clients(  # noqa: E731
                name=query, sort_by=sort_by, page=p, per_page=per_page, **when)
        elif op == "transfers":
            when = dates(("scheduled_date_from", "scheduled_date_to",
                          "updated_at_from", "updated_at_to"))
            fetch = lambda p: _client().list_sepa_transfers(  # noqa: E731
                status, sort_by=sort_by, page=p, per_page=per_page, **when)
        elif op == "beneficiaries":
            fetch = lambda p: _client().list_sepa_beneficiaries(  # noqa: E731
                status, trusted=trusted, sort_by=sort_by, page=p, per_page=per_page)
        elif op == "requests":
            if date_to is not None:
                raise _bad("op='requests' filters on a lower bound only (`date_from`). "
                           "Nothing was done.")
            when = dates(("created_at_from", "processed_at_from"))
            fetch = lambda p: _client().list_requests(  # noqa: E731
                status, request_type=request_type, sort_by=sort_by, page=p,
                per_page=per_page, **when)
        elif op == "members":
            _refuse_ignored(op, status=status, sort_by=sort_by)
            fetch = lambda p: _client().list_memberships(page=p, per_page=per_page)  # noqa: E731
        else:  # labels
            _refuse_ignored(op, status=status, sort_by=sort_by)
            fetch = lambda p: _client().list_labels(page=p, per_page=per_page)  # noqa: E731

        return _view(op, _pages(op, fetch, page, all_pages), full)
