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
classe, escalader, clore — et, depuis le 09/11/2026, DEUX réparations qui se défont :
donner à l'agent UN outil de lecture que sa procédure appelle déjà (`add_tool`), écrire
une nouvelle version de sa procédure d'org (`set_procedure`), chacune dite au client
(`runner_triggers.repair_note`). Elle ne touche ni une clé, ni une identité, ni un
horaire, ni une donnée — ces remèdes appartiennent au titulaire de l'agent.
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
    op: Literal["list", "get", "rerun", "diagnose", "escalate", "close", "add_tool",
                "set_procedure"]
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
    tool: Optional[str] = Field(default=None, max_length=120, description=(
        "add_tool: the ONE tool to give the agent — only a READ tool its procedure or a "
        "published recipe it runs already calls (`missing_tools` on op=get)."))
    body_md: Optional[str] = Field(default=None, max_length=200_000, description=(
        "set_procedure: the whole new body of the agent's procedure."))
    expected_version: Optional[int] = Field(default=None, ge=1, description=(
        "set_procedure: the version you read (op=get `procedure.version`) — required."))
    client_headline: Optional[str] = Field(default=None, max_length=300, description=(
        "diagnose, owner-side: what went wrong, ONE plain sentence for the client in the "
        "workspace's language — no tool names, codes or ids. Shown on the agent in the "
        "app and sent on Slack (\"Depuis le 7/11, l'agent ne peut plus ajouter de leads "
        "dans lemlist : la clé API branchée sur Tulina a été créée par un compte qui n'est "
        "plus dans votre équipe lemlist.\")."))
    client_todo: Optional[str] = Field(default=None, max_length=400, description=(
        "diagnose, owner-side: the exact step the client takes, one or two sentences "
        "(\"Créez une nouvelle clé API dans lemlist (Settings → Integrations → API) et "
        "collez-la dans Tulina.\")."))
    client_link: Optional[str] = Field(default=None, max_length=500, description=(
        "diagnose: where they do it — an app path (`/org/270/connectors/anthropic`) or a "
        "URL. Becomes the message's button."))
    client_action: Optional[Literal["credits", "connectors"]] = Field(default=None, description=(
        "diagnose: the gesture, when it is one of these."))
    client_note: Optional[str] = Field(default=None, max_length=300, description=(
        "add_tool/set_procedure: what you changed, in ONE plain sentence for the client, "
        "in the workspace's language, no tool names or codes (\"Tulina a ajouté la "
        "recherche web à l'agent.\"). Required: shown in the app and sent on Slack."))


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
    missing_tools: Optional[list[dict]] = None
    procedure: Optional[dict] = None


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


#: Les gestes qui CHANGENT l'agent — après l'un d'eux, la relance est permise même du
#: côté du titulaire : c'est précisément ce qui vient d'être réparé.
_REPARATIONS = ("ajout_outil", "procedure")


def _outils_manquants(t: dict, org_id: int) -> list[dict]:
    """Ce que la procédure de l'agent et les recettes qu'elle lance appellent, hors de ses
    outils — `[{tool, called_by, read}]` (la même lecture que `missing_tools` d'oto_trigger)."""
    from . import runner_triggers as cap_triggers
    from .. import tool_registry
    return [{"tool": nom, "called_by": par, "read": tool_registry.effet_de(nom) == "read"}
            for nom, par in cap_triggers._outils_non_accordes(org_id, t)]


def _exiger_la_phrase(inp: AgentRepairInput) -> str:
    note = (inp.client_note or "").strip()
    if not note:
        raise AuthzDenied(400, "client_note_required",
                          "`client_note` : ce que tu as changé, en une phrase pour le client.")
    return note


def _ajouter_un_outil(ctx: ResolvedCtx, ep: dict, inp: AgentRepairInput) -> dict:
    """`add_tool` : UN outil de LECTURE que la procédure (ou une recette publiée qu'elle
    lance) appelle déjà et que l'agent n'a pas. Cas du 09/11/2026 (Klark) : la recette de
    la procédure appelle `serper_search`, absent des outils de l'agent → arrêt `blocked`.

    ⚠️ Pourquoi la LECTURE seule : la prose d'une procédure et le corps d'une recette, un
    agent peut les écrire (`runner_triggers._appeles_par_ses_outils`, même règle). Donner
    sur leur seule foi un outil qui écrit ou envoie, ce serait laisser un texte élargir
    les droits de l'agent — `tools_human_only` reste la règle de tout autre chemin. Une
    écriture manquante s'escalade au titulaire."""
    from .. import deprecations
    org_id, tid = int(ep["org_id"]), int(ep["trigger_id"])
    t = db.get_trigger(tid, org_id)
    if t is None:
        raise AuthzDenied(404, "agent_not_found", "L'agent de cet épisode n'existe plus.")
    if not inp.tool:
        raise AuthzDenied(400, "tool_required", "`tool` est requis pour op=add_tool.")
    note = _exiger_la_phrase(inp)
    outil = deprecations.tool_canonique(inp.tool.strip())
    manquants = {m["tool"]: m for m in _outils_manquants(t, org_id)}
    if outil not in manquants:
        return {"episode": ep, "refus": (
            f"`{outil}` n'est appelé ni par la procédure de l'agent ni par une recette "
            "publiée qu'elle lance, ou l'agent l'a déjà"),
            "missing_tools": list(manquants.values())}
    if not manquants[outil]["read"]:
        return {"episode": ep, "refus": (
            f"`{outil}` n'est pas une lecture : donner un outil qui écrit ou envoie revient "
            "au titulaire de l'agent — escalade-le"),
            "missing_tools": list(manquants.values())}
    from ..db import episodes_agents
    if episodes_agents.noter_reparation(int(ep["id"]), action="ajout_outil",
                                        acteur=ctx.sub, detail=f"+ {outil}",
                                        maximum=5) is None:
        return {"episode": episodes_agents.lire(int(ep["id"])),
                "refus": "épisode clos, ou plafond d'ajouts atteint"}
    outils = list(t.get("tools") or [])
    db.update_trigger(tid, org_id, {"tools": outils + [outil]}, par=ctx.sub)
    db.noter_reparation_pour_le_client(tid, org_id, note)
    logger.info("épisode %s (agent %s, org %s) : + outil %s par %s", ep["id"], tid,
                org_id, outil, ctx.sub)
    return {"episode": episodes_agents.lire(int(ep["id"])),
            "agent": _contexte(episodes_agents.lire(int(ep["id"])))[0]}


def _reecrire_la_procedure(ctx: ResolvedCtx, ep: dict, inp: AgentRepairInput) -> dict:
    """`set_procedure` : une NOUVELLE version de la procédure de l'agent, dans son org —
    les précédentes restent (`oto_procedure op=set from_version=<n>` défait le geste).
    `expected_version` est requis : on ne réécrit pas une procédure qu'on n'a pas lue
    dans son état présent. Cas du 09/11/2026 (3Dnatives) : des messages privés Slack
    adressés à un identifiant que l'app ne peut pas joindre."""
    from .. import org_store
    org_id, tid = int(ep["org_id"]), int(ep["trigger_id"])
    t = db.get_trigger(tid, org_id)
    if t is None or not t.get("procedure"):
        raise AuthzDenied(404, "agent_not_found", "L'agent de cet épisode n'a pas de procédure.")
    if not (inp.body_md or "").strip() or inp.expected_version is None:
        raise AuthzDenied(400, "missing_fields",
                          "`body_md` et `expected_version` sont requis pour op=set_procedure.")
    note = _exiger_la_phrase(inp)
    slug = t["procedure"]
    if org_store.get_instruction("org", org_id, slug) is None:
        return {"episode": ep, "refus": (
            f"la procédure `{slug}` n'est pas une procédure de l'org {org_id} "
            "(personnelle, d'équipe ou de la plateforme) : escalade-la")}
    from ..db import episodes_agents
    if episodes_agents.noter_reparation(
            int(ep["id"]), action="procedure", acteur=ctx.sub,
            detail=f"{slug} v{inp.expected_version} → v{inp.expected_version + 1}",
            maximum=3) is None:
        return {"episode": episodes_agents.lire(int(ep["id"])),
                "refus": "épisode clos, ou plafond de réécritures atteint"}
    try:
        version = org_store.set_instruction("org", org_id, slug, inp.body_md,
                                               set_by=ctx.sub,
                                               expected_version=inp.expected_version)
    except org_store.InstructionVersionConflict as e:
        return {"episode": episodes_agents.lire(int(ep["id"])), "refus": (
            f"la procédure a changé depuis ta lecture (version {e.current_version}) : "
            "relis-la")}
    db.noter_reparation_pour_le_client(tid, org_id, note)
    logger.info("épisode %s (agent %s, org %s) : procédure %s v%s par %s", ep["id"], tid,
                org_id, slug, version, ctx.sub)
    return {"episode": episodes_agents.lire(int(ep["id"])),
            "procedure": {"slug": slug, "version": version}}


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
        manquants, procedure = None, None
        t = db.get_trigger(int(ep["trigger_id"]), int(ep["org_id"]))
        if t is not None:
            manquants = _outils_manquants(t, int(ep["org_id"]))
            if t.get("procedure"):
                from .. import org_store
                p = org_store.get_instruction("org", int(ep["org_id"]), t["procedure"])
                procedure = None if p is None else {
                    "slug": p["slug"], "version": p["version"], "body_md": p["body_md"]}
        return {"episode": ep, "agent": agent, "dernier_travail": job,
                "missing_tools": manquants, "procedure": procedure}

    if inp.op == "add_tool":
        return _ajouter_un_outil(ctx, ep, inp)

    if inp.op == "set_procedure":
        return _reecrire_la_procedure(ctx, ep, inp)

    if inp.op == "rerun":
        org_id = int(ep["org_id"])
        job = db.get_job(int(ep["dernier_job_id"]), org_id) if ep.get("dernier_job_id") \
            else None
        t = db.get_trigger(int(ep["trigger_id"]), org_id)
        if ep["statut"] == "clos":
            return {"episode": ep, "relance": False, "refus": "épisode clos"}
        if t is None or not t.get("enabled"):
            return {"episode": ep, "relance": False, "refus": "agent éteint ou supprimé"}
        repare = any((r or {}).get("action") in _REPARATIONS
                     for r in (ep.get("reparations") or []))
        if ep["cote"] == "proprietaire" and not repare:
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
        client = {"headline": inp.client_headline, "todo": inp.client_todo,
                  "link": inp.client_link, "action": inp.client_action}
        if not (inp.diagnostic or inp.classe or inp.cote or any(client.values())):
            raise AuthzDenied(400, "nothing_to_record",
                              "`diagnostic`, `classe`, `cote` ou `client_*` : au moins un.")
        if any(client.values()) and not inp.client_headline:
            raise AuthzDenied(400, "client_headline_required",
                              "`client_headline` est requis avec les autres `client_*`.")
        if inp.diagnostic or inp.classe or inp.cote:
            episodes_agents.poser(int(ep["id"]), diagnostic=inp.diagnostic,
                                  classe=inp.classe, cote=inp.cote)
        if inp.client_headline:
            from ..db import alertes_slack
            alertes_slack.poser_message_client(int(ep["id"]), client)
        return {"episode": episodes_agents.lire(int(ep["id"]))}

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
            "loop no longer reclassifies that episode; and, for what the CLIENT must "
            "do, `client_headline` + `client_todo` (+ `client_link`, `client_action`): "
            "plain sentences in the workspace's language, shown on the agent in the app "
            "and sent to the org's Slack) / escalate (hand "
            "it to a human, with your `diagnostic`) / close (`resolution`). "
            "op=get also serves `missing_tools` ([{tool, called_by, read}]: what its "
            "procedure or a published recipe it runs calls and the agent lacks) and "
            "`procedure` ({slug, version, body_md}, the org procedure it follows). Two "
            "UNDOABLE repairs, each written on the episode before it is done and told to "
            "the client (`client_note`, one plain sentence in the workspace's language, "
            "required): add_tool (`tool`: ONE read tool from `missing_tools` — never a "
            "tool that writes or sends) / set_procedure (`body_md` + `expected_version`: "
            "a new version of the agent's org procedure, prior versions kept — "
            "`oto_procedure op=set from_version` undoes it). After either, `rerun` is "
            "allowed even on an owner-side episode. Nothing here touches a key, an "
            "identity, a schedule or any data: those fixes belong to the agent's owner — "
            "say what they are in `diagnostic`."),
        mcp="oto_admin_agent_repair",
    ),
]
