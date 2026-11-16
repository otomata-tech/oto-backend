"""SIRENE stock — access to the full INSEE parquet via DuckDB.

"Stock" side of the FR companies connector (`sirene`): `fr_stock_*` namespace,
counterpart of the live `fr_*` tools (which hit the SIRENE/Recherche Entreprises APIs). The
parquet (~2GB compressed, ~35M rows: headquarters + secondary, active/closed) is read
from Object Storage over httpfs (`SIRENE_STOCK_PARQUET_PATH=s3://…`, ADR 0002),
refreshed monthly by `deploy/refresh_sirene_stock_s3.sh`.

Tools (parquet source — no key, exhaustive/bulk, monthly vintage):
- `fr_stock_enrich(sirens=[...] | sirets=[...])` — headquarters of a LIST of SIRENs, or a
  LIST of establishments with their headcount band and employer status, in ONE scan
- `fr_stock_siege(siren)` — headquarters of a SIREN
- `fr_stock_etablissements(siren)` — all establishments of a company
- `fr_stock_siret(siret)` — one establishment by SIRET
- `fr_stock_search(...)` — multi-criteria search (NAF, commune, enseigne…)

Typical use case: batch enrichment of several thousand SIRENs where
the SIRENE API (rate-limited) or Recherche Entreprises (~10 req/s) are too
slow, and exhaustive enumeration (>10k) that an indexed API does not allow.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from ..mcp_errors import McpError
from .lecture import LECTURE

from oto_mcp.fod import client as sirene_duckdb  # ADR 0028: scan offloaded to FOD

#: SIRETs per `fr_stock_enrich(sirets=…)` call: the data service's batch bound
#: (`sirene_stock.SIRETS_MAX`), refused here before the call with a named code.
SIRETS_MAX = 1000


def _avec_raison_sociale(rows: list[dict]) -> list[dict]:
    """Join the legal unit (second SIRENE stock) on each row's SIREN.

    The establishment stock's `denomination` is `denominationUsuelleEtablissement`,
    empty for most establishments (334 nulls out of 337 SIRENs measured, a CAC 40
    company among them): the company NAME lives in the legal-unit file. Each row
    gets `raison_sociale` (null for a sole trader, whose name is a person's) and
    `categorie_entreprise` (PME/ETI/GE). A SIREN missing from the legal-unit stock
    gets `unite_legale: "absente"` — said, never left to read as "no name"."""
    sirens = [r["siren"] for r in rows if r.get("siren")]
    unites = sirene_duckdb.lookup_unites_legales(sirens) if sirens else {}
    for r in rows:
        ul = unites.get(r.get("siren"))
        if ul is None:
            r["unite_legale"] = "absente"
            continue
        r["raison_sociale"] = ul.get("denomination")
        r["categorie_entreprise"] = ul.get("categorie_entreprise")
    return rows


def register(mcp: FastMCP) -> None:

    @mcp.tool(annotations=LECTURE)
    def fr_stock_siege(siren: str) -> Optional[dict]:
        """Headquarters (siège) of a French company from the local SIRENE
        stock parquet (INSEE, monthly snapshot).

        Faster than the live INSEE API for batch enrichment. Returns the latest
        active siège (etablissementSiege=True). None if SIREN unknown.

        Args:
            siren: SIREN number (9 digits).
        """
        return sirene_duckdb.lookup_siege(siren)

    @mcp.tool(annotations=LECTURE)
    def fr_stock_etablissements(siren: str, active_only: bool = True) -> list[dict]:
        """All establishments (siège + secondaires) of a French company from
        the local SIRENE stock parquet.

        Use this to map subsidiaries / branches / retail locations of a group.
        E.g. all Carrefour Express locations for a holding's SIREN.

        Args:
            siren: SIREN number (9 digits).
            active_only: filter etatAdministratif='A' (default True).
        """
        return sirene_duckdb.list_establishments(siren, active_only=active_only)

    @mcp.tool(annotations=LECTURE)
    def fr_stock_enrich(sirens: Optional[list[str]] = None,
                        sirets: Optional[list[str]] = None) -> dict:
        """Batch enrichment from the stock parquet in a SINGLE scan: pass a LIST of
        SIRENs (their head offices) OR a LIST of SIRETs (those exact establishments) —
        exactly one of the two. Far faster than N calls to fr_get/fr_stock_siege/
        fr_stock_siret (one scan vs one network round-trip each).

        Use it to enrich a list of prospects (from fr_search, a Folk export,
        Unipile contacts…) with address/NAF before pushing to a CRM; with `sirets`,
        to know whether each SITE employs staff and how many.

        `sirens` returns {siren: {street, postal_code, city, code_commune, naf,
        raison_sociale, categorie_entreprise, denomination, status, ...}} ;
        SIRENs without a known siège are omitted.

        `sirets` returns {items: {siret: {...same fields, is_headquarters,
        tranche_effectifs, annee_effectifs, caractere_employeur}}, erreurs:
        [{siret, code}], count}. `tranche_effectifs` is the ESTABLISHMENT's INSEE
        headcount band (00=0, 01=1-2, 02=3-5, 03=6-9, 11=10-19, 12=20-49, 21=50-99,
        22=100-199, 31=200-249, 32=250-499, 41=500-999, 42=1000-1999, 51=2000-4999,
        52=5000-9999, 53=10000+; null = not filled) for the year `annee_effectifs`;
        `caractere_employeur` is O (employer) or N. Errors are PER SIRET, never for
        the batch: `siret_invalide` (not 14 digits), `introuvable` (not in the stock).

        `raison_sociale` is the company name (legal-unit stock, null for a sole
        trader); `denomination` is the establishment's USUAL name, empty for most.

        Args:
            sirens: list of SIREN numbers (9 digits), up to 10000.
            sirets: list of SIRET numbers (14 digits), up to 1000 distinct.
        """
        if (sirens is None) == (sirets is None):
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                "Refused `sirens_ou_sirets`: give EXACTLY ONE of `sirens` (head offices) "
                "or `sirets` (establishments)."),
                data={"code": "sirens_ou_sirets", "retryable": False}))
        if sirets is not None:
            distincts = {str(s).strip() for s in sirets}
            if len(distincts) > SIRETS_MAX:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    f"Refused `sirets_lot_trop_grand`: {len(distincts)} distinct SIRETs, "
                    f"at most {SIRETS_MAX} per call — split the list."),
                    data={"code": "sirets_lot_trop_grand", "retryable": False,
                          "limit": SIRETS_MAX, "count": len(distincts)}))
            res = sirene_duckdb.lookup_sirets([str(s).strip() for s in sirets])
            _avec_raison_sociale(list(res["items"].values()))
            return res
        clean = [str(s).strip() for s in sirens]
        sieges = sirene_duckdb.headquarters_addresses(clean)
        _avec_raison_sociale(list(sieges.values()))
        return sieges

    @mcp.tool(annotations=LECTURE)
    def fr_stock_siret(siret: str) -> Optional[dict]:
        """Fetch a specific establishment by SIRET (14 digits) from the stock parquet.

        Args:
            siret: SIRET number (14 digits).
        """
        return sirene_duckdb.lookup_siret(siret)

    @mcp.tool(annotations=LECTURE)
    def fr_stock_search(
        naf: Optional[str] = None,
        code_commune: Optional[str] = None,
        code_postal: Optional[str] = None,
        departement: Optional[str] = None,
        denomination: Optional[str] = None,
        enseigne: Optional[str] = None,
        active_only: bool = True,
        sieges_only: bool = False,
        tranche_effectifs: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Multi-criteria search over the SIRENE stock parquet (INSEE local snapshot).

        All filters are AND'd. Returns paginated establishments matching, each
        with `raison_sociale` and `categorie_entreprise` from the legal-unit
        stock. `denomination` is the establishment's USUAL name, empty for most
        (~88 %): the company name is `raison_sociale`.

        Use cases:
        - All NAF 4711F (supermarkets) in Marseille (`code_commune=13201` or `code_postal=13001`)
        - All "Carrefour Express" branded locations (`enseigne='carrefour express'`)
        - All "Intermarché" supermarkets in a département (`enseigne='intermarché', naf='47.11F', departement='26'`)
        - **Companies of 100-499 employees HEADQUARTERED in a département**
          (`departement='13', sieges_only=True, tranche_effectifs='22,31,32'`) — the
          clean way to enumerate ETI/large-PME by real HQ location + size, server-side.

        Args:
            naf: APE/NAF code exact match (ex. "4711F").
            code_commune: INSEE COG code (5 digits, ex. "13201").
            code_postal: 5 digits (ex. "13001").
            departement: 2 chars for mainland France (e.g. "26") or 3 chars for overseas
                departments (e.g. "971"). Matches on the postal code prefix —
                enseigne+naf+departement enumerates all sites of a brand in a
                département.
            denomination: case-insensitive substring on the establishment's
                usual name — rarely filled, so it misses most companies: to find
                a company by its name, use `fr_search(query=…)`.
            enseigne: case-insensitive substring across enseigne 1/2/3.
            active_only: filter etatAdministratif='A' (default True).
            sieges_only: restrict to headquarters only (default False).
            tranche_effectifs: comma-separated INSEE TEFEN size codes — keep only
                establishments whose effectif is one of them. Codes: 00=0, 01=1-2,
                02=3-5, 03=6-9, 11=10-19, 12=20-49, 21=50-99, 22=100-199, 31=200-249,
                32=250-499, 41=500-999, 42=1000-1999, 51=2000-4999, 52=5000-9999,
                53=10000+. Ex. "22,31,32" = 100-499 employees. With sieges_only=True
                this filters by company size for single-site firms.
            limit: max 1000, default 100.
            offset: pagination offset.
        """
        tranches = (
            [c.strip() for c in tranche_effectifs.split(",") if c.strip()]
            if tranche_effectifs
            else None
        )
        items = sirene_duckdb.search(
            naf=naf,
            code_commune=code_commune,
            code_postal=code_postal,
            departement=departement,
            denomination=denomination,
            enseigne=enseigne,
            active_only=active_only,
            sieges_only=sieges_only,
            tranche_effectifs=tranches,
            limit=limit,
            offset=offset,
        )
        _avec_raison_sociale(items)
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}
