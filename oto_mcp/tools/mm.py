"""Movinmotion back-office — lecture seule, auth compte de service headless.

Surface **masquée par défaut et grant-only** (cf. `tool_visibility`) : un user
non-admin ne voit ces tools que si un admin lui a accordé le namespace `mm`
(`oto_admin_grant_namespace`). Le credential est un secret serveur
(`MM_REFRESH_TOKEN`), pas une clé par-user : compte de service unique « Admin 2 ».

⚠️ Tout est prod, aucune mutation exposée. Périmètre actuel = la seule société
Movinmotion (l'accès staff cross-clients reste à obtenir).
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    from oto.tools.mm import MovinmotionClient

    def _client() -> MovinmotionClient:
        # Backstop d'autorisation AU CALL-TIME : le credential est un secret
        # serveur (MM_REFRESH_TOKEN), pas une clé per-user, donc la résolution
        # de clé ne protège pas (contrairement à gocardless). On vérifie
        # l'entitlement ici pour ne PAS dépendre du seul masquage de visibilité
        # (qui peut fail-open si list_tools échoue au handshake).
        access.require_namespace("mm")
        return MovinmotionClient()

    @mcp.tool()
    async def mm_subscription_companies() -> dict:
        """Sociétés administrables par le compte de service Movinmotion.

        ⚠️ Périmètre actuel : une seule société (Movinmotion elle-même) — l'accès
        staff cross-clients (~3100 clients) n'est pas encore ouvert.
        """
        return _client().subscription_companies()

    @mcp.tool()
    async def mm_company_infos(company_hash: str) -> dict:
        """Fiche d'une société (identité, contexte) par son hash."""
        return _client().company_infos(company_hash)

    @mcp.tool()
    async def mm_api_get(path: str) -> dict:
        """GET brut lecture seule sur l'API back-office (`back.app.movinmotion.com`).

        `path` doit commencer par `/api/` (ex. `/api/core/v1/companies/<hash>/roles`).
        Échappatoire d'exploration tant que la surface n'est pas stabilisée —
        strictement lecture, aucune mutation possible.
        """
        return _client().get(path)
