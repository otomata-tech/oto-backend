"""Foncier — site / parcel / address data (French open data, no key).

Groups in one place what characterizes a **site** (as opposed to company
identity, namespace `fr`): geocoding, cadastre, existing buildings,
risks/ICPE, solar yield, electricity consumption signals, real-estate
valuation by comparables. All clients come from `france-opendata`
(open data, no key).

ADR 0010 (coherent namespaces): `foncier_icpe` (Géorisques) and the DVF tools
used to be scattered under `fr` / `dvf` — grouped here. `foncier_permis_search`
(Sit@del) queries the DiDo API `/rows` **live** (server-side commune/dept/year filter) —
the queryable counterpart of the solar yield; bulk ingestion via the national CSV
(276 MB) stays reserved for consumers that cross sources (cf. GR), outside oto.

Open-data connector: no credential. Exposed only if activated in DB
(activation notch, ADR 0010) — register_all gates on `connector_activation`.

**Consolidated surface (ADR 0047 §Amendment, applied to the foncier connector)**: one
tool per business OBJECT, the verb as an `op` parameter — 14 JSON tools → 8.

⚠️ The connector is **READ-ONLY**: open data, no writes, no
credit consumed. No op has side effects, so the `op` defaults are
reads like the rest. What costs here is the VOLUME swept upstream: the
two national-scan guards are kept as they were (`foncier_permis_search`
requires a commune/dept/applicant scope, `foncier_conso_elec` requires a perimeter).

| before                         | after                                    |
| ------------------------------ | ---------------------------------------- |
| `foncier_reverse`              | `foncier_site(op="adresse")`             |
| `foncier_parcelle`             | `foncier_site(op="parcelle")` — default  |
| `foncier_bati`                 | `foncier_site(op="bati")`                |
| `foncier_productible_solaire`  | `foncier_site(op="solaire")`             |
| `foncier_prix_m2`              | `foncier_dvf(op="prix_m2")` — default    |
| `foncier_comparables`          | `foncier_dvf(op="comparables")`          |
| `foncier_comparables_adresse`  | `foncier_dvf(op="comparables_adresse")`  |
| `foncier_dpe_adresse`          | `foncier_dpe(op="adresse")` — default    |
| `foncier_dpe_stats`            | `foncier_dpe(op="stats")`                |

`foncier_site` is keyed by the POINT: its four ops take exactly
`lat`/`lon` (+ `kwc` for the solar op only). FIVE tools stay ALONE — their
parameters don't overlap those of their neighbors, and a `oneOf` of disjoint
variants weighs what the separate tools weighed (the criterion is parameter
homogeneity, not the count):
- `foncier_geocode`: key = a free-text address (+ its postcode/commune filters),
  no `lat`/`lon` — it is the ENTRY of the namespace (address → point), not a
  read at a point; its postcode fallback is its own;
- `foncier_isochrone`: shares `lat`/`lon` with `foncier_site`, but adds four
  disjoint parameters (time budget OR distance, mode, direction) and returns a ZONE
  (polygon) instead of a characteristic of the point — it would double the schema of
  `foncier_site` for a single op;
- `foncier_permis_search`: nine parameters, including the APPLICANT axis (`siren`/`siret`)
  which exists nowhere else in the namespace;
- `foncier_conso_elec`: scope year × perimeter × MWh band, two grid tiers;
- `foncier_icpe`: key `siret` or `code_insee` (Géorisques register, its own
  pagination), no shared parameter.

The three variants rendered `*_app` (MCP Apps SEP-1865) are OUT of the scope of the
consolidation — they return a UI component, not JSON. Their prose still names
the old tools: the table above gives the mapping.
"""
from __future__ import annotations

import re
from typing import Literal, Optional

from fastmcp import FastMCP
from france_opendata import compact

from .. import output_projection
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

# OPTIONAL import of prefab_ui (extra `fastmcp[apps]`) at MODULE level — and NOT
# local to register(): the *_app tools below annotate their return `-> Card`,
# and FastMCP resolves the type hints (via get_type_hints, especially since
# `from __future__ import annotations` makes them lazy) against `fn.__globals__`,
# the MODULE namespace. A local import leaves `Card` undefined at module level →
# `NameError: name 'Card' is not defined` at registration (issue #69), which
# disabled ALL foncier *_app tools in prod. If it's missing (extra `apps`
# absent), the *_app tools are not registered — the JSON tools remain (graceful
# degradation, same principle as "if rendering fails, use the JSON tool").
try:
    from prefab_ui.components import (  # type: ignore
        Card, Column, DataTable, DataTableColumn, Heading, Text,
    )
    _PREFAB_UI_AVAILABLE = True
# noqa: SILENT — extra `apps` absent ⇒ no *_app tool, the JSON tools remain
except Exception:  # pragma: no cover - extra `apps` absent
    _PREFAB_UI_AVAILABLE = False


# Page sizes allowed by the DiDo API (Sit@del) — inlined (formerly imported from
# france_opendata.sitadel, removed at B4: no direct dependency on the lib anymore).
_DIDO_PAGE_SIZES = (10, 20, 50, 100)

# Geocoding — detects "the query carries a street number" ("227 rue X"), which
# makes the absence of a `housenumber` candidate suspicious rather than normal.
_NUMBERED_ADDRESS_RE = re.compile(r"^\s*\d{1,4}\s*(?:bis|ter|quater)?\s+\S", re.I)
_POSTCODE_RE = re.compile(r"\b\d{5}\b")


# Ops of each consolidated tool. SINGLE SOURCE: input validation AND the
# refusal message derive from it (`_ops_error`), so an added op can't be
# accepted without being announced to the agent, nor the reverse.
_SITE_OPS = ("parcelle", "bati", "solaire", "adresse")
_DVF_OPS = ("prix_m2", "comparables", "comparables_adresse")
_DPE_OPS = ("adresse", "stats", "tertiaire")

# `years` does NOT have the same default depending on the DVF op (2 years for the raw
# mutations of a commune, 3 for the stats and the neighborhood of an address): the
# merged parameter is therefore `None` by default and resolved here, so as to change
# the behavior of NONE of the three reads.
_DVF_YEARS_DEFAULT = {"prix_m2": 3, "comparables": 2, "comparables_adresse": 3}

# Sit@del files served by DiDo. SINGLE SOURCE: the refusal of an unknown `kind`
# derives from it. Without this check, the value went as-is down to the lib, whose
# `ValueError` surfaced as an opaque 500 — indistinguishable from a real outage.
_PERMIS_KINDS = ("logements", "locaux", "amenager")

# HARD cap of the ADEME DataFair sources (BEGES, tertiary DPE): `size` is bounded
# to 10,000 server-side, without saying so. We announce it rather than let it be discovered.
_SOURCE_SIZE_CAP = 10_000

# EXACT labels of the ERP activity sector of the tertiary DPE (ADEME). The source
# filters on the exact phrase (`secteur_activite:"…"`), not free text: an
# approximate value returns 0, indistinguishable from a real empty. Taken on 2026-09-09 from
# `values_agg` of dataset j9ol0fwjqckyf49vr29nknbu (32 values, ~560k diagnostics);
# refreshed by that same call if ADEME adds some. These are French DATA values
# matched at runtime: kept in French.
_DPE_TERTIAIRE_SECTEURS = (
    "M : Magasins de vente, centres commerciaux",
    "autres tertiaires non ERP",
    "W : Administrations, banques, bureaux",
    "locaux d'entreprise (bureaux)",
    "N : Restaurants et débits de boisson",
    "J : Structures d\u2019accueil pour personnes \u00e2g\u00e9es ou personnes handicap\u00e9es",
    "U : \u00c9tablissements de soins",
    "R : \u00c9tablissements d\u2019\u00e9veil, d\u2019enseignement, de formation, centres de vacances, "
    "centres de loisirs sans h\u00e9bergement",
    "O : H\u00f4tels et pensions de famille",
    "L : Salles d'auditions, de conf\u00e9rences, de r\u00e9unions, de spectacles ou \u00e0 usage multiple",
    "GHW : Bureaux",
    "X : \u00c9tablissements sportifs couverts",
    "T : Salles d'exposition \u00e0 vocation commerciale",
    "GHZ : Usage mixte",
    "P : Salles de danse et salles de jeux",
    "V : \u00c9tablissements de divers cultes",
    "S : Biblioth\u00e8ques, centres de documentation",
    "PA : \u00c9tablissements de Plein Air",
    "OA : H\u00f4tels-restaurants d'Altitude",
    "GA : Gares Accessibles au public (chemins de fer, t\u00e9l\u00e9ph\u00e9riques, remonte-pentes...)",
    "Y : Mus\u00e9es",
    "PS : Parcs de Stationnement couverts",
    "GHR : Enseignement",
    "GHU : Usage sanitaire",
    "GHO : H\u00f4tel",
    "GHA : Habitation",
    "REF : REFuges de montagne",
    "CTS : Chapiteaux, Tentes et Structures toile",
    "EF : \u00c9tablissements flottants (eaux int\u00e9rieures)",
    "GHS : D\u00e9p\u00f4t d'archives",
    "GHTC : tour de contr\u00f4le",
    "SG : Structures Gonflables",
)


def _secteur_tertiaire(valeur: str) -> str:
    """Resolves `secteur` to an EXACT ERP label, or refuses by naming them.

    The source does NOT do free search: it compares the whole phrase. The three
    examples this doc used to give ("hospital", "enseignement", "bureaux") therefore
    returned 0, and that zero was indistinguishable from a sector with no diagnostic.
    A filter that doesn't bite must say so — it can't return a credible empty.

    An ambiguous word is not settled on our behalf: "bureaux" designates three labels
    (159,000 rows spread across them), we name them and the caller chooses.
    """
    v = (valeur or "").strip()
    exact = {s.casefold(): s for s in _DPE_TERTIAIRE_SECTEURS}
    if v.casefold() in exact:
        return exact[v.casefold()]
    proches = [s for s in _DPE_TERTIAIRE_SECTEURS if v.casefold() in s.casefold()]
    if len(proches) == 1:
        return proches[0]
    liste = proches or list(_DPE_TERTIAIRE_SECTEURS)
    tete = ("several ERP sector labels contain it" if proches
            else "no ERP sector label matches it")
    raise _bad(
        f'secteur={v!r} is not an ERP sector label of the ADEME tertiary DPE, and '
        f"{tete}. This filter is an EXACT match on the whole label, not free text: an "
        "unknown value would return zero rows, which is indistinguishable from a sector "
        "with no diagnostic. Admitted values"
        + (" containing it" if proches else "")
        + ": " + " | ".join(liste)
    )


def _borne(limit: Optional[int]) -> int:
    """`limit` actually requested from the source.

    `limit <= 0` means "no cap" everywhere in this namespace — that was true
    of `foncier_conso_elec` but not of `foncier_dpe(op="tertiaire")`, where `-1` went
    as-is as the page size and returned ONE row (hence `total: 1`, which read
    as "this département has only one diagnostic"). The same parameter can't
    mean two things.
    """
    if limit is None or limit <= 0:
        return _SOURCE_SIZE_CAP
    return min(limit, _SOURCE_SIZE_CAP)


def _marquer_troncature(res: dict, borne: int, compte: Optional[int] = None) -> dict:
    """Names the cut when `total` has saturated on the bound.

    `total` in these reads is the number of rows RETURNED, never the population:
    it saturates on `limit` without any field saying so, and a caller who keeps the
    default reads a truncation as a count. A cut that isn't named is a
    false figure, not a partial answer.
    """
    if not isinstance(res, dict):
        return res
    n = compte if compte is not None else res.get("total")
    tronque = isinstance(n, int) and n >= borne
    res["tronque"] = tronque
    if tronque:
        plafond = ", hard cap of the source" if borne >= _SOURCE_SIZE_CAP else ""
        # ⚠️ **When the cut applies to a DIFFERENT count than `total`, say so — otherwise
        # the warning lies too** (#859, 2026-09-10). On a read with
        # thresholding, the bound falls on the rows READ and `total` only counts the
        # retained ones: a `total: 2` with `lignes_lues: 60` announced "total = 60 is
        # the number of rows RETURNED", wrong twice. Measured on a whole
        # département: 2 announced, 61 real — a deposit wrong by 97%, under a
        # warning meant to save the stake.
        retenus = res.get("total")
        if compte is not None and isinstance(retenus, int) and retenus != n:
            res["avertissement_troncature"] = (
                f"{n} rows were READ and the cut fell there ({borne}"
                f"{plafond}) — UPSTREAM of the thresholding. `total` = {retenus} only counts "
                f"the rows retained AMONG these {n}: it is neither the population "
                f"nor a count of what exists. Rerun with `limit=-1` to "
                f"learn the real size."
            )
        else:
            res["avertissement_troncature"] = (
                f"`total` = {n} is the number of rows RETURNED, not the population: "
                f"the cut fell on the bound ({borne}{plafond}). Tighten the "
                "filters or raise `limit` — don't read this figure as a count."
            )
    return res


def _annee_servie(transport: dict) -> Optional[str]:
    """The vintage ACTUALLY returned, read from the rows — not the one requested.

    `/api/foncier/odre/conso` re-echoes the requested year in `annee`, even when it
    doesn't exist in the source: comparing that field to the request is comparing a
    value to itself. The only attestation of a vintage is carried by the rows.
    """
    for sig in (transport.get("signals") or []):
        an = sig.get("annee")
        if an:
            return str(an)[:4]
    return None


def _millesime(transport: Optional[dict], annee: str, reseau: str) -> Optional[str]:
    """Names what the transmission tier didn't return, and why it may have returned nothing.

    A tier zero reads as "no site connected to transmission" when it just as often
    means "this vintage doesn't exist yet". ODRÉ lags behind Enedis, and
    the current year is exactly the one a caller naturally picks on the
    distribution side: crossing the two erases the whole transmission tier without a word.
    """
    if reseau not in ("transport", "les_deux") or not isinstance(transport, dict):
        return None
    demandee = str(annee)[:4]
    if not transport.get("total"):
        # The service may return the vintages it has (additive field): if it does,
        # we NAME the year to replay instead of making the caller guess. If it doesn't,
        # we still say the zero is ambiguous — we invent no year.
        dispo = transport.get("annees_disponibles") or []
        connu = (" Vintages actually served by ODRÉ: "
                 + ", ".join(str(a) for a in dispo) + ".") if dispo else ""
        return (
            f"transmission tier (ODRÉ/RTE): 0 rows for {demandee}. This dataset lags behind "
            "Enedis — a zero here means \"vintage absent from the source\" as "
            "often as \"no site connected to the transmission grid\". Don't read this "
            "result as a complete perimeter; replay on a vintage the "
            f"source carries before concluding.{connu}"
        )
    servie = _annee_servie(transport)
    if servie and servie != demandee:
        return (
            f"distribution {demandee} vs transmission {servie} — the two tiers are "
            "not aligned, don't sum without saying so"
        )
    return None


# ICPE record reading (energy headings first, capped, inspections): shared library,
# `france_opendata.compact.compact_icpe` — the same one every consumer uses.


def _ops_error(ops: tuple[str, ...]) -> str:
    quoted = [f"'{o}'" for o in ops]
    return "op must be " + ", ".join(quoted[:-1]) + f" ou {quoted[-1]}"


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Mandatory argument for THIS op — actionable error that NAMES the op and
    the argument, never a fallback.

    An EMPTY value counts as absent: `adresse=""` on `op='comparables_adresse'`
    would go geocode nothing and return an arbitrary neighborhood, which would pass
    for an answer.
    """
    if value is None or (isinstance(value, (str, list)) and not value):
        raise _bad(f"op='{op}' requires {name}")
    return value


def _has_housenumber(candidates: list[dict]) -> bool:
    return any(c.get("type") == "housenumber" for c in candidates)


def register(mcp: FastMCP) -> None:
    from ..fod import foncier as fod_foncier
    from ..fod import urba as fod_urba  # georisques (ICPE) — servi par FOD depuis B3

    # Site data served by the dedicated FOD service (ADR 0028) — the backend
    # no longer runs these calls in-process. Proxy objects with a surface identical to the
    # france_opendata clients (same methods/signatures) → only these bindings
    # change, the tool bodies remain unchanged.
    ban = fod_foncier.ban
    cadastre = fod_foncier.cadastre
    bdtopo = fod_foncier.bdtopo
    pvgis = fod_foncier.pvgis
    ign = fod_foncier.ign
    enedis = fod_foncier.enedis
    odre = fod_foncier.odre
    beges = fod_foncier.beges
    bdnb = fod_foncier.bdnb
    irep = fod_foncier.irep
    dpe_tertiaire = fod_foncier.dpe_tertiaire
    dvf = fod_foncier.dvf
    dpe = fod_foncier.dpe
    sitadel = fod_foncier.sitadel
    # georisques (ICPE): served by FOD (B3), shared with urba — same proxy.
    georisques = fod_urba.georisques

    # --- geocoding (BAN — Base Adresse Nationale) ----------------------------

    @mcp.tool()
    def foncier_geocode(
        adresse: str,
        limit: int = 5,
        code_postal: Optional[str] = None,
        code_commune: Optional[str] = None,
    ) -> list[dict]:
        """Geocode a French address → coordinates, canonical label, INSEE code.

        Returns candidates (label, score, lat, lon, citycode, postcode, `type`), best
        first. The BAN label is a canonical address key (two spellings converge on one
        point). `type` grades the match: housenumber (exact door) > street > locality >
        municipality — a locality answer to a numbered query is NOT the address.

        Postcode fallback: SIRENE addresses often carry the commune's generic postcode
        (80000 Amiens) while the BAN indexes the real one for that stretch (80090). The
        query then yields only a low-score locality. When the query carries a street
        number and no housenumber comes back, this retries WITHOUT the postcode (both
        the argument and the one written in the address) — those candidates are tagged
        `relaxed="postcode"`. If that still finds no housenumber, every candidate is
        tagged `warning="no_housenumber_match"`: treat them as approximate.

        The reverse direction (point → nearest address) is `foncier_site(op="adresse")`.

        Args:
            adresse: free-form address (e.g. "44 la canebière marseille").
            limit: max candidates (default 5).
            code_postal: restrict to a postcode.
            code_commune: restrict to an INSEE commune code. Safer than code_postal to
                narrow a search — a commune code is stable, a postcode is not.
        """
        res = ban.search(adresse, limit=limit, postcode=code_postal, citycode=code_commune)
        if not _NUMBERED_ADDRESS_RE.match(adresse) or _has_housenumber(res):
            return res
        # The postcode hides in TWO places: the argument and the string. Measured on
        # case #324 ("227 rue Saint-Fuscien 80000 Amiens") — removing the argument alone
        # changes nothing, it's the postcode written in the text that overrides the stretch; without it
        # the BAN returns the right number at 0.98 instead of a locality at 0.57.
        relaxed = " ".join(_POSTCODE_RE.sub(" ", adresse).split())
        if relaxed != adresse or code_postal:
            retry = ban.search(relaxed, limit=limit, postcode=None, citycode=code_commune)
            if _has_housenumber(retry):
                return [{**c, "relaxed": "postcode"} for c in retry]
        return [{**c, "warning": "no_housenumber_match"} for c in res]

    # --- the site at a point: address / cadastre / buildings / solar ---------------

    @mcp.tool()
    def foncier_site(
        lat: float,
        lon: float,
        op: Literal["parcelle", "bati", "solaire", "adresse"] = "parcelle",
        kwc: Optional[float] = None,
    ) -> Optional[dict]:
        """What a point (lat, lon) carries — cadastral parcel, built footprint,
        nearest address, solar yield. Geocode the address first (foncier_geocode).

        `op`:
        - **"parcelle"** (default): cadastral parcel at the point (API Carto IGN),
          or null. Returns idu (unique id), commune, INSEE code, section, numéro,
          area (contenance_m2) and GeoJSON geometry. Use to identify the land unit
          under an address.
        - **"bati"**: built footprint on that parcel (IGN BDTOPO V3) — ground area,
          real CES, uses, heights. Resolves the cadastral parcel at the point, then
          sums BDTOPO buildings whose centroid falls inside it. `ces_reel` = built
          area / parcel area (low CES in a dense area = under-developed land signal).
          Returns an `error` key if no parcel is found at the point.
        - **"solaire"**: annual solar yield (kWh) for a PV system of `kwc` kWp at the
          point, via PVGIS (JRC). Picks optimal tilt/azimuth for a rooftop install.
          Returns physical data only (productible_kwh_an, irradiance, losses, optimal
          angles) — no tariff or business assumptions. Null if inputs invalid or
          PVGIS unavailable.
        - **"adresse"**: reverse-geocode the point (BAN) → nearest known address,
          or null.

        Args:
            lat: latitude of the point (WGS84).
            lon: longitude of the point (WGS84).
            op: parcelle (default) | bati | solaire | adresse.
            kwc: op="solaire" — peak power of the PV system, in kWp. REQUIRED for
                that op, ignored by the others.
        """
        if op not in _SITE_OPS:
            raise _bad(_ops_error(_SITE_OPS))

        if op == "parcelle":
            return cadastre.parcelle_at(lat, lon)

        if op == "adresse":
            return ban.reverse(lat, lon)

        if op == "solaire":
            return pvgis.productible(lat, lon, _need(kwc, "kwc", op))

        if op == "bati":
            parcelle = cadastre.parcelle_at(lat, lon)
            if not parcelle or not parcelle.get("geometry"):
                return {"error": "no_parcel_at_point", "lat": lat, "lon": lon}
            return bdtopo.bati_parcelle(
                parcelle["geometry"], contenance_m2=parcelle.get("contenance_m2"))

        # Structurally unreachable (input guard above) — a safety net against
        # an implicit `return None` if an op were added to `_SITE_OPS` without its
        # branch: better to refuse than to return "nothing" as a success.
        raise _bad(_ops_error(_SITE_OPS))

    # --- isochrone / catchment area (IGN Géoplateforme) ------------------

    @mcp.tool()
    def foncier_isochrone(lat: float, lon: float, minutes: Optional[float] = None,
                          metres: Optional[int] = None, mode: str = "pied",
                          direction: str = "departure") -> dict:
        """Reachable-area (isochrone / catchment) polygon around a point, via IGN.

        The travel-time zone one can reach from (lat, lon) — the primitive for a
        retail catchment area. Give EITHER `minutes` (time budget) OR `metres`
        (distance budget), not both. `mode`: "pied"/pedestrian or "voiture"/car.
        `direction`: "departure" (area reachable FROM the point) or "arrival"
        (area FROM WHICH the point is reachable — differs by car with one-ways).

        Returns the GeoJSON `geometry` (Polygon of the reachable area) plus its
        `centroid` and `bbox`. To answer "who is > N min away from X", compute an
        isochrone around each X and test population/points against the polygons —
        this tool returns one zone; the coverage analysis is the caller's compose
        step (e.g. cross with urba_iris population). Geocode the address first
        (foncier_geocode → lat/lon).
        """
        prof = {"pied": "pedestrian", "voiture": "car"}.get(mode, mode)
        return ign.isochrone(lat, lon, minutes=minutes, metres=metres,
                             profile=prof, direction=direction)

    # --- planning permits (Sit@del / SDES, live DiDo API) ------------------

    def _snap_page_size(limit: int) -> int:
        """Snaps `limit` to an allowed DiDo page size (10/20/50/100)."""
        return next((s for s in _DIDO_PAGE_SIZES if s >= limit), _DIDO_PAGE_SIZES[-1])

    @mcp.tool()
    def foncier_permis_search(
        code_commune: Optional[str] = None,
        dept: Optional[str] = None,
        kind: str = "logements",
        annee_min: Optional[int] = None,
        annee_max: Optional[int] = None,
        siren: Optional[str] = None,
        siret: Optional[str] = None,
        page: int = 1,
        limit: int = 50,
    ) -> dict:
        """Building/urbanism permits (Sit@del, SDES) for a commune, department or APPLICANT.

        Live query on the DiDo API (server-side filter) — no bulk download. National
        register of urban-planning authorizations (PC/PA/DP) since 2013, monthly refresh.
        A scope is REQUIRED — `code_commune`, `dept` **or** `siren`/`siret` — because a
        national scan is huge. `siren` needs NO geography: "every permit filed by this
        company", France-wide, in one query (due diligence on a company's projects).

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

        To find the permits on a given cadastral PARCEL there is no server-side filter
        (DiDo stores up to three section/number pairs per permit and ANDs them): scope by
        commune, then match the `parcelles` key of the returned permits.

        Args:
            code_commune: INSEE commune code (e.g. "75056"). Exact match.
            dept: INSEE department code (e.g. "59", "2A"). Use for a whole department.
            kind: "logements" (default) | "locaux" | "amenager". Any other value is
                REFUSED here, naming the three: it used to travel down to the source
                and come back as an opaque 500, which looks exactly like a real outage.
            annee_min / annee_max: deposit-year bounds (inclusive).
            siren: applicant's SIREN — server-side filter, combinable with the geography
                but sufficient on its own. Note ~35 % of permits carry no applicant
                (natural persons, GDPR diffusion rule): those are out of reach by design,
                so an empty result is not proof the company filed nothing.
            siret: applicant's SIRET, same idea at establishment level.
            page: 1-based page.
            limit: max permits per page (snapped to 10/20/50/100, cap 100). `total` in
                the result is the full server-side count — page through for more.
        """
        if kind not in _PERMIS_KINDS:
            raise _bad(
                f"kind={kind!r} is not a Sit@del file. Allowed values: "
                + ", ".join(f"'{k}'" for k in _PERMIS_KINDS)
                + ". (A value outside the enumeration used to go all the way to the source and "
                "come back as an opaque 500, impossible to tell apart from an outage.)"
            )
        if not code_commune and not dept and not siren and not siret:
            raise ValueError(
                "Provide `code_commune`, `dept`, `siren` or `siret` "
                "(an unfiltered national scan is prohibited)."
            )
        page_size = _snap_page_size(max(1, limit))
        res = sitadel.search(
            kind,
            communes=code_commune or None,
            dept=dept or None,
            an_min=annee_min,
            an_max=annee_max,
            siren=siren or None,
            siret=siret or None,
            page=page,
            page_size=page_size,
        )
        res["permis"] = res["permis"][:limit]
        res["kind"] = kind
        return res

    # --- electricity consumption by address (Enedis) ------------------------

    @mcp.tool()
    def foncier_conso_elec(
        annee: str,
        dept: Optional[str] = None,
        secteur: Optional[str] = None,
        naf2: Optional[list[str]] = None,
        code_commune: Optional[list[str]] = None,
        code_epci: Optional[str] = None,
        min_mwh: Optional[float] = None,
        max_mwh: Optional[float] = None,
        reseau: Literal["distribution", "transport", "les_deux"] = "distribution",
        maille: Literal["ligne", "site"] = "ligne",
        limit: int = 200,
    ) -> dict:
        """Annual electricity consumption of French sites (open data, no key).

        TWO GRID TIERS, and they are not interchangeable. `reseau="distribution"`
        (default) reads Enedis: consumption per ADDRESS, with the NAF division.
        `reseau="transport"` reads ODRE (RTE): the sites connected to the transmission
        grid, which are ABSENT from Enedis entirely — and they are the largest consumers
        in the country. Saint-Jean-de-Maurienne (73248) returns zero NAF-24 address on
        Enedis and 1,702,616 MWh on ODRE for 2023 — one IRIS (732480104) holding 3
        delivery points, so that row comes back marked `maille="iris_agrege"`, not as a
        single site. A "heavy electricity user" list built on distribution alone is a
        list without the heavy users: use `reseau="les_deux"` for a real one.

        ⚠️ THE TRANSPORT TIER IS A YEAR BEHIND. ODRE's newest millésime is older than
        Enedis's (2023 against 2024, measured 2026-09-09). Asking ODRE for a year it does
        not carry returns ZERO rows for the whole tier — which reads exactly like "no
        transmission-connected site here" while it means "that year does not exist yet".
        That zero is now NAMED in `avertissement_millesime`: read it before concluding.
        Nothing is invented to fill the hole — an absent year stays absent, it is only
        said out loud.

        TWO GRAINS, and `maille` governs BOTH tiers with the same meaning: "site" =
        one row is one site. On DISTRIBUTION, Enedis publishes ONE ROW PER ADDRESS AND
        PER NAF DIVISION, so `maille="ligne"` (default) returns rows, and thresholding
        them one by one MISSES sites whose divisions are each below the bar but whose
        total is above it; `maille="site"` sums an address's divisions and applies
        `min_mwh` AFTER the sum — the grain almost every caller means. A site then
        carries `naf2_principal`, `naf2_detail` and `multi_naf2`. On TRANSPORT, "site"
        keeps only the IRIS that hold a single delivery point, and "ligne" also returns
        the aggregated ones. Filtering those out is how a query for the largest
        consumers returns the small ones — see `maille` below.

        Rows Enedis publishes without an address are real consumption that cannot be
        located: they are never returned as sites, and counted in `lignes_ignorees` /
        `mwh_ignores` instead of being silently dropped.

        Args:
            annee: reference year (e.g. "2024"). ODRE lags a year behind Enedis, so the
                year that fits distribution may return nothing at all on transport —
                `avertissement_millesime` says so rather than letting it pass for empty.
            dept: INSEE department code (e.g. "59").
            secteur: "INDUSTRIE" | "TERTIAIRE" | "AGRICULTURE" — coarse: a hospital and
                an office tower are both TERTIAIRE. Prefer `naf2`.
            naf2: NAF divisions, two digits (e.g. ["24", "23", "86"]) — the grain Enedis
                actually publishes in. Ignored on the transport tier, which carries no NAF.
            code_commune / code_epci: INSEE commune codes, or one EPCI (a métropole).
            min_mwh / max_mwh: consumption band (MWh/year), never GW — no French open
                data publishes subscribed POWER.
            reseau: which grid tier(s) to read.
            maille: "ligne" (as published, default) or "site" (one row = one site).
                On DISTRIBUTION, "site" sums an address's NAF divisions. On TRANSPORT,
                "site" keeps only the IRIS holding a SINGLE delivery point; "ligne"
                also returns the aggregated IRIS, each marked `maille="iris_agrege"`.
                Those aggregates carry 59 % of the transmission tier (2023 national:
                55.3 TWh over 931 rows, of which 22.9 TWh over the 729 single-site
                ones), and the biggest consumer in France is one of them — filtering
                them out is how a "largest consumers" query returns the small ones.
            limit: max rows per tier (default 200); `limit=-1` means NO ceiling, and
                `total` is the number of rows RETURNED, not the population — it
                saturates on `limit`, and says so in `tronque` /
                `avertissement_troncature` when it does.
                ⚠️ **`limit` caps the rows READ, upstream of `min_mwh`** — the
                threshold is applied AFTER reading, so a capped read thresholds only
                what it happened to read. With `min_mwh` set, `total` is therefore a
                FLOOR, never a count: measured on one department, `limit=60` answered
                `total: 2` where `limit=-1` answers 61. **Pass `limit=-1` whenever you
                pass `min_mwh`**, or read `total` as "at least".
        """
        if reseau not in ("distribution", "transport", "les_deux"):
            raise _bad('reseau must be "distribution", "transport" or "les_deux"')
        if not (dept or code_commune or code_epci) and reseau != "transport":
            raise _bad(
                "a perimeter is required (dept, code_commune or code_epci): a national "
                "scan of the distribution tier is huge, and with maille=\"site\" the "
                "threshold cannot be pushed to the server at all."
            )

        out: dict = {"annee": annee, "reseau": reseau, "maille": maille}
        if reseau in ("distribution", "les_deux"):
            if maille == "site":
                out["distribution"] = enedis.sites_par_adresse(
                    annee, dept=dept, code_commune=code_commune, code_epci=code_epci,
                    naf2=naf2, secteur=secteur, min_mwh=min_mwh, limit=limit,
                )
                # Here the cut applies to the ROWS read, before aggregation into sites
                # and before `min_mwh`: `total` (of sites) can't reveal it.
                _marquer_troncature(out["distribution"], _borne(limit),
                                    compte=out["distribution"].get("lignes_lues"))
            else:
                out["distribution"] = enedis.consommation_par_adresse(
                    annee, dept=dept, secteur=secteur, naf2=naf2,
                    code_commune=code_commune, code_epci=code_epci,
                    min_mwh=min_mwh, max_mwh=max_mwh, limit=limit,
                )
        if reseau in ("transport", "les_deux"):
            # `site_unique=True` (the lib's default) only keeps the IRIS with ONE delivery
            # point. The tool forced it without exposing it: aggregated IRIS — 59% of
            # transmission MWh, including France's top consumer — were therefore
            # invisible, and Saint-Jean-de-Maurienne returned 0 though the data exists.
            # The caller's grain governs both tiers, with the same meaning.
            out["transport"] = odre.consommation_transport(
                annee, dept=dept, code_commune=code_commune, min_mwh=min_mwh,
                site_unique=(maille == "site"), limit=limit,
            )
            _marquer_troncature(out["transport"], _borne(limit))
        out["avertissement_millesime"] = _millesime(out.get("transport"), annee, reseau)
        return out

    # --- industrial risks / ICPE (Géorisques) — taken over from `fr` ------------

    @mcp.tool()
    def foncier_icpe(
        op: Literal["installations", "emissions"] = "installations",
        siret: Optional[str] = None,
        code_insee: Optional[str] = None,
        departement: Optional[str] = None,
        page: int = 1,
        annee: int = 2024,
        polluant: Optional[str] = None,
        milieu: Optional[str] = "Air",
        limit: int = 50,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Declared industrial installations (Géorisques): ICPE registry, or IREP emissions.

        `op="installations"` (default) — the ICPE registry by `siret` or `code_insee`:
        regime (Déclaration / Enregistrement / Autorisation), IED, Seveso, activity
        state, geolocation, DREAL service, latest inspection reports, and `rubriques`
        (nomenclature number, nature, authorised quantity) with `rubriques_energie` —
        those whose very activity IS energy use, each with a plain reading: 2910/3110
        combustion, 2920/2921 cooling, 4735/1185 industrial refrigeration. Long sheets
        are capped at 10, energy ones FIRST, and `rubriques_tronquees` says so.
        Detects HEAVY INDUSTRIAL SITES when power consumption is masked in Enedis
        open data — a SOURCED "big consumer" presumption (cite the codeAIOT), NOT a
        consumption: an authorised quantity is m³ or MW of plant, never kWh.

        `op="emissions"` — the IREP registry: declared emissions per ESTABLISHMENT,
        with its SIRET and coordinates, by `departement`, `code_insee` or `siret`.
        The complement to `foncier_beges`: a GHG inventory covers a whole
        ORGANISATION and never says where, IREP names WHICH site weighs — and
        therefore which address to call on.
        ⚠️ 89% of the registry's quantities are the string "< seuil" (declared BELOW
        the reporting threshold): they come back as `quantite: null` with
        `sous_seuil: true`, never as zero, sorted after the known quantities.
        ⚠️ CO2 comes in three labels — fossil (the default), biomass, and the total
        that sums both; reading the total as fossil inflates a site that burns wood.

        A parameter the chosen op cannot honour is REFUSED, never silently dropped.
        `fields` keeps only these keys in each record; the envelope always stays.

        Args:
            op: "installations" (ICPE, default) or "emissions" (IREP).
            siret: establishment SIRET (14 digits).
            code_insee: INSEE commune code.
            departement: INSEE department code (emissions only).
            page: 1-based page of 20 (installations only).
            annee: registry vintage (emissions only).
            polluant: EXACT dataset label (emissions only); omitted = fossil CO2,
                "" = every pollutant.
            milieu: "Air" by default; empty = water and soil too (emissions only).
            limit: establishments returned (emissions only).
            fields: keys kept in each record.
        """
        if op == "emissions":
            if page != 1:
                raise _bad("op='emissions' ne pagine pas par `page` : utiliser `limit`")
            if not any([siret, code_insee, departement]):
                raise _bad("op='emissions' requiert siret, code_insee ou departement")
            res = irep.emetteurs(
                annee=annee, departement=departement, code_commune=code_insee,
                siret=siret, polluant=polluant, milieu=milieu, limit=limit,
            )
            return output_projection.project(res, items_path="signaux", fields=fields)
        refuses = [n for n, v in (("departement", departement), ("polluant", polluant)) if v]
        if refuses:
            raise _bad(f"op='installations' does not accept {', '.join(refuses)} (reserved for op='emissions')")
        if not siret and not code_insee:
            raise _bad("op='installations' requires siret or code_insee")
        res = georisques.installations_classees(siret=siret, code_insee=code_insee, page=page)
        return output_projection.project({
            "results": res.get("results", 0),
            "page": res.get("page", page),
            "total_pages": res.get("total_pages", 1),
            "data": [compact.compact_icpe(d) for d in res.get("data", [])],
        }, items_path="data", fields=fields)

    @mcp.tool()
    def foncier_proprietaire(
        code_commune: Optional[str] = None,
        siren: Optional[str] = None,
        batiment_groupe_id: Optional[str] = None,
        departement: Optional[str] = None,
        emprise_min: Optional[float] = None,
        limit: int = 50,
        full: bool = False,
    ) -> dict:
        """Buildings and the SIREN of their legal-entity owner (BDNB, CSTB).

        The only public source that ties a PLACE to a LEGAL ENTITY without address
        matching: the link comes from the land registry, so it is exact. Works both
        ways — `code_commune` (+ `emprise_min`) lists a town's large buildings with
        their owner, `siren` lists every building a company owns.

        Each record also carries floor footprint, use, construction year, DPE class
        and the building's professional electricity and gas consumption (kWh/year,
        2020 vintage), so a site is qualified without a second call.

        ⚠️ ONLY legal entities published in MAJIC are here. Natural persons — family
        SCIs, farmers, craftsmen — are anonymised at source by the tax administration
        and absent entirely: a missing building is NOT a building without an owner.
        Every response repeats this in `couverture_partielle`.

        ⚠️ The upstream API serves 10 rows per call, so `limit` above 10 costs one
        round-trip per additional 10 — `requetes` says how many were made. `total` is
        what was RETURNED, never what exists: the source publishes no count.

        ⚠️ **No offset: only the FIRST `limit` rows are readable**, in the source's
        own order (ascending owner SIREN, not size). On a large commune or a
        department, `tronque: true` says more exist and you hold the smallest
        SIRENs, not the biggest buildings — narrow with `emprise_min`, or go
        commune by commune, before concluding on a territory.

        Args:
            code_commune: INSEE commune code.
            siren: every building owned by this company (indexed, fast).
            batiment_groupe_id: one building group (`bdnb-bg-…`).
            departement: INSEE department code.
            emprise_min: minimum ground footprint in m² — the prospecting filter.
            limit: buildings returned, 1 to 500 (default 50).
            full: also return `raw`, the BDNB row each record was built from. Every
                column it holds is already in the record, reshaped.
        """
        res = bdnb.batiments(
            code_commune=code_commune, siren=siren,
            batiment_groupe_id=batiment_groupe_id, departement=departement,
            emprise_min=emprise_min, limit=limit,
        )
        if full:
            return res
        # `raw` copies the BDNB row whose every column is already returned, reshaped,
        # in the same record: pure duplication, which the default
        # removes (ADR 0047).
        return output_projection.project(res, items_path="signaux", item_drop=("raw",))

    # --- real-estate valuation (DVF+ Cerema, since 2014) — taken over from `dvf` -

    @mcp.tool()
    def foncier_dvf(
        op: Literal["prix_m2", "comparables", "comparables_adresse"] = "prix_m2",
        code_commune: Optional[str] = None,
        adresse: Optional[str] = None,
        type_local: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        years: Optional[int] = None,
        limit: int = 50,
        radius_m: int = 500,
        with_dpe: bool = False,
    ) -> dict:
        """Real-estate transactions from DVF+ open data (Cerema, since 2014) — price
        stats for a commune, or the raw mutations of a commune / around an address.

        `op`:
        - **"prix_m2"** (default): price stats (€/m²) for a French commune.
          Median/mean/min/max €/m² + per-year breakdown, on clean mono-bien sales
          (one Appartement or Maison per mutation; outliers <100 or >50000 €/m²
          filtered). Use to value a property by comparables. Needs `code_commune`.
        - **"comparables"**: RAW transactions for a commune. NOT filtered: ALL
          property types (flats, houses, land, dependencies, mixed-use, commercial)
          and ALL natures (sale, VEFA off-plan, auction, exchange) — the agent
          decides the use (valuation, land analysis, market volume…). For a clean
          median €/m², use op="prix_m2" instead. Needs `code_commune`.
        - **"comparables_adresse"**: RAW transactions around a PRECISE address.
          Geocodes the address (BAN), returns ALL mutations whose parcel lies within
          `radius_m` metres (distance to nearest parcel vertex — robust to
          multi-parcel goods), nearest first, each with `distance_m`. NOT filtered by
          property type/nature; `median_prix_m2` is computed on residential mono-bien
          rows only (indicative). Needs `adresse`.

        Each raw row (both "comparables" ops): date_mutation, nature_mutation,
        valeur_fonciere, type_bien (raw DVF+ label) + type_local (set only for
        residential mono-bien, else null), surface_reelle_bati, surface_terrain,
        prix_m2 (null if not computable), nombre_locaux, vefa, id_parcelle(s),
        adresse (reverse-geocoded BAN), lat/lon. Most recent first (nearest first
        for "comparables_adresse").

        ⚠️ **Dense communes (Paris arrondissements) can time out on a FIRST call.**
        The public DVF+ API takes 20 to 35 s to serve the first page of one
        arrondissement-year it has not served lately (measured 26/10/2026: 75115,
        75116, 75117), beyond this tool's wait; once served, the same query
        answers in ~1 s. Each year warms up on its own, so a few calls may be
        needed. A timeout here says nothing about the data: retry once or twice
        before concluding.

        With `with_dpe=True` (op="comparables_adresse" only), each sale is enriched
        with ADEME energy data: a HOUSE gets its matched `dpe` (etiquette +
        `dpe_match` confidence by proximity & surface); a FLAT gets `dpe_immeuble`
        (the building's DPE list — NO 1:1 match, as DVF and DPE share no dwelling key).

        Args:
            op: prix_m2 (default) | comparables | comparables_adresse.
            code_commune: op="prix_m2"/"comparables" — INSEE code, 5 digits
                (e.g. "13201" = Marseille 1er).
            adresse: op="comparables_adresse" — free-form address (e.g. "44 la
                canebière marseille").
            type_local: "Appartement" | "Maison". Default: both for "prix_m2",
                everything (all property types) for the two "comparables" ops.
            surface_min / surface_max: OPTIONAL surface bâtie band m² (comparables ops).
            years: lookback in years WITH data (DVF lags ~6 months; up to ~2014).
                Defaults differ per op: 3 for "prix_m2" and "comparables_adresse",
                2 for "comparables".
            limit: comparables ops — max rows (default 50).
            radius_m: op="comparables_adresse" — search radius in metres (default 500).
            with_dpe: op="comparables_adresse" — attach ADEME DPE energy labels per
                sale (default False).
        """
        if op not in _DVF_OPS:
            raise _bad(_ops_error(_DVF_OPS))
        annees = _DVF_YEARS_DEFAULT[op] if years is None else years

        if op == "prix_m2":
            return dvf.stats(code_commune=_need(code_commune, "code_commune", op),
                             type_local=type_local, years=annees)

        if op == "comparables":
            return dvf.comparables(
                code_commune=_need(code_commune, "code_commune", op),
                type_local=type_local, surface_min=surface_min,
                surface_max=surface_max, years=annees, limit=limit,
            )

        if op == "comparables_adresse":
            adr = _need(adresse, "adresse", op)
            res = dvf.comparables_by_address(
                adresse=adr, radius_m=radius_m, type_local=type_local,
                surface_min=surface_min, surface_max=surface_max,
                years=annees, limit=limit,
            )
            if with_dpe and res.get("mutations"):
                from ..dpe_match import attach_dpe_to_sales
                zone = dpe.by_address(adr, radius_m=radius_m, limit=1000)
                attach_dpe_to_sales(res["mutations"], zone.get("dpe", []))
            return res

        raise _bad(_ops_error(_DVF_OPS))

    # --- energy performance (DPE, ADEME) --------------------------------

    @mcp.tool()
    def foncier_dpe(
        op: Literal["adresse", "stats", "tertiaire"] = "adresse",
        adresse: Optional[str] = None,
        code_commune: Optional[str] = None,
        radius_m: int = 200,
        type_batiment: Optional[str] = None,
        etiquette: Optional[str] = None,
        surface_min: Optional[float] = None,
        surface_max: Optional[float] = None,
        secteur: Optional[str] = None,
        departement: Optional[str] = None,
        limit: int = 50,
    ) -> dict:
        """Energy performance diagnostics (DPE, ADEME open data) — raw records around
        an address, or the label distribution of a commune. ~15M dwellings, since
        July 2021.

        `op`:
        - **"adresse"** (default): geocodes the address (BAN), returns raw DPE records
          within `radius_m` metres, nearest first. Each: etiquette_dpe (A–G),
          etiquette_ges, conso_ep_kwh_m2_an, surface_habitable, annee_construction,
          type_batiment, adresse, date_dpe, distance_m, lat/lon. Needs `adresse`.
        - **"stats"**: DPE label distribution (A–G) for a commune — aggregated view of
          energy performance across all its dwellings. Needs `code_commune`.
        - **"tertiaire"**: the NON-residential stock — hospitals, schools, offices,
          shops, restaurants (~560k diagnostics). The other half of the building stock,
          and the one electricity-consumption data describes without qualifying: Enedis
          says HOW MUCH a site consumes, this says WHAT the building is (ERP sector,
          SHON surface, label). Coordinates come back already in Lambert 93 under
          `lambert_x`/`lambert_y`, so a row matches an establishment with no
          intermediate geocoding. `sans_position` counts the ungeocoded ones — they are
          never placed at the centre of their commune. Needs `code_commune` or
          `departement`.

        Args:
            op: adresse (default) | stats.
            adresse: op="adresse" — free-form address.
            code_commune: op="stats" — INSEE code, 5 digits.
            radius_m: op="adresse" — search radius in metres (default 200).
            type_batiment: OPTIONAL "maison" | "appartement" | "immeuble" (both ops).
            etiquette: op="adresse" — OPTIONAL DPE label filter (A..G).
            surface_min / surface_max: op="adresse" — OPTIONAL surface habitable band m².
                op="tertiaire" — `surface_min` applies to SHON instead.
            secteur: op="tertiaire" — the ERP sector label, matched EXACTLY (the
                source compares the whole label, it is not a free-text search).
                Examples that really match: "GHW : Bureaux",
                "W : Administrations, banques, bureaux", "M : Magasins de vente,
                centres commerciaux", "U : Établissements de soins". Any other value
                is refused with the list of the 32 admitted labels — it would
                otherwise return zero rows, indistinguishable from a sector with no
                diagnostic. Careful: "bureaux" alone names THREE labels, so it is
                refused with those three rather than one of them picked for you.
            departement: op="tertiaire" — INSEE department code.
            limit: max records (default 50; nearest first on op="adresse"). On
                op="tertiaire", `limit=-1` means NO ceiling and lands on the source's
                hard cap of 10,000 — and `total` there is the number of rows RETURNED,
                not the population: it saturates on `limit` and says so in `tronque` /
                `avertissement_troncature`.
        """
        if op not in _DPE_OPS:
            raise _bad(_ops_error(_DPE_OPS))

        if op == "adresse":
            return dpe.by_address(
                adresse=_need(adresse, "adresse", op), radius_m=radius_m,
                type_batiment=type_batiment, etiquette=etiquette,
                surface_min=surface_min, surface_max=surface_max, limit=limit,
            )

        if op == "stats":
            return dpe.stats(code_commune=_need(code_commune, "code_commune", op),
                             type_batiment=type_batiment)

        if op == "tertiaire":
            if not (code_commune or departement):
                raise _bad('op="tertiaire" needs code_commune or departement — the '
                           "national stock is ~560k diagnostics.")
            borne = _borne(limit)
            res = dpe_tertiaire.diagnostics(
                code_commune=code_commune, departement=departement,
                secteur=_secteur_tertiaire(secteur) if secteur else None,
                etiquette=etiquette, surface_min=surface_min, size=borne)
            return _marquer_troncature(res, borne)

        raise _bad(_ops_error(_DPE_OPS))

    # --- declared GHG assessments (BEGES, ADEME) ---------------------------------

    @mcp.tool()
    def foncier_beges(
        siren: Optional[str] = None,
        naf: Optional[str] = None,
        annee: Optional[int] = None,
        departement: Optional[str] = None,
        obligee: Optional[bool] = None,
        limit: int = 100,
    ) -> dict:
        """Declared greenhouse-gas inventories (BEGES, ADEME open data), keyed by SIREN.

        ~11,800 published inventories, ~7,000 of them from organisations under the legal
        obligation (art. L229-25: companies over 500 staff, communes over 50,000
        inhabitants, the State). Each carries emissions per category, the reporting year,
        headcount band, and a link to the full report.

        WHY THIS EXISTS ALONGSIDE `foncier_conso_elec`. Consumption data is indexed by
        ADDRESS or by IRIS: it describes a SITE that still has to be resolved to a
        company, and it does not locate everything — on one métropole, 25 rows and
        10,621 MWh carry no usable address at all. Here the key IS the SIREN, so the
        inventory joins straight to the organisation. It reports DECLARED energy and
        emissions rather than metered consumption: a different fact, not a better one.

        ⚠️ The reporting year is not the publication year — an inventory published in
        2026 may cover 2015. `annee` filters on the reporting year, which is the one
        that makes two organisations comparable.

        ⚠️ A missing emission post is not a zero. Totals sum only what was declared, and
        each category carries `postes_declares` / `postes_absents` so a low total can be
        told apart from a partial declaration.

        Beyond emissions, each inventory carries `contact` (the declared energy
        officer — name, role, phone, e-mail; absent means MASKED at source, not
        unlisted), `entites_consolidees` (the SIREN of the declared consolidation
        perimeter — neither ownership nor directorships) and `electricite`, a MWh
        figure DERIVED from post 2.1 with the French average factor, marked
        `certitude: "infere"` and returned with that factor. It is the only public
        route to a consumption tied to a NAMED legal entity — grid data (Enedis by
        address, RTE by IRIS) is anonymous on both tiers.

        ⚠️ `total` is the number of inventories RETURNED, not how many exist: it
        saturates on `limit` (department 59 at limit=5 reports total 5, at limit=1000
        reports 504). When it does, `tronque` is true and `avertissement_troncature`
        says so — never read a saturated `total` as a count.

        Args:
            siren: 9 digits. The source stores it as a NUMBER, so 150 rows lost their
                leading zero — this is handled on both sides, pass the real SIREN.
            naf: a full code ("8610Z") or a division prefix ("86"), which then covers
                all its sub-classes.
            annee: reporting year.
            departement: INSEE department code.
            obligee: True keeps only organisations under the legal obligation.
            limit: max inventories RETURNED (default 100); `limit=-1` means no ceiling
                and lands on the source's hard cap of 10,000.
        """
        borne = _borne(limit)
        res = beges.bilans(siren=siren, naf=naf, annee=annee,
                           departement=departement, obligee=obligee, size=borne)
        return _marquer_troncature(res, borne)

    # --- MCP Apps: variants with a rendered interface (SEP-1865) ------------------
    # A few "flagship" *_app tools that return a UI (map + table) rendered
    # by the host (Claude.ai, sandboxed iframe) instead of raw JSON — useful when
    # the user wants to SEE a site summary / comparables.
    #
    # OPTIONAL import of prefab_ui (extra `fastmcp[apps]`): if it's missing (editable
    # venv not reinstalled), we simply do NOT register these tools — the JSON
    # tools above remain available (graceful degradation, same
    # principle as "if rendering fails, use the equivalent JSON tools").
    if not _PREFAB_UI_AVAILABLE:
        return

    # Curated labels for the known keys; otherwise we humanize the raw key,
    # which makes the renderers robust to the exact shape returned by the
    # france_opendata clients (no hard dependency on a field name).
    _LABELS = {
        "label": "Address", "score": "Geocoding score", "citycode": "INSEE code",
        "postcode": "Postcode", "city": "Commune", "lat": "Latitude",
        "lon": "Longitude", "idu": "Parcel ID", "commune": "Commune",
        "code_insee": "INSEE code", "section": "Section", "numero": "Number",
        "contenance_m2": "Area (m²)", "surface_bati_m2": "Built area (m²)",
        "surface_sol_m2": "Ground footprint (m²)", "ces_reel": "Actual CES",
        "nb_batiments": "Buildings", "hauteur_max_m": "Max height (m)",
        "usages": "Uses", "valeur_fonciere": "Price (€)", "surface": "Area (m²)",
        "surface_reelle_bati": "Built area (m²)", "prix_m2": "€/m²",
        "eur_m2": "€/m²", "date_mutation": "Date", "date": "Date",
        "adresse": "Address", "type_local": "Type", "distance_m": "Distance (m)",
        "annee": "Year", "year": "Year", "median": "Median €/m²",
        "mediane": "Median €/m²", "moyenne": "Mean €/m²", "mean": "Mean €/m²",
        "min": "Min €/m²", "max": "Max €/m²", "count": "Sales", "nb": "Sales",
    }

    def _label(k: str) -> str:
        return _LABELS.get(k) or str(k).replace("_", " ").capitalize()

    def _fmt(v: object) -> str:
        if isinstance(v, bool):
            return "yes" if v else "no"
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
            return _message_card("Address not found", f"No BAN result for « {adresse} ».")
        top = hits[0]
        lat, lon = top.get("lat"), top.get("lon")
        parcelle = cadastre.parcelle_at(lat, lon) if lat is not None and lon is not None else None
        bati = None
        if parcelle and parcelle.get("geometry"):
            try:
                bati = bdtopo.bati_parcelle(parcelle["geometry"], contenance_m2=parcelle.get("contenance_m2"))
            # noqa: SILENT — optional buildings layer on the site sheet
            except Exception:
                bati = None
        with Card() as card:
            with Column(gap=4):
                Heading(str(top.get("label") or adresse))
                _facts(_scalars(top))
                if parcelle:
                    Heading("Cadastral parcel")
                    _facts(_scalars(parcelle))
                else:
                    Text("No cadastral parcel at the geocoded point.")
                if bati and not bati.get("error"):
                    Heading("Existing buildings")
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
                _facts(_scalars(res))  # headline stats (local median, etc.)
                if records:
                    _table(records)
                else:
                    Text("No comparable sale found within the requested radius.")
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
                Heading(f"Price per m² — {code_commune}")
                _facts(_scalars(res))
                if per_year:
                    Heading("By year")
                    _table(per_year)
        return card
