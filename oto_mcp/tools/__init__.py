"""Registers all MCP tools on a FastMCP instance.

Each connector lives in its own module; importing it lazy keeps startup fast
and isolates failures (a missing API key for one connector doesn't kill the
whole server).
"""
from __future__ import annotations

from fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    import logging

    log = logging.getLogger("oto_mcp.tools")

    # Connecteurs API-only — fail fast (les clés sont gérées par oto.config.get_secret).
    from . import recherche_entreprises, sirene, serper, hunter
    recherche_entreprises.register(mcp)
    sirene.register(mcp)
    serper.register(mcp)
    hunter.register(mcp)

    # Browser — optionnel : si o-browser ou patchright manquent, on log et on
    # continue sans LinkedIn plutôt que de cracher tout le MCP.
    try:
        from . import linkedin
        linkedin.register(mcp)
    except Exception as e:
        log.warning("LinkedIn tools disabled: %s", e)
