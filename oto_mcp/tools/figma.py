"""Figma — files, image export, comments, FigJam extraction.

Wrappe `oto.tools.figma.FigmaClient`. Token résolu par appel via
`access.resolve_api_key("figma")` — byo. **Cache disque désactivé**
(`cache_enabled=False`) : sur un host multi-utilisateur le cache fichier n'est
pas clefé par token → fuite cross-user.
"""
from __future__ import annotations

from typing import Optional

from fastmcp import FastMCP

from .. import access
from ..connectors import verify as connector_verify


def _verify(fields: dict, config: dict | None = None) -> None:
    """Sonde « tester la connexion » — otomata-tech/oto#69. Couvre `auth` SEUL.

    `GET /v1/me`. Ce que la doc Figma établit :

    - **authentifié** — jeton en en-tête `X-Figma-Token`, comme le reste de
      l'API ;
    - **sans effet de bord** — une lecture d'identité (`id`, `handle`, `email`) ;
    - **le coût** — aucune mention de coût ni de limite de débit particulière
      pour cet appel. Absence de mention, indice, pas une preuve.

    ⚠️ **Quatrième règle d'oto#69 : une sonde ne transforme jamais sa propre
    limite en verdict sur la clé.** `/v1/me` exige le scope `current_user:read`
    — SÉPARÉ des scopes réels du connecteur (lecture de fichiers/design). Un
    jeton légitimement scopé pour l'usage réel peut refuser CET appel sans être
    cassé. Figma documente `403` pour un scope manquant et `401` pour un jeton
    mort/invalide — deux codes distincts, donc distinguables : le 403 lève un
    `RuntimeError` NU (jamais `NonAutorise`) pour tomber sur le verdict
    `unknown` (« je ne sais pas »), PAS `unauthorized` (« remplace ta clé ») —
    un faux négatif ici pousserait à révoquer une clé qui marche. Le vrai 401
    lève `NonAutorise`, verdict `unauthorized` mérité.
    """
    import requests
    from oto.tools.figma.client import FigmaClient

    try:
        infos = FigmaClient(token=fields["key"], cache_enabled=False)._request(
            "GET", "me", use_cache=False) or {}
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else None
        if status == 403:
            raise RuntimeError(
                "Figma refuse CET appel de vérification (403, scope "
                "current_user:read) — ça ne dit RIEN de la clé pour l'usage "
                "réel du connecteur (fichiers/design, un scope différent). "
                "Non concluant, pas invalide.") from e
        if status == 401:
            raise connector_verify.NonAutorise(
                f"Figma refuse cette clé (401) : {str(e)[:200]}") from e
        raise
    if not infos.get("id"):
        raise RuntimeError(
            "Figma a répondu sans identifier d'utilisateur pour cette clé — "
            f"réponse inattendue : {str(infos)[:200]}")


def register(mcp: FastMCP) -> None:
    from oto.tools.figma.client import FigmaClient

    connector_verify.register("figma", _verify)

    def _client() -> FigmaClient:
        key, _ = access.resolve_api_key("figma")
        return FigmaClient(token=key, cache_enabled=False)

    @mcp.tool()
    def figma_get_file(
        file_key: str,
        depth: Optional[int] = None,
        node_ids: Optional[list[str]] = None,
    ) -> dict:
        """Get a Figma/FigJam file structure.

        Args:
            file_key: the key from the file URL (figma.com/file/<KEY>/…).
            depth: limit tree depth (cheaper for big files).
            node_ids: restrict to specific nodes.
        """
        return _client().get_file(file_key, depth=depth, node_ids=node_ids)

    @mcp.tool()
    def figma_file_meta(file_key: str) -> dict:
        """Get a file's metadata only (name, last modified, thumbnail…)."""
        return _client().get_file_meta(file_key)

    @mcp.tool()
    def figma_get_images(
        file_key: str,
        node_ids: list[str],
        format: str = "png",
        scale: float = 2,
    ) -> dict:
        """Export rendered images for nodes. Returns temporary image URLs.

        Args:
            format: png | jpg | svg | pdf.
            scale: scale factor (1–4).
        """
        return _client().get_images(file_key, node_ids, format=format, scale=scale)

    @mcp.tool()
    def figma_get_comments(file_key: str, as_markdown: bool = False) -> dict:
        """List comments on a file."""
        return _client().get_comments(file_key, as_markdown=as_markdown)

    @mcp.tool()
    def figma_post_comment(
        file_key: str,
        message: str,
        comment_id: Optional[str] = None,
    ) -> dict:
        """Post a comment on a file (or reply to `comment_id`)."""
        return _client().post_comment(file_key, message, comment_id=comment_id)
