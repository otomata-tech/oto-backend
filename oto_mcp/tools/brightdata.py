"""Brightdata — coquille vide (scaffold).

Connecteur câblé côté plateforme (entrée registre `providers.py` + clé platform +
quota), mais **aucun tool fonctionnel** n'est encore exposé : les produits Bright
Data (SERP API, Web Unlocker, Web Scraper/Datasets) restent à implémenter.

`register(mcp)` est appelé par `register_all` (dérivé du registre) mais
n'enregistre rien pour l'instant — les helpers `_client`/`_run` sont posés pour les
futurs tools `brightdata_*` (résolution + comptage d'usage plateforme identiques à
serper/serpapi). Voir `oto.tools.brightdata.client.BrightDataClient` (oto-core) pour
les produits documentés en TODO.
"""
from __future__ import annotations

from fastmcp import FastMCP

from .. import access


def register(mcp: FastMCP) -> None:
    # Imports/helpers posés pour les futurs tools — pas de tool exposé (coquille vide).
    from oto.tools.brightdata.client import BrightDataClient  # noqa: F401

    def _client() -> tuple[BrightDataClient, bool]:
        key, is_platform = access.resolve_api_key("brightdata", "BRIGHTDATA_API_KEY")
        return BrightDataClient(api_key=key), is_platform

    def _run(method: str, **kwargs) -> dict:
        """Résout la clé, appelle la méthode du client, compte l'usage plateforme."""
        client, is_platform = _client()
        result = getattr(client, method)(**kwargs)
        if is_platform:
            access.record_platform_usage("brightdata")
        return result

    # TODO — enregistrer les tools produit ici (cf. docstring du module) :
    #   brightdata_serp(query, engine="google", ...)  -> SERP parsée
    #   brightdata_unlock(url, data_format=None, ...)  -> HTML / Markdown
    #   brightdata_dataset_*(...)                      -> datasets async
    _ = (_client, _run)  # référencés pour éviter un warning « unused » prématuré.
