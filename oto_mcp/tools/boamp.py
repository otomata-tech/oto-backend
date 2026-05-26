"""BOAMP — appels d'offres publics (open data DILA). Pas de clé requise."""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.boamp import BoampClient

    client = BoampClient()

    @mcp.tool()
    async def boamp_search(
        query: Optional[str] = None,
        descripteur: Optional[str] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        type_marche: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Search French public procurement notices (BOAMP).

        Useful for finding public tenders by keyword, sector, department,
        or date range. Returns notice title, buyer, deadline, department, and URL.

        Args:
            query: Full-text search in the notice subject (objet).
            descripteur: BOAMP descriptor label (e.g. "Photovoltaïque", "Informatique").
            departement: French department code (e.g. "75").
            date_from: Publication date start (YYYY-MM-DD).
            date_to: Publication date end (YYYY-MM-DD).
            type_marche: Market type filter (TRAVAUX, FOURNITURES, SERVICES).
            limit: Max results (default 20, max 100).
        """
        return client.search(
            query=query, descripteur=descripteur, departement=departement,
            date_from=date_from, date_to=date_to, type_marche=type_marche,
            limit=limit,
        )

    @mcp.tool()
    async def boamp_get(idweb: str) -> dict:
        """Fetch a single BOAMP notice by its ID.

        Args:
            idweb: BOAMP notice identifier (e.g. "20-12345").
        """
        result = client.get(idweb)
        if result is None:
            return {"error": "not_found", "idweb": idweb}
        return result
