"""Culture (Ministère de la Culture open data) — MCP wrappers.

Currently exposes the LES dataset (Licences entrepreneurs spectacles vivants).
Adds composed AND filters and group_by aggregation on top of what the
official `mcp.data.gouv.fr` MCP can do.
"""

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.culture import SpectacleClient

    client = SpectacleClient()

    @mcp.tool()
    async def culture_spectacle_search(
        status: str = "Valide",
        categorie: Optional[str] = None,
        naf: Optional[str] = None,
        region: Optional[str] = None,
        departement: Optional[str] = None,
        code_postal: Optional[str] = None,
        siren: Optional[str] = None,
        type_declarant_like: Optional[str] = None,
        deposited_since: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
    ) -> dict:
        """Search Licences Entrepreneurs Spectacle (LES) — French open data.

        Composed AND filters: this is the gap vs the official datagouv MCP
        which only supports one filter at a time. Returns ~63k valid licences
        as of 2026.

        Args:
            status: Valide|Invalide|Expiré|Invalidé|En instruction (case-sensitive).
            categorie: 1 (lieu), 2 (producteur), 3 (diffuseur).
            naf: NAF prefix like "90.01Z" or "9001Z" (dot optional).
            region: e.g. "Île-de-France", "Provence-Alpes-Côte d'Azur".
            departement: e.g. "Paris", "Bouches-du-Rhône".
            code_postal: 5-digit French postal code.
            siren: SIREN (9) or SIRET (14) prefix.
            type_declarant_like: substring of legal form, e.g. "privé",
                "association", "public", "EURL".
            deposited_since: YYYY-MM-DD lower bound on date_depot_dossier.
            limit: 1-100 per page.
            offset: pagination offset.
        """
        return client.search(
            status=status, categorie=categorie, naf=naf, region=region,
            departement=departement, code_postal=code_postal, siren=siren,
            type_declarant_like=type_declarant_like, deposited_since=deposited_since,
            limit=limit, offset=offset,
        )

    @mcp.tool()
    async def culture_spectacle_get(siren: str) -> dict:
        """Fetch all récépissés (L1/L2/L3 categories) for a given SIREN/SIRET.

        A single structure often holds multiple licences across categories;
        use this to pivot from a SIREN to its full LES footprint.

        Args:
            siren: SIREN (9 digits) or SIRET (14 digits) prefix.
        """
        return client.get(siren)

    @mcp.tool()
    async def culture_spectacle_stats(
        group_by: str,
        status: str = "Valide",
        categorie: Optional[str] = None,
        naf: Optional[str] = None,
        region: Optional[str] = None,
        departement: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Group-by aggregation on LES — fills the gap of the official datagouv MCP.

        Use cases: distribution NAF among L2/L3 producers, top régions for
        active spectacle entities, count per départment for territorial
        targeting.

        Args:
            group_by: Field to group on. Common: "code_naf_ape",
                "region_siret", "departement_siret", "categorie",
                "type_declarant".
            status, categorie, naf, region, departement: scope filters
                (same semantics as culture_spectacle_search).
            limit: Number of buckets to return.
        """
        return client.stats(
            group_by,
            where_filters={
                "status": status, "categorie": categorie, "naf": naf,
                "region": region, "departement": departement,
            },
            limit=limit,
        )

    @mcp.tool()
    async def culture_spectacle_export_url(
        fmt: str = "csv",
        status: Optional[str] = "Valide",
        categorie: Optional[str] = None,
        naf: Optional[str] = None,
        region: Optional[str] = None,
        departement: Optional[str] = None,
    ) -> dict:
        """Build a direct export URL for a filtered subset of LES.

        The caller (or another tool) fetches the URL — the full valid set is
        ~6 MB CSV, regional subsets are smaller.

        Args:
            fmt: csv|json|parquet|xlsx.
            status, categorie, naf, region, departement: scope filters.
        """
        return {
            "url": client.export_url(
                fmt=fmt, status=status, categorie=categorie, naf=naf,
                region=region, departement=departement,
            ),
            "format": fmt,
        }
