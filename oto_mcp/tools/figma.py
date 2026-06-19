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


def register(mcp: FastMCP) -> None:
    from oto.tools.figma.client import FigmaClient

    def _client() -> FigmaClient:
        key, _ = access.resolve_api_key("figma", "FIGMA_API_KEY")
        return FigmaClient(token=key, cache_enabled=False)

    @mcp.tool()
    async def figma_get_file(
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
    async def figma_file_meta(file_key: str) -> dict:
        """Get a file's metadata only (name, last modified, thumbnail…)."""
        return _client().get_file_meta(file_key)

    @mcp.tool()
    async def figma_get_images(
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
    async def figma_get_comments(file_key: str, as_markdown: bool = False) -> dict:
        """List comments on a file."""
        return _client().get_comments(file_key, as_markdown=as_markdown)

    @mcp.tool()
    async def figma_post_comment(
        file_key: str,
        message: str,
        comment_id: Optional[str] = None,
    ) -> dict:
        """Post a comment on a file (or reply to `comment_id`)."""
        return _client().post_comment(file_key, message, comment_id=comment_id)
