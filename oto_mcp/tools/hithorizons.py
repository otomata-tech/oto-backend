"""HitHorizons — European company data (search + details).

Wrappe `oto.tools.hithorizons.HitHorizonsClient`. Clé résolue par appel via
`access.resolve_api_key("hithorizons")` — byo. En-tête `Ocp-Apim-Subscription-Key`
géré côté client. Pays par défaut FR (override par tool).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.hithorizons.client import HitHorizonsClient

    def _client(country: str = "FR") -> HitHorizonsClient:
        key, _ = access.resolve_api_key("hithorizons")
        return HitHorizonsClient(api_key=key, country=country)

    @mcp.tool()
    async def hithorizons_search_company(
        name: str,
        city: Optional[str] = None,
        postal_code: Optional[str] = None,
        country: str = "FR",
        max_results: int = 5,
    ) -> dict:
        """Search companies by name (+ optional city / postal code).

        Args:
            country: ISO country code (default FR).
        """
        return {"results": _client(country).search_company(
            name, city=city, postal_code=postal_code, max_results=max_results)}

    @mcp.tool()
    async def hithorizons_search_unstructured(
        name: str,
        address: Optional[str] = None,
        country: str = "FR",
        max_results: int = 5,
    ) -> dict:
        """Search companies with a free-text name + address string."""
        return {"results": _client(country).search_unstructured(
            name, address=address, max_results=max_results)}

    @mcp.tool()
    async def hithorizons_company(company_id: str, country: str = "FR") -> dict:
        """Fetch full company details by HitHorizons company id. {} if not found."""
        return _client(country).get_detail(company_id) or {}

    @mcp.tool()
    async def hithorizons_suggestions(
        query: str, country: str = "FR", max_results: int = 10,
    ) -> dict:
        """Company-name autocomplete suggestions."""
        return {"suggestions": _client(country).suggestions(query, max_results=max_results)}
