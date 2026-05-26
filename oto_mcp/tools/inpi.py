"""INPI/BCE — bilans financiers (ratios Banque de France, open data). Pas de clé requise."""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.inpi import InpiClient

    client = InpiClient()

    @mcp.tool()
    async def inpi_list_exercises(siren: str) -> dict:
        """List available INPI/BCE annual filings for a SIREN.

        Returns exercise dates, bilan type (C=complet, S=simplifié, K=consolidé),
        confidentiality status, and chiffre d'affaires for each year.
        Typically 3-9 years of history. Use this to pick which exercise
        to fetch in detail via inpi_get_bilan.

        Args:
            siren: SIREN number (9 digits).
        """
        items = client.list_exercises(siren)
        return {"siren": siren, "items": items, "total": len(items)}

    @mcp.tool()
    async def inpi_get_bilan(siren: str, date_cloture: str) -> dict:
        """Fetch one INPI/BCE annual filing with full financial ratios.

        Returns: CA, EBE, EBIT, résultat net, marge EBE, autonomie financière,
        taux d'endettement, liquidité, vétusté, BFR, rotation stocks,
        crédit clients/fournisseurs, couverture intérêts.
        Use inpi_list_exercises first to discover available dates.

        Args:
            siren: SIREN number (9 digits).
            date_cloture: Exercise closing date (YYYY-MM-DD, e.g. "2024-12-31").
        """
        result = client.get_bilan(siren, date_cloture)
        if result is None:
            return {"error": "exercise_not_found", "siren": siren, "date_cloture": date_cloture}
        return result
