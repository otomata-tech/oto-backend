"""INSEE SIRENE — données plus granulaires que recherche-entreprises (établissements / SIRET).

Clé résolue par appel : user key (`/account`) prioritaire, sinon platform
key + quota daily (member). Guest doit poser sa propre clé.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.sirene import SireneClient

    def _client() -> tuple[SireneClient, bool]:
        key, is_platform = access.resolve_api_key("sirene", "SIRENE_API_KEY")
        return SireneClient(api_key=key), is_platform

    @mcp.tool()
    async def sirene_search(
        query: Optional[str] = None,
        naf: Optional[str] = None,
        postal: Optional[str] = None,
        commune: Optional[str] = None,
        employees: Optional[str] = None,
        headquarters_only: bool = True,
        limit: int = 20,
    ) -> dict:
        """Search French establishments (SIRET) in INSEE SIRENE.

        Plus précis que recherche-entreprises pour filtrer par effectifs ou
        établissements précis. Renvoie une liste d'établissements (SIRET).

        Pour filtrer par département, utiliser `recherche_entreprises_search`
        à la place — INSEE SIRENE n'expose pas ce filtre dans `oto-cli`.

        Args:
            query: Free-text query (company name).
            naf: NAF codes, comma-separated.
            postal: Postal code (préfixes acceptés ex `13` pour Bouches-du-Rhône).
            commune: City name.
            employees: TEFEN employee-range codes, comma-separated.
            headquarters_only: If true, returns only sièges (default).
            limit: Max results.
        """
        client, is_platform = _client()
        results = client.search_siret(
            name=query,
            naf=[s.strip() for s in naf.split(",")] if naf else None,
            employees=[s.strip() for s in employees.split(",")] if employees else None,
            postal_code=postal,
            city=commune,
            headquarters_only=headquarters_only,
            limit=limit,
        )
        if is_platform:
            access.record_platform_usage("sirene")
        ets = results.get("etablissements", [])
        return {
            "total": results.get("header", {}).get("total", len(ets)),
            "count": len(ets),
            "etablissements": ets,
        }

    @mcp.tool()
    async def sirene_get(siren: str) -> dict:
        """Fetch a French legal unit by SIREN (9 digits) from INSEE SIRENE."""
        client, is_platform = _client()
        result = client.get_by_siren(siren)
        if is_platform:
            access.record_platform_usage("sirene")
        return result

    @mcp.tool()
    async def sirene_etablissement(siret: str) -> dict:
        """Fetch a French establishment by SIRET (14 digits) from INSEE SIRENE."""
        client, is_platform = _client()
        result = client.get_siret(siret)
        if is_platform:
            access.record_platform_usage("sirene")
        return result

    @mcp.tool()
    async def sirene_headquarters(siren: str) -> Optional[dict]:
        """Fetch the headquarters establishment (siège) for a SIREN."""
        client, is_platform = _client()
        result = client.get_headquarters(siren)
        if is_platform:
            access.record_platform_usage("sirene")
        return result
