"""Le catalogue des causes de panne d'agent, vu de l'ADMIN PLATEFORME : l'écran où un
opérateur approuve ce que l'agent médecin a proposé (13/11/2026).

Deux routes REST, sur le modèle d'`oto_admin_agent_repair` `op=causes` / `op=set_cause`,
dont elles reprennent les gardes sans les recopier (`agent_repair._decider_une_cause`) :

- `GET /api/admin/agent-causes` — les causes, avec ce qu'elles couvrent (épisodes,
  vivants, orgs, classes vues — un DÉSACCORD avec la regex s'y lit), les plus récemment
  vues d'abord ; `status` filtre ;
- `PUT /api/admin/agent-causes/{key}` — approuver, refuser ou rouvrir une cause, poser sa
  classe, son côté, sa copie au client par langue. Approuver une cause du côté du
  titulaire exige sa copie (`client_copy_required`) ; une copie que le client ne peut pas
  lire est refusée (`client_copy_refused`).

REST seulement : c'est un formulaire humain. L'agent médecin propose par l'outil MCP, il
n'approuve jamais (`approval_by_operator_only`).

⚠️ Les INCIDENTS de la boucle (`medecin_agents._INCIDENTS`) vivent dans la mémoire du
processus de la boucle : ils ne se servent pas ici. Ils se lisent au journal et au suivi
d'erreurs (`INCIDENT —`).
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from ._authz import PLATFORM_ADMIN
from ._types import Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

_KEY = {"key": "cause_key"}


class AgentCausesInput(BaseModel):
    status: Optional[Literal["proposee", "approuvee", "refusee"]] = None
    limit: int = Field(default=50, ge=1, le=200)


class AgentCauseSetInput(BaseModel):
    cause_key: str = Field(max_length=32)
    status: Optional[Literal["proposee", "approuvee", "refusee"]] = None
    classe: Optional[str] = Field(default=None, max_length=60)
    cote: Optional[Literal["plateforme", "proprietaire", "inconnu"]] = None
    client_copy: Optional[dict[Literal["fr", "en"], dict[str, str]]] = None


class AgentCauses(BaseModel):
    """`causes` : `{cle, empreinte, classe, cote, statut, copie, gestes,
    vue_premiere_at, episodes, vivants, orgs, derniere_vue, classes_vues}`."""
    causes: list[dict]


class AgentCause(BaseModel):
    cause: dict


def _lister(ctx: ResolvedCtx, inp: AgentCausesInput) -> dict:
    from ..db import causes_agents
    return {"causes": causes_agents.lister(statut=inp.status, limit=inp.limit)}


def _decider(ctx: ResolvedCtx, inp: AgentCauseSetInput) -> dict:
    from . import agent_repair as AR
    return {"cause": AR._decider_une_cause(ctx, AR.AgentRepairInput(
        op="set_cause", cause_key=inp.cause_key, cause_status=inp.status,
        classe=inp.classe, cote=inp.cote, client_copy=inp.client_copy))}


CAPABILITIES += [
    Capability(key="admin.agent_causes.list", handler=_lister, Input=AgentCausesInput,
               Output=AgentCauses, authz=PLATFORM_ADMIN, mcp=None,
               rest=RestBinding("GET", "/api/admin/agent-causes")),
    Capability(key="admin.agent_causes.set", handler=_decider, Input=AgentCauseSetInput,
               Output=AgentCause, authz=PLATFORM_ADMIN, mcp=None,
               rest=RestBinding("PUT", "/api/admin/agent-causes/{key}", _KEY)),
]
