"""data.gouv.fr "API Recherche Entreprises" — pas de clé requise."""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.sirene import EntreprisesClient

    client = EntreprisesClient()

    @mcp.tool()
    async def recherche_entreprises_search(
        query: Optional[str] = None,
        naf: Optional[str] = None,
        departement: Optional[str] = None,
        code_postal: Optional[str] = None,
        commune: Optional[str] = None,
        employees: Optional[str] = None,
        ca_min: Optional[int] = None,
        ca_max: Optional[int] = None,
        idcc: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Search French companies via data.gouv.fr's "API Recherche Entreprises".

        Returns enriched data: identity, siege (HQ), NAF, employees, dirigeants,
        finances, matched establishments. At least one filter is required.

        Args:
            query: Full-text search (company name, SIREN, brand…).
            naf: NAF activity codes, comma-separated (e.g. "62.01Z,62.02A").
            departement: French department code (e.g. "75").
            code_postal: Postal code (e.g. "75001").
            commune: City name.
            employees: Employee-range codes (INSEE TEFEN), comma-separated.
            ca_min: Minimum turnover in euros.
            ca_max: Maximum turnover in euros.
            idcc: IDCC codes (conventions collectives), comma-separated
                (e.g. "1285,3090" pour spectacle vivant subventionné + privé).
            page: 1-based page number.
            per_page: Page size, capped at 25 by upstream.
        """
        return client.search(
            query=query,
            naf=[s.strip() for s in naf.split(",")] if naf else None,
            departement=departement,
            code_postal=code_postal,
            commune=commune,
            employees=[s.strip() for s in employees.split(",")] if employees else None,
            ca_min=ca_min,
            ca_max=ca_max,
            idcc=[s.strip() for s in idcc.split(",")] if idcc else None,
            page=page,
            per_page=per_page,
        )

    @mcp.tool()
    async def recherche_entreprises_get(siren: str) -> Optional[dict]:
        """Fetch a single French company by SIREN (9 digits).

        Returns the full enriched record (siege, dirigeants, finances,
        matching_etablissements…) or null if not found.
        """
        return client.get_by_siren(siren)

    @mcp.tool()
    async def recherche_entreprises_directors(siren: str) -> list[dict]:
        """List the directors (`dirigeants`) of a French company by SIREN."""
        return client.get_directors(siren)

    @mcp.tool()
    async def recherche_entreprises_finances(siren: str) -> Optional[dict]:
        """Return the financial data block (`finances`) for a SIREN, or null."""
        return client.get_finances(siren)
