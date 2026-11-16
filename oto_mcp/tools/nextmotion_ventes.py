"""Nextmotion — sales: quotes, invoices (and credit notes), payments, revenue
statistics, and a patient's financial totals.

Sibling module of `nextmotion.py` (cf. `Connector.modules`). Six tools, because their
parameters do not overlap (ADR 0047):

- `nextmotion_quote` — list | get | update | delete | validate. A quote is only CREATED
  under a consultation (medical): no creation here.
- `nextmotion_invoice` — list | get | update | validate | pay | credit_note ; invoice
  period filter applied TOOL-SIDE → `nextmotion_periode` (not on quotes:
  `OApiQuote` has no `invoiced_time`, its `issued_time` is nullable). Neither creation
  (under a consultation only) nor deletion (an accounting document is corrected by
  a credit note);
- `nextmotion_payment` — list | get | update. A payment EMBEDS its whole invoice,
  hence its patient: the invoice goes through the same allowlist as
  `nextmotion_invoice`. A payment cannot be deleted (accounting document). The list
  filters by payment day (`paid_from` / `paid_to`, applied by Nextmotion), and
  `with_practitioner` names, on each invoice line, the practitioner of its act — read
  once per treatment within the call (see `_PRACTITIONER_NOTE`) — and splits a payment
  that settles its invoice between them (`nextmotion_ventilation`).
- `nextmotion_statistics` — `kind` x a period: clinic aggregates, no
  patient. `meta` (free-form object, not described by the spec) and `label_field` (undocumented
  parameter, which could change what the labels name) are not served.
- `nextmotion_package_usage` — the sessions of ONE patient's packages: how many were
  consumed, counted against which quote or invoice, next to the quantities sold.
  Administrative counters only (ids, quantities, dates, catalogue names): the care
  rows are reduced by the client itself, no note, zone or free text ever leaves it.
- `nextmotion_patient_stats` — the totals of ONE patient designated by their id: quotes,
  invoiced, paid, credit notes, refunds, first and last visit dates. Nothing that
  identifies them; neither the opening date of their file nor their photo activity (the life of the
  medical file).

Every write has `dry_run=True` by default and its `data` goes through the input allowlist
(`nextmotion_entrees`); its response goes back through the resource allowlist.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP

from . import nextmotion_periode as periode
from . import nextmotion_ventilation as ventilation
from ..mcp_errors import McpError
from .nextmotion_entrees import (_IN_CREDIT_NOTE, _IN_INVOICE, _IN_INVOICE_VALIDATE,
                                 _IN_PAY, _IN_PAYMENT, _IN_QUOTE, _IN_QUOTE_VALIDATE)
from .nextmotion_garde import (Kind, Write, _bad, _client, _need, _paging, _refuse_ignored,
                               _run, _serve_write, _uuid)
from .nextmotion_socle import (_CHART, _CREDIT_NOTE, _PATIENT_STATS, _PAYMENT, _WITHHELD,
                               _invoice, _one, _page, _quote, _shape)

_payment = _shape(_PAYMENT)
_chart = _shape(_CHART)
_patient_stats = _shape(_PATIENT_STATS)

#: The patient's appointments read to count package sessions: one call per
#: appointment, so a bound — said in the answer when reached.
_USAGE_MAX_APPOINTMENTS = 300
_USAGE_NOTE = (
    "consumed = sessions Nextmotion counted against this quote or invoice, by treatment "
    "type and pricing. Nextmotion does not say which LINE a session decrements: compare "
    "each group with the sold lines of the same document by their name. A group with "
    "neither quote nor invoice was consumed outside any sale.")


def _all_pages(fetch, cap: int) -> tuple:
    """Every row of a paginated read, up to `cap`; (rows, truncated)."""
    rows, offset = [], 0
    while True:
        env = _run(lambda: fetch(offset))
        data = env.get("data") if isinstance(env, dict) else None
        if not isinstance(data, list):
            raise _bad("unexpected Nextmotion answer: no `data` list.")
        rows += data
        if len(rows) >= cap:
            return rows[:cap], len(rows) > cap or env.get("next") is not None
        if env.get("next") is None or not data:
            return rows, False
        offset += len(data)


def _package_usage(c, clinic_id: str, patient_id: str) -> dict:
    from oto.tools.nextmotion import BILLED_COUNT_FIELDS, consumed_by_billing

    appointments, truncated = _all_pages(
        lambda o: c.list_appointments(clinic_id, patient_id=patient_id, limit=100,
                                      offset=o), _USAGE_MAX_APPOINTMENTS)
    rows = []
    for a in appointments:
        got, _ = _all_pages(lambda o, i=a.get("id"): c.list_appointment_billed_counts(
            i, limit=100, offset=o), 10_000)
        # The client already cut each row down; cut again here, so that what this tool
        # serves does not hang on a library promise alone.
        rows += [{k: r.get(k) for k in BILLED_COUNT_FIELDS} for r in got
                 if isinstance(r, dict)]
    groups = _run(lambda: consumed_by_billing(rows))
    names = {}
    for t in {g["treatment_type"] for g in groups if g["treatment_type"]}:
        env = _run(lambda t=t: c.get_treatment_type(t))
        data = env.get("data") if isinstance(env, dict) else None
        names[t] = data.get("name") if isinstance(data, dict) else None
    sold = {}
    for kind, key, read, shape, lines in (
            ("invoice", "invoice", c.get_invoice, _invoice, "invoiced_treatments"),
            ("quote", "quote", c.get_quote, _quote, "quoted_treatments")):
        for doc_id in sorted({g[key] for g in groups if g[key]}):
            doc = shape((_run(lambda d=doc_id: read(d)) or {}).get("data") or {})
            sold[doc_id] = {
                "kind": kind, "number": doc.get("number"), "status": doc.get("status"),
                "issued_time": doc.get("issued_time"),
                "lines": [{"name": li.get("name"), "quantity": li.get("quantity")}
                          for li in doc.get(lines) or []]}
    for g in groups:
        g["treatment_type_name"] = names.get(g["treatment_type"])
    return {"patient_id": patient_id, "appointments_read": len(appointments),
            "truncated": truncated, "consumed": groups, "sold": sold,
            "note": _USAGE_NOTE}

_PRACTITIONER_NOTE = (
    "practitioner = the practitioner of the act of each invoice line, read from its "
    "treatment; each line keeps its own price, the payment's amounts stay the "
    "payment's (its split is in `split`). `status`: ok | none (Nextmotion names no "
    "practitioner on the treatment) | no_treatment (the line names no treatment) | "
    "unavailable (the read failed, `reason` says why — the line's practitioner is "
    "unknown, not absent).")


def _practitioner(c, line: dict, cache: dict) -> dict:
    """The practitioner of ONE invoice line, read once per treatment within the call
    (`cache`). A failed read is said on the line, never turned into an empty value."""
    treatment_id = line.get("treatment")
    if treatment_id is None:
        return {"status": "no_treatment", "reason": "this invoice line names no treatment."}
    if not isinstance(treatment_id, str):
        return {"status": "unavailable",
                "reason": f"unexpected treatment reference on the line: {treatment_id!r}."}
    if treatment_id not in cache:
        try:
            env = _run(lambda: c.get_treatment_provider(treatment_id))
        except McpError as e:
            cache[treatment_id] = {"status": "unavailable", "reason": e.error.message}
        else:
            provider = env["data"]["provider"]
            cache[treatment_id] = (
                {"status": "none",
                 "reason": "Nextmotion names no practitioner on this treatment."}
                if provider is None else
                {"status": "ok", "id": provider.get("id"),
                 "first_name": provider.get("first_name"),
                 "last_name": provider.get("last_name")})
    return cache[treatment_id]


#: Payments of one invoice read to know whether a payment settles it.
_INVOICE_PAYMENTS_MAX = 500


def _invoice_payments(c, clinic_id: str, invoice_id: Any, cache: dict) -> tuple:
    """All the payments of one invoice, read once per invoice within the call:
    (rows, truncated) — or (None, reason) when the read failed."""
    if invoice_id not in cache:
        if not isinstance(invoice_id, str):
            cache[invoice_id] = (None, f"unexpected invoice reference: {invoice_id!r}.")
        else:
            try:
                cache[invoice_id] = _all_pages(lambda o: c.list_payments(
                    clinic_id, invoice_id=invoice_id, limit=100, offset=o),
                    _INVOICE_PAYMENTS_MAX)
            except McpError as e:
                cache[invoice_id] = (None, e.error.message)
    return cache[invoice_id]


def _split(c, clinic_id: str, raw: dict, practitioners: list, cache: dict) -> dict:
    invoice = raw.get("invoice") if isinstance(raw.get("invoice"), dict) else {}
    rows, truncated = _invoice_payments(c, clinic_id, invoice.get("id"), cache)
    if rows is None:
        try:
            own = str(ventilation.amount(raw).quantize(ventilation.CENT))
        except ventilation.Unreadable as e:
            return {"status": "undetermined", "reason": str(e), "amount": None}
        return {"status": "undetermined", "amount": own,
                "reason": f"the invoice's payments could not be read: {truncated}"}
    return ventilation.ventilate(raw, practitioners, rows, truncated)


def _payment_with_practitioners(c, clinic_id: str):
    """The payment allowlist, plus the practitioner of each invoice line and the
    payment's `split`. The caches live as long as the returned shape: one tool call;
    `shape.splits` keeps every split served, for the totals."""
    cache: dict = {}
    paid: dict = {}

    def shape(raw):
        out = _payment(raw)
        if not isinstance(raw, dict) or not isinstance(out, dict):
            return out
        invoice = raw.get("invoice") if isinstance(raw.get("invoice"), dict) else {}
        lines = [li for li in invoice.get("invoiced_treatments") or []
                 if isinstance(li, dict)]
        practitioners = [_practitioner(c, line, cache) for line in lines]
        shaped = out.get("invoice")
        if isinstance(shaped, dict):
            for who, served in zip(practitioners, shaped.get("invoiced_treatments") or []):
                served["practitioner"] = who
        out["split"] = _split(c, clinic_id, raw, practitioners, paid)
        shape.splits.append((raw.get("id"), out["split"]))
        return out
    shape.keys = _payment.keys + ("split",)
    shape.splits = []
    return shape


def _unavailable(rows: list) -> int:
    return sum(1 for r in rows if isinstance(r, dict)
               for li in ((r.get("invoice") or {}).get("invoiced_treatments") or [])
               if (li.get("practitioner") or {}).get("status") == "unavailable")


def _practitioners_said(out: dict, key: str, shape) -> dict:
    rows = out[key] if isinstance(out[key], list) else [out[key]]
    out["practitioners_unavailable"] = _unavailable(rows)
    out["practitioner_note"] = _PRACTITIONER_NOTE
    out["split_note"] = ventilation.SPLIT_NOTE
    if key == "payments":
        out["totals"] = ventilation.totals(shape.splits)
    return out


def _invoice_of_credit_note(c, _, body):
    """The preview of a credit note: the invoice it points to, if it points to one."""
    invoice_id = (body or {}).get("invoice")
    if invoice_id is None:
        return {}
    _uuid("credit_note", "data.invoice", invoice_id)
    return _one(_run(lambda: c.get_invoice(invoice_id)), "invoice", _invoice)


_QUOTES = {"quote": Kind(
    "quotes", _quote, lire=lambda c, i: c.get_quote(i), withheld=_WITHHELD,
    writes={"update": Write(lambda c, i, b: c.update_quote(i, body=b), "item", _IN_QUOTE),
            "delete": Write(lambda c, i, b: c.delete_quote(i)),
            "validate": Write(lambda c, i, b: c.validate_quote(i, body=b), "item",
                              _IN_QUOTE_VALIDATE, optional_body=True)})}
_INVOICES = {"invoice": Kind(
    "invoices", _invoice, lire=lambda c, i: c.get_invoice(i), withheld=_WITHHELD,
    writes={"update": Write(lambda c, i, b: c.update_invoice(i, body=b), "item",
                            _IN_INVOICE),
            "validate": Write(lambda c, i, b: c.validate_invoice(i, body=b), "item",
                              _IN_INVOICE_VALIDATE, optional_body=True),
            "pay": Write(lambda c, i, b: c.pay_invoice(i, body=b), "item", _IN_PAY),
            "credit_note": Write(lambda c, cid, b: c.create_credit_note(cid, body=b),
                                 "clinic", _IN_CREDIT_NOTE, ("patient", "items"),
                                 current=_invoice_of_credit_note, key="credit_note",
                                 shape=_shape(_CREDIT_NOTE))})}
_PAYMENTS = {"payment": Kind(
    "payments", _payment, lire=lambda c, i: c.get_payment(i), withheld=_WITHHELD,
    writes={"update": Write(lambda c, i, b: c.update_payment(i, body=b), "item",
                            _IN_PAYMENT)})}

_STATISTICS = {
    "appointment_income": lambda c, cid, **kw: c.get_appointment_income_statistics(cid, **kw),
    "treatment_types": lambda c, cid, **kw: c.list_treatment_type_statistics(cid, **kw),
    "treatment_types_income": lambda c, cid, **kw: c.list_treatment_type_income_statistics(
        cid, **kw),
}


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    def nextmotion_quote(
        op: Literal["list", "get", "update", "delete", "validate"] = "list",
        clinic_id: Optional[str] = None,
        quote_id: Optional[str] = None,
        patient_id: Optional[str] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Quotes (devis) of a Nextmotion clinic — number, status, lines (sub-pricings,
        markup, accounting codes), totals, follow-up and channel. Health data, titles
        and free text are withheld; the patient is served by ID ONLY — resolve it with
        `nextmotion_patient(op="get")` when needed.

        Status codes: 1 NEW, 2 QUOTED, 3 ACCEPTED, 4 REJECTED, 5 INVOICED, 6 ACQUAINTED.

        `op`: "list" (default, `clinic_id`, optional `patient_id`) | "get" | "update"
        (`data`: rebate, title, free text, dates, follow-up fields…) | "validate" (draft
        → validated quote, optional `data`) | "delete" — all by `quote_id`. Lines
        cannot be edited here (each carries a clinical treatment id), nor can a quote
        be created (Nextmotion creates it under a consultation). `data` takes the fields the
        Nextmotion spec accepts for the op; any other field is refused.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act.

        Args:
            op: list (default) | get | update | delete | validate.
            clinic_id: op="list".
            quote_id: every op but list.
            patient_id: op="list" — only this patient's quotes.
            data: op="update"/"validate" — the fields to send.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        if op in ("update", "delete", "validate"):
            return _serve_write(
                _QUOTES, "quote", op, client=_client, clinic_id=clinic_id,
                item_id=quote_id, data=data, dry_run=dry_run, item_name="quote_id",
                unused={"patient_id": patient_id, "limit": limit, "offset": offset,
                        "fields": fields})
        _refuse_ignored(op, data=data, dry_run=dry_run)
        c = _client()
        if op == "list":
            _need(op, clinic_id=clinic_id)
            _refuse_ignored(op, quote_id=quote_id)
            return _page(_run(lambda: c.list_quotes(
                clinic_id, patient_id=patient_id, **_paging(limit, offset))),
                "quotes", _quote, fields=fields)
        if op == "get":
            _need(op, quote_id=quote_id)
            _refuse_ignored(op, clinic_id=clinic_id, patient_id=patient_id,
                            limit=limit, offset=offset, fields=fields)
            return _one(_run(lambda: c.get_quote(quote_id)), "quote", _quote)
        raise _bad("op must be 'list', 'get', 'update', 'delete' or 'validate'.")

    @mcp.tool()
    def nextmotion_invoice(
        op: Literal["list", "get", "update", "validate", "pay", "credit_note"] = "list",
        clinic_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
        invoiced_from: Optional[str] = None,
        invoiced_to: Optional[str] = None,
        max_pages: Optional[int] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Invoices of a Nextmotion clinic — number, status, lines, totals, payment
        methods. Health data, titles and free text are withheld; the patient is served
        by ID ONLY — resolve it with `nextmotion_patient(op="get")` when needed.

        Status codes: 2 NEW, 3 VALIDATED, 4 NEW_ONLY_DEPOSITS, 5 NEW_ISSUED,
        6 NEW_DEPOSITS_PAID, 9 ONLY_DEPOSITS, 10 ISSUED, 11 DEPOSITS_PAID, 12 NEUTRALIZED.

        Period filter (op="list"): `invoiced_from` / `invoiced_to` (YYYY-MM-DD, inclusive,
        on `invoiced_time`). Nextmotion neither filters nor orders, so the tool reads
        EVERY page (100 per call, up to `max_pages`) and says `pages_lues`,
        `factures_parcourues`, `complet`. `complet: false` = PARTIAL: call again with
        `offset=offset_suivant` and the same period. With a period, `limit` is refused
        and `offset` is where the scan starts. No consumables nor lots per invoice
        (`nextmotion_product` is not linked to invoices). Payments:
        `nextmotion_payment(invoice_id=…)`.

        `op` writes: "update" (`invoice_id` + `data`: rebate, title, free text, dates) |
        "validate" (draft → validated, optional `issued_time` / `invoiced_time`) | "pay"
        (`invoice_id` + amounts per means) | "credit_note" (`clinic_id` + `data`,
        `patient` and `items` [{name, amount, vat_rate…}] required; preview shows the
        `invoice` it names). Lines cannot be edited here (each carries a clinical
        treatment id); no create (under a consultation only), no delete (issue a credit
        note). `data` takes the fields the Nextmotion spec accepts for the op; any other
        field is refused.

        ⚠️ Writes DEFAULT to `dry_run=True`: `data` is checked, the current object and
        what would be sent are returned, nothing is written. `dry_run=False` to act.

        Args:
            op: list (default) | get | update | validate | pay | credit_note.
            clinic_id: op="list"/"credit_note".
            invoice_id: op="get"/"update"/"validate"/"pay".
            invoiced_from / invoiced_to: op="list" — period bounds, YYYY-MM-DD.
            max_pages: op="list" with a period — page cap, 1..100 (default 20).
            data: writes — the fields to send.
            dry_run: writes — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        if op in ("update", "validate", "pay", "credit_note"):
            return _serve_write(
                _INVOICES, "invoice", op, client=_client, clinic_id=clinic_id,
                item_id=invoice_id, data=data, dry_run=dry_run, item_name="invoice_id",
                unused={"invoiced_from": invoiced_from, "invoiced_to": invoiced_to,
                        "max_pages": max_pages, "limit": limit, "offset": offset,
                        "fields": fields})
        _refuse_ignored(op, data=data, dry_run=dry_run)
        c = _client()
        if op == "list":
            _need(op, clinic_id=clinic_id)
            _refuse_ignored(op, invoice_id=invoice_id)
            if invoiced_from is None and invoiced_to is None:
                if max_pages is not None:
                    raise _bad(f"op={op!r} does not use `max_pages` without "
                               "`invoiced_from`/`invoiced_to`.")
                return _page(_run(lambda: c.list_invoices(
                    clinic_id, **_paging(limit, offset))), "invoices", _invoice,
                    fields=fields)
            if limit is not None:
                raise _bad(f"op={op!r} does not use `limit` with a period: all the "
                           "invoices of the period are read and returned; `offset` is "
                           "the starting point of the scan there.")
            return _run(lambda: periode.lister(
                lambda o: c.list_invoices(clinic_id, limit=periode.PAGE, offset=o),
                invoiced_from, invoiced_to, offset=0 if offset is None else offset,
                max_pages=max_pages, fields=fields))
        if op == "get":
            _need(op, invoice_id=invoice_id)
            _refuse_ignored(op, clinic_id=clinic_id, limit=limit, offset=offset,
                            fields=fields, invoiced_from=invoiced_from,
                            invoiced_to=invoiced_to, max_pages=max_pages)
            return _one(_run(lambda: c.get_invoice(invoice_id)), "invoice", _invoice)
        raise _bad("op must be 'list', 'get', 'update', 'validate', 'pay' or "
                   "'credit_note'.")


    @mcp.tool()
    def nextmotion_payment(
        op: Literal["list", "get", "update"] = "list",
        clinic_id: Optional[str] = None,
        payment_id: Optional[str] = None,
        invoice_id: Optional[str] = None,
        paid_from: Optional[str] = None,
        paid_to: Optional[str] = None,
        with_practitioner: Optional[bool] = None,
        data: Optional[dict] = None,
        dry_run: Optional[bool] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Payments of a Nextmotion clinic — amount per means (check, cash, card,
        transfer, stripe, voucher, other, custom mediums), deferred date, and the
        invoice paid. Health data, titles and free text are withheld; the patient is
        served by ID ONLY — resolve it with `nextmotion_patient(op="get")` when needed.

        `op`: "list" (default, `clinic_id`, optional `invoice_id`, `paid_from`,
        `paid_to`) | "get" | "update" (`payment_id` + `data`). A payment is recorded by
        `nextmotion_invoice(op="pay")` and never deleted. `data` takes the fields the
        Nextmotion spec accepts for the op; any other field is refused.
        ⚠️ `dry_run` DEFAULTS TO TRUE on update.

        Practitioner (`with_practitioner=True`, list/get — get then needs
        `clinic_id`): each line of the paid invoice gets `practitioner` {status, id,
        first_name, last_name} — the practitioner of that act. `status` other than
        "ok" comes with a `reason`; "unavailable" = the read failed, the practitioner
        is unknown (count in `practitioners_unavailable`). Each payment gets `split`:
        - "split": the payment SETTLES its invoice (with it, the invoice's payments in
          payment_time order reach total_price exactly) → the WHOLE invoice is split
          at this date: `split_amount` = total_price (deposits included, listed in
          `deposits`), `shares` [{practitioner, amount}] pro rata of the lines' price,
          to the cent, remainder on the last practitioner, sum = split_amount; a line
          without a named practitioner gives a share with its status, no
          practitioner made up;
        - "not_split": deposit or partial payment, `amount` whole, split later with
          the invoice at its settlement (`settled_by` {id, payment_time} if known);
        - "undetermined": settlement not certain (reason), nothing split.
        The list adds `totals` over THE PAGE, no double count: by_practitioner,
        unattributed (by status), brought_forward (deposits paid outside the page,
        split by a settlement on it), not_split (only deposits NOT settled on the
        page), undetermined. Page payments = by_practitioner + unattributed −
        brought_forward + not_split + undetermined. One extra read per distinct
        treatment and per distinct invoice of the call.

        Args:
            op: list (default) | get | update.
            clinic_id: op="list".
            payment_id: op="get"/"update".
            invoice_id: op="list" — only this invoice's payments.
            paid_from / paid_to: op="list" — payment days, YYYY-MM-DD, both inclusive
                (clinic time zone), filtered by Nextmotion.
            with_practitioner: op="list"/"get" — name each line's practitioner and
                split the payment between them.
            data: op="update" — the fields to send.
            dry_run: op="update" — default True.
            limit / offset: op="list" — pagination (limit 1..100, default 50).
            fields: op="list" — keep only these keys per row (`id` kept)."""
        if op == "update":
            return _serve_write(
                _PAYMENTS, "payment", op, client=_client, clinic_id=clinic_id,
                item_id=payment_id, data=data, dry_run=dry_run, item_name="payment_id",
                unused={"invoice_id": invoice_id, "limit": limit, "offset": offset,
                        "fields": fields, "paid_from": paid_from, "paid_to": paid_to,
                        "with_practitioner": with_practitioner})
        _refuse_ignored(op, data=data, dry_run=dry_run)
        if op == "list":
            _need(op, clinic_id=clinic_id)
            _refuse_ignored(op, payment_id=payment_id)
            _run(lambda: periode.bornes(paid_from, paid_to, "paid_from", "paid_to"))
            c = _client()
            shape = (_payment_with_practitioners(c, clinic_id) if with_practitioner
                     else _payment)
            out = _page(_run(lambda: c.list_payments(
                clinic_id, invoice_id=invoice_id, paid_from=paid_from, paid_to=paid_to,
                **_paging(limit, offset))), "payments", shape, fields=fields)
            return (_practitioners_said(out, "payments", shape) if with_practitioner
                    else out)
        if op == "get":
            _need(op, payment_id=payment_id)
            if with_practitioner:
                # The invoice's payments are read under the clinic: to know whether
                # this payment settles it.
                _need(op + " with_practitioner", clinic_id=clinic_id)
            else:
                _refuse_ignored(op, clinic_id=clinic_id)
            _refuse_ignored(op, invoice_id=invoice_id, limit=limit,
                            offset=offset, fields=fields, paid_from=paid_from,
                            paid_to=paid_to)
            c = _client()
            shape = (_payment_with_practitioners(c, clinic_id) if with_practitioner
                     else _payment)
            out = _one(_run(lambda: c.get_payment(payment_id)), "payment", shape)
            return (_practitioners_said(out, "payment", shape) if with_practitioner
                    else out)
        raise _bad("op must be 'list', 'get' or 'update'.")

    @mcp.tool()
    def nextmotion_statistics(
        kind: Literal["appointment_income", "treatment_types", "treatment_types_income"],
        clinic_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period_type: Optional[Literal["year", "month", "week", "day"]] = None,
    ) -> dict:
        """Clinic-level statistics of a Nextmotion clinic, as bar charts: `title`,
        `labels` (one per period) and `datasets` (`data` aligned on the labels).

        `kind`:
        - "appointment_income" — income from appointments (one chart).
        - "treatment_types" — draft-quoted, quoted and invoiced treatment counts, one
          chart per treatment type.
        - "treatment_types_income" — income from invoiced treatments, one chart per
          treatment type.

        Args:
            kind: which statistic.
            clinic_id: the clinic.
            start_date / end_date: YYYY-MM-DD, end inclusive; omitted, Nextmotion
                spans 6 periods up to the current one.
            period_type: year | month (Nextmotion default) | week | day.
        """
        if kind not in _STATISTICS:
            raise _bad(f"kind inconnu : {kind!r}.")
        _need("statistics", clinic_id=clinic_id)
        c = _client()
        env = _run(lambda: _STATISTICS[kind](
            c, clinic_id, start_date=start_date, end_date=end_date,
            period_type=period_type))
        data = env.get("data") if isinstance(env, dict) else None
        charts = data if isinstance(data, list) else [data] if data is not None else []
        return {"kind": kind, "charts": [_chart(ch) for ch in charts]}

    @mcp.tool()
    def nextmotion_package_usage(clinic_id: str, patient_id: str) -> dict:
        """The sessions consumed from ONE patient's packages, next to what was sold.

        Reads the patient's appointments (up to 300, `truncated` says when more exist)
        and, for each, the sessions Nextmotion counted against a quote or an invoice;
        adds them up by document, treatment type and pricing (`consumed`), and returns
        the sold lines of each document (`sold`: number, status, lines with name and
        quantity). Remaining = sold quantity of the matching line minus `consumed`.
        To price a refund of a protocol started then abandoned.

        Administrative counters only: ids, quantities, dates, catalogue names. The
        care rows are cut down to them by the client — no practitioner's note, treated
        zone, free text, consultation or consent ever leaves it.

        ⚠️ Nextmotion does not say which line a session decrements: a group is
        matched to a sold line of the same document by name (see `note`).

        Args:
            clinic_id: the clinic.
            patient_id: the patient id, as served by appointments, quotes or invoices.
        """
        _need("get", clinic_id=clinic_id, patient_id=patient_id)
        _uuid("get", "clinic_id", clinic_id)
        _uuid("get", "patient_id", patient_id)
        return _package_usage(_client(), clinic_id, patient_id)

    @mcp.tool()
    def nextmotion_patient_stats(patient_id: str) -> dict:
        """Financial totals of ONE Nextmotion patient, by patient id: quoted,
        invoiced, paid, credit-note and reimbursement totals, first and last visit
        times, review requests and clicks. Nothing identifies the patient here; the
        identity is read with `nextmotion_patient(op="get")`.

        Args:
            patient_id: the patient id, as served by appointments, quotes, invoices,
                payments or journeys.
        """
        _need("get", patient_id=patient_id)
        c = _client()
        return _one(_run(lambda: c.get_patient_stats(patient_id)), "patient_stats",
                    _patient_stats, withheld=_WITHHELD)
