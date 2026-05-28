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

    # Connecteurs API-only — la résolution de clé (user vs platform) se fait
    # par appel via `access.resolve_api_key`, pas au register. Pas besoin que
    # les secrets soient configurés au boot.
    from . import fr, serper, hunter, attio
    fr.register(mcp)
    serper.register(mcp)
    hunter.register(mcp)
    attio.register(mcp)

    # Connecteurs récents — wrapper en try/except au cas où la version d'oto-cli
    # déployée serait en retard sur le module attendu.
    for mod_name in ("reddit", "lemlist", "culture", "kaspr", "fullenrich", "dvf", "pennylane"):
        try:
            mod = __import__(f"oto_mcp.tools.{mod_name}", fromlist=[mod_name])
            mod.register(mcp)
        except Exception as e:
            log.warning("%s tools disabled: %s", mod_name, e)

    # Browser — optionnel : si o-browser ou patchright manquent, on log et on
    # continue sans LinkedIn plutôt que de cracher tout le MCP.
    try:
        from . import linkedin
        linkedin.register(mcp)
    except Exception as e:
        log.warning("LinkedIn tools disabled: %s", e)

    try:
        from . import crunchbase
        crunchbase.register(mcp)
    except Exception as e:
        log.warning("Crunchbase tools disabled: %s", e)

    # WhatsApp — Baileys via Node.js subprocess. Réservé aux admins (pairing
    # QR manuel pour l'instant). Gracefully disabled si Node manque ou si
    # l'install npm pose problème.
    try:
        from . import whatsapp
        whatsapp.register(mcp)
    except Exception as e:
        log.warning("WhatsApp tools disabled: %s", e)

    # Slack — platform bot token (SLACK_BOT_TOKEN). Disabled si le secret manque.
    try:
        from . import slack
        slack.register(mcp)
    except Exception as e:
        log.warning("Slack tools disabled: %s", e)

    # Datastore — Google Sheets per-user. Requiert Google OAuth grant côté
    # user (`/account`). On register quoi qu'il en soit ; les tools lèvent
    # une McpError actionnable à l'appel si l'user n'a pas grant.
    try:
        from . import datastore
        datastore.register(mcp)
    except Exception as e:
        log.warning("Datastore tools disabled: %s", e)

    # SIRENE stock — DuckDB sur parquet INSEE local. Gracefully disabled si
    # duckdb manque ou parquet introuvable (on log à l'appel, pas au register).
    try:
        from . import sirene_stock
        sirene_stock.register(mcp)
    except Exception as e:
        log.warning("SIRENE stock tools disabled: %s", e)
