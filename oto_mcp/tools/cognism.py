"""Cognism — recherche société/personne B2B + reveal (email/téléphone) +
enrichissement par identité.

Connecteur classique (`kind="tools"`) sur l'API REST synchrone de Cognism
(developers.cognism.com). Contrat LLM curé ici ; le client HTTP vit dans
oto-core (`oto.tools.cognism.client.CognismClient`). Cascade de clé standard
(`resolve_api_key("cognism")` : BYO user > BYO org) — pas de mode plateforme
(clé partagée à l'échelle d'un org via BYO org, pas un grant Otomata).

La DSL de filtre (`filters`) est un dict passé quasi tel quel à Cognism — trop
large (~150 champs, imbrication profonde) pour être modélisée champ par champ
côté tool. Référence complète : guide `cognism-filters` (`oto_guide`,
op=read, slug="cognism-filters"). Les champs à valeurs FERMÉES sont validés
côté client AVANT l'appel réseau (typo d'enum → erreur explicite, pas une
page vide silencieuse).
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.cognism.client import CognismClient

    def _client() -> tuple[CognismClient, bool]:
        key, is_platform = access.resolve_api_key("cognism")
        return CognismClient(api_key=key), is_platform

    def _run(fn):
        """Exécute un appel Cognism : traduit une erreur en McpError actionnable
        (ValueError = filtre invalide détecté côté client, pas d'appel réseau ;
        5xx amont = réessayer ; 401 = clé invalide ; sinon = erreur Cognism telle
        quelle) et compte l'usage plateforme sur succès (mode plateforme
        actuellement non ouvert pour Cognism, no-op de fait)."""
        client, is_platform = _client()
        try:
            result = fn(client)
        except McpError:
            raise
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
        except Exception as e:
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status and status >= 500:
                msg = ("Cognism est momentanément indisponible (erreur serveur "
                       f"{status}). Réessaie dans un moment — ce n'est pas ton entrée.")
            elif status == 401:
                msg = "Clé Cognism invalide ou révoquée (401). Vérifie la clé posée."
            else:
                msg = f"Cognism n'a pas pu traiter la requête ({e})."
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        if is_platform:
            access.record_platform_usage("cognism")
        return result

    @mcp.tool()
    def cognism_search_contacts(
        filters: Optional[dict] = None,
        index_size: int = 25,
        last_returned_key: Optional[str] = None,
    ) -> dict:
        """Search B2B contacts by name/title/seniority/location/company + more (Cognism).

        Args:
            filters: nested filter dict matching Cognism's exact JSON shape —
                top-level contact fields (firstName, jobTitles, seniority…)
                plus a nested `account` object for the employer's firmographics
                (types, industries, headcount, technologies…). See the
                `cognism-filters` guide (oto_guide, op=read) for the full DSL
                (~150 fields) — closed-set fields (seniority, jobFunctions,
                managementLevel, account.types, funding type/series, hiring
                department, sort_fields, accountSearchOptions) are validated
                before the network call.
            index_size: page size, default 25, max 100.
            last_returned_key: cursor from the previous page's response. Empty
                = first page. Cognism paginates SEQUENTIALLY only — you cannot
                jump to an arbitrary page.

        Returns the raw Cognism page: `results[]` (contacts with `has*`
        boolean flags — NOT real email/phone), `totalResults`,
        `lastReturnedKey` (cursor for the next page). Use
        `cognism_redeem_contacts` to reveal the real email/phone for a match
        (spends credits, unlike search).
        """
        return _run(lambda c: c.search_contacts(
            filters, index_size=index_size, last_returned_key=last_returned_key,
        ))

    @mcp.tool()
    def cognism_search_accounts(
        filters: Optional[dict] = None,
        index_size: int = 100,
        last_returned_key: Optional[str] = None,
    ) -> dict:
        """Search B2B companies by name/domain/industry/headcount/technologies + more (Cognism).

        Args:
            filters: nested filter dict matching Cognism's exact JSON shape —
                same firmographic fields as the `account` object in
                `cognism_search_contacts`, but AT THE ROOT here (no `account`
                prefix — the company IS the root object for this endpoint).
                See the `cognism-filters` guide (oto_guide, op=read) for the
                full DSL — closed-set fields (types, funding type/series,
                hiring department, accountSearchOptions) are validated before
                the network call.
            index_size: page size, default 100, max 100.
            last_returned_key: cursor from the previous page's response. Empty
                = first page. Sequential pagination only, same as contacts.

        Returns the raw Cognism page: `results[]` (companies with `has*`
        boolean flags), `totalResults`, `lastReturnedKey`. Use
        `cognism_redeem_accounts` to reveal real data for a match.
        """
        return _run(lambda c: c.search_accounts(
            filters, index_size=index_size, last_returned_key=last_returned_key,
        ))

    @mcp.tool()
    def cognism_redeem_contacts(
        ids: Optional[list[str]] = None,
        redeem_ids: Optional[list[str]] = None,
        merge_phones_and_locations: bool = False,
    ) -> dict:
        """Reveal full contact data (real email/phone) for contacts found via
        `cognism_search_contacts` (Cognism). SPENDS CREDITS — unlike search.

        Args:
            ids: contact ids from a prior search, OR…
            redeem_ids: redeemIds from a prior search (encode contact + current
                job title + company — Cognism falls back to the current
                redeemId if this one is stale after a job change). Exactly one
                of `ids`/`redeem_ids` is required — mixing both in one call is
                not supported by Cognism.
            merge_phones_and_locations: merge the phones/locations arrays in
                the response.

        Returns `{"total": <int>, "result": [<full contact records>]}`.
        """
        if not ids and not redeem_ids:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="cognism_redeem_contacts requires `ids` or `redeem_ids`.",
            ))
        return _run(lambda c: c.redeem_contacts(
            ids=ids, redeem_ids=redeem_ids,
            merge_phones_and_locations=merge_phones_and_locations,
        ))

    @mcp.tool()
    def cognism_redeem_accounts(
        ids: Optional[list[str]] = None,
        redeem_ids: Optional[list[str]] = None,
        merge_phones_and_locations: bool = False,
    ) -> dict:
        """Reveal full company data for accounts found via
        `cognism_search_accounts` (Cognism). SPENDS CREDITS.

        Args:
            ids: account ids from a prior search, OR…
            redeem_ids: redeemIds from a prior search. Exactly one of
                `ids`/`redeem_ids` is required.
            merge_phones_and_locations: merge the phones/locations arrays.

        Returns `{"total": <int>, "result": [<full account records>]}`.
        """
        if not ids and not redeem_ids:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message="cognism_redeem_accounts requires `ids` or `redeem_ids`.",
            ))
        return _run(lambda c: c.redeem_accounts(
            ids=ids, redeem_ids=redeem_ids,
            merge_phones_and_locations=merge_phones_and_locations,
        ))

    @mcp.tool()
    def cognism_enrich_contact(
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        email: Optional[str] = None,
        sha256: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        phone_number: Optional[str] = None,
        job_title: Optional[str] = None,
        account_name: Optional[str] = None,
        account_website: Optional[str] = None,
        anchor_fields: Optional[list[str]] = None,
        min_match_score: Optional[int] = None,
    ) -> dict:
        """Find ONE best-match contact from identity details, no search step (Cognism).

        Args:
            email / sha256 / linkedin_url: unique identifiers — best accuracy
                alone.
            first_name + last_name + job_title, combined with account_name or
                account_website: second-best accuracy combo.
            phone_number: searched across all phone number types.
            anchor_fields: fields that MUST match for a result to be returned.
            min_match_score: minimum score to return a match (Cognism default
                30; below ~27 is considered low quality). Provide as many
                fields as you have — Cognism returns its best match.

        At least one identity field is required. Returns the matched contact
        (shape depends on your entitlement) with a match score, or an empty
        result if nothing scored above `min_match_score`.
        """
        return _run(lambda c: c.enrich_contact(
            first_name=first_name, last_name=last_name, email=email,
            sha256=sha256, linkedin_url=linkedin_url, phone_number=phone_number,
            job_title=job_title, account_name=account_name,
            account_website=account_website, anchor_fields=anchor_fields,
            min_match_score=min_match_score,
        ))

    @mcp.tool()
    def cognism_enrich_account(
        name: Optional[str] = None,
        website: Optional[str] = None,
        domain: Optional[str] = None,
        linkedin_url: Optional[str] = None,
        country: Optional[str] = None,
        city: Optional[str] = None,
        anchor_fields: Optional[list[str]] = None,
        min_match_score: Optional[int] = None,
    ) -> dict:
        """Find ONE best-match company from identity details, no search step (Cognism).

        Args:
            website / domain / linkedin_url: unique identifiers — best
                accuracy alone.
            name, combined with country or city (HQ or office): second-best
                accuracy combo.
            anchor_fields: fields that MUST match for a result to be returned.
            min_match_score: minimum score to return a match (Cognism default
                40 here — NOTE: different from `cognism_enrich_contact`'s
                default of 30; below ~35 is considered low quality for
                accounts).

        At least one identity field is required.
        """
        return _run(lambda c: c.enrich_account(
            name=name, website=website, domain=domain, linkedin_url=linkedin_url,
            country=country, city=city,
            anchor_fields=anchor_fields, min_match_score=min_match_score,
        ))

    @mcp.tool()
    def cognism_contact_entitlement() -> dict:
        """Which contact fields the configured Cognism key can see (email,
        phones, education, skills…) — check before assuming a field will come
        back populated."""
        return _run(lambda c: c.contact_entitlement())

    @mcp.tool()
    def cognism_account_entitlement() -> dict:
        """Which account/company fields the configured Cognism key can see."""
        return _run(lambda c: c.account_entitlement())

    @mcp.tool()
    def cognism_filter_values(
        kind: str,
        search: Optional[str] = None,
        index_size: int = 20,
        last_returned_key: Optional[str] = None,
    ) -> dict:
        """Allowed values for a DYNAMIC Cognism filter field (Cognism).

        Args:
            kind: one of "technologies", "managementLevels", "companySizes",
                "industries", "jobFunctions", "regions", "countries",
                "states", "sic", "isic", "naics", "skills", "companyTypes",
                "seniority". NOTE: seniority/jobFunctions/managementLevel are
                already validated client-side against a fixed list (see the
                `cognism-filters` guide) — you don't need this tool for those
                unless you suspect Cognism has updated the list.
            search: only for kind="technologies" (the one searchable/paginated
                list) — filters by substring.
            index_size, last_returned_key: pagination, kind="technologies" only.
        """
        return _run(lambda c: c.filter_values(
            kind, search=search, index_size=index_size,
            last_returned_key=last_returned_key,
        ))
