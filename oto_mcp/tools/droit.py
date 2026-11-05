"""French legal info — case law, consolidated codes, collective agreements.

French legal reference (as opposed to company identity, namespace `fr`):
the applicable LAW, not a company's data. Three namespaces under a single
connector card (`droit` in the registry, `providers/droit.py`):

- `juris_*` — case law (DILA Cass/CE collections + live CEDH/CJUE/Judilibre);
- `loi_*`   — versioned consolidated codes (LEGI, text in force at a given date);
- `ccn_*`   — sector-level collective agreements (KALI/DILA).

All these sources are served by the **FOD service** (`fod/juris`/`fod/loi`/`fod/ccn`
→ HTTP, `FOD_BASE_URL`), not by a direct lib client. Extracted from the `sirene`/`fr`
connector (they were crammed in there under the label of the time "INSEE SIRENE",
publisher "INSEE" — misleading for them; that label was fixed on 2026-09-02).

Open-data connector: no credential. Gated by DB activation (ADR 0010).

**Consolidated surface (ADR 0047 §Amendment, applied to the droit connector)**: one tool
per business OBJECT, the verb as an `op` parameter — 9 → 5 tools. Consolidation happens
**WITHIN each namespace, never across them**: `namespace_of` resolves on the prefix
DECLARED in the registry (`juris`/`loi`/`ccn`), so a `droit_*` tool — or a tool that
mixed two corpora — would fall outside the visibility/activation gate. Each
corpus keeps the same pair "object + scope resolver":

- `ccn_article` (op=search|get) + `ccn_conventions` (resolves the IDCC);
- `loi_article` (op=get|versions|search) + `loi_codes` (resolves the code alias);
- `juris_decision` (op=search|get) — its scope (`fond`) is a closed enum
  documented in the tool, hence no separate resolver.

The two resolvers stay ALONE: they return a CONTAINER (a KALI convention,
a LEGI code), not an article, and their `query` is a title substring (ILIKE), not
the French-stemming FTS query of the article tools — same word, different
semantics. Everything is READ-ONLY (open data): no op writes, deletes, or
consumes credit; each tool's default is therefore a risk-free read.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _need(value, name: str, op: str):
    """Argument required for THIS op — actionable error that NAMES the op and
    the argument, never a fallback (a legal citation drawn from a guessed
    argument is silently wrong)."""
    if value is None:
        raise _bad(f"op='{op}' requires {name}")
    return value


def register(mcp: FastMCP) -> None:
    # --- Conventions collectives (KALI, via service FOD) ---
    # Full DILA stock (~290k articles, ~1.4k containers) FTS-indexed french +
    # IDCC filter by france-opendata-service (#6). Complements fr_accords_*:
    # ACCO = COMPANY agreements (who negotiated what), KALI = the SECTOR's
    # LAW (the applicable text: minima, leave, bonuses, classifications).

    @mcp.tool()
    def ccn_article(
        op: Literal["search", "get"] = "search",
        query: Optional[str] = None,
        idcc: Optional[str] = None,
        en_vigueur: bool = True,
        limit: int = 20,
        sort: str = "relevance",
        kali_id: Optional[str] = None,
    ) -> dict:
        """An article of a French collective agreement (convention collective,
        KALI/DILA) — search the full text, or read one article in full.

        `op`:
        - **"search"** (default): search the full text of French collective
          agreements: articles, avenants, salary schedules, extension orders.
          Returns {count, articles: [{id, num, texte_titre, idcc, convention,
          extrait, permalien, lien_construit, …}]}. Fetch full text with op="get".
        - **"get"**: full consolidated text of a collective-agreement article
          (KALIARTI…), with its parent text (avenant/accord), convention (IDCC),
          a verifiable `permalien` and a best-effort Légifrance `lien_construit`.

        Args:
            op: search (default) | get.
            query: op="search" — full-text query (websearch syntax: phrases in
                quotes, OR, -). French stemming applied ("congés payés" matches
                "congé payé").
            idcc: op="search" — restrict to one branch agreement (4-digit IDCC,
                ex "1285" spectacle vivant public, "3090" spectacle vivant
                privé). Use ccn_conventions or fr_search(idcc=…) to resolve an
                IDCC.
            en_vigueur: op="search" — only in-force article versions (default
                True — salary schedules exist in many superseded versions).
            limit: op="search" — max results (default 20, max 50).
            sort: op="search" — "relevance" (FTS rank, default) | "recent" (date
                d'effet first — use for salary schedules where the latest avenant
                wins).
            kali_id: op="get" — DILA article id returned by op="search"
                (KALIARTI000…).
        """
        from ..fod import ccn as fod_ccn

        if op == "search":
            return fod_ccn.search(_need(query, "query", op), idcc=idcc,
                                  en_vigueur=en_vigueur, limit=limit, sort=sort)
        if op == "get":
            return fod_ccn.article(_need(kali_id, "kali_id", op))
        raise _bad("op must be 'search' or 'get'")

    @mcp.tool()
    def ccn_conventions(
        idcc: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """List French branch collective agreements (conventions collectives) by
        exact IDCC or title substring. Resolve "which convention is 3090?" or
        "conventions du spectacle" before searching articles with
        ccn_article(op="search").

        Args:
            idcc: Exact 4-digit IDCC.
            query: Title substring (ILIKE), ex "spectacle vivant". NOT the
                full-text query of ccn_article — this one matches the convention
                TITLE only.
            limit: Max results (default 20, max 100).
        """
        from ..fod import ccn as fod_ccn
        return fod_ccn.conventions(idcc=idcc, query=query, limit=limit)

    @mcp.tool()
    def ccn_rupture_conventionnelle(
        date_entree: str,
        date_signature: str,
        date_rupture_envisagee: str,
        indemnite_convenue: float,
        mois: list[dict],
        idcc: Optional[str] = None,
        jours_non_comptes_anciennete: int = 0,
        date_depot: Optional[str] = None,
        alsace_moselle: bool = False,
    ) -> dict:
        """Check a French mutual termination (rupture conventionnelle) BEFORE it is
        filed for approval: deterministic, nothing stored, nothing filed.

        Returns `verdict` (`conforme` | `non_conforme` | `incomplet`) and one entry
        per check in `controles` — reference salary, length of service, legal
        severance floor, collective-agreement floor, withdrawal period, filing
        date, earliest termination date — each with its figure, its status
        (`ok` | `ko` | `non_couvert` | `donnees_insuffisantes` | `info`) and the
        article it applies. `non_couvert` means the agreement's own floor is not
        coded yet: say it must be checked by hand, never report it as compliant.
        `telerc` lists the fields of the online filing (TéléRC, mandatory since
        April 2022 — no paper form to fill) in their order, for HR to copy as is;
        a `null` field could not be established and is filled in by hand.
        The figures come from the employee's payroll: read them with the payroll
        tools, never estimate them.

        Args:
            date_entree: Hiring date, YYYY-MM-DD.
            date_signature: Date the agreement is signed, YYYY-MM-DD.
            date_rupture_envisagee: Planned end of contract, YYYY-MM-DD.
            indemnite_convenue: Agreed severance, gross euros.
            mois: Gross pay month by month, at least the 12 months before the
                signature month: `{"mois": "YYYY-MM", "brut": 2400.0,
                "primes_exceptionnelles": 0, "neutralise": false}`.
                `primes_exceptionnelles` = the part of `brut` paid as an annual or
                one-off bonus. `neutralise: true` for a month whose pay dropped
                because of sick leave, maternity or birth leave, or any
                suspension — give the reason to nobody, it is health data — and
                add the months BEFORE it so 12 unneutralised months remain.
            idcc: Collective agreement IDCC of the employee (from payroll).
            jours_non_comptes_anciennete: Days of suspension excluded from length
                of service by law or agreement (default 0).
            date_depot: Planned filing date, YYYY-MM-DD (default: earliest).
            alsace_moselle: True for an establishment in Alsace-Moselle (Good
                Friday and 26 December are public holidays there).
        """
        from ..fod import rupture as fod_rupture
        dossier = {"date_entree": date_entree, "date_signature": date_signature,
                   "date_rupture_envisagee": date_rupture_envisagee,
                   "indemnite_convenue": indemnite_convenue, "mois": mois, "idcc": idcc,
                   "jours_non_comptes_anciennete": jours_non_comptes_anciennete,
                   "date_depot": date_depot, "alsace_moselle": alsace_moselle}
        return fod_rupture.controler({k: v for k, v in dossier.items() if v is not None})

    # --- Consolidated codes (LEGI, via FOD service) ---
    # 22 French codes WITH historical versions: the article in force at a
    # given date (a 1992 decision cites art. 1128 CC → text of that time).

    @mcp.tool()
    def loi_article(
        op: Literal["get", "versions", "search"] = "get",
        code: Optional[str] = None,
        num: Optional[str] = None,
        date: Optional[str] = None,
        query: Optional[str] = None,
        en_vigueur: bool = True,
        limit: int = 20,
    ) -> dict:
        """An article of a French consolidated code (LEGI) — its text as in force
        at a given date, its version timeline, or finding it by concept.

        THE tool for citing law: exact text + verifiable Légifrance URL.

        `op`:
        - **"get"** (default): consolidated text of a French code article
          (`code` + `num`), as in force at a given date.
        - **"versions"**: full version timeline of a code article (`code` +
          `num`) — every rewriting with dates and statuses. Use to see WHEN an
          article changed before picking a `date` for op="get".
        - **"search"**: full-text search across French consolidated codes (LEGI).
          Find the article when you know the concept but not the number ("période
          d'essai CDD", "clause de non-concurrence").

        Args:
            op: get (default) | versions | search.
            code: op="get"/"versions" (required) — short alias: CT (travail), CC
                (civil), CP (pénal), CSS (sécurité sociale), CCOM, CGI, CPI…
                (loi_codes lists all 22) — or a raw LEGITEXT id. op="search"
                (optional) — restrict to one code (alias CT/CC/… or LEGITEXT).
            num: op="get"/"versions" — article number, ex "L1242-2", "1128",
                "R4228-20".
            date: op="get" — YYYY-MM-DD, version in force AT THAT DATE (default:
                today). Use the date of the document citing the article: a 1992
                ruling cites the 1992 wording, not today's.
            query: op="search" — full-text query (websearch syntax, french
                stemming).
            en_vigueur: op="search" — only versions in force today (default True).
            limit: op="search" — max results (default 20, max 50).
        """
        from ..fod import loi as fod_loi

        if op == "get":
            return fod_loi.article(_need(code, "code", op),
                                   _need(num, "num", op), date)
        if op == "versions":
            return fod_loi.versions(_need(code, "code", op),
                                    _need(num, "num", op))
        if op == "search":
            return fod_loi.search(_need(query, "query", op), code=code,
                                  en_vigueur=en_vigueur, limit=limit)
        raise _bad("op must be 'get', 'versions' or 'search'")

    @mcp.tool()
    def loi_codes() -> dict:
        """List the 22 French consolidated codes covered (alias → LEGITEXT +
        label). Discovery helper for loi_article — both its `code` argument and
        its op="search" filter."""
        from ..fod import loi as fod_loi
        return fod_loi.codes()

    # --- Jurisprudence (fonds DILA + CEDH/CJUE/live, via service FOD) ---
    # Cass (published + unpublished), courts of appeal, CE/CAA/TA (bulk + live), Conseil
    # constit, CNIL, CEDH, CJUE, Judilibre. Ranking relevance × authority
    # (constit/CEDH/CJUE > Cass/CE > CAA/CA > TA/TJ/CNIL).

    @mcp.tool()
    def juris_decision(
        op: Literal["search", "get"] = "search",
        query: Optional[str] = None,
        fond: Optional[str] = None,
        juridiction: Optional[str] = None,
        date_min: Optional[str] = None,
        date_max: Optional[str] = None,
        limit: int = 20,
        expand: bool = True,
        decision_id: Optional[str] = None,
    ) -> dict:
        """A French or European court decision (jurisprudence) — search the
        collections full text, or read one decision in full.

        `op`:
        - **"search"** (default): search French & European case law
          (jurisprudence) full text — how courts actually ruled. Unified
          collections, ranked by FTS relevance × court authority, with
          legal-thesaurus query expansion. Returns {count, decisions: [{id,
          titre, juridiction, date_dec, solution, extrait, source_url, …}]}.
          Full text via op="get".
        - **"get"**: full text of a French court decision, with metadata
          (juridiction, formation, solution, ECLI) and a verifiable Légifrance
          source_url.

        Args:
            op: search (default) | get.
            query: op="search" — full-text query (websearch syntax, french
                stemming), ex "requalification CDD d'usage intermittent".
            fond: op="search" — restrict to one collection — "cass" (Cour de
                cassation, published) | "inca" (cassation, unpublished) | "capp"
                (cours d'appel) | "jade" (administrative DILA: CE/CAA/TA) |
                "jade_live" (administrative, portail live) | "constit"
                (Conseil constitutionnel) | "cnil" | "cedh" (Cour EDH) |
                "cjue" (CJUE/Tribunal UE) | "judilibre" (Cass/CA/TJ live).
            juridiction: op="search" — court name filter (ILIKE), ex "cassation",
                "appel de Paris", "Conseil d'État".
            date_min / date_max: op="search" — decision date bounds (YYYY-MM-DD).
            limit: op="search" — max results (default 20, max 50).
            expand: op="search" — legal-thesaurus synonym expansion (default True
                — set False for strict literal matching).
            decision_id: op="get" — id returned by op="search" (JURITEXT…,
                CETATEXT…, CONSTEXT…, CNILTEXT…).
        """
        from ..fod import juris as fod_juris

        if op == "search":
            return fod_juris.search(_need(query, "query", op), fond=fond,
                                    juridiction=juridiction, date_min=date_min,
                                    date_max=date_max, limit=limit, expand=expand)
        if op == "get":
            return fod_juris.decision(_need(decision_id, "decision_id", op))
        raise _bad("op must be 'search' or 'get'")
