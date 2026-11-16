"""Nextmotion — splitting a payment between the practitioners of its invoice lines.

Pure functions, no I/O: the tool reads (the payment, the invoice's payments, each
line's practitioner) and hands the rows over.

The rule:
- the payment that SETTLES its invoice carries the split of the WHOLE invoice (the
  total of its payments, deposits included, = `total_price`), pro rata of the lines'
  `price`, by practitioner, at the settlement's date; an invoice paid at once is split
  for its own amount;
- a deposit or a partial payment is NOT split (`not_split`): it is split with the
  invoice, at the payment that settles it (named in `settled_by` when it exists);
- the share of a line whose practitioner is not named (`none`, `no_treatment`,
  `unavailable`) goes to an explicit entry carrying that status — no practitioner is
  made up.

"Settles" is read from the invoice's own payments, never guessed from a status code:
all the payments of the invoice are summed in `payment_time` order; the payment
settles the invoice when the paid total reaches `total_price` exactly with it, and
was below before it. Anything that keeps this from being certain (an amount that is
not a decimal, payments missing a date, a paid total above the invoice total, a list
of payments that does not contain the payment itself, a cut list) gives
`undetermined` and its reason, and nothing is split.

Amounts are decimals to the cent; each share is rounded half-up, and the rounding
remainder goes to the last practitioner, so the shares add up EXACTLY to the invoice
total.

Totals of a list page, with no double count: a deposit whose settling payment is on
the same page is counted in `by_practitioner` (through that settlement) and NOT in
`not_split`; a deposit paid outside the page but split by a settlement on it is
`brought_forward`. Hence: sum of the page's payments = by_practitioner + unattributed
- brought_forward + not_split + undetermined.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Optional

CENT = Decimal("0.01")
#: The payment's amount fields (decimal strings), plus `custom_medium_list[].amount`.
MEANS = ("check", "cash", "card", "transfer", "stripe", "voucher", "other")

SPLIT_NOTE = (
    "split (with_practitioner): the payment that SETTLES its invoice carries the split "
    "of the WHOLE invoice (`split_amount` = all its payments, deposits included = "
    "total_price; `deposits` lists the earlier ones), pro rata of the lines' price by "
    "practitioner (`shares`, to the cent, the rounding remainder on the last "
    "practitioner, sum = split_amount). Settled = with this payment, the invoice's "
    "payments (in payment_time order) reach its total_price exactly. A deposit or "
    "partial payment is `not_split` (whole amount): it is split at the settlement "
    "(`settled_by` when known). A line without a named practitioner (none | "
    "no_treatment | unavailable) gives a share with that status, never a made-up "
    "practitioner. `undetermined` = settlement could not be established for sure "
    "(reason given): nothing split. `totals` covers THIS PAGE, no double count: a "
    "deposit settled on the page is in by_practitioner, not in not_split; "
    "`brought_forward` = deposits paid outside the page split by a settlement on it; "
    "sum of the page's payments = by_practitioner + unattributed - brought_forward + "
    "not_split + undetermined.")


class Unreadable(ValueError):
    """An amount that is not a decimal: said, never taken as zero."""


def _dec(value: Any, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise Unreadable(f"{what} is not a decimal amount: {value!r}.")
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise Unreadable(f"{what} is not a decimal amount: {value!r}.") from None
    if not d.is_finite():
        raise Unreadable(f"{what} is not a decimal amount: {value!r}.")
    return d


def amount(payment: dict) -> Decimal:
    """The amount of a payment: the sum of its means. Raises `Unreadable`."""
    total = Decimal("0")
    for key in MEANS:
        if payment.get(key) is not None:
            total += _dec(payment[key], f"payment `{key}`")
    for medium in payment.get("custom_medium_list") or []:
        total += _dec((medium or {}).get("amount") if isinstance(medium, dict) else medium,
                      "payment `custom_medium_list[].amount`")
    return total


def _cents(d: Decimal) -> str:
    return str(d.quantize(CENT))


def settles(payment: dict, invoice_payments: list, truncated: bool) -> dict:
    """Whether `payment` settles its invoice, from all the invoice's payments.

    {"status": "settles", "total", "deposits"} | {"status": "not_split", "reason",
    "settled_by"?} | {"status": "undetermined", "reason"}."""
    def undetermined(reason):
        return {"status": "undetermined", "reason": reason}

    invoice = payment.get("invoice") if isinstance(payment.get("invoice"), dict) else {}
    if truncated:
        return undetermined("the invoice has more payments than were read.")
    try:
        total = _dec(invoice.get("total_price"), "invoice `total_price`")
        rows = [(p.get("payment_time"), str(p.get("id")), amount(p))
                for p in invoice_payments if isinstance(p, dict)]
    except Unreadable as e:
        return undetermined(str(e))
    if total <= 0:
        return undetermined(f"invoice total_price is {total}: nothing to settle.")
    if str(payment.get("id")) not in {r[1] for r in rows}:
        return undetermined("the payment is not among its invoice's payments.")
    if len(rows) > 1 and any(not isinstance(t, str) or not t for t, _, _ in rows):
        return undetermined("a payment of the invoice has no payment_time: their order "
                            "is unknown.")
    paid = sum((r[2] for r in rows), Decimal("0"))
    if paid > total:
        return undetermined(f"the invoice's payments ({paid}) exceed its total_price "
                            f"({total}).")
    cumul, before, settler = Decimal("0"), [], None
    for when, pid, value in sorted(rows):
        reached = cumul < total <= cumul + value
        cumul += value
        if reached:
            settler = (when, pid)
            break
        before.append((when, pid, value))
    if settler is not None and settler[1] == str(payment.get("id")):
        return {"status": "settles", "total": total,
                "deposits": [{"id": pid, "payment_time": when, "amount": _cents(value)}
                             for when, pid, value in before]}
    upto = Decimal("0")
    for _, pid, value in sorted(rows):
        upto += value
        if pid == str(payment.get("id")):
            break
    out = {"status": "not_split",
           "reason": f"deposit or partial payment (paid {upto} of {total} with it): it "
                     "is split with the whole invoice at the payment that settles it."}
    if settler is not None:
        out["settled_by"] = {"id": settler[1], "payment_time": settler[0]}
    else:
        out["reason"] += " The invoice is not settled yet."
    return out


def _key(practitioner: dict) -> tuple:
    if practitioner.get("status") == "ok":
        return ("ok", practitioner.get("id"))
    return (practitioner.get("status"), practitioner.get("reason"))


def _who(practitioner: dict) -> dict:
    if practitioner.get("status") == "ok":
        return {k: practitioner.get(k) for k in ("status", "id", "first_name", "last_name")}
    return {"status": practitioner.get("status"), "reason": practitioner.get("reason")}


def split(value: Decimal, lines: Iterable[tuple]) -> list:
    """`value` split pro rata of the lines' price, by practitioner.

    `lines`: (price, practitioner dict). Named practitioners first (line order), then
    the entries without one; the rounding remainder goes to the last named
    practitioner (else the last entry). Raises `Unreadable` on a price that is not a
    decimal, ValueError when the prices add up to zero or less."""
    weights: dict = {}
    who: dict = {}
    for price, practitioner in lines:
        k = _key(practitioner)
        weights[k] = weights.get(k, Decimal("0")) + _dec(price, "invoice line `price`")
        who.setdefault(k, _who(practitioner))
    base = sum(weights.values(), Decimal("0"))
    if base <= 0:
        raise ValueError(f"the invoice lines' prices add up to {base}: no pro rata.")
    keys = ([k for k in weights if k[0] == "ok"] + [k for k in weights if k[0] != "ok"])
    last = max((i for i, k in enumerate(keys) if k[0] == "ok"), default=len(keys) - 1)
    shares = [(value * weights[k] / base).quantize(CENT, ROUND_HALF_UP) for k in keys]
    shares[last] = value - (sum(shares, Decimal("0")) - shares[last])
    return [{"practitioner": who[k], "amount": str(s.quantize(CENT))}
            for k, s in zip(keys, shares)]


def ventilate(payment: dict, practitioners: list, invoice_payments: list,
              truncated: bool) -> dict:
    """The `split` of one payment. `practitioners`: one per invoice line, in order."""
    try:
        own = amount(payment)
    except Unreadable as e:
        return {"status": "undetermined", "reason": str(e), "amount": None}
    verdict = settles(payment, invoice_payments, truncated)
    if verdict["status"] != "settles":
        return {**verdict, "amount": _cents(own)}
    invoice = payment.get("invoice") or {}
    lines = [li for li in invoice.get("invoiced_treatments") or [] if isinstance(li, dict)]
    try:
        shares = split(verdict["total"], [(li.get("price"), p)
                                          for li, p in zip(lines, practitioners)])
    except ValueError as e:
        return {"status": "undetermined", "reason": str(e), "amount": _cents(own)}
    return {"status": "split", "amount": _cents(own),
            "split_amount": _cents(verdict["total"]), "deposits": verdict["deposits"],
            "shares": shares}


def totals(splits: Iterable[tuple]) -> dict:
    """Totals over one page, without double count. `splits`: (payment id, split).

    A deposit whose settling payment is on the page is in `by_practitioner` (through
    that settlement), not in `not_split`; a deposit outside the page split by a
    settlement on it is `brought_forward`."""
    splits = [(str(pid), s) for pid, s in splits if s]
    on_page = {pid for pid, _ in splits}
    settled_here = {pid for pid, s in splits if s["status"] == "split"}
    by: dict = {}
    who: dict = {}
    not_split = undetermined = brought = Decimal("0")
    unreadable = 0
    for _, s in splits:
        if s["status"] == "split":
            for share in s["shares"]:
                p = share["practitioner"]
                k = ("ok", p["id"]) if p["status"] == "ok" else (p["status"],)
                by[k] = by.get(k, Decimal("0")) + Decimal(share["amount"])
                who.setdefault(k, {key: p[key] for key in ("status", "id", "first_name",
                                                           "last_name") if key in p})
            brought += sum((Decimal(d["amount"]) for d in s["deposits"]
                            if d["id"] not in on_page), Decimal("0"))
        elif s["amount"] is None:
            unreadable += 1
        elif s["status"] == "not_split":
            if (s.get("settled_by") or {}).get("id") not in settled_here:
                not_split += Decimal(s["amount"])
        else:
            undetermined += Decimal(s["amount"])
    return {
        "by_practitioner": [{"practitioner": who[k], "amount": _cents(v)}
                            for k, v in by.items() if k[0] == "ok"],
        "unattributed": [{"status": k[0], "amount": _cents(v)}
                         for k, v in by.items() if k[0] != "ok"],
        "brought_forward": _cents(brought),
        "not_split": _cents(not_split),
        "undetermined": _cents(undetermined),
        "undetermined_without_amount": unreadable,
    }
