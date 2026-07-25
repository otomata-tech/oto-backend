"""Agent SERVER-SIDE d'un projet — capacité `oto_agent` (ADR 0009 / 0047).

Le projet publié avait déjà deux faces (UI navigable `share_ui`, endpoint MCP
`subdomain_project`). Celle-ci en ajoute une troisième : **oto fait tourner la
boucle de tool calling** au lieu d'attendre que le visiteur branche l'endpoint
dans SON client. Deux surfaces pour le même moteur (`agent_runtime`) :

- **publique / sans login** — `POST /agent` du sous-domaine du projet, servi par
  `subdomain_project` sous le contexte anonyme (credentials de l'org propriétaire).
- **authentifiée** — CETTE capacité (`oto_agent` en MCP, `POST /api/me/agent` en
  REST) : la boucle tourne sous l'identité de l'APPELANT, donc sous SES
  credentials, SON org, SES gates. Sert le dashboard (« essayer l'agent avant de
  publier ») et la délégation depuis un autre agent.

Un tool par objet, verbe en `op` (ADR 0047) : `run` / `configure` / `status`.
Les paliers d'autz divergent par op → gate explicite dans le handler contre
`ownership` (`can_access` pour lire/exécuter, `can_govern` pour régler), même
patron que `oto_project`.

**Aucun périmètre d'outils neuf** : l'allowlist par défaut est celle du preset MCP
du projet (`mcp_tools`) ; un appelant authentifié peut la RESTREINDRE pour un run
(`tools=`), jamais l'élargir — l'intersection est prise, fail-closed.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel

from .. import agent_llm, agent_runtime, db, ownership
from ._authz import ORG_MEMBER
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

RTYPE = "project"


class AgentInput(BaseModel):
    op: Literal["run", "configure", "status"] = "run"
    project_id: int
    # run
    message: Optional[str] = None
    messages: Optional[list] = None   # fil rendu au tour précédent, rejoué tel quel
    tools: Optional[list[str]] = None  # SOUS-ensemble du preset (jamais un élargissement)
    # configure
    enabled: Optional[bool] = None
    prompt_md: Optional[str] = None
    max_steps: Optional[int] = None


def _require(cond, code: str, msg: str, status: int = 400) -> None:
    if not cond:
        raise AuthzDenied(status, code, msg)


def _project(ctx: ResolvedCtx, inp: AgentInput) -> dict:
    row = db.get_project_by_id(int(inp.project_id))
    _require(row is not None, "not_found", "Projet introuvable.", 404)
    return row


def _allowlist(row: dict, asked: Optional[list[str]]) -> frozenset:
    """Outils du run. Défaut = le preset du projet ; `asked` ne peut que RÉTRÉCIR
    (intersection) — un appelant ne s'ouvre jamais un outil que le projet n'expose
    pas, même s'il y aurait droit par ailleurs (le projet reste le périmètre)."""
    preset = frozenset(row.get("mcp_tools") or ())
    if not asked:
        return preset
    return preset & frozenset(t for t in asked if t)


async def _agent(ctx: ResolvedCtx, inp: AgentInput) -> dict:
    row = _project(ctx, inp)
    pid = str(row["id"])

    if inp.op == "status":
        _require(ownership.can_access(ctx.sub, RTYPE, pid, want="read"),
                 "forbidden", "Accès refusé à ce projet.", 403)
        return {
            "project_id": row["id"],
            "enabled": bool(row.get("agent_enabled")),
            # « Disponible » = le déploiement porte le substrat LLM. Distinct de
            # `enabled` (choix de l'auteur) → le front rend les deux, sans deviner.
            "available": agent_runtime.available(),
            "model": agent_llm.model() if agent_runtime.available() else None,
            "max_steps": row.get("agent_max_steps"),
            "prompt_md": row.get("agent_prompt_md") or "",
            "tools": list(row.get("mcp_tools") or []),
        }

    if inp.op == "configure":
        _require(ownership.can_govern(ctx.sub, RTYPE, pid),
                 "forbidden", "Réservé à qui gouverne ce projet.", 403)
        _require(inp.enabled is not None or inp.prompt_md is not None
                 or inp.max_steps is not None, "nothing_to_set",
                 "Rien à régler : passer enabled, prompt_md ou max_steps.", 400)
        enabled = (bool(row.get("agent_enabled")) if inp.enabled is None
                   else bool(inp.enabled))
        db.set_project_agent(int(row["id"]), enabled=enabled,
                             prompt_md=inp.prompt_md, max_steps=inp.max_steps)
        db.log_project_activity(int(row["id"]), ctx.sub, "project.agent",
                                "on" if enabled else "off")
        fresh = db.get_project_by_id(int(row["id"])) or row
        out = {"project_id": fresh["id"], "enabled": bool(fresh.get("agent_enabled")),
               "max_steps": fresh.get("agent_max_steps"),
               "prompt_md": fresh.get("agent_prompt_md") or "",
               "available": agent_runtime.available()}
        if enabled and not out["available"]:
            out["warning"] = ("Agent activé mais le substrat LLM n'est pas configuré "
                              "sur ce déploiement — la surface reste inerte.")
        if enabled and not (fresh.get("mcp_tools") or []):
            out["warning_tools"] = ("Aucun outil dans le preset du projet : l'agent "
                                    "répondra sans pouvoir agir. Publie un toolset "
                                    "(oto_project op=publish_mcp mcp_tools=[…]).")
        return out

    # op == "run"
    _require(ownership.can_access(ctx.sub, RTYPE, pid, want="read"),
             "forbidden", "Accès refusé à ce projet.", 403)
    _require(agent_runtime.available(), "agent_unavailable",
             "Le substrat LLM n'est pas configuré sur ce déploiement.", 503)
    message = (inp.message or "").strip()
    _require(bool(message), "empty_message", "`message` requis.", 400)
    tools = _allowlist(row, inp.tools)
    _require(bool(tools), "no_tools",
             "Ce projet n'expose aucun outil : publie un toolset "
             "(oto_project op=publish_mcp mcp_tools=[…]) avant de faire tourner l'agent.",
             400)
    spec = agent_runtime.project_spec(row, tools)
    result = await agent_runtime.run(spec, message[:8000], history=inp.messages)
    out = result.as_dict()
    out["messages"] = result.messages
    out["project_id"] = row["id"]
    return out


CAPABILITIES += [
    Capability(
        key="me.agent", handler=_agent, Input=AgentInput, authz=ORG_MEMBER,
        mcp="oto_agent",
        description=(
            "Run a project's SERVER-SIDE agent — oto executes the tool-calling loop "
            "itself instead of you wiring the project's MCP endpoint into a client. "
            "op=run (project_id + message, optional `messages` = the thread returned by "
            "the previous turn — replay it verbatim, there is NO server-side session) "
            "executes the loop under YOUR identity: your credentials, your org, your "
            "call-time gates, and the project's tool allowlist (`mcp_tools`). Optional "
            "`tools` can only NARROW that allowlist, never widen it. Returns the reply, "
            "the tools it used (`steps`), why it stopped, and the thread to replay. "
            "op=configure (can_govern) turns the agent on/off for the project and sets "
            "`prompt_md` (author instructions prepended to the agent's system prompt) "
            "and `max_steps` (tool rounds per request, 1-12) — once ON, a PUBLISHED "
            "project also answers visitors on its public page with no login, spending "
            "the OWNER ORG's credentials, so enable it deliberately. op=status reports "
            "enabled/available/model/tools without running anything."),
        rest=RestBinding("POST", "/api/me/agent"),
    ),
]
