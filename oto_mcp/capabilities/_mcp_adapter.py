"""Adaptateur MCP de la couche capacité (ADR 0009).

Boucle sur le registre et monte un tool FastMCP par capacité ayant un binding
`mcp`. Chaque tool applique, avant le handler : validation `Input` → autz →
handler. L'`AuthzDenied` neutre est traduit en `McpError`. Le schéma du tool
est aplati (params plats) via `apply_flat_signature`.

Dépend du core (sens unique ADR 0004) ; le core n'importe pas cet adaptateur.
"""
from __future__ import annotations

import inspect
import logging
from typing import Optional

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_context
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, INVALID_PARAMS

from .. import session_org
from ..auth_hooks import current_user_sub_from_token
from ..session_visibility import apply_session_visibility
from ._types import AuthzDenied, Capability, RawCtx, apply_flat_signature

logger = logging.getLogger(__name__)


def _org_echo(org_id: int) -> dict:
    """Écho de l'org effective (`_org`) dans les payloads MCP org-sensibles.

    Lève l'ambiguïté « sous quelle org ai-je agi ? » après un `oto_use_org` : le
    client voit l'org résolue par le serveur à CHAQUE réponse, sans avoir à la
    déduire. Best-effort (le nom peut manquer, jamais l'id)."""
    try:
        from .. import org_store
        org = org_store.get_org(org_id)
        return {"id": org_id, "name": (org or {}).get("name")}
    except Exception:
        return {"id": org_id}


def _org_param_reserved(cap: Capability) -> bool:
    """`org=` est un axe-contexte RÉSERVÉ pour cette cap ssi elle NE déclare PAS déjà un
    champ `org` métier (ex. `UseOrgInput.org` = l'org CIBLE d'`oto_use_org`, pas le contexte
    d'appel) : ces caps « possèdent » le nom, l'axe-contexte n'y a pas de sens."""
    return cap.mcp is not None and "org" not in cap.Input.model_fields


def reserved_org_tool_names(capabilities: list[Capability]) -> frozenset:
    """Noms des tools MCP où `org=` est injecté comme axe-contexte → pilote
    `CallContextMiddleware` (pose la ContextVar `_CALL_ORG` autour de toute la chaîne)."""
    return frozenset(cap.mcp for cap in capabilities if _org_param_reserved(cap))


def _make_tool(cap: Capability):
    async def _tool(**kwargs):
        # `org=` (axe-contexte, modèle sans état de session) est posé EN AMONT par
        # `CallContextMiddleware` (ContextVar per-appel, lue par `current_org`) → ici on
        # le retire simplement des kwargs pour ne pas le passer à l'`Input` de la capacité.
        kwargs.pop("org", None)
        raw = RawCtx(sub=current_user_sub_from_token())
        try:
            inp = cap.Input(**kwargs)                 # validation (seule source : Input)
            ctx = cap.authz(raw, inp)                 # autz (peut lire inp pour ORG_ADMIN_OF)
            result = cap.handler(ctx, inp)            # handler core
            if inspect.isawaitable(result):           # handler async (ex. doctrine + manifeste)
                result = await result
        except AuthzDenied as d:
            raise McpError(ErrorData(code=INVALID_PARAMS, message=d.message or d.code))
        if isinstance(result, dict) and ctx.org_id is not None:
            # Org EFFECTIVE APRÈS le handler : un `oto_use_org` vient peut-être de
            # basculer l'override de session → `ctx.org_id`, résolu à l'autz AVANT le
            # handler, est périmé et échoerait l'org d'AVANT le switch (#110 : réponse
            # `{active_org: 83, _org: {id: 2}}`). On relit `current_org` post-handler
            # (la ContextVar `_CALL_ORG` vit encore — le middleware reset APRÈS) ;
            # repli sur `ctx.org_id` si non résoluble (perso/clear).
            from .. import access
            eff = access.current_org(raw.sub) if raw.sub else None
            result.setdefault("_org", _org_echo(eff if eff is not None else ctx.org_id))
        if cap.refresh_visibility and raw.sub:
            # Bascule de profil (org/groupe actif déjà commitée par le handler) →
            # re-pousse la denylist de la NOUVELLE org sur la session MCP courante,
            # émettant tools/list_changed. Best-effort : un échec de refresh ne doit
            # pas faire échouer la bascule (la prochaine session corrigera).
            try:
                await apply_session_visibility(get_context(), raw.sub, reset=True)
            except Exception:
                logger.warning("refresh_visibility post-hook failed for %s/%s",
                               cap.key, raw.sub, exc_info=True)
        return result
    _tool.__name__ = cap.mcp
    _tool.__doc__ = cap.description or cap.key
    tool = apply_flat_signature(_tool, cap.Input)
    # Paramètre commun `org=` (axe-contexte per-appel) ajouté au schéma plat SANS toucher
    # l'`Input` de chaque capacité. Prime sur l'org maison, robuste au reset/absence de
    # session (claude.ai) ; inerte pour les caps non org-scopées. (`project=`/`run_id=`
    # suivront en passe profonde.) La pose/garde de la ContextVar vit dans le middleware.
    if _org_param_reserved(cap):
        sig = tool.__signature__
        extra = inspect.Parameter("org", inspect.Parameter.KEYWORD_ONLY,
                                  annotation=Optional[int], default=None)
        tool.__signature__ = sig.replace(parameters=[*sig.parameters.values(), extra])
        tool.__annotations__["org"] = Optional[int]
    return tool


def register(instance: FastMCP, capabilities: list[Capability]) -> None:
    """Monte un tool par capacité MCP. No-op si la liste est vide (canari)."""
    for cap in capabilities:
        if cap.mcp is None:
            continue
        instance.tool(name=cap.mcp, description=cap.description or None)(_make_tool(cap))
