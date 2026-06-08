"""Movinmotion back-office — lecture seule, auth compte de service headless.

Surface **masquée par défaut et grant-only** (cf. `tool_visibility`) : un user
non-admin ne voit ces tools que si un admin lui a accordé le namespace `mm`
(`oto_admin_grant_namespace`). Le credential (refresh_token Movinmotion) est un
**secret d'org** posé sur l'org propriétaire (`oto_admin_set_org_secret`, mm est
org-partageable), pas une clé par-user ni un secret serveur SOPS : il est résolu
depuis l'org active du sub et **injecté** dans le client (cf. Phase 5/coffre).

Le client vit dans le package **privé** `oto-mm` (hors core public `oto-cli`,
cf. otomata-tech/oto-cli#9). Doit donc être installé dans l'environnement
d'oto-mcp ; sinon ce module est gracieusement désactivé au register (try/except
dans `tools/__init__.py`).

⚠️ Tout est prod, aucune mutation exposée. Périmètre actuel = la seule société
Movinmotion (l'accès staff cross-clients reste à obtenir).
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access
from ..auth_hooks import current_user_sub_from_token


def register(mcp: FastMCP) -> None:
    from oto_mm import MovinmotionClient

    def _client() -> MovinmotionClient:
        # Backstop d'autorisation AU CALL-TIME : mm est grant-only à credential
        # NON per-user, donc la résolution de clé ne protège pas (contrairement
        # à gocardless). On vérifie l'entitlement ici pour ne PAS dépendre du seul
        # masquage de visibilité (qui peut fail-open si list_tools échoue).
        access.require_namespace("mm")
        # stdio local (sub=None, CLI-like) : MovinmotionClient s'auto-résout via
        # require_secret (fallback CLI). Contexte serveur authentifié : injection
        # du refresh_token depuis le credential de l'org active, jamais de lecture
        # SOPS serveur (cf. Phase 6).
        if current_user_sub_from_token() is None:
            return MovinmotionClient()
        return MovinmotionClient(refresh_token=access.resolve_org_credential("mm"))

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
