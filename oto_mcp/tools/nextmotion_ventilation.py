"""Nextmotion — splitting a payment between the practitioners of its invoice lines.

Pure functions, no I/O: the tool reads (the payment, the invoice's payments, each
line's practitioner) and hands the rows over.

The rule:
- a payment that SETTLES its invoice is split pro rata of the lines' `price`, by
  practitioner;
- a deposit or a partial payment is NOT split (`not_split`, the invoice is not settled
  by it);
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
remainder goes to the last practitioner, so the shares add up EXACTLY to the payment.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Iterable, Optional

CENT = Decimal("0.01")
#: The payment's amount fields (decimal strings), plus `custom_medium_list[].amount`.
MEANS = ("check", "cash", "card", "transfer", "stripe", "voucher", "other")

SPLIT_NOTE = (
    "split (with_practitioner): a payment that SETTLES its invoice is split pro rata of "
    "the lines' price by practitioner (`shares`, to the cent, the rounding remainder on "
    "the last practitioner, sum = the payment). Settled = with this payment, the "
    "invoice's payments (in payment_time order) reach its total_price exactly. A "
    "deposit or partial payment is `not_split` (whole amount, reason). A line without a "
    "named practitioner (none | no_treatment | unavailable) gives a share with that "
    "status, never a made-up practitioner. `undetermined` = settlement could not be "
    "established for sure (reason given): nothing split. `totals` adds up the payments "
    "OF THIS PAGE only.")


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


def settles(payment: dict, invoice_payments: list, truncated: bool) -> dict:
    """Whether `payment` settles its invoice, from all the invoice's payments.

    {"status": "settles"} | {"status": "not_split", "reason"} |
    {"status": "undetermined", "reason"}."""
    def undetermined(reason):
        return {"status": "undetermined", "reason": reason}

    invoice = payment.get("invoice") if isinstance(payment.get("invoice"), dict) else {}
    if truncated:
        return undetermined("the invoice has more payments than were read.")
    try:
        total = _dec(invoice.get("total_price"), "invoice `total_price`")
        own = amount(payment)
        rows = [(p.get("payment_time"), str(p.get("id")), amount(p))
                for p in invoice_payments if isinstance(p, dict)]
    except Unreadable as e:
        return undetermined(str(e))
    if total <= 0:
        return undetermined(f"invoice total_price is {total}: nothing to settle.")
    if payment.get("id") not in {r[1] for r in rows}:
        return undetermined("the payment is not among its invoice's payments.")
    if len(rows) > 1 and any(not isinstance(t, str) or not t for t, _, _ in rows):
        return undetermined("a payment of the invoice has no payment_time: their order "
                            "is unknown.")
    before = Decimal("0")
    for _, pid, value in sorted(rows):
        if pid == payment.get("id"):
            break
        before += value
    after = before + own
    paid = sum((r[2] for r in rows), Decimal("0"))
    if paid > total:
        return undetermined(f"the invoice's payments ({paid}) exceed its total_price "
                            f"({total}).")
    if after == total and before < total:
        return {"status": "settles"}
    return {"status": "not_split",
            "reason": f"this payment does not settle the invoice (deposit or partial "
                      f"payment): paid {after} of {total} with it."}


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
        return {**verdict, "amount": str(own.quantize(CENT))}
    invoice = payment.get("invoice") or {}
    lines = [li for li in invoice.get("invoiced_treatments") or [] if isinstance(li, dict)]
    try:
        shares = split(own, [(li.get("price"), p) for li, p in zip(lines, practitioners)])
    except ValueError as e:
        return {"status": "undetermined", "reason": str(e),
                "amount": str(own.quantize(CENT))}
    return {"status": "split", "amount": str(own.quantize(CENT)), "shares": shares}


def totals(splits: Iterable[Optional[dict]]) -> dict:
    """Totals over the given payments: by practitioner, by unnamed status, not split,
    undetermined (an undetermined payment without a readable amount is counted)."""
    by: dict = {}
    who: dict = {}
    not_split = undetermined = Decimal("0")
    unreadable = 0
    for s in splits:
        if not s:
            continue
        if s["status"] == "split":
            for share in s["shares"]:
                p = share["practitioner"]
                k = ("ok", p["id"]) if p["status"] == "ok" else (p["status"],)
                by[k] = by.get(k, Decimal("0")) + Decimal(share["amount"])
                who.setdefault(k, {key: p[key] for key in ("status", "id", "first_name",
                                                           "last_name") if key in p})
        elif s["amount"] is None:
            unreadable += 1
        elif s["status"] == "not_split":
            not_split += Decimal(s["amount"])
        else:
            undetermined += Decimal(s["amount"])
    return {
        "by_practitioner": [{"practitioner": who[k], "amount": str(v)}
                            for k, v in by.items() if k[0] == "ok"],
        "unattributed": [{"status": k[0], "amount": str(v)}
                         for k, v in by.items() if k[0] != "ok"],
        "not_split": str(not_split.quantize(CENT)),
        "undetermined": str(undetermined.quantize(CENT)),
        "undetermined_without_amount": unreadable,
    }
