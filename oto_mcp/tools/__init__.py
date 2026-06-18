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

    # Méta-tools — pilotage de la visibility par l'user depuis la conversation.
    # Pas de dépendance externe, register en premier.
    from . import meta
    meta.register(mcp)

    # Meta-tools du palier organization (gestion orgs/membres/secrets +
    # switcher d'org active). Pas de dépendance externe non plus.
    from . import orgs
    orgs.register(mcp)

    # Harnais prospection « scout » (ADR 0008) — tools scout_* sur le substrat
    # factgraph, scopés à l'org active. Pas de dépendance externe.
    from . import scout
    scout.register(mcp)

    # Datastore (ADR 0016) — spine plateforme `data_*` sur substrat PG natif, plus
    # un connecteur Google. Chargé explicitement (comme meta/orgs/scout), donc hors
    # gate d'activation. Pas de dépendance externe.
    from . import datastore
    datastore.register(mcp)

    # Déroulés de doctrine (ADR 0017) — verbes doctrine_start/finish (spine). Le
    # run_id posé en état de session est stampé sur chaque tool_call par le sink
    # calllog. Pas de dépendance externe.
    from . import doctrine_run
    doctrine_run.register(mcp)

    # Connecteurs remote (bridges, ADR 0003) — middleware générique, zéro code
    # client : forward HTTP vers le bridge résolu depuis le credential d'org.
    from . import remote
    remote.register(mcp)

    # Connecteurs mount (fédération MCP, otomata#16) — monte un MCP distant via
    # proxy FastMCP, credential per-user injecté par requête. Inerte tant
    # qu'aucun connecteur kind="mount" n'est déclaré au registre (canari).
    from . import mount
    mount.register(mcp)

    # Connecteurs — chargement DÉRIVÉ DU REGISTRE (ADR 0010/0011, #24). Fin de la
    # liste hardcodée : pour chaque provider `kind="tools"`, on importe ses
    # modules `tools/<m>.py` (`Connector.modules`, défaut = le nom du provider) et
    # on appelle `register(mcp)`. Le registre `providers.py` est l'UNIQUE source.
    #
    # - `kind="mount"` (memento/planity) et `kind="remote"` sont EXCLUS : déjà
    #   gérés par mount.register / remote.register (génériques) ci-dessus.
    # - try/except par module (résilience uniforme) : un connecteur dont une dép
    #   optionnelle manque (oto-cli en retard, duckdb/o-browser absents, parquet
    #   introuvable…) se désactive en loggant un warning SANS faire tomber le
    #   serveur — exactement la classe du 502 qu'on élimine.
    # - L'exposition réelle reste gouvernée à la VISIBILITÉ par session
    #   (UserDisabledToolsMiddleware + connector_activation), pas au chargement.
    from .. import providers  # oto_mcp.providers (parent package, pas tools/)

    loaded: set[str] = set()
    for c in providers.REGISTRY.values():
        if c.kind != "tools":
            continue
        for mod_name in (c.modules or (c.name,)):
            if mod_name in loaded:
                continue
            loaded.add(mod_name)
            try:
                mod = __import__(f"oto_mcp.tools.{mod_name}", fromlist=[mod_name])
                mod.register(mcp)
            except Exception as e:
                log.warning("%s tools disabled: %s", mod_name, e)
