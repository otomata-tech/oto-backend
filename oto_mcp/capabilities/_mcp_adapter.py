"""Adaptateur MCP de la couche capacité (ADR 0009).

Boucle sur le registre et monte un tool FastMCP par capacité ayant un binding
`mcp`. Chaque tool applique, avant le handler : validation `Input` → autz →
handler. L'`AuthzDenied` neutre est traduit en `McpError`. Le schéma du tool
est aplati (params plats) via `apply_flat_signature`.

Dépend du core (sens unique ADR 0004) ; le core n'importe pas cet adaptateur.
"""
from __future__ import annotations

from fastmcp import FastMCP
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from ..auth_hooks import current_user_sub_from_token
from ._types import AuthzDenied, Capability, RawCtx, apply_flat_signature


def _make_tool(cap: Capability):
    async def _tool(**kwargs):
        raw = RawCtx(sub=current_user_sub_from_token())
        try:
            inp = cap.Input(**kwargs)                 # validation (seule source : Input)
            ctx = cap.authz(raw, inp)                 # autz (peut lire inp pour ORG_ADMIN_OF)
            return cap.handler(ctx, inp)              # handler core
        except AuthzDenied as d:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=d.message or d.code))
    _tool.__name__ = cap.mcp
    _tool.__doc__ = cap.description or cap.key
    return apply_flat_signature(_tool, cap.Input)


def register(instance: FastMCP, capabilities: list[Capability]) -> None:
    """Monte un tool par capacité MCP. No-op si la liste est vide (canari)."""
    for cap in capabilities:
        if cap.mcp is None:
            continue
        instance.tool(name=cap.mcp, description=cap.description or None)(_make_tool(cap))
