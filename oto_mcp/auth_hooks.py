"""Récupère l'identité de l'utilisateur courant côté MCP tool.

Le bearer JWT est validé par FastMCP en amont des handlers (auth provider) ;
ici on lit juste le sub depuis le contexte. `OTO_MCP_DEV_SUB` court-circuite
en dev quand on tourne sans auth (`MCP_TRANSPORT=stdio`).
"""
from __future__ import annotations

import os
from typing import Optional


def current_user_sub_from_token() -> Optional[str]:
    """Sub de l'utilisateur courant depuis le bearer JWT MCP."""
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
        if token and getattr(token, "claims", None):
            sub = token.claims.get("sub")
            if sub:
                return sub
    except Exception:
        pass
    return os.environ.get("OTO_MCP_DEV_SUB")
