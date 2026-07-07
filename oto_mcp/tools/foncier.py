"""Foncier — données de site / parcelle / adresse (open data France, sans clé).

Regroupe au même endroit ce qui caractérise un **site** (par opposition à
l'identité entreprise, namespace `fr`) : géocodage, cadastre, bâti existant,
risques/ICPE, productible solaire, signaux de conso électrique, valorisation
immobilière par comparables. Tous les clients viennent de `france-opendata`
(open data, pas de clé).

ADR 0010 (namespaces cohérents) : `foncier_icpe` (Géorisques) et les `foncier_*`
DVF étaient auparavant dispersés sous `fr` / `dvf` — regroupés ici. `foncier_permis_search`
(Sit@del) interroge l'API DiDo `/rows` **en live** (filtre serveur commune/dept/année) —
le pendant requêtable du productible solaire ; l'ingestion de masse via CSV national
(276 Mo) reste réservée aux consommateurs qui croisent les sources (cf. GR), hors oto.

Connecteur open-data : pas de credential. Exposé seulement si activé en DB
(cran d'activation, ADR 0010) — register_all gate sur `connector_activation`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

# Import OPTIONNEL de prefab_ui (extra `fastmcp[apps]`) au niveau MODULE — et NON
# local à register() : les tools *_app ci-dessous annotent leur retour `-> Card`,
# et FastMCP résout les type-hints (via get_type_hints, d'autant que
# `from __future__ import annotations` les rend lazy) contre `fn.__globals__`,
# le namespace MODULE. Un import local laisse `Card` indéfini au module →
# `NameError: name 'Card' is not defined` à l'enregistrement (issue #69), ce qui
# désactivait TOUS les tools foncier *_app en prod. S'il manque (extra `apps`
# absent), on n'enregistre pas les *_app — les tools JSON restent (dégradation
# gracieuse, même principe que « si le rendu échoue, utiliser le tool JSON »).
try:
    from prefab_ui.components import (  # type: ignore
        Card, Column, DataTable, DataTableColumn, Heading, Text,
    )
    _PREFAB_UI_AVAILABLE = True
except Exception:  # pragma: no cover - extra `apps` absent
    _PREFAB_UI_AVAILABLE = False


def register(mcp: FastMCP) -> None:
    from france_opendata.sitadel import DIDO_PAGE_SIZES

    from .. import fod_foncier
    from .. import fod_urba  # georisques (ICPE) — servi par FOD depuis B3

    # Données de site servies par le service FOD dédié (ADR 0028) — le backend
    # n'exécute plus ces appels in-process. Objets proxy à surface identique aux
    # clients france_opendata (mêmes méthodes/signatures) → seuls ces bindings
    # changent, les corps des tools restent inchangés.
    ban = fod_foncier.ban
    cadastre = fod_foncier.cadastre
    bdtopo = fod_foncier.bdtopo
    pvgis = fod_foncier.pvgis
    enedis = fod_foncier.enedis
    dvf = fod_foncier.dvf
    dpe = fod_foncier.dpe
    sitadel = fod_foncier.sitadel
    # georisques (ICPE) : servi par FOD (B3), partagé avec urba — même proxy.
    georisques = fod_urba.georisques

    # --- géocodage (BAN — Base Adresse Nationale) ----------------------------

    @mcp.tool()
    def foncier_geocode(
        adresse: str,
        limit: int = 5,
        code_postal: Optional[str] = None,
        code_commune: Optional[str] = None,
    ) -> list[dict]:
        """Geocode a French address → coordinates, canonical label, INSEE code.

        Returns candidates (label, score, lat, lon, citycode, postcode), best first.
        The BAN label is a canonical address key (two spellings converge on one point).

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
            limit: max candidates (default 5).
            code_postal: restrict to a postcode.
            code_commune: restrict to an INSEE commune code.
        """
        return ban.search(adresse, limit=limit, postcode=code_postal, citycode=code_commune)

    @mcp.tool()
    def foncier_reverse(lat: float, lon: float) -> Optional[dict]:
        """Reverse-geocode a point (lat, lon) → nearest known address, or null."""
        return ban.reverse(lat, lon)

    # --- cadastre (API Carto IGN) --------------------------------------------

    @mcp.tool()
    def foncier_parcelle(lat: float, lon: float) -> Optional[dict]:
        """Cadastral parcel at a point (lat, lon), or null.

        Returns idu (unique id), commune, INSEE code, section, numéro, area
        (contenance_m2) and GeoJSON geometry. Use to identify the land unit
        under an address (geocode first to get lat/lon).
        """
        return cadastre.parcelle_at(lat, lon)

    # --- bâti existant (IGN BDTOPO V3) ---------------------------------------

    @mcp.tool()
    def foncier_bati(lat: float, lon: float) -> dict:
        """Built footprint on the parcel at (lat, lon): ground area, real CES, uses, heights.

        Resolves the cadastral parcel at the point, then sums BDTOPO buildings
        whose centroid falls inside it. `ces_reel` = built area / parcel area
        (low CES in a dense area = under-developed land signal). Returns an
        `error` key if no parcel is found at the point.
        """
        parcelle = cadastre.parcelle_at(lat, lon)
        if not parcelle or not parcelle.get("geometry"):
            return {"error": "no_parcel_at_point", "lat": lat, "lon": lon}
        return bdtopo.bati_parcelle(parcelle["geometry"], contenance_m2=parcelle.get("contenance_m2"))

    # --- productible solaire (PVGIS, JRC) ------------------------------------

    @mcp.tool()
    def foncier_productible_solaire(lat: float, lon: float, kwc: float) -> Optional[dict]:
        """Annual solar yield (kWh) for a PV system of `kwc` kWp at (lat, lon), via PVGIS.

        Picks optimal tilt/azimuth for a rooftop install. Returns physical data
        only (productible_kwh_an, irradiance, losses, optimal angles) — no tariff
        or business assumptions. Null if inputs invalid or PVGIS unavailable.
        """
        return pvgis.productible(lat, lon, kwc)

    # --- permis d'urbanisme (Sit@del / SDES, API DiDo live) ------------------

    def _snap_page_size(limit: int) -> int:
        """Cale `limit` sur une taille de page DiDo autorisée (10/20/50/100)."""
        return next((s for s in DIDO_PAGE_SIZES if s >= limit), DIDO_PAGE_SIZES[-1])

    @mcp.tool()
    def foncier_permis_search(
        code_commune: Optional[str] = None,
        dept: Optional[str] = None,
        kind: str = "logements",
        annee_min: Optional[int] = None,
        annee_max: Optional[int] = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict:
        """Building/urbanism permits (Sit@del, SDES) for a commune or department, live.

        Live query on the DiDo API (server-side filter) — no bulk download. National
        register of urban-planning authorizations (PC/PA/DP) since 2013, monthly refresh.
        Scope is REQUIRED (`code_commune` or `dept`) — a national scan is huge.

        Three files, pick with `kind`:
          - "logements": permits creating housing (developer/promoteur core).
          - "locaux": non-residential premises (offices, retail, industry, warehouses —
            the big-roof PV / commercial prospecting file; carries `destination_libelle`
            and `sp_finale_estimee_m2`).
          - "amenager": land-development permits (subdivisions, large layouts).

        Each permit is normalized: identity (num_dau, type, etat), commune/dept, deposit
        year, real dates, applicant (demandeur: SIREN/SIRET/denomination/APE — ~35 %
        empty by GDPR for natural persons, this is the diffusion rule not a data gap),
        terrain address + cadastral parcels, surfaces.

        Args:
            code_commune: INSEE commune code (e.g. "75056"). Exact match.
            dept: INSEE department code (e.g. "59", "2A"). Use for a whole department.
            kind: "logements" (default) | "locaux" | "amenager".
            annee_min / annee_max: deposit-year bounds (inclusive).
            page: 1-based page.
            limit: max permits per page (snapped to 10/20/50/100, cap 100). `total` in
                the result is the full server-side count — page through for more.
        """
        if not code_commune and not dept:
            raise ValueError("Renseigner `code_commune` ou `dept` (un scan national est proscrit).")
        page_size = _snap_page_size(max(1, limit))
        res = sitadel.search(
            kind,
            communes=code_commune or None,
            dept=dept or None,
            an_min=annee_min,
            an_max=annee_max,
            page=page,
            page_size=page_size,
        )
        res["permis"] = res["permis"][:limit]
        res["kind"] = kind
        return res

    # --- consommation électrique par adresse (Enedis) ------------------------

    @mcp.tool()
    def foncier_conso_elec(
        annee: str,
        dept: str,
        secteur: Optional[str] = None,
        min_mwh: Optional[float] = None,
        max_mwh: Optional[float] = None,
        limit: int = 200,
    ) -> dict:
        """Annual electricity consumption signals by address (Enedis open data, N-1).

        Band query → returns {total, signals[]} (address, MWh/year, NAF2, sector,
        site count). `dept` is REQUIRED (a national scan is huge). Big consumers
        are the best PV prospecting targets — filter with `min_mwh` (e.g. 150).

        Args:
            annee: reference year (e.g. "2024").
            dept: INSEE department code (e.g. "59") — required.
            secteur: "INDUSTRIE" | "TERTIAIRE" | "AGRICULTURE".
            min_mwh / max_mwh: consumption band (MWh/year).
            limit: max signals returned (default 200).
        """
        return enedis.consommation_par_adresse(
            annee, dept=dept, secteur=secteur, min_mwh=min_mwh, max_mwh=max_mwh, limit=limit
        )

    # --- risques industriels / ICPE (Géorisques) — repris de `fr` ------------

    _ICPE_KEEP = (
        "raisonSociale", "siret", "adresse1", "codePostal", "codeInsee", "commune",
        "codeNaf", "longitude", "latitude", "regime", "ied", "statutSeveso",
        "prioriteNationale", "etatActivite", "codeAIOT", "serviceAIOT",
        "industrie", "carriere", "eolienne", "bovins", "porcs", "volailles",
    )

    def _compact_icpe(d: dict) -> dict:
        out = {k: d.get(k) for k in _ICPE_KEEP}
        inspections = d.get("inspections") or []
        out["inspections"] = [
            {"date": i.get("dateInspection"),
             "url": (i.get("fichierInspection") or {}).get("urlFichier")}
            for i in inspections[-3:]
        ]
        return out

    @mcp.tool()
    def foncier_icpe(
        siret: Optional[str] = None,
        code_insee: Optional[str] = None,
        page: int = 1,
    ) -> dict:
        """ICPE registry (classified installations, Géorisques) by SIRET or commune.

        Detects HEAVY INDUSTRIAL SITES when power consumption is masked in Enedis
        open data (statistical secrecy): returns ICPE regime (Déclaration /
        Enregistrement / Autorisation), IED status, Seveso, activity state,
        geolocation, DREAL inspection service and latest inspection reports.
        Grounds a SOURCED "big consumer" presumption (cite the codeAIOT) — it does
        NOT return energy consumption.

        Args:
            siret: establishment SIRET (14 digits) — exact match.
            code_insee: INSEE commune code — all ICPE of the commune.
            page: 1-based page (20 per page).
        """
        res = georisques.installations_classees(siret=siret, code_insee=code_insee, page=page)
        return {
            "results": res.get("results", 0),
            "page": res.get("page", page),
            "total_pages": res.get("total_pages", 1),
            "data": [_compact_icpe(d) for d in res.get("data", [])],
        }

    # --- valorisation immobilière (DVF+ Cerema, depuis 2014) — repris de `dvf` -

    @mcp.tool()
    def foncier_prix_m2(
        code_commune: str,
        type_local: Optional[str] = None,
        years: int = 3,
    ) -> dict:
        """Real-estate price stats (€/m²) for a French commune, from DVF+ open data
        (Cerema, transactions since 2014).

        Median/mean/min/max €/m² + per-year breakdown, on clean mono-bien sales
        (one Appartement or Maison per mutation; outliers <100 or >50000 €/m²
        filtered). Use to value a property by comparables.

        Args:
            code_commune: INSEE code, 5 digits (e.g. "13201" = Marseille 1er).
            type_local: "Appartement" | "Maison" (default: both).
            years: lookback in years WITH data (DVF lags ~6 months; default 3,
                up to ~2014).
        """
        return dvf.stats(code_commune=code_commune, type_local=type_local, years=years)

    @mcp.tool()
    def foncier_comparables(
        code_commune: str,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 2,
        limit: int = 50,
    ) -> dict:
        """Raw DVF+ real-estate transactions for a commune (Cerema open data, since
        2014). NOT filtered: ALL property types (flats, houses, land, dependencies,
        mixed-use, commercial) and ALL natures (sale, VEFA off-plan, auction,
        exchange) — the agent decides the use (valuation, land analysis, market
        volume…). For a clean median €/m², use foncier_prix_m2 instead.

        Each row: date_mutation, nature_mutation, valeur_fonciere, type_bien (raw
        DVF+ label) + type_local (set only for residential mono-bien, else null),
        surface_reelle_bati, surface_terrain, prix_m2 (null if not computable),
        nombre_locaux, vefa, id_parcelle(s), adresse (reverse-geocoded BAN), lat/lon.
        Most recent first.

        Args:
            code_commune: INSEE code, 5 digits.
            type_local: OPTIONAL filter "Appartement" | "Maison" (default: everything).
            surface_min / surface_max: OPTIONAL surface bâtie band m².
            years: lookback in years with data (default 2, up to ~2014).
            limit: max rows, most recent first (default 50).
        """
        return dvf.comparables(
            code_commune=code_commune, type_local=type_local,
            surface_min=surface_min, surface_max=surface_max, years=years, limit=limit,
        )

    @mcp.tool()
    def foncier_comparables_adresse(
        adresse: str,
        radius_m: int = 500,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 3,
        limit: int = 50,
        with_dpe: bool = False,
    ) -> dict:
        """Raw DVF+ transactions around a precise address (Cerema open data, since
        2014). Geocodes the address (BAN), returns ALL mutations whose parcel lies
        within `radius_m` metres (distance to nearest parcel vertex — robust to
        multi-parcel goods), nearest first, each with `distance_m`. NOT filtered by
        property type/nature; `median_prix_m2` is computed on residential mono-bien
        rows only (indicative). Same fields as foncier_comparables.

        With `with_dpe=True`, each sale is enriched with ADEME energy data: a HOUSE
        gets its matched `dpe` (etiquette + `dpe_match` confidence by proximity &
        surface); a FLAT gets `dpe_immeuble` (the building's DPE list — NO 1:1 match,
        as DVF and DPE share no dwelling key).

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
            radius_m: search radius in metres (default 500).
            type_local: OPTIONAL filter "Appartement" | "Maison" (default: everything).
            surface_min / surface_max: OPTIONAL surface bâtie band m².
            years: lookback in years with data (default 3, up to ~2014).
            limit: max rows, nearest first (default 50).
            with_dpe: attach ADEME DPE energy labels per sale (default False).
        """
        res = dvf.comparables_by_address(
            adresse=adresse, radius_m=radius_m, type_local=type_local,
            surface_min=surface_min, surface_max=surface_max, years=years, limit=limit,
        )
        if with_dpe and res.get("mutations"):
            from ..dpe_match import attach_dpe_to_sales
            zone = dpe.by_address(adresse, radius_m=radius_m, limit=1000)
            attach_dpe_to_sales(res["mutations"], zone.get("dpe", []))
        return res

    @mcp.tool()
    def foncier_dpe_adresse(
        adresse: str,
        radius_m: int = 200,
        type_batiment: Optional[str] = None,
        etiquette: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        limit: int = 50,
    ) -> dict:
        """Energy performance diagnostics (DPE, ADEME open data) around an address.

        Geocodes the address (BAN), returns raw DPE records within `radius_m` metres,
        nearest first. Each: etiquette_dpe (A–G), etiquette_ges, conso_ep_kwh_m2_an,
        surface_habitable, annee_construction, type_batiment, adresse, date_dpe,
        distance_m, lat/lon. ~15M dwellings, since July 2021.

        Args:
            adresse: free-form address.
            radius_m: search radius in metres (default 200).
            type_batiment: OPTIONAL "maison" | "appartement" | "immeuble".
            etiquette: OPTIONAL DPE label filter (A..G).
            surface_min / surface_max: OPTIONAL surface habitable band m².
            limit: max records, nearest first (default 50).
        """
        return dpe.by_address(
            adresse=adresse, radius_m=radius_m, type_batiment=type_batiment,
            etiquette=etiquette, surface_min=surface_min, surface_max=surface_max, limit=limit,
        )

    @mcp.tool()
    def foncier_dpe_stats(code_commune: str, type_batiment: Optional[str] = None) -> dict:
        """DPE label distribution (A–G) for a commune, from ADEME open data.

        Aggregated view of energy performance across all dwellings of the commune.

        Args:
            code_commune: INSEE code, 5 digits.
            type_batiment: OPTIONAL "maison" | "appartement" | "immeuble".
        """
        return dpe.stats(code_commune=code_commune, type_batiment=type_batiment)

    # --- MCP Apps : variantes à interface rendue (SEP-1865) ------------------
    # Quelques tools "flagship" *_app qui renvoient une UI (carte + table) rendue
    # par le host (Claude.ai, iframe sandbox) au lieu de JSON brut — utile quand
    # l'utilisateur veut VOIR une synthèse de site / des comparables.
    #
    # Import OPTIONNEL de prefab_ui (extra `fastmcp[apps]`) : s'il manque (venv
    # editable pas réinstallé), on n'enregistre simplement PAS ces tools — les
    # tools JSON ci-dessus restent disponibles (dégradation gracieuse, même
    # principe que « si le rendu échoue, utiliser les tools JSON équivalents »).
    if not _PREFAB_UI_AVAILABLE:
        return

    # Libellés FR curés pour les clés connues ; sinon on humanise la clé brute,
    # ce qui rend les renderers robustes à la forme exacte renvoyée par les
    # clients france_opendata (pas de dépendance dure à un nom de champ).
    _LABELS = {
        "label": "Adresse", "score": "Score géocodage", "citycode": "Code INSEE",
        "postcode": "Code postal", "city": "Commune", "lat": "Latitude",
        "lon": "Longitude", "idu": "Identifiant parcelle", "commune": "Commune",
        "code_insee": "Code INSEE", "section": "Section", "numero": "Numéro",
        "contenance_m2": "Contenance (m²)", "surface_bati_m2": "Surface bâtie (m²)",
        "surface_sol_m2": "Emprise au sol (m²)", "ces_reel": "CES réel",
        "nb_batiments": "Bâtiments", "hauteur_max_m": "Hauteur max (m)",
        "usages": "Usages", "valeur_fonciere": "Prix (€)", "surface": "Surface (m²)",
        "surface_reelle_bati": "Surface bâtie (m²)", "prix_m2": "€/m²",
        "eur_m2": "€/m²", "date_mutation": "Date", "date": "Date",
        "adresse": "Adresse", "type_local": "Type", "distance_m": "Distance (m)",
        "annee": "Année", "year": "Année", "median": "Médiane €/m²",
        "mediane": "Médiane €/m²", "moyenne": "Moyenne €/m²", "mean": "Moyenne €/m²",
        "min": "Min €/m²", "max": "Max €/m²", "count": "Ventes", "nb": "Ventes",
    }

    def _label(k: str) -> str:
        return _LABELS.get(k) or str(k).replace("_", " ").capitalize()

    def _fmt(v: object) -> str:
        if isinstance(v, bool):
            return "oui" if v else "non"
        if isinstance(v, float):
            return f"{v:,.0f}".replace(",", " ") if abs(v) >= 100 else f"{v:.2f}"
        return str(v)

    def _is_scalar(v: object) -> bool:
        return isinstance(v, (str, int, float, bool)) or v is None

    def _scalars(d: Optional[dict]) -> dict:
        return {k: v for k, v in (d or {}).items() if _is_scalar(v)}

    def _first_record_list(d: Optional[dict]) -> Optional[list]:
        """First value of `d` that is a non-empty list of dicts (the table rows)."""
        for v in (d or {}).values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return None

    def _facts(d: dict) -> None:
        """Render scalar key/values as Text rows (call inside an active Column)."""
        for k, v in d.items():
            if v is None or v == "":
                continue
            Text(f"{_label(k)} : {_fmt(v)}")

    def _table(records: list) -> None:
        """Render a list of dicts as a searchable DataTable (scalar cells only)."""
        rows, keys = [], []
        for r in records:
            row = {}
            for k, v in r.items():
                if _is_scalar(v):
                    row[k] = v
                    if k not in keys:
                        keys.append(k)
            rows.append(row)
        cols = [DataTableColumn(key=k, header=_label(k), sortable=True) for k in keys]
        DataTable(columns=cols, rows=rows, search=True)

    def _message_card(title: str, message: str) -> "Card":
        with Card() as card:
            with Column(gap=4):
                Heading(title)
                Text(message)
        return card

    @mcp.tool(app=True)
    def foncier_site_app(adresse: str) -> Card:
        """Rendered SITE sheet for a French address (MCP App / interactive card).

        Visual flagship variant of foncier_geocode + foncier_parcelle + foncier_bati:
        geocodes the address (BAN), resolves the cadastral parcel and built footprint,
        and renders ONE card — canonical address, parcel id/section/number, area
        (contenance), real CES, buildings. Use when the user wants to *see* a parcel/
        site summary. For raw JSON, use the individual foncier_* tools.

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
        """
        hits = ban.search(adresse, limit=1)
        if not hits:
            return _message_card("Adresse introuvable", f"Aucun résultat BAN pour « {adresse} ».")
        top = hits[0]
        lat, lon = top.get("lat"), top.get("lon")
        parcelle = cadastre.parcelle_at(lat, lon) if lat is not None and lon is not None else None
        bati = None
        if parcelle and parcelle.get("geometry"):
            try:
                bati = bdtopo.bati_parcelle(parcelle["geometry"], contenance_m2=parcelle.get("contenance_m2"))
            except Exception:
                bati = None
        with Card() as card:
            with Column(gap=4):
                Heading(str(top.get("label") or adresse))
                _facts(_scalars(top))
                if parcelle:
                    Heading("Parcelle cadastrale")
                    _facts(_scalars(parcelle))
                else:
                    Text("Pas de parcelle cadastrale au point géocodé.")
                if bati and not bati.get("error"):
                    Heading("Bâti existant")
                    _facts(_scalars(bati))
        return card

    @mcp.tool(app=True)
    def foncier_comparables_app(
        adresse: str,
        radius_m: int = 500,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 3,
        limit: int = 50,
    ) -> Card:
        """Rendered transactions around an address (MCP App / interactive table), DVF+.

        Visual flagship variant of foncier_comparables_adresse: geocodes the address,
        then renders the local median €/m² plus a sortable/searchable table of nearby
        DVF+ mutations (date, address, type, surface, price, €/m², distance — all
        property types). Use when the user wants to *see* nearby sales. For raw JSON
        use foncier_comparables_adresse.

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
            radius_m: search radius in metres (default 500).
            type_local: "Appartement" | "Maison" (default: both).
            surface_min / surface_max: surface bâtie band m².
            years: lookback in years with data (default 3).
            limit: max comparables, nearest first (default 50).
        """
        res = dvf.comparables_by_address(
            adresse=adresse, radius_m=radius_m, type_local=type_local,
            surface_min=surface_min, surface_max=surface_max, years=years, limit=limit,
        ) or {}
        records = _first_record_list(res) or []
        with Card() as card:
            with Column(gap=4):
                Heading(f"Comparables — {adresse}")
                _facts(_scalars(res))  # headline stats (médiane locale, etc.)
                if records:
                    _table(records)
                else:
                    Text("Aucune vente comparable trouvée dans le rayon demandé.")
        return card

    @mcp.tool(app=True)
    def foncier_prix_m2_app(
        code_commune: str,
        type_local: Optional[str] = None,
        years: int = 3,
    ) -> Card:
        """Rendered PRICE STATS (€/m²) for a commune (MCP App / interactive card), DVF.

        Visual flagship variant of foncier_prix_m2: renders the headline €/m² figures
        (median/mean/min/max) and a per-year breakdown table. Use when the user wants
        to *see* a commune's price levels. For raw JSON use foncier_prix_m2.

        Args:
            code_commune: INSEE code, 5 digits (e.g. "13201" = Marseille 1er).
            type_local: "Appartement" | "Maison" (default: both).
            years: lookback in years WITH data (DVF lags ~6 months; default 3).
        """
        res = dvf.stats(code_commune=code_commune, type_local=type_local, years=years) or {}
        per_year = _first_record_list(res)
        with Card() as card:
            with Column(gap=4):
                Heading(f"Prix au m² — {code_commune}")
                _facts(_scalars(res))
                if per_year:
                    Heading("Par année")
                    _table(per_year)
        return card
