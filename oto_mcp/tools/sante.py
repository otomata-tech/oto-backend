"""Health — healthcare & medico-social establishments (French open data, no key).

- **FINESS**: directory of establishments (healthcare + medico-social), data.gouv.
- **HAS ESSMS**: quality evaluations of ESSMS (HAS framework), read with DuckDB
  over remote parquet — requires the `france-opendata[sante]` extra.
- **`sante(op=…)`**: medicines (BDPM), clinical trials (ClinicalTrials.gov, CTIS),
  biomedical publications (PubMed, Europe PMC) and declared conflicts of interest
  (Transparence Santé), all read-only, served by the FOD service (`fod/sante`).

The namespace is not migrated to the `<ns>` / `<ns>_act` pair yet
(`tests/test_convention_act.A_MIGRER`): new sources land as ops of its reader `sante`,
never as new `sante_*` tools; the four FINESS / ESSMS tools wait for their own migration.

Open-data connector: no credential. Exposed only if activated in the DB
(activation notch, ADR 0010) — register_all gates on `connector_activation`.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastmcp import FastMCP
from mcp.types import INVALID_PARAMS, ErrorData

from .. import output_projection
from ..mcp_errors import McpError
from .lecture import LECTURE

#: The ops of the reader `sante`, all read-only.
OPS = ("search_medicines", "get_medicine",
       "search_clinicaltrials", "get_clinicaltrial", "search_ctis_trials", "get_ctis_trial",
       "search_pubmed", "get_pubmed_article", "search_europepmc", "get_europepmc_article",
       "search_interest_declarations", "interest_totals_by_company")

#: What a search's default view leaves out of each record (`full=True` keeps it): fields
#: that repeat the query, or that the matching get op returns. `(list key, dropped keys)`.
_VUE = {
    "search_medicines": ("medicaments", ("lien",)),
    "search_clinicaltrials": ("essais", ("titre_officiel", "collaborateurs", "pays_sites")),
    "search_ctis_trials": ("essais", ("type_promoteur", "domaines_therapeutiques")),
    "search_pubmed": ("articles", ("revue_abregee", "volume", "numero", "pages", "langues")),
    "search_europepmc": ("articles", ("base",)),
    "search_interest_declarations": ("declarations", ("periode", "publiee_le", "statut")),
}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument required for THIS op: a refusal that names the op and the argument."""
    if value is None or value == "":
        raise _bad(f"op='{op}' requires {name}")
    return value


def _found(record: Optional[dict], **cle: Any) -> dict:
    """An unknown identifier comes back `null` from FOD: say it, with the key asked."""
    return record if record is not None else {"found": False, **cle}


def register(mcp: FastMCP) -> None:
    from ..fod import sante as fod_sante

    # FINESS + HAS ESSMS (remote DuckDB) served by the FOD service (ADR 0028 B3) —
    # no more in-process execution. Proxies with the same surface as the france_opendata clients.
    finess = fod_sante.finess
    has = fod_sante.has

    # --- FINESS directory ----------------------------------------------------

    @mcp.tool()
    def sante_finess_search(
        q: str,
        departement: Optional[str] = None,
        categorie: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """Search FINESS establishments by name or FINESS code (health + medico-social).

        Returns {count, results} with FINESS ET/EJ, raison sociale, address,
        commune, category, SIRET, APE.

        Args:
            q: FINESS code (prefix) or free-text name (multi-word, accent-insensitive).
            departement: INSEE department code (e.g. "75").
            categorie: substring of the category label (e.g. "EHPAD", "Centre Hospitalier").
            limit: max results (default 20).
        """
        return finess.search(q, departement=departement, categorie=categorie, limit=limit)

    @mcp.tool()
    def sante_finess(finess: str) -> Optional[dict]:
        """FINESS establishment by exact code (ET or EJ), or null."""
        return finess.by_code(finess)

    # --- ESSMS quality evaluations (HAS) -------------------------------------

    @mcp.tool()
    def sante_essms_search(
        region_libelle: Optional[str] = None,
        departement_code: Optional[str] = None,
        secteur: Optional[str] = None,
        type_structure: Optional[str] = None,
        statut_juridique: Optional[str] = None,
        annee_min: Optional[int] = None,
        annee_max: Optional[int] = None,
        limit: int = 50,
    ) -> dict:
        """Search evaluated ESSMS (HAS quality evaluations, référentiel EDS).

        Returns {total, returned, results} (FINESS, raison sociale, region,
        category, sector, evaluation date, quality index). ⚠️ `region_libelle`
        values are UPPERCASE, no accents (e.g. "ILE DE FRANCE") — call
        `sante_essms_dimensions` to get the exact filter values.

        Args:
            region_libelle: region label (uppercase, no accents).
            departement_code: INSEE department code.
            secteur: "Social" | "Médico-social".
            type_structure: "Etablissement" | "Service".
            statut_juridique: legal status label.
            annee_min / annee_max: evaluation year band.
            limit: max results (default 50).
        """
        return has.search(
            region_libelle=region_libelle, departement_code=departement_code,
            secteur=secteur, type_structure=type_structure, statut_juridique=statut_juridique,
            annee_min=annee_min, annee_max=annee_max, limit=limit,
        )

    @mcp.tool()
    def sante_essms_dimensions() -> dict:
        """Distinct filter values for ESSMS evaluations (regions, sectors, types,
        legal statuses, categories, years) with counts — to build a valid
        `sante_essms_search` query."""
        return has.dimensions()

    # --- Medicines, trials, publications, conflicts of interest (FOD) ---------

    @mcp.tool(annotations=LECTURE)
    def sante(
        op: Literal[OPS],
        q: Optional[str] = None,
        cis: Optional[str] = None,
        marketed_only: bool = False,
        condition: Optional[str] = None,
        intervention: Optional[str] = None,
        sponsor: Optional[str] = None,
        product: Optional[str] = None,
        text: Optional[str] = None,
        country: Optional[str] = None,
        countries: Optional[list[str]] = None,
        status: Optional[list[str]] = None,
        nct: Optional[str] = None,
        eu_ct_number: Optional[str] = None,
        query: Optional[str] = None,
        sort: Optional[Literal["date", "relevance"]] = None,
        pmid: Optional[str] = None,
        doi: Optional[str] = None,
        company: Optional[str] = None,
        siren: Optional[str] = None,
        beneficiary: Optional[str] = None,
        rpps: Optional[str] = None,
        city: Optional[str] = None,
        department: Optional[str] = None,
        types: Optional[list[Literal["avantage", "convention", "remuneration"]]] = None,
        date_min: Optional[str] = None,
        date_max: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        page: Optional[int] = None,
        page_token: Optional[str] = None,
        cursor: Optional[str] = None,
        full: bool = False,
    ) -> dict:
        """Health data, read-only: French medicines, clinical trials, biomedical
        publications, declared conflicts of interest. Each answer carries `source`:
        cite its producer (and its update date for medicines). An unknown id
        returns `{"found": false}`.

        `op`:
        - **"search_medicines"** (`q`): French public medicines database (ANSM/HAS/
          Assurance maladie), by name or active substance; each hit has its `cis`.
        - **"get_medicine"** (`cis`): composition, packs (price, reimbursement), HAS
          SMR/ASMR opinions, prescribing conditions, generics, and `securite`: ANSM
          safety notices, shortages and supply tensions, major therapeutic interest.
        - **"search_clinicaltrials"**: ClinicalTrials.gov, at least one of `condition`,
          `intervention`, `sponsor`, `text`; sites in `country` (English name, default
          France, "" = worldwide). Next page: pass `page_suivante` as `page_token`.
        - **"get_clinicaltrial"** (`nct`): summary and every site in `country`.
        - **"search_ctis_trials"**: EU trials (CTIS), at least one of `text`,
          `condition`, `sponsor`, `product`; `countries` = ISO alpha-2 (default FR),
          `status` = portal codes "1".."12" (3 and 4 recruiting, 8 ended). CTIS is the
          EMA public portal's undocumented API: a 502 means it changed or is down.
        - **"get_ctis_trial"** (`eu_ct_number`, e.g. 2024-520051-24-00): products,
          status and sites in `country` (ISO alpha-2, default FR).
        - **"search_pubmed"** (`query`, PubMed syntax: `[mh]`, `[ad]`, `[dp]`): newest
          first, `sort="relevance"` otherwise. No abstract: get_europepmc_article has it.
        - **"get_pubmed_article"** (`pmid`).
        - **"search_europepmc"** (`query`, Europe PMC syntax: `AUTH:`, `AFF:`,
          `PUB_YEAR:`). A malformed query returns zero hits. Next page: pass
          `curseur_suivant` as `cursor`.
        - **"get_europepmc_article"** (`pmid` or `doi`): abstract and full-text links.
        - **"search_interest_declarations"**: Transparence Santé, gifts (`avantage`),
          agreements (`convention`) and fees (`remuneration`) paid by health companies
          to named beneficiaries, newest first; `offset + limit` <= 10000.
        - **"interest_totals_by_company"**: the same, summed per company and type; `suite`
          says more groups follow.
          Both need one of `company`, `siren`, `beneficiary`, `rpps`, `city`,
          `department`. NEVER add amounts of different types: a fee or gift can
          execute an agreement (`convention_liee`), the same money then counts twice.
          Personal data under licence CC BY-NC-SA 4.0, reuse restricted to
          transparency purposes, no strictly commercial use, not to be exposed to
          search engines. Any reuse must credit "Source : base Transparence - Santé
          (Direction Générale de la Santé), transparence.sante.gouv.fr, licence
          CC BY-NC-SA 4.0." (as given in `source.attribution`).

        Args:
            op: The read to run.
            q: search_medicines — words of the name or substance (min 3 chars).
            cis: get_medicine — 8-digit CIS code.
            marketed_only: search_medicines — only medicines on the market.
            condition: Trial searches — disease.
            intervention: search_clinicaltrials — drug, device or procedure.
            sponsor: Trial searches — sponsor (or collaborator).
            product: search_ctis_trials — medicinal product.
            text: Trial searches — free text.
            country: search_clinicaltrials / get_clinicaltrial — English name;
                get_ctis_trial — ISO alpha-2.
            countries: search_ctis_trials — ISO alpha-2 codes.
            status: search_clinicaltrials — RECRUITING, NOT_YET_RECRUITING,
                COMPLETED…; search_ctis_trials — codes "1" to "12".
            nct: get_clinicaltrial — e.g. NCT03958240.
            eu_ct_number: get_ctis_trial — EU CT number.
            query: search_pubmed / search_europepmc.
            sort: search_pubmed — "date" (default) or "relevance".
            pmid: get_pubmed_article / get_europepmc_article.
            doi: get_europepmc_article.
            company: Interest ops — declaring company name (words).
            siren: Interest ops — declaring company SIREN (9 digits).
            beneficiary: Interest ops — beneficiary last (and first) name.
            rpps: Interest ops — 11-digit RPPS of the professional.
            city: Interest ops — beneficiary city.
            department: Interest ops — beneficiary department (75, 2A, 974).
            types: Interest ops — avantage, convention, remuneration.
            date_min: Interest ops — YYYY-MM-DD.
            date_max: Interest ops — YYYY-MM-DD.
            limit: Searches — max results (service default 20, 50 for totals).
            offset: search_pubmed / interest ops — results to skip.
            page: search_ctis_trials — page number, from 1.
            page_token: search_clinicaltrials — `page_suivante` of the previous page.
            cursor: search_europepmc — `curseur_suivant` of the previous page.
            full: Searches — every field of each record (default: the fields the
                matching get op repeats are left out).
        """
        from ..fod import sante as fod

        filtres = {"entreprise": company, "siren": siren, "beneficiaire": beneficiary,
                   "rpps": rpps, "ville": city, "departement": department, "types": types,
                   "date_min": date_min, "date_max": date_max}
        if op == "search_medicines":
            r = fod.medicaments.search(_need(q, "q", op), commercialises=marketed_only or None,
                                       limit=limit)
        elif op == "get_medicine":
            return _found(fod.medicaments.fiche(_need(cis, "cis", op)), cis=cis)
        elif op == "search_clinicaltrials":
            r = fod.essais.clinicaltrials(condition=condition, intervention=intervention,
                                          promoteur=sponsor, terme=text, pays=country,
                                          statut=status, limit=limit, page_token=page_token)
        elif op == "get_clinicaltrial":
            return _found(fod.essais.clinicaltrial(_need(nct, "nct", op), pays=country), nct=nct)
        elif op == "search_ctis_trials":
            try:
                codes = None if status is None else [int(s) for s in status]
            except ValueError:
                raise _bad(f"op='{op}': status takes the portal codes '1' to '12'") from None
            r = fod.essais.ctis(texte=text, condition=condition, promoteur=sponsor,
                                produit=product, pays=countries, statut=codes, page=page,
                                limit=limit)
        elif op == "get_ctis_trial":
            return _found(fod.essais.ctis_essai(_need(eu_ct_number, "eu_ct_number", op),
                                                pays=country), eu_ct_number=eu_ct_number)
        elif op == "search_pubmed":
            r = fod.publications.pubmed(_need(query, "query", op), limit=limit, offset=offset,
                                        tri={"relevance": "pertinence"}.get(sort, sort))
        elif op == "get_pubmed_article":
            return _found(fod.publications.pubmed_article(_need(pmid, "pmid", op)), pmid=pmid)
        elif op == "search_europepmc":
            r = fod.publications.europepmc(_need(query, "query", op), limit=limit,
                                           curseur=cursor)
        elif op == "get_europepmc_article":
            if not (pmid or doi):
                raise _bad(f"op='{op}' requires pmid or doi")
            return _found(fod.publications.europepmc_article(pmid=pmid, doi=doi),
                          **({"pmid": pmid} if pmid else {"doi": doi}))
        elif op == "search_interest_declarations":
            r = fod.transparence.declarations(limit=limit, offset=offset, **filtres)
        elif op == "interest_totals_by_company":
            return fod.transparence.par_entreprise(limit=limit, offset=offset, **filtres)
        else:
            raise _bad(f"op must be one of {', '.join(OPS)}")
        if full:
            return r
        cle, retires = _VUE[op]
        return output_projection.project(r, items_path=cle, item_drop=retires)
