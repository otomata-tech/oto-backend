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
        res = entreprises.search(
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
        # Même compactage que fr_get : le payload brut (sièges 30+ champs,
        # matching_etablissements géo intégrale) explose vite (vu 48k chars).
        # Les établissements compactés restent là — test de co-localisation.
        res["results"] = [_compact_identity(r) for r in res.get("results", [])]
        return res

    # 7 ratios top B2B + métadonnées d'exercice. Le reste (marge_brute, ebit,
    # capacite_de_remboursement, couverture_des_interets, caf_sur_ca,
    # ratio_de_vetuste) reste accessible via fr_bilan(siren, date).
    _LATEST_BILAN_KEYS = (
        "date_cloture_exercice", "type_bilan",
        "chiffre_d_affaires", "resultat_net", "ebe",
        "marge_ebe", "autonomie_financiere", "taux_d_endettement",
        "ratio_de_liquidite",
    )

    # fr_get compact : le payload brut recherche-entreprises pèse jusqu'à 40k chars
    # (matching_etablissements intégraux avec géo, compléments, sièges 30+ champs).
    # On garde tout ce qu'un agent de prospection consomme — identité, NAF,
    # effectifs, dirigeants, finances, et la LISTE des établissements (compactée :
    # nécessaire au test de co-localisation commune INSEE / établissement actif).
    _ETAB_KEEP = (
        "siret", "adresse", "code_postal", "commune", "libelle_commune",
        "etat_administratif", "est_siege", "activite_principale",
        "liste_enseignes", "nom_commercial", "date_creation",
    )
    _DIRIGEANT_KEEP = (
        "nom", "prenoms", "denomination", "siren", "qualite",
        "annee_de_naissance", "type_dirigeant",
    )
    _IDENTITY_KEEP = (
        "siren", "nom_complet", "nom_raison_sociale", "sigle",
        "etat_administratif", "nature_juridique", "activite_principale",
        "section_activite_principale", "tranche_effectif_salarie",
        "annee_tranche_effectif_salarie", "categorie_entreprise",
        "date_creation", "date_fermeture", "site_internet",
        "nombre_etablissements", "nombre_etablissements_ouverts", "finances",
    )
    _EVENT_KEEP = (
        "id", "dateparution", "familleavis", "familleavis_lib", "typeavis",
        "typeavis_lib", "tribunal", "commercant", "jugement", "registre",
    )

    def _pick(d: dict, keys: tuple) -> dict:
        return {k: d[k] for k in keys if k in d and d[k] is not None}

    def _compact_identity(identity: dict) -> dict:
        out = _pick(identity, _IDENTITY_KEEP)
        siege = identity.get("siege")
        if isinstance(siege, dict):
            out["siege"] = _pick(siege, _ETAB_KEEP)
        dirigeants = identity.get("dirigeants") or []
        out["dirigeants"] = [_pick(d, _DIRIGEANT_KEEP) for d in dirigeants[:10]]
        etabs = identity.get("matching_etablissements") or []
        out["etablissements"] = [_pick(e, _ETAB_KEEP) for e in etabs[:25]]
        if len(etabs) > 25:
            out["_etablissements_truncated"] = len(etabs)
        return out

    @mcp.tool()
    async def fr_get(siren: str) -> dict:
        """Full company profile by SIREN: identity (siège, directors, NAF,
        employees) + 7 top financial ratios from the latest INPI/BCE filing
        + recent BODACC legal events. Aggregates 3 open data sources in parallel.
        Use this as first call when investigating a company.

        `latest_bilan` is trimmed to 7 B2B-relevant ratios (CA, résultat net,
        EBE, marge EBE, autonomie financière, taux d'endettement, liquidité).
        For the full ratio set, call `fr_bilan(siren, date_cloture)`.

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
            full = inpi.get_bilan(siren, exercises[0]["date_cloture_exercice"])
            if full:
                latest_bilan = {k: full.get(k) for k in _LATEST_BILAN_KEYS}

        events_data = f_events.result()

        return {
            "siren": siren,
            "identity": _compact_identity(identity),
            "latest_bilan": latest_bilan,
            "recent_events": [
                _pick(e, _EVENT_KEEP) for e in events_data.get("results", [])
            ],
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
        key, is_platform = access.resolve_api_key("sirene")
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

