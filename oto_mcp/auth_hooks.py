"""Récupère l'identité de l'utilisateur courant côté MCP tool.

Le bearer JWT est validé par FastMCP en amont des handlers (auth provider) ;
ici on lit juste le sub depuis le contexte. `OTO_MCP_DEV_SUB` : repli d'identité
en **dev local uniquement** (opt-in par env, jamais posé en prod). Depuis le
retrait du transport stdio (2026-06-13), le serveur est toujours en
streamable_http authentifié — ce repli ne sert plus qu'à un run http local sans
vrai Logto, et reste sans effet tant que l'env n'est pas posée.
"""
from __future__ import annotations

import contextlib
import contextvars
import os
from typing import Iterator, Optional

# Override d'identité pour la face REST (contextvar, par requête). La face MCP lit
# le sub du token via `get_access_token()` (contextvar posé par FastMCP) ; en REST
# ce contextvar n'existe pas. Quand un handler REST veut INVOQUER un tool sous
# l'identité de l'appelant (ex. « tester un outil » depuis le dashboard), il pose
# ce sub-override → `resolve_api_key`/`current_org`/… résolvent la bonne identité
# et les gates de call-time (credential, RBAC connecteur) restent intacts. Copié
# dans le threadpool avec le contexte (anyio `to_thread` propage les contextvars).
_sub_override: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "oto_rest_sub_override", default=None)


@contextlib.contextmanager
def sub_override(sub: Optional[str]) -> Iterator[None]:
    """Fixe l'identité (`sub`) courante le temps d'un bloc — face REST uniquement.

    Ne touche PAS la face MCP (qui lit toujours le token). Réentrant (reset propre)."""
    token = _sub_override.set(sub)
    try:
        yield
    finally:
        _sub_override.reset(token)


def current_user_sub_from_token() -> Optional[str]:
    """Sub de l'utilisateur courant depuis le bearer JWT MCP (ou l'override REST)."""
    override = _sub_override.get()
    if override:
        return override
    try:
        from fastmcp.server.dependencies import get_access_token  # type: ignore
        token = get_access_token()
        if token and getattr(token, "claims", None):
            sub = token.claims.get("sub")
            if sub:
                # Bascule de tenant (B1, otomata#35) : pendant la fenêtre, canonicaliser
                # le sub (vieux token en drain → compte migré) et déclencher la migration
                # pour les users MCP-only. Gaté env → no-op (et aucun coût) hors bascule.
                if os.environ.get("OTO_MCP_TENANT_MIGRATION_ISS"):
                    from . import db
                    sub = db.resolve_sub(sub)
                    db.upsert_user(sub, email=token.claims.get("email"),
                                   name=token.claims.get("name"), iss=token.claims.get("iss"))
                return sub
    except Exception:
        pass
    return os.environ.get("OTO_MCP_DEV_SUB")
