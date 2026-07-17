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

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connector_verify


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
    def aiark_credits() -> dict:
        """Remaining AI Ark credits for the resolved account (`{"total": <int>}`)."""
        return _run(lambda c: c.credits())

    @mcp.tool()
    def aiark_company_search(
        account: Optional[dict] = None,
        lookalike_domains: Optional[list[str]] = None,
        lists: Optional[dict] = None,
        page: int = 0,
        size: int = 10,
    ) -> dict:
        """Search B2B companies by firmographics (AI Ark).

        Args:
            account: filter object. AI Ark nested DSL — each field takes an
                include/exclude matcher. Examples:
                - name: {"name": {"any": {"include": {"mode": "SMART", "content": ["Amazon"]}}}}
                - location: {"location": {"any": {"include": ["United States"]}}}
                - employee size: {"employeeSize": {"type": "RANGE", "range": [{"start": 1000, "end": 5000}]}}
                Combine keys in one object. Supports domain, website, industries,
                revenue, foundedYear, technologies, keywords, funding, naics…
            lookalike_domains: up to 5 company URLs to find similar companies.
            lists: exclude companies already in saved lists.
            page: zero-based page number. size: 0-100 (default 10).

        Returns the raw AI Ark page: `content[]` (company records with summary,
        link, contact, financial, location, technologies…), `totalElements`,
        `totalPages`, `pageable`. Cost: credits per returned record.
        """
        return _run(lambda c: c.search_companies(
            account=account, lists=lists,
            lookalike_domains=lookalike_domains, page=page, size=size,
        ))

    @mcp.tool()
    def aiark_people_search(
        account: Optional[dict] = None,
        contact: Optional[dict] = None,
        lists: Optional[dict] = None,
        page: int = 0,
        size: int = 10,
    ) -> dict:
        """Search B2B people by company + contact filters (AI Ark).

        Args:
            account: filters on the person's company (same DSL as
                aiark_company_search), e.g. {"domain": {"any": {"include": ["amazon.com"]}}}.
            contact: filters on the person, e.g.
                {"seniority": {"any": {"include": ["founder"]}}}. Supports title,
                department, seniority, location…
            lists: exclude people already in saved lists.
            page: zero-based page number. size: 0-100 (default 10).

        Returns the raw AI Ark page: `content[]` (people with profile, link,
        location, company, department, skills…), `totalElements`, `totalPages`,
        `trackId`. Note: search results do NOT include emails — use
        aiark_export_person to get an email for a specific match.
        """
        return _run(lambda c: c.search_people(
            account=account, contact=contact, lists=lists, page=page, size=size,
        ))

    @mcp.tool()
    def aiark_export_person(
        id: Optional[str] = None,
        url: Optional[str] = None,
    ) -> dict:
        """Export one person WITH email (synchronous email finder, AI Ark).

        Args:
            id: an AI Ark person id from a prior aiark_people_search result, OR…
            url: a LinkedIn profile URL. At least one of `id`/`url` is required.

        Returns the person profile with `email.output[]` (each: address, status,
        domainType), or `{"found": false}` if no profile/email was found.
        """
        if not id and not url:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="aiark_export_person exige `id` ou `url`.",
            ))
        result = _run(lambda c: c.export_person(id=id, url=url))
        if result is None:
            return {"found": False}
        return {"found": True, **result}

    @mcp.tool()
    def aiark_reverse_lookup(search: str) -> dict:
        """Reverse-lookup a person from a contact detail (AI Ark).

        Args:
            search: an email, phone number, or other contact info to resolve.

        Returns the full person profile, or `{"found": false}` if not found.
        """
        result = _run(lambda c: c.reverse_lookup(search))
        if result is None:
            return {"found": False}
        return {"found": True, **result}

    @mcp.tool()
    def aiark_mobile_phone(
        linkedin: Optional[str] = None,
        domain: Optional[str] = None,
        name: Optional[str] = None,
    ) -> dict:
        """Find a person's mobile phone number(s) (AI Ark).

        Args:
            linkedin: the person's LinkedIn profile URL (alone), OR…
            domain + name: the company domain AND the person's name (together).

        Returns `{"found": true, ...}` with `data` (list of phone numbers) and the
        matched id/linkedin, or `{"found": false}` if none.
        """
        if not linkedin and not (domain and name):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="aiark_mobile_phone exige `linkedin` OU (`domain` ET `name`).",
            ))
        result = _run(lambda c: c.mobile_phone(
            linkedin=linkedin, domain=domain, name=name))
        if result is None:
            return {"found": False}
        return {"found": True, **result}
