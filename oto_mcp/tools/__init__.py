"""Registers all MCP tools on a FastMCP instance.

Each connector lives in its own module; importing it lazy keeps startup fast
and isolates failures (a missing API key for one connector doesn't kill the
whole server).
"""
from __future__ import annotations

from fastmcp import FastMCP


def register_all(mcp: FastMCP, *, include_mounts: bool = True) -> None:
    import logging

    log = logging.getLogger("oto_mcp.tools")

    # Méta-tools — pilotage de la visibility par l'user depuis la conversation.
    # Pas de dépendance externe, register en premier.
    from . import meta
    meta.register(mcp)

    # (La fiche « situation avec oto » — `oto_profile` — est une CAPACITÉ depuis le
    # 2026-07-28, montée par `_mcp_adapter` : plus de tool écrit à la main ici.
    # ADR 0042 §Convergence des surfaces, Décision 4.)

    # Whoami — identité MCP courante (compte × org active × groupe actif) servie à
    # l'agent pour savoir pour qui/dans quel contexte il agit. Spine, hors gate
    # d'activation, toujours visible (PROTECTED_TOOLS). Pas de dépendance externe.
    from . import whoami
    whoami.register(mcp)

    # (Les guides — `oto_guide` — sont une CAPACITÉ depuis le 2026-07-28, montée par
    # `_mcp_adapter`. Leur index per-(sub, org) enrichit toujours la description au
    # `tools/list` — `DynamicInstructionsMiddleware`, par NOM de tool.)

    # Email — envoi d'un message à contenu libre (rédigé par l'agent) via le mailer
    # Otomata. Brique d'onboarding piloté par l'agent (guide + datastore). Spine,
    # hors gate d'activation ; gaté super_admin dans le handler + masqué par défaut.
    from . import email
    email.register(mcp)

    # Le palier organization (orgs/membres/secrets/switch + guide/instructions)
    # est 100% migré en capacités (ADR 0009) — monté par `_mcp_adapter`/`_rest_adapter`
    # depuis `capabilities.registry`, plus aucun `tools/orgs.py`.

    # Datastore (ADR 0016) — spine plateforme `data_*` sur substrat PG natif, plus
    # un connecteur Google. Chargé explicitement (comme meta/orgs), donc hors
    # gate d'activation. Pas de dépendance externe.
    from . import datastore
    datastore.register(mcp)

    # Docs app — variante MCP App rendue d'`oto_doc` (lecture/parcours des pages d'un
    # projet + KB d'org). Spine, hors gate d'activation ; ne s'enregistre que si
    # l'extra prefab_ui est présent (import gardé dans le module).
    from . import docs_app
    docs_app.register(mcp)

    # Runs / déroulés (ADR 0017) — verbes run_start/finish (spine). Le run_id posé
    # en état de session est stampé sur chaque tool_call par le sink calllog. Pas
    # de dépendance externe.
    from . import guide_run
    guide_run.register(mcp)

    # Connecteurs mount (fédération MCP, otomata#16) — monte un MCP distant via
    # proxy FastMCP, credential per-user injecté par requête. Inerte tant
    # qu'aucun connecteur kind="mount" n'est déclaré au registre (canari).
    # `include_mounts=False` : le catalogue LOCAL sans aller chercher les
    # catalogues distants. Le fetch attend un tiers sans délai maximal propre
    # (oto-backend#892) ; il n'a sa place qu'au démarrage, jamais dans un import.
    if include_mounts:
        from . import mount
        mount.register(mcp)

    # Connecteurs — chargement DÉRIVÉ DU REGISTRE (ADR 0010/0011, #24). Fin de la
    # liste hardcodée : pour chaque provider `kind="tools"`, on importe ses
    # modules `tools/<m>.py` (`Connector.modules`, défaut = le nom du provider) et
    # on appelle `register(mcp)`. Le registre `providers/` est l'UNIQUE source.
    #
    # - `kind="mount"` (atlassian/planity) et `kind="remote"` sont EXCLUS : déjà
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
