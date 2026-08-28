"""AI Ark — recherche société/personne B2B + enrichissement contact (LinkedIn).

Connecteur classique (kind="tools", ex-mount #152→#160) sur l'API REST synchrone
d'AI Ark (docs.ai-ark.com). Contrat LLM curé ici ; le client HTTP vit dans oto-core
(`oto.tools.aiark.client.AiArkClient`). Cascade de clé standard
(`resolve_api_key("aiark")` : BYO user/org > grant plateforme + quota) → mode
plateforme possible via `record_platform_usage`.

v1 = endpoints SYNCHRONES seulement. Les exports/find-emails EN LOT d'AI Ark
répondent par webhook (async) → hors périmètre (itération suivante).
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, output_projection
from ..connectors import verify as connector_verify

# ── Vue de tri d'une page de recherche ───────────────────────────────────────────
# Une page `size=100` rendait 2,8 à 3,2 M de caractères : au-delà du plafond d'un tool
# result, donc un déversement en fichier + un `jq` à CHAQUE page (11 pages sur un seul run
# de sourcing). Et un agent sans shell — client MCP nu, n8n — n'a pas cette échappatoire.
#
# DENYLIST de clés nommées, jamais une allowlist : une allowlist a déjà fait disparaître
# `liste_idcc` en silence sur `fr_get` (champ « IDCC vérifié » resté vide sur 500 lignes).
# Une denylist ne peut escamoter qu'un champ qu'on a écrit ; un champ neuf d'AI Ark passe.
#
# Mesuré le 14/08 sur un enregistrement réel (~12 000 c.) : `position_groups` en pèse à lui
# seul plus de la moitié — la description COMPLÈTE de chaque société où la personne est
# passée, jusqu'au stage de 2010. Ce que le sourcing lit tient dans `profile` (nom, titre,
# headline), `location`, `link`, `department.seniority` et l'identité de la société.
_PERSON_DROP = ("educations", "volunteer_experiences", "awards", "skills", "languages",
                "member_badges", "statistics", "position_groups")
# `company` est GARDÉ (le sourcing en a besoin) mais délesté : ces blocs sont répétés à
# l'identique sur chacune des 100 personnes d'une même société.
_COMPANY_DROP = ("technologies", "keywords", "naics", "industries", "languages",
                 "last_updated")

# ── Filtres qu'AI Ark ACCEPTE et n'applique JAMAIS ───────────────────────────────
# Une clé de filtre non indexée ne fait pas 400 : l'API l'avale et rend la base ENTIÈRE,
# triée par effectif. L'agent lit alors 72 M de sociétés en croyant lire son résultat
# filtré — un faux, pas un échec. Vécu le 14/08 (`account.website` = finecobank.com →
# `totalElements` 72 343 404, Tata Group et Amazon en tête de « FinecoBank »).
#
# Vérifié par DIFFÉRENTIEL le 15/08/2026 (même requête `size=1` avec et sans la clé) :
#   account.website → 72 343 404 dans les DEUX formes (liste nue ET wrapper SMART),
#                     là où `account.domain` en liste nue rend 3.
#   contact.title   → 8 191 335 avec et sans, au même premier id.
# D'où le refus plutôt que l'avertissement : sur un filtre mort, tout ce qui revient est
# faux, et rien dans la réponse ne le signale.
_DEAD_FILTERS: dict[str, dict[str, str]] = {
    "account": {
        "website": 'domain, en liste nue : {"domain": {"any": {"include": '
                   '["exemple.com"]}}} (le wrapper {"mode": "SMART", "content": […]} '
                   "ne vaut que pour `name`)",
    },
    "contact": {
        "title": "seniority (+ location), puis un tri des titres CÔTÉ CLIENT sur les "
                 "pages rendues — AI Ark n'indexe pas l'intitulé de poste",
    },
}


def _reject_dead_filters(**blocks) -> None:
    """Refuse un filtre qu'AI Ark accepterait sans l'appliquer (cf. `_DEAD_FILTERS`)."""
    for block, value in blocks.items():
        for field, remedy in _DEAD_FILTERS.get(block, {}).items():
            if isinstance(value, dict) and field in value:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                    f"Filtre `{block}.{field}` : AI Ark l'accepte et ne l'applique "
                    f"PAS — la recherche rendrait la base entière en la faisant passer "
                    f"pour un résultat filtré (vérifié par différentiel). "
                    f"À la place : {remedy}.")))
# URLs d'images : un agent ne les regarde pas.
_PROFILE_DROP = ("picture", "background")


def _slim_company(company: object) -> object:
    if not isinstance(company, dict):
        return company
    out = {k: v for k, v in company.items() if k not in _COMPANY_DROP}
    loc = out.get("location")
    if isinstance(loc, dict) and "headquarter" in loc:
        # `locations[]` reprend le siège et ses annexes ; le siège suffit à situer.
        out["location"] = {"headquarter": loc["headquarter"]}
    return out


def _slim_person(row: object) -> object:
    if not isinstance(row, dict):
        return row
    out = {k: v for k, v in row.items() if k not in _PERSON_DROP}
    if isinstance(prof := out.get("profile"), dict):
        out["profile"] = {k: v for k, v in prof.items() if k not in _PROFILE_DROP}
    if "company" in out:
        out["company"] = _slim_company(out["company"])
    return out


def _shape(payload: object, op: str, full: bool, fields: Optional[list[str]]) -> object:
    """Page AI Ark resserrée. `full=True` = la page brute, `fields=[…]` = ces clés seules.

    Le nom porte l'intention (ADR 0047) : `full=True` dit ce qu'on obtient, là où
    `compact=False` se lisait comme une double négation. Et le défaut RESSERRE : une
    économie qu'il faut connaître pour en bénéficier ne bénéficie à personne — mesuré,
    aucun agent branché en direct ne passait l'opt-in.

    L'enveloppe (`totalElements`, `totalPages`, `trackId`, la pagination) est intacte :
    sans elle l'agent croit avoir tout vu."""
    if full or not isinstance(payload, dict):
        return payload
    rows = payload.get("content")
    if not isinstance(rows, list):
        return payload
    slim = _slim_person if op == "people" else _slim_company
    out = dict(payload)
    out["content"] = [slim(r) for r in rows]
    if fields:
        out = output_projection.project(out, items_path="content", fields=fields)
    return out


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001 (config: contrat de sonde, non utilisé ici)
    """Sonde « tester la connexion » : la clé authentifie-t-elle vraiment ?

    `verify_key()` (oto-core) fait un GET crédits sans effet de bord — 401 sur
    clé invalide. Lève — le message remonte tel quel à l'UI.
    """
    from oto.tools.aiark.client import AiArkClient
    AiArkClient(api_key=fields["key"]).verify_key()


def register(mcp: FastMCP) -> None:
    from oto.tools.aiark.client import AiArkClient

    connector_verify.register("aiark", _verify)

    def _client() -> tuple[AiArkClient, bool]:
        key, is_platform = access.resolve_api_key("aiark")
        return AiArkClient(api_key=key), is_platform

    def _run(fn):
        """Exécute un appel AI Ark : traduit une erreur HTTP en McpError
        actionnable (5xx amont = réessayer ; sinon entrée invalide) et compte
        l'usage plateforme sur succès."""
        client, is_platform = _client()
        try:
            result = fn(client)
        except McpError:
            raise
        except Exception as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status and status >= 500:
                msg = ("AI Ark est momentanément indisponible (erreur serveur "
                       f"{status}). Réessaie dans un moment — ce n'est pas ton entrée.")
            elif status == 401:
                msg = "Clé AI Ark invalide ou révoquée (401). Vérifie la clé posée."
            else:
                msg = f"AI Ark n'a pas pu traiter la requête ({e})."
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        if is_platform:
            access.record_platform_usage("aiark")
        return result

    @mcp.tool()
    def linkedin_aiark_credits() -> dict:
        """Remaining AI Ark credits for the resolved account (`{"total": <int>}`).

        ⚠️ On the PLATFORM key (credits paid by oto), the balance is not yours to
        read — the call is refused rather than showing someone else's pool. Bring
        your own AI Ark key to see a balance.
        """
        _, is_platform = _client()
        if is_platform:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=(
                "Ces crédits AI Ark sont fournis par oto : leur solde ne t'est pas "
                "exposé. Pose ta propre clé AI Ark pour suivre un solde.")))
        return _run(lambda c: c.credits())

    @mcp.tool()
    def linkedin_aiark_search(
        op: Literal["people", "companies"] = "people",
        account: Optional[dict] = None,
        contact: Optional[dict] = None,
        lookalike_domains: Optional[list[str]] = None,
        lists: Optional[dict] = None,
        page: int = 0,
        size: int = 10,
        full: bool = False,
        fields: Optional[list[str]] = None,
    ) -> dict:
        """Search LinkedIn-sourced B2B data through AI Ark (bought data, per-credit).

        Not interchangeable with `linkedin_search`: that one drives YOUR
        connected LinkedIn session (and is rate-limited by LinkedIn); this one
        queries AI Ark's index and BILLS CREDITS per returned record.

        `op`:
        - **"people"** (default): people by company + contact filters. Results do
          NOT include emails — use `linkedin_aiark_person(op="export")` for one.
        - **"companies"**: companies by firmographics.

        Args:
            op: "people" (default) | "companies".
            account: filters on the company. AI Ark nested DSL — each field takes an
                include/exclude matcher. Examples:
                - name: {"name": {"any": {"include": {"mode": "SMART", "content": ["Amazon"]}}}}
                - location: {"location": {"any": {"include": ["United States"]}}}
                - employee size: {"employeeSize": {"type": "RANGE", "range": [{"start": 1000, "end": 5000}]}}
                - a company's site: {"domain": {"any": {"include": ["example.com"]}}}
                  — plain list, NOT the SMART wrapper (that one is for `name` only).
                Combine keys in one object. Supports domain, industries, revenue,
                foundedYear, technologies, keywords, funding, naics…
                ⚠️ `website` is REFUSED here: AI Ark accepts it and silently ignores
                it, returning the whole 72 M database as if it were your filtered
                result. Filter on `domain` instead.
            contact: op="people" — filters on the person, e.g.
                {"seniority": {"any": {"include": ["founder"]}}}. Supports seniority,
                location, department…
                ⚠️ `title` is REFUSED for the same reason (job titles are not indexed):
                filter on `seniority`, then sort titles client-side on the pages you
                fetched.
            lookalike_domains: op="companies" — up to 5 company URLs to find similar ones.
            lists: exclude records already in saved lists.
            page: zero-based page number. size: 0-100 (default 10).
            full: True = the RAW AI Ark record, every block. The DEFAULT is a sourcing
                view that drops what sourcing never reads — past positions with the
                full write-up of every company worked at, education, volunteering,
                awards, skills, badges, statistics, languages, image URLs — plus the
                company blocks repeated identically on all 100 people of one firm.
                A `size=100` page returned ~3 M characters, past any tool-result cap.
            fields: keep ONLY these keys on each record; the envelope (totals,
                pagination, trackId) always stays — without it you would think you
                saw everything. Combine with `full=True` to project the raw record.

        Returns the AI Ark page: `content[]`, `totalElements`, `totalPages`.
        """
        _reject_dead_filters(account=account, contact=contact)
        if op == "people":
            result = _run(lambda c: c.search_people(
                account=account, contact=contact, lists=lists, page=page, size=size))
        elif op == "companies":
            result = _run(lambda c: c.search_companies(
                account=account, lists=lists,
                lookalike_domains=lookalike_domains, page=page, size=size))
        else:
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message="op doit être 'people' ou 'companies'"))
        return _shape(result, op, full, fields)

    @mcp.tool()
    def linkedin_aiark_person(
        op: Literal["export", "reverse", "mobile"] = "export",
        id: Optional[str] = None,
        url: Optional[str] = None,
        search: Optional[str] = None,
        linkedin: Optional[str] = None,
        domain: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict:
        """Resolve ONE person through AI Ark (bought data, per-credit).

        `op`:
        - **"export"** (default): the person WITH their email (synchronous email
          finder). Give `id` (from `linkedin_aiark_search(op="people")`) OR `url`
          (a LinkedIn profile URL).
        - **"reverse"**: find the person FROM a contact detail (`search` = an email,
          a phone number…).
        - **"mobile"**: their mobile phone number(s). Give `linkedin` (profile URL)
          alone, OR `domain` AND `name` together.

        Every op returns `{"found": false}` rather than an error when nothing
        matches — an absence is a result, not a failure.

        Args:
            op: export (default) | reverse | mobile.
            id: op="export" — an AI Ark person id from a prior search.
            url: op="export" — a LinkedIn profile URL.
            search: op="reverse" — the contact detail to resolve.
            linkedin: op="mobile" — the person's LinkedIn profile URL.
            domain: op="mobile" — the company domain (with `name`).
            name: op="mobile" — the person's name (with `domain`).
        """
        def _need(cond: bool, msg: str) -> None:
            if not cond:
                raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))

        if op == "export":
            _need(bool(id or url), "op='export' exige `id` ou `url`.")
            result = _run(lambda c: c.export_person(id=id, url=url))
        elif op == "reverse":
            _need(bool(search), "op='reverse' exige `search`.")
            result = _run(lambda c: c.reverse_lookup(search))
        elif op == "mobile":
            _need(bool(linkedin) or bool(domain and name),
                  "op='mobile' exige `linkedin` OU (`domain` ET `name`).")
            result = _run(lambda c: c.mobile_phone(
                linkedin=linkedin, domain=domain, name=name))
        else:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="op doit être 'export', 'reverse' ou 'mobile'"))

        if result is None:
            return {"found": False}
        return {"found": True, **result}
