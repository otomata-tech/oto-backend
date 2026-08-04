"""Lemlist — campagnes, séquences, leads, stats, et lead lifecycle.

Volontairement borné : `lemlist_create_lead`/`lemlist_launch_lead`/
`lemlist_add_lead_variables` exposent la création + le lancement d'un lead et
la pose de ses variables — mais pas créer/pauser une campagne ni supprimer un
lead. Un mauvais call LLM peut là aussi déclencher un envoi involontairement,
donc ce périmètre reste la porte d'entrée écrite minimale ; le reste passe par
l'UI Lemlist.

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
        key, is_platform = access.resolve_api_key("lemlist")
        return LemlistClient(api_key=key), is_platform

    def _record_if_platform(is_platform: bool) -> None:
        if is_platform:
            access.record_platform_usage("lemlist")

    @mcp.tool()
    def lemlist_status() -> dict:
        """Workspace status (account, credits, plan)."""
        client, is_platform = _client()
        result = client.status()
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_list_campaigns() -> dict:
        """List all campaigns in the workspace.

        Returns a list of `{id, name, status, senders, emoji}`. Use `id` for
        the other lemlist tools.
        """
        client, is_platform = _client()
        campaigns = client.list_campaigns()
        _record_if_platform(is_platform)
        return {"campaigns": [asdict(c) for c in campaigns]}

    @mcp.tool()
    def lemlist_get_campaign(campaign_id: str) -> dict:
        """Fetch full campaign details by ID."""
        client, is_platform = _client()
        result = client.get_campaign(campaign_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_get_campaign_stats(campaign_id: str) -> dict:
        """Get campaign performance stats (sent, opened, replied, bounced…)."""
        client, is_platform = _client()
        result = client.get_campaign_stats(campaign_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_get_activities(
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
    def lemlist_get_leads(campaign_id: str) -> dict:
        """List all leads for a campaign with their state (sent, replied…)."""
        client, is_platform = _client()
        leads = client.get_all_leads(campaign_id)
        _record_if_platform(is_platform)
        return {"leads": leads}

    @mcp.tool()
    def lemlist_create_lead(
        campaign_id: str,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        company_name: Optional[str] = None,
        job_title: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        phone: Optional[str] = None,
        company_domain: Optional[str] = None,
        icebreaker: Optional[str] = None,
        timezone: Optional[str] = None,
        contact_owner: Optional[str] = None,
        custom_variables: Optional[dict] = None,
        deduplicate: bool = False,
        linkedin_enrichment: bool = False,
        find_email: bool = False,
        verify_email: bool = False,
        find_phone: bool = False,
    ) -> dict:
        """Create a lead in a campaign.

        All lead fields are optional (lemlist accepts phone/LinkedIn-only
        leads), but you'll usually pass at least `email` or `linkedin_url`.
        `custom_variables` merges extra key-value pairs into the lead, used for
        campaign personalization (e.g. `{{variableName}}` in a template).

        Enrichment flags (all default False, each may cost lemlist credits):
        `deduplicate` skips the insert if the email already exists in another
        campaign, `linkedin_enrichment` runs LinkedIn enrichment,
        `find_email`/`verify_email` find or verify the email, `find_phone`
        finds a phone number.

        Returns the created lead, including `_id` — pass it to
        `lemlist_launch_lead`/`lemlist_add_lead_variables`. If the campaign has
        review-before-send enabled, the lead is created paused and won't send
        until `lemlist_launch_lead` is called.
        """
        lead = {
            k: v for k, v in {
                "email": email,
                "firstName": first_name,
                "lastName": last_name,
                "companyName": company_name,
                "jobTitle": job_title,
                "linkedinUrl": linkedin_url,
                "phone": phone,
                "companyDomain": company_domain,
                "icebreaker": icebreaker,
                "timezone": timezone,
                "contactOwner": contact_owner,
            }.items() if v is not None
        }
        if custom_variables:
            lead.update(custom_variables)
        client, is_platform = _client()
        result = client.create_lead(
            campaign_id, lead,
            deduplicate=deduplicate, linkedin_enrichment=linkedin_enrichment,
            find_email=find_email, verify_email=verify_email, find_phone=find_phone,
        )
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_launch_lead(lead_id: str) -> dict:
        """Launch a lead that's paused for manual review.

        Only relevant for a campaign with review-before-send enabled — such a
        campaign leaves a newly created lead paused until launched. Returns
        `{"ok": true}` on success; raises with a lemlist error code if it can't
        launch (already launched, paused, no sender available, invalid AI
        variable, campaign step errors…).
        """
        client, is_platform = _client()
        result = client.launch_lead(lead_id)
        _record_if_platform(is_platform)
        return result

    @mcp.tool()
    def lemlist_add_lead_variables(lead_id: str, variables: dict) -> dict:
        """Set custom variables on a lead — merged into its personalization
        data, e.g. for `{{variableName}}` placeholders in campaign templates."""
        client, is_platform = _client()
        result = client.add_lead_variables(lead_id, variables)
        _record_if_platform(is_platform)
        return result
