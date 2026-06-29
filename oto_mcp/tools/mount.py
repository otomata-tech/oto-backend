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

# Noms d'outils fédérés actuellement enregistrés, par connecteur — pour pouvoir
# les retirer/re-poser au refresh à chaud (oto_admin_refresh_mount) sans restart.
_REGISTERED: dict[str, set[str]] = {}


# Fédération MCP systématique (otomata#16) : memento est monté **d'office** —
# c'est la base de connaissance commune de l'écosystème, pas une intégration
# client. Les autres mounts (ex. planity, client-spécifique) restent opt-in via
# `OTO_MCP_MOUNTS_ENABLED`.
_DEFAULT_ENABLED_MOUNTS = frozenset({"memento"})


def _db_activated_mounts() -> set[str]:
    """Mounts dont l'exposition est activée en DB (`connector_activation`) — master
    global OU n'importe quel override d'org ON. Best-effort (DB indispo au boot →
    set vide, le reste d'oto intact). C'est le cran « la DB gouverne l'exposition »
    (ADR 0010/0011) appliqué au montage : activer un connecteur fédéré au catalogue
    le **monte** aussi, sans toucher à `OTO_MCP_MOUNTS_ENABLED`."""
    try:
        from .. import connector_activation
        on = {r["connector"] for r in connector_activation.list_activations() if r.get("enabled")}
        return {c.name for c in connectors.MOUNT_CONNECTORS if c.name in on}
    except Exception as e:
        log.warning("mount : lecture connector_activation échouée (%s) → 0 mount DB", e)
        return set()


def _enabled_mounts() -> set[str]:
    """Mounts actifs = base (env) ∪ mounts activés en DB. `OTO_MCP_MOUNTS_ENABLED` :
    - **non défini** → défaut systématique (`_DEFAULT_ENABLED_MOUNTS`, càd memento) ;
    - `*`           → tous les mounts déclarés (DB ignorée — c'est déjà tout) ;
    - `""` (vide)   → kill-switch ABSOLU (aucun, DB ignorée) ;
    - CSV           → ceux listés, PLUS les activés en DB.
    Le `∪ DB` fait qu'(dés)activer un fédéré au catalogue suffit à le monter — zéro env."""
    raw = os.environ.get("OTO_MCP_MOUNTS_ENABLED")
    if raw is not None and raw.strip() == "":
        return set()  # kill-switch absolu
    if raw is not None and raw.strip() == "*":
        return {c.name for c in connectors.MOUNT_CONNECTORS}
    if raw is None:
        base = {c.name for c in connectors.MOUNT_CONNECTORS if c.name in _DEFAULT_ENABLED_MOUNTS}
    else:
        base = {n.strip() for n in raw.split(",") if n.strip()}
    return base | _db_activated_mounts()


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
    if connector.name == "atlassian":
        from .. import atlassian_oauth
        return atlassian_oauth.access_token_for(sub)
    return credentials_store.get_credential("user", sub, connector.name)


def _service_catalog(connector: connectors.Connector) -> list | None:
    """Catalogue via un endpoint **service-à-service** (secret partagé oto↔distant),
    PAS un token OAuth user — fix durable (otomata#16) : le boot ne dépend plus d'un
    token personnel révocable, ni du dual-sub. Le catalogue est product-level
    (mêmes outils pour tous) → un credential de service stable suffit.

    None si non configuré pour ce connecteur (→ fallback au token user). Renvoie des
    `mcp.types.Tool` (même type que `client.list_tools()`), pour `_register_one`."""
    if connector.name != "memento":
        return None
    url = os.environ.get("MEMENTO_FEDERATION_CATALOG_URL")
    secret = os.environ.get("MEMENTO_FEDERATION_SECRET")
    if not url or not secret:
        return None
    import requests
    from mcp.types import Tool
    r = requests.get(url, headers={"Authorization": f"Bearer {secret}"},
                     timeout=CATALOG_TIMEOUT)
    r.raise_for_status()
    tools = (r.json() or {}).get("tools", [])
    return [Tool(name=t["name"], description=t.get("description"),
                 inputSchema=t.get("inputSchema") or {"type": "object"})
            for t in tools]


def _fetch_catalog(connector: connectors.Connector) -> list:
    """Liste d'outils du MCP distant, récupérée une fois au boot via le token
    d'un user déjà connecté. [] si personne n'est connecté ou si l'appel échoue
    (dégradation propre : pas d'outils fédérés, le reste d'oto intact)."""
    # Dégradation propre TOTALE : tout (y compris le lookup DB du compte désigné)
    # est sous try/except — une DB momentanément indispo au boot (warmup du pool)
    # ne doit JAMAIS crasher le register (sinon 502). [] = pas d'outils fédérés,
    # le reste d'oto intact ; un restart après stabilisation re-fetchera.
    try:
        # Voie durable (otomata#16) : catalogue via endpoint service (secret partagé),
        # sans token user. Si configuré (memento + env), on s'en tient là.
        svc = _service_catalog(connector)
        if svc is not None:
            log.info("mount %s : catalogue via endpoint service (%d outils)",
                     connector.name, len(svc))
            return svc
        # Fallback historique : token OAuth d'un user connecté (compte désigné admin).
        sub = credentials_store.first_entity_with(
            "user", connector.name, prefer=os.environ.get("OTO_MCP_ADMIN_SUB"))
        if not sub:
            log.info("mount %s : aucun user connecté → catalogue non chargé "
                     "(restart après le 1er connect)", connector.name)
            return []
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
        # Garde d'accès = la résolution du token : `resolve_mount_token` lève si le
        # user n'a pas connecté son compte fédéré (OAuth per-user). Plus de
        # `require_namespace` (relicat grant-only, ADR 0031).
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


def _register_one(mcp: FastMCP, c: connectors.Connector) -> int:
    if not c.mount_url:
        log.warning("mount connector %s sans mount_url — ignoré", c.name)
        return 0
    ns = c.namespaces[0]
    catalog = _fetch_catalog(c)
    if not catalog:
        return 0
    factory = _make_factory(c)
    names: set[str] = set()
    for t in catalog:
        # ProxyTool natif (vrai schéma) renommé <ns>_<nom> → namespace gouverné.
        # model_copy préserve le nom backend pour le forward (cf. ProxyTool).
        fq = f"{ns}_{t.name}"
        mcp.add_tool(ProxyTool.from_mcp_tool(factory, t).model_copy(update={"name": fq}))
        names.add(fq)
    _REGISTERED[c.name] = names
    log.info("federated MCP %s → %d outils natifs montés (namespace %s)", c.name, len(names), ns)
    return len(names)


def refresh(mcp: FastMCP, connector_name: str) -> dict:
    """Re-fetch le catalogue d'un MCP fédéré et re-enregistre ses outils À CHAUD
    (sans restart). Retire d'abord les outils précédemment montés, puis re-pose
    depuis le catalogue frais. Un nouvel outil distant devient visible au prochain
    `tools/list` des clients. Réservé admin (gating dans le méta-tool appelant)."""
    c = connectors.REGISTRY.get(connector_name)
    if c is None or c.kind != "mount":
        raise ValueError(f"`{connector_name}` n'est pas un connecteur fédéré (kind=mount)")
    if connector_name not in _enabled_mounts():
        raise ValueError(f"mount `{connector_name}` non activé (OTO_MCP_MOUNTS_ENABLED)")
    before = set(_REGISTERED.get(connector_name, set()))
    for name in before:
        try:
            mcp.local_provider.remove_tool(name)  # mcp.remove_tool déprécié
        except Exception as e:
            log.warning("refresh %s : remove_tool %s a échoué (%s)", connector_name, name, e)
    _REGISTERED[connector_name] = set()
    _register_one(mcp, c)
    after = set(_REGISTERED.get(connector_name, set()))
    return {
        "connector": connector_name,
        "total": len(after),
        "added": sorted(after - before),
        "removed": sorted(before - after),
    }


def register(mcp: FastMCP) -> None:
    enabled = _enabled_mounts()
    active = False
    for c in connectors.MOUNT_CONNECTORS:
        if c.name not in enabled:
            log.info("mount %s déclaré mais non activé (OTO_MCP_MOUNTS_ENABLED)", c.name)
            continue
        _register_one(mcp, c)
        active = True

    if not active:
        return

    @mcp.tool(
        name="oto_admin_refresh_mount",
        description=(
            "[admin] Re-fetch le catalogue d'un MCP fédéré et re-enregistre ses "
            "outils à chaud, sans restart du serveur (pour qu'un nouvel outil du "
            "MCP distant apparaisse). `connector` = nom du connecteur mount (ex. "
            "`memento`). Réservé aux admins plateforme."
        ),
    )
    async def refresh_mount(connector: str) -> dict:
        import asyncio as _asyncio
        sub = current_user_sub_from_token()
        if sub is None or not access.is_platform_operator(sub):
            raise McpError(ErrorData(code=INVALID_PARAMS,
                                     message="Réservé aux admins plateforme."))
        try:
            return await _asyncio.to_thread(refresh, mcp, connector)
        except ValueError as e:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=str(e)))
