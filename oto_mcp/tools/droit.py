"""Info légale FR — jurisprudence, codes consolidés, conventions collectives.

Référence légale française (par opposition à l'identité entreprise, namespace `fr`) :
le DROIT applicable, pas les données d'une société. Trois namespaces sous une même
carte de connecteur (`droit` au registre `providers.py`) :

- `juris_*` — jurisprudence (fonds DILA Cass/CE + CEDH/CJUE/Judilibre live) ;
- `loi_*`   — codes consolidés versionnés (LEGI, texte en vigueur à une date) ;
- `ccn_*`   — conventions collectives de branche (KALI/DILA).

Toutes ces sources sont servies par le **service FOD** (`fod_juris`/`fod_loi`/`fod_ccn`
→ HTTP, `FOD_BASE_URL`), pas par un client lib en direct. Extraites du connecteur
`sirene`/`fr` (elles y étaient crammées sous « INSEE SIRENE », publisher trompeur).

Connecteur open-data : pas de credential. Gaté par activation DB (ADR 0010).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP


def register(mcp: FastMCP) -> None:
    # --- Conventions collectives (KALI, via service FOD) ---
    # Stock DILA complet (~290k articles, ~1,4k conteneurs) indexé FTS french +
    # filtre IDCC par france-opendata-service (#6). Complément de fr_accords_* :
    # ACCO = accords d'ENTREPRISE (qui a négocié quoi), KALI = le DROIT de la
    # BRANCHE (le texte applicable : minima, congés, primes, classifications).

    @mcp.tool()
    def ccn_search(
        query: str,
        idcc: Optional[str] = None,
        en_vigueur: bool = True,
        limit: int = 20,
        sort: str = "relevance",
    ) -> dict:
        """Search the full text of French collective agreements (conventions
        collectives, KALI/DILA): articles, avenants, salary schedules, extension
        orders.

        Args:
            query: Full-text query (websearch syntax: phrases in quotes, OR, -).
                French stemming applied ("congés payés" matches "congé payé").
            idcc: Restrict to one branch agreement (4-digit IDCC, ex "1285"
                spectacle vivant public, "3090" spectacle vivant privé). Use
                ccn_conventions or fr_search(idcc=…) to resolve an IDCC.
            en_vigueur: Only in-force article versions (default True — salary
                schedules exist in many superseded versions).
            limit: Max results (default 20, max 50).
            sort: "relevance" (FTS rank, default) | "recent" (date d'effet
                first — use for salary schedules where the latest avenant wins).

        Returns {count, articles: [{id, num, texte_titre, idcc, convention,
        extrait, source_url, …}]}. Fetch full text with ccn_get(id).
        """
        from .. import fod_ccn
        return fod_ccn.search(query, idcc=idcc, en_vigueur=en_vigueur,
                              limit=limit, sort=sort)

    @mcp.tool()
    def ccn_get(kali_id: str) -> dict:
        """Full consolidated text of a collective-agreement article (KALIARTI…),
        with its parent text (avenant/accord), convention (IDCC) and a
        verifiable Légifrance source_url.

        Args:
            kali_id: DILA article id returned by ccn_search (KALIARTI000…).
        """
        from .. import fod_ccn
        return fod_ccn.article(kali_id)

    @mcp.tool()
    def ccn_conventions(
        idcc: Optional[str] = None,
        query: Optional[str] = None,
        limit: int = 20,
    ) -> dict:
        """List French branch collective agreements (conventions collectives) by
        exact IDCC or title substring. Resolve "which convention is 3090?" or
        "conventions du spectacle" before searching articles.

        Args:
            idcc: Exact 4-digit IDCC.
            query: Title substring (ILIKE), ex "spectacle vivant".
            limit: Max results (default 20, max 100).
        """
        from .. import fod_ccn
        return fod_ccn.conventions(idcc=idcc, query=query, limit=limit)

    # --- Codes consolidés (LEGI, via service FOD) ---
    # 22 codes français AVEC versions historiques : l'article en vigueur à une
    # date donnée (une décision de 1992 cite l'art. 1128 CC → texte d'époque).

    @mcp.tool()
    def loi_article(code: str, num: str, date: Optional[str] = None) -> dict:
        """Consolidated text of a French code article, as in force at a given
        date. THE tool for citing law: exact text + verifiable Légifrance URL.

        Args:
            code: Short alias — CT (travail), CC (civil), CP (pénal), CSS
                (sécurité sociale), CCOM, CGI, CPI… (loi_codes lists all 22)
                — or a raw LEGITEXT id.
            num: Article number, ex "L1242-2", "1128", "R4228-20".
            date: YYYY-MM-DD — version in force AT THAT DATE (default: today).
                Use the date of the document citing the article: a 1992 ruling
                cites the 1992 wording, not today's.
        """
        from .. import fod_loi
        return fod_loi.article(code, num, date)

    @mcp.tool()
    def loi_versions(code: str, num: str) -> dict:
        """Full version timeline of a code article (every rewriting with dates
        and états). Use to see WHEN an article changed before picking a date
        for loi_article.

        Args:
            code: Short alias (CT, CC…) or LEGITEXT id.
            num: Article number.
        """
        from .. import fod_loi
        return fod_loi.versions(code, num)

    @mcp.tool()
    def loi_search(
        query: str,
        code: Optional[str] = None,
        en_vigueur: bool = True,
        limit: int = 20,
    ) -> dict:
        """Full-text search across French consolidated codes (LEGI). Find the
        article when you know the concept but not the number ("période d'essai
        CDD", "clause de non-concurrence").

        Args:
            query: Full-text query (websearch syntax, french stemming).
            code: Restrict to one code (alias CT/CC/… or LEGITEXT).
            en_vigueur: Only versions in force today (default True).
            limit: Max results (default 20, max 50).
        """
        from .. import fod_loi
        return fod_loi.search(query, code=code, en_vigueur=en_vigueur, limit=limit)

    @mcp.tool()
    def loi_codes() -> dict:
        """List the 22 French consolidated codes covered (alias → LEGITEXT +
        label). Discovery helper for loi_article/loi_search."""
        from .. import fod_loi
        return fod_loi.codes()

    # --- Jurisprudence (fonds DILA + CEDH/CJUE/live, via service FOD) ---
    # Cass (publiés + inédits), cours d'appel, CE/CAA/TA (bulk + live), Conseil
    # constit, CNIL, CEDH, CJUE, Judilibre. Tri pertinence × autorité
    # (constit/CEDH/CJUE > Cass/CE > CAA/CA > TA/TJ/CNIL).

    @mcp.tool()
    def juris_search(
        query: str,
        fond: Optional[str] = None,
        juridiction: Optional[str] = None,
        date_min: Optional[str] = None,
        date_max: Optional[str] = None,
        limit: int = 20,
        expand: bool = True,
    ) -> dict:
        """Search French & European case law (jurisprudence) full text — how
        courts actually ruled. Unified collections, ranked by FTS relevance ×
        court authority, with legal-thesaurus query expansion.

        Args:
            query: Full-text query (websearch syntax, french stemming), ex
                "requalification CDD d'usage intermittent".
            fond: Restrict to one collection — "cass" (Cour de cassation,
                published) | "inca" (cassation, unpublished) | "capp" (cours
                d'appel) | "jade" (administrative DILA: CE/CAA/TA) |
                "jade_live" (administrative, portail live) | "constit"
                (Conseil constitutionnel) | "cnil" | "cedh" (Cour EDH) |
                "cjue" (CJUE/Tribunal UE) | "judilibre" (Cass/CA/TJ live).
            juridiction: Court name filter (ILIKE), ex "cassation",
                "appel de Paris", "Conseil d'État".
            date_min / date_max: Decision date bounds (YYYY-MM-DD).
            limit: Max results (default 20, max 50).
            expand: Legal-thesaurus synonym expansion (default True — set
                False for strict literal matching).

        Returns {count, decisions: [{id, titre, juridiction, date_dec,
        solution, extrait, source_url, …}]}. Full text via juris_get(id).
        """
        from .. import fod_juris
        return fod_juris.search(query, fond=fond, juridiction=juridiction,
                                date_min=date_min, date_max=date_max,
                                limit=limit, expand=expand)

    @mcp.tool()
    def juris_get(decision_id: str) -> dict:
        """Full text of a French court decision, with metadata (juridiction,
        formation, solution, ECLI) and a verifiable Légifrance source_url.

        Args:
            decision_id: Id returned by juris_search (JURITEXT…, CETATEXT…,
                CONSTEXT…, CNILTEXT…).
        """
        from .. import fod_juris
        return fod_juris.decision(decision_id)
