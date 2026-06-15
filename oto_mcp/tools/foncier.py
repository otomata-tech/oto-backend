"""Foncier — données de site / parcelle / adresse (open data France, sans clé).

Regroupe au même endroit ce qui caractérise un **site** (par opposition à
l'identité entreprise, namespace `fr`) : géocodage, cadastre, bâti existant,
risques/ICPE, productible solaire, signaux de conso électrique, valorisation
immobilière par comparables. Tous les clients viennent de `france-opendata`
(open data, pas de clé).

ADR 0010 (namespaces cohérents) : `foncier_icpe` (Géorisques) et les `foncier_*`
DVF étaient auparavant dispersés sous `fr` / `dvf` — regroupés ici. Sit@del
(permis) n'est pas exposé : sa source est un CSV national ~276 Mo non requêtable
(pré-fetch + cache requis, hors tool MCP) — le client lib reste disponible.

Connecteur open-data : pas de credential. Exposé seulement si activé en DB
(cran d'activation, ADR 0010) — register_all gate sur `connector_activation`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from france_opendata import (
        ApiCartoClient,
        BanClient,
        BdTopoClient,
        DvfClient,
        EnedisClient,
        PvgisClient,
    )
    from france_opendata.georisques import GeorisquesClient

    ban = BanClient()
    cadastre = ApiCartoClient()
    bdtopo = BdTopoClient()
    pvgis = PvgisClient()
    enedis = EnedisClient()
    dvf = DvfClient()
    georisques = GeorisquesClient()

    # --- géocodage (BAN — Base Adresse Nationale) ----------------------------

    @mcp.tool()
    async def foncier_geocode(
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
    async def foncier_reverse(lat: float, lon: float) -> Optional[dict]:
        """Reverse-geocode a point (lat, lon) → nearest known address, or null."""
        return ban.reverse(lat, lon)

    # --- cadastre (API Carto IGN) --------------------------------------------

    @mcp.tool()
    async def foncier_parcelle(lat: float, lon: float) -> Optional[dict]:
        """Cadastral parcel at a point (lat, lon), or null.

        Returns idu (unique id), commune, INSEE code, section, numéro, area
        (contenance_m2) and GeoJSON geometry. Use to identify the land unit
        under an address (geocode first to get lat/lon).
        """
        return cadastre.parcelle_at(lat, lon)

    # --- bâti existant (IGN BDTOPO V3) ---------------------------------------

    @mcp.tool()
    async def foncier_bati(lat: float, lon: float) -> dict:
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
    async def foncier_productible_solaire(lat: float, lon: float, kwc: float) -> Optional[dict]:
        """Annual solar yield (kWh) for a PV system of `kwc` kWp at (lat, lon), via PVGIS.

        Picks optimal tilt/azimuth for a rooftop install. Returns physical data
        only (productible_kwh_an, irradiance, losses, optimal angles) — no tariff
        or business assumptions. Null if inputs invalid or PVGIS unavailable.
        """
        return pvgis.productible(lat, lon, kwc)

    # --- consommation électrique par adresse (Enedis) ------------------------

    @mcp.tool()
    async def foncier_conso_elec(
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
    async def foncier_icpe(
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

    # --- valorisation immobilière (DVF Etalab) — repris de `dvf` --------------

    @mcp.tool()
    async def foncier_prix_m2(
        code_commune: str,
        type_local: Optional[str] = None,
        years: int = 3,
    ) -> dict:
        """Real-estate price stats (€/m²) for a French commune, from DVF open data.

        Median/mean/min/max €/m² + per-year breakdown, on clean mono-bien sales
        (one Appartement or Maison per mutation; outliers <100 or >50000 €/m²
        filtered). Use to value a property by comparables.

        Args:
            code_commune: INSEE code, 5 digits (e.g. "13201" = Marseille 1er).
            type_local: "Appartement" | "Maison" (default: both).
            years: lookback in years WITH data (DVF lags ~6 months; default 3).
        """
        return dvf.stats(code_commune=code_commune, type_local=type_local, years=years)

    @mcp.tool()
    async def foncier_comparables(
        code_commune: str,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 2,
        limit: int = 50,
    ) -> dict:
        """Comparable real-estate transactions for a commune, from DVF open data.

        Individual mono-bien sales (date, valeur_fonciere, surface, €/m², adresse,
        lat/lon), most recent first. Filter by type + surface band to find true
        comparables.

        Args:
            code_commune: INSEE code, 5 digits.
            type_local: "Appartement" | "Maison" (default: both).
            surface_min / surface_max: surface bâtie band m².
            years: lookback in years with data (default 2).
            limit: max comparables, most recent first (default 50).
        """
        return dvf.comparables(
            code_commune=code_commune, type_local=type_local,
            surface_min=surface_min, surface_max=surface_max, years=years, limit=limit,
        )

    @mcp.tool()
    async def foncier_comparables_adresse(
        adresse: str,
        radius_m: int = 500,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: int = 3,
        limit: int = 50,
    ) -> dict:
        """Comparable sales around a precise address (geocode + radius filter), DVF.

        Geocodes the free-form address (BAN), then returns DVF mono-bien sales
        within `radius_m` metres, nearest first, each with `distance_m`, plus the
        local median €/m². Sharper than commune-level stats for one property.

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
            radius_m: search radius in metres (default 500).
            type_local: "Appartement" | "Maison" (default: both).
            surface_min / surface_max: surface bâtie band m².
            years: lookback in years with data (default 3).
            limit: max comparables, nearest first (default 50).
        """
        return dvf.comparables_by_address(
            adresse=adresse, radius_m=radius_m, type_local=type_local,
            surface_min=surface_min, surface_max=surface_max, years=years, limit=limit,
        )
