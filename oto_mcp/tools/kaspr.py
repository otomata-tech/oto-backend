"""Kaspr — enrichissement contacts B2B depuis URL LinkedIn (emails + téléphones).

Provider user-only : pas de quota plateforme, chaque user pose sa clé sur
`/account`. Kaspr facture en crédits à l'enrichissement.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connector_verify

# La normalisation du slug LinkedIn (URL → slug nu, sinon Kaspr 500) vit dans le
# client oto-core (`oto.tools.kaspr.client.linkedin_slug`), pas ici — logique
# canonique partagée par tous les consommateurs. Ce wrapper ne fait que traduire
# une erreur Kaspr en McpError actionnable.


def _verify(fields: dict, config: dict | None = None) -> None:  # noqa: ARG001 (config: contrat de sonde, non utilisé ici)
    """Sonde « tester la connexion » : la clé authentifie-t-elle vraiment ?

    `verify_key()` (oto-core) fait un POST sentinel sans effet de bord ni crédit
    consommé (Kaspr n'a pas de `/me`) — 401 sur clé invalide. Lève — le message
    remonte tel quel à l'UI.
    """
    from oto.tools.kaspr.client import KasprClient
    KasprClient(api_key=fields["key"]).verify_key()


def register(mcp: FastMCP) -> None:
    from oto.tools.kaspr.client import KasprClient

    connector_verify.register("kaspr", _verify)

    def _client() -> tuple[KasprClient, bool]:
        key, is_platform = access.resolve_api_key("kaspr")
        return KasprClient(api_key=key), is_platform

    @mcp.tool()
    def kaspr_enrich_linkedin(
        linkedin_id: str,
        name: Optional[str] = None,
        with_phone: bool = False,
        data_to_get: Optional[list[str]] = None,
    ) -> dict:
        """Enrich a LinkedIn profile with emails and (optionally) phone numbers.

        Args:
            linkedin_id: the person's LinkedIn handle. Either the bare slug
                ("alexis-laporte") OR the full profile URL
                ("https://www.linkedin.com/in/alexis-laporte/") — both work, the
                slug is extracted automatically. NOT a name or a search query.
            name: Optional fallback name if the slug alone is ambiguous.
            with_phone: Request mobile/work phones (extra credits cost).
            data_to_get: Subset of fields to retrieve (Kaspr-specific, e.g.
                ["emails", "phones", "company"]). Defaults to all.

        Cost: 1 credit per email, +1 per phone if `with_phone=True`.
        """
        client, is_platform = _client()
        # with_phone=True → include "phone" in data_to_get (costs extra credits)
        effective_data = data_to_get
        if effective_data is None and with_phone:
            effective_data = ["workEmail", "phone"]
        try:
            # Le client oto-core normalise linkedin_id (URL → slug) avant l'appel.
            result = client.enrich_linkedin(
                linkedin_id=linkedin_id,
                name=name,
                is_phone_required=with_phone,
                data_to_get=effective_data,
            )
        except Exception as e:
            # Distingue panne Kaspr (5xx upstream → réessayer) d'une mauvaise
            # entrée → message actionnable plutôt qu'un 500 brut.
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status and status >= 500:
                msg = ("Kaspr est momentanément indisponible (erreur serveur Kaspr "
                       f"{status}). Réessaie dans un moment — ce n'est pas ton entrée.")
            else:
                msg = (f"Kaspr n'a pas pu enrichir `{linkedin_id}` ({e}). Vérifie le "
                       f"profil LinkedIn (slug ou URL valide).")
            raise McpError(ErrorData(code=INVALID_PARAMS, message=msg))
        if is_platform:
            access.record_platform_usage("kaspr")
        return result
