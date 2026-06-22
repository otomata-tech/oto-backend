"""Error tracking Sentry (SaaS) — init + capture des exceptions de tools MCP.

Deux surfaces d'erreur, deux mécanismes :
- **Routes REST `/api/*`** : l'intégration Starlette du SDK (auto-activée par
  `sentry-sdk[starlette]`) capture les 500 ASGI avec traceback complet. Rien à
  câbler ici — il suffit que `init_sentry()` tourne AVANT `mcp.http_app()`.
- **Tools MCP** : une exception de tool devient une erreur JSON-RPC en HTTP 200 →
  l'intégration Starlette ne la voit pas. `SentryToolErrorMiddleware` la capture là
  où l'exception est vivante (vrai traceback), puis re-raise (comportement inchangé).

Gaté par `OTO_SENTRY_DSN` : absent → `sentry_sdk` n'est jamais initialisé, tout
`capture_exception` est un no-op. Le serveur boote normalement sans Sentry.

RGPD : `send_default_pii=False` (pas d'IP/cookies/headers auto-collectés) et on
n'attache JAMAIS les arguments d'appel (emails, données entreprise) à l'event —
seulement le nom du tool + le `sub` Logto (id opaque pseudonyme, utile au debug).
"""
from __future__ import annotations

import logging
import os

import sentry_sdk
from fastmcp.server.middleware import Middleware

from .auth_hooks import current_user_sub_from_token

logger = logging.getLogger("oto_mcp")


def init_sentry() -> bool:
    """Initialise Sentry si `OTO_SENTRY_DSN` est posé. Retourne True si actif."""
    dsn = os.environ.get("OTO_SENTRY_DSN", "").strip()
    if not dsn:
        logger.info("Sentry désactivé (OTO_SENTRY_DSN absent)")
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("OTO_SENTRY_ENV", "production"),
        release=os.environ.get("OTO_SENTRY_RELEASE") or None,
        # RGPD : pas d'IP / cookies / headers auto-collectés.
        send_default_pii=False,
        # Tracing de perf désactivé par défaut (on cible l'error tracking).
        traces_sample_rate=float(os.environ.get("OTO_SENTRY_TRACES_SAMPLE_RATE", "0") or "0"),
    )
    logger.info("Sentry actif (env=%s)", os.environ.get("OTO_SENTRY_ENV", "production"))
    return True


class SentryToolErrorMiddleware(Middleware):
    """Capture les exceptions des tools MCP vers Sentry, puis re-raise.

    No-op si Sentry n'est pas initialisé (`capture_exception` ne fait rien). Sur le
    chemin nominal, ce middleware ne fait que déléguer — aucun surcoût.
    """

    async def on_call_tool(self, context, call_next):
        try:
            return await call_next(context)
        except Exception as e:
            try:
                with sentry_sdk.new_scope() as scope:
                    scope.set_tag("mcp.tool", context.message.name)
                    try:
                        sub = current_user_sub_from_token()
                    except Exception:
                        sub = None
                    if sub:
                        scope.set_user({"id": sub})
                    sentry_sdk.capture_exception(e)
            except Exception:
                # La capture ne doit jamais masquer l'erreur d'origine.
                pass
            raise
