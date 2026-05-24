"""Kaspr — enrichissement contacts B2B depuis URL LinkedIn (emails + téléphones).

Provider user-only : pas de quota plateforme, chaque user pose sa clé sur
`/account`. Kaspr facture en crédits à l'enrichissement.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.kaspr.client import KasprClient

    def _client() -> tuple[KasprClient, bool]:
        key, is_platform = access.resolve_api_key("kaspr", "KASPR_API_KEY")
        return KasprClient(api_key=key), is_platform

    @mcp.tool()
    async def kaspr_verify_key() -> dict:
        """Verify the configured Kaspr API key — returns account info + remaining credits."""
        client, is_platform = _client()
        result = client.verify_key()
        if is_platform:
            access.record_platform_usage("kaspr")
        return result

    @mcp.tool()
    async def kaspr_enrich_linkedin(
        linkedin_id: str,
        name: Optional[str] = None,
        with_phone: bool = False,
        data_to_get: Optional[list[str]] = None,
    ) -> dict:
        """Enrich a LinkedIn profile with emails and (optionally) phone numbers.

        Args:
            linkedin_id: LinkedIn slug (e.g. "alexis-laporte") or full URL.
            name: Optional fallback name if the slug alone is ambiguous.
            with_phone: Request mobile/work phones (extra credits cost).
            data_to_get: Subset of fields to retrieve (Kaspr-specific, e.g.
                ["emails", "phones", "company"]). Defaults to all.

        Cost: 1 credit per email, +1 per phone if `with_phone=True`.
        """
        client, is_platform = _client()
        # with_phone=True → include "phone" in data_to_get (costs extra credits)
        effective_data = data_to_get
        if effective_data is None and with_phone:
            effective_data = ["workEmail", "phone"]
        result = client.enrich_linkedin(
            linkedin_id=linkedin_id,
            name=name,
            is_phone_required=with_phone,
            data_to_get=effective_data,
        )
        if is_platform:
            access.record_platform_usage("kaspr")
        return result
