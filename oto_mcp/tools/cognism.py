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

**Surface consolidée (ADR 0047 §Amendement, appliqué au connecteur cognism)** :
9 → 6 tools. L'axe homogène ici est la **cible** (contact = la personne /
account = la société), pas le verbe : `search` et `redeem` prennent EXACTEMENT
les mêmes paramètres des deux côtés (`filters`/`index_size`/`last_returned_key`
d'une part, `ids`/`redeem_ids`/`merge_phones_and_locations` de l'autre), et
`entitlement` n'en prend aucun. D'où `cognism_search`, `cognism_redeem` et
`cognism_entitlement`, la cible en paramètre `op`.

⚠️ **La frontière gratuit/payant est portée par le NOM DU TOOL, délibérément** :
`cognism_search` = preview GRATUIT (flags `has*`, pas d'email/téléphone réel) ;
`cognism_redeem` = reveal **facturé en crédits**, tool entier, aucune op gratuite
dedans. Regrouper par objet métier (`cognism_contact(op=search|redeem|enrich)`)
aurait noyé le seul fait qui coûte de l'argent dans une liste d'ops d'un tool de
recherche gratuit — en plus de réunir trois jeux de paramètres disjoints. Corollaire :
**`op` n'a de défaut nulle part dans ce module** — sur `cognism_redeem` parce
qu'aucun crédit ne doit pouvoir partir sans une intention explicite, sur les deux
autres parce que les cibles ne sont pas substituables (le `filters` d'une recherche
société n'a pas la même racine que celui d'une recherche contact : une cible devinée
rendrait une page vide ou fausse, pas une erreur).

Trois tools restent SEULS :
- `cognism_enrich_contact` / `cognism_enrich_account` : variantes DISJOINTES —
  11 et 8 paramètres d'identité dont 3 seulement en commun (`linkedin_url`,
  `anchor_fields`, `min_match_score`) ; le reste (email/sha256/phone_number/
  job_title/account_name/account_website… contre name/website/domain/country/city)
  ne se recouvre pas, et même `min_match_score` n'a pas le même défaut amont
  (30 contact / 40 account). Fusionnées, elles pèseraient au schéma exactement ce
  qu'elles pèsent séparées (critère = homogénéité des paramètres, pas le comptage).
- `cognism_filter_values` : découverte des valeurs autorisées d'un champ de filtre
  DYNAMIQUE — son `kind` (technologies/regions/naics/…) est un vocabulaire sans
  rapport avec la cible contact/account, le confondre avec `op` créerait deux sens
  pour un même paramètre. Même cas que `zoho_modules`.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `verify_key()` (déjà dans le client — un appel `GET
    entitlement/contactEntitlementSubscription`, écrit dans oto-core pour
    exactement cet usage : « Valide la clé via un appel entitlement. Lève la
    HTTPError amont (401 = clé invalide) si KO. »). Bearer token, lecture sans
    effet de bord.

    **Authentifié ≠ utilisable** (classe oto#69) : ne distingue pas de scope
    ici — l'entitlement Contact est une souscription DE BASE, pas une des deux
    cibles (`contact`/`account`) que `_target()` refuse déjà en amont pour une
    op inconnue.
    """
    from oto.tools.cognism.client import CognismClient

    CognismClient(api_key=fields["key"]).verify_key()


# Les deux CIBLES du connecteur, valeurs de `op` : le contact (la personne) et
# l'account (la société). Source unique — la validation d'entrée ET le message de
# refus en dérivent, donc une cible ajoutée ne peut pas être acceptée sans être
# annoncée (ni l'inverse).
_TARGETS = ("contact", "account")
_TARGETS_ERROR = "op doit être 'contact' ou 'account'"

# Taille de page par défaut CHEZ COGNISM, qui diffère selon la cible (25 contacts,
# 100 sociétés). `index_size=None` = « le défaut de la cible » : figer une valeur
# unique changerait silencieusement la pagination d'un des deux côtés.
_DEFAULT_INDEX_SIZE = {"contact": 25, "account": 100}


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _target(op: str) -> str:
    """Valide `op` AVANT toute résolution de clé et tout appel réseau — une cible
    inconnue ne doit jamais atteindre le client (donc jamais, par un chemin dérivé,
    consommer un crédit)."""
    if op not in _TARGETS:
        raise _bad(_TARGETS_ERROR)
    return op


def register(mcp: FastMCP) -> None:
    from oto.tools.cognism.client import CognismClient

    connector_verify.register("cognism", _verify)

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
                msg = (f"Cognism a rendu une erreur serveur ({status}). Un 5xx amont "
                       "ne prouve pas une panne : vérifie d'abord les paramètres de "
                       "l'appel. Si l'entrée est correcte : une seule nouvelle "
                       "tentative, différée.")
            elif status == 401:
                msg = "Clé Cognism invalide ou révoquée (401). Vérifie la clé posée."
            else:
                msg = f"Cognism n'a pas pu traiter la requête ({e})."
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        if is_platform:
            access.record_platform_usage("cognism")
        return result

    @mcp.tool()
    def cognism_search(
        op: Literal["contact", "account"],
        filters: Optional[dict] = None,
        index_size: Optional[int] = None,
        last_returned_key: Optional[str] = None,
    ) -> dict:
        """Search the B2B database — contacts (people) or accounts (companies).
        FREE preview: SPENDS NO CREDITS (Cognism).

        `op` — the target. Required, no default: the two are NOT interchangeable
        (the `filters` dict does not have the same root, see below), so a guessed
        target would return an empty or wrong page rather than an error.
        - **"contact"**: search B2B contacts by name/title/seniority/location/
          company + more.
        - **"account"**: search B2B companies by name/domain/industry/headcount/
          technologies + more.

        Returns the raw Cognism page: `results[]` (contacts/companies with `has*`
        boolean flags — NOT real email/phone), `totalResults`, `lastReturnedKey`
        (cursor for the next page). Use `cognism_redeem` to reveal the real
        email/phone (contact) or the full record (account) for a match — that one
        SPENDS CREDITS, unlike this search.

        Args:
            op: contact | account.
            filters: nested filter dict matching Cognism's exact JSON shape.
                - op="contact": top-level contact fields (firstName, jobTitles,
                  seniority…) plus a nested `account` object for the employer's
                  firmographics (types, industries, headcount, technologies…).
                - op="account": the same firmographic fields, but AT THE ROOT
                  (no `account` prefix — the company IS the root object for this
                  endpoint).
                See the `cognism-filters` guide (oto_guide, op=read) for the full
                DSL (~150 fields) — closed-set fields are validated before the
                network call: op="contact" (seniority, jobFunctions,
                managementLevel, account.types, funding type/series, hiring
                department, sort_fields, accountSearchOptions), op="account"
                (types, funding type/series, hiring department,
                accountSearchOptions).
            index_size: page size, max 100. Omitted = Cognism's own default FOR
                THAT TARGET: 25 for op="contact", 100 for op="account".
            last_returned_key: cursor from the previous page's response. Empty
                = first page. Cognism paginates SEQUENTIALLY only — you cannot
                jump to an arbitrary page. Same for both targets.
        """
        target = _target(op)
        size = index_size if index_size is not None else _DEFAULT_INDEX_SIZE[target]
        if target == "contact":
            return _run(lambda c: c.search_contacts(
                filters, index_size=size, last_returned_key=last_returned_key,
            ))
        return _run(lambda c: c.search_accounts(
            filters, index_size=size, last_returned_key=last_returned_key,
        ))

    @mcp.tool()
    def cognism_redeem(
        op: Literal["contact", "account"],
        ids: Optional[list[str]] = None,
        redeem_ids: Optional[list[str]] = None,
        merge_phones_and_locations: bool = False,
    ) -> dict:
        """⚠️ SPENDS CREDITS. Reveal the full record of matches previously found
        with `cognism_search` — searching is free, THIS call is billed (Cognism).

        `op` — the target. Required, no default: nothing in this tool is free.
        - **"contact"**: reveal full contact data (real email/phone) for contacts
          found via `cognism_search(op="contact")`.
        - **"account"**: reveal full company data for accounts found via
          `cognism_search(op="account")`.

        Returns `{"total": <int>, "result": [<full contact/account records>]}`.

        Args:
            op: contact | account.
            ids: contact ids (op="contact") / account ids (op="account") from a
                prior search, OR…
            redeem_ids: redeemIds from a prior search (they encode contact +
                current job title + company — Cognism falls back to the current
                redeemId if this one is stale after a job change). Exactly one of
                `ids`/`redeem_ids` is required — mixing both in one call is not
                supported by Cognism.
            merge_phones_and_locations: merge the phones/locations arrays in the
                response.
        """
        target = _target(op)
        if not ids and not redeem_ids:
            raise _bad(f"cognism_redeem(op='{target}') requiert `ids` ou "
                       "`redeem_ids` — cet appel consomme des crédits, rien n'est "
                       "deviné. Les deux viennent d'un `cognism_search` précédent.")
        if target == "contact":
            return _run(lambda c: c.redeem_contacts(
                ids=ids, redeem_ids=redeem_ids,
                merge_phones_and_locations=merge_phones_and_locations,
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

        At least one identity field is required. Returns the matched contact
        (shape depends on your entitlement) with a match score, or an empty
        result if nothing scored above `min_match_score`.

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

        At least one identity field is required.

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
        """
        return _run(lambda c: c.enrich_account(
            name=name, website=website, domain=domain, linkedin_url=linkedin_url,
            country=country, city=city,
            anchor_fields=anchor_fields, min_match_score=min_match_score,
        ))

    @mcp.tool()
    def cognism_entitlement(op: Literal["contact", "account"]) -> dict:
        """Which fields the configured Cognism key can see — check before assuming
        a field will come back populated (Cognism).

        `op` — the target. Required, no default.
        - **"contact"**: contact fields (email, phones, education, skills…).
        - **"account"**: account/company fields.

        Args:
            op: contact | account.
        """
        target = _target(op)
        if target == "contact":
            return _run(lambda c: c.contact_entitlement())
        return _run(lambda c: c.account_entitlement())

    @mcp.tool()
    def cognism_filter_values(
        kind: Literal["technologies", "managementLevels", "companySizes",
                      "industries", "jobFunctions", "regions", "countries",
                      "states", "sic", "isic", "naics", "skills",
                      "companyTypes", "seniority"],
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
