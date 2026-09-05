"""Hunter.io — emails par domaine + email finder + verifier.

Clé résolue par appel : user key (`/account`) prioritaire, sinon platform
key + quota daily (member). Guest doit poser sa propre clé.
"""
from __future__ import annotations

from typing import Optional

import datetime as _dt

from fastmcp import FastMCP

from ..connectors import verify as connector_verify

from .. import access, output_projection


def _verify(fields: dict, config: dict | None = None) -> dict:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth+quota`.

    `GET https://api.hunter.io/v2/account`. Ce que la doc de Hunter établit, cité :

    - **authentifié** — la clé est exigée (`api_key` en query, `X-API-KEY`, ou
      `Authorization: Bearer`) ;
    - **sans effet de bord** — un GET d'information de compte ;
    - **gratuit, et là c'est ÉCRIT** — « All these calls are free. », de la section
      « Account & API management ». Contrairement à Folk et Pennylane, où il fallait
      se contenter de l'absence de compteur de crédits comme indice : ici la doc
      l'affirme.

    Première sonde `auth+quota` : elle lit le SOLDE, pas seulement l'authentification.
    C'est ce qui la rend capable de distinguer les deux refus qu'un appelant confond
    toujours — une clé fausse (qu'il faut remplacer) et un compte à sec (qu'il faut
    recharger). Reconnecter dans le second cas ne sert à rien, et c'est pourtant le
    réflexe.

    Le solde est RENDU, jamais affiché sur la fiche : un chiffre affiché promettrait
    une fraîcheur que la plateforme ne tient qu'en interrogeant, et interroger coûte.
    La fiche porte le verdict et sa date ; le chiffre vit dans la réponse de ce test,
    avec l'instant où il a été lu.
    """
    from oto.tools.hunter.client import HunterClient

    infos = HunterClient(api_key=fields["api_key"]).account_info()
    data = (infos or {}).get("data") or {}
    if not data:
        raise RuntimeError(
            f"Hunter a répondu sans information de compte : {str(infos)[:200]}")

    requetes = (data.get("requests") or {}).get("searches") or {}
    disponible, utilise = requetes.get("available"), requetes.get("used")
    if isinstance(disponible, int) and isinstance(utilise, int):
        restant = disponible - utilise
        if restant <= 0:
            raise connector_verify.QuotaEpuise(
                f"La clé Hunter est bonne, mais le compte est à sec : "
                f"{utilise} recherches utilisées sur {disponible}. Recharge le "
                "compte chez Hunter — reconnecter n'y changerait rien.")
        return {"quota": {
            "restant": restant, "utilise": utilise, "inclus": disponible,
            # L'UNITÉ, sans quoi un nombre nu se lit comme on veut : ce sont des
            # recherches, pas des euros ni des appels.
            "unite": "recherches",
            # L'INSTANT : ce chiffre vieillit dès qu'il est lu, et rien ne le
            # rafraîchit tant que personne ne re-teste.
            "mesure_a": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        }}
    # Le solde n'est pas lisible : la clé authentifie, on le dit, et on ne fabrique
    # pas un quota qu'on n'a pas mesuré.
    return {}


def register(mcp: FastMCP) -> None:
    from oto.tools.hunter.client import HunterClient

    connector_verify.register("hunter", _verify,
                              couvre=connector_verify.AUTH_QUOTA)

    def _client() -> tuple[HunterClient, bool]:
        key, is_platform = access.resolve_api_key("hunter")
        return HunterClient(api_key=key), is_platform

    @mcp.tool()
    def hunter_domain_search(domain: str, limit: int = 10,
                             full: bool = False) -> dict:
        """List public emails found on a company domain (Hunter domain-search).

        Useful to discover existing email patterns and contacts.
        Coût : 1 crédit Hunter par tranche de 10 emails.

        Args:
            domain: Company domain (e.g. "gallimard.fr").
            limit: Max emails to return (1 credit per 10).
            full: return the per-address PROVENANCE too — `sources` (every page where
                the address was seen) and the `verification` detail. They dominate the
                payload and a contact sweep never reads them, hence dropped by
                default; ask for them when you must justify WHERE an address comes
                from (a real need under GDPR).
        """
        client, is_platform = _client()
        result = client.domain_search(domain=domain, limit=limit)
        if is_platform:
            access.record_platform_usage("hunter")
        # Défaut resserré (#36) : l'économie qui demande à être connue ne sert personne
        # — mesuré, aucun agent ne passait l'ancien `compact=True`.
        if not full:
            result = output_projection.project(
                result, items_path="data.emails",
                item_drop=("sources", "verification"))
        return result

    @mcp.tool()
    def hunter_email_finder(
        domain: str,
        first_name: Optional[str] = None,
        last_name: Optional[str] = None,
        full_name: Optional[str] = None,
    ) -> dict:
        """Find a specific person's email at a company (Hunter email-finder).

        Provide either (`first_name` + `last_name`) or `full_name`.
        Coût : 1 crédit Hunter par appel.
        """
        client, is_platform = _client()
        result = client.email_finder(
            domain=domain, first_name=first_name, last_name=last_name, full_name=full_name,
        )
        if is_platform:
            access.record_platform_usage("hunter")
        return result

    @mcp.tool()
    def hunter_email_verify(email: str) -> dict:
        """Verify a single email's deliverability (Hunter email-verifier).

        Coût : 1 crédit Hunter par appel.
        """
        client, is_platform = _client()
        result = client.email_verifier(email=email)
        if is_platform:
            access.record_platform_usage("hunter")
        return result
