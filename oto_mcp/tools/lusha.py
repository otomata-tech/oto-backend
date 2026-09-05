"""Lusha — contact search + enrich (email/phone reveal) in one call.

Clé résolue par appel via `access.resolve_api_key("lusha")` — provider
byo-only (user key posée sur /account, ou credential partagé de l'org
active). Pas de clé plateforme.

Un seul endpoint câblé pour l'instant : search-and-enrich (jusqu'à 100
contacts par appel, identifiés par email/LinkedIn/nom+société/id Lusha).
Chaque contact peut échouer INDIVIDUELLEMENT (`results[].error` :
NOT_FOUND, COMPLIANCE_RESTRICTED, ENRICH_FAILED) sans faire échouer l'appel
entier — ce n'est PAS un mode bulk "boucle sur du single-record" comme
folk : Lusha accepte nativement un lot dans une seule requête HTTP,
donc aucun `_bulk_run` ici.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from ..mcp_errors import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access
from ..connectors import verify as connector_verify

_MAX_CONTACTS_PER_CALL = 100
_REVEAL_VALUES = frozenset({"emails", "phones"})


def _bad(msg: str) -> McpError:
    return McpError(ErrorData(code=INVALID_PARAMS, message=msg))


def _verify(fields: dict, config: dict | None = None) -> dict:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth+quota`.

    `GET /account/usage`. Ce que la doc Lusha établit : « the Account Usage
    endpoint itself has no charge for checking your balance (it's a utility
    endpoint for monitoring) » — écrit noir sur blanc, pas seulement une
    absence de compteur.

    Le solde (`remaining`) distingue une clé morte d'un compte à sec —
    recharger n'est pas reconnecter.
    """
    from oto.tools.lusha.client import LushaClient

    infos = LushaClient(api_key=fields["key"])._request("GET", "/account/usage") or {}
    restant = infos.get("remaining")
    if not isinstance(restant, int):
        raise RuntimeError(
            f"Lusha a répondu sans solde de crédits lisible : {str(infos)[:200]}")
    if restant <= 0:
        raise connector_verify.QuotaEpuise(
            "La clé Lusha est bonne, mais le compte est à sec (0 crédit "
            "restant). Recharge le compte chez Lusha — reconnecter n'y "
            "changerait rien.")
    return {"quota": {"restant": restant, "unite": "crédits"}}


def register(mcp: FastMCP) -> None:
    from oto.tools.lusha.client import LushaClient

    connector_verify.register("lusha", _verify, couvre=connector_verify.AUTH_QUOTA)

    def _client() -> LushaClient:
        key, _ = access.resolve_api_key("lusha")
        return LushaClient(api_key=key)

    @mcp.tool()
    def lusha_search_and_enrich(
        contacts: list[dict],
        reveal: Optional[list[str]] = None,
        include_partial_profiles: Optional[bool] = None,
    ) -> dict:
        """Search for contacts and reveal their emails/phones in ONE call —
        up to 100 contacts per request.

        Returns —
            {requestId, results: [{id, firstName, lastName, fullName,
            jobTitle, location, tags, emails, phones, company, socialLinks,
            previousEmployment, updateDate, clientReferenceId, error?}],
            billing: {creditsCharged, resultsReturned}}. A contact Lusha
            couldn't resolve or reveal carries an `error: {code, message}`
            (NOT_FOUND | COMPLIANCE_RESTRICTED | ENRICH_FAILED) instead of
            the profile fields — inspect it PER-RESULT, a 200 response can
            still contain per-contact failures.

            ⚠️ Billing: TWO charges apply — one for the search (api_search)
            PLUS one per revealed field per contact.
            `billing.creditsCharged` is the actual total charged for THIS
            call — surface it back before repeating a large reveal.

        Args:
            contacts: one dict per contact to search, up to 100. Identify
                each by ANY combination of: `id` (a Lusha contact id),
                `linkedinUrl`, `email`, `firstName`+`lastName`+
                (`companyName` or `companyDomain`). `clientReferenceId` is
                an optional free-text tag you set yourself, echoed back on
                the matching result — use it to correlate results when you
                don't already have a stable Lusha `id`.
            reveal: which fields to unlock — "emails", "phones", or both.
                Omit to search/match contacts WITHOUT unlocking data
                (billed as search-only, see Returns).
            include_partial_profiles: include results Lusha considers
                incomplete rather than dropping them.
        """
        if not contacts:
            raise _bad("contacts : au moins un contact requis.")
        if len(contacts) > _MAX_CONTACTS_PER_CALL:
            raise _bad(
                f"{len(contacts)} contacts — Lusha plafonne search-and-enrich "
                f"à {_MAX_CONTACTS_PER_CALL} par appel, découper en plusieurs appels.")
        if reveal:
            unknown = set(reveal) - _REVEAL_VALUES
            if unknown:
                raise _bad(
                    f"reveal : valeur(s) inconnue(s) {sorted(unknown)} — "
                    f"attendu parmi {sorted(_REVEAL_VALUES)}.")
        return _client().search_and_enrich(
            contacts, reveal=reveal, include_partial_profiles=include_partial_profiles)
