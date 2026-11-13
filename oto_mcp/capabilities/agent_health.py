"""La SANTÉ des agents d'une org — l'onglet Santé d'un tableau de bord (13/11/2026).

Trois routes REST :

- `GET /api/orgs/{id}/agent-health` — membres : les agents qu'ils voient, ceux qui
  fonctionnent, ce qui attend le CLIENT (un problème par cause, avec les agents qu'il
  arrête), ce que le médecin a réparé ces trente derniers jours, et le réglage ;
- `PUT /api/orgs/{id}/agent-health` — admins : « laisser Tulina réparer mes agents »
  (`auto_repair`, allumé par défaut) ;
- `POST /api/orgs/{id}/agent-health/undo` `{episode_id}` — qui peut MODIFIER l'agent
  (`_acces_agent.exiger(ECRIRE)`, et pas sur le forfait d'autrui sans prêt : le
  `can_edit` de la liste des agents) : défaire la réparation du médecin. Seul `add_tool`
  se défait — l'outil sort des outils de l'agent s'il y est encore, l'annulation s'écrit
  sur l'épisode (`annulation`), la note « réparé par Tulina » de l'agent s'efface si elle
  date de cet épisode, et le médecin ne redonne plus cet outil à cet agent
  (`runner_triggers.repair_undone_tools` → `add_tool` 409 `repair_undone`).

Une lecture par ORG, jamais une par agent : l'onglet d'une org de deux cents agents
coûte quelques requêtes. Un problème se dit avec le texte que reçoit Slack
(`alertes_slack_agents.message_du_probleme` : la copie de la cause approuvée, sinon le
texte du médecin, sinon la phrase de la classe) et se regroupe comme lui : les agents
dont le client lirait exactement le même problème forment UN problème.

⚠️ **Deux états, comme la carte d'un agent** (`agent-state.ts` du tableau de bord) : un
agent fonctionne, ou il ne fonctionne pas. Ne fonctionne pas : soit il attend le CLIENT
(`needs_you`, côté titulaire : le geste), soit c'est à NOUS (`on_us` : côté plateforme,
ou pas encore compris — une phrase qui dit qu'on s'en occupe, aucun geste). Un membre ne
voit que les agents qu'il voit dans la liste des agents (`_acces_agent.niveaux`) ; un
admin les voit tous.

Sous le réglage d'instance des alertes (`alertes_slack_agents.actives`), comme Réglages ▸
Alertes et `problem` sur la file : éteint, 404 `agent_alerts_off`. Une org d'un tenant
tiers n'est pas suivie par le médecin (`covered: false`) : rien ne s'y dit.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel

from ._authz import ORG_ADMIN_OF, ORG_MEMBER_OF
from ._types import AuthzDenied, Capability, ResolvedCtx, RestBinding
from .registry import CAPABILITIES

logger = logging.getLogger(__name__)

_ID = {"id": "org_id"}

#: Ce qui se dit d'une panne qui est à NOUS, par côté et par langue : aucun geste.
_A_NOUS = {
    "inconnu": {
        "fr": "Nous cherchons pourquoi il s'est arrêté. Nous vous dirons s'il y a quelque "
              "chose à faire.",
        "en": "We are looking into why it stopped. We will tell you if there is anything "
              "to do."},
}

#: D3 (13/11) : une panne de NOTRE côté ne se dit pas avant six heures — la carte de
#: l'agent n'en dit rien non plus, la boucle la relance. Passé six heures, une ligne grise
#: par incident, dans l'onglet seulement (ni Slack, ni courriel). La durée se lit sur
#: l'épisode (`ouvert_at`), pas sur l'incident de la boucle, qui vit en mémoire.
DUREE_LONGUE = timedelta(hours=6)
_A_NOUS_LONGUE = {
    "fr": "Un problème de notre côté l'arrête depuis plus de six heures. Nous y travaillons : "
          "rien à faire du vôtre.",
    "en": "A problem on our side has stopped it for more than six hours. We are working on "
          "it: nothing to do on yours.",
}

#: Ce qui se dit d'une relance qui a guéri l'agent, par langue.
_RELANCE = {
    "fr": "Il s'est arrêté sur une panne passagère de notre côté. Nous l'avons relancé.",
    "en": "It stopped on a short outage on our side. We ran it again.",
}

#: Ce qui se dit d'une réparation qu'une personne a défaite, par langue.
_DEFAITE = {
    "fr": "Nous avions changé cet agent pour le réparer ; ce changement a été annulé.",
    "en": "We had changed this agent to fix it; that change was undone.",
}


class AgentHealthInput(BaseModel):
    org_id: int


class AgentHealthSaveInput(BaseModel):
    org_id: int
    auto_repair: bool


class AgentHealthUndoInput(BaseModel):
    org_id: int
    episode_id: int


class AgentHealth(BaseModel):
    """`watched` agents allumés que l'appelant voit, dont `working` fonctionnent ;
    `needs_you` = un problème par cause qui attend le client : `{headline, todo, action,
    link, since, agents: [{id, label}]}` ; `on_us` = ce qui est à nous, par côté :
    `{side: platform|operator, headline, since, lasting, agents}` — une panne de la
    plateforme ne s'y dit qu'ouverte depuis six heures au moins (D3, `lasting`) ; avant,
    son agent compte comme fonctionnant, comme sur sa carte ; `fixed` = ce que Tulina a
    réparé ces trente derniers jours, le plus récent d'abord : `{episode_id, agent: {id,
    label}, at, what, undo, undone}` — `undo` = l'appelant peut défaire cette réparation
    (`POST …/agent-health/undo {episode_id}`) : un ajout d'outil du médecin pas encore
    défait, sur un agent qu'il peut modifier ; une relance ne se défait pas ; `undone` =
    elle a été défaite (`what` le dit alors) ;
    `auto_repair` = le réglage de l'org ; `covered` = le médecin suit cette org (false :
    org d'un tenant tiers, rien ne s'y dit)."""
    watched: int
    working: int
    needs_you: list[dict]
    on_us: list[dict]
    fixed: list[dict]
    auto_repair: bool
    covered: bool


def _visibles(ctx: ResolvedCtx, org_id: int) -> tuple[dict[int, dict], set[int]]:
    """Les agents que l'appelant voit, et parmi eux ceux qu'il peut MODIFIER — la règle
    de `can_edit` de la liste des agents (`_acces_agent.avec_acces`) : un niveau qui
    écrit, et pas sur le forfait d'autrui sans prêt. Une lecture de partages et au plus
    une de prêts pour toute l'org."""
    from .. import db
    from . import _acces_agent
    agents = db.list_triggers(org_id)
    niveaux = _acces_agent.niveaux(ctx.sub, org_id, agents)
    visibles = {int(t["id"]): t for t in agents if niveaux[int(t["id"])]}
    pretes = _acces_agent.forfaits_pretes(ctx.sub, list(visibles.values()))
    modifiables = {i for i, t in visibles.items()
                   if niveaux[i] in _acces_agent.ECRIRE
                   and not _acces_agent.forfait_ferme(ctx.sub, t, pretes)}
    return visibles, modifiables


def _nom(t: dict) -> str:
    return (t.get("label") or "").strip() or t.get("procedure") or f"#{t['id']}"


def _quand(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v) if v else None


def _date(v: Any) -> datetime:
    """Une date d'épisode, objet ou texte ISO (les lignes passent par `_str_dict_row`)."""
    d = v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _lire(ctx: ResolvedCtx, inp: AgentHealthInput) -> dict:
    from .. import alertes_slack_agents as A
    from ..alerte_agents_bloques import org_a_nous
    from ..db import episodes_agents, sante_agents
    from .agent_alerts import _exiger_les_alertes
    _exiger_les_alertes()
    org_id = int(inp.org_id)
    visibles, modifiables = _visibles(ctx, org_id)
    allumes = {i: t for i, t in visibles.items() if t.get("enabled")}
    langue = A.langue(org_id)
    marque = A.marque_de_l_org(org_id).nom

    problemes: dict[tuple, dict] = {}
    a_nous: dict[str, dict] = {}
    maintenant = datetime.now(timezone.utc)
    for ep in sante_agents.episodes_vivants(org_id):
        t = allumes.get(int(ep["trigger_id"]))
        if t is None:
            continue
        if ep["cote"] == "plateforme" and maintenant - _date(ep["ouvert_at"]) < DUREE_LONGUE:
            # D3 : une panne de NOTRE côté se tait six heures, comme sur la carte de
            # l'agent (`agent-state.ts` : rien de dit) — la boucle la relance ; l'agent
            # compte comme fonctionnant.
            continue
        if ep["cote"] != "proprietaire":
            cote = "plateforme" if ep["cote"] == "plateforme" else "inconnu"
            phrases = _A_NOUS_LONGUE if cote == "plateforme" else _A_NOUS[cote]
            p = a_nous.setdefault(cote, {
                "side": "platform" if cote == "plateforme" else "operator",
                "headline": phrases.get(langue, phrases["en"]),
                "since": ep["ouvert_at"], "agents": []})
            p["since"] = min(p["since"], ep["ouvert_at"])
            p["agents"].append({"id": int(t["id"]), "label": _nom(t)})
            continue
        pb = A.message_du_probleme(org_id, ep, langue, marque=marque, sub=ctx.sub)
        cle = (pb["headline"], pb.get("todo"), pb.get("link"), pb.get("action"))
        p = problemes.setdefault(cle, {**pb, "since": ep["ouvert_at"], "agents": []})
        p["since"] = min(p["since"], ep["ouvert_at"])
        p["agents"].append({"id": int(t["id"]), "label": _nom(t)})
    arretes = {a["id"] for p in [*problemes.values(), *a_nous.values()]
               for a in p["agents"]}

    fixed = []
    for ep in sante_agents.reparations_recentes(org_id):
        t = visibles.get(int(ep["trigger_id"]))
        if t is None:
            continue
        note = ep.get("repair_note") or {}
        reps = ep.get("reparations") or []
        a_defaire = episodes_agents.outils_a_defaire(reps)
        defaite = bool(episodes_agents.outils_ajoutes(reps)) and not a_defaire
        what = (_DEFAITE.get(langue, _DEFAITE["en"]) if defaite
                else note.get("what") or _RELANCE.get(langue, _RELANCE["en"]))
        fixed.append({"episode_id": int(ep["id"]),
                      "agent": {"id": int(t["id"]), "label": _nom(t)},
                      "at": _quand(ep["clos_at"]), "what": what,
                      "undo": bool(a_defaire) and int(t["id"]) in modifiables,
                      "undone": defaite})

    needs_you = sorted(problemes.values(), key=lambda p: p["since"])
    on_us = sorted(a_nous.values(), key=lambda p: p["since"])
    for p in on_us:
        # Une panne de la plateforme n'arrive ici qu'après six heures (plus haut).
        p["lasting"] = p["side"] == "platform"
    for p in [*needs_you, *on_us]:
        p["since"] = _quand(p["since"])
    return {"watched": len(allumes), "working": len(set(allumes) - arretes),
            "needs_you": needs_you, "on_us": on_us, "fixed": fixed,
            "auto_repair": sante_agents.auto_repair(org_id),
            "covered": org_a_nous(org_id, qui="santé des agents")}


def _poser(ctx: ResolvedCtx, inp: AgentHealthSaveInput) -> dict:
    from ..db import sante_agents
    sante_agents.poser_auto_repair(int(inp.org_id), inp.auto_repair)
    return _lire(ctx, AgentHealthInput(org_id=inp.org_id))


def _defaire(ctx: ResolvedCtx, inp: AgentHealthUndoInput) -> dict:
    """Défaire la réparation du médecin sur un épisode : seulement des ajouts d'outils
    (`add_tool`). Dans l'ordre : le droit de modifier l'agent ; la marque qui fait
    refuser ces outils au médecin (idempotente) ; l'annulation écrite sur l'épisode, sous
    garde (deux clics, un geste) ; les outils retirés de l'agent s'ils y sont encore ; la
    note « réparé par Tulina » effacée si elle date de cet épisode. Rend la santé relue,
    comme `PUT`."""
    from .. import db
    from ..db import episodes_agents, runner_triggers as db_triggers
    from . import _acces_agent
    from .agent_alerts import _exiger_les_alertes
    _exiger_les_alertes()
    org_id = int(inp.org_id)
    ep = episodes_agents.lire(int(inp.episode_id))
    if ep is None or int(ep["org_id"]) != org_id:
        raise AuthzDenied(404, "episode_not_found", "Aucun épisode sous ce numéro dans "
                          "cette org.")
    tid = int(ep["trigger_id"])
    t = db.get_trigger(tid, org_id)
    _acces_agent.exiger(ctx, t, _acces_agent.ECRIRE)
    if _acces_agent.forfait_ferme(ctx.sub, t):
        raise AuthzDenied(403, "trigger_edit_forbidden",
                          "cet agent tourne sur l'abonnement personnel de quelqu'un "
                          "d'autre, qui ne te l'a pas prêté : tu ne peux pas le modifier.")
    outils = episodes_agents.outils_a_defaire(ep.get("reparations"))
    if outils:
        # La marque d'abord : idempotente, elle tient même si la suite tombe — le
        # médecin ne redonne jamais un outil qu'une personne a commencé de retirer.
        db_triggers.annuler_outils(tid, org_id, outils)
    if not outils or episodes_agents.noter_annulation(
            int(ep["id"]), acteur=ctx.sub, outils=outils) is None:
        raise AuthzDenied(404, "repair_not_undoable",
                          "Rien à défaire sur cet épisode : seul un outil ajouté par "
                          "Tulina se défait, une fois.")
    restants = [o for o in (t.get("tools") or []) if o not in outils]
    if len(restants) != len(t.get("tools") or []):
        db.update_trigger(tid, org_id, {"tools": restants}, par=ctx.sub)
    db_triggers.effacer_reparation_pour_le_client(tid, org_id, depuis=ep["ouvert_at"],
                                                  jusqu_a=ep.get("clos_at"))
    logger.info("épisode %s (agent %s, org %s) : réparation défaite par %s (%s)",
                ep["id"], tid, org_id, ctx.sub, ", ".join(outils))
    return _lire(ctx, AgentHealthInput(org_id=org_id))


CAPABILITIES += [
    Capability(key="org.agent_health.get", handler=_lire, Input=AgentHealthInput,
               Output=AgentHealth, authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("GET", "/api/orgs/{id}/agent-health", _ID)),
    Capability(key="org.agent_health.set", handler=_poser, Input=AgentHealthSaveInput,
               Output=AgentHealth, authz=ORG_ADMIN_OF("org_id"), mcp=None,
               rest=RestBinding("PUT", "/api/orgs/{id}/agent-health", _ID)),
    Capability(key="org.agent_health.undo", handler=_defaire, Input=AgentHealthUndoInput,
               Output=AgentHealth, authz=ORG_MEMBER_OF("org_id"), mcp=None,
               rest=RestBinding("POST", "/api/orgs/{id}/agent-health/undo", _ID)),
]
