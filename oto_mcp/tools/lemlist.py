"""Lemlist — lecture des campagnes, séquences, leads et stats.

Read/stats only : on n'expose pas les writes (création/pause de campagne,
ajout/suppression de lead) car un mauvais call LLM peut envoyer une
campagne involontairement. Pour modifier, passer par l'UI Lemlist.

Clé résolue par appel via `access.resolve_api_key("lemlist")`. Pas de
quota plateforme par défaut — chaque user voit SES propres campagnes,
donc user key obligatoire.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.lemlist import LemlistClient

    def _client() -> tuple[LemlistClient, bool]:
        key, is_platform = access.resolve_api_key("lemlist", "LEMLIST_API_KEY")
        return LemlistClient(api_key=key), is_platform

    def _record_if_platform(is_platform: bool) -> None:
        if is_platform:
            access.record_platform_usage("lemlist")

    @mcp.tool()
    async def lemlist_status() -> dict:
        """Workspace status (account, credits, plan)."""
        client, is_platform = _client()
        result = client.status()
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def lemlist_list_campaigns() -> dict:
        """List all campaigns in the workspace.

        Returns a list of `{id, name, status, senders, emoji}`. Use `id` for
        the other lemlist tools.
        """
        client, is_platform = _client()
        campaigns = client.list_campaigns()
        _record_if_platform(is_platform)
        return {"campaigns": [asdict(c) for c in campaigns]}

    @mcp.tool()
    async def lemlist_get_campaign(campaign_id: str) -> dict:
        """Fetch full campaign details by ID."""
        client, is_platform = _client()
        result = client.get_campaign(campaign_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def lemlist_get_campaign_stats(campaign_id: str) -> dict:
        """Get campaign performance stats (sent, opened, replied, bounced…)."""
        client, is_platform = _client()
        result = client.get_campaign_stats(campaign_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    async def lemlist_get_activities(
        campaign_id: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Get recent activity events (opens, clicks, replies…).

        Args:
            campaign_id: Restrict to a campaign (optional).
            limit: Max events (default 100).
            offset: Pagination offset.
        """
        client, is_platform = _client()
        events = client.get_activities(
            campaign_id=campaign_id, limit=limit, offset=offset,
        )
        _record_if_platform(is_platform)
        return {"activities": events}

    @mcp.tool()
    async def lemlist_get_leads(campaign_id: str) -> dict:
        """List all leads for a campaign with their state (sent, replied…)."""
        client, is_platform = _client()
        leads = client.get_all_leads(campaign_id)
        _record_if_platform(is_platform)
        return {"leads": leads}
