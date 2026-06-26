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
    from .. import db  # BOAMP : index PG local (france-opendata#3), pas d'API live

    entreprises = EntreprisesClient()
    inpi = InpiClient()
    bodacc = BodaccClient()

    # --- Identité (API Recherche Entreprises, open data) ---

    @mcp.tool()
    def fr_search(
        query: Optional[str] = None,
        naf: Optional[str] = None,
        departement: Optional[str] = None,
        code_postal: Optional[str] = None,
        commune: Optional[str] = None,
        employees: Optional[str] = None,
        categorie_entreprise: Optional[str] = None,
        ca_min: Optional[int] = None,
        ca_max: Optional[int] = None,
        idcc: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Search French companies — returns identity, HQ, NAF, employees,
        directors, finances, matched establishments. At least one filter required.

        ⚠️ Geographic filters (departement, code_postal, commune) match ANY
        establishment, NOT only the head office (siège). To target companies whose
        SIÈGE is in a département, use `fr_stock_search(departement=…,
        sieges_only=True)`.

        Args:
            query: Full-text search (company name, SIREN, brand…).
            naf: NAF activity codes, comma-separated (e.g. "62.01Z,62.02A").
            departement: Department code (e.g. "75").
            code_postal: Postal code (e.g. "75001").
            commune: INSEE commune code (COG, 5 digits — e.g. "67482" for
                Strasbourg). NOT a city name (a name raises "valeur non valide").
                For a place, pass `code_postal`, or use `fr_stock_search` (which
                resolves enseigne/commune by code too).
            employees: Employee-range codes (INSEE TEFEN) of the unité légale, comma-separated.
            categorie_entreprise: INSEE size category — "PME", "ETI" or "GE".
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
            categorie_entreprise=categorie_entreprise,
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
    def fr_get(siren: str) -> dict:
        """Full company profile by SIREN: identity (siège, directors, NAF,
        employees) + 7 top financial ratios from the latest INPI/BCE filing
        + recent BODACC legal events. Aggregates 3 open data sources in parallel.
        Use this as first call when investigating a company.

        `latest_bilan` is trimmed to 7 B2B-relevant ratios (CA, résultat net,
        EBE, marge EBE, autonomie financière, taux d'endettement, liquidité).
        For the full ratio set, call `fr_bilan(siren, date_cloture)`.

        Resilient to per-source failures: a timeout or error on INPI (bilan) or
        BODACC (events) degrades gracefully — the available blocks are returned
        and the failing sources are listed under `partial_errors`. Only an
        identity failure (the keystone source) fails the whole call.

        Args:
            siren: SIREN number (9 digits).
        """
        from concurrent.futures import ThreadPoolExecutor

        partial_errors: dict[str, str] = {}

        def _safe(label, fn, *fn_args):
            try:
                return fn(*fn_args)
            except Exception as exc:  # dégradation gracieuse par sous-source
                partial_errors[label] = f"{type(exc).__name__}: {exc}"
                return None

        with ThreadPoolExecutor(max_workers=3) as pool:
            f_identity = pool.submit(_safe, "identity", entreprises.get_by_siren, siren)
            f_bilans = pool.submit(_safe, "latest_bilan", inpi.list_exercises, siren)
            f_events = pool.submit(_safe, "recent_events", bodacc.search_by_siren, siren, None, 10)

        identity = f_identity.result()
        if not identity:
            # L'identité est la pièce maîtresse : sans elle, pas de fiche.
            if "identity" in partial_errors:
                return {"error": "identity_unavailable", "siren": siren,
                        "partial_errors": partial_errors}
            return {"error": "not_found", "siren": siren}

        exercises = f_bilans.result()
        latest_bilan = None
        finances_note = None
        latest_confidentiality = None
        if exercises:  # liste non vide = au moins un dépôt exploitable (BdF)
            latest_ex = exercises[0]
            latest_confidentiality = latest_ex.get("confidentiality")
            full = _safe("latest_bilan", inpi.get_bilan, siren,
                         latest_ex["date_cloture_exercice"])
            if full:
                latest_bilan = {k: full.get(k) for k in _LATEST_BILAN_KEYS}
            if latest_confidentiality and latest_confidentiality != "Public":
                finances_note = (
                    f"comptes « {latest_confidentiality.lower()} » (art. L.232-25) — "
                    "certains ratios sont absents par déclaration de confidentialité"
                )
        elif exercises == []:  # succès mais 0 dépôt exploitable au dataset BdF
            finances_note = (
                "aucun compte exploitable au dataset Banque de France : jamais déposé "
                "OU déposé en confidentialité totale (les micro/petites entreprises "
                "peuvent rendre leurs comptes confidentiels). Vérifier l'existence d'un "
                "dépôt confidentiel via les actes RNE sur data.inpi.fr."
            )

        events_data = f_events.result() or {}

        out = {
            "siren": siren,
            "identity": _compact_identity(identity),
            "latest_bilan": latest_bilan,
            "latest_bilan_confidentiality": latest_confidentiality,
            "recent_events": [
                _pick(e, _EVENT_KEEP) for e in events_data.get("results", [])
            ],
            "events_total": events_data.get("total_count", 0),
        }
        if finances_note:
            out["finances_note"] = finances_note
        if partial_errors:
            out["partial_errors"] = partial_errors
        return out

    @mcp.tool()
    def fr_directors(siren: str) -> list[dict]:
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
    def fr_siret(siret: str) -> dict:
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
    def fr_headquarters(siren: str) -> Optional[dict]:
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
    def fr_bilans(siren: str) -> dict:
        """List available INPI/BCE annual filings for a SIREN.

        Returns exercise dates, bilan type (C=complet, S=simplifié, K=consolidé),
        confidentiality status, and turnover. Typically 3-9 years of history.

        Args:
            siren: SIREN number (9 digits).
        """
        items = inpi.list_exercises(siren)
        return {"siren": siren, "items": items, "total": len(items)}

    @mcp.tool()
    def fr_bilan(siren: str, date_cloture: str) -> dict:
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
    def fr_events(
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
    def fr_tenders_search(
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
        return db.search_boamp(
            query=query, descripteur=descripteur, departement=departement,
            date_from=date_from, date_to=date_to, type_marche=type_marche,
            limit=limit,
        )

    @mcp.tool()
    def fr_tenders_get(idweb: str) -> dict:
        """Fetch a single BOAMP tender by its ID.

        Args:
            idweb: BOAMP notice identifier (e.g. "26-50647").
        """
        result = db.get_boamp(idweb)
        if result is None:
            return {"error": "not_found", "idweb": idweb}
        return result

    # --- Accords d'entreprise (ACCO, open data) ---
    # Base nationale des accords collectifs (DILA), accords conclus depuis le
    # 01/09/2017. Métadonnées : qui (SIRET, raison sociale, IDCC = convention
    # collective), quoi (thèmes codés), quand (date_texte), nature (ACCORD initial
    # vs AVENANT = renégociation). Le texte intégral n'est pas toujours publié
    # (conforme_version_integrale), mais le « qui a négocié quoi et quand » l'est.

    @mcp.tool()
    def fr_accords_search(
        query: Optional[str] = None,
        themes: Optional[list[str]] = None,
        nature: Optional[str] = None,
        siren: Optional[str] = None,
        siret: Optional[str] = None,
        idcc: Optional[str] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        latest_per_siret: bool = False,
        sort_by: str = "date",
        sort_dir: str = "desc",
        limit: int = 20,
    ) -> dict:
        """Search French company collective agreements (accords d'entreprise, ACCO).

        Neutral primitive returning raw rows — compose your own need via filters,
        sort and per-company reduction. Common recipes:
        - Who just renegotiated their health/pension scheme:
          themes=["111","112"], nature="AVENANT", sort_dir="desc".
        - Companies whose health scheme is STALE (dormant contract, no recent act):
          themes=["111","112"], latest_per_siret=True, sort_dir="asc",
          date_to=<today-12months>.
        - Does THIS company have any health/pension agreement (and when):
          siren="123456789", themes=["111","112"].

        Args:
            query: Substring in the agreement title (ILIKE).
            themes: Theme codes (OR). Health/pension: "111" (complémentaire santé),
                "112" (prévoyance), "113" (retraite supplémentaire). Use
                fr_accords_themes to discover codes.
            nature: ACCORD (initial) | AVENANT (amendment = renegotiation) | …
            siren: Company SIREN (9 digits) — matches ALL its establishments.
                PREFER this over siret to check a company: ACCO files an agreement
                under the DEPOSITING establishment's SIRET, often not the siège, so a
                siège-SIRET lookup misses agreements.
            siret: Exact establishment SIRET (14 digits).
            idcc: Exact branch code (convention collective).
            departement: Postal code prefix (2 digits).
            date_from / date_to: Bounds on the signature date (YYYY-MM-DD).
            latest_per_siret: Keep only one row per company — its most recent act —
                BEFORE applying date_from/date_to (so date bounds then filter the
                company's LAST act → dormant-contract detection).
            sort_by: date | date_depot | date_diffusion | date_maj (default date).
            sort_dir: asc (oldest first) | desc (newest first).
            limit: Max results (default 20, max 100).
        """
        return db.search_acco(
            query=query, themes=themes, nature=nature, siren=siren, siret=siret,
            idcc=idcc, departement=departement, date_from=date_from, date_to=date_to,
            latest_per_siret=latest_per_siret, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit,
        )

    @mcp.tool()
    def fr_accords_get(id_or_numero: str) -> dict:
        """Fetch a single company agreement by its DILA id (ACCOTEXT…) or numero (T…).

        Args:
            id_or_numero: DILA identifier (ACCOTEXT000…) or deposit number (T…).
        """
        result = db.get_acco(id_or_numero)
        if result is None:
            return {"error": "not_found", "id_or_numero": id_or_numero}
        return result

    @mcp.tool()
    def fr_accords_themes() -> list[dict]:
        """List the agreement theme codes present in the database (code → label →
        count). Discovery helper so you can pick `themes` for fr_accords_search."""
        return db.acco_themes()

