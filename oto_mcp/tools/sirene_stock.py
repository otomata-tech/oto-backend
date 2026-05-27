"""SIRENE stock — accès au parquet INSEE complet via DuckDB.

Le parquet (~2GB compressé, ~35M lignes établissements + sièges + secondaires +
actifs/fermés) vit sur le serveur (`/opt/oto-mcp/data/sirene/StockEtablissement.parquet`).
Refresh mensuel manuel/cron côté serveur.

3 tools complémentaires aux endpoints `fr_*` (qui frappent les APIs live) :
- `sirene_stock_siege(siren)` — siège pour batch fast
- `sirene_stock_etablissements(siren)` — tous les établissements d'une boîte
- `sirene_stock_search(...)` — recherche multi-critères (NAF, commune, enseigne…)

Cas d'usage typique : enrichissement batch de plusieurs milliers de SIRENs où
l'API SIRENE (rate-limited) ou Recherche Entreprises (~10 req/s) sont trop
lentes. Le parquet via DuckDB tourne à 10-50ms/lookup à chaud.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import sirene_duckdb, sirene_resolve


def register(mcp: FastMCP) -> None:

    @mcp.tool()
    async def sirene_stock_siege(siren: str) -> Optional[dict]:
        """Headquarters (siège) of a French company from the local SIRENE
        stock parquet (INSEE, monthly snapshot).

        Faster than the live INSEE API for batch enrichment. Returns the latest
        active siège (etablissementSiege=True). None if SIREN unknown.

        Args:
            siren: SIREN number (9 digits).
        """
        return sirene_duckdb.lookup_siege(siren)

    @mcp.tool()
    async def sirene_stock_etablissements(siren: str, active_only: bool = True) -> list[dict]:
        """All establishments (siège + secondaires) of a French company from
        the local SIRENE stock parquet.

        Use this to map subsidiaries / branches / retail locations of a group.
        E.g. all Carrefour Express locations for a holding's SIREN.

        Args:
            siren: SIREN number (9 digits).
            active_only: filter etatAdministratif='A' (default True).
        """
        return sirene_duckdb.list_establishments(siren, active_only=active_only)

    @mcp.tool()
    async def sirene_stock_siret(siret: str) -> Optional[dict]:
        """Fetch a specific establishment by SIRET (14 digits) from the stock parquet.

        Args:
            siret: SIRET number (14 digits).
        """
        return sirene_duckdb.lookup_siret(siret)

    @mcp.tool()
    async def sirene_stock_search(
        naf: Optional[str] = None,
        code_commune: Optional[str] = None,
        code_postal: Optional[str] = None,
        denomination: Optional[str] = None,
        enseigne: Optional[str] = None,
        active_only: bool = True,
        sieges_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Multi-criteria search over the SIRENE stock parquet (INSEE local snapshot).

        All filters are AND'd. Returns paginated establishments matching.

        Use cases:
        - All NAF 4711F (supermarchés) in Marseille (`code_commune=13201` or `code_postal=13001`)
        - All "Carrefour Express" branded locations (`enseigne='carrefour express'`)
        - All active establishments of a given activity in a commune

        Args:
            naf: APE/NAF code exact match (ex. "4711F").
            code_commune: INSEE COG code (5 digits, ex. "13201").
            code_postal: 5 digits (ex. "13001").
            denomination: case-insensitive substring on denomination usuelle.
            enseigne: case-insensitive substring across enseigne 1/2/3.
            active_only: filter etatAdministratif='A' (default True).
            sieges_only: restrict to headquarters only (default False).
            limit: max 1000, default 100.
            offset: pagination offset.
        """
        items = sirene_duckdb.search(
            naf=naf,
            code_commune=code_commune,
            code_postal=code_postal,
            denomination=denomination,
            enseigne=enseigne,
            active_only=active_only,
            sieges_only=sieges_only,
            limit=limit,
            offset=offset,
        )
        return {"items": items, "count": len(items), "limit": limit, "offset": offset}

    @mcp.tool()
    async def sirene_stock_search_by_address(
        adresse: str,
        code_commune: str,
        naf2_hint: Optional[str] = None,
        top_n: int = 3,
    ) -> dict:
        """Resolve a free-form address to candidate SIRET(s), scored.

        Uses INSEE local stock + UniteLegale parquets. Filters by INSEE
        commune code (auto-expands Paris/Lyon/Marseille arrondissements ↔
        global codes), matches the address libelle via tokens OR fallback
        on enseigne (Carrefour, Lidl…) / denomination usuelle.

        Score factors :
        - commune match (always, baseline)
        - voie libelle match (+0.3) OR enseigne match (+0.2)
        - exact numero match (+0.3)
        - NAF2 hint match (+0.5) vs mismatch (+0.1) vs no hint (+0.2)
        - is siège (+0.1)
        - NAF section C (manufacturing) (+0.1)

        Excludes sections K (finance), L (immo), O (admin pub), S (autres
        services personnels). Returns up to `top_n` candidates sorted by
        descending confidence.

        Args:
            adresse: free-form, e.g. "12 RUE DE LA PAIX" or "ZA LES PINS".
            code_commune: INSEE COG (5 digits or 2A/2B + 3 for Corsica).
                          For Paris/Lyon/Marseille, pass the global OR an
                          arrondissement code — both work, expansion is auto.
            naf2_hint: optional 2-digit NAF prefix hint, e.g. "10" (agro).
            top_n: 1..10, default 3.
        """
        candidates = sirene_resolve.lookup_by_address(
            adresse=adresse,
            code_commune=code_commune,
            naf2_hint=naf2_hint,
            top_n=top_n,
        )
        return {"candidates": candidates, "count": len(candidates)}
