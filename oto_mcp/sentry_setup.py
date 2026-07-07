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

Sentry = défauts du CODE. Un **refus client amont** (4xx d'une API tierce : input
rejeté, credential invalide, cible absente) n'en est pas un — c'est une *erreur de
connecteur gérée*, déjà tracée dans le backlog `tool_calls` (calllog) et renvoyée à
l'agent en `ToolError`. On la classe **par type** (`UpstreamHTTPError` d'oto-core,
ou `httpx`/`requests` HTTPError, ou erreur connecteur typée portant un statut) et on
ne la reporte pas. Deux chemins de capture à neutraliser : le middleware explicite
(qui ne capture pas) et la LoggingIntegration sur le `logger.error` de fastmcp (le
`before_send` la droppe). Les 5xx et les vraies exceptions code restent reportées.
"""
from __future__ import annotations

import logging
import os

import sentry_sdk
from fastmcp.server.middleware import Middleware

from .auth_hooks import current_user_sub_from_token
# Classifieurs partagés (D2, #124) : source unique de la taxonomie d'exceptions,
# consommée aussi par `ErrorEnvelopeMiddleware`. Ré-exportés ici (les tests et le
# reste du module les référencent via `sentry_setup`).
from .error_taxonomy import (  # noqa: F401
    _USER_INPUT_CODES,
    _is_arg_validation_error,
    _is_expected_error,
    _is_managed_connector_error,
    _is_user_input_error,
    _upstream_status,
)

logger = logging.getLogger("oto_mcp")


def _before_send(event, hint):
    """Droppe les erreurs gérées — couvre aussi la copie LoggingIntegration."""
    exc_info = (hint or {}).get("exc_info")
    if exc_info and _is_expected_error(exc_info[1]):
        return None
    return event


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
        # Ne pas reporter les erreurs gérées (4xx amont + refus d'entrée/config
        # user McpError) : pas des bugs backend.
        before_send=_before_send,
    )
    logger.info("Sentry actif (env=%s)", os.environ.get("OTO_SENTRY_ENV", "production"))
    return True


class SentryToolErrorMiddleware(Middleware):
    """Capture les exceptions des tools MCP vers Sentry, puis re-raise.

    No-op si Sentry n'est pas initialisé (`capture_exception` ne fait rien). Sur le
    chemin nominal, ce middleware ne fait que déléguer — aucun surcoût. Une **erreur
    gérée** (4xx amont OU refus d'entrée/config user) n'est PAS capturée (cf. module).
    """

    async def on_call_tool(self, context, call_next):
        try:
            return await call_next(context)
        except Exception as e:
            if not _is_expected_error(e):
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
