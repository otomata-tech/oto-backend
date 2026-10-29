"""Stripe — customers, subscriptions, invoices, payments, balance, catalog.

Wraps `oto.tools.stripe.client.StripeClient` (API v1, Bearer, form-encoded
bodies). THREE-field credential (`secret_kind="fields"`, resolved by
`access.resolve_credential_fields`): the key, plus two NON-secret satellites
that decide **what the key reads**.

- `api_version` — empty = the account's default version, the one the customer
  sees in their own dashboard. Pinning a version that diverges from the account
  silently changes response shapes.
- `stripe_account` — `acct_…` (Connect). This is not decoration: with
  Connect, THE SAME question returns the revenue of a DIFFERENT company
  depending on this header. Making it a credential field rather than a tool
  parameter is the strongest form of "never inferred per call": one
  credential = one set of books, set once, visible on the card, and
  impossible to switch mid-conversation.

**byo-only, never a platform key**: these are the customer's account books.
A Stripe key shared by several orgs makes no sense.

**Nothing moves money, and that is structural.** This module can NOT
refund, cancel, finalize, collect, send or delete: the corresponding methods
do not exist on `StripeClient` (a choice documented in its docstring). So this
is not a policy of this file, which a one-line PR would lift — it is a boundary
that takes an oto-core PR to move.

**Nine tools, one per business object** (ADR 0047), verb in `op=`. No parameter
is silently swallowed: an `op` that does not use a supplied argument REFUSES
(`_refuse_ignored` pattern, silae/granola).

**Live-tested on 2026-08-22** against a real Stripe account in test mode
(restricted key `rk_test_`): the 20 reads and the safe writes respond as coded.
Two behaviors the docs do not mention, both found by probing:

1. ⚠️ **`create_draft` does NOT pick up pending lines by default.** Creating
   a line (`op="add_item"`) then an invoice returned `total=0` and zero lines,
   the line staying `invoice=None`. An agent would have announced "invoice created"
   while producing an EMPTY invoice and leaving the amount dangling. Hence the
   default `pending_items="include"` here (measured: `total=4200`, 1 line).
2. ⚠️ **A payment link may require a `tax_code` on the product** when the
   account is eligible for managed payments: `create_link` returned
   `400 "the product tax code is missing"`. Setting `tax_code` on the product
   (e.g. `txcd_10000000`, generic services) unblocks it — the error message now
   says so explicitly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify
from .lecture import LECTURE

# Stripe caps its lists at 100 and falls back SILENTLY to 10 when `limit` is
# omitted. So we always set an explicit value.
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 100

# Sweep cap for `op="totals"`: 20 pages × 100 = 2,000 objects. Beyond that,
# the response SAYS so (`complete: false`) instead of returning a partial sum
# that would pass for the real revenue.
_AGGREGATE_MAX_PAGES = 20


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _refuse_ignored(op: str, hint: str, **provided) -> None:
    """An argument that was supplied but that THIS op does not use is an error of intent.
    Otherwise `stripe_invoice(op="list", invoice_id=…)` would return ALL invoices
    while letting the caller believe one was targeted."""
    for name, value in provided.items():
        if value is not None:
            raise _bad(f"op={op!r} does not use `{name}` — {hint}")


def _limit(value: Optional[int]) -> int:
    if value is None:
        return _DEFAULT_LIMIT
    if not 1 <= value <= _MAX_LIMIT:
        raise _bad(f"`limit` must be between 1 and {_MAX_LIMIT} (got {value}) — "
                   "Stripe does not return more per page.")
    return value


def _window(created_after: Optional[int], created_before: Optional[int]) -> Optional[dict]:
    """Stripe's time window is a dict of operators, not two fields."""
    w = {}
    if created_after is not None:
        w["gte"] = created_after
    if created_before is not None:
        w["lt"] = created_before
    return w or None


def _upstream_message(e) -> str:
    status = e.status_code
    body = e.body if isinstance(e.body, dict) else {}
    err = body.get("error", {}) if isinstance(body, dict) else {}
    detail = err.get("message") or ""
    code = err.get("code")
    param = err.get("param")
    req = body.get("request_id")
    tail = f" (request_id {req})" if req else ""
    if status in (401, 403):
        return (f"Stripe rejected the key (HTTP {status}) — either it is invalid, or "
                f"it is a RESTRICTED key missing the read OR "
                f"WRITE permission for this resource (a write, e.g. create_coupon, needs "
                f"the Write scope, not just Read). Stripe Dashboard → Developers → "
                f"API keys.{tail} {detail}")
    if status == 404:
        return (f"Stripe: object not found. Check the id, and above all the MODE: "
                f"a test object does not exist in live mode, and vice versa.{tail} {detail}")
    if status == 429:
        return f"Stripe: too many requests (429) — try again in a moment.{tail}"
    if status >= 500:
        return f"Stripe is temporarily unavailable (HTTP {status}) — try again later.{tail}"
    extra = ""
    if "tax code is missing" in detail:
        extra = (" — set a tax code on the product before creating the link: "
                 "`stripe_catalog(op=\"update_product\", product_id=…, "
                 "tax_code=\"txcd_10000000\")` (generic services).")
    where = f" (field `{param}`)" if param else ""
    return f"Stripe refused the request (HTTP {status}, code {code}){where}: {detail}{extra}{tail}"


def _verify(fields: dict, config: dict | None = None) -> None:
    """"Test the connection" probe: two GETs, no side effects.

    `GET /v1/balance` proves the key is alive — but a key restricted to
    Balance:read alone would pass it while failing on every real question. The
    second read (one invoice, `limit=1`) therefore proves the CAPABILITY, not
    just authentication: this is the lesson of the Zoho probe (auth OK,
    zero CRM scopes).
    """
    from oto.tools.stripe.client import StripeClient
    cfg = config or {}
    client = StripeClient(fields["api_key"],
                          api_version=cfg.get("api_version") or None,
                          stripe_account=cfg.get("stripe_account") or None)
    balance = client.balance()
    mode = "live" if balance.get("livemode") else "test"
    try:
        client.list_invoices(limit=1)
    except Exception as e:  # noqa: BLE001 — the exception message IS the error return
        raise RuntimeError(
            f"The key authenticates fine (balance read, {mode} mode) but cannot read "
            f"invoices — it is a restricted key without the Invoices:read permission. "
            f"Detail: {e}") from e


def register(mcp: FastMCP) -> None:
    from oto.tools.common.errors import UpstreamHTTPError
    from oto.tools.stripe.client import StripeClient

    connector_verify.register("stripe", _verify)

    def _client() -> StripeClient:
        fields = access.resolve_credential_fields("stripe")
        return StripeClient(fields["api_key"],
                            api_version=fields.get("api_version") or None,
                            stripe_account=fields.get("stripe_account") or None)

    def _run(fn):
        try:
            return fn()
        except ValueError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))

    # ================================================================
    # Search — the most valuable read surface
    # ================================================================

    @mcp.tool(annotations=LECTURE)
    def stripe_search(
        resource: Literal["customers", "invoices", "subscriptions", "charges",
                          "payment_intents", "products", "prices"],
        query: str,
        limit: Optional[int] = None,
        page: Optional[str] = None,
    ) -> object:
        """Search Stripe with its own query language. One verb, no `op` — the
        axis here is `resource`.

        Only these SEVEN resources are searchable; anything else must be listed
        with filters instead.

        Only these SEVEN resources are searchable; anything else must be listed
        with filters instead.

        Search is eventually consistent: an object created seconds ago may not
        appear yet. To read something just written, fetch it by id.

        Args:
            resource: which object type to search.
            query: Stripe Query Language, e.g. `email~"acme.com"`,
                `status:"active"`, `created>1704067200`,
                `metadata["order_id"]:"6735"`, `total>10000 AND currency:"eur"`.
                Operators: `:` exact, `~` contains, `>` `<` `>=` `<=`,
                `AND`/`OR`, `-` negation. Searchable FIELDS differ per resource.
            limit: 1-100 (default 100).
            page: the opaque `next_page` token from a previous response — NOT
                the `starting_after` cursor the list ops use; they are different
                paginators.

        """
        client = _client()
        return _run(lambda: client.search(resource, query, limit=_limit(limit), page=page))

    # ================================================================
    # Customers
    # ================================================================

    @mcp.tool()
    def stripe_customer(
        op: Literal["list", "get", "payment_methods", "create", "update"] = "list",
        customer_id: Optional[str] = None,
        email: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_after: Optional[int] = None,
        created_before: Optional[int] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """A Stripe customer — the entity almost every other question resolves
        through.

        "payment_methods" is the diagnosis for involuntary churn: an expired card
        (`card.exp_month`/`exp_year`) or an empty list explains most `past_due`
        subscriptions.

        Args:
            op: "list" (default) | "get" | "payment_methods" | "create" | "update".
            customer_id: REQUIRED by "get"/"payment_methods"/"update" (`cus_…`).
            email: on "list", an EXACT-match filter. On "create"/"update", the
                value to set. For partial matching use
                `stripe_search(resource="customers", query='email~"acme.com"')`.
            name/description/metadata: "create"/"update" only.
            created_after/created_before: "list" only — Unix timestamps.
            limit: "list" only, 1-100 (default 100).
            starting_after: "list" pagination cursor — the id of the LAST object
                of the previous page.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='create' to create one, op='get' to target one",
                            customer_id=customer_id, name=name, description=description,
                            metadata=metadata)
            return _run(lambda: client.list_customers(
                email=email, created=_window(created_after, created_before),
                limit=_limit(limit), starting_after=starting_after))
        if op in ("get", "payment_methods"):
            if not customer_id:
                raise _bad(f"op={op!r} requires `customer_id`")
            _refuse_ignored(op, "these fields only apply to list/create/update",
                            email=email, name=name, description=description,
                            metadata=metadata, created_after=created_after,
                            created_before=created_before, starting_after=starting_after)
            if op == "get":
                return _run(lambda: client.get_customer(customer_id))
            return _run(lambda: client.list_customer_payment_methods(
                customer_id, limit=_limit(limit)))
        if op == "create":
            _refuse_ignored(op, "a new customer does not have an id yet",
                            customer_id=customer_id, created_after=created_after,
                            created_before=created_before, starting_after=starting_after,
                            limit=limit)
            if not email and not name:
                raise _bad("op='create' requires at least `email` or `name` — a customer "
                           "with neither cannot be found afterwards.")
            return _run(lambda: client.create_customer(
                email=email, name=name, description=description, metadata=metadata))
        if op == "update":
            if not customer_id:
                raise _bad("op='update' requires `customer_id`")
            _refuse_ignored(op, "these filters only apply to op='list'",
                            created_after=created_after, created_before=created_before,
                            starting_after=starting_after, limit=limit)
            body = {k: v for k, v in dict(email=email, name=name,
                                          description=description,
                                          metadata=metadata).items() if v is not None}
            if not body:
                raise _bad("op='update' requires at least one field to modify")
            return _run(lambda: client.update_customer(customer_id, **body))
        raise _bad("op must be 'list', 'get', 'payment_methods', 'create' or 'update'")

    # ================================================================
    # Subscriptions
    # ================================================================

    @mcp.tool(annotations=LECTURE)
    def stripe_subscription(
        op: Literal["list", "get", "items"] = "list",
        subscription_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        status: Optional[Literal["active", "past_due", "trialing", "canceled",
                                 "unpaid", "paused", "incomplete",
                                 "incomplete_expired", "ended", "all"]] = None,
        price_id: Optional[str] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """Subscriptions — read-only (cancelling is not available in this
        connector; do it from the Stripe dashboard).

        Stripe has no "churning" status. Build it: `cancel_at_period_end == true`
        on active subs (voluntary, scheduled), `status == "past_due"` or
        `"unpaid"` (involuntary, payment failing), `status == "canceled"` with
        `canceled_at` in the window (already gone).

        "items" returns the subscription's lines, where price and quantity
        (seat counts) actually live for multi-product subscriptions.

        Args:
            op: "list" (default) | "get" | "items".
            subscription_id: REQUIRED by "get"/"items" (`sub_…`).
            customer_id/price_id: "list" filters.
            status: "list" filter. ⚠️ WITHOUT it Stripe returns only active and
                trialing subscriptions — cancelled ones silently disappear, which
                is exactly wrong for a churn question. Pass "all" to see everything.
            limit: 1-100 (default 100). starting_after: pagination cursor.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific subscription",
                            subscription_id=subscription_id)
            return _run(lambda: client.list_subscriptions(
                customer=customer_id, status=status, price=price_id,
                limit=_limit(limit), starting_after=starting_after))
        if not subscription_id:
            raise _bad(f"op={op!r} requires `subscription_id`")
        _refuse_ignored(op, "these filters only apply to op='list'",
                        customer_id=customer_id, status=status, price_id=price_id,
                        starting_after=starting_after)
        if op == "get":
            return _run(lambda: client.get_subscription(subscription_id))
        if op == "items":
            return _run(lambda: client.list_subscription_items(
                subscription_id, limit=_limit(limit)))
        raise _bad("op must be 'list', 'get' or 'items'")

    # ================================================================
    # Invoices — including the bounded aggregate, the real answer to "how much"
    # ================================================================

    @mcp.tool()
    def stripe_invoice(
        op: Literal["list", "get", "lines", "totals", "list_items",
                    "create_draft", "add_item", "update"] = "list",
        invoice_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        subscription_id: Optional[str] = None,
        status: Optional[Literal["draft", "open", "paid", "uncollectible", "void"]] = None,
        created_after: Optional[int] = None,
        created_before: Optional[int] = None,
        amount: Optional[int] = None,
        currency: Optional[str] = None,
        description: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        pending_items: Optional[Literal["include", "exclude"]] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """Invoices — what was billed, and the only op here that SUMS.

        **`op="totals"` is how you answer "how much did we bill last month".**
        Listing caps at 100 per page and Stripe gives no total_count, so summing
        by hand across pages is both expensive and silently truncated. "totals"
        sweeps up to 2000 invoices server-side and returns
        `{count, complete, by_currency: {eur: {total, amount_paid, amount_due}}}`.
        ⚠️ It groups BY CURRENCY and never adds currencies together — a single
        number across mixed currencies would be meaningless. If `complete` is
        false, the window held more than the sweep cap: narrow the dates and say
        so rather than reporting the partial sum as fact. Amounts are in each
        currency's smallest unit (cents).

        ⚠️ **"create_draft" only creates a DRAFT.** Nothing is sent to the
        customer and nothing is charged — finalising, sending and paying are not
        available in this connector. Verified live: without
        `pending_items="include"` the draft comes back with `total=0` and no
        lines even when items were just added, leaving the amount dangling —
        hence that default.

        Args:
            op: "list" (default) | "get" | "lines" | "totals" | "list_items" |
                "create_draft" | "add_item" | "update".
            invoice_id: REQUIRED by "get"/"lines"/"update" (`in_…`).
            customer_id: filter on "list"/"totals"/"list_items"; REQUIRED by
                "create_draft"/"add_item".
            subscription_id/status: "list"/"totals" filters.
            created_after/created_before: Unix timestamps — the window for
                "list"/"totals".
            amount: "add_item" — in the currency's SMALLEST unit (cents). A
                NEGATIVE amount is a credit/goodwill gesture.
            currency: "add_item" (e.g. "eur"). description/metadata: writes.
            pending_items: "create_draft" — "include" (default) pulls the
                customer's pending invoice items onto the invoice; "exclude"
                creates an empty draft. See the warning below.
            limit/starting_after: "list"/"list_items" pagination.
        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific invoice",
                            invoice_id=invoice_id, amount=amount, currency=currency,
                            description=description, metadata=metadata,
                            pending_items=pending_items)
            return _run(lambda: client.list_invoices(
                customer=customer_id, subscription=subscription_id, status=status,
                created=_window(created_after, created_before),
                limit=_limit(limit), starting_after=starting_after))
        if op == "totals":
            _refuse_ignored(op, "an aggregate neither targets an invoice nor writes",
                            invoice_id=invoice_id, amount=amount, currency=currency,
                            description=description, metadata=metadata,
                            pending_items=pending_items, starting_after=starting_after,
                            limit=limit)
            return _run(lambda: _totals(
                client, customer=customer_id, subscription=subscription_id,
                status=status, created=_window(created_after, created_before)))
        if op == "list_items":
            _refuse_ignored(op, "these fields do not apply to op='list_items'",
                            subscription_id=subscription_id, status=status,
                            amount=amount, currency=currency, description=description,
                            metadata=metadata, pending_items=pending_items)
            return _run(lambda: client.list_invoice_items(
                customer=customer_id, invoice=invoice_id, limit=_limit(limit),
                starting_after=starting_after))
        if op in ("get", "lines"):
            if not invoice_id:
                raise _bad(f"op={op!r} requires `invoice_id`")
            _refuse_ignored(op, "these filters only apply to op='list'/'totals'",
                            customer_id=customer_id, subscription_id=subscription_id,
                            status=status, created_after=created_after,
                            created_before=created_before, amount=amount,
                            currency=currency, metadata=metadata,
                            pending_items=pending_items)
            if op == "get":
                return _run(lambda: client.get_invoice(invoice_id))
            return _run(lambda: client.get_invoice_lines(invoice_id, limit=_limit(limit)))
        if op == "add_item":
            if not customer_id:
                raise _bad("op='add_item' requires `customer_id`")
            if amount is None or not currency:
                raise _bad("op='add_item' requires `amount` (in cents) and `currency`")
            _refuse_ignored(op, "these filters do not apply to creating a line",
                            subscription_id=subscription_id, status=status,
                            created_after=created_after, created_before=created_before,
                            pending_items=pending_items, starting_after=starting_after,
                            limit=limit)
            return _run(lambda: client.create_invoice_item(
                customer=customer_id, amount=amount, currency=currency,
                invoice=invoice_id, description=description, metadata=metadata))
        if op == "create_draft":
            if not customer_id:
                raise _bad("op='create_draft' requires `customer_id`")
            _refuse_ignored(op, "a new invoice does not have an id yet",
                            invoice_id=invoice_id, status=status,
                            created_after=created_after, created_before=created_before,
                            amount=amount, currency=currency,
                            starting_after=starting_after, limit=limit)
            return _run(lambda: client.create_invoice(
                customer=customer_id, subscription=subscription_id,
                auto_advance=False,
                pending_invoice_items_behavior=pending_items or "include",
                description=description, metadata=metadata))
        if op == "update":
            if not invoice_id:
                raise _bad("op='update' requires `invoice_id`")
            body = {k: v for k, v in dict(description=description,
                                          metadata=metadata).items() if v is not None}
            if not body:
                raise _bad("op='update' requires at least `description` or `metadata` — "
                           "a FINALIZED invoice only accepts these fields.")
            return _run(lambda: client.update_invoice(invoice_id, **body))
        raise _bad("unknown op for stripe_invoice")

    def _totals(client, **filters) -> dict:
        """Sweeps a window's invoices and sums BY CURRENCY, up to the
        cap. The `complete` flag says whether the sweep saw everything — a partial
        sum presented as the revenue would be the worst possible
        outcome."""
        by_currency: Dict[str, Dict[str, int]] = {}
        counts_by_status: Dict[str, int] = {}
        count = 0
        cursor = None
        complete = True
        for page in range(_AGGREGATE_MAX_PAGES + 1):
            if page == _AGGREGATE_MAX_PAGES:
                complete = False
                break
            batch = client.list_invoices(limit=_MAX_LIMIT, starting_after=cursor,
                                         **filters)
            rows = batch.get("data", [])
            for inv in rows:
                cur = (inv.get("currency") or "?").lower()
                acc = by_currency.setdefault(cur, {"total": 0, "amount_paid": 0,
                                                   "amount_due": 0})
                acc["total"] += inv.get("total") or 0
                acc["amount_paid"] += inv.get("amount_paid") or 0
                acc["amount_due"] += inv.get("amount_due") or 0
                st = inv.get("status") or "?"
                counts_by_status[st] = counts_by_status.get(st, 0) + 1
            count += len(rows)
            if not batch.get("has_more") or not rows:
                break
            cursor = rows[-1]["id"]
        return {
            "count": count,
            "complete": complete,
            "by_currency": by_currency,
            "count_by_status": counts_by_status,
            "note": ("Amounts in the smallest unit of each currency (cents). "
                     "Never added across currencies." if by_currency else
                     "No invoices in this window.")
            + ("" if complete else
               f" ⚠️ INCOMPLETE: more than {_AGGREGATE_MAX_PAGES * _MAX_LIMIT} invoices "
               "in the window, the sum only covers the first ones. Narrow "
               "the period."),
        }

    # ================================================================
    # Payments — intents, charges, refunds, disputes
    # ================================================================

    @mcp.tool(annotations=LECTURE)
    def stripe_payment(
        op: Literal["list_intents", "get_intent", "list_charges", "get_charge",
                    "list_refunds", "get_refund", "list_disputes",
                    "get_dispute"] = "list_charges",
        payment_intent_id: Optional[str] = None,
        charge_id: Optional[str] = None,
        refund_id: Optional[str] = None,
        dispute_id: Optional[str] = None,
        customer_id: Optional[str] = None,
        created_after: Optional[int] = None,
        created_before: Optional[int] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """Payments — read-only. Refunding is not available in this connector.

        For "why did this payment fail": the PaymentIntent's `last_payment_error`
        is authoritative (`code`, `decline_code`, `message`); on a Charge, prefer
        `outcome.seller_message`, which is written for the merchant.

        Disputes carry a hard deadline in `evidence_details.due_by`, and the money
        is already withdrawn from the balance while one is open — surface them
        proactively rather than on request.

        Args:
            op: "list_charges" (default) | "get_charge" | "list_intents" |
                "get_intent" | "list_refunds" | "get_refund" | "list_disputes" |
                "get_dispute".
            payment_intent_id/charge_id/refund_id/dispute_id: REQUIRED by the
                matching "get_*" op; on "list_refunds"/"list_disputes",
                `charge_id`/`payment_intent_id` narrow the list.
            customer_id, created_after/created_before, limit, starting_after:
                list filters.

        """
        client = _client()
        window = _window(created_after, created_before)
        if op == "list_charges":
            return _run(lambda: client.list_charges(
                customer=customer_id, payment_intent=payment_intent_id, created=window,
                limit=_limit(limit), starting_after=starting_after))
        if op == "list_intents":
            return _run(lambda: client.list_payment_intents(
                customer=customer_id, created=window, limit=_limit(limit),
                starting_after=starting_after))
        if op == "list_refunds":
            return _run(lambda: client.list_refunds(
                charge=charge_id, payment_intent=payment_intent_id, created=window,
                limit=_limit(limit), starting_after=starting_after))
        if op == "list_disputes":
            return _run(lambda: client.list_disputes(
                charge=charge_id, payment_intent=payment_intent_id, created=window,
                limit=_limit(limit), starting_after=starting_after))
        targets = {"get_intent": (payment_intent_id, "payment_intent_id", client.get_payment_intent),
                   "get_charge": (charge_id, "charge_id", client.get_charge),
                   "get_refund": (refund_id, "refund_id", client.get_refund),
                   "get_dispute": (dispute_id, "dispute_id", client.get_dispute)}
        if op in targets:
            value, name, fn = targets[op]
            if not value:
                raise _bad(f"op={op!r} requires `{name}`")
            _refuse_ignored(op, "these filters only apply to the list ops",
                            customer_id=customer_id, created_after=created_after,
                            created_before=created_before, starting_after=starting_after,
                            limit=limit)
            return _run(lambda: fn(value))
        raise _bad("unknown op for stripe_payment")

    # ================================================================
    # Balance, balance transactions, payouts
    # ================================================================

    @mcp.tool(annotations=LECTURE)
    def stripe_balance(
        op: Literal["get", "transactions", "transaction", "payouts", "payout"] = "get",
        transaction_id: Optional[str] = None,
        payout_id: Optional[str] = None,
        currency: Optional[str] = None,
        created_after: Optional[int] = None,
        created_before: Optional[int] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """Money actually held and moved — as opposed to money billed.

        "transactions" is the right answer to "how much did we actually MAKE",
        because each row carries `fee` and `net` — invoices only know what was
        billed. To see what was inside a payout, pass its id as `payout_id`
        with op="transactions".

        Creating, cancelling or reversing a payout is not available here.

        Args:
            op: "get" (default, the current balance) | "transactions" |
                "transaction" | "payouts" | "payout".
            transaction_id/payout_id: REQUIRED by "transaction"/"payout".
            currency, created_after/created_before, limit, starting_after:
                filters for "transactions"/"payouts".

        """
        client = _client()
        if op == "get":
            _refuse_ignored(op, "the current balance takes no filter",
                            transaction_id=transaction_id, payout_id=payout_id,
                            currency=currency, created_after=created_after,
                            created_before=created_before, starting_after=starting_after,
                            limit=limit)
            return _run(lambda: client.balance())
        window = _window(created_after, created_before)
        if op == "transactions":
            return _run(lambda: client.list_balance_transactions(
                currency=currency, payout=payout_id, created=window,
                limit=_limit(limit), starting_after=starting_after))
        if op == "payouts":
            return _run(lambda: client.list_payouts(
                created=window, limit=_limit(limit), starting_after=starting_after))
        if op == "transaction":
            if not transaction_id:
                raise _bad("op='transaction' requires `transaction_id`")
            return _run(lambda: client.get_balance_transaction(transaction_id))
        if op == "payout":
            if not payout_id:
                raise _bad("op='payout' requires `payout_id`")
            return _run(lambda: client.get_payout(payout_id))
        raise _bad("unknown op for stripe_balance")

    # ================================================================
    # Catalog — products, prices, discounts
    # ================================================================

    @mcp.tool()
    def stripe_catalog(
        op: Literal["list_products", "get_product", "create_product", "update_product",
                    "list_prices", "get_price", "create_price", "update_price",
                    "list_coupons", "get_coupon", "create_coupon", "update_coupon",
                    "list_promotion_codes", "get_promotion_code", "create_promotion_code",
                    "update_promotion_code"] = "list_products",
        product_id: Optional[str] = None,
        price_id: Optional[str] = None,
        coupon_id: Optional[str] = None,
        promotion_code_id: Optional[str] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        tax_code: Optional[str] = None,
        active: Optional[bool] = None,
        unit_amount: Optional[int] = None,
        currency: Optional[str] = None,
        recurring_interval: Optional[Literal["day", "week", "month", "year"]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        code: Optional[str] = None,
        customer_id: Optional[str] = None,
        percent_off: Optional[float] = None,
        amount_off: Optional[int] = None,
        duration: Optional[Literal["once", "repeating", "forever"]] = None,
        duration_in_months: Optional[int] = None,
        max_redemptions: Optional[int] = None,
        redeem_by: Optional[int] = None,
        expires_at: Optional[int] = None,
        restrictions: Optional[Dict[str, Any]] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """The catalogue: products, prices, coupons, promotion codes.

        ⚠️ **A Stripe price is immutable on its amount.** "Change the price" means
        creating a NEW price and deactivating the old one — `update_price` only
        touches `active`, `metadata` and `nickname`. Say that rather than
        appearing to edit an amount.

        A **coupon** is the discount RULE (percent_off/amount_off, how long it
        applies); a **promotion code** is the TEXT a customer actually types at
        checkout, and always points at one coupon. Create the coupon first, then
        one or more promotion codes for it — several codes ("LAUNCH20",
        "PARTNER20") can share the same underlying coupon/discount.

        Deleting a product, price or coupon is not available here (and a Coupon
        has no `active` flag to toggle); revoke a promotion code instead
        (`update_promotion_code`, `active=false`) — the redemption rule stays on
        file but nothing more can be redeemed with it.

        ⚠️ Verified live 2026-08-23: a promotion code's `expires_at` cannot be
        LATER than its coupon's `redeem_by` — Stripe rejects it outright,
        naming both timestamps. And on a promotion code object, the coupon it
        applies is under `promotion.coupon` (with `promotion.type == "coupon"`),
        NOT a top-level `coupon` field — that flat shape was retired.

        Args:
            op: "list_products" (default) and the other fifteen verbs.
            product_id: REQUIRED by "get_product"/"update_product"/"create_price";
                filters "list_prices".
            price_id: REQUIRED by "get_price"/"update_price".
            coupon_id: REQUIRED by "get_coupon"/"update_coupon", and by
                "create_promotion_code" (the discount rule the new code applies).
            promotion_code_id: REQUIRED by "get_promotion_code"/"update_promotion_code".
            name: REQUIRED by "create_product". On "create_coupon"/"update_coupon",
                what the customer sees on their invoice (optional).
            description/metadata: writes, several ops.
            tax_code: on "create_product"/"update_product" — e.g. "txcd_10000000"
                (general services). Needed before a payment link can be created
                on accounts eligible for managed payments (verified live).
            active: filter on lists; on "update_product"/"update_price",
                `active=false` retires it from sale without deleting anything; on
                "update_promotion_code", `active=false` revokes the code.
            unit_amount: "create_price", in cents. currency: "create_price",
                and "create_coupon" when paired with `amount_off`.
            recurring_interval: "create_price" — omit for a one-off price.
            code: "list_promotion_codes" filter; on "create_promotion_code", the
                literal text the customer types (omit to let Stripe generate one).
            customer_id: "create_promotion_code" — restricts the code to one
                customer (omit for account-wide).
            percent_off/amount_off: "create_coupon" — exactly ONE of the two
                (`amount_off` needs `currency` alongside it).
            duration: REQUIRED by "create_coupon" — "once" | "repeating" | "forever".
            duration_in_months: "create_coupon", REQUIRED when `duration="repeating"`.
            max_redemptions: "create_coupon"/"create_promotion_code" — caps total uses.
            redeem_by: "create_coupon" — Unix timestamp after which the coupon
                itself can no longer be attached to a new subscription/order.
            expires_at: "create_promotion_code" — Unix timestamp after which this
                CODE stops working (the coupon behind it can outlive it).
            restrictions: "create_promotion_code" — e.g.
                `{"first_time_transaction": true}` or `{"minimum_amount": 5000,
                "minimum_amount_currency": "eur"}`.
            limit/starting_after: list pagination.
        """
        client = _client()
        if op == "list_products":
            return _run(lambda: client.list_products(
                active=active, limit=_limit(limit), starting_after=starting_after))
        if op == "list_prices":
            return _run(lambda: client.list_prices(
                product=product_id, active=active, currency=currency,
                limit=_limit(limit), starting_after=starting_after))
        if op == "list_coupons":
            return _run(lambda: client.list_coupons(
                limit=_limit(limit), starting_after=starting_after))
        if op == "list_promotion_codes":
            return _run(lambda: client.list_promotion_codes(
                code=code, coupon=coupon_id, active=active, limit=_limit(limit),
                starting_after=starting_after))
        if op == "get_coupon":
            if not coupon_id:
                raise _bad("op='get_coupon' requires `coupon_id`")
            return _run(lambda: client.get_coupon(coupon_id))
        if op == "create_coupon":
            if not duration:
                raise _bad("op='create_coupon' requires `duration` "
                           "('once', 'repeating' or 'forever')")
            if duration == "repeating" and duration_in_months is None:
                raise _bad("op='create_coupon' with duration='repeating' also requires "
                           "`duration_in_months`")
            if (percent_off is None) == (amount_off is None):
                raise _bad("op='create_coupon' requires `percent_off` OR `amount_off` "
                           "— one of the two, never both, never neither")
            if amount_off is not None and not currency:
                raise _bad("op='create_coupon' with `amount_off` also requires `currency`")
            _refuse_ignored(op, "`code` is the text of a promotion_code, not of a coupon "
                            "— create the coupon then use op='create_promotion_code' to set "
                            "the text the customer types", code=code)
            body = {k: v for k, v in dict(
                percent_off=percent_off,
                amount_off=amount_off, currency=currency if amount_off is not None else None,
                duration=duration, duration_in_months=duration_in_months, name=name,
                max_redemptions=max_redemptions, redeem_by=redeem_by,
                metadata=metadata).items() if v is not None}
            return _run(lambda: client.create_coupon(**body))
        if op == "update_coupon":
            if not coupon_id:
                raise _bad("op='update_coupon' requires `coupon_id`")
            body = {k: v for k, v in dict(name=name, metadata=metadata).items()
                    if v is not None}
            if not body:
                raise _bad("op='update_coupon' requires `name` or `metadata` — a Stripe "
                           "coupon has nothing else that can be modified (amount/duration frozen, "
                           "like the amount of a price)")
            return _run(lambda: client.update_coupon(coupon_id, **body))
        if op == "get_promotion_code":
            if not promotion_code_id:
                raise _bad("op='get_promotion_code' requires `promotion_code_id`")
            return _run(lambda: client.get_promotion_code(promotion_code_id))
        if op == "create_promotion_code":
            if not coupon_id:
                raise _bad("op='create_promotion_code' requires `coupon_id` — the coupon "
                           "(discount rule) that this code applies. Find it with "
                           "op='list_coupons', or create it first with op='create_coupon'.")
            _refuse_ignored(op, "a new code is active by default — use "
                            "op='update_promotion_code' to deactivate it afterwards",
                            active=active)
            body = {k: v for k, v in dict(
                coupon=coupon_id, code=code, customer=customer_id,
                max_redemptions=max_redemptions, expires_at=expires_at,
                restrictions=restrictions, metadata=metadata).items() if v is not None}
            return _run(lambda: client.create_promotion_code(**body))
        if op == "update_promotion_code":
            if not promotion_code_id:
                raise _bad("op='update_promotion_code' requires `promotion_code_id`")
            body = {k: v for k, v in dict(active=active, metadata=metadata).items()
                    if v is not None}
            if not body:
                raise _bad("op='update_promotion_code' requires `active` or `metadata` — "
                           "nothing else can be modified after creation (the code, the "
                           "linked coupon and the restrictions are frozen)")
            return _run(lambda: client.update_promotion_code(promotion_code_id, **body))
        if op == "get_product":
            if not product_id:
                raise _bad("op='get_product' requires `product_id`")
            return _run(lambda: client.get_product(product_id))
        if op == "get_price":
            if not price_id:
                raise _bad("op='get_price' requires `price_id`")
            return _run(lambda: client.get_price(price_id))
        if op == "create_product":
            if not name:
                raise _bad("op='create_product' requires `name`")
            return _run(lambda: client.create_product(
                name=name, description=description, tax_code=tax_code,
                active=active, metadata=metadata))
        if op == "update_product":
            if not product_id:
                raise _bad("op='update_product' requires `product_id`")
            body = {k: v for k, v in dict(name=name, description=description,
                                          tax_code=tax_code, active=active,
                                          metadata=metadata).items() if v is not None}
            if not body:
                raise _bad("op='update_product' requires at least one field to modify")
            return _run(lambda: client.update_product(product_id, **body))
        if op == "create_price":
            if not product_id or unit_amount is None or not currency:
                raise _bad("op='create_price' requires `product_id`, `unit_amount` "
                           "(in cents) and `currency`")
            recurring = {"interval": recurring_interval} if recurring_interval else None
            return _run(lambda: client.create_price(
                product=product_id, unit_amount=unit_amount, currency=currency,
                recurring=recurring, metadata=metadata))
        if op == "update_price":
            if not price_id:
                raise _bad("op='update_price' requires `price_id`")
            if unit_amount is not None:
                raise _bad("The AMOUNT of a Stripe price is immutable: create a new "
                           "price (op='create_price') then deactivate the old one "
                           "(op='update_price', active=false).")
            body = {k: v for k, v in dict(active=active,
                                          metadata=metadata).items() if v is not None}
            if not body:
                raise _bad("op='update_price' requires `active` or `metadata`")
            return _run(lambda: client.update_price(price_id, **body))
        raise _bad("unknown op for stripe_catalog")

    # ================================================================
    # Hosted collection — payment links & Checkout sessions
    # ================================================================

    @mcp.tool()
    def stripe_checkout(
        op: Literal["list_links", "get_link", "link_line_items", "create_link",
                    "update_link", "list_sessions", "get_session",
                    "session_line_items"] = "list_links",
        payment_link_id: Optional[str] = None,
        session_id: Optional[str] = None,
        price_id: Optional[str] = None,
        quantity: Optional[int] = None,
        customer_id: Optional[str] = None,
        status: Optional[Literal["open", "complete", "expired"]] = None,
        active: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
        created_metadata: Optional[Dict[str, Any]] = None,
        max_uses: Optional[int] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """Hosted collection — payment links and Checkout sessions.

        **"create_link" is the safest way to collect money here**: it returns a
        reusable URL to a page hosted by Stripe, so no card number ever touches
        oto, and nobody is charged until a human opens it and pays.

        ⚠️ Creating a link can fail with "the product tax code is missing" on
        accounts eligible for managed payments (verified live). Fix it on the
        product: `stripe_catalog(op="update_product", product_id=…,
        tax_code="txcd_10000000")`.

        Args:
            op: "list_links" (default) | "get_link" | "link_line_items" |
                "create_link" | "update_link" | "list_sessions" | "get_session" |
                "session_line_items".
            payment_link_id: REQUIRED by "get_link"/"link_line_items"/"update_link".
            session_id: REQUIRED by "get_session"/"session_line_items".
            price_id: REQUIRED by "create_link" — from `stripe_catalog`.
            quantity: "create_link" (default 1).
            active: filter on "list_links"; on "update_link", `active=false`
                switches a link off without deleting it.
            customer_id/status: "list_sessions" filters — `status="open"` are
                abandoned checkouts.
            metadata: on the link itself. Stripe does NOT copy it onto the
                subscription / payment created at checkout — use
                `created_metadata` for that.
            created_metadata: "create_link" — metadata set on what each checkout
                creates: the subscription for a recurring price, the payment
                intent otherwise. Reads the price first (key needs Prices: Read).
            max_uses: "create_link" — the link deactivates itself after N
                completed checkouts (1 = paid once, by whoever opens it first).
            limit, starting_after: as elsewhere.

        """
        if op != "create_link":
            _refuse_ignored(op, "these fields only apply to op='create_link'",
                            price_id=price_id, quantity=quantity,
                            created_metadata=created_metadata, max_uses=max_uses)
        client = _client()
        if op == "list_links":
            return _run(lambda: client.list_payment_links(
                active=active, limit=_limit(limit), starting_after=starting_after))
        if op == "list_sessions":
            return _run(lambda: client.list_checkout_sessions(
                customer=customer_id, status=status, limit=_limit(limit),
                starting_after=starting_after))
        if op == "get_link":
            if not payment_link_id:
                raise _bad("op='get_link' requires `payment_link_id`")
            return _run(lambda: client.get_payment_link(payment_link_id))
        if op == "link_line_items":
            if not payment_link_id:
                raise _bad("op='link_line_items' requires `payment_link_id`")
            return _run(lambda: client.get_payment_link_line_items(
                payment_link_id, limit=_limit(limit)))
        if op == "get_session":
            if not session_id:
                raise _bad("op='get_session' requires `session_id`")
            return _run(lambda: client.get_checkout_session(session_id))
        if op == "session_line_items":
            if not session_id:
                raise _bad("op='session_line_items' requires `session_id`")
            return _run(lambda: client.get_checkout_session_line_items(
                session_id, limit=_limit(limit)))
        if op == "create_link":
            if not price_id:
                raise _bad("op='create_link' requires `price_id` — list them with "
                           "stripe_catalog(op='list_prices').")
            items: List[Dict[str, Any]] = [{"price": price_id, "quantity": quantity or 1}]
            if max_uses is not None and (isinstance(max_uses, bool) or max_uses < 1):
                raise _bad("`max_uses` must be an integer ≥ 1")
            body: Dict[str, Any] = {}
            if metadata:
                body["metadata"] = metadata
            if created_metadata:
                price = _run(lambda: client.get_price(price_id))
                ptype = price.get("type") if isinstance(price, dict) else None
                if ptype not in ("recurring", "one_time"):
                    raise _bad(f"price {price_id!r}: unreadable type ({ptype!r}) — "
                               "cannot tell where to set `created_metadata`.")
                target = "subscription_data" if ptype == "recurring" else "payment_intent_data"
                body[target] = {"metadata": created_metadata}
            if max_uses is not None:
                body["restrictions"] = {"completed_sessions": {"limit": max_uses}}
            return _run(lambda: client.create_payment_link(items, **body))
        if op == "update_link":
            if not payment_link_id:
                raise _bad("op='update_link' requires `payment_link_id`")
            body = {k: v for k, v in dict(active=active,
                                          metadata=metadata).items() if v is not None}
            if not body:
                raise _bad("op='update_link' requires `active` or `metadata`")
            return _run(lambda: client.update_payment_link(payment_link_id, **body))
        raise _bad("unknown op for stripe_checkout")

    # ================================================================
    # Account activity
    # ================================================================

    @mcp.tool(annotations=LECTURE)
    def stripe_event(
        op: Literal["list", "get", "webhook_endpoints"] = "list",
        event_id: Optional[str] = None,
        type: Optional[str] = None,
        created_after: Optional[int] = None,
        created_before: Optional[int] = None,
        limit: Optional[int] = None,
        starting_after: Optional[str] = None,
    ) -> object:
        """The account's activity feed — every state change, newest first, with
        the affected object embedded in `data.object`.

        ⚠️ **Stripe keeps events for 30 DAYS only.** Any question about a longer
        period ("this quarter", "last year") will silently come back short here —
        use `stripe_invoice(op="totals")` or the list ops, which read the objects
        themselves and have no such window.

        "webhook_endpoints" lists which integrations are listening, for
        diagnosis. Creating or deleting them is not available here: deleting one
        silently breaks whatever automation depended on it.

        Args:
            op: "list" (default) | "get" | "webhook_endpoints".
            event_id: REQUIRED by "get" (`evt_…`).
            type: "list" filter, wildcards allowed — `invoice.*`,
                `invoice.payment_failed`, `customer.subscription.deleted`.
            created_after/created_before, limit, starting_after: as elsewhere.

        """
        client = _client()
        if op == "list":
            _refuse_ignored(op, "use op='get' for a specific event", event_id=event_id)
            return _run(lambda: client.list_events(
                type=type, created=_window(created_after, created_before),
                limit=_limit(limit), starting_after=starting_after))
        if op == "get":
            if not event_id:
                raise _bad("op='get' requires `event_id`")
            _refuse_ignored(op, "these filters only apply to op='list'",
                            type=type, created_after=created_after,
                            created_before=created_before, starting_after=starting_after)
            return _run(lambda: client.get_event(event_id))
        if op == "webhook_endpoints":
            _refuse_ignored(op, "the endpoint list does not take these filters",
                            event_id=event_id, type=type, created_after=created_after,
                            created_before=created_before)
            return _run(lambda: client.list_webhook_endpoints(limit=_limit(limit)))
        raise _bad("op must be 'list', 'get' or 'webhook_endpoints'")
