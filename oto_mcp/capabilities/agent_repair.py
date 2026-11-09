"""Capacité admin : les ÉPISODES de panne des agents hébergés, et leurs soins (27/10/2026).

La boucle `medecin_agents` range chaque panne et soigne seule ce qui est passager. Ce
qu'elle ne sait pas ranger (`inconnu`) reste `ouvert` : c'est le travail de l'agent
médecin, un agent hébergé de la plateforme qui reçoit cet outil en `platform_tools`
(`_outils_plateforme.py`) — un opérateur peut s'en servir de la même façon.

⚠️ **Pourquoi un outil d'admin, et pas `oto_trigger`** : `oto_trigger` agit en MEMBRE,
dans l'org de l'appelant. L'agent médecin vit dans l'org de la plateforme et soigne des
agents d'autres orgs : sans cette porte, il diagnostiquerait sans pouvoir rien faire.

⚠️ **Ce que cette porte permet est une liste FERMÉE** : lire un épisode et le dernier
travail mort (sans sa sortie : `_CHAMPS_DU_TRAVAIL`), relancer ce travail (sous les
gardes de la boucle, `medecin_agents.relancer` — une relance par travail mort, tenue en
base, `RELANCES_MAX` par épisode, jamais par-dessus une occurrence qui attend ou tourne,
jamais au-delà de `AGE_MAX`), écrire un diagnostic, corriger la
classe, escalader, clore. Elle ne touche ni une clé, ni une identité, ni une procédure,
ni un horaire, ni une donnée — ces remèdes appartiennent au titulaire de l'agent.
Chaque geste s'écrit sur l'épisode avec son auteur (`ctx.sub`), avant d'être fait.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .. import db
from ._authz import PLATFORM_ADMIN
from ._types import AuthzDenied, Capability, ResolvedCtx
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_COTES = Literal["plateforme", "proprietaire", "inconnu"]


class AgentRepairInput(BaseModel):
    op: Literal["list", "get", "rerun", "diagnose", "escalate", "close"]
    episode_id: Optional[int] = None
    statut: Optional[Literal["vivants", "ouvert", "en_soin", "escalade", "clos"]] = Field(
        default=None, description="list: filter; `vivants` = every episode not closed.")
    org_id: Optional[int] = Field(default=None, description="list: one org only.")
    cote: Optional[_COTES] = Field(default=None, description=(
        "list: filter by who can fix it. diagnose: the corrected side."))
    classe: Optional[str] = Field(default=None, max_length=60, description=(
        "diagnose: the corrected class (a short slug)."))
    diagnostic: Optional[str] = Field(default=None, max_length=2000, description=(
        "diagnose/escalate: what you found, in one paragraph — read by the operator "
        "and, for an owner-side fault, the basis of what the owner is told."))
    resolution: Optional[str] = Field(default=None, max_length=200, description=(
        "close: why it is closed (e.g. `faux_positif`, `repare_a_la_main`)."))
    limit: Optional[int] = Field(default=None, ge=1, le=200)


class AgentRepair(BaseModel):
    """`episodes` pour `list`, `episode` (+ `agent`, `dernier_travail`) pour les autres
    gestes. `refus` = la raison pour laquelle une relance n'est PAS partie : un `rerun`
    refusé n'est pas une erreur de l'appel, c'est une garde qui a tenu."""
    episodes: Optional[list[dict]] = None
    episode: Optional[dict] = None
    agent: Optional[dict] = None
    dernier_travail: Optional[dict] = None
    relance: Optional[bool] = None
    refus: Optional[str] = None


def _episode(inp: AgentRepairInput) -> dict:
    from ..db import episodes_agents
    if inp.episode_id is None:
        raise AuthzDenied(400, "episode_id_required",
                          f"`episode_id` est requis pour op={inp.op}.")
    ep = episodes_agents.lire(inp.episode_id)
    if ep is None:
        raise AuthzDenied(404, "episode_not_found", "Aucun épisode sous ce numéro.")
    return ep


#: Ce qui se sert du dernier travail mort : de quoi diagnostiquer, rien de ce que l'agent
#: a PRODUIT. ⚠️ Ni `result` ni `attempt_errors` : le premier est la sortie de l'agent chez
#: son titulaire, le second peut en recopier des morceaux (une trace d'outil, une donnée
#: lue) — une porte de plateforme sur TOUTES les orgs n'en a pas besoin pour soigner.
_CHAMPS_DU_TRAVAIL = ("id", "kind", "status", "attempts", "last_error", "finished_at",
                      "run_id", "run_outcome", "run_note")


def _contexte(ep: dict) -> tuple[Optional[dict], Optional[dict]]:
    """L'agent (sans sa configuration secrète) et le dernier travail mort de l'épisode
    (`_CHAMPS_DU_TRAVAIL`)."""
    org_id = int(ep["org_id"])
    t = db.get_trigger(int(ep["trigger_id"]), org_id) or None
    agent = None if t is None else {
        k: t.get(k) for k in ("id", "label", "procedure", "model", "enabled", "kind",
                              "cron", "tz", "tools", "max_run_seconds", "max_tokens")}
    job = db.get_job(int(ep["dernier_job_id"]), org_id) if ep.get("dernier_job_id") else None
    if job is not None:
        job = {k: job.get(k) for k in _CHAMPS_DU_TRAVAIL}
    return agent, job


def _agent_repair(ctx: ResolvedCtx, inp: AgentRepairInput) -> dict:
    from .. import medecin_agents
    from ..db import episodes_agents

    if inp.op == "list":
        return {"episodes": episodes_agents.lister(
            statut=inp.statut or "vivants", org_id=inp.org_id, cote=inp.cote,
            limit=inp.limit or 50)}

    ep = _episode(inp)
    if inp.op == "get":
        agent, job = _contexte(ep)
        return {"episode": ep, "agent": agent, "dernier_travail": job}

    if inp.op == "rerun":
        org_id = int(ep["org_id"])
        job = db.get_job(int(ep["dernier_job_id"]), org_id) if ep.get("dernier_job_id") \
            else None
        t = db.get_trigger(int(ep["trigger_id"]), org_id)
        if ep["statut"] == "clos":
            return {"episode": ep, "relance": False, "refus": "épisode clos"}
        if t is None or not t.get("enabled"):
            return {"episode": ep, "relance": False, "refus": "agent éteint ou supprimé"}
        if ep["cote"] == "proprietaire":
            return {"episode": ep, "relance": False,
                    "refus": "panne du côté du titulaire : relancer ne la répare pas"}
        panne = {"org_id": org_id, "trigger_id": int(ep["trigger_id"]),
                 "model": t.get("model"),
                 "job_id": job["id"] if job else None,
                 "kind": job.get("kind") if job else None,
                 "payload": job.get("payload") if job else None,
                 "sub": job.get("sub") if job else None,
                 "dernier": job.get("finished_at") if job else None}
        refus = medecin_agents.relancer(panne, ep, acteur=ctx.sub)
        logger.info("épisode %s (agent %s, org %s) : relance par %s → %s", ep["id"],
                    ep["trigger_id"], org_id, ctx.sub, refus or "partie")
        return {"episode": episodes_agents.lire(int(ep["id"])), "relance": refus is None,
                "refus": refus}

    if inp.op == "diagnose":
        if not (inp.diagnostic or inp.classe or inp.cote):
            raise AuthzDenied(400, "nothing_to_record",
                              "`diagnostic`, `classe` ou `cote` : au moins un.")
        return {"episode": episodes_agents.poser(
            int(ep["id"]), diagnostic=inp.diagnostic, classe=inp.classe, cote=inp.cote)}

    if inp.op == "escalate":
        return {"episode": episodes_agents.poser(
            int(ep["id"]), statut="escalade", diagnostic=inp.diagnostic)}

    # close
    return {"episode": episodes_agents.poser(
        int(ep["id"]), statut="clos", resolution=inp.resolution or "clos_a_la_main")}


CAPABILITIES += [
    Capability(
        key="admin.agent_repair", handler=_agent_repair,
        Input=AgentRepairInput, Output=AgentRepair,
        authz=PLATFORM_ADMIN,
        description=(
            "[platform admin] Failure EPISODES of hosted agents, across every org, and "
            "the closed list of repairs allowed on them. An episode = an enabled agent's "
            "dead jobs since its last success; the background loop opens it at the first "
            "dead job, classifies it (`classe`; `cote` = who can fix it: plateforme | "
            "proprietaire | inconnu), re-runs transient platform failures by itself, and "
            "closes it on the next success. op=list (`statut`, default `vivants`; "
            "`org_id`, `cote`, `limit`) / get (`episode_id` → the episode, the agent, "
            "and its last dead job: status, attempts, `last_error` — never its result nor "
            "its per-attempt log) / rerun (re-queue the last "
            "dead job — refused, with the reason in `refus`, when the episode is closed, "
            "the agent is off, the fault is the owner's, the job was already re-run, it is older than 24 h, "
            "an occurrence is already waiting or running, or 2 re-runs were already spent; "
            "it re-queues the job as it was, reading none of the agent's content. ⚠️ No "
            "guard knows whether the dead run already wrote or sent something: a re-run "
            "redoes it) / "
            "diagnose (`diagnostic`, and/or a corrected `classe`/`cote` — kept: the "
            "loop no longer reclassifies that episode) / escalate (hand "
            "it to a human, with your `diagnostic`) / close (`resolution`). Nothing here "
            "touches a key, an identity, a procedure, a schedule or any data: those "
            "fixes belong to the agent's owner — say what they are in `diagnostic`."),
        mcp="oto_admin_agent_repair",
    ),
]
