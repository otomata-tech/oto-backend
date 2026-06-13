"""Connecteur universel de fédération MCP (otomata#16) — forward describe/call.

⚠️ Réécrit après incident (2026-06-13) : l'approche live-mount via
`FastMCPProxy.mount()` est ABANDONNÉE. Son `ProxyInitializeMiddleware` force une
connexion au backend à CHAQUE `initialize` (chaque connexion client) — or le
token est *per-user* et absent au handshake → l'initialize du backend lève →
tout le connecteur oto échoue. Le modèle mount() suppose un backend joignable +
authentifié dès le handshake, incompatible avec un credential per-user.

À la place — même pattern que le bridge REST `tools/remote.py`, mais vers un MCP :
deux outils STATIQUES par connecteur `kind="mount"` :
- `<ns>_describe` : liste les outils du MCP distant (découverte).
- `<ns>_call(tool, arguments)` : forwarde l'appel.

`tools/list` d'oto ne touche jamais le réseau ni un token → handshake sûr. Le
token per-user n'est résolu qu'à l'APPEL (`access.resolve_mount_token`), avec le
gating `require_namespace`. Un user non connecté → McpError actionnable à l'appel,
jamais un crash de session.

(Évolution possible : outils memento NATIFS — `memento_mem_search` avec le vrai
schéma — via sous-classe Tool + snapshot du catalogue. Plus ergonomique, mais
demande de construire des Tools à schéma explicite ; reporté.)
"""
from __future__ import annotations

import logging
import os

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connectors
from ..auth_hooks import current_user_sub_from_token

log = logging.getLogger("oto_mcp.tools.mount")
TIMEOUT = 45


def _enabled_mounts() -> set[str]:
    """Kill-switch / activation explicite par env. `OTO_MCP_MOUNTS_ENABLED` (CSV)
    liste les connecteurs mount à activer ; vide → aucun (défaut sûr) ; `*` = tous."""
    raw = (os.environ.get("OTO_MCP_MOUNTS_ENABLED") or "").strip()
    if raw == "*":
        return {c.name for c in connectors.MOUNT_CONNECTORS}
    return {n.strip() for n in raw.split(",") if n.strip()}


def _client(connector: connectors.Connector) -> Client:
    """Client MCP vers le backend distant, authentifié avec le token DU USER
    courant (résolu par requête, jamais figé). Gating namespace au call-time."""
    ns = connector.namespaces[0]
    access.require_namespace(ns)
    if current_user_sub_from_token() is None:
        raise McpError(ErrorData(
            code=INVALID_PARAMS,
            message=(
                f"Connecteur fédéré `{connector.name}` indisponible en stdio local "
                f"(credential per-user serveur requis)."
            ),
        ))
    token = access.resolve_mount_token(connector.name)  # lève si non connecté
    return Client(StreamableHttpTransport(
        connector.mount_url, headers={"Authorization": f"Bearer {token}"}))


def _register_one(mcp: FastMCP, c: connectors.Connector) -> None:
    if not c.mount_url:
        log.warning("mount connector %s sans mount_url — ignoré", c.name)
        return
    ns = c.namespaces[0]

    @mcp.tool(
        name=f"{ns}_describe",
        description=(
            f"{c.label} — liste les outils disponibles du MCP fédéré ({c.help}). "
            f"À appeler d'abord pour découvrir les outils invocables via {ns}_call."
        ),
    )
    async def describe() -> dict:
        async with _client(c) as client:
            tools = await client.list_tools()
        return {"tools": [
            {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
            for t in tools
        ]}

    @mcp.tool(
        name=f"{ns}_call",
        description=(
            f"{c.label} — appelle un outil du MCP fédéré ({c.help}). "
            f"`tool` = nom de l'outil distant (voir {ns}_describe), `arguments` = "
            f"ses paramètres. Exécuté avec TON compte {c.label} (per-user)."
        ),
    )
    async def call(tool: str, arguments: dict | None = None) -> dict:
        async with _client(c) as client:
            res = await client.call_tool(tool, arguments or {}, raise_on_error=False)
        if getattr(res, "is_error", False):
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=f"{c.label} `{tool}` a renvoyé une erreur : {res.content}",
            ))
        return res.data if getattr(res, "data", None) is not None else {"content": [
            getattr(b, "text", str(b)) for b in (res.content or [])
        ]}

    log.info("federated MCP %s → tools %s_describe / %s_call", c.name, ns, ns)


def register(mcp: FastMCP) -> None:
    enabled = _enabled_mounts()
    for c in connectors.MOUNT_CONNECTORS:
        if c.name not in enabled:
            log.info("mount %s déclaré mais non activé (OTO_MCP_MOUNTS_ENABLED)", c.name)
            continue
        _register_one(mcp, c)
