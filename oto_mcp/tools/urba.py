"""Urban planning — what qualifies / encumbers a place (France open data, no key).

Counterpart of the `foncier` namespace (which describes the **physical site**: geocoding,
cadastre, buildings, solar, consumption). `urba` covers the **regulatory and
territorial envelope** of a point or a commune:
- enforceable PLU/PLUi zoning (Géoportail de l'Urbanisme),
- recorded natural/technological risks + clay shrink-swell hazard,
- Quartiers Prioritaires de la Ville (tax zoning),
- EPFIF intervention sectors (land control, Île-de-France),
- commune-level socio-demographics (INSEE Mélodi) and at IRIS/neighbourhood level (bundled INSEE parquet).

All clients come from `france-opendata` (open data, no key), except `urba_lieu`,
which wraps oto-core's `UrbanismeClient` (IGN API Carto GPU + cadastre + geocoder,
called directly): what applies to a PARCEL or an address, grouped, without geometries.
For the point tools, geocode the address beforehand via `foncier_geocode` (→ lat/lon +
INSEE code).

Open-data connector: no credential. Exposed only if enabled in DB
(activation gate, ADR 0010) — register_all gates on `connector_activation`.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import output_projection
from ..mcp_errors import McpError


def register(mcp: FastMCP) -> None:
    from ..fod import urba as fod_urba

    # Regulatory envelope served by the dedicated FOD service (ADR 0028 B3) — the
    # backend no longer runs these calls (including the IRIS DuckDB) in-process. Proxy objects
    # with the same surface as the france_opendata clients → only these bindings change.
    gpu = fod_urba.gpu
    georisques = fod_urba.georisques
    qpv = fod_urba.qpv
    insee = fod_urba.insee
    iris = fod_urba.iris
    elus = fod_urba.elus
    annuaire = fod_urba.annuaire
    epfif = fod_urba.epfif

    # --- PLU/PLUi zoning (Géoportail de l'Urbanisme) -------------------------

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
                       max_extraits: int = 8, context_lignes: int = 30,
                       ingest: bool = False) -> dict:
        """Targeted excerpts of a PLU/PLUi written règlement, via the shared FOD service.

        Intercommunal règlements are huge (often >50 MB, >1000 pages, many zones):
        the FOD service parses and caches each document **once** (keyed by `idurba`)
        and this tool serves the relevant **excerpts** for a zone / keyword — never the
        whole text. READ the excerpts to lift the rules (max height, ground coverage,
        setback, parking) — never invent a figure that is absent.

        A règlement not yet in the cache answers `cached=false` with its `statut`:
        - `absent`: call again with `ingest=true` — the service downloads and parses it
          in the background (20 s to a few minutes);
        - `en_cours`: ingestion running — call again with the same arguments after
          `retry_after_seconds` (about 20-30 s); the excerpts come back once ready.
        Never answer from memory while it is not ready. A scanned règlement (image
        PDF) comes back `scanne_probable=true`: say it is unreadable here, give the
        source PDF, do not invent rules.

        Named refusals (`data.code`):
        - `reglement_service_occupe` (retryable): another ingestion is running on the
          service (one at a time) — retry after `retry_after_seconds`;
        - `reglement_trop_volumineux`: a piece exceeds the on-demand size limit
          (80 MB) — only the service's batch processing can ingest it; say so;
        - `reglement_ingestion_echec`: the ingestion failed — say so and cite
          `erreur`; a new attempt is possible after `relance_possible_apres`;
        - `reglement_introuvable`: the GPU no longer publishes this document or its
          written règlement — `autres_documents` names the ones it publishes.

        Args:
            idurba: document version id — the `zone.idurba` field from `urba_zonage`.
            zone: zone label to search for (e.g. "UCt2", "UM").
            query: alternative keyword (e.g. "hauteur maximale", "emprise au sol").
            max_extraits: max passages returned (1-50). context_lignes: lines kept after each hit.
            ingest: start the on-demand ingestion when the règlement is not cached
                (default false: only report its state).
        """
        from oto_mcp.fod import reglement as fod_reglement

        def _extraits() -> dict:
            return fod_reglement.extraits(idurba, zone=zone, query=query,
                                          max_extraits=max_extraits,
                                          context_lignes=context_lignes)

        res = _extraits()
        if res.get("cached"):
            return res
        etat = _etat_ingestion(fod_reglement, idurba, lancer=ingest)
        # Prêt ou scanné entre les deux appels : les extraits (ou le constat de scan) du
        # cache, pas l'état.
        return _extraits() if etat is None else etat

    @mcp.tool()
    def urba_lieu(address: Optional[str] = None, idu: Optional[str] = None,
                  geom: Optional[dict[str, Any]] = None, on_parcel: bool = True,
                  sup_categories: Optional[list[str]] = None) -> dict:
        """Every planning rule that applies to a PLACE — an address, a cadastral
        parcel or a geometry — via the Géoportail de l'Urbanisme (open data, no key).

        Unlike `urba_zonage` (one point), this queries the WHOLE parcel: a zone,
        prescription or servitude that only touches a corner of the plot is found.
        Returns, without geometries: `location` (what was queried: the parcel under
        the address when `on_parcel`, else the point), `municipalities` (`is_rnu`),
        `documents` (PLU, PLUi, CC, PSMV…), `zones`, `cc_sectors`, `prescriptions`,
        `informations`, `servitudes` (public-utility easements: category such as AC1
        historic monument surroundings or PM1 risk plan, footprints and generators),
        and `notes` — READ them: an empty list does NOT prove absence, the GPU is not
        complete. Identical objects are grouped with a `count`.

        Give EXACTLY ONE of `address`, `idu`, `geom`.

        Args:
            address: a postal address, geocoded (Géoplateforme/BAN).
            idu: cadastral parcel identifier, 14 characters (e.g. "35238000AC0680").
            geom: a GeoJSON geometry (Point, Polygon or MultiPolygon, WGS84).
            on_parcel: with `address` — query the parcel under the point (default)
                rather than the point itself.
            sup_categories: restrict servitudes to these categories (e.g. ["AC1"]).
        """
        import requests
        from oto.tools.common.errors import UpstreamHTTPError
        from oto.tools.urbanisme import UrbanismeClient, UrbanismeNotFound

        try:
            return UrbanismeClient().report(address=address, idu=idu, geom=geom,
                                            on_parcel=on_parcel,
                                            sup_categories=sup_categories)
        except (ValueError, UrbanismeNotFound) as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e))) from e
        except UpstreamHTTPError as e:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"The Géoportail de l'Urbanisme refused the request "
                        f"(HTTP {e.status_code}): {str(e.body)[:300]}")) from e
        except requests.RequestException as e:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"The IGN planning services could not be reached "
                        f"({type(e).__name__}) — retry later.")) from e

    # --- risks (Géorisques) ------------------------------------------------

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
    def urba_annuaire(
        op: Literal["services", "maires", "presidents_epci"] = "services",
        code_commune: Optional[str] = None,
        siren: Optional[str] = None,
        departement: Optional[str] = None,
        type_service: Optional[str] = None,
        limit: int = 20,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Who decides and who answers on a PUBLIC target: services, mayors, EPCI presidents.

        On a public-sector prospect the decision-maker is an elected official and the
        contact a public service — neither is a company director, and paid enrichment
        finds nothing.

        `op="services"` (default) — the DILA administration directory (~36,000 town
        halls plus prefectures, tax offices, departmental directorates) by
        `code_commune` or `siren`, optionally narrowed by `type_service` (the
        directory's « pivot »: `mairie`, `prefecture`, `dd_fip`…). Returns switchboard,
        generic e-mail, website and `responsables` — the NAMED head of the service with
        their role. A value the source serialised badly is listed in
        `champs_illisibles`, never silently emptied.

        `op="maires"` — the mayor, by `code_commune` or `departement`.
        `op="presidents_epci"` — the intercommunality president, by `siren` (the EPCI's)
        or `departement`; only the PRESIDENT is kept, not the thousands of community
        councillors. Both return the start date of the office — useful to tell whether
        the contact changed since the last campaign. Birth date and sex are in the
        source file and deliberately NOT returned.

        Match on the INSEE code, never on the commune NAME: "Sainte-Marie" exists
        dozens of times. A parameter the chosen op cannot honour is REFUSED.
        `fields` keeps only these keys in each record; the envelope always stays.

        Args:
            op: "services" (default), "maires" or "presidents_epci".
            code_commune: INSEE commune code (services, maires).
            siren: organisation SIREN (services) or EPCI SIREN (presidents_epci).
            departement: department code (maires, presidents_epci).
            type_service: directory « pivot » type (services only).
            limit: records returned.
            fields: keys kept in each record.
        """
        acceptes = {
            "services": {"code_commune", "siren", "type_service"},
            "maires": {"code_commune", "departement"},
            "presidents_epci": {"siren", "departement"},
        }[op]
        poses = {n for n, v in (("code_commune", code_commune), ("siren", siren),
                                ("departement", departement), ("type_service", type_service)) if v}
        if poses - acceptes:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"op='{op}' n'accepte pas {', '.join(sorted(poses - acceptes))} "
                f"(accepte : {', '.join(sorted(acceptes))})")))
        if not poses & (acceptes - {"type_service"}):
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"op='{op}' requiert au moins : {', '.join(sorted(acceptes - {'type_service'}))}")))
        if op == "maires":
            res = elus.maires(code_commune=code_commune, departement=departement, limit=limit)
        elif op == "presidents_epci":
            res = elus.presidents_epci(siren=siren, departement=departement, limit=limit)
        else:
            res = annuaire.services(siren=siren, code_commune=code_commune,
                                    type_service=type_service, limit=limit)
        return output_projection.project(res, items_path="signaux", fields=fields)

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

    # --- EPFIF (land control, Île-de-France) ---------------------------------

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

    # --- commune socio-demographics (INSEE Mélodi) ---------------------------

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
            # noqa: SILENT — the per-layer failure is rendered in the result row
            except Exception as e:  # noqa: BLE001 — degrade per block
                out[key] = {"error": f"{type(e).__name__}: {e}"}
        return out

    # --- census at IRIS / neighbourhood level (INSEE, bundled parquet) --------

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


# --- règlement à la demande : l'état d'une ingestion et ses refus nommés ----------------

#: La cause qu'écrit le service quand une pièce dépasse sa taille admise à la demande
#: (`france_opendata.reglement.ReglementTropVolumineux`, préfixe du message d'échec).
_CAUSE_TROP_VOLUMINEUX = "ReglementTropVolumineux"


def _refus_reglement(code: str, message: str, **data: Any) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=f"Refused `{code}`: {message}",
                              data={"code": code, **data}))


def _etat_ingestion(fod_reglement: Any, idurba: str, *, lancer: bool) -> Optional[dict]:
    """L'état d'un règlement absent du cache, lancé (`lancer`) ou relu. `None` = il est
    devenu prêt (ou scanné) entre-temps : relire les extraits. Un échec, un service
    occupé ou un document introuvable LÈVENT, chacun sous son code."""
    http, e, apres = fod_reglement.ingestion(idurba, lancer=lancer)
    if http == 503:
        raise _refus_reglement(
            "reglement_service_occupe",
            "the service is already ingesting another règlement (one at a time). Retry "
            "after the delay; do not answer from memory meanwhile.",
            retryable=True, retry_after_seconds=apres or 60)
    if http == 404:
        raise _refus_reglement(
            "reglement_introuvable",
            f"{e.get('detail') or 'the GPU does not publish this document'}. Use one of "
            "`autres_documents` (from `urba_zonage`), never a guess.",
            retryable=False, autres_documents=list(e.get("autres_documents") or []))
    statut = e.get("statut")
    if statut in ("pret", "scanne"):
        return None
    if statut == "echec":
        erreur = str(e.get("erreur") or "")
        if erreur.startswith(_CAUSE_TROP_VOLUMINEUX):
            raise _refus_reglement(
                "reglement_trop_volumineux",
                "a piece of this règlement exceeds the on-demand size limit (80 MB): only "
                "the service's batch processing can ingest it. Say so and give the source "
                "PDF; never invent a rule.",
                retryable=False, erreur=erreur)
        raise _refus_reglement(
            "reglement_ingestion_echec",
            f"the ingestion failed ({erreur or 'no cause given'}). Say so, cite the cause, "
            "never invent a rule.",
            retryable=False, erreur=erreur,
            relance_possible_apres=e.get("relance_possible_apres"))
    if statut == "en_cours":
        return {"idurba": idurba, "cached": False, "statut": "en_cours",
                "depuis": e.get("depuis"), "retry_after_seconds": apres or 20,
                "message": "Ingestion running (20 s to a few minutes): call urba_reglement "
                           "again with the same arguments after `retry_after_seconds`."}
    if statut == "absent":
        return {"idurba": idurba, "cached": False, "statut": "absent",
                "message": "Not ingested yet: call urba_reglement again with `ingest=true` "
                           "to start the ingestion (20 s to a few minutes), then again "
                           "until the excerpts come back."}
    raise RuntimeError(f"Unknown ingestion state from the data service: {statut!r}")
