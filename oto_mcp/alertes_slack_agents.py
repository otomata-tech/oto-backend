"""Prévenir une org SUR SLACK qu'un de ses agents ne fonctionne plus (09/11/2026).

Le courriel au premier admin (`alerte_agents_bloques.py`) arrive au troisième travail
mort, dans une boîte qu'on lit rarement, et ne dit pas quoi faire. Le 09/11/2026, quatre
clients avaient un agent arrêté (Klark, 3Dnatives, Growth Room, Koncile) : deux se
réparaient de notre côté, deux demandaient un geste du client — une clé lemlist à
recréer, un abonnement Claude à reconnecter — et chacun a été écrit à la main, sur Slack.

Ce module écrit ces messages-là, sur le Slack de l'org, par SON app (le connecteur Slack
de l'org, `author="app"`), dans le canal réglé ou, sans canal, en message privé à la
personne qui a connecté Slack (`db/alertes_slack.py`). Trois messages, un de chaque par
épisode (`agent_episodes`), écrits dans la langue de l'espace :

- **l'agent ne fonctionne plus** — seulement quand le CLIENT doit agir (`cote =
  proprietaire`) : depuis quand, pourquoi, le geste, un bouton vers l'endroit où le
  faire (`message_client.link`, sinon l'agent avec `&fix=1`, qui ouvre la réparation
  avec Tulina dans l'app) et « Ouvrir l'agent ». Le texte est celui que l'agent médecin
  écrit pour le client (`oto_admin_agent_repair op=diagnose`, `client_*`) ; sans lui,
  une phrase par classe, et seulement après `ATTENTE_MIN` (le temps de laisser réparer
  ce qui se répare) ;
- **Tulina l'a réparé** — après une réparation du médecin (`runner_triggers.
  repair_note`) : une ligne, sa phrase ;
- **il refonctionne** — l'épisode guéri après une alerte.

Une panne de NOTRE côté (`plateforme`) ne s'écrit jamais au client. Une org d'un tenant
tiers est écartée (`alerte_agents_bloques.org_a_nous`). Rien de l'agent médecin n'appelle
un modèle ici : le texte est déjà écrit.

⚠️ La marque vient APRÈS l'envoi (`alertes_slack.marquer`), comme le courriel : un envoi
raté se rejoue au passage suivant, il ne devient pas un silence. Et le courriel aux admins
se TAIT pour un épisode annoncé sur Slack (`alerte_agents_bloques._annonces_sur_slack`) :
il reste le filet quand Slack n'est pas réglé ou refuse.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("oto_mcp.alertes_slack_agents")

#: La cadence : cinq minutes, comme le médecin des agents.
INTERVALLE_S = 300

#: Sans message écrit pour le client par l'agent médecin, on attend ce délai avant
#: d'alerter avec la phrase de la classe : il a le temps de réparer ce qui se répare.
ATTENTE_MIN = 120

_PHRASES = {
    "fr": {
        "titre": "⚠️ *{agent}* ne fonctionne plus depuis {depuis}",
        "repare": "🔧 *{agent}* — réparé par Tulina : {quoi}",
        "retour": "✅ *{agent}* refonctionne.",
        "reparer": "Réparer avec Tulina",
        "faire": "Le faire maintenant",
        "ouvrir": "Ouvrir l'agent",
        "test": "Test : c'est ici que Tulina vous préviendra quand un de vos agents ne "
                "fonctionne plus, avec le geste pour le réparer.",
        "classes": {
            "cle_refusee": ("Une clé ou un compte branché sur Tulina a été refusé.",
                            "Reconnectez-le dans Connecteurs : l'agent repartira tout seul.",
                            "connectors"),
            "identite": ("Le compte au nom duquel l'agent tourne n'est plus valide.",
                         "Ouvrez l'agent et enregistrez-le de nouveau.", None),
        },
        "defaut": ("Il s'est arrêté avant la fin de son travail.",
                   "Ouvrez l'agent : Tulina vous dit quoi faire.", None),
    },
    "en": {
        "titre": "⚠️ *{agent}* has not been working since {depuis}",
        "repare": "🔧 *{agent}* — fixed by Tulina: {quoi}",
        "retour": "✅ *{agent}* works again.",
        "reparer": "Fix it with Tulina",
        "faire": "Do it now",
        "ouvrir": "Open the agent",
        "test": "Test: this is where Tulina will tell you when one of your agents stops "
                "working, with what to do to fix it.",
        "classes": {
            "cle_refusee": ("A key or account connected to Tulina was refused.",
                            "Reconnect it in Connectors: the agent will restart on its own.",
                            "connectors"),
            "identite": ("The account the agent runs as is no longer valid.",
                         "Open the agent and save it again.", None),
        },
        "defaut": ("It stopped before finishing its work.",
                   "Open the agent: Tulina tells you what to do.", None),
    },
}


def message_du_probleme(org_id: int, episode: dict, langue: str) -> dict[str, Any]:
    """`{headline, todo, action, link}` pour le client : celui que l'agent médecin a
    écrit, sinon la phrase de la classe. `link` est un chemin de l'app (ou une URL)."""
    m = episode.get("message_client") or {}
    if m.get("headline"):
        return {"headline": m["headline"], "todo": m.get("todo"),
                "action": m.get("action") if m.get("action") in ("credits", "connectors")
                else None,
                "link": m.get("link")}
    p = _PHRASES[langue]
    titre, geste, action = p["classes"].get(episode.get("classe") or "", p["defaut"])
    lien = f"/org/{int(org_id)}/connectors" if action == "connectors" else None
    return {"headline": titre, "todo": geste, "action": action, "link": lien}


def _langue(org_id: int) -> str:
    """La langue de l'espace : celle du premier admin joignable, sinon de la marque."""
    from . import email_brand, org_store
    from .alerte_agents_bloques import destinataire_admin
    from .db import users as db_users
    try:
        dest = destinataire_admin(org_id)
        if dest:
            loc = (db_users.get_user(dest[0]) or {}).get("locale")
            if loc in ("fr", "en"):
                return loc
        return email_brand.marque(org_store.org_front(org_id)[1]).langue or "fr"
    # noqa: SILENT — une langue illisible n'empêche pas de prévenir : le français.
    except Exception:  # noqa: BLE001
        return "fr"


def _base(org_id: int, sub: Optional[str]) -> str:
    from . import config, org_store
    return (org_store.org_front(org_id)[0] or config.dashboard_url_for(sub)).rstrip("/")


def _absolu(base: str, lien: Optional[str]) -> Optional[str]:
    if not lien:
        return None
    return lien if lien.startswith(("http://", "https://")) else base + lien


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
    """Poste par l'app Slack de l'org. False (dit au journal) si rien n'est parti."""
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


def _depuis(quand: Any, langue: str) -> str:
    if not isinstance(quand, datetime):
        return str(quand or "")[:16]
    mois_fr = ("janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.",
               "oct.", "nov.", "déc.")
    if langue == "fr":
        return f"le {quand.day} {mois_fr[quand.month - 1]} à {quand:%H:%M} UTC"
    return f"{quand:%b} {quand.day} at {quand:%H:%M} UTC"


def composer(ep: dict, langue: str, base: str) -> tuple[str, Optional[list]]:
    """Le texte (et les blocs) du message que l'épisode attend (`ep["message"]`)."""
    p = _PHRASES[langue]
    agent = ep.get("libelle") or f"#{ep['trigger_id']}"
    org, tid = int(ep["org_id"]), int(ep["trigger_id"])
    if ep["message"] == "repare":
        quoi = ((ep.get("repair_note") or {}).get("what") or "").strip()
        return p["repare"].format(agent=agent, quoi=quoi), None
    if ep["message"] == "retour":
        return p["retour"].format(agent=agent), None
    pb = message_du_probleme(org, ep, langue)
    titre = p["titre"].format(agent=agent, depuis=_depuis(ep.get("ouvert_at"), langue))
    lignes = [titre, pb["headline"]] + ([pb["todo"]] if pb.get("todo") else [])
    agent_url = f"{base}/org/{org}/agents?agent={tid}"
    lien = _absolu(base, pb.get("link"))
    premier = ({"text": p["faire"], "url": lien} if lien
               else {"text": p["reparer"], "url": f"{agent_url}&fix=1"})
    boutons = [
        {"type": "button", "style": "primary",
         "text": {"type": "plain_text", "text": premier["text"]}, "url": premier["url"]},
        {"type": "button", "text": {"type": "plain_text", "text": p["ouvrir"]},
         "url": agent_url},
    ]
    blocs = [{"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lignes)}},
             {"type": "actions", "elements": boutons}]
    return "\n".join(lignes), blocs


def message_de_test(langue: str) -> str:
    return _PHRASES[langue]["test"]


def balayer(maintenant: Optional[datetime] = None) -> dict:
    """Un passage : les orgs aux alertes allumées, leurs épisodes qui attendent un
    message, un message chacun, marqué après l'envoi. Synchrone."""
    from .alerte_agents_bloques import _heures_calmes, org_a_nous
    from .db import alertes_slack

    maintenant = maintenant or datetime.now(timezone.utc)
    if _heures_calmes(maintenant):
        return {"heures_calmes": True}
    reglages = {o: r for o, r in alertes_slack.orgs_a_prevenir().items()
                if org_a_nous(o, qui="alertes Slack des agents")}
    envoyes, rates = 0, 0
    langues: dict[int, str] = {}
    for ep in alertes_slack.a_annoncer(list(reglages), attente_min=ATTENTE_MIN):
        org = int(ep["org_id"])
        langue = langues.setdefault(org, _langue(org))
        texte, blocs = composer(ep, langue, _base(org, None))
        if poster(org, reglages[org], texte, blocs):
            alertes_slack.marquer(int(ep["id"]), ep["message"])
            envoyes += 1
        else:
            rates += 1
    return {"orgs": len(reglages), "envoyes": envoyes, "rates": rates}


async def run_alertes_slack_loop(interval: int = INTERVALLE_S) -> None:
    """La boucle de fond (`boucles_de_fond.py`, `tiers=True`)."""
    log.info("alertes Slack des agents : démarrée (passage toutes les %ss).", interval)
    while True:
        try:
            bilan = await asyncio.to_thread(balayer)
            if bilan.get("envoyes") or bilan.get("rates"):
                log.info("alertes Slack des agents : %s", bilan)
        except Exception as e:  # noqa: BLE001 — un passage raté ne tue pas la boucle
            log.warning("alertes Slack des agents : passage en échec : %s", e)
        await asyncio.sleep(interval)
