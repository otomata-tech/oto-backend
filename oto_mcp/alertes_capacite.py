"""Prévenir une org que ses agents ne démarrent plus : aucun forfait ne peut les servir
(19/11/2026, `docs/alertes-de-capacite.md` §A).

Constaté le 19/11/2026 : une org dont tous les agents tournent sur abonnement, en pool,
avec un seul prêteur. Son forfait passe le seuil de l'org en début d'après-midi
(`paused_limit`, échéance dans plusieurs jours) ; pendant huit heures, rien ne démarre et
rien ne prévient. Les alertes d'agents (`alerte_agents_bloques`, `alertes_slack_agents`)
partent d'un ÉPISODE de travaux MORTS : un travail qui attend n'est pas mort, une
péremption non plus, et la cause est celle de l'org, pas d'un agent.

Ce module écrit l'alerte qui manquait, une par INCIDENT (`db/incidents_capacite.py`) :

- **le constat** — des travaux d'abonnement dus qu'AUCUN forfait ne peut servir, avec les
  prédicats de la réservation et sans repli qui les rejoue
  (`runner_jobs.travaux_sans_forfait`), classés par `_abonnement.cause_du_forfait` (le
  classement du texte de péremption) : `plafond`, `reconnexion`, `pool_vide`. Ouvert
  quand il tient `DELAI_MIN` minutes ;
- **à l'org** — un message sur son Slack, à la destination des alertes d'agents
  (`db/alertes_slack.py`, même app : `alertes_slack_agents.poster`), sinon le courriel au
  premier admin (`alerte_agents_bloques.destinataire_admin`) : depuis quand, combien
  d'agents allumés attendent, la cause AU NIVEAU DE DISCRÉTION de
  `_abonnement.raison_de_peremption` — jamais le nom d'un prêteur, son palier ni son usage
  (`org_subscription_pool.preteurs` ne les sert pas, ce message non plus) —, l'heure de
  reprise quand elle est connue, et les gestes (page Agents › Modèles) ;
- **au titulaire** du forfait (les prêteurs en pool, le propriétaire des agents sinon) —
  un courriel de plateforme : SON forfait, au plafond jusqu'à quand, ou à reconnecter. Lui
  seul peut lever une `reconnexion` ;
- **à la reprise** — une ligne : depuis quand, et combien d'occurrences ont été manquées.
  Seulement quand la capacité est revenue : plus rien qui attend alors que la cause
  tient (`la_cause_tient`), ce n'est pas une reprise, et rien ne le dit.

Rien n'appelle un modèle ici : le texte est fixe, en français ou en anglais (la langue de
l'espace, `alertes_slack_agents.langue`), sous la marque du tenant de l'org.

**Sous un réglage d'instance** (`ENV_ACTIVES`, lu par `interrupteurs.oui_non` à chaque
appel ; éteint par défaut, une valeur illisible LÈVE) : éteint, la boucle n'est pas armée
et `balayer` refuse de passer. Une org d'un tenant tiers est écartée et dite au journal
(`alerte_agents_bloques.org_a_nous`). Les heures calmes (`_heures_calmes`) retiennent les
ENVOIS, jamais le constat : une panne de la nuit dit « depuis 21 h », pas « depuis 8 h ».

⚠️ La marque d'envoi vient APRÈS l'envoi (`incidents_capacite.marquer_*`) : un envoi raté
se rejoue au passage suivant ; marquer d'abord ferait d'un échec un silence définitif — la
panne même que ce module supprime.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

from . import interrupteurs

log = logging.getLogger("oto_mcp.alertes_capacite")

#: Le réglage d'instance : les alertes de capacité. Éteint par défaut.
ENV_ACTIVES = "OTO_ALERTES_CAPACITE_ENABLED"

#: La cadence : cinq minutes, comme les alertes Slack d'agents.
INTERVALLE_S = 300

#: Combien de minutes l'attente doit tenir avant d'être un incident. Une file qui se vide
#: en quelques minutes n'est pas une panne ; un quart d'heure sans rien qui parte, si.
DELAI_MIN = 15

#: Combien de secondes une attente doit avoir cédé avant que l'incident se close : une
#: passe tombée pendant la sonde d'un forfait ne voit rien attendre sans que rien soit
#: reparti (`db/incidents_capacite.py`). Une passe de plus suffit.
DELAI_CLOTURE_S = INTERVALLE_S

#: L'ordre des causes quand les travaux d'une org n'attendent pas tous la même chose :
#: d'abord celles qui ne cèdent pas seules (un pool vide, une reconnexion), puis le
#: plafond, qui tombe à son échéance.
_PRIORITE = ("pool_vide", "reconnexion", "plafond")

#: Les orgs d'un tenant tiers déjà dites au journal par CE processus : une fois, pas
#: toutes les cinq minutes.
_ECARTEES_DITES: set[int] = set()


class AlertesCapaciteEteintes(RuntimeError):
    """Les alertes de capacité ne sont pas allumées sur cette instance."""


def actives() -> bool:
    """Le réglage de l'instance, lu à chaque appel. Une valeur illisible lève
    (`interrupteurs.InterrupteurIllisible`)."""
    return interrupteurs.oui_non(ENV_ACTIVES, os.environ.get(ENV_ACTIVES))


# ── Le constat ─────────────────────────────────────────────────────────────────────


def _rang(voie_cause: tuple[bool, str]) -> tuple[int, int]:
    pool, cause = voie_cause
    return (_PRIORITE.index(cause), 0 if pool else 1)


def _reprise(org_id: int, famille: str, bloques: list[dict]) -> Optional[datetime]:
    """L'échéance la plus proche qui relancera ces travaux au plafond : celle du pool
    (`pool_au_plafond.reprise`, la même lecture que le texte de péremption), celle du
    forfait de chaque propriétaire en voie personnelle. None : inconnue."""
    from .capabilities import _abonnement
    from .db import org_subscription_pool, user_subscriptions
    echeances = []
    if any(b["par_le_pool"] and b["cause"] == "plafond" for b in bloques):
        echeances.append(_abonnement.echeance_utc(
            org_subscription_pool.pool_au_plafond(org_id, famille, None)["reprise"]))
    for sub in sorted({b["sub"] for b in bloques
                       if not b["par_le_pool"] and b["cause"] == "plafond" and b["sub"]}):
        ligne = user_subscriptions.get_subscription(sub, famille) or {}
        echeances.append(_abonnement.echeance_utc(ligne.get("limit_reset_at")))
    maintenant = datetime.now(timezone.utc)
    futures = [e for e in echeances if e and e > maintenant]
    return min(futures) if futures else None


def _titulaires(org_id: int, famille: str, bloques: list[dict]) -> list[str]:
    """Qui tient le forfait qui manque : les prêteurs VIVANTS du pool pour un travail qui
    passe par lui (aucun pour un pool vide), le demandeur pour un travail en voie
    personnelle. Ceux-là seuls reçoivent le détail de leur propre forfait."""
    from .db import org_subscription_pool
    subs: set[str] = set()
    if any(b["par_le_pool"] and b["cause"] != "pool_vide" for b in bloques):
        subs |= {p["sub"] for p in org_subscription_pool.preteurs(org_id, famille)}
    subs |= {b["sub"] for b in bloques if not b["par_le_pool"] and b["sub"]}
    return sorted(subs)


def _constat(org_id: int, famille: str, lot: list[dict]) -> Optional[dict[str, Any]]:
    """Ce que la passe dit de l'incident d'une org et d'une famille, ou None si aucun
    travail du lot n'a de cause (l'état a changé entre la lecture et le diagnostic)."""
    from .capabilities import _abonnement
    causes: dict[tuple[bool, Optional[str]], Optional[str]] = {}
    bloques = []
    for t in lot:
        cle = (t["par_le_pool"], None if t["par_le_pool"] else t["sub"])
        if cle not in causes:
            causes[cle] = _abonnement.cause_du_forfait(org_id, famille, t["sub"],
                                                       pool=t["par_le_pool"])
        if causes[cle]:
            bloques.append({**t, "cause": causes[cle]})
    if not bloques:
        return None
    pool, cause = min({(b["par_le_pool"], b["cause"]) for b in bloques}, key=_rang)
    return {"org_id": org_id, "famille": famille, "cause": cause, "par_le_pool": pool,
            "travaux": len(bloques),
            "agents": len({b["trigger_id"] for b in bloques if b["trigger_id"]}),
            "reprise_at": _reprise(org_id, famille, bloques) if cause == "plafond" else None,
            "titulaires": _titulaires(org_id, famille, bloques)}


def la_cause_tient(inc: dict) -> bool:
    """La cause d'un incident tient-elle encore, travaux ou pas ? En voie du pool : le
    pool n'a toujours aucun prêteur servable (`cause_du_forfait`). En voie personnelle :
    un de ses titulaires est toujours au plafond ou à reconnecter. Quand plus rien
    n'attend alors qu'elle tient (un webhook périmé à sa fraîcheur, une file vidée, un
    agent éteint), l'incident se clôt sans dire « les agents ont repris »."""
    from .capabilities import _abonnement
    org, famille = int(inc["org_id"]), inc["famille"]
    if inc["par_le_pool"]:
        return _abonnement.cause_du_forfait(org, famille, None, pool=True) is not None
    return any(etat_du_titulaire(sub, famille) for sub in inc.get("titulaires") or ())


def constater() -> tuple[dict[str, int], dict[tuple[int, str], dict]]:
    """La moitié de la passe qui n'envoie rien : relève les travaux sans forfait, les range
    par (org, famille), écarte les orgs d'un tenant tiers, et fait avancer les incidents
    (`incidents_capacite.constater`). Rend le bilan, et ce que la passe a vu."""
    from . import org_store
    from .alerte_agents_bloques import org_a_nous
    from .capabilities import _abonnement
    from .db import incidents_capacite, user_subscriptions
    from .db import runner_jobs as db_runner_jobs

    travaux = db_runner_jobs.travaux_sans_forfait(
        _abonnement.DEFAUT_LIMITE_PCT, marge_s=user_subscriptions.VERIFICATION_S)
    par_cle: dict[tuple[int, str], list[dict]] = {}
    for t in travaux:
        par_cle.setdefault((t["org_id"], t["famille"]), []).append(t)
    vus: dict[tuple[int, str], dict] = {}
    ecartees = 0
    for (org_id, famille), lot in sorted(par_cle.items()):
        if not org_a_nous(org_id, qui="alertes de capacité"):
            ecartees += 1
            if org_id not in _ECARTEES_DITES:
                _ECARTEES_DITES.add(org_id)
                log.info("alertes de capacité : org %s d'un tenant tiers écartée "
                         "(%d travail(aux) sans forfait, aucun message)", org_id, len(lot))
            continue
        c = _constat(org_id, famille, lot)
        if c:
            vus[(org_id, famille)] = c
    # Ce que la passe ne voit plus : une org suspendue n'est pas lue (son incident reste
    # tel quel) ; ailleurs, plus rien n'attend — reste à savoir si la cause a cédé.
    suspendues = set(org_store.suspended_org_ids())
    tenus, sans_reprise = [], []
    for inc in incidents_capacite.vivants():
        cle = (int(inc["org_id"]), inc["famille"])
        if cle in vus:
            continue
        if cle[0] in suspendues:
            tenus.append(cle)
        elif la_cause_tient(inc):
            sans_reprise.append(cle)
    bilan = incidents_capacite.constater(
        vus.values(), delai_min_s=DELAI_MIN * 60, delai_cloture_s=DELAI_CLOTURE_S,
        tenus=tenus, sans_reprise=sans_reprise)
    return {**bilan, "tenant_tiers_ecartes": ecartees}, vus


# ── Les textes ─────────────────────────────────────────────────────────────────────

_PHRASES = {
    "fr": {
        "titre": "⏸️ *Vos agents ne démarrent plus depuis {depuis}*",
        "agents": ("1 agent allumé attend.", "{n} agents allumés attendent."),
        "travaux": ("1 travail attend.", "{n} travaux attendent."),
        "causes": {
            (True, "plafond"): "Le forfait partagé avec l'organisation est au plafond "
                               "de consommation.",
            (True, "reconnexion"): "Le forfait partagé avec l'organisation doit être "
                                   "reconnecté par la personne qui le prête.",
            (True, "pool_vide"): "Plus aucun forfait n'est partagé avec l'organisation.",
            (False, "plafond"): "Le forfait de leur propriétaire est au plafond de "
                                "consommation.",
            (False, "reconnexion"): "Le forfait de leur propriétaire doit être reconnecté.",
        },
        "reprise": "Ils repartiront seuls {quand}.",
        "gestes": {
            "plafond": "Pour les relancer plus tôt, dans {marque} › Agents › Modèles : "
                       "partagez un autre forfait avec l'organisation, ou faites-les "
                       "tourner sur une clé d'API.",
            "reconnexion": "Ils repartiront seuls dès qu'il sera reconnecté. Un autre "
                           "membre peut aussi partager son forfait avec l'organisation, "
                           "dans {marque} › Agents › Modèles.",
            "pool_vide": "Un membre peut partager son forfait avec l'organisation, dans "
                         "{marque} › Agents › Modèles : ils repartiront seuls.",
        },
        "partager": "Partager un forfait",
        "cle": "Ajouter une clé d'API",
        "sujet": "Vos agents ne démarrent plus",
        "retour": "✅ Les agents ont repris à {heure} UTC. {manquees}",
        "manquees": ("Aucune occurrence manquée pendant l'arrêt.",
                     "1 occurrence manquée pendant l'arrêt.",
                     "{n} occurrences manquées pendant l'arrêt."),
        "sujet_retour": "Vos agents ont repris",
        "titulaire": {
            "plafond": ("Votre forfait est au plafond : des agents attendent",
                        "Votre forfait, qui fait tourner les agents de l'organisation "
                        "{org}, a atteint son plafond de consommation.",
                        "Il se réinitialise {quand} : ils repartiront seuls.",
                        "Ils repartiront seuls à sa réinitialisation.",
                        "Voir mon forfait"),
            "reconnexion": ("Reconnectez votre forfait : des agents attendent",
                            "Votre forfait, qui fait tourner les agents de "
                            "l'organisation {org}, doit être reconnecté.",
                            "Reconnectez-le : ils repartiront seuls.",
                            "Reconnectez-le : ils repartiront seuls.",
                            "Reconnecter mon forfait"),
        },
        "depuis_n": "Depuis {depuis}, {attente}",
    },
    "en": {
        "titre": "⏸️ *Your agents have not been able to start since {depuis}*",
        "agents": ("1 enabled agent is waiting.", "{n} enabled agents are waiting."),
        "travaux": ("1 run is waiting.", "{n} runs are waiting."),
        "causes": {
            (True, "plafond"): "The plan shared with the organization has reached its "
                               "usage cap.",
            (True, "reconnexion"): "The plan shared with the organization must be "
                                   "reconnected by the person who shares it.",
            (True, "pool_vide"): "No plan is shared with the organization any more.",
            (False, "plafond"): "Their owner's plan has reached its usage cap.",
            (False, "reconnexion"): "Their owner's plan must be reconnected.",
        },
        "reprise": "They will restart on their own on {quand}.",
        "gestes": {
            "plafond": "To restart them sooner, in {marque} › Agents › Models: share "
                       "another plan with the organization, or run them on an API key.",
            "reconnexion": "They will restart on their own once it is reconnected. "
                           "Another member can also share their plan with the "
                           "organization, in {marque} › Agents › Models.",
            "pool_vide": "A member can share their plan with the organization, in "
                         "{marque} › Agents › Models: they will restart on their own.",
        },
        "partager": "Share a plan",
        "cle": "Add an API key",
        "sujet": "Your agents can no longer start",
        "retour": "✅ Agents running again since {heure} UTC. {manquees}",
        "manquees": ("No run missed during the outage.",
                     "1 run missed during the outage.",
                     "{n} runs missed during the outage."),
        "sujet_retour": "Your agents are running again",
        "titulaire": {
            "plafond": ("Your plan has reached its cap: agents are waiting",
                        "Your plan, which runs agents of the organization {org}, has "
                        "reached its usage cap.",
                        "It resets on {quand}: they will restart on their own.",
                        "They will restart on their own when it resets.",
                        "View my plan"),
            "reconnexion": ("Reconnect your plan: agents are waiting",
                            "Your plan, which runs agents of the organization {org}, "
                            "must be reconnected.",
                            "Reconnect it: they will restart on their own.",
                            "Reconnect it: they will restart on their own.",
                            "Reconnect my plan"),
        },
        "depuis_n": "Since {depuis}, {attente}",
    },
}


def _date(v: Any) -> Optional[datetime]:
    from .capabilities._abonnement import echeance_utc
    return echeance_utc(v) if v else None


def _quand(v: Any, langue_: str) -> str:
    """« le 19 nov. à 14:05 UTC » / « Nov 19 at 14:05 UTC » — le format des alertes
    d'agents (`alertes_slack_agents._depuis`)."""
    from .alertes_slack_agents import _depuis
    return _depuis(_date(v), langue_)


def _attente(inc: dict, p: dict) -> str:
    """Combien attendent : les agents allumés, sinon les travaux (sans agent)."""
    n, (un, plusieurs) = ((inc["agents"], p["agents"]) if inc.get("agents")
                          else (inc.get("travaux") or 0, p["travaux"]))
    return un if n == 1 else plusieurs.format(n=n)


def _boutons(inc: dict, p: dict) -> list[tuple[str, str]]:
    """Les gestes de l'org, sur la page Agents › Modèles (`links`, type déclaré
    `agent_models`) : partager un forfait, et, pour un plafond, une clé d'API — le seul
    repli qui existe ne prend qu'un forfait au plafond. Non déclarée : aucun bouton."""
    from . import links
    org = int(inc["org_id"])
    boutons = []
    partager = links.link_for("agent_models", org=org, section="plans")
    if partager:
        boutons.append((p["partager"], partager))
    if inc["cause"] == "plafond":
        cle = links.link_for("agent_models", org=org, section="key")
        if cle:
            boutons.append((p["cle"], cle))
    return boutons


def lignes_de_l_alerte(inc: dict, langue_: str, *, marque: str) -> list[str]:
    """Les phrases de l'alerte, dans l'ordre : le titre, combien attendent, la cause, la
    reprise quand elle est connue, les gestes. AUCUNE ne nomme un titulaire : la cause
    dit « le forfait partagé », « le forfait de leur propriétaire »."""
    p = _PHRASES[langue_]
    lignes = [p["titre"].format(depuis=_quand(inc["vu_at"], langue_)), _attente(inc, p),
              p["causes"][(bool(inc["par_le_pool"]), inc["cause"])]]
    if inc["cause"] == "plafond" and inc.get("reprise_at"):
        lignes.append(p["reprise"].format(quand=_quand(inc["reprise_at"], langue_)))
    lignes.append(p["gestes"][inc["cause"]].format(marque=marque))
    return lignes


def composer_alerte(inc: dict, langue_: str, *, marque: str) -> tuple[str, list]:
    """Le message Slack de l'alerte : le texte, et ses blocs (les boutons)."""
    texte = "\n".join(lignes_de_l_alerte(inc, langue_, marque=marque))
    blocs: list = [{"type": "section", "text": {"type": "mrkdwn", "text": texte}}]
    boutons = _boutons(inc, _PHRASES[langue_])
    if boutons:
        blocs.append({"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": libelle}, "url": url,
             **({"style": "primary"} if i == 0 else {})}
            for i, (libelle, url) in enumerate(boutons)]})
    return texte, blocs


def ligne_de_reprise(inc: dict, langue_: str) -> str:
    """La ligne de clôture : l'heure où les agents ont repris, les occurrences manquées."""
    p = _PHRASES[langue_]
    n = int(inc.get("perimees") or 0)
    aucune, une, plusieurs = p["manquees"]
    manquees = aucune if n == 0 else une if n == 1 else plusieurs.format(n=n)
    clos = _date(inc["clos_at"])
    return p["retour"].format(heure=f"{clos:%H:%M}" if clos else "", manquees=manquees)


# ── Les envois ─────────────────────────────────────────────────────────────────────


def _courriel(to: str, sujet: str, paragraphes: list[str],
              boutons: list[tuple[str, str]], *, brand: Optional[str],
              locale: Optional[str]) -> bool:
    """Un courriel de plateforme sous la marque de l'org (`email_brand`), comme celui des
    agents bloqués : des paragraphes déjà échappés, puis les boutons."""
    from . import email_brand as _charte
    from .email_templates import _envoyer
    m = _charte.marque(brand)
    locale = locale or m.langue or None
    corps = "".join(f'<p style="{_charte.PARA}">{p}</p>' for p in paragraphes)
    corps += "".join(_charte.bouton(m, url, libelle) for libelle, url in boutons)
    return _envoyer(m, to, sujet, _charte.page(
        m, corps, preheader=sujet,
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))


def _sans_gras(ligne: str) -> str:
    return ligne.replace("*", "")


#: `_dire_a_l_org` n'a trouvé personne à qui le dire : ni Slack réglé, ni admin joignable.
#: Rien ne se marque — le jour où l'org en a un, l'incident vivant part —, mais ce n'est
#: pas un envoi raté : le bilan le compte à part (`sans_destinataire`).
_PERSONNE = ""


def _dire_a_l_org(org_id: int, reglage: Optional[dict], texte: str,
                  blocs: Optional[list], sujet: str, paragraphes: list[str],
                  boutons: list[tuple[str, str]], langue_: str) -> Optional[str]:
    """Dit à l'org : sur son Slack s'il est réglé et accepte (`slack`), sinon par
    courriel au premier admin joignable (`courriel`). None : un envoi a été refusé ;
    `_PERSONNE` : personne à qui le dire. Dans les deux cas rien ne se marque, le
    passage suivant rejoue."""
    from . import alertes_slack_agents, org_store
    from .alerte_agents_bloques import destinataire_admin
    if reglage and alertes_slack_agents.poster(org_id, reglage, texte, blocs):
        return "slack"
    dest = destinataire_admin(org_id)
    if dest is None:
        return None if reglage else _PERSONNE
    _base, brand = org_store.org_front(org_id)
    if _courriel(dest[1], sujet, paragraphes, boutons, brand=brand, locale=langue_):
        return "courriel"
    log.warning("alertes de capacité : courriel refusé pour l'org %s — rejoué au passage "
                "suivant", org_id)
    return None


def _alerter(inc: dict, reglage: Optional[dict], langue_: str, marque: str) -> Optional[str]:
    from . import email as _email
    p = _PHRASES[langue_]
    texte, blocs = composer_alerte(inc, langue_, marque=marque)
    paragraphes = [_email._esc(_sans_gras(l))
                   for l in lignes_de_l_alerte(inc, langue_, marque=marque)]
    return _dire_a_l_org(int(inc["org_id"]), reglage, texte, blocs, p["sujet"],
                         paragraphes, _boutons(inc, p), langue_)


def _dire_la_reprise(inc: dict, reglage: Optional[dict], langue_: str) -> Optional[str]:
    from . import email as _email
    ligne = ligne_de_reprise(inc, langue_)
    return _dire_a_l_org(int(inc["org_id"]), reglage, ligne, None,
                         _PHRASES[langue_]["sujet_retour"], [_email._esc(ligne)], [],
                         langue_)


def etat_du_titulaire(sub: str, famille: str) -> Optional[tuple[str, Optional[datetime]]]:
    """L'état du forfait d'un titulaire, dans les mots de son courriel : `("plafond",
    échéance)` pour une pause à échéance future, `("reconnexion", None)` pour un forfait
    à reconnecter, déconnecté ou sans sandbox ; None s'il peut servir (rien à lui dire)."""
    from .capabilities._abonnement import echeance_utc
    from .db import user_subscriptions as US
    ligne = US.get_subscription(sub, famille) or {}
    statut = ligne.get("statut")
    if statut == US.PLAFOND:
        reset = echeance_utc(ligne.get("limit_reset_at"))
        if reset and reset > datetime.now(timezone.utc):
            return ("plafond", reset)
    if not ligne.get("sandbox_id") or statut in (US.A_RECONNECTER, US.DECONNECTE):
        return ("reconnexion", None)
    return None


def courriel_du_titulaire(sub: str, inc: dict, langue_: str) -> Optional[tuple]:
    """`(sujet, paragraphes, boutons)` du courriel à ce titulaire — SON forfait, qu'il est
    seul à voir —, ou None s'il n'a rien à faire."""
    from . import email as _email
    from . import links, org_store
    etat = etat_du_titulaire(sub, inc["famille"])
    if etat is None:
        return None
    cause, reset = etat
    sujet, constat, reprise, sans_date, bouton = _PHRASES[langue_]["titulaire"][cause]
    p = _PHRASES[langue_]
    org = int(inc["org_id"])
    nom = (org_store.get_org(org) or {}).get("name") or f"#{org}"
    paragraphes = [
        _email._esc(constat.format(org=nom)) + " " + _email._esc(p["depuis_n"].format(
            depuis=_quand(inc["vu_at"], langue_), attente=_attente(inc, p))),
        _email._esc(reprise.format(quand=_quand(reset, langue_)) if reset else sans_date)]
    lien = links.link_for("agent_models", sub=sub, org=org, section="plans")
    return sujet, paragraphes, ([(bouton, lien)] if lien else [])


def _prevenir_le_titulaire(sub: str, inc: dict) -> Optional[bool]:
    """Le courriel à un titulaire. True parti, False raté (rejoué), None rien à envoyer
    (il n'a plus rien à faire, ou aucune adresse connue — jamais une adresse inventée)."""
    from . import email_brand, org_store
    from .db import users as db_users
    # L'adresse comme pour les admins (`destinataire_admin`) : celle du compte, jamais
    # une adresse devinée.
    adresse = db_users.emails_by_subs([sub]).get(sub)
    if not adresse:
        return None
    fiche = db_users.get_user(sub) or {}
    _base, brand = org_store.org_front(int(inc["org_id"]))
    langue_ = fiche.get("locale") if fiche.get("locale") in _PHRASES else (
        email_brand.marque(brand).langue or "fr")
    message = courriel_du_titulaire(sub, inc, langue_)
    if message is None:
        return None
    sujet, paragraphes, boutons = message
    return _courriel(adresse, sujet, paragraphes, boutons, brand=brand, locale=langue_)


def prevenir() -> dict[str, int]:
    """La moitié de la passe qui envoie : les alertes des incidents ouverts, les courriels
    de leurs titulaires (ceux que la dernière passe a relevés), les lignes de reprise.
    Chaque envoi se marque après coup ; un raté se rejoue."""
    from . import alertes_slack_agents
    from .db import alertes_slack, incidents_capacite
    reglages = alertes_slack.orgs_a_prevenir()
    par_org: dict[int, tuple[str, str]] = {}

    def _de_l_org(org: int) -> tuple[str, str]:
        if org not in par_org:
            par_org[org] = (alertes_slack_agents.langue(org),
                            alertes_slack_agents.marque_de_l_org(org).nom)
        return par_org[org]

    bilan = {"alertes": 0, "titulaires": 0, "reprises": 0, "rates": 0,
             "sans_destinataire": 0}

    def _compter(par: Optional[str], cle: str) -> bool:
        bilan[cle if par else "sans_destinataire" if par == _PERSONNE else "rates"] += 1
        return bool(par)

    for inc in incidents_capacite.ouverts():
        org = int(inc["org_id"])
        if inc["alerte_at"] is None:
            langue_, marque = _de_l_org(org)
            par = _alerter(inc, reglages.get(org), langue_, marque)
            if _compter(par, "alertes"):
                incidents_capacite.marquer_alerte(int(inc["id"]), par)
        deja = set(inc.get("titulaires_prevenus") or ())
        for sub in inc.get("titulaires") or ():
            if sub in deja:
                continue
            parti = _prevenir_le_titulaire(sub, inc)
            if parti:
                incidents_capacite.marquer_titulaire(int(inc["id"]), sub)
                bilan["titulaires"] += 1
            elif parti is False:
                bilan["rates"] += 1
    for inc in incidents_capacite.a_dire_la_reprise():
        org = int(inc["org_id"])
        langue_, _marque = _de_l_org(org)
        if _compter(_dire_la_reprise(inc, reglages.get(org), langue_), "reprises"):
            incidents_capacite.marquer_reprise(int(inc["id"]))
    return bilan


def balayer(maintenant: Optional[datetime] = None) -> dict:
    """Un passage : le constat (toujours), puis les envois (hors heures calmes).
    Synchrone. Lève `AlertesCapaciteEteintes` si le réglage de l'instance est éteint :
    la boucle n'est alors pas armée."""
    from .alerte_agents_bloques import _heures_calmes
    if not actives():
        raise AlertesCapaciteEteintes(f"{ENV_ACTIVES} est éteint : aucune alerte de "
                                      "capacité.")
    maintenant = maintenant or datetime.now(timezone.utc)
    bilan, _vus = constater()
    if _heures_calmes(maintenant):
        return {**bilan, "heures_calmes": True}
    return {**bilan, **prevenir()}


async def run_alertes_capacite_loop(interval: int = INTERVALLE_S) -> None:
    """La boucle de fond (`boucles_de_fond.py`, `tiers=True`)."""
    log.info("alertes de capacité : démarrée (passage toutes les %ss).", interval)
    from .db import verrou_de_boucle
    while True:
        try:
            # La marque vient APRÈS l'envoi : deux processus sur la même base (bleu/vert)
            # posteraient deux fois le même message. Un seul joue le passage
            # (`db/verrou_de_boucle.py`) ; l'index partiel tient le reste.
            bilan = await asyncio.to_thread(verrou_de_boucle.sous_verrou,
                                            "alertes_capacite", balayer, si_saute={})
            if any(bilan.get(k) for k in ("ouverts", "clos", "alertes", "titulaires",
                                          "reprises", "rates")):
                log.info("alertes de capacité : %s", bilan)
        except Exception as e:  # noqa: BLE001 — un passage raté ne tue pas la boucle
            log.warning("alertes de capacité : passage en échec : %s", e)
        await asyncio.sleep(interval)
