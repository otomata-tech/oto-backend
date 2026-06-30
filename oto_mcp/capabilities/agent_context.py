"""Capacité « contexte agent » (issue otomata-private#49) — vue de transparence.

`GET /api/me/agent-context` rend, pour l'utilisateur courant, **exactement ce que
son Claude reçoit d'oto** au handshake, assemblé en 3 couches dérivées (zéro état
neuf) :

1. **instructions** — les instructions serveur statiques (`instructions.render()` :
   posture + bootstrap + boucle d'usage + catalogue de namespaces dérivé du registre).
2. **doctrine** — la doctrine d'org effective (bundle session-start), via le handler
   canonique `orgs_instructions._get_doctrine` (réemploi, pas de duplication).
3. **tools** — les outils EFFECTIVEMENT visibles pour `(sub, org active)`, via la
   logique de visibilité canonique `session_visibility.compute_hidden_tools` (même
   calcul que le handshake MCP), regroupés par namespace.

REST-only : l'agent n'a pas besoin de s'appeler lui-même (il a déjà ce contexte) ;
la surface sert le dashboard. `SUB_ONLY` → chacun voit le sien.
"""
from __future__ import annotations

import logging
import types

from pydantic import BaseModel

from .. import db as _db
from .. import instructions as _instructions
from .. import session_visibility, tool_registry
from . import orgs_instructions
from ._authz import SUB_ONLY
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


class AgentContextInput(BaseModel):
    pass


def _namespace_of(tool_name: str) -> str:
    return tool_name.split("_", 1)[0]


async def _tools_view(ctx: ResolvedCtx) -> dict:
    """Outils visibles/masqués pour `(sub, org active)`, groupés par namespace.
    Réutilise `compute_hidden_tools` (logique de visibilité du handshake) via un
    shim portant l'instance FastMCP liée au boot."""
    inst = tool_registry.bound_instance()
    if inst is None:
        return {"available": False}   # hors serveur (tests) — pas d'instance liée
    try:
        all_tools = await inst.list_tools(run_middleware=False)
        hidden = await session_visibility.compute_hidden_tools(
            types.SimpleNamespace(fastmcp=inst), ctx.sub)
    except Exception as e:           # derive-only : on n'échoue pas la vue
        logger.warning("agent-context tools view failed for %s: %s", ctx.sub, e)
        return {"available": False}
    by_ns: dict[str, dict] = {}
    for t in all_tools:
        ns = _namespace_of(t.name)
        slot = by_ns.setdefault(ns, {"namespace": ns, "visible": 0, "total": 0})
        slot["total"] += 1
        if t.name not in hidden:
            slot["visible"] += 1
    namespaces = sorted(by_ns.values(), key=lambda s: s["namespace"])
    total_visible = sum(s["visible"] for s in namespaces)
    return {
        "available": True,
        "total_visible": total_visible,
        "total_hidden": len(all_tools) - total_visible,
        "namespaces": namespaces,
    }


async def _agent_context(ctx: ResolvedCtx, inp: AgentContextInput) -> dict:
    doctrine = await orgs_instructions._get_doctrine(
        ctx, types.SimpleNamespace(slug=None, scope=None, version=None,
                                   org_id=None, with_history=False))
    # Instructions RÉELLEMENT reçues = artefact composé A/B/C (#50), même chemin que
    # DynamicInstructionsMiddleware → la vue montre exactement ce que reçoit l'agent.
    try:
        onboarded = bool(_db.get_account_profile(ctx.sub).get("onboarded"))
    except Exception:
        onboarded = True
    return {
        "org_id": ctx.org_id,
        "instructions": _instructions.compose_session(
            ctx.sub, ctx.org_id, onboarded=onboarded),
        "doctrine": doctrine,
        "tools": await _tools_view(ctx),
    }


CAPABILITIES += [
    Capability(
        key="me.agent_context", handler=_agent_context, Input=AgentContextInput,
        authz=SUB_ONLY,
        description="The exact oto context this user's Claude receives: static server "
                    "instructions (posture + derived namespace catalog), effective org "
                    "doctrine, and the tools currently visible for the active org.",
        rest=RestBinding("GET", "/api/me/agent-context"),
    ),
]
