"""Nextmotion — the invoice period filter, applied TOOL-SIDE.

`GET /v4/clinics/{clinic_id}/invoices` only accepts `limit` and `offset`: no date
filter, and the order of the list is not documented. The tool therefore reads the upstream in pages
of 100 and keeps the invoices whose `invoiced_time` falls within the period.

⚠️ **It never stops early**: stopping at the first out-of-period invoice
would assume a sort order the spec does not promise, and would return a partial result presented
as complete. It reads to the last page, bounded by a page cap; when the
cap cuts, the response SAYS so (`complet: false`) and gives the resume `offset`.

An invoice's date is the one `invoiced_time` writes, in its own timezone
(`2026-01-31T23:30:00+01:00` is January 31st). An invoice without a readable
`invoiced_time` raises: the spec declares it mandatory, and silently discarding it would lie about
the period.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Callable, Optional

from .nextmotion_socle import _invoice, _page

PAGE = 100
MAX_PAGES_DEFAUT = 20
MAX_PAGES_MAX = 100
_JOUR = re.compile(r"\d{4}-\d{2}-\d{2}")


def _borne(valeur: Optional[str], nom: str) -> Optional[date]:
    if valeur is None:
        return None
    try:
        if not isinstance(valeur, str) or not _JOUR.fullmatch(valeur):
            raise ValueError
        return date.fromisoformat(valeur)
    except ValueError:
        raise ValueError(f"`{nom}` must be a YYYY-MM-DD date — got {valeur!r}.") from None


def bornes(du: Optional[str], au: Optional[str], nom_du: str,
           nom_au: str) -> tuple:
    """The two bounds of a period, checked: YYYY-MM-DD each, the first not after the
    second."""
    debut, fin = _borne(du, nom_du), _borne(au, nom_au)
    if debut and fin and debut > fin:
        raise ValueError(f"`{nom_du}` is after `{nom_au}`.")
    return debut, fin


def _jour_facture(facture: Any) -> date:
    brut = facture.get("invoiced_time") if isinstance(facture, dict) else None
    try:
        return datetime.fromisoformat(brut).date()
    except (TypeError, ValueError):
        ident = facture.get("id") if isinstance(facture, dict) else None
        raise ValueError(
            f"Nextmotion: invoice {ident!r} has no readable `invoiced_time` "
            f"({brut!r}) — cannot tell whether it is within the period.") from None


def lister(lire_page: Callable[[int], Any], invoiced_from: Optional[str],
           invoiced_to: Optional[str], *, offset: int, max_pages: Optional[int],
           fields: Optional[list]) -> dict:
    """The invoices of the period, read from `offset` over at most `max_pages` pages.

    `lire_page(offset)` returns an upstream page of `PAGE` invoices. Everything is validated BEFORE
    the first call."""
    debut, fin = bornes(invoiced_from, invoiced_to, "invoiced_from", "invoiced_to")
    plafond = MAX_PAGES_DEFAUT if max_pages is None else max_pages
    if not 1 <= plafond <= MAX_PAGES_MAX:
        raise ValueError(f"max_pages must be between 1 and {MAX_PAGES_MAX} — got {plafond}.")
    if offset < 0:
        raise ValueError(f"offset must be >= 0 — got {offset}.")

    gardees: list = []
    pages = parcourues = 0
    total = None
    complet = False
    while pages < plafond:
        env = lire_page(offset + parcourues)
        env = env if isinstance(env, dict) else {}
        lignes = env.get("data") or []
        total = env.get("count")
        pages += 1
        parcourues += len(lignes)
        gardees += [f for f in lignes
                    if (debut is None or _jour_facture(f) >= debut)
                    and (fin is None or _jour_facture(f) <= fin)]
        if env.get("next") is None:
            complet = True
            break
        if not lignes:
            raise ValueError("Nextmotion: empty page although `next` announces more — "
                             f"walk interrupted at offset {offset + parcourues}.")

    out = _page({"count": None, "next": None, "data": gardees}, "invoices", _invoice,
                fields=fields)
    out.pop("count")
    out.pop("has_more")
    return {
        **out,
        "periode": {"invoiced_from": invoiced_from, "invoiced_to": invoiced_to},
        "trouvees": len(gardees),
        "pages_lues": pages,
        "factures_parcourues": parcourues,
        "factures_total_amont": total,
        "complet": complet,
        "offset_suivant": None if complet else offset + parcourues,
    }
