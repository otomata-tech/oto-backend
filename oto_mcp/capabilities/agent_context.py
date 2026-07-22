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
        # Scope EXPLICITE sur l'org de la vue (ctx.org_id) — sinon compute_hidden_tools
        # re-dérive current_org(sub), qui dans ce chemin REST retombe sur la sélection
        # GLOBALE (org 0) et gonfle le compte à ~609 sur une org neuve vide (oto/#5.3).
        hidden = await session_visibility.compute_hidden_tools(
            types.SimpleNamespace(fastmcp=inst), ctx.sub, org=ctx.org_id)
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
    # Instructions RÉELLEMENT reçues = artefact composé A/C (#50), même chemin que
    # DynamicInstructionsMiddleware → la vue montre exactement ce que reçoit l'agent.
    return {
        "org_id": ctx.org_id,
        "instructions": _instructions.compose_session(ctx.sub, ctx.org_id),
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


# ── oto_context : rechargement PULL du contexte au changement de scope (call pt 1) ──
# Les instructions injectées sont FIGÉES au handshake (MCP n'a pas de « instructions
# changed »). Quand l'agent bascule d'org/équipe/projet en cours de session, la toolbox
# (tools/list_changed) et les credentials (résolution par appel, ADR 0038) suivent, mais
# le bloc C (readme org+équipe, guides, procédures) reste gelé. `oto_context` laisse
# l'agent TIRER ce bloc C frais pour le scope EFFECTIF — `ctx.org_id` respecte les jetons
# org=/project=/group= de l'appel. Focalisé (bloc C seul, pas la doctrine ni les tools) :
# c'est ce qui change au switch (leçon D1 : ne pas gonfler la sortie).
class ContextInput(BaseModel):
    pass


def _context(ctx: ResolvedCtx, inp: ContextInput) -> dict:
    return {"org_id": ctx.org_id, "context": _instructions._block_c(ctx.sub, ctx.org_id)}


CAPABILITIES += [
    Capability(
        key="me.context", handler=_context, Input=ContextInput, authz=SUB_ONLY,
        description="Reload YOUR contextual instructions for the CURRENT effective scope "
                    "(org + team agent-readme, guides index, procedures index, recent "
                    "projects/runs). Your injected context is frozen at connection time; "
                    "after you switch org/team/project — or pass org=/project=/group= on "
                    "this call — the toolbox and credentials follow but this prose does "
                    "NOT. Call it to act on the right org/team's knowledge, not the one "
                    "you connected under.",
        mcp="oto_context",
    ),
]
