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
classe, escalader, clore — et UNE réparation qui se défait : donner à l'agent UN outil
de LECTURE que sa procédure appelle déjà (`add_tool`), dite au client
(`runner_triggers.repair_note`). Elle ne touche ni une clé, ni une identité, ni une
procédure, ni un horaire, ni une donnée — ces remèdes appartiennent au titulaire de
l'agent. ⚠️ Réécrire une procédure n'est PAS ici : aucun mécanisme ne fait valider ce
geste par une personne, et l'agent médecin agit sur toutes les orgs.
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


#: Les champs d'une copie de cause, par langue, et leur longueur maximale. Plat : un
#: schéma d'outil ne porte pas de `$defs` (`tests/test_with_signature_flat.py`).
_COPIE_CHAMPS = {"headline": 300, "todo": 400, "action": 20}
_COPIE_ACTIONS = ("credits", "connectors")


class AgentRepairInput(BaseModel):
    op: Literal["list", "get", "rerun", "diagnose", "escalate", "close", "add_tool",
                "causes", "set_cause"]
    episode_id: Optional[int] = None
    statut: Optional[Literal["vivants", "ouvert", "en_soin", "escalade", "clos"]] = Field(
        default=None, description="list: filter; `vivants` = every episode not closed.")
    org_id: Optional[int] = Field(default=None, description="list: one org only.")
    cote: Optional[_COTES] = Field(default=None, description=(
        "list: filter by who can fix it. diagnose / set_cause: the corrected side."))
    classe: Optional[str] = Field(default=None, max_length=60, description=(
        "diagnose / set_cause: the corrected class (a short slug)."))
    diagnostic: Optional[str] = Field(default=None, max_length=2000, description=(
        "diagnose/escalate: what you found, in one paragraph — read by the operator "
        "and, for an owner-side fault, the basis of what the owner is told."))
    resolution: Optional[str] = Field(default=None, max_length=200, description=(
        "close: why it is closed (e.g. `faux_positif`, `repare_a_la_main`)."))
    limit: Optional[int] = Field(default=None, ge=1, le=200)
    tool: Optional[str] = Field(default=None, max_length=120, description=(
        "add_tool: the ONE tool to give the agent — only a READ tool its procedure or a "
        "published recipe it runs already calls (`missing_tools` on op=get)."))
    client_headline: Optional[str] = Field(default=None, max_length=300, description=(
        "diagnose, owner-side: what went wrong, ONE plain sentence for the client in the "
        "workspace's language — no tool names, codes or ids. Shown on the agent in the "
        "app and sent on Slack (\"Depuis le 7/11, l'agent ne peut plus ajouter de leads "
        "dans l'outil d'emailing : la clé API a été créée par un compte qui n'est plus "
        "dans votre équipe.\"). Only when this instance sends agent alerts."))
    client_todo: Optional[str] = Field(default=None, max_length=400, description=(
        "diagnose, owner-side: the exact step the client takes, one or two sentences "
        "(\"Créez une nouvelle clé API dans l'outil et collez-la dans Connecteurs.\")."))
    client_link: Optional[str] = Field(default=None, max_length=500, description=(
        "diagnose: where they do it — a path of the client's dashboard "
        "(`/org/270/connectors`) or a URL ON that dashboard; any other address is "
        "refused. Becomes the message's button."))
    client_action: Optional[Literal["credits", "connectors"]] = Field(default=None, description=(
        "diagnose: the gesture, when it is one of these."))
    cause_key: Optional[str] = Field(default=None, max_length=32, description=(
        "set_cause: the cause to decide (`cle` from op=causes, `empreinte` on an "
        "episode)."))
    cause_status: Optional[Literal["proposee", "approuvee", "refusee"]] = Field(
        default=None, description=(
            "causes: filter. set_cause: approve (`approuvee`), refuse (`refusee`) or "
            "reopen (`proposee`) the cause."))
    client_copy: Optional[dict[Literal["fr", "en"], dict[str, str]]] = Field(
        default=None, description=(
            "set_cause: what the client reads for this cause, per language "
            "(`{fr: {headline, todo, action}, en: {...}}`; `headline` required, "
            "`action` = credits | connectors) — plain sentences, no tool names, codes, "
            "ids or addresses; `{marque}` is the only placeholder. Required to approve "
            "an owner-side cause."))
    client_note: Optional[str] = Field(default=None, max_length=300, description=(
        "add_tool: what you changed, in ONE plain sentence for the client, in the "
        "workspace's language, no tool names or codes (\"Nous avons ajouté la recherche "
        "web à l'agent.\"). Required: served on the agent as `repair`."))


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
    causes: Optional[list[dict]] = None
    cause: Optional[dict] = None


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
_REPARATIONS = ("ajout_outil",)


def _outils_manquants(t: dict, org_id: int) -> list[dict]:
    """Ce que la procédure de l'agent et les recettes qu'elle lance appellent, hors de ses
    outils — `[{tool, called_by, read}]` (la même lecture que `missing_tools` d'oto_trigger)."""
    from . import runner_triggers as cap_triggers
    from .. import tool_registry
    return [{"tool": nom, "called_by": par, "read": tool_registry.effet_de(nom) == "read"}
            for nom, par in cap_triggers._outils_non_accordes(org_id, t)]


def _ajouter_un_outil(ctx: ResolvedCtx, ep: dict, inp: AgentRepairInput) -> dict:
    """`add_tool` : UN outil de LECTURE que la procédure (ou une recette publiée qu'elle
    lance) appelle déjà et que l'agent n'a pas. Cas du 09/11/2026 : la recette de la
    procédure d'un agent appelait un outil de recherche absent de ses outils → arrêt
    `blocked`.

    ⚠️ Pourquoi la LECTURE seule : la prose d'une procédure et le corps d'une recette, un
    agent peut les écrire (`runner_triggers._appeles_par_ses_outils`, même règle). Donner
    sur leur seule foi un outil qui écrit ou envoie, ce serait laisser un texte élargir
    les droits de l'agent — `tools_human_only` reste la règle de tout autre chemin. Une
    écriture manquante s'escalade au titulaire."""
    from .. import deprecations
    from ..db import episodes_agents
    org_id, tid = int(ep["org_id"]), int(ep["trigger_id"])
    t = db.get_trigger(tid, org_id)
    if t is None:
        raise AuthzDenied(404, "agent_not_found", "L'agent de cet épisode n'existe plus.")
    if not inp.tool:
        raise AuthzDenied(400, "tool_required", "`tool` est requis pour op=add_tool.")
    note = (inp.client_note or "").strip()
    if not note:
        raise AuthzDenied(400, "client_note_required",
                          "`client_note` : ce que tu as changé, en une phrase pour le client.")
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
    if episodes_agents.noter_reparation(int(ep["id"]), action="ajout_outil",
                                        acteur=ctx.sub, detail=f"+ {outil}",
                                        maximum=5) is None:
        return {"episode": episodes_agents.lire(int(ep["id"])),
                "refus": "épisode clos, ou plafond d'ajouts atteint"}
    db.update_trigger(tid, org_id, {"tools": list(t.get("tools") or []) + [outil]},
                      par=ctx.sub)
    db.noter_reparation_pour_le_client(tid, org_id, note)
    logger.info("épisode %s (agent %s, org %s) : + outil %s par %s", ep["id"], tid,
                org_id, outil, ctx.sub)
    ep = episodes_agents.lire(int(ep["id"]))
    return {"episode": ep, "agent": _contexte(ep)[0]}


def _valider_le_message_client(ep: dict, client: dict) -> None:
    """Les champs `client_*` de `diagnose` : seulement si l'instance envoie des alertes
    d'agents (`alertes_slack_agents.actives`) ; `client_headline` exigé avec les autres ;
    `client_link` limité au tableau de bord du titulaire de l'agent
    (`links.lien_de_l_instance`) — un lien qui mène ailleurs est refusé."""
    from .. import alertes_slack_agents, links
    if not alertes_slack_agents.actives():
        raise AuthzDenied(400, "agent_alerts_off",
                          "`client_*` : cette instance n'envoie pas d'alertes d'agents.")
    if not client["headline"]:
        raise AuthzDenied(400, "client_headline_required",
                          "`client_headline` est requis avec les autres `client_*`.")
    from .. import causes_pannes
    for champ in ("headline", "todo"):
        refus = causes_pannes.copie_refusee(client[champ])
        if refus:
            raise AuthzDenied(400, "client_copy_refused", f"`client_{champ}` : {refus}.")
    if client["link"]:
        t = db.get_trigger(int(ep["trigger_id"]), int(ep["org_id"]))
        sub = (t or {}).get("sub")
        if links.lien_de_l_instance(client["link"], sub=sub) is None:
            raise AuthzDenied(400, "client_link_off_instance",
                              "`client_link` : un chemin ou une adresse du tableau de bord "
                              "du client seulement.")


def _proposer_a_la_cause(ep: dict, client: dict, *, acteur: str) -> None:
    """D5 : le texte du médecin part tout de suite (`message_du_probleme`) ET devient la
    copie PROPOSÉE de la cause de l'épisode, dans la langue de l'org, si elle n'en a pas
    encore dans cette langue — un opérateur l'approuve une fois pour toutes les orgs. Une
    cause approuvée ou refusée ne se touche pas ; le lien, propre à l'org, ne s'y range
    pas."""
    from .. import alertes_slack_agents
    from ..db import causes_agents
    cause = causes_agents.lire(ep["empreinte"]) if ep.get("empreinte") else None
    if cause is None or cause["statut"] != "proposee":
        return
    langue = alertes_slack_agents.langue(int(ep["org_id"]))
    if langue in (cause.get("copie") or {}):
        return
    copie = {k: client[k] for k in ("headline", "todo", "action") if client.get(k)}
    causes_agents.decider(cause["cle"], acteur=acteur, copie={langue: copie})


def _copie_lue(brute: dict) -> dict:
    """La copie par langue, champs connus seulement, bornés, `headline` exigé."""
    copie = {}
    for lang, champs in brute.items():
        inconnus = set(champs) - set(_COPIE_CHAMPS)
        if inconnus:
            raise AuthzDenied(400, "client_copy_refused",
                              f"`client_copy.{lang}` : champ inconnu {sorted(inconnus)}.")
        c = {k: (v or "").strip() for k, v in champs.items() if (v or "").strip()}
        if not c.get("headline"):
            raise AuthzDenied(400, "client_copy_refused",
                              f"`client_copy.{lang}.headline` est requis.")
        for k, v in c.items():
            if len(v) > _COPIE_CHAMPS[k]:
                raise AuthzDenied(400, "client_copy_refused",
                                  f"`client_copy.{lang}.{k}` : {_COPIE_CHAMPS[k]} caractères "
                                  "au plus.")
        if c.get("action") and c["action"] not in _COPIE_ACTIONS:
            raise AuthzDenied(400, "client_copy_refused",
                              f"`client_copy.{lang}.action` : credits ou connectors.")
        copie[lang] = c
    return copie


def _decider_une_cause(ctx: ResolvedCtx, inp: AgentRepairInput) -> dict:
    """Un geste sur une cause du catalogue. Approuver une cause du côté du titulaire
    exige sa copie au client (il la lira), et une copie se juge avant d'être écrite
    (`causes_pannes.copie_refusee`). ⚠️ En ombre, une cause approuvée ne décide encore
    d'aucun geste de la boucle : elle se compare à la regex (`catalogue_desaccords`)."""
    from .. import causes_pannes
    from ..db import causes_agents
    if not inp.cause_key:
        raise AuthzDenied(400, "cause_key_required", "`cause_key` est requis pour op=set_cause.")
    cause = causes_agents.lire(inp.cause_key)
    if cause is None:
        raise AuthzDenied(404, "cause_not_found", "Aucune cause sous cette clé.")
    if not (inp.cause_status or inp.classe or inp.cote or inp.client_copy):
        raise AuthzDenied(400, "nothing_to_record",
                          "`cause_status`, `classe`, `cote` ou `client_copy` : au moins un.")
    copie = _copie_lue(inp.client_copy) if inp.client_copy else None
    for c in (copie or {}).values():
        for texte in (c.get("headline"), c.get("todo")):
            refus = causes_pannes.copie_refusee(texte)
            if refus:
                raise AuthzDenied(400, "client_copy_refused", f"`client_copy` : {refus}.")
    from ._outils_plateforme import _depuis_un_agent_heberge
    if inp.cause_status == "approuvee" and _depuis_un_agent_heberge():
        # L'agent médecin PROPOSE (classe, côté, copie) ; un opérateur approuve. Sans
        # cette porte, la copie qu'il a écrite irait à toutes les orgs sans personne.
        raise AuthzDenied(403, "approval_by_operator_only",
                          "Une cause s'approuve par un opérateur, pas par un agent hébergé : "
                          "propose la classe, le côté et la copie, et laisse-la `proposee`.")
    cote = inp.cote or cause["cote"]
    if (inp.cause_status or cause["statut"]) == "approuvee" and cote == "proprietaire" \
            and not (copie or cause.get("copie")):
        raise AuthzDenied(400, "client_copy_required",
                          "Une cause du côté du titulaire s'approuve avec sa `client_copy`.")
    logger.info("cause %s : %s par %s", inp.cause_key, inp.cause_status or "modifiée",
                ctx.sub)
    return causes_agents.decider(inp.cause_key, acteur=ctx.sub, statut=inp.cause_status,
                                 classe=inp.classe, cote=inp.cote, copie=copie)


def _exiger_la_reparation_permise(ep: dict) -> None:
    """Un agent hébergé (l'agent médecin) ne relance ni ne répare l'agent d'une org qui a
    éteint « laisser Tulina réparer mes agents » ; un opérateur, lui, garde la main."""
    from ..db import sante_agents
    from ._outils_plateforme import _depuis_un_agent_heberge
    if _depuis_un_agent_heberge() and not sante_agents.auto_repair(int(ep["org_id"])):
        raise AuthzDenied(403, "auto_repair_off",
                          "Cette org a éteint « laisser Tulina réparer mes agents » : "
                          "diagnostique et escalade, ne relance ni ne répare.")


def _agent_repair(ctx: ResolvedCtx, inp: AgentRepairInput) -> dict:
    from .. import medecin_agents
    from ..db import episodes_agents

    if inp.op == "list":
        return {"episodes": episodes_agents.lister(
            statut=inp.statut or "vivants", org_id=inp.org_id, cote=inp.cote,
            limit=inp.limit or 50)}
    if inp.op == "causes":
        from ..db import causes_agents
        return {"causes": causes_agents.lister(statut=inp.cause_status,
                                               limit=inp.limit or 50)}
    if inp.op == "set_cause":
        return {"cause": _decider_une_cause(ctx, inp)}

    ep = _episode(inp)
    if inp.op == "get":
        agent, job = _contexte(ep)
        t = db.get_trigger(int(ep["trigger_id"]), int(ep["org_id"]))
        manquants = None if t is None else _outils_manquants(t, int(ep["org_id"]))
        return {"episode": ep, "agent": agent, "dernier_travail": job,
                "missing_tools": manquants}

    if inp.op in ("add_tool", "rerun"):
        _exiger_la_reparation_permise(ep)
    if inp.op == "add_tool":
        return _ajouter_un_outil(ctx, ep, inp)

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
        if any(client.values()):
            _valider_le_message_client(ep, client)
        if inp.diagnostic or inp.classe or inp.cote:
            episodes_agents.poser(int(ep["id"]), diagnostic=inp.diagnostic,
                                  classe=inp.classe, cote=inp.cote)
        if any(client.values()):
            from ..db import alertes_slack
            alertes_slack.poser_message_client(int(ep["id"]), client)
            _proposer_a_la_cause(ep, client, acteur=ctx.sub)
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
            "loop no longer reclassifies that episode; and, when this instance sends "
            "agent alerts, for what the CLIENT must do: `client_headline` + "
            "`client_todo` (+ `client_link` — a path or URL of the client's own "
            "dashboard, anything else refused —, `client_action`): plain sentences in "
            "the workspace's language, shown on the agent in the app and sent to the "
            "org's Slack) / escalate (hand "
            "it to a human, with your `diagnostic`) / close (`resolution`). "
            "op=get also serves `missing_tools` ([{tool, called_by, read}]: what its "
            "procedure or a published recipe it runs calls and the agent lacks). ONE "
            "undoable repair, written on the episode before it is done and told to the "
            "client: add_tool (`tool`: ONE read tool from `missing_tools` — never a tool "
            "that writes or sends; `client_note`, one plain sentence in the workspace's "
            "language, required, served on the agent as `repair`). After it, `rerun` is "
            "allowed even on an owner-side episode. Nothing here touches a key, an "
            "identity, a procedure, a schedule or any data: those fixes belong to the "
            "agent's owner — say what they are in `diagnostic`. The CAUSE CATALOGUE "
            "(in shadow: it decides nothing yet, the loop compares it with its own "
            "classification): each episode carries `empreinte`, the key of its failure "
            "message with ids, dates, numbers and addresses stripped, the same for every "
            "org. op=causes (`cause_status`, `limit` → each cause with the episodes, "
            "live episodes and orgs it covers, and `classes_vues`: the classes the loop "
            "gave them) / set_cause (`cause_key`, and `cause_status`, a corrected "
            "`classe`/`cote`, `client_copy` — merged per language, required to approve "
            "an owner-side cause; a hosted agent proposes, only an operator approves). "
            "An org that turned off \"let Tulina fix my agents\" refuses rerun and add_tool "
            "from a hosted agent (`auto_repair_off`)."),
        mcp="oto_admin_agent_repair",
    ),
]
