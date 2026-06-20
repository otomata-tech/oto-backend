"""SerpApi — recherche d'offres d'emploi (Google Jobs).

Wrappe `oto.tools.serpapi.SerpAPIClient` (clé SerpApi). Clé résolue par appel via
`access.resolve_api_key("serpapi")` — byo (clé user sur /account ou credential
partagé de l'org). Pas de clé plateforme.

Pourquoi SerpApi et pas Serper pour les jobs : Serper n'expose **aucun** vertical
Google Jobs ; SerpApi a le moteur dédié `google_jobs` (offres structurées :
titre, entreprise, localisation, liens de candidature, date) + `google_jobs_listing`
pour le détail d'une offre. Complète les ATS (offres ouvertes d'une cible, veille
marché) côté sourcing.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.serpapi.client import SerpAPIClient

    def _client() -> SerpAPIClient:
        key, _ = access.resolve_api_key("serpapi", "SERPAPI_API_KEY")
        return SerpAPIClient(api_key=key)

    @mcp.tool()
    async def serpapi_search_jobs(
        query: Optional[str] = None,
        company: Optional[str] = None,
        location: Optional[str] = None,
        country: Optional[str] = None,
        language: str = "en",
        max_results: int = 50,
        no_cache: bool = False,
    ) -> dict:
        """Search Google Jobs for live job postings (job-board sourcing).

        Args:
            query: free-text job query, e.g. "data engineer Paris", "senior
                python remote". Preferred for general sourcing.
            company: shortcut — if `query` is omitted, searches "<company> jobs".
            location: e.g. "Paris, France".
            country: 2-letter code, e.g. "fr", "us" (Google `gl`).
            language: language code (Google `hl`).
            max_results: max postings (pagination handled).
            no_cache: force fresh results.

        Returns the SerpApi payload incl. `jobs_results` (each with a `job_id`
        usable in serpapi_job_details).
        """
        return _client().search_jobs(
            query=query, company=company, location=location, country=country,
            language=language, max_results=max_results, no_cache=no_cache)

    @mcp.tool()
    async def serpapi_job_details(job_id: str) -> dict:
        """Fetch the full detail of one job posting by its Google Jobs `job_id`
        (apply options, full description) — `job_id` comes from serpapi_search_jobs."""
        return _client().get_job_details(job_id)
