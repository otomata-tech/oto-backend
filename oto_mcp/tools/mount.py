"""Connecteur universel `mount()` — fédération de MCP distants (otomata#16).

Pour chaque connecteur `kind="mount"` du registre, monte le serveur MCP distant
(`mount_url`) dans oto via un proxy FastMCP : ses tools apparaissent nativement,
préfixés par le namespace (`memento_mem_search`…). Le chaînage est transparent
(MCP-sur-MCP : chaque hop forwarde tools/list + tools/call).

Différence avec le bridge `remote` (ADR 0003, tools/remote.py) :
- remote = shim REST `describe`/`call`, credential d'ORG (token M2M du bridge).
- mount = MCP distant déjà riche + déjà authentifié PAR USER (ex. memento, OAuth
  Supabase). Chaque user porte SON token.

Injection per-user : `FastMCPProxy(client_factory=…)` appelle la factory **à
chaque invocation, dans le contexte de requête** (async supporté). La factory lit
le `sub` courant, résout SON token dans le coffre (`access.resolve_mount_token`),
et renvoie un `Client` HTTP portant ce bearer. Aucun token figé au montage.

Gating + visibilité unifiés par la factory : `require_namespace` (deny-by-default)
puis résolution du token lèvent une McpError pour un user non-entitled / non-
connecté → le ProxyProvider (provider_error_strategy="warn") **skip** les tools de
ce mount pour cette session, au lieu de crasher la liste. Un user entitled +
connecté les voit et les appelle ; les autres ne les voient pas.
"""
from __future__ import annotations

import logging

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import FastMCPProxy

from .. import access, connectors
from ..auth_hooks import current_user_sub_from_token

log = logging.getLogger("oto_mcp.tools.mount")


def _make_client_factory(connector: connectors.Connector):
    ns = connector.namespaces[0]
    url = connector.mount_url

    async def factory() -> Client:
        # Gating call-time : deny-by-default sur le namespace grant-only, comme
        # les connecteurs in-process. Lève → ProxyProvider warn+skip (invisible).
        access.require_namespace(ns)
        # Le sub doit être identifiable (pas de coffre en stdio local).
        if current_user_sub_from_token() is None:
            raise RuntimeError(
                f"mount `{connector.name}` indisponible en stdio local "
                f"(credential per-user serveur requis)."
            )
        token = access.resolve_mount_token(connector.name)
        transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
        return Client(transport)

    return factory


def _register_one(mcp: FastMCP, c: connectors.Connector) -> None:
    if not c.mount_url:
        log.warning("mount connector %s sans mount_url — ignoré", c.name)
        return
    proxy = FastMCPProxy(client_factory=_make_client_factory(c))
    mcp.mount(proxy, namespace=c.namespaces[0])
    log.info("mounted federated MCP %s → namespace %s", c.name, c.namespaces[0])


def register(mcp: FastMCP) -> None:
    for c in connectors.MOUNT_CONNECTORS:
        _register_one(mcp, c)
