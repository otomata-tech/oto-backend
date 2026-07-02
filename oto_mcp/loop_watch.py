"""Détection des callbacks qui BLOQUENT l'event loop (gel du serveur mono-loop).

Le serveur est mono-event-loop : un callback synchrone long (I/O bloquant dans un
`async def`, sérialisation géante, DNS…) gèle TOUTES les requêtes le temps de son
exécution — vu de l'extérieur, le service est down, sans crash ni exception, donc
invisible de Sentry et du calllog (rien ne finit → rien n'est loggé). Vécu
2026-07-02 : gel de ~7 min, aucun coupable identifiable a posteriori.

`aiodebug.log_slow_callbacks` = la détection native d'asyncio (debug mode) sans
son overhead : patch de `Handle._run` qui chronomètre chaque callback. Ici :
- tout callback ≥ `OTO_SLOW_CALLBACK_WARN` (déf. 1 s) → **warning journal** avec
  le nom de la coroutine (l'attribution du coupable) ;
- ≥ `OTO_SLOW_CALLBACK_SENTRY` (déf. 10 s) → **event Sentry** en plus (no-op si
  Sentry inactif).

Limite assumée : le log s'écrit à la FIN du callback — un deadlock infini ne
logge rien (là, py-spy à la main sur la box). Un gel qui finit est attribué.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("oto_mcp.loop")


def enable() -> None:
    """Active la surveillance. Appelé au boot (avant le démarrage de la loop —
    le patch est global). Fail-open : aiodebug absent → warning, le serveur boote."""
    try:
        from aiodebug import log_slow_callbacks
    except ImportError:
        logger.warning("aiodebug absent — détection des gels d'event loop désactivée")
        return
    warn_s = float(os.environ.get("OTO_SLOW_CALLBACK_WARN", "1.0") or "1.0")
    sentry_s = float(os.environ.get("OTO_SLOW_CALLBACK_SENTRY", "10.0") or "10.0")

    def on_slow(name: str, duration: float) -> None:
        logger.warning("event loop bloquée %.1fs par %s", duration, name)
        if duration >= sentry_s:
            try:
                import sentry_sdk
                sentry_sdk.capture_message(
                    f"event loop bloquée {duration:.1f}s par {name}", level="error")
            except Exception:  # la capture ne doit jamais casser la loop
                pass

    log_slow_callbacks.enable(warn_s, on_slow_callback=on_slow)
    logger.info("surveillance des gels d'event loop active (warn ≥%.1fs, Sentry ≥%.1fs)",
                warn_s, sentry_s)
