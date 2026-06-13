"""Connecteur universel de fédération MCP (otomata#16) — outils NATIFS.

Monte les outils d'un MCP distant `kind="mount"` (ex. memento) **nativement**
dans oto : `memento_mem_search`, `memento_mem_get`… avec leurs vrais schémas,
appelables directement (pas un tunnel describe/call).

Pourquoi PAS `FastMCPProxy.mount()` (incident 2026-06-13) : il ajoute un
`ProxyInitializeMiddleware` qui connecte+initialise le backend à CHAQUE
`initialize` d'oto → avec un token per-user absent au handshake, ça casse tout
le connecteur. On utilise donc `ProxyTool` **directement**, ajouté statiquement :

- **Catalogue figé au boot** : on récupère la liste d'outils du MCP distant UNE
  fois (réseau au boot, jamais au handshake), avec le token d'un user déjà
  connecté (le catalogue est partagé — mêmes verbes pour tous). `tools/list`
  d'oto reste 100% statique → handshake sûr.
- **Forward per-user à l'appel** : chaque `ProxyTool` a une `client_factory`
  appelée PAR APPEL → résout le token DU user courant (`resolve_mount_token`,
  refresh transparent) + gating `require_namespace`. Un user non connecté →
  McpError actionnable à l'appel, jamais un crash.

Limite v1 : le catalogue est figé au boot, donc (a) il faut ≥1 user connecté au
démarrage pour le charger, (b) un nouvel outil distant n'apparaît qu'après
restart. Acceptable pour le pilote ; un refresh lazy/admin viendra si besoin.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import os

from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.providers.proxy import ProxyTool
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import access, connectors, credentials_store
from ..auth_hooks import current_user_sub_from_token

log = logging.getLogger("oto_mcp.tools.mount")
CATALOG_TIMEOUT = 20


def _enabled_mounts() -> set[str]:
    """Activation explicite / kill-switch par env. `OTO_MCP_MOUNTS_ENABLED` (CSV)
    liste les mounts actifs ; vide → aucun (défaut sûr) ; `*` = tous."""
    raw = (os.environ.get("OTO_MCP_MOUNTS_ENABLED") or "").strip()
    if raw == "*":
        return {c.name for c in connectors.MOUNT_CONNECTORS}
    return {n.strip() for n in raw.split(",") if n.strip()}


def _run_sync(coro):
    """Exécute une coroutine dans un loop propre (thread dédié) — sûr depuis le
    contexte sync de register_all, qu'un event loop tourne ou non."""
    with concurrent.futures.ThreadPoolExecutor(1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def _catalog_token(connector: connectors.Connector, sub: str) -> str | None:
    """Token (d'un user connecté) pour récupérer le catalogue PARTAGÉ au boot.
    Connector-spécifique pour le refresh OAuth (memento)."""
    if connector.name == "memento":
        from .. import memento_oauth
        return memento_oauth.access_token_for(sub)
    return credentials_store.get_credential("user", sub, connector.name)


def _fetch_catalog(connector: connectors.Connector) -> list:
    """Liste d'outils du MCP distant, récupérée une fois au boot via le token
    d'un user déjà connecté. [] si personne n'est connecté ou si l'appel échoue
    (dégradation propre : pas d'outils fédérés, le reste d'oto intact)."""
    sub = credentials_store.first_entity_with("user", connector.name)
    if not sub:
        log.info("mount %s : aucun user connecté → catalogue non chargé "
                 "(restart après le 1er connect)", connector.name)
        return []
    try:
        token = _catalog_token(connector, sub)
        if not token:
            return []

        async def _list():
            client = Client(StreamableHttpTransport(
                connector.mount_url, headers={"Authorization": f"Bearer {token}"}))
            async with client:
                return await client.list_tools()

        return _run_sync(asyncio.wait_for(_list(), CATALOG_TIMEOUT))
    except Exception as e:
        log.warning("mount %s : échec du fetch catalogue (%s) → 0 outil fédéré",
                    connector.name, e)
        return []


def _make_factory(connector: connectors.Connector):
    """client_factory per-appel : token DU user courant, gating au call-time."""
    ns = connector.namespaces[0]

    async def factory() -> Client:
        access.require_namespace(ns)
        if current_user_sub_from_token() is None:
            raise McpError(ErrorData(
                code=INVALID_PARAMS,
                message=(f"Connecteur fédéré `{connector.name}` indisponible en "
                         f"stdio local (credential per-user serveur requis)."),
            ))
        token = access.resolve_mount_token(connector.name)  # lève si non connecté
        return Client(StreamableHttpTransport(
            connector.mount_url, headers={"Authorization": f"Bearer {token}"}))

    return factory


def _register_one(mcp: FastMCP, c: connectors.Connector) -> None:
    if not c.mount_url:
        log.warning("mount connector %s sans mount_url — ignoré", c.name)
        return
    ns = c.namespaces[0]
    catalog = _fetch_catalog(c)
    if not catalog:
        return
    factory = _make_factory(c)
    n = 0
    for t in catalog:
        # ProxyTool natif (vrai schéma) renommé <ns>_<nom> → namespace gouverné.
        # model_copy préserve le nom backend pour le forward (cf. ProxyTool).
        pt = ProxyTool.from_mcp_tool(factory, t).model_copy(update={"name": f"{ns}_{t.name}"})
        mcp.add_tool(pt)
        n += 1
    log.info("federated MCP %s → %d outils natifs montés (namespace %s)", c.name, n, ns)


def register(mcp: FastMCP) -> None:
    enabled = _enabled_mounts()
    for c in connectors.MOUNT_CONNECTORS:
        if c.name not in enabled:
            log.info("mount %s déclaré mais non activé (OTO_MCP_MOUNTS_ENABLED)", c.name)
            continue
        _register_one(mcp, c)
