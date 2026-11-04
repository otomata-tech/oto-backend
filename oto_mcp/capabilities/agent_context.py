"""Capacité « contexte agent » (issue otomata-private#49) — vue de transparence.

`GET /api/me/agent-context` rend, pour l'utilisateur courant, **exactement ce que
son Claude reçoit d'oto** au handshake, assemblé en 3 couches dérivées (zéro état
neuf) :

1. **instructions** — les instructions serveur statiques (`instructions.render()` :
   posture + bootstrap + boucle d'usage + catalogue de namespaces dérivé du registre).
2. **guide** — le guide d'org effectif (bundle session-start), via le handler
   canonique `orgs_instructions._get_guide` (réemploi, pas de duplication).
3. **tools** — les outils EFFECTIVEMENT visibles pour `(sub, org active)`, via la
   logique de visibilité canonique `session_visibility.compute_hidden_tools` (même
   calcul que le handshake MCP), regroupés par namespace.

REST-only : l'agent n'a pas besoin de s'appeler lui-même (il a déjà ce contexte) ;
la surface sert le dashboard. `SUB_ONLY` → chacun voit le sien.

`GET /api/orgs/{id}/context/preview` (#1194) rend la même composition pour un MEMBRE
TYPE d'une org/équipe, vue par qui la gouverne (org_admin ou opérateur).
"""
from __future__ import annotations

import logging
import types

from typing import Literal, Optional

from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import instructions as _instructions
from .. import group_store, org_store, providers, session_visibility, tool_registry
from ..connectors import activation as connector_activation
from .orgs import instructions as orgs_instructions
from ._authz import ORG_ADMIN_OF_OR_OPERATOR, SUB_ONLY
from ._types import AuthzDenied, Capability, DeclaredError, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)


class AgentContextInput(BaseModel):
    pass


def _namespace_of(tool_name: str) -> str:
    return tool_name.split("_", 1)[0]


class ContextLayer(BaseModel):
    """Une couche de l'artefact injecté, dans son ORDRE d'injection. `chars` est son
    poids : c'est le thermomètre de 0055-D5 — on mesure et on alerte, on n'ampute
    jamais. Les couches au corps vide sont omises."""
    key: str                                     # platform | catalog | … (bloc C)
    label: str
    body: str
    chars: int


class ToolsNamespace(BaseModel):
    namespace: str
    visible: int
    total: int


class ToolsView(BaseModel):
    """⚠️ `available: false` signifie que la vue n'a **pas pu être dérivée** (hors
    serveur, ou échec du calcul de visibilité) — ce n'est **pas** « aucun outil ».
    Un front qui l'affiche comme un zéro ment ; les compteurs sont alors absents."""
    available: bool
    total_visible: Optional[int] = None
    total_hidden: Optional[int] = None
    namespaces: Optional[list[ToolsNamespace]] = None


class AgentContextView(BaseModel):
    """Ce que le Claude de cet utilisateur reçoit vraiment — la vue de transparence."""
    org_id: Optional[int] = None
    # L'artefact EXACT injecté au handshake (blocs A + C concaténés).
    instructions: str
    # Le MÊME artefact, décomposé. Invariant tenu par `instructions.session_layers` :
    # `"\n\n".join(couches non vides) == instructions`.
    layers: list[ContextLayer]
    # Le guide d'org résolu, tel que le sert `oto_procedure op=get` — forme non
    # redéclarée ici pour ne pas en tenir deux copies. Servi sous ses DEUX noms le
    # temps du préavis (#519) ; `doctrine` s'en va le 30/11/2026.
    guide: dict
    doctrine: dict
    tools: ToolsView


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
    guide = await orgs_instructions._get_guide(
        ctx, types.SimpleNamespace(slug=None, scope=None, version=None,
                                   org_id=None, with_history=False))
    # Instructions RÉELLEMENT reçues = artefact composé A/C (#50), même chemin que
    # DynamicInstructionsMiddleware → la vue montre exactement ce que reçoit l'agent.
    # `layers` = le même artefact DÉCOMPOSÉ (pile de couches avec poids) pour la vue
    # anatomie de /context ; `instructions` (concaténé) reste pour compat.
    # SQL synchrone (`_resolve_context`) : `execute()` ne met PAS en thread le corps d'un
    # handler `async` — hors boucle explicitement (docs/event-loop-perf.md).
    layers = await run_in_threadpool(_instructions.session_layers, ctx.sub, ctx.org_id)
    return {
        "org_id": ctx.org_id,
        "instructions": "\n\n".join(l["body"] for l in layers if l["body"]),
        "layers": [{**l, "chars": len(l["body"])} for l in layers if l["body"]],
        "guide": guide,
        "doctrine": guide,   # ALIAS déprécié (retrait 30/11/2026, #519)
        "tools": await _tools_view(ctx),
    }


CAPABILITIES += [
    Capability(
        key="me.agent_context", handler=_agent_context, Input=AgentContextInput,
        authz=SUB_ONLY, Output=AgentContextView,
        description="The exact oto context this user's Claude receives: static server "
                    "instructions (posture + derived namespace catalog), effective org "
                    "guide, and the tools currently visible for the active org.",
        rest=RestBinding("GET", "/api/me/agent-context"),
    ),
]


# ── oto_context : rechargement PULL du contexte au changement de scope (call pt 1) ──
# Les instructions injectées sont FIGÉES au handshake (MCP n'a pas de « instructions
# changed »). Quand l'agent bascule d'org/équipe/projet en cours de session, la toolbox
# (tools/list_changed) et les credentials (résolution par appel, ADR 0038) suivent, mais
# le bloc C (readme org+équipe, guides, procédures) reste gelé. `oto_context` laisse
# l'agent TIRER ce bloc C frais pour le scope EFFECTIF — `ctx.org_id` respecte les jetons
# org=/project=/group= de l'appel. Focalisé (bloc C seul, pas le guide ni les tools) :
# c'est ce qui change au switch (leçon D1 : ne pas gonfler la sortie).
class ContextInput(BaseModel):
    pass


def _context(ctx: ResolvedCtx, inp: ContextInput) -> dict:
    from .. import access
    return {"org_id": ctx.org_id, "context": _instructions._block_c(ctx.sub, ctx.org_id),
            # #1499: the accounts of other orgs to NAME (lent to you, D1; your own key
            # elsewhere, #172) and the defaults that settle a tie (D3).
            "accounts": access.comptes_du_contexte(
                ctx.sub, ctx.org_id,
                access.current_group(ctx.sub) if ctx.org_id is not None else None)}


CAPABILITIES += [
    Capability(
        key="me.context", handler=_context, Input=ContextInput, authz=SUB_ONLY,
        # ⚠️ **La 1re ligne est un CONTRAT DE SÉLECTION** : c'est tout ce que certains
        # clients montrent au modèle au moment de choisir un outil. Phrase complète,
        # courte, autonome, impérative — le reste est du détail pour l'appel.
        #
        # Elle a dit « Reload YOUR contextual instructions » jusqu'au 2026-08-28, ce qui
        # se lisait comme un rechargement OPTIONNEL après coup, et annonçait un contexte
        # « frozen at connection time » — c'est-à-dire un mécanisme qu'on croyait fiable.
        # Il ne l'est pas : le champ `instructions` du handshake est tronqué à 2048
        # caractères par Claude Code et n'atteint pas le modèle sur claude.ai
        # (oto-backend#478). Cet outil est donc, en pratique, le SEUL canal sûr — d'où
        # l'impératif. Une description d'outil, elle, arrive toujours.
        description="CALL THIS FIRST, before any other oto tool — it carries your org's "
                    "working rules and guardrails.\n"
                    "Returns the contextual instructions for your CURRENT effective scope: "
                    "your org (its readme = its working rules; your role, active team and "
                    "connectors, recent projects and runs) and YOU — your profile card "
                    "(`accounts`: the accounts of other orgs you may NAME — lent to you, "
                    "with the lending org and your level, or your own key elsewhere — and "
                    "the defaults of your org and team, with who set them and when) "
                    "(`oto_profile`) and your own readme (`oto_guide scope=user "
                    "delivery=init`). No procedure index here: `oto_procedure op=list`.\n"
                    "⚠️ The same context is also injected at connection time, but do NOT "
                    "assume you received it: several clients truncate it, and some drop it "
                    "entirely. If you have not read it in this session, you are missing "
                    "rules your org expects you to follow — including when an action needs "
                    "human validation before it goes out.\n"
                    "Platform-wide usage rules live in the `notice` guide — read it once "
                    "per session too: `oto_guide op=read slug=notice`.\n"
                    "Call it again after you switch org/team/project — or pass "
                    "org=/project=/group= here: the toolbox and credentials follow the "
                    "switch, this prose does not.",
        mcp="oto_context",
    ),
]


# ── Aperçu d'un MEMBRE TYPE (#1194) — la page « Instructions de l'org » ─────────
# Ce que reçoit l'agent d'un membre d'une équipe, vu par qui la gouverne. Même
# composition que la session (`instructions.member_preview_layers`, même exposition que
# la visibilité : `connector_activation.exposed_for`), le sub en moins — d'où l'exclusion
# de ce qui ne vaut que pour une personne, dite dans la description.
class ContextPreviewInput(BaseModel):
    org_id: int
    group_id: Optional[int] = None


class PreviewLayer(BaseModel):
    """Une couche d'instructions, dans l'ORDRE où l'agent la reçoit, variables
    communes résolues. `platform` = le socle (celui du tenant qui héberge l'org s'il en
    a écrit un) ; `updated_at` null = le socle par défaut du code, jamais édité."""
    scope: Literal["platform", "org", "group"]
    body_md: str
    updated_at: Optional[str] = None


class PreviewConnector(BaseModel):
    connector: str
    label: str
    category: str


class ContextPreview(BaseModel):
    """Ce que reçoit l'agent d'un membre type de l'org (et de l'équipe `group_id`).
    Une couche au corps vide est ABSENTE (l'agent ne la reçoit pas). `connectors` =
    les connecteurs EXPOSÉS à ce membre (plafonds plateforme et tenant, override d'org,
    coupures de l'équipe), dans l'ordre du registre — pas ceux qu'il a installés."""
    org_id: int
    group_id: Optional[int] = None
    layers: list[PreviewLayer]
    connectors: list[PreviewConnector]


def _context_preview(ctx: ResolvedCtx, inp: ContextPreviewInput) -> dict:
    org = org_store.get_org(inp.org_id)
    if not org:
        raise AuthzDenied(404, "unknown_org", f"Org #{inp.org_id} inconnue.")
    group = None
    if inp.group_id is not None:
        group = group_store.get_group(inp.group_id)
        # Absente ou d'une autre org : même refus — l'aperçu ne dit pas qu'une équipe
        # existe ailleurs.
        if not group or int(group["org_id"]) != inp.org_id:
            raise AuthzDenied(404, "group_not_in_org",
                              f"Aucune équipe #{inp.group_id} dans l'org #{inp.org_id}.")
    exposed = connector_activation.exposed_for(inp.org_id, inp.group_id)
    return {
        "org_id": inp.org_id, "group_id": inp.group_id,
        "layers": _instructions.member_preview_layers(org, group),
        "connectors": [{"connector": name, "label": c.label, "category": c.category}
                       for name, c in providers.REGISTRY.items() if name in exposed],
    }


CAPABILITIES += [
    Capability(
        key="org.context.preview", handler=_context_preview, Input=ContextPreviewInput,
        authz=ORG_ADMIN_OF_OR_OPERATOR("org_id"), Output=ContextPreview,
        errors=(DeclaredError(404, "unknown_org", "the org does not exist"),
                DeclaredError(404, "group_not_in_org",
                              "`group_id` is not a team of this org")),
        description="What the agent of a TYPICAL member of the org (and of team "
                    "`group_id`, if given) receives: the instruction layers in delivery "
                    "order (platform base, org readme, team readme), shared variables "
                    "resolved ({{org}}, {{équipe}}, {{date}}), and the connectors exposed "
                    "to that member. Per-person parts are EXCLUDED: the 'your oto "
                    "context' section (role, keys, projects, runs), the profile card, "
                    "the personal note, the member's installed selection, and the "
                    "personal variables ({{user}}, {{rôle}}, {{connecteurs_actifs}}, "
                    "{{projets_récents}}), left as written. The namespace catalog is "
                    "not an instruction layer and is not returned. Org admin of this "
                    "org, or platform operator.",
        rest=RestBinding("GET", "/api/orgs/{id}/context/preview", {"id": "org_id"}),
    ),
]
