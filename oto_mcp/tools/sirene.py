"""INSEE SIRENE — données plus granulaires que recherche-entreprises (établissements / SIRET).

Nécessite `SIRENE_API_KEY` côté serveur (résolu via `oto.config.get_secret`).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.sirene import SireneClient

    client = SireneClient()

    @mcp.tool()
    async def sirene_search(
        query: Optional[str] = None,
        naf: Optional[str] = None,
        departement: Optional[str] = None,
        postal: Optional[str] = None,
        commune: Optional[str] = None,
        employees: Optional[str] = None,
        headquarters_only: bool = True,
        limit: int = 20,
    ) -> dict:
        """Search French establishments (SIRET) in INSEE SIRENE.

        Plus précis que recherche-entreprises pour filtrer par effectifs ou
        établissements précis. Renvoie une liste d'établissements (SIRET).

        Args:
            query: Free-text query (company name).
            naf: NAF codes, comma-separated.
            departement: Department code (e.g. "75").
            postal: Postal code.
            commune: City name.
            employees: TEFEN employee-range codes, comma-separated.
            headquarters_only: If true, returns only sièges (default).
            limit: Max results.
        """
        results = client.search_siret(
            name=query,
            naf=[s.strip() for s in naf.split(",")] if naf else None,
            employees=[s.strip() for s in employees.split(",")] if employees else None,
            departement=departement,
            postal_code=postal,
            city=commune,
            headquarters_only=headquarters_only,
            limit=limit,
        )
        ets = results.get("etablissements", [])
        return {
            "total": results.get("header", {}).get("total", len(ets)),
            "count": len(ets),
            "etablissements": ets,
        }

    @mcp.tool()
    async def sirene_get(siren: str) -> dict:
        """Fetch a French legal unit by SIREN (9 digits) from INSEE SIRENE."""
        return client.get_by_siren(siren)

    @mcp.tool()
    async def sirene_etablissement(siret: str) -> dict:
        """Fetch a French establishment by SIRET (14 digits) from INSEE SIRENE."""
        return client.get_siret(siret)

    @mcp.tool()
    async def sirene_headquarters(siren: str) -> Optional[dict]:
        """Fetch the headquarters establishment (siège) for a SIREN."""
        return client.get_headquarters(siren)
