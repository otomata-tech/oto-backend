"""Apollo.io — B2B prospection (organizations, people, job postings).

Wrappe `oto.tools.apollo.ApolloClient`. Clé résolue par appel via
`access.resolve_api_key("apollo")` — byo (user key sur /account ou credential
d'org). Pas de clé plateforme.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.apollo.client import ApolloClient

    def _client() -> ApolloClient:
        key, _ = access.resolve_api_key("apollo")
        return ApolloClient(api_key=key)

    @mcp.tool()
    def apollo_search_organizations(
        name: Optional[str] = None,
        domain: Optional[str] = None,
        country: Optional[str] = None,
        per_page: int = 10,
    ) -> dict:
        """Search Apollo organizations by name, domain and/or country."""
        return _client().search_organizations(
            name=name, domain=domain, country=country, per_page=per_page)

    @mcp.tool()
    def apollo_enrich_organization(domain: str) -> dict:
        """Enrich a company from its domain (firmographics, size, industry…)."""
        return _client().enrich_organization(domain)

    @mcp.tool()
    def apollo_search_people(
        domains: Optional[list[str]] = None,
        org_ids: Optional[list[str]] = None,
        departments: Optional[list[str]] = None,
        titles: Optional[list[str]] = None,
        seniorities: Optional[list[str]] = None,
        per_page: int = 25,
        page: int = 1,
    ) -> dict:
        """Search people by company domains/ids, departments, titles, seniorities.

        Args:
            departments: e.g. ["engineering", "sales"].
            seniorities: e.g. ["c_suite", "director", "manager"].
        """
        return _client().search_people(
            domains=domains, org_ids=org_ids, departments=departments,
            titles=titles, seniorities=seniorities, per_page=per_page, page=page)

    @mcp.tool()
    def apollo_match_person(
        linkedin_url: Optional[str] = None,
        email: Optional[str] = None,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        name: Optional[str] = None,
        domain: Optional[str] = None,
        org_name: Optional[str] = None,
    ) -> dict:
        """Match a single person (enrichment). Returns {} if no match.

        Pass the strongest identifier you have (linkedin_url or email best).
        """
        return _client().match_person(
            linkedin_url=linkedin_url, email=email, first_name=first_name,
            last_name=last_name, name=name, domain=domain, org_name=org_name) or {}

    @mcp.tool()
    def apollo_job_postings(org_id: str) -> dict:
        """List active job postings for an Apollo organization id (hiring signal)."""
        return _client().get_job_postings(org_id)
