"""Hooks pour récupérer l'identité de l'utilisateur courant.

Sépare l'auth (un autre Claude implémente le wiring Logto) du reste du code.
Les fonctions ci-dessous sont des points d'extension : à compléter quand
le `RemoteAuthProvider` Logto sera branché.

Côté MCP tools : `current_user_sub_from_token()` lit le sub depuis le bearer
JWT validé par FastMCP.

Côté UI `/settings` : `current_user_sub_from_request()` lit le sub depuis la
session navigateur (cookie OIDC à brancher par l'auth Claude).

Pendant le dev (avant que Logto soit branché), `OTO_MCP_DEV_SUB` permet de
fixer un sub par défaut pour ne pas bloquer le développement.
"""
from __future__ import annotations

import os
from typing import Optional


def _dev_sub() -> Optional[str]:
    return os.environ.get("OTO_MCP_DEV_SUB")


def current_user_sub_from_token() -> Optional[str]:
    """Sub de l'utilisateur courant depuis le bearer JWT MCP.

    À CÂBLER : lire depuis le contexte FastMCP (ex. `get_access_token().claims['sub']`)
    quand l'auth Logto sera en place. Fallback `OTO_MCP_DEV_SUB` pour le dev.
    """
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
        if token and getattr(token, "claims", None):
            sub = token.claims.get("sub")
            if sub:
                return sub
    except Exception:
        pass
    return _dev_sub()


def current_user_sub_from_request(request) -> Optional[str]:  # type: ignore[no-untyped-def]
    """Sub de l'utilisateur courant depuis une requête HTTP Starlette.

    À CÂBLER : à brancher sur la session navigateur OIDC Logto. Pour l'instant,
    cherche un header `X-Oto-User-Sub` (à supprimer en prod) ou fallback dev.
    """
    sub = request.headers.get("x-oto-user-sub")
    if sub:
        return sub
    return _dev_sub()
