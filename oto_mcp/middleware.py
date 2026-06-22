"""Middlewares FastMCP — application des préférences user au boot de session."""
from __future__ import annotations

import json
import logging

from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from .auth_hooks import current_user_sub_from_token
from .session_visibility import apply_session_visibility
from .tool_visibility import namespace_of

logger = logging.getLogger(__name__)


class UserDisabledToolsMiddleware(Middleware):
    """Applique la visibilité des tools du user à sa session MCP.

    Au handshake `initialize`, pour le `sub` JWT courant, on calcule l'ensemble
    effectif des tools à masquer = `user_disabled_tools` ∪ (masqués par défaut non
    activés) ∪ (connecteurs non activés/en pause) ∪ (gates admin/alpha) et on pose
    une visibility rule session-scopée. Le calcul + l'application vivent dans
    `session_visibility` (partagés avec le refresh à chaud post-`oto_use_org`,
    ADR 0009/0011/0015). fastmcp gère nativement filtrage `tools/list`, blocage
    `tools/call` et émission de `tools/list_changed`.

    Pas de sub identifiable (stdio local, discovery non-authentifié) → on ne filtre
    rien : la machine du dev a accès complet, le masquage par défaut ne concerne que
    la surface multi-user authentifiée.
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
        await apply_session_visibility(ctx, sub)
        return result


class FieldRedactionMiddleware(Middleware):
    """Redacte les champs sensibles du RÉSULTAT de tout tool, selon la politique de
    rédaction de l'org active (ADR 0009/0015, « la policy gouverne l'exposition »).

    Point d'application unique de la rédaction : remplace le filtrage qui vivait au
    niveau des clients (folk/silae/pennylane) et couvre désormais **tous** les
    connecteurs (unipile, ATS…) sans câblage par tool. La cascade (org → défaut
    serveur → vide) est résolue par `access.resolve_field_filter(<namespace>)` ;
    `FieldFilter` matche par nom de clé feuille, récursivement.

    Doit être enregistré **en dernier** (`add_middleware`) : l'exécution étant en
    ordre inverse, il enveloppe les autres et retouche le **résultat final**.

    Deux canaux à garder cohérents : un tool renvoie son dict en `structured_content`
    ET/OU en `content` (TextContent JSON). On redacte la donnée puis on réémet les
    deux depuis la version redactée — sinon un canal brut fuirait (Claude lit surtout
    `content`).

    **Fail-closed** : si l'application de la rédaction lève alors qu'une politique
    existe (ex. Faker absent pour `pseudonym`), on RETIENT la sortie plutôt que de
    laisser fuiter le brut. Une simple absence de policy (`is_empty`) = passe-through.
    """

    async def on_call_tool(self, context, call_next):
        result = await call_next(context)
        if getattr(result, "is_error", False):
            return result
        name = getattr(context.message, "name", "") or ""
        service = namespace_of(name)

        try:
            ff = _resolve_field_filter(service)
        except Exception:
            # Résolution de policy en échec (ex. DB) : on ne connaît pas la policy.
            # Pour un service à PII connu (défaut serveur déclaré) → fail-closed ;
            # sinon passe-through pour ne pas casser tous les tools sur un aléa DB.
            logger.exception("resolve_field_filter a échoué pour %s", name)
            if _service_has_server_default(service):
                return _withheld(name)
            return result

        if ff.is_empty:
            return result

        # Une politique de rédaction EXISTE pour ce service → fail-closed à partir d'ici.
        try:
            sc = getattr(result, "structured_content", None)
            if isinstance(sc, dict):
                red = ff.apply(sc)
                return self._rebuild(result, structured=red, payload=red)
            content = getattr(result, "content", None) or []
            block = content[0] if content else None
            text = getattr(block, "text", None)
            if isinstance(text, str):
                data = json.loads(text)
                if isinstance(data, (dict, list)):
                    red = ff.apply(data)
                    return self._rebuild(result, structured=sc, payload=red)
            # Rien de structuré à redacter (texte libre, binaire…) → inchangé.
            return result
        except Exception:
            logger.exception("rédaction de %s en échec — sortie retenue", name)
            return _withheld(name)

    @staticmethod
    def _rebuild(result, *, structured, payload) -> ToolResult:
        """Réémet le résultat avec `payload` redacté sur le canal texte, et
        `structured` sur le canal structuré (déjà redacté, ou laissé tel quel s'il
        était absent/non-dict)."""
        return ToolResult(
            content=[TextContent(type="text", text=json.dumps(payload, default=str))],
            structured_content=structured if isinstance(structured, dict) else None,
            meta=getattr(result, "meta", None),
            is_error=False,
        )


def _resolve_field_filter(service: str):
    # Import tardif : access importe des stores ; éviter un cycle au chargement module.
    from . import access
    return access.resolve_field_filter(service)


def _service_has_server_default(service: str) -> bool:
    from . import field_filter_defaults
    return service in field_filter_defaults.SERVER_DEFAULTS


def _withheld(name: str) -> ToolResult:
    return ToolResult(
        content=[TextContent(
            type="text",
            text=f"[oto] rédaction de « {name} » impossible — sortie retenue par sécurité.")],
        is_error=True,
    )
