"""Données entreprise France — identité, finances, événements légaux, appels d'offres.

Sources open data (pas de clé) : API Recherche Entreprises, INPI/BCE, BODACC, BOAMP.
Source payante (clé SIRENE) : INSEE SIRENE (SIRET, siège).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.sirene import EntreprisesClient, SireneClient
    from oto.tools.inpi import InpiClient
    from oto.tools.bodacc import BodaccClient
    from oto.tools.boamp import BoampClient

    entreprises = EntreprisesClient()
    inpi = InpiClient()
    bodacc = BodaccClient()
    boamp = BoampClient()

    # --- Identité (API Recherche Entreprises, open data) ---

    @mcp.tool()
    async def fr_search(
        query: Optional[str] = None,
        naf: Optional[str] = None,
        departement: Optional[str] = None,
        code_postal: Optional[str] = None,
        commune: Optional[str] = None,
        employees: Optional[str] = None,
        ca_min: Optional[int] = None,
        ca_max: Optional[int] = None,
        idcc: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Search French companies — returns identity, HQ, NAF, employees,
        directors, finances, matched establishments. At least one filter required.

        Args:
            query: Full-text search (company name, SIREN, brand…).
            naf: NAF activity codes, comma-separated (e.g. "62.01Z,62.02A").
            departement: Department code (e.g. "75").
            code_postal: Postal code (e.g. "75001").
            commune: City name.
            employees: Employee-range codes (INSEE TEFEN), comma-separated.
            ca_min: Minimum turnover in euros.
            ca_max: Maximum turnover in euros.
            idcc: IDCC codes (conventions collectives), comma-separated.
            page: 1-based page number.
            per_page: Page size (max 25).
        """
        return entreprises.search(
            query=query,
            naf=[s.strip() for s in naf.split(",")] if naf else None,
            departement=departement,
            code_postal=code_postal,
            commune=commune,
            employees=[s.strip() for s in employees.split(",")] if employees else None,
            ca_min=ca_min, ca_max=ca_max,
            idcc=[s.strip() for s in idcc.split(",")] if idcc else None,
            page=page, per_page=per_page,
        )

    @mcp.tool()
    async def fr_get(siren: str) -> dict:
        """Full company profile by SIREN: identity (siège, directors, NAF,
        employees) + latest INPI/BCE financial ratios + recent BODACC legal
        events. Aggregates 3 open data sources in parallel.
        Use this as first call when investigating a company.

        Args:
            siren: SIREN number (9 digits).
        """
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_identity = pool.submit(entreprises.get_by_siren, siren)
            f_bilans = pool.submit(inpi.list_exercises, siren)
            f_events = pool.submit(bodacc.search_by_siren, siren, None, 10)

        identity = f_identity.result()
        if not identity:
            return {"error": "not_found", "siren": siren}

        exercises = f_bilans.result()
        latest_bilan = None
        if exercises:
            latest_bilan = inpi.get_bilan(siren, exercises[0]["date_cloture_exercice"])

        events_data = f_events.result()

        return {
            "siren": siren,
            "identity": identity,
            "latest_bilan": latest_bilan,
            "recent_events": events_data.get("results", []),
            "events_total": events_data.get("total_count", 0),
        }

    @mcp.tool()
    async def fr_directors(siren: str) -> list[dict]:
        """List directors (dirigeants) of a French company.

        Args:
            siren: SIREN number (9 digits).
        """
        return entreprises.get_directors(siren)

    # --- INSEE SIRENE (clé payante) ---

    def _sirene_client() -> tuple[SireneClient, bool]:
        key, is_platform = access.resolve_api_key("sirene", "SIRENE_API_KEY")
        return SireneClient(api_key=key), is_platform

    @mcp.tool()
    async def fr_siret(siret: str) -> dict:
        """Fetch a French establishment by SIRET (14 digits) from INSEE SIRENE.

        Args:
            siret: SIRET number (14 digits).
        """
        client, is_platform = _sirene_client()
        result = client.get_siret(siret)
        if is_platform:
            access.record_platform_usage("sirene")
        return result

    @mcp.tool()
    async def fr_headquarters(siren: str) -> Optional[dict]:
        """Fetch the headquarters (siège) of a company from INSEE SIRENE.

        Args:
            siren: SIREN number (9 digits).
        """
        client, is_platform = _sirene_client()
        result = client.get_headquarters(siren)
        if is_platform:
            access.record_platform_usage("sirene")
        return result

    # --- Finances (INPI/BCE, open data) ---

    @mcp.tool()
    async def fr_bilans(siren: str) -> dict:
        """List available INPI/BCE annual filings for a SIREN.

        Returns exercise dates, bilan type (C=complet, S=simplifié, K=consolidé),
        confidentiality status, and turnover. Typically 3-9 years of history.

        Args:
            siren: SIREN number (9 digits).
        """
        items = inpi.list_exercises(siren)
        return {"siren": siren, "items": items, "total": len(items)}

    @mcp.tool()
    async def fr_bilan(siren: str, date_cloture: str) -> dict:
        """Fetch one INPI/BCE annual filing with full financial ratios.

        Returns: CA, EBE, EBIT, résultat net, marge EBE, autonomie financière,
        taux d'endettement, liquidité, vétusté, BFR, rotation stocks,
        crédit clients/fournisseurs, couverture intérêts.
        Use fr_bilans first to discover available dates.

        Args:
            siren: SIREN number (9 digits).
            date_cloture: Exercise closing date (YYYY-MM-DD, e.g. "2024-12-31").
        """
        result = inpi.get_bilan(siren, date_cloture)
        if result is None:
            return {"error": "exercise_not_found", "siren": siren, "date_cloture": date_cloture}
        return result

    # --- Événements légaux (BODACC, open data) ---

    @mcp.tool()
    async def fr_events(
        siren: str,
        famille: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """List BODACC legal events for a company: creations, modifications,
        sales, collective proceedings, annual filings.

        Args:
            siren: SIREN number (9 digits).
            famille: Filter by type — creation, modification, radiation, vente,
                procedure_collective, dpc (dépôt des comptes).
            limit: Max results (default 20).
        """
        return bodacc.search_by_siren(siren, famille=famille, limit=limit)

    # --- Appels d'offres (BOAMP, open data) ---

    @mcp.tool()
    async def fr_tenders_search(
        query: Optional[str] = None,
        descripteur: Optional[str] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        type_marche: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Search French public procurement tenders (BOAMP).

        Args:
            query: Full-text search in the notice subject.
            descripteur: BOAMP descriptor (e.g. "Photovoltaïque", "Informatique").
            departement: Department code (e.g. "75").
            date_from: Publication date start (YYYY-MM-DD).
            date_to: Publication date end (YYYY-MM-DD).
            type_marche: Market type (TRAVAUX, FOURNITURES, SERVICES).
            limit: Max results (default 20, max 100).
        """
        return boamp.search(
            query=query, descripteur=descripteur, departement=departement,
            date_from=date_from, date_to=date_to, type_marche=type_marche,
            limit=limit,
        )

    @mcp.tool()
    async def fr_tenders_get(idweb: str) -> dict:
        """Fetch a single BOAMP tender by its ID.

        Args:
            idweb: BOAMP notice identifier (e.g. "26-50647").
        """
        result = boamp.get(idweb)
        if result is None:
            return {"error": "not_found", "idweb": idweb}
        return result

