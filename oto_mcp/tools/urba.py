"""Urbanisme — ce qui qualifie / grève un lieu (open data France, sans clé).

Pendant du namespace `foncier` (qui décrit le **site physique** : géocodage,
cadastre, bâti, solaire, conso). `urba` couvre l'**enveloppe réglementaire et
territoriale** d'un point ou d'une commune :
- zonage PLU/PLUi opposable (Géoportail de l'Urbanisme),
- risques naturels/technologiques recensés + aléa retrait-gonflement des argiles,
- Quartiers Prioritaires de la Ville (zonage fiscal),
- secteurs d'intervention EPFIF (maîtrise foncière, Île-de-France),
- socio-démographie communale (INSEE Mélodi) et à l'IRIS/quartier (parquet INSEE bundlé).

Tous les clients viennent de `france-opendata` (open data, pas de clé). Géocoder
l'adresse au préalable via `foncier_geocode` (→ lat/lon + code INSEE).

Connecteur open-data : pas de credential. Exposé seulement si activé en DB
(cran d'activation, ADR 0010) — register_all gate sur `connector_activation`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from .. import fod_urba

    # Enveloppe réglementaire servie par le service FOD dédié (ADR 0028 B3) — le
    # backend n'exécute plus ces appels (dont l'IRIS DuckDB) in-process. Objets proxy
    # à surface identique aux clients france_opendata → seuls ces bindings changent.
    gpu = fod_urba.gpu
    georisques = fod_urba.georisques
    qpv = fod_urba.qpv
    insee = fod_urba.insee
    iris = fod_urba.iris
    epfif = fod_urba.epfif

    # --- zonage PLU/PLUi (Géoportail de l'Urbanisme) -------------------------

    @mcp.tool()
    def urba_zonage(lat: float, lon: float) -> dict:
        """Opposable urban-planning zoning at a point (lat, lon), via the GPU.

        Returns the primary PLU/PLUi zone (libellé, type, dominant destination,
        direct règlement PDF URL when available), superimposed zones, prescriptions,
        information layers, public-utility easements, and covering documents. `zone`
        is null if no digitized document covers the point (commune under RNU, or PLU
        not published on the GPU — see `avertissements`). Geocode the address first.
        """
        return gpu.zonage(lon, lat)

    @mcp.tool()
    def urba_reglement(idurba: str, zone: Optional[str] = None, query: Optional[str] = None,
                       max_extraits: int = 8, context_lignes: int = 30) -> dict:
        """Targeted excerpts of a PLU/PLUi written règlement, via the shared FOD service.

        Intercommunal règlements are huge (often >50 MB, >1000 pages, many zones):
        the FOD service parses and caches each document **once** (keyed by `idurba`)
        and this tool serves the relevant **excerpts** for a zone / keyword — never the
        whole text. READ the excerpts to lift the rules (max height, ground coverage,
        setback, parking) — never invent a figure that is absent.

        `cached=false` means the règlement is not yet ingested in the service (batch
        ingestion, not on-the-fly): report that rather than guessing.

        Args:
            idurba: document version id — the `zone.idurba` field from `urba_zonage`.
            zone: zone label to search for (e.g. "UCt2", "UM").
            query: alternative keyword (e.g. "hauteur maximale", "emprise au sol").
            max_extraits: max passages returned (1-50). context_lignes: lines kept after each hit.
        """
        from oto_mcp import fod_reglement

        return fod_reglement.extraits(idurba, zone=zone, query=query,
                                      max_extraits=max_extraits, context_lignes=context_lignes)

    # --- risques (Géorisques) ------------------------------------------------

    @mcp.tool()
    def urba_risques(code_insee: str) -> dict:
        """Natural & technological risks recorded for a commune (Géorisques GASPAR).

        Returns distinct long labels: flooding, ground movement, clay shrink-swell,
        seismicity, hazardous-materials transport, ICPE/Seveso… Empty list if none
        recorded. Takes the INSEE commune code (5 chars).
        """
        return georisques.risques_commune(code_insee)

    @mcp.tool()
    def urba_argiles(lat: float, lon: float) -> dict:
        """Clay shrink-swell hazard (RGA) at a point (lat, lon), via Géorisques.

        Returns exposure level (faible / moyen / fort). High clay exposure is a
        foundation-cost driver. Geocode the address first.
        """
        return georisques.alea_argiles(lon, lat)

    # --- Quartiers Prioritaires de la Ville (QPV) ----------------------------

    @mcp.tool()
    def urba_qpv(code_insee: str) -> dict:
        """Priority urban districts (QPV) of a commune (national dataset).

        Returns the QPV list and count. Presence of a QPV is the geographic
        condition for several incentives (e.g. reduced-VAT home ownership), not a
        full eligibility check. Takes the INSEE commune code.
        """
        return qpv.by_commune(code_insee)

    @mcp.tool()
    def urba_qpv_proximite(lat: float, lon: float, rayon_m: int = 300) -> dict:
        """QPV within `rayon_m` metres of a point (lat, lon) — server-side geo filter.

        `eligible_geo`=True if at least one QPV falls within the radius (300 m is the
        default regulatory perimeter). Geocode the address first.
        """
        return qpv.near_point(lon, lat, radius_m=rayon_m)

    # --- EPFIF (maîtrise foncière, Île-de-France) ----------------------------

    @mcp.tool()
    def urba_epfif(code_insee: str) -> dict:
        """EPFIF land-control intervention status of a commune (Île-de-France only).

        Returns whether the commune is under an EPFIF sector (veille / maîtrise /
        ORCOD-IN) — a land-pressure signal the GPU zoning does not carry (pre-emption
        often delegated to the EPFIF). `secteur_epfif` is False outside any known
        sector, null if the source is unavailable. Data scraped live from the EPFIF
        cartography page and cached. Takes the INSEE commune code.
        """
        return epfif.lookup(code_insee)

    # --- socio-démographie communale (INSEE Mélodi) --------------------------

    @mcp.tool()
    def urba_socio(code_insee: str) -> dict:
        """Commune socio-demographic profile (INSEE Mélodi, open data).

        Aggregates, best-effort (a failing block is reported per section, not fatal):
        population (last 3 census millésimes → trend), households by family type,
        one-person households, income (median standard of living, poverty rate) and
        housing (main/vacant/secondary dwellings, tenure split). Takes the INSEE
        commune code — for Paris/Lyon/Marseille an ARRONDISSEMENT code (e.g. 13201 =
        Marseille 1er) works too. For a finer, within-commune breakdown use urba_iris.
        """
        out: dict = {"code_insee": code_insee}
        blocks = {
            "population": lambda: insee.population(code_insee),
            "familles": lambda: insee.familles(code_insee),
            "personnes_seules": lambda: insee.personnes_seules(code_insee),
            "revenus": lambda: insee.revenus(code_insee),
            "logement": lambda: insee.logement(code_insee),
        }
        for key, fn in blocks.items():
            try:
                out[key] = fn()
            except Exception as e:  # noqa: BLE001 — dégrader par bloc
                out[key] = {"error": f"{type(e).__name__}: {e}"}
        return out

    # --- recensement à l'IRIS / quartier (INSEE, parquet bundlé) --------------

    @mcp.tool()
    def urba_iris(code: str) -> dict:
        """INSEE census at the IRIS ('quartier') level — finer than a commune.

        The IRIS (~2 000 inhabitants) is INSEE's neighbourhood mesh: communes of
        ≥10 000 inhabitants (and most of 5 000-10 000) are split into IRIS. `urba_socio`
        stops at the commune; this drills inside it.

        `code` accepts either:
        - a 5-digit COMMUNE / arrondissement code → returns ALL IRIS of that commune
          plus commune totals (e.g. '13201' = Marseille 1er, '75112' = Paris 12e) ;
        - a 9-digit IRIS code → returns that single IRIS.

        Per IRIS (RP 2021 counts): population (+ age bands 0-19/20-64/65+), dwellings,
        main/secondary/vacant residences, houses vs flats, and households living in a
        flat (rp_en_appartement — a proxy for laundromat/shared-service demand).
        `typ_iris`: H habitat / A activité / D divers / Z whole undivided commune.
        Neighbourhood NAMES aren't in this file (lab_iris is INSEE's numeric label).
        For population TREND per quartier, note this millésime is single-year; use
        urba_socio at the arrondissement level for evolution.
        """
        code = str(code).strip()
        if len(code) >= 9:
            row = iris.by_iris(code)
            return row or {"code": code, "found": False,
                           "note": "code IRIS (9 chiffres) inconnu"}
        return iris.by_commune(code)
