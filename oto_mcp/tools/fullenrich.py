"""FullEnrich — waterfall multi-provider contact enrichment (phones + emails).

~70% phone hit rate. Async bulk API (POST → poll). Pay-per-result.
Provider user-only : chaque user pose sa clé FULLENRICH_API_KEY.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.fullenrich.client import FullenrichClient

    def _client() -> tuple[FullenrichClient, bool]:
        key, is_platform = access.resolve_api_key("fullenrich")
        return FullenrichClient(api_key=key), is_platform

    @mcp.tool()
    async def fullenrich_enrich_linkedin(
        linkedin_slug: str,
        first_name: str,
        last_name: str,
        company_name: Optional[str] = None,
    ) -> dict:
        """Enrich a LinkedIn profile with phones + emails via FullEnrich (waterfall 20+ providers).

        Args:
            linkedin_slug: LinkedIn slug (e.g. "alexis-laporte") — NOT a full URL.
            first_name: Contact's first name.
            last_name: Contact's last name.
            company_name: Optional company name (improves matching).

        Returns dict with: linkedin_slug, first_name, last_name, full_name, title,
        company_name, phones[], work_emails[], personal_emails[], location, fetched_at.
        Returns {"found": false} if no data found.

        Cost: 10 credits/phone, 1 credit/work_email, 3 credits/personal_email.
        Pay-per-result only (no charge if nothing found).
        Async: POST job → poll (~30s to 4min).
        """
        client, is_platform = _client()
        profile = client.enrich_linkedin(
            linkedin_slug=linkedin_slug,
            first_name=first_name,
            last_name=last_name,
            company_name=company_name,
        )
        if is_platform:
            access.record_platform_usage("fullenrich")
        if profile is None:
            return {"found": False, "linkedin_slug": linkedin_slug}
        return {"found": True, **profile.to_dict()}
