"""Middlewares FastMCP — application des préférences user au boot de session."""
from __future__ import annotations

import asyncio
import logging
import time

from fastmcp.server.middleware import Middleware
from fastmcp.server.transforms.visibility import disable_components

from . import access, db
from .auth_hooks import current_user_sub_from_token
from .tool_visibility import (
    DEFAULT_HIDDEN_TOOLS,
    effective_disabled,
    is_default_hidden,
    is_grant_only,
)

logger = logging.getLogger(__name__)

# Noms de tools grant-only vus lors d'un list_tools réussi. Sert de repli
# FAIL-CLOSED : si list_tools échoue au handshake, on réinjecte ces noms dans
# `all_names` pour qu'ils restent candidats au masquage (sinon la denylist
# fastmcp les laisserait visibles — défaut is_enabled=True). Backstop de
# listing ; l'autorisation d'APPEL est garantie indépendamment par
# access.require_namespace dans les tools à credential serveur (cf. mm).
_KNOWN_GRANT_ONLY: set[str] = set()

# Même repli pour les masqués-par-défaut par NAMESPACE (ex. attio) : leurs noms
# ne sont pas connaissables statiquement, on mémorise ceux vus au listing.
_KNOWN_DEFAULT_HIDDEN: set[str] = set()


def _result_error_text(result) -> str:
    """Extrait un message d'erreur lisible d'un résultat de tool `isError`."""
    content = getattr(result, "content", None)
    if content:
        for block in content:
            text = getattr(block, "text", None)
            if text:
                return str(text)[:500]
    return "tool error"


class CallMonitoringMiddleware(Middleware):
    """Journalise chaque appel de tool MCP dans `tool_call_log` (monitoring admin).

    Point d'interception unique : `on_call_tool` enveloppe l'exécution réelle,
    capture le `sub` JWT courant, le nom du tool, la durée et le statut
    (succès / échec + message d'erreur tronqué). Le logging est best-effort :
    une erreur d'écriture ne doit JAMAIS faire échouer l'appel ni en masquer
    l'exception métier. Lu par `/api/admin/monitoring/*`.
    """

    async def on_call_tool(self, context, call_next):
        tool_name = getattr(getattr(context, "message", None), "name", None) or "?"
        try:
            sub = current_user_sub_from_token()
        except Exception:
            sub = None
        start = time.perf_counter()
        ok = True
        error: str | None = None
        try:
            result = await call_next(context)
            # fastmcp peut convertir une exception de tool en résultat `isError`
            # plutôt que de la propager : couvrir les deux formes.
            is_err = getattr(result, "isError", None)
            if is_err is None:
                is_err = getattr(result, "is_error", None)
            if is_err:
                ok = False
                error = _result_error_text(result)
            return result
        except Exception as e:
            ok = False
            error = f"{type(e).__name__}: {e}"[:500]
            raise
        finally:
            duration_ms = int((time.perf_counter() - start) * 1000)
            try:
                # to_thread : l'INSERT PG (pool psycopg sync) ne doit pas
                # bloquer l'event loop sur le chemin chaud de chaque tool call.
                await asyncio.to_thread(db.record_tool_call, sub, tool_name, duration_ms, ok, error)
            except Exception as log_err:  # best-effort, jamais bloquant
                logger.warning("record_tool_call failed for %s: %s", tool_name, log_err)


class UserDisabledToolsMiddleware(Middleware):
    """Applique la visibilité des tools du user à sa session MCP.

    Au handshake `initialize`, pour le `sub` JWT courant, on calcule
    l'ensemble effectif des tools à masquer = `user_disabled_tools` ∪
    (tools masqués par défaut non explicitement activés). On pose une
    visibility rule session-scopée via `disable_components`. Le reste —
    filtrage `tools/list`, blocage `tools/call`, émission de
    `tools/list_changed` — est géré nativement par fastmcp.

    Pas de sub identifiable (stdio local, discovery non-authentifié) → on ne
    filtre rien : la machine du dev a accès complet, le masquage par défaut
    ne concerne que la surface multi-user authentifiée.
    """

    async def on_initialize(self, context, call_next):
        result = await call_next(context)
        try:
            sub = current_user_sub_from_token()
        except Exception:
            sub = None
        if not sub:
            return result
        ctx = context.fastmcp_context
        if ctx is None:
            logger.warning("fastmcp_context is None at on_initialize for sub=%s", sub)
            return result
        try:
            disabled = set(db.list_user_disabled_tools(sub))
            enabled_override = set(db.list_user_enabled_tools(sub))
            # Union grants per-user + entitlements de l'org active (source unique).
            granted = access.granted_namespaces_for(sub)
            is_admin = access.get_user_role(sub) == access.ADMIN
        except Exception as e:
            # FAIL-CLOSED : sur erreur DB, ne PAS révéler les namespaces grant-only.
            # granted=∅ + is_admin=False → is_tool_visible masque tout grant-only
            # (la visibilité est ergonomie, mais grant-only est une vraie barrière).
            logger.warning("Cannot read tool visibility for %s (fail-closed): %s", sub, e)
            disabled, enabled_override, granted, is_admin = set(), set(), frozenset(), False
        try:
            all_tools = await ctx.fastmcp.list_tools(run_middleware=False)
            all_names = {t.name for t in all_tools}
            _KNOWN_GRANT_ONLY.update(n for n in all_names if is_grant_only(n))
            _KNOWN_DEFAULT_HIDDEN.update(n for n in all_names if is_default_hidden(n))
        except Exception as e:
            logger.warning("Cannot list tools for %s: %s", sub, e)
            # repli FAIL-CLOSED : disabled explicites + masqués connus + tous les
            # grant-only déjà vus (sinon ils resteraient visibles, denylist
            # incomplète). Les noms inconnus de ce process restent couverts par
            # le backstop call-time (access.require_namespace).
            all_names = (
                disabled | DEFAULT_HIDDEN_TOOLS | _KNOWN_DEFAULT_HIDDEN | _KNOWN_GRANT_ONLY
            )
        to_hide = effective_disabled(all_names, disabled, enabled_override, granted, is_admin)
        if not to_hide:
            return result
        try:
            await disable_components(ctx, names=to_hide, components={"tool"})
        except Exception as e:
            logger.warning("Failed to apply tool visibility for %s: %s", sub, e)
        return result
