"""Urbanisme — ce qui qualifie / grève un lieu (open data France, sans clé).

Pendant du namespace `foncier` (qui décrit le **site physique** : géocodage,
cadastre, bâti, solaire, conso). `urba` couvre l'**enveloppe réglementaire et
territoriale** d'un point ou d'une commune :
- zonage PLU/PLUi opposable (Géoportail de l'Urbanisme),
- risques naturels/technologiques recensés + aléa retrait-gonflement des argiles,
- Quartiers Prioritaires de la Ville (zonage fiscal),
- secteurs d'intervention EPFIF (maîtrise foncière, Île-de-France),
- socio-démographie communale (INSEE Mélodi).

Tous les clients viennent de `france-opendata` (open data, pas de clé). Géocoder
l'adresse au préalable via `foncier_geocode` (→ lat/lon + code INSEE).

Connecteur open-data : pas de credential. Exposé seulement si activé en DB
(cran d'activation, ADR 0010) — register_all gate sur `connector_activation`.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    from france_opendata import EpfifClient, GpuClient, InseeMelodiClient, QpvClient
    from france_opendata.georisques import GeorisquesClient

    gpu = GpuClient()
    georisques = GeorisquesClient()
    qpv = QpvClient()
    insee = InseeMelodiClient()
    epfif = EpfifClient()  # instance unique → cache TTL partagé sur la durée du process

    # --- zonage PLU/PLUi (Géoportail de l'Urbanisme) -------------------------

    @mcp.tool()
    async def urba_zonage(lat: float, lon: float) -> dict:
        """Opposable urban-planning zoning at a point (lat, lon), via the GPU.

        Returns the primary PLU/PLUi zone (libellé, type, dominant destination,
        direct règlement PDF URL when available), superimposed zones, prescriptions,
        information layers, public-utility easements, and covering documents. `zone`
        is null if no digitized document covers the point (commune under RNU, or PLU
        not published on the GPU — see `avertissements`). Geocode the address first.
        """
        return gpu.zonage(lon, lat)

    # --- risques (Géorisques) ------------------------------------------------

    @mcp.tool()
    async def urba_risques(code_insee: str) -> dict:
        """Natural & technological risks recorded for a commune (Géorisques GASPAR).

        Returns distinct long labels: flooding, ground movement, clay shrink-swell,
        seismicity, hazardous-materials transport, ICPE/Seveso… Empty list if none
        recorded. Takes the INSEE commune code (5 chars).
        """
        return georisques.risques_commune(code_insee)

    @mcp.tool()
    async def urba_argiles(lat: float, lon: float) -> dict:
        """Clay shrink-swell hazard (RGA) at a point (lat, lon), via Géorisques.

        Returns exposure level (faible / moyen / fort). High clay exposure is a
        foundation-cost driver. Geocode the address first.
        """
        return georisques.alea_argiles(lon, lat)

    # --- Quartiers Prioritaires de la Ville (QPV) ----------------------------

    @mcp.tool()
    async def urba_qpv(code_insee: str) -> dict:
        """Priority urban districts (QPV) of a commune (national dataset).

        Returns the QPV list and count. Presence of a QPV is the geographic
        condition for several incentives (e.g. reduced-VAT home ownership), not a
        full eligibility check. Takes the INSEE commune code.
        """
        return qpv.by_commune(code_insee)

    @mcp.tool()
    async def urba_qpv_proximite(lat: float, lon: float, rayon_m: int = 300) -> dict:
        """QPV within `rayon_m` metres of a point (lat, lon) — server-side geo filter.

        `eligible_geo`=True if at least one QPV falls within the radius (300 m is the
        default regulatory perimeter). Geocode the address first.
        """
        return qpv.near_point(lon, lat, radius_m=rayon_m)

    # --- EPFIF (maîtrise foncière, Île-de-France) ----------------------------

    @mcp.tool()
    async def urba_epfif(code_insee: str) -> dict:
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
    async def urba_socio(code_insee: str) -> dict:
        """Commune socio-demographic profile (INSEE Mélodi, open data).

        Aggregates, best-effort (a failing block is reported per section, not fatal):
        population (2011/2016/2022), households by family type, one-person households,
        income (median standard of living, poverty rate) and housing (main/vacant/
        secondary dwellings, tenure split). Takes the INSEE commune code.
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
