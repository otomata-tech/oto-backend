"""DVF — transactions immobilières (open data Etalab geo-dvf).

Valorisation par comparables pour use cases patrimoniaux/immo. Pas de clé.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from oto.tools.dvf import DvfClient

    client = DvfClient()

    @mcp.tool()
    async def dvf_stats(
        code_commune: str,
        type_local: Optional[str] = None,
        years: int = 3,
    ) -> dict:
        """Real-estate price stats (€/m²) for a French commune, from DVF open data.

        Median/mean/min/max €/m² + per-year breakdown, computed on clean
        mono-bien sales (one Appartement or Maison per mutation — multi-lot
        sales excluded, outliers <100 or >50000 €/m² filtered).

        Use to value a property by comparables (CGP, immo due diligence).

        Args:
            code_commune: INSEE code, 5 digits (e.g. "13201" = Marseille 1er).
            type_local: "Appartement" | "Maison" (default: both).
            years: lookback in years WITH data — DVF lags ~6 months so the
                   current calendar year is skipped if empty (default 3).
        """
        return client.stats(code_commune=code_commune, type_local=type_local, years=years)

    @mcp.tool()
    async def dvf_comparables_by_address(
        adresse: str,
        radius_m: int = 500,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 3,
        limit: int = 50,
    ) -> dict:
        """Comparable sales around a precise address (geocode + radius filter).

        Geocodes the free-form address via the BAN (Base Adresse Nationale),
        then returns DVF mono-bien sales within `radius_m` metres, sorted by
        distance, with a `distance_m` field and the local median €/m². Much
        sharper than commune-level stats for valuing one specific property.

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
            radius_m: search radius in metres around the geocoded point (default 500).
            type_local: "Appartement" | "Maison" (default: both).
            surface_min / surface_max: surface bâtie band m².
            years: lookback in years with data (default 3).
            limit: max comparables, nearest first (default 50).
        """
        return client.comparables_by_address(
            adresse=adresse,
            radius_m=radius_m,
            type_local=type_local,
            surface_min=surface_min,
            surface_max=surface_max,
            years=years,
            limit=limit,
        )

    @mcp.tool()
    async def dvf_comparables(
        code_commune: str,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 2,
        limit: int = 50,
    ) -> dict:
        """Comparable real-estate transactions for a commune, from DVF open data.

        Returns individual mono-bien sales (date, valeur_fonciere, surface,
        €/m², adresse, lat/lon), most recent first. Filter by type and surface
        band to find true comparables for a given property.

        Args:
            code_commune: INSEE code, 5 digits.
            type_local: "Appartement" | "Maison" (default: both).
            surface_min: min surface bâtie m².
            surface_max: max surface bâtie m².
            years: lookback in years with data (default 2).
            limit: max comparables returned, most recent first (default 50).
        """
        return client.comparables(
            code_commune=code_commune,
            type_local=type_local,
            surface_min=surface_min,
            surface_max=surface_max,
            years=years,
            limit=limit,
        )
