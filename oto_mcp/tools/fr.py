"""French company data — identity, finances, legal events, tenders.

Open-data sources (no key): Recherche Entreprises API, INPI/BCE, BODACC, BOAMP.
Paid source (SIRENE key): INSEE SIRENE (SIRET, headquarters).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Literal, Optional, get_args

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import INVALID_PARAMS, ErrorData

from .. import access, output_projection, session_org
from .lecture import LECTURE
# Outside `tools/`: this module serves NO tool, it carries the reading of the
# people register. `tools/<m>.py` is reserved for modules mounted from
# the connector registry (guardrail `test_capabilities_drift`).
from .. import fr_registre
from france_opendata.erreurs import ParametreInvalide
# The agent reading of company records (projection, legal-form retry, CA-filter warning,
# multi-source profile, batch) lives in the shared library, used as is by every consumer
# — no local copy to drift. The NAF labels stay here: they come from oto-core.
from france_opendata import compact


# The REAL values of the BODACC `familleavis` field (taken from the france-opendata lib,
# `bodacc.py`). An unknown family used to return ZERO notices — read by the agent as
# "none of these companies had a modification" (oto#206, measured on "Modifications
# diverses", the LABEL the output serves in `famille`, copied back as input). The lib
# has refused it since 0.47.0; here, declared in the schema so the agent reads them BEFORE
# calling, and re-checked in the body: an internal call does not go through the schema.
FamilleBodacc = Literal["collective", "conciliation", "creation", "divers", "dpc",
                        "immatriculation", "modification", "radiation",
                        "retablissement_professionnel", "vente"]
_FAMILLES_BODACC = get_args(FamilleBodacc)


def _famille_bodacc(famille: Optional[str]) -> Optional[str]:
    """The BODACC family if it is allowed, `None` for all; otherwise a refusal that
    NAMES the allowed values — never a silent zero."""
    if famille is None or famille in _FAMILLES_BODACC:
        return famille
    raise McpError(ErrorData(code=INVALID_PARAMS, message=(
        f"Unknown BODACC family: {famille!r}. Allowed values: "
        f"{', '.join(_FAMILLES_BODACC)} (or omitted for all). The output gives a "
        "label (e.g. \"Modifications diverses\"): the input takes the code "
        "(`modification`).")))


_NAF_LABELS: dict[str, str] = {}


def _naf_labels() -> dict[str, str]:
    """NAF rév. 2 code → label, read once from the connector library's nomenclature.
    An empty table is a broken install, never "no label": it raises."""
    if not _NAF_LABELS:
        from oto.tools.sirene import load_naf_codes
        codes = load_naf_codes()
        if not codes:
            raise RuntimeError("NAF nomenclature missing from oto.tools.sirene "
                               "(data/naf_codes.txt): reinstall oto-core")
        _NAF_LABELS.update(codes)
    return _NAF_LABELS


def _depots_sans_donnees(notices: list[dict], dates_rendues: list) -> dict:
    """The accounts filings BODACC publishes (`dpc` notices) whose closing date is
    not among `dates_rendues` — filed, but absent from the usable INPI dataset
    (confidential, not keyed yet). Plus a warning when one closes after the most
    recent date returned: the figures served are then not the latest filed.

    Notices of other families are ignored, so the BODACC events of `fr_get`
    (all families mixed) go through the same reading."""
    rendues = {d for d in dates_rendues if d}
    depots: dict[str, dict] = {}
    for n in notices:
        if n.get("familleavis") != "dpc":
            continue
        depot = n.get("depot") if isinstance(n.get("depot"), dict) else {}
        cloture = depot.get("dateCloture")
        if not cloture or cloture in rendues or cloture in depots:
            continue
        depots[cloture] = {k: v for k, v in {
            "date_cloture": cloture,
            "date_parution": n.get("dateparution"),
            "type_depot": depot.get("typeDepot"),
            "descriptif": depot.get("descriptif"),
            "url_complete": n.get("url_complete"),
            "source": "bodacc",
        }.items() if v is not None}
    sans = sorted(depots.values(), key=lambda d: d["date_cloture"], reverse=True)
    out: dict = {"depots_sans_donnees": sans}
    derniere = max(rendues) if rendues else None
    posterieurs = [d["date_cloture"] for d in sans
                   if derniere is None or d["date_cloture"] > derniere]
    if posterieurs:
        out["avertissement"] = (
            f"{len(posterieurs)} accounts filing(s) published in BODACC close after "
            f"the most recent exercise with usable figures ({derniere or 'none'}), "
            f"the latest on {posterieurs[0]}: the company filed more recent accounts "
            "than the ones returned (confidential or not keyed in the dataset). "
            "Do not present these figures as its latest.")
    return out


def register(mcp: FastMCP) -> None:
    from ..fod import fr as fod_fr  # company data + keyed INSEE (passthrough) + BOAMP/ACCO index → FOD service

    # Open-data company data served by the dedicated FOD service (ADR 0028) — the
    # backend no longer runs these calls (including INPI DuckDB, a heavy workload) in-process.
    # Proxy objects with the same surface as the france_opendata clients → only these
    # bindings change, the tool bodies stay unchanged.
    entreprises = fod_fr.entreprises
    inpi = fod_fr.inpi
    bodacc = fod_fr.bodacc
    egapro = fod_fr.egapro

    # --- Identity (Recherche Entreprises API, open data) ---

    @mcp.tool(meta={"exhaustive_via": "fr_stock_search"}, annotations=LECTURE)
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
        nature_juridique: Optional[str] = None,
        page: int = 1,
        per_page: int = 25,
    ) -> dict:
        """Search French companies — returns identity, HQ, NAF, employees,
        directors, finances, matched establishments. At least one filter required.

        ⚠️ **Enumeration capped at ~10,000** (`page × per_page`, per_page ≤ 25):
        the API truncates **without error** beyond that. To enumerate a large
        set exhaustively ("all companies in sector X in region Y" when there are
        tens of thousands), switch to **`fr_stock_search`** (SIRENE parquet,
        no cap). This tool remains the right choice for searching/qualifying (indexed,
        fast, rich filters) as long as the result stays under ~10k.

        ⚠️ Geographic filters (departement, code_postal, commune) match ANY
        establishment, NOT only the head office (siège). To target companies whose
        SIÈGE is in a département, use `fr_stock_search(departement=…,
        sieges_only=True)`.

        ⚠️ **`ca_min`/`ca_max` do NOT qualify a company's size.** The
        filter applies to an amount whose unit varies by filing (euros or
        thousands, sometimes from one year to the next for the same company) and whose
        0 means "not declared" — so any upper bound brings back en masse
        companies whose revenue is unknown, which biases toward the LARGEST.
        Measured: `tranche_effectif_salarie=51,52,53 & ca_max=400000` returns 12
        results, all large companies, no true positive. To target by
        size, use `tranche_effectif_salarie` or `categorie_entreprise`.
        The `finances` block of the results carries the same caveats, flagged
        line by line (`alerte`) — see `finances_avertissement`.

        Spoken legal forms: people say "the SCI Untel", but the register rarely writes
        the form into the name — so a query like "SCI ASC" only matches companies
        literally NAMED that. On page 1, this tool detects such a prefix and ALSO runs
        the name alone filtered on that form, appending the extra hits (flagged
        `matched_by="legal_form"`) and reporting what it did under `legal_form_retry`.

        Args:
            query: Full-text search (company name, SIREN, brand…).
            naf: NAF activity codes, comma-separated (e.g. "62.01Z,62.02A").
            departement: Department code (e.g. "75").
            code_postal: Postal code (e.g. "75001").
            commune: INSEE commune code (COG, 5 digits — e.g. "67482" for
                Strasbourg). NOT a city name (a name raises "valeur non valide" — the API's French error message).
                For a place, pass `code_postal`, or use `fr_stock_search` (which
                resolves enseigne/commune by code too).
            employees: Employee-range codes (INSEE TEFEN) of the unité légale, comma-separated.
            categorie_entreprise: INSEE size category — "PME", "ETI" or "GE".
            ca_min: Minimum turnover — ⚠️ NOT reliably in euros, see below.
            ca_max: Maximum turnover — ⚠️ NOT reliably in euros, see below.
            idcc: IDCC codes (conventions collectives), comma-separated.
            nature_juridique: INSEE legal-form codes, comma-separated (e.g. "6540" for
                SCI, "5710" for SAS). Exact 4-digit codes only — an invalid value makes
                the API answer with the full list of valid ones.
            page: 1-based page number.
            per_page: Page size (max 25).
        """
        def _csv(v):
            return [x.strip() for x in v.split(",")] if v else None

        return compact.rechercher(
            entreprises, query=query, naf=_csv(naf), departement=departement,
            code_postal=code_postal, commune=commune, employees=_csv(employees),
            categorie_entreprise=categorie_entreprise, ca_min=ca_min, ca_max=ca_max,
            idcc=_csv(idcc), nature_juridique=_csv(nature_juridique),
            page=page, per_page=per_page, naf_labels=_naf_labels(),
        )

    @mcp.tool(annotations=LECTURE)
    def fr_get(siren: str | None = None, sirens: list | None = None) -> dict:
        """Full company profile by SIREN: identity (siège, directors, NAF code
        with its rév. 2 label `activite_principale_libelle`, employees) + 7 top financial ratios from the latest INPI/BCE filing
        + recent BODACC legal events. Aggregates 3 open data sources in parallel.
        Use this as first call when investigating a company.

        ⚠️ **`latest_bilan` = LITERALLY the last filed fiscal year, which does not
        necessarily carry revenue** — a simplified balance sheet has no
        "total revenue" box. For revenue, go back through the years: `fr_bilans(siren)`
        returns them from most recent to oldest with their `chiffre_d_affaires`, and you
        must take the first one that carries one. Do not conclude "no revenue"
        from `latest_bilan` alone. Seen on Norauto: last year
        (simplified) silent, €974,718,176 the year before.

        ⚠️ **`identity.etat_administratif` is the SIRENE register's state, which
        does NOT follow collective proceedings**: a company whose liquidation was
        closed years ago can still read "A". When the BODACC events read here
        (the 10 most recent) carry a judgment closing a liquidation,
        `procedure_collective_cloturee` gives it (nature, date) and
        `etat_administratif_avertissement` flags the contradiction. Older
        proceedings: `fr_events(siren, famille="collective")`.

        BATCH: pass `sirens=[…]` (max 20 per call, chunk beyond) to qualify a
        LIST in one call — returns `{profiles: […], count}`, one profile per
        SIREN in input order; per-SIREN failures degrade to `{error, siren}`
        without failing the batch. For bulk HQ addresses only (no financials),
        `fr_stock_enrich` is cheaper.

        `bilans_outline` surveys every usable exercise, newest first (closing date,
        type, confidentiality, `chiffre_d_affaires`, `alerte`): the place to look for
        the first year that carries a revenue.

        `latest_bilan` is trimmed to 7 B2B-relevant ratios (CA, résultat net,
        EBE, marge EBE, autonomie financière, taux d'endettement, liquidité).
        For the full ratio set, call `fr_bilan(siren, date_cloture)`.

        Resilient to per-source failures: a timeout or error on INPI (bilan) or
        BODACC (events) degrades gracefully — the available blocks are returned
        and the failing sources are listed under `partial_errors`. Only an
        identity failure (the keystone source) fails the whole call.

        Args:
            siren: SIREN number (9 digits) — single-company mode.
            sirens: list of SIREN numbers (max 20) — batch mode. Give one OR
                the other, not both.
        """
        if (siren is None) == (sirens is None):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="give `siren` (single) OR `sirens` (batch), not both"))
        sources = dict(entreprises=entreprises, inpi=inpi, bodacc=bodacc, naf_labels=_naf_labels())
        if sirens is None:
            return compact.profil_entreprise(str(siren).strip(), **sources)
        try:
            return compact.profils_entreprises(sirens, **sources)
        except ParametreInvalide as e:  # empty list, or over the batch bound
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    # Bound of the `fr_directors` batch: 5x that of `fr_get`, because a record there
    # costs ONE upstream call (the identity, from which we take directors AND legal
    # form) where an `fr_get` profile opens three to four. The field
    # qualifies in batches of a hundred (#612).
    _FR_DIRECTORS_BATCH_MAX = 100
    # Minimum spacing between two starts toward the upstream (5 per second). See the
    # batch comment: a prudent choice, the upstream quota not being published.
    _FR_DIRECTORS_CADENCE_S = float(os.environ.get("FR_DIRECTORS_CADENCE_S", "0.2"))

    @mcp.tool(annotations=LECTURE)
    def fr_directors(siren: str | None = None, sirens: list | None = None) -> dict:
        """Directors declared at the French registry (RNE), for one company or a
        LIST — `sirens=[…]` (max 100) returns `{entreprises, count, obtenues,
        en_echec, erreurs, not_found, synthese}`, one entry per SIREN in input
        order.

        ⚠️ An empty `dirigeants` has THREE meanings, told apart by `registre`:
        the SIREN is unknown (`error: "not_found"`), the legal form is not
        registered so it declares nobody (`hors_registre` — association,
        commune: the emptiness says nothing about the company), or the company
        is registered with nobody on file (`attendu`). Never read an empty list
        as "no director" without reading `registre`. `personnes_physiques`
        counts the NAMED natural persons — a company whose only director is
        another company scores 0.

        ⚠️ **`count` counts the records OBTAINED, not the lines returned** —
        `en_echec` and `erreurs` name what is missing, at the top level.
        ⚠️ **An `error` other than `not_found` is an upstream failure, not a fact
        about the company** — and it is RETRYABLE.

        A batch of 50 where 21 calls failed returns `count: 29`, `en_echec: 21`
        and names those 21 SIRENs in `erreurs` — at the top level, like
        `not_found`, because a hundred-line list is not re-read to find them.
        Reading `count` and `not_found` alone used to say "50 records, none
        missing" while 21 companies silently dropped out of the deliverable.
        The batch paces itself and retries the upstream quota (429, honouring
        `Retry-After`), so what reaches you has already been given a second
        chance; a SIREN still in `erreurs` says the upstream is busy, never that
        the company has no director. Ask for those SIRENs again, later or in a
        smaller batch.

        Args:
            siren: SIREN number (9 digits) — single-company mode.
            sirens: list of SIREN numbers (max 100) — batch mode. Give one OR
                the other, not both.
        """
        from concurrent.futures import ThreadPoolExecutor

        if (siren is None) == (sirens is None):
            raise McpError(ErrorData(code=INVALID_PARAMS, message="give `siren` (single) OR `sirens` (batch), not both"))
        if sirens is None:
            un = str(siren).strip()
            return fr_registre.fiche(un, entreprises.get_by_siren(un))
        cleaned = [str(s).strip() for s in sirens if str(s).strip()]
        if not cleaned:
            raise McpError(ErrorData(code=INVALID_PARAMS, message="`sirens` is empty"))
        if len(cleaned) > _FR_DIRECTORS_BATCH_MAX:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"`sirens` is limited to {_FR_DIRECTORS_BATCH_MAX} per call "
                        f"(received {len(cleaned)}) — split into batches"))

        # A CAP on in-flight calls does not bound the RATE: four requests that take over
        # from each other as soon as a slot frees up send as fast as the upstream answers. The
        # Recherche Entreprises quota is per IP — FOD's, shared by the whole
        # platform — so a batch of 50 triggered its own 429, while the same
        # SIRENs requested again in two goes passed without error (otomata-tech/oto#44).
        #
        # ⚠️ The value below is a PRUDENT choice, not a measurement: the quota
        # is not published. Observed landmark — a batch of 50 failed, batches of 10
        # and 11 passed; 5 starts per second stay well below. To be
        # adjusted if someone measures the real threshold, not by feel.
        cadence = _FR_DIRECTORS_CADENCE_S
        verrou, dernier_depart = threading.Lock(), [0.0]

        def _attendre_son_tour() -> None:
            with verrou:
                reste = cadence - (time.monotonic() - dernier_depart[0])
                if reste > 0:
                    time.sleep(reste)
                dernier_depart[0] = time.monotonic()

        def _one(s: str) -> dict:
            _attendre_son_tour()
            try:
                return fr_registre.fiche(s, entreprises.get_by_siren(s))
            # noqa: SILENT — the per-siren failure is rendered in the result row
            except Exception as exc:  # one failing SIREN does not bring down the batch
                return {"error": f"{type(exc).__name__}: {exc}", "siren": s}

        # 4 in flight, like the `fr_get` batch — but spread out (see above).
        with ThreadPoolExecutor(max_workers=4) as pool:
            fiches = list(pool.map(_one, cleaned))
        # An upstream failure is NOT a record. `count` used to be the number of rows
        # returned, failures included: a response of 50 where 21 had failed was
        # read as "50 records, none not found", and the 21 companies
        # disappeared from the deliverable or passed for "no director"
        # (otomata-tech/oto#44). The split is now explicit, and the failed SIRENs
        # are named TWICE — in their row and here — on a par
        # with the not-found ones: a list of a hundred records is not re-read to
        # find them.
        en_echec = [f["siren"] for f in fiches
                    if f.get("error") and f.get("error") != "not_found"]
        obtenues = [f for f in fiches if not f.get("error")]
        return {
            "entreprises": fiches,
            "count": len(obtenues),
            "obtenues": len(obtenues),
            "en_echec": len(en_echec),
            "erreurs": en_echec,
            "not_found": [f["siren"] for f in fiches if f.get("error") == "not_found"],
            "synthese": fr_registre.synthese(fiches),
        }

    # --- INSEE SIRENE (paid key — passthrough via FOD) ---
    # The backend resolves the key (vault: member/org BYO → platform key) + tracks the
    # quota, and PASSES it to FOD per call (ADR 0028/0037). The INSEE call runs on FOD;
    # the credential stays mastered in the backend vault, never stored on the FOD side.

    def _sirene_key() -> tuple[str, bool]:
        return access.resolve_api_key("sirene")  # (key, is_platform)

    @mcp.tool(annotations=LECTURE)
    def fr_siret(siret: str) -> dict:
        """Fetch a French establishment by SIRET (14 digits) from INSEE SIRENE.

        Args:
            siret: SIRET number (14 digits).
        """
        key, is_platform = _sirene_key()
        result = fod_fr.insee_siret(siret, key)
        if is_platform:
            session_org.note_call_trace(units=1)
        return result

    @mcp.tool(annotations=LECTURE)
    def fr_avis_sirene(siret: str) -> dict:
        """Official INSEE « Avis de situation au répertoire SIRENE » PDF of an
        establishment — the signed 1-page administrative document, for a dossier.

        Unlike `fr_siret` (JSON identity, needs the SIRENE key), this wraps INSEE's
        PUBLIC avis endpoint (no key) and returns a directly-fetchable URL to the PDF
        (availability is checked). The caller downloads the URL to get the file.

        Args:
            siret: SIRET number (14 digits ; spaces/dots are ignored).
        """
        import requests
        digits = "".join(c for c in str(siret) if c.isdigit())
        if len(digits) != 14:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"Invalid SIRET: {siret!r} — 14 digits expected.")))
        url = f"https://api-avis-situation-sirene.insee.fr/identification/pdf/{digits}"
        try:
            resp = requests.head(url, timeout=20)
        except requests.RequestException as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"INSEE avis-situation endpoint unreachable: {e}")))
        if resp.status_code == 404:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"no SIRENE avis for SIRET {digits} — establishment unknown "
                "to the directory (wrong SIRET?).")))
        if resp.status_code != 200 or "pdf" not in resp.headers.get("Content-Type", "").lower():
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"INSEE avis-situation answered HTTP {resp.status_code} "
                f"({resp.headers.get('Content-Type', '?')}).")))
        return {"siret": digits, "url": url, "format": "pdf"}

    @mcp.tool(annotations=LECTURE)
    def fr_headquarters(siren: str) -> Optional[dict]:
        """Fetch the headquarters (siège) of a company from INSEE SIRENE.

        Args:
            siren: SIREN number (9 digits).
        """
        key, is_platform = _sirene_key()
        result = fod_fr.insee_headquarters(siren, key)
        if is_platform:
            session_org.note_call_trace(units=1)
        return result

    # --- Finances (INPI/BCE, open data) ---

    # How many BODACC "dpc" notices `fr_bilans` reads to name the filings the
    # INPI dataset does not carry: one per filed year, so 20 covers two decades.
    _DPC_NOTICES_MAX = 20

    @mcp.tool(annotations=LECTURE)
    def fr_bilans(siren: str) -> dict:
        """List available INPI/BCE annual filings for a SIREN.

        Returns exercise dates, bilan type (C=complet, S=simplifié, K=consolidé),
        confidentiality status, and turnover. Typically 3-9 years of history.

        ⚠️ **`items` holds only the filings whose figures are USABLE** (the
        annual-accounts dataset): a confidential or not-yet-keyed filing is absent
        from it, so the first item is NOT necessarily the latest filed year.
        `depots_sans_donnees` names the filings published in BODACC (accounts
        filing notices, `dpc`) whose closing date is not in `items` — filed,
        but without usable figures here — and `avertissement` says when one of
        them closes AFTER the most recent item: the figures you hold are then
        older than the company's latest accounts. If BODACC fails, the
        cross-check is missing and `partial_errors` says so.

        Args:
            siren: SIREN number (9 digits).
        """
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as pool:
            f_items = pool.submit(inpi.list_exercises, siren)
            f_dpc = pool.submit(bodacc.search_by_siren, siren, "dpc", _DPC_NOTICES_MAX)
        items = f_items.result()
        out = {"siren": siren, "items": items, "total": len(items)}
        try:
            notices = f_dpc.result().get("results", [])
        # noqa: SILENT — the missing cross-check is rendered in partial_errors
        except Exception as exc:  # the INPI items stand on their own
            out["partial_errors"] = {"depots_sans_donnees": f"{type(exc).__name__}: {exc}"}
            return out
        out.update(_depots_sans_donnees(
            notices, [i.get("date_cloture_exercice") for i in items]))
        return out

    @mcp.tool(annotations=LECTURE)
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

    # --- Legal events (BODACC, open data) ---

    @mcp.tool(annotations=LECTURE)
    def fr_events(
        siren: str,
        famille: Optional[FamilleBodacc] = None,
        limit: int = 20,
    ) -> dict:
        """List BODACC legal events for a company: creations, modifications,
        sales, collective proceedings, annual filings.

        Args:
            siren: SIREN number (9 digits).
            famille: Filter by BODACC family code (the schema lists them, e.g.
                collective, modification, vente, dpc = dépôt des comptes). Output
                `familleavis_lib` is a label, not an input. None = all.
            limit: Max results (default 20).
        """
        return bodacc.search_by_siren(siren, famille=_famille_bodacc(famille), limit=limit)

    @mcp.tool(annotations=LECTURE)
    def fr_events_batch(
        sirens: list[str],
        famille: Optional[FamilleBodacc] = "collective",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> dict:
        """Check BODACC legal events for MANY companies at once (e.g. screen 700
        SIRENs for collective proceedings) — batched into a few upstream requests.

        Deterministic: returns a flat `annonces` list — one row per announcement
        AND per requested SIREN it names (a sale names both parties: requesting
        both gives two rows, same `bodacc_id`; a non-requested party never shows
        up). Each row's `partie` is that SIREN's role: sujet, ancien_proprietaire,
        ancien_exploitant, nouveau_titulaire, or indeterminee (the announcement
        does not say — do not guess). `synthese` counts on the requested SIRENs
        (sirens_avec_annonce, sirens_sans_annonce, par_partie, …; annonces_total =
        distinct announcements, lignes_total = rows).

        `texte` is the announcement's wording for EVERY family (jugement,
        modification or sale descriptif… — `texte_source` names the field);
        rows the source serves without it are counted in `annonces_sans_texte`.
        It does NOT decide whether a company is *in* proceedings, nor what a
        modification changed (a director or just the auditor): read `texte` and
        judge per SIREN — counting announcements is not a signal.

        Args:
            sirens: list of SIRENs (9 digits).
            famille: BODACC family CODE (the schema lists them). Default
                "collective" (procédures collectives); None = all families. The
                output `famille` is a LABEL ("Modifications diverses"), not an
                input: pass the code ("modification"). Unknown values are refused.
            date_from: earliest PUBLICATION date, inclusive (YYYY-MM-DD). None =
                no lower bound (the whole history comes back).
            date_to: latest publication date, inclusive (YYYY-MM-DD). A malformed
                date or date_from > date_to is refused; `synthese.periode` echoes
                the window the counts apply to. It filters on publication, not
                on the jugement date (`date_jugement`).
        """
        return bodacc.search_batch(sirens, famille=_famille_bodacc(famille),
                                   date_from=date_from, date_to=date_to)

    # --- Tenders (BOAMP, open data) ---

    @mcp.tool(annotations=LECTURE)
    def fr_tenders_search(
        op: Literal["notices", "awarded"] = "notices",
        query: Optional[str] = None,
        departement: Optional[str] = None,
        date_from: Optional[str] = None,
        descripteur: Optional[str] = None,
        date_to: Optional[str] = None,
        type_marche: Optional[str] = None,
        titulaire_siret: Optional[str] = None,
        acheteur_siret: Optional[str] = None,
        limit: int = 20,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """French public procurement — tender NOTICES (BOAMP) or AWARDED contracts (DECP).

        `op="notices"` (default): the notice — a need, a deadline.
        `op="awarded"`: the OUTCOME — winner SIRET, amount, notification date,
        duration, procedure. The only source that says who actually wins the
        contracts of a territory: the real competition, not the assumed one.

        Shared: `query` (text in the subject), `departement`, `date_from`. For
        `awarded`, `departement` is matched as the PREFIX of the place-of-performance
        code (a department "59" or a postcode — the code type varies per contract).
        Notices only: `descripteur`, `date_to`, `type_marche`. Awarded only:
        `titulaire_siret` (every contract won by that establishment, alone or in
        a consortium), `acheteur_siret` (every contract passed by that buyer). A
        parameter the chosen op cannot honour is REFUSED, never silently dropped.

        awarded reads two regimes split at the notification date — the 2022
        decree from 2024 on, the 2019 decree before — and each record carries its
        `arrete`. ⚠️ Since 2024 the source publishes NO names at all (buyer, place,
        winners); before 2024, never the main winner's. Resolve them from their
        SIRET with `fr_siret`: `nom`/`denomination: null` is the source's shape,
        not a gap.

        `fields` keeps only these keys in each record; the envelope (counts,
        pagination) always stays.

        Args:
            op: "notices" (BOAMP, default) or "awarded" (DECP).
            query: text searched in the subject.
            departement: department code; for awarded, a place-code prefix.
            date_from: start date (YYYY-MM-DD) — publication, or notification.
            descripteur: BOAMP descriptor (notices only).
            date_to: publication end date (notices only).
            type_marche: TRAVAUX, FOURNITURES, SERVICES (notices only).
            titulaire_siret: winner establishment (awarded only).
            acheteur_siret: buyer establishment (awarded only).
            limit: max results (1-100).
            fields: keys kept in each record.
        """
        propres = {
            "notices": {"descripteur": descripteur, "date_to": date_to, "type_marche": type_marche},
            "awarded": {"titulaire_siret": titulaire_siret, "acheteur_siret": acheteur_siret},
        }
        autre = "awarded" if op == "notices" else "notices"
        refuses = [nom for nom, v in propres[autre].items() if v]
        if refuses:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                f"op='{op}' does not accept {', '.join(refuses)} (reserved for op='{autre}')")))
        if op == "awarded":
            if not any([query, departement, titulaire_siret, acheteur_siret]):
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    "op='awarded' requires at least one criterion: query, departement, "
                    "titulaire_siret or acheteur_siret — 700,000 contracts without a filter "
                    "are not an answer")))
            res = fod_fr.search_decp(
                mot_cle=query, titulaire_siret=titulaire_siret, acheteur_siret=acheteur_siret,
                lieu=departement, depuis=date_from, limit=limit,
            )
            return output_projection.project(res, items_path="signaux", fields=fields)
        res = fod_fr.search_boamp(
            query=query, descripteur=descripteur, departement=departement,
            date_from=date_from, date_to=date_to, type_marche=type_marche, limit=limit,
        )
        return output_projection.project(res, items_path="results", fields=fields)

    @mcp.tool(annotations=LECTURE)
    def fr_tenders_get(idweb: str) -> dict:
        """Fetch a single BOAMP tender by its ID.

        Args:
            idweb: BOAMP notice identifier (e.g. "26-50647").
        """
        result = fod_fr.get_boamp(idweb)
        if result is None:
            return {"error": "not_found", "idweb": idweb}
        return result

    # --- Public aid for companies (data.aides-entreprises.fr, open data) ---

    @mcp.tool(annotations=LECTURE)
    def fr_aides_search(
        insee: Optional[str] = None,
        code_postal: Optional[str] = None,
        effectif: Optional[int] = None,
        nature: Optional[str] = None,
        echeance_avant: Optional[str] = None,
        q: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """Shortlist of French public aid (grants, loans, guarantees, calls for projects) for
        a company/project — data.aides-entreprises.fr database (State reference, ~2,400
        active aids, updated daily, the database prunes expired ones).

        Returns the DETERMINISTIC filter (geo by hierarchy commune→dept→region→
        France/EU + headcount band + nature + deadline) with the measured funnel
        (`funnel`). ⚠️ SECTOR relevance can NOT come from the database (its profile
        tagging is 99% over-inclusive) nor from lexical scoring: it is UP TO
        YOU to re-rank the shortlist by reading name/purpose. Anti-hallucination rule:
        keep only `id`s, then re-render each record via `fr_aides_get(id)` —
        NEVER rephrase name/purpose from memory, quote literally.

        Args:
            insee: INSEE commune code (preferred — e.g. "31555" Toulouse).
            code_postal: fallback when no INSEE code (best-effort resolution).
            effectif: number of employees (filters the bands; aids with no
                restriction stay).
            nature: substring of the aid type, matched against the French source values
                ("subvention", "prêt", "garantie", "avance", "exonération",
                "prestation"...).
            echeance_avant: YYYY-MM-DD — aids with a deadline closing before the date
                (call-for-projects watch; excludes permanent aids).
            q: lexical AND filter (coarse pre-filter, NOT a relevance sort).
            limit: records returned (default 50; `count` = filtered total).
            offset: pagination.
        """
        try:
            return fod_fr.search_aides(
                insee=insee, code_postal=code_postal, effectif=effectif, nature=nature,
                echeance_avant=echeance_avant, q=q, limit=limit, offset=offset,
            )
        except ValueError as e:  # commune/postcode unknown to the territories reference
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))

    @mcp.tool(annotations=LECTURE)
    def fr_aides_get(id_aid: str, raw: bool = False) -> dict:
        """COMPLETE record of an aid (source of truth after re-ranking
        `fr_aides_search` — full purpose/conditions/amount, funders,
        contacts, official sources). Decoded text (HTML entities cleaned) and
        `cache_indexation` reduced to its useful extracts (natures, funders,
        territories, contacts, sources).

        Args:
            id_aid: aid identifier (`id` field of fr_aides_search).
            raw: True = raw database record (not decoded, bulky;
                for a consumer that depends on it).
        """
        result = fod_fr.get_aide(id_aid, raw=raw)
        if result is None:
            return {"error": "not_found", "id_aid": id_aid}
        return result

    # --- Company agreements (ACCO, open data) ---
    # National database of collective agreements (DILA), agreements concluded since
    # 01/09/2017. Metadata: who (SIRET, company name, IDCC = collective
    # agreement), what (coded themes), when (date_texte), nature (initial ACCORD
    # vs AVENANT = renegotiation). The full text is not always published
    # (conforme_version_integrale), but the "who negotiated what and when" is.

    @mcp.tool(annotations=LECTURE)
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
        offset: int = 0,
        tranche_effectifs: Optional[list[str]] = None,
        categories_entreprise: Optional[list[str]] = None,
        exclude_categories: Optional[list[str]] = None,
        scan_cap: Optional[int] = None,
    ) -> dict:
        """Search French company collective agreements (accords d'entreprise, ACCO).

        Neutral primitive returning raw rows — compose your own need via filters,
        sort and per-company reduction.

        ⚠️ `themes` PROVES PRESENCE, NEVER ABSENCE. Theme codes come from DILA's
        own indexing of the filing: declarative, uneven, and demonstrably
        incomplete. Measured case — CAFES BIBAL VENDING (SIREN 345255087) filed
        the SAME kind of agreement twice: the 2019 one is coded 111/112, the
        2025 one is coded 081-084 only, yet its article 5 ("Régime de
        remboursement complémentaire de frais de santé et de prévoyance")
        re-institutes health AND pension for 4 years. Filtering on 111/112 there
        returns the 2019 act alone — reading "no recent act" off that is a FALSE
        NEGATIVE produced by the source, not by this tool. Health clauses
        routinely travel inside an agreement titled "égalité professionnelle" or
        "NAO", and get coded accordingly.

        Consequence for prospecting: to assert a company's scheme is DORMANT,
        theme codes are not enough — search by `siren` WITHOUT `themes`, then
        read the recent acts with `fr_accords_text` (health sections carry very
        stable headings: "frais de santé", "régime de prévoyance",
        "complémentaire santé"). Use `themes` to FIND candidates cheaply, the
        text to CONFIRM the ones you are about to act on.

        Common recipes:
        - Who just renegotiated their health/pension scheme (candidates, not an
          exhaustive set): themes=["111","112"], nature="AVENANT", sort_dir="desc".
        - Does THIS company have a health/pension agreement, and when: search by
          siren WITHOUT themes, sort_dir="desc", then read the recent acts.
        - PROSPECTING AUTONOMOUS SMEs (skip group subsidiaries, whose insurance is
          decided at HQ): exclude_categories=["GE"]. 26% of the companies filing a
          health/pension agreement are GE — filtering here is one query instead of
          a per-company qualification pass.

        Args:
            query: Substring in the agreement TITLE (ILIKE) — not in its text. The
                local index holds metadata only; body text is fetched per act by
                fr_accords_text, so a clause cannot be searched across the corpus.
            themes: Theme codes (OR). Health/pension: "111" (complémentaire santé),
                "112" (prévoyance), "113" (retraite supplémentaire). Use
                fr_accords_themes to discover codes. Presence-only — see the
                warning above before concluding anything from their ABSENCE.
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
            offset: Skip that many rows — page N = offset=(N-1)*limit. THE way to
                exhaust a result set bigger than `limit`: `total_count` tells you
                the volume, walk it with offset (do NOT slide `date_from`, which
                loses rows silently when more than `limit` share the same date).
            tranche_effectifs: INSEE employee-range codes (TEFEN) of the filing
                establishment, e.g. ["11","12","21"] — ACCO carries no company
                size, so this is resolved against the local SIRENE stock. Use it to
                keep SMEs only instead of post-filtering by hand.
            exclude_categories: drop these INSEE size categories ("PME"|"ETI"|"GE").
                THE way to skip group subsidiaries: the category is computed by
                INSEE over the GROUP perimeter, so a subsidiary that is small by
                its own headcount still reads "GE" (GTIE Rennes, €8M and a 20-49
                band, is a GE because it belongs to VINCI). No other field carries
                that. Resolved against the SIRENE legal-unit stock.
            categories_entreprise: keep ONLY these categories. ⚠️ NOT the mirror of
                exclude_categories: 4% of the companies filing an agreement have no
                category on record — an inclusion drops them (you cannot assert a
                company is an SME without knowing), an exclusion keeps them. To
                target autonomous SMEs, prefer exclude_categories=["GE"].
            scan_cap: how many agreements the SIRENE cross-check examines before
                paginating (default 5000, max 25000). `effectifs_filter.truncated`
                =true in the response means the pool is LARGER than this cap — the
                answer is then NOT exhaustive. Raise it to cover a whole pool
                (health/pension nationally is ~9700 agreements).
        """
        return fod_fr.search_acco(
            query=query, themes=themes, nature=nature, siren=siren, siret=siret,
            idcc=idcc, departement=departement, date_from=date_from, date_to=date_to,
            latest_per_siret=latest_per_siret, sort_by=sort_by, sort_dir=sort_dir,
            limit=limit, offset=offset, tranche_effectifs=tranche_effectifs,
            categories_entreprise=categories_entreprise,
            exclude_categories=exclude_categories, scan_cap=scan_cap,
        )

    @mcp.tool(annotations=LECTURE)
    def fr_accords_get(id_or_numero: str, include_text: bool = False) -> dict:
        """Fetch a single company agreement by its DILA id (ACCOTEXT…) or numero (T…).

        Returns METADATA (who, when, themes, branch…). The body text is NOT in the
        local index — it is fetched per act from Légifrance, so ask for it with
        `include_text=True` (or call `fr_accords_text`, which also paginates long
        agreements). Without that, this tool cannot tell you what an agreement
        SAYS — and what it says is often the point: theme codes are declarative
        and incomplete (cf. fr_accords_search), so a health clause is regularly
        found only by reading.

        Args:
            id_or_numero: DILA identifier (ACCOTEXT000…) or deposit number (T…).
            include_text: also fetch the full text (one Légifrance call). Long
                agreements come back truncated here — `texte_tronque`=true means
                use `fr_accords_text(acco_id, offset=…)` to walk the rest.
        """
        result = fod_fr.get_acco(id_or_numero)
        if result is None:
            return {"error": "not_found", "id_or_numero": id_or_numero}
        if not include_text:
            return result
        from ..fod import ccn as fod_ccn
        # The text is requested by DILA ID: the caller may have named the act by its
        # deposit number (T…), which Légifrance does not know.
        text = fod_ccn.accords_text(result.get("id") or id_or_numero)
        return {**result, "texte": text.get("texte"),
                "texte_chars": text.get("texte_chars"),
                "texte_tronque": text.get("tronque"),
                "next_offset": text.get("next_offset"),
                # Breaking FOD #335 (relayed #343): `permalien` (verifiable, honest
                # 404) + `lien_construit` (Légifrance, best-effort) replace
                # `source_url`, which has disappeared from this dataset.
                "permalien": text.get("permalien"),
                "lien_construit": text.get("lien_construit")}

    @mcp.tool(annotations=LECTURE)
    def fr_accords_themes() -> list[dict]:
        """List the agreement theme codes present in the database (code → label →
        count). Discovery helper so you can pick `themes` for fr_accords_search.

        The counts say how often DILA APPLIED a code, not how many agreements
        cover the topic — the indexing is declarative and misses clauses (see
        the warning on fr_accords_search)."""
        return fod_fr.acco_themes()

    @mcp.tool(annotations=LECTURE)
    def fr_accords_text(acco_id: str, offset: int = 0) -> dict:
        """Full text of a company agreement (accord d'entreprise) by its DILA
        id — fetched on demand from Légifrance (the local ACCO index only has
        metadata). Chain after fr_accords_search / fr_accords_get.

        Returns metadata + `texte` (extracted from the filed docx; may be empty
        when no integral version was published) + `texte_chars`/`offset`/
        `next_offset` + `permalien` (verifiable link, 404s honestly when the
        text is absent) + `lien_construit` (best-effort Légifrance pattern,
        not guaranteed to resolve).

        Args:
            acco_id: DILA id (ACCOTEXT000…) from fr_accords_search results.
            offset: start position in the text. Long agreements come back in
                chunks: when `tronque` is true, call again with
                offset=`next_offset` to get the rest (health/pension clauses and
                final provisions usually sit at the END of a merger agreement).
        """
        from ..fod import ccn as fod_ccn
        return fod_ccn.accords_text(acco_id, offset=offset)

    # Case law / codes / collective agreements (juris_*/loi_*/ccn_*) have
    # been extracted to the `droit` connector (tools/droit.py) — "FR legal
    # info" card, they were not INSEE. `fr_accords_*` stays here (company-scoped
    # by SIREN).

    # --- Gender-equality index (Egapro, open data) ---------------------------

    @mcp.tool(annotations=LECTURE)
    def fr_egapro_declaration(siren: str, year: Optional[int] = None) -> dict:
        """Gender-equality index (Egapro) declaration of a company, by SIREN.

        Every French company with 50+ employees must file an annual Egapro index.
        The payoff here is the **exact headcount** (`entreprise.effectif.total`) —
        where SIRENE only gives a bracket — plus the NAF code and per-indicator
        scores. Useful to qualify a lead's real size and confirm it is a 50+
        employer subject to social obligations.

        `year` omitted = most recent filing found (the API does not list a SIREN's
        years, so it scans back from the current year). Returns `{found: false}`
        when the company has no Egapro declaration (under 50 employees, or not filed).
        """
        decl = egapro.declaration(siren, year) if year else egapro.latest_declaration(siren)
        if decl is None:
            return {"found": False, "siren": siren,
                    "message": "No Egapro declaration (company with <50 employees, or not filed)."}
        return decl

