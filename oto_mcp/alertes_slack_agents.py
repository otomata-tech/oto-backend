"""Prévenir une org SUR SLACK qu'un de ses agents ne fonctionne plus (09/11/2026).

Le courriel au premier admin (`alerte_agents_bloques.py`) arrive au troisième travail
mort, dans une boîte qu'on lit rarement, et ne dit pas quoi faire. Constaté le
09/11/2026 : plusieurs clients avaient un agent arrêté ; certains se réparaient de notre
côté, d'autres demandaient un geste du client (une clé à recréer, un abonnement à
reconnecter), et chacun a été écrit à la main, sur Slack.

Ce module écrit ces messages-là, sur le Slack de l'org, par SON app (le connecteur Slack
de l'org), dans le canal réglé ou, sans canal, en message privé à la personne qui a
connecté Slack (`db/alertes_slack.py`). Trois messages, un de chaque par épisode
(`agent_episodes`), écrits dans la langue de l'espace, sous la marque de son tenant
(`email_brand.marque` du tenant de l'org, lu au registre des tenants). **Une org reçoit
UN message par problème, pas par agent** (13/11/2026, `grouper`) : douze agents qu'arrête
la même clé refusée font un message qui les nomme, avec le geste une fois ; les retours
d'un passage partent ensemble ; une réparation part seule :

- **l'agent ne fonctionne plus** — seulement quand le CLIENT doit agir (`cote =
  proprietaire`) : depuis quand, pourquoi, le geste, un bouton vers l'endroit où le
  faire (`message_client.link`, sinon la page de réparation de l'agent) et « Ouvrir
  l'agent ». Le texte est celui que l'agent médecin écrit pour le client
  (`oto_admin_agent_repair op=diagnose`, `client_*`) ; sans lui, une phrase par classe,
  et seulement après `ATTENTE_MIN` (le temps de laisser réparer ce qui se répare) ;
- **réparé** — après une réparation du médecin (`runner_triggers.repair_note`) : une
  ligne, sa phrase ;
- **il refonctionne** — l'épisode guéri après une alerte.

Une panne de NOTRE côté (`plateforme`) ne s'écrit jamais au client. Une org d'un tenant
tiers est écartée (`alerte_agents_bloques.org_a_nous`). Rien n'appelle un modèle ici : le
texte est déjà écrit.

**Les liens** ne mènent qu'au tableau de bord de l'instance ou du tenant
(`links.lien_de_l_instance`, `links.link_for`) : un lien écrit par l'agent médecin qui
mène ailleurs est omis ; une page que le produit ne déclare pas (`agent`,
`agent_repair`) ne donne pas de bouton.

**Sous un réglage d'instance** (`ENV_ACTIVES`, lu par `interrupteurs.oui_non` à chaque
appel) : éteint (absent, vide, non), rien de ce dispositif ne s'applique — la boucle
n'est pas armée, les routes de réglage répondent 404, la connexion de Slack n'allume
rien, le courriel aux admins ne consulte pas Slack, la file des travaux ne sert pas
`problem` et `diagnose` refuse les champs `client_*`. Une valeur illisible LÈVE.

⚠️ La marque d'envoi vient APRÈS l'envoi (`alertes_slack.marquer`), comme le courriel :
un envoi raté se rejoue au passage suivant, il ne devient pas un silence. Et le courriel
aux admins se TAIT pour un épisode annoncé sur Slack
(`alerte_agents_bloques._annonces_sur_slack`) : il reste le filet quand Slack n'est pas
réglé ou refuse.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from . import interrupteurs

log = logging.getLogger("oto_mcp.alertes_slack_agents")

#: Le réglage d'instance : les alertes d'agents sur le Slack des orgs. Éteint par défaut.
ENV_ACTIVES = "OTO_ALERTES_SLACK_AGENTS_ENABLED"

#: La cadence : cinq minutes, comme le médecin des agents.
INTERVALLE_S = 300

#: Sans message écrit pour le client par l'agent médecin, on attend ce délai avant
#: d'alerter avec la phrase de la classe : il a le temps de réparer ce qui se répare.
ATTENTE_MIN = 120

#: Les gestes du client qu'un message peut proposer.
ACTIONS = ("credits", "connectors")


class AlertesEteintes(RuntimeError):
    """Les alertes Slack des agents ne sont pas allumées sur cette instance."""


def actives() -> bool:
    """Le réglage de l'instance, lu à chaque appel. Une valeur illisible lève
    (`interrupteurs.InterrupteurIllisible`)."""
    return interrupteurs.oui_non(ENV_ACTIVES, os.environ.get(ENV_ACTIVES))


_PHRASES = {
    "fr": {
        "titre": "⚠️ *{agent}* ne fonctionne plus depuis {depuis}",
        "titre_n": "⚠️ {n} agents ne fonctionnent plus depuis {depuis} : {agents}",
        "retour_n": "✅ {agents} refonctionnent.",
        "et_autres": "et {n} autres",
        "sante": "Voir la santé des agents",
        "repare": "🔧 *{agent}* — réparé par {marque} : {quoi}",
        "retour": "✅ *{agent}* refonctionne.",
        "reparer": "Réparer avec {marque}",
        "faire": "Le faire maintenant",
        "ouvrir": "Ouvrir l'agent",
        "test": "Test : c'est ici que {marque} vous préviendra quand un de vos agents ne "
                "fonctionne plus, avec le geste pour le réparer.",
        "classes": {
            "cle_refusee": ("Une clé ou un compte branché sur {marque} a été refusé.",
                            "Reconnectez-le dans Connecteurs : l'agent repartira tout seul.",
                            "connectors"),
            "identite": ("Le compte au nom duquel l'agent tourne n'est plus valide.",
                         "Ouvrez l'agent et enregistrez-le de nouveau.", None),
        },
        "defaut": ("Il s'est arrêté avant la fin de son travail.",
                   "Ouvrez l'agent : {marque} vous dit quoi faire.", None),
    },
    "en": {
        "titre": "⚠️ *{agent}* has not been working since {depuis}",
        "titre_n": "⚠️ {n} agents have not been working since {depuis}: {agents}",
        "retour_n": "✅ {agents} work again.",
        "et_autres": "and {n} more",
        "sante": "See agent health",
        "repare": "🔧 *{agent}* — fixed by {marque}: {quoi}",
        "retour": "✅ *{agent}* works again.",
        "reparer": "Fix it with {marque}",
        "faire": "Do it now",
        "ouvrir": "Open the agent",
        "test": "Test: this is where {marque} will tell you when one of your agents stops "
                "working, with what to do to fix it.",
        "classes": {
            "cle_refusee": ("A key or account connected to {marque} was refused.",
                            "Reconnect it in Connectors: the agent will restart on its own.",
                            "connectors"),
            "identite": ("The account the agent runs as is no longer valid.",
                         "Open the agent and save it again.", None),
        },
        "defaut": ("It stopped before finishing its work.",
                   "Open the agent: {marque} tells you what to do.", None),
    },
}


def marque_de_l_org(org_id: int):
    """La marque du tenant de l'org (`db.org_tenant_slug` → `email_brand.marque`) : le
    nom qui signe les messages et la langue par défaut de l'espace."""
    from . import db, email_brand
    return email_brand.marque(db.org_tenant_slug(int(org_id)))


def langue(org_id: int) -> str:
    """La langue de l'espace : celle du premier admin joignable, sinon celle de la marque
    de son tenant ; une marque sans langue sert le français (`email_brand.Marque`)."""
    from .alerte_agents_bloques import destinataire_admin
    from .db import users as db_users
    dest = destinataire_admin(org_id)
    if dest:
        loc = (db_users.get_user(dest[0]) or {}).get("locale")
        if loc in _PHRASES:
            return loc
    return marque_de_l_org(org_id).langue or "fr"


#: Le lien du tableau de bord où se fait le geste d'une action.
_LIEN_DE_L_ACTION = {"connectors": "connectors", "credits": "billing"}


def _copie_de_la_cause(episode: dict, langue_: str) -> Optional[dict]:
    """La copie de la cause APPROUVÉE de l'épisode dans cette langue, si la cause est du
    même côté que l'épisode : une cause approuvée « titulaire » ne parle pas au client
    d'une panne que la boucle tient pour la nôtre."""
    copie = episode.get("cause_copie") or {}
    if episode.get("cause_cote") != episode.get("cote"):
        return None
    c = copie.get(langue_) if isinstance(copie, dict) else None
    return c if isinstance(c, dict) and c.get("headline") else None


def message_du_probleme(org_id: int, episode: dict, langue_: str, *, marque: str,
                        sub: Optional[str]) -> dict[str, Any]:
    """`{headline, todo, action, link}` pour le client, dans cet ordre de préférence :
    la copie de la CAUSE approuvée (`agent_causes`, la même pour toutes les orgs), sinon
    ce que l'agent médecin a écrit pour CET épisode (une cause neuve), sinon la phrase de
    la classe. `link` est une URL du tableau de bord de ce compte
    (`links.lien_de_l_instance`), ou None — un lien qui mène ailleurs est omis."""
    from . import links
    cause = _copie_de_la_cause(episode, langue_)
    if cause:
        action = cause.get("action") if cause.get("action") in ACTIONS else None
        lien = (links.link_for(_LIEN_DE_L_ACTION[action], sub=sub, org=int(org_id))
                if action else None)
        return {"headline": cause["headline"].replace("{marque}", marque),
                "todo": (cause.get("todo") or "").replace("{marque}", marque) or None,
                "action": action, "link": lien}
    m = episode.get("message_client") or {}
    if m.get("headline"):
        return {"headline": m["headline"], "todo": m.get("todo"),
                "action": m.get("action") if m.get("action") in ACTIONS else None,
                "link": links.lien_de_l_instance(m.get("link"), sub=sub)}
    p = _PHRASES[langue_]
    titre, geste, action = p["classes"].get(episode.get("classe") or "", p["defaut"])
    lien = (links.link_for("connectors", sub=sub, org=int(org_id))
            if action == "connectors" else None)
    return {"headline": titre.format(marque=marque), "todo": geste.format(marque=marque),
            "action": action, "link": lien}


def bot_de_l_org(org_id: int) -> Optional[tuple[str, str]]:
    """`(bot_token, nom de l'espace Slack)` de la connexion Slack de l'ORG — celle que le
    bouton « Connecter Slack » pose (`install_source = oauth`) d'abord — ou None."""
    from . import credentials_store
    lignes = credentials_store.list_accounts("org", str(int(org_id)), "slack")
    lignes.sort(key=lambda r: (r.get("meta") or {}).get("install_source") != "oauth")
    for ligne in lignes:
        secret = credentials_store.get_credential("org", str(int(org_id)), "slack",
                                                  ligne["account"])
        champs = credentials_store.unpack_secret("slack", secret or "") if secret else {}
        if (champs or {}).get("bot_token"):
            return champs["bot_token"], str((ligne.get("meta") or {}).get("team_name") or "")
    return None


def destination(reglage: dict) -> Optional[str]:
    return reglage.get("channel_id") or reglage.get("dm_user")


def poster(org_id: int, reglage: dict, texte: str,
           blocs: Optional[list] = None) -> bool:
    """Poste par l'app Slack de l'org. False, dit au journal, si rien n'est parti :
    l'appelant ne marque rien (la boucle rejoue, la route de test répond 502)."""
    from oto.tools.slack.client import SlackClient
    bot = bot_de_l_org(org_id)
    cible = destination(reglage)
    if bot is None or not cible:
        log.info("alertes Slack : org %s sans connexion Slack ou sans destination", org_id)
        return False
    try:
        SlackClient(bot_token=bot[0]).post_message(cible, texte, blocks=blocs)
        return True
    # noqa: SILENT — dit au journal ; l'épisode n'est pas marqué, il se rejoue.
    except Exception as e:  # noqa: BLE001
        log.warning("alertes Slack : envoi refusé pour l'org %s : %s", org_id, e)
        return False


def _depuis(quand: Any, langue_: str) -> str:
    if not isinstance(quand, datetime):
        return str(quand or "")[:16]
    mois_fr = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.",
               "oct.", "nov.", "déc.")
    if langue_ == "fr":
        return f"le {quand.day} {mois_fr[quand.month - 1]} à {quand:%H:%M} UTC"
    return f"{quand:%b} {quand.day} at {quand:%H:%M} UTC"


def composer(ep: dict, langue_: str, *, marque: str) -> tuple[str, Optional[list]]:
    """Le texte (et les blocs) du message que l'épisode attend (`ep["message"]`). Les
    liens sont ceux du tableau de bord du titulaire de l'agent (`ep["agent_sub"]`)."""
    from . import links
    p = _PHRASES[langue_]
    agent = ep.get("libelle") or f"#{ep['trigger_id']}"
    org, tid, sub = int(ep["org_id"]), int(ep["trigger_id"]), ep.get("agent_sub")
    if ep["message"] == "repare":
        quoi = ((ep.get("repair_note") or {}).get("what") or "").strip()
        return p["repare"].format(agent=agent, marque=marque, quoi=quoi), None
    if ep["message"] == "retour":
        return p["retour"].format(agent=agent), None
    pb = message_du_probleme(org, ep, langue_, marque=marque, sub=sub)
    titre = p["titre"].format(agent=agent, depuis=_depuis(ep.get("ouvert_at"), langue_))
    lignes = [titre, pb["headline"]] + ([pb["todo"]] if pb.get("todo") else [])
    page_agent = links.link_for("agent", sub=sub, org=org, id=tid)
    reparer = links.link_for("agent_repair", sub=sub, org=org, id=tid)
    premier = ({"text": p["faire"], "url": pb["link"]} if pb.get("link")
               else {"text": p["reparer"].format(marque=marque), "url": reparer}
               if reparer else None)
    boutons = []
    if premier:
        boutons.append({"type": "button", "style": "primary",
                        "text": {"type": "plain_text", "text": premier["text"]},
                        "url": premier["url"]})
    if page_agent:
        boutons.append({"type": "button", "text": {"type": "plain_text", "text": p["ouvrir"]},
                        "url": page_agent})
    blocs = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lignes)}}]
    if boutons:
        blocs.append({"type": "actions", "elements": boutons})
    return "\n".join(lignes), blocs


#: Au-delà, un message groupé nomme les premiers agents et compte les autres.
AGENTS_NOMMES_MAX = 5


def _nommer(eps: list[dict], langue_: str) -> str:
    """Les agents d'un paquet, en gras, chacun LIEN vers sa page quand l'instance la
    déclare (`agent`) : un message groupé sans geste à lien garde un endroit où cliquer
    par agent."""
    from . import links

    def _un(e: dict) -> str:
        nom = (e.get("libelle") or f"#{e['trigger_id']}").replace("|", "/").replace(">", "›")
        url = links.link_for("agent", sub=e.get("agent_sub"), org=int(e["org_id"]),
                             id=int(e["trigger_id"]))
        return f"*<{url}|{nom}>*" if url else f"*{nom}*"
    noms = [_un(e) for e in eps]
    if len(noms) > AGENTS_NOMMES_MAX:
        reste = len(noms) - AGENTS_NOMMES_MAX
        noms = noms[:AGENTS_NOMMES_MAX] + [_PHRASES[langue_]["et_autres"].format(n=reste)]
    return ", ".join(noms)


def grouper(eps: list[dict], langue_: str, *, marque: str) -> list[list[dict]]:
    """Les messages d'un passage pour UNE org, rangés en paquets d'un message chacun :
    les alertes dont le client lirait EXACTEMENT le même problème (même titre, même
    geste, même lien — la même cause) partent ensemble, les retours ensemble ; une
    réparation part seule (chacune dit son geste). Un paquet d'un seul épisode est le
    message d'avant, mot pour mot."""
    paquets: dict[tuple, list[dict]] = {}
    for ep in eps:
        if ep["message"] == "alerte":
            pb = message_du_probleme(int(ep["org_id"]), ep, langue_, marque=marque,
                                     sub=ep.get("agent_sub"))
            cle_ = ("alerte", pb["headline"], pb.get("todo"), pb.get("link"),
                    pb.get("action"))
        elif ep["message"] == "retour":
            cle_ = ("retour",)
        else:
            cle_ = ("repare", ep["id"])
        paquets.setdefault(cle_, []).append(ep)
    return list(paquets.values())


def composer_paquet(eps: list[dict], langue_: str, *,
                    marque: str) -> tuple[str, Optional[list]]:
    """Le message d'un paquet (`grouper`) : celui d'un épisode seul (`composer`), ou un
    message qui nomme les agents qu'arrête la même cause, avec le geste une fois."""
    from . import links
    if len(eps) == 1:
        return composer(eps[0], langue_, marque=marque)
    p = _PHRASES[langue_]
    premier = eps[0]
    org, sub = int(premier["org_id"]), premier.get("agent_sub")
    if premier["message"] == "retour":
        return p["retour_n"].format(agents=_nommer(eps, langue_)), None
    pb = message_du_probleme(org, premier, langue_, marque=marque, sub=sub)
    ouverts = [e.get("ouvert_at") for e in eps if isinstance(e.get("ouvert_at"), datetime)]
    depuis = _depuis(min(ouverts) if ouverts else premier.get("ouvert_at"), langue_)
    lignes = [p["titre_n"].format(n=len(eps), depuis=depuis, agents=_nommer(eps, langue_)),
              pb["headline"]] + ([pb["todo"]] if pb.get("todo") else [])
    boutons = []
    if pb.get("link"):
        boutons.append({"type": "button", "style": "primary",
                        "text": {"type": "plain_text", "text": p["faire"]},
                        "url": pb["link"]})
    sante = links.link_for("agent_health", sub=sub, org=org)
    if sante:
        boutons.append({"type": "button", "text": {"type": "plain_text", "text": p["sante"]},
                        "url": sante})
    blocs = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lignes)}}]
    if boutons:
        blocs.append({"type": "actions", "elements": boutons})
    return "\n".join(lignes), blocs


def message_de_test(org_id: int) -> str:
    return _PHRASES[langue(org_id)]["test"].format(marque=marque_de_l_org(org_id).nom)


def balayer(maintenant: Optional[datetime] = None) -> dict:
    """Un passage : les orgs aux alertes allumées, leurs épisodes qui attendent un
    message, un message chacun, marqué après l'envoi. Synchrone. Lève `AlertesEteintes`
    si le réglage de l'instance est éteint : la boucle n'est alors pas armée."""
    from .alerte_agents_bloques import _heures_calmes, org_a_nous
    from .db import alertes_slack

    if not actives():
        raise AlertesEteintes(f"{ENV_ACTIVES} est éteint : aucune alerte Slack d'agent.")
    maintenant = maintenant or datetime.now(timezone.utc)
    if _heures_calmes(maintenant):
        return {"heures_calmes": True}
    reglages = {o: r for o, r in alertes_slack.orgs_a_prevenir().items()
                if org_a_nous(o, qui="alertes Slack des agents")}
    envoyes, rates, messages = 0, 0, 0
    par_org: dict[int, list[dict]] = {}
    for ep in alertes_slack.a_annoncer(list(reglages), attente_min=ATTENTE_MIN):
        par_org.setdefault(int(ep["org_id"]), []).append(ep)
    for org, eps in par_org.items():
        langue_, marque = langue(org), marque_de_l_org(org).nom
        for paquet in grouper(eps, langue_, marque=marque):
            texte, blocs = composer_paquet(paquet, langue_, marque=marque)
            if poster(org, reglages[org], texte, blocs):
                for ep in paquet:
                    alertes_slack.marquer(int(ep["id"]), ep["message"])
                envoyes += len(paquet)
                messages += 1
            else:
                rates += len(paquet)
    return {"orgs": len(reglages), "envoyes": envoyes, "messages": messages,
            "rates": rates}


async def run_alertes_slack_loop(interval: int = INTERVALLE_S) -> None:
    """La boucle de fond (`boucles_de_fond.py`, `tiers=True`)."""
    log.info("alertes Slack des agents : démarrée (passage toutes les %ss).", interval)
    while True:
        try:
            bilan = await asyncio.to_thread(balayer)
            if bilan.get("envoyes") or bilan.get("rates"):  # `messages` ≤ `envoyes`
                log.info("alertes Slack des agents : %s", bilan)
        except Exception as e:  # noqa: BLE001 — un passage raté ne tue pas la boucle
            log.warning("alertes Slack des agents : passage en échec : %s", e)
        await asyncio.sleep(interval)
