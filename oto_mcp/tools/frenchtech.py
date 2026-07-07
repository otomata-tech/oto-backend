"""French Tech ecosystem — regional capital directory, events, financing.

Live WordPress REST API of a French Tech capital site (default: Aix-Marseille) —
open data, no key. Companies (startups / support structures / providers) with
director + email + phone, plus events, calls for projects and financing schemes.
Bonus: French Tech Central bookable scenarios (state correspondents) via Synbird.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from .. import fod_frenchtech

    # Écosystème French Tech servi par le service FOD (ADR 0028 B3) — proxy à surface
    # identique à FrenchTechClient (Aix-Marseille par défaut côté FOD).
    ft = fod_frenchtech.ft

    @mcp.tool()
    def frenchtech_search_annuaire(
        query: Optional[str] = None,
        secteur: Optional[str] = None,
        ville: Optional[str] = None,
        type_annuaire: Optional[str] = None,
        all_results: bool = False,
        per_page: int = 100,
    ) -> dict:
        """Search the French Tech ecosystem directory (~694 companies).

        Returns companies with name, pitch, director, email, phone, website, city,
        sectors, headcount, turnover, needs. A rich B2B prospecting dataset.

        Args:
            query: Full-text search (company name / content).
            secteur: Sector taxonomy term name (exact, resolved to id).
            ville: Filter on the ACF city field (client-side, exact match).
            type_annuaire: Directory type term name (startup / support structure / …).
            all_results: Paginate through everything (~694). Else first page only.
            per_page: Page size (max 100).
        """
        return ft.search_annuaire(
            query=query, secteur=secteur, ville=ville,
            type_annuaire=type_annuaire, all_results=all_results, per_page=per_page,
        )

    @mcp.tool()
    def frenchtech_get_annuaire(slug: str) -> Optional[dict]:
        """Get a single directory company by its slug."""
        return ft.get_annuaire(slug)

    @mcp.tool()
    def frenchtech_membres(query: Optional[str] = None, all_results: bool = False) -> dict:
        """List community members (named people fiches)."""
        return ft.list_membres(query=query, all_results=all_results)

    @mcp.tool()
    def frenchtech_evenements(query: Optional[str] = None, all_results: bool = False) -> dict:
        """List agenda events (meetups, conferences), most recent first."""
        return ft.list_evenements(query=query, all_results=all_results)

    @mcp.tool()
    def frenchtech_appels(query: Optional[str] = None, all_results: bool = False) -> dict:
        """List calls for projects / competitions / AMI (appels à candidatures)."""
        return ft.list_appels(query=query, all_results=all_results)

    @mcp.tool()
    def frenchtech_financements(query: Optional[str] = None, all_results: bool = False) -> dict:
        """List financing schemes (with type, amount, stage, eligibility)."""
        return ft.list_financements(query=query, all_results=all_results)

    @mcp.tool()
    def frenchtech_ftc_scenarios() -> dict:
        """List French Tech Central bookable scenarios (RDV with state correspondents)."""
        return ft.ftc_scenarios()
