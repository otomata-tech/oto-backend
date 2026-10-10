"""BODACC — the French bulletin of civil and commercial notices, as a whole (DILA open data).

Wraps `oto.tools.bodacc.notices.BodaccNoticesClient` (OpenDataSoft v2.1, dataset
`annonces-commerciales`). Open-data connector: no credential, no cascade. Exposed only
if activated in DB (activation notch, ADR 0010).

ONE tool per business object (ADR 0047): `bodacc_notice`, the verb as `op` —
`search` (default) lists notices, `count` groups them, `get` reads one in full. The
filters are the same for `search` and `count`.

⚠️ Not the same question as `fr_events` (`sirene` connector): that one reads the
notices of a company known by its SIREN; this one finds companies FROM the notices.

The client calls are written out in plain form (`_client().search(…)`): that is what
makes them verifiable by the version-skew probe.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.types import ErrorData, INVALID_PARAMS

from ..mcp_errors import McpError

_SOURCE = "BODACC — DILA open data (annonces-commerciales), Licence Ouverte 2.0"


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _upstream_message(e) -> str:
    status = e.status_code
    if status == 429:
        return "BODACC: too many requests (429) — retry in a moment."
    if status in (500, 502, 503, 504):
        return f"BODACC is temporarily unavailable (HTTP {status}) — retry later."
    body = e.body.get("message") if isinstance(e.body, dict) else e.body
    return f"BODACC refused the request (HTTP {status}): {body}"


def register(mcp: FastMCP) -> None:
    import requests
    from oto.tools.bodacc.notices import BodaccNoticesClient, BodaccQueryError
    from oto.tools.common.errors import UpstreamHTTPError

    def _client() -> BodaccNoticesClient:
        return BodaccNoticesClient()

    @mcp.tool()
    def bodacc_notice(
        op: Literal["search", "count", "get"] = "search",
        q: Optional[str] = None,
        famille: Optional[Literal[
            "collective", "conciliation", "creation", "divers", "dpc", "immatriculation",
            "modification", "radiation", "retablissement_professionnel", "vente"]] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        commercant: Optional[str] = None,
        ville: Optional[str] = None,
        tribunal: Optional[str] = None,
        type_avis: Optional[Literal["annonce", "rectificatif", "annulation"]] = None,
        siren: Optional[str] = None,
        group_by: Optional[Literal[
            "famille", "departement", "region", "tribunal", "type_avis", "mois", "jour"]] = None,
        id: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        order: Literal["desc", "asc"] = "desc",
        full: bool = False,
    ) -> dict:
        """BODACC legal notices across ALL French companies (DILA open data, no key):
        find companies FROM their notices — every receivership opened in a department
        this week, every business sale in a city, accounts filed, deregistrations.
        For the notices of ONE known company, `fr_events(siren)` is the shorter path.

        op="search" (default) — notices matching the filters, most recent first.
        Returns `total_count`, `next_offset` (null on the last page) and `notices`:
        flat lines `{id, date_parution, famille, type_avis, siren, commercant, ville,
        cp, departement, tribunal, jugement_nature, jugement_date, date_cloture,
        activite, resume, url}` — `resume` is the notice's own text. `full=true`
        returns the full notices instead (heavy: officers, establishments, judgment). ⚠️ Pagination stops at offset+limit = 10000:
        past it, narrow the period (`date_from`/`date_to`) rather than paging.

        op="count" — the number of matching notices grouped by `group_by` (required),
        largest first; `mois`/`jour` come in date order. Count BEFORE paging a large
        set: one call sizes it.

        op="get" — one notice in full by `id` (the `id` of a search line).

        Filters (search and count) combine with AND. Always bound a sweep by date:
        the bulletin holds tens of millions of notices since 2008.

        Args:
            op: search | count | get.
            q: full-text search over the whole notice (e.g. "liquidation judiciaire").
            famille: notice family — collective (insolvency proceedings), conciliation,
                creation, divers, dpc (accounts filed), immatriculation, modification,
                radiation (deregistration), retablissement_professionnel, vente (sales
                and transfers of a business).
            departement: department code — "75", "2A", "974".
            date_from: publication date from, YYYY-MM-DD, inclusive.
            date_to: publication date to, YYYY-MM-DD, inclusive.
            commercant: words in the company or trader name.
            ville: words in the city name.
            tribunal: words in the court name (e.g. "Nanterre").
            type_avis: annonce (initial notice), rectificatif, annulation.
            siren: 9-digit SIREN.
            group_by: op=count only — famille, departement, region, tribunal, type_avis,
                mois (YYYY-MM), jour.
            id: op=get only — the notice id (e.g. "A202601943821").
            limit: search: 1-100 per page (default 20); count: max groups (default 20,
                up to 100).
            offset: search only — first notice to return (pagination).
            order: search only — desc (newest first, default) or asc.
            full: search only — the full notices instead of the flat lines.
        """
        filters = dict(q=q, famille=famille, departement=departement, date_from=date_from,
                       date_to=date_to, commercant=commercant, ville=ville,
                       tribunal=tribunal, type_avis=type_avis, siren=siren)
        try:
            if op == "get":
                if not id:
                    raise _bad("op='get' needs `id` — the `id` of a notice from op='search'.")
                notice = _client().get(id)
                if notice is None:
                    raise _bad(f"No BODACC notice has the id {id!r}.")
                return {"source": _SOURCE, "notice": notice}
            if op == "count":
                if not group_by:
                    raise _bad("op='count' needs `group_by` — famille, departement, region, "
                               "tribunal, type_avis, mois or jour.")
                return {"source": _SOURCE,
                        **_client().count(group_by, limit=limit, **filters)}
            return {"source": _SOURCE,
                    **_client().search(limit=limit, offset=offset, order=order, raw=full,
                                       **filters)}
        except BodaccQueryError as e:
            raise _bad(str(e))
        except UpstreamHTTPError as e:
            raise _bad(_upstream_message(e))
        except requests.RequestException as e:
            raise _bad(f"BODACC could not be reached ({type(e).__name__}) — retry later.")
