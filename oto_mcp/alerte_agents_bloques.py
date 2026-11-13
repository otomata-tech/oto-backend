"""Prévenir l'admin d'une org quand un de ses agents programmés est BLOQUÉ (signal 1168).

Une routine a buté 33 passages de suite sur une clé refusée par le fournisseur, et
personne n'a été prévenu : chaque passage écrivait son compte rendu dans un canal que
plus personne ne lisait. Un compte rendu dans le canal que l'agent tient n'est pas une
escalade. Ce module en est une, et la plus petite possible :

- **qui** : l'admin de l'org PROPRIÉTAIRE de l'agent (`runner_triggers.org_id`), par le
  courrier de PLATEFORME, sous la marque de l'org (`org_store.org_front`) — jamais par
  un connecteur de l'org, qui peut être précisément ce qui est tombé ;
- **quand** : un épisode — les travaux morts consécutifs depuis le dernier succès — qui
  atteint `SEUIL` (déclaré ici), annoncé UNE fois (`db/alertes_agents.py`), hors des
  heures calmes par défaut du courrier (`scheduler.DEFAULT_QUIET_HOURS`) : un épisode
  vu la nuit part au premier passage du matin ;
- **un courriel par org**, quel que soit le nombre d'agents bloqués ;
- **où** : seulement la PRODUCTION — la boucle se déclare `tiers=True` dans
  `boucles_de_fond.py`, la préprod (même base) et le développement ne la composent pas ;
- ⚠️ **un agent dont la panne est la NÔTRE est écarté** (`medecin_agents.py`) : un
  épisode du côté plateforme ne s'annonce pas au titulaire, qui n'y peut rien — tant
  que la boucle le VOIT (`medecin_agents.FRAICHEUR`) : boucle éteinte, l'alerte reprend ;
- ⚠️ **une org d'un tenant tiers est ÉCARTÉE** (`db.org_tenant_slug`, fermé sur
  erreur) : on ne s'adresse pas aux clients d'un partenaire dans son produit sans une
  règle que le tenant aurait déclarée — il n'en existe aucune pour ce courrier
  (`docs/tenants.md`, convention du 08/10/2026 : rien de neuf par tenant). L'écart est
  compté et journalisé, jamais silencieux.

**Ce que dit le courriel** (14/11/2026) : sous le réglage d'instance des alertes
(`alertes_slack_agents.actives`), il parle comme Slack. Le texte de chaque agent est celui
du problème de son épisode vivant (`alertes_slack_agents.message_du_probleme` : la copie de
la cause approuvée, sinon le texte du médecin, sinon la phrase de la classe), et les agents
dont le client lirait exactement le même problème forment UN bloc qui les nomme, avec le
geste une fois et son bouton (`links`, comme Slack). ⚠️ Le motif BRUT (`motif` : un nom
d'outil, un code, un identifiant) n'y entre jamais : sans épisode lisible, c'est la phrase
par défaut. Réglage éteint, le courriel d'avant, mot pour mot.

⚠️ La marque vient APRÈS l'envoi : marquer d'abord ferait d'un envoi raté un silence
définitif — la panne même que ce module supprime.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("oto_mcp.alerte_agents_bloques")

#: Le seuil DÉCLARÉ : trois travaux morts de suite. Un échec isolé se rejoue (le travail
#: retente déjà, `max_attempts`) ; deux peuvent être un amont instable une nuit ; trois
#: passages d'un agent quotidien, c'est trois jours de travail perdu.
SEUIL = 3

#: La cadence de la boucle. L'épisode s'annonce au plus tard un quart d'heure après le
#: travail qui l'ouvre (hors heures calmes).
INTERVALLE_S = 900

#: Les orgs d'un tenant tiers déjà dites au journal par CE processus : on le dit une fois,
#: pas toutes les quinze minutes.
_ECARTEES_DITES: set[int] = set()


def destinataire_admin(org_id: int) -> Optional[tuple[str, str]]:
    """`(sub, adresse)` du premier admin de l'org qui a une adresse, ou None.

    La même règle que l'alerte de clé (`maintenance.alertes_credential`), qui l'appelle :
    une seule source pour « qui prévient-on dans une org »."""
    from . import org_store
    from .db import users as db_users

    admins = [m["sub"] for m in org_store.list_org_members(org_id)
              if m.get("org_role") == "org_admin"]
    adresses = db_users.emails_by_subs(admins)
    sub = next((s for s in admins if adresses.get(s)), None)
    return (sub, adresses[sub]) if sub else None


def org_a_nous(org_id: int, *, qui: str) -> bool:
    """L'org relève-t-elle du tenant primaire, sur réponse franche ? Fermé sur erreur.
    `qui` = le dispositif qui demande, nommé au journal. Partagé avec la boucle
    `medecin_agents`, qui écarte les mêmes orgs : une seule règle pour « cette org
    est-elle la nôtre »."""
    from . import db, tenancy
    try:
        return db.org_tenant_slug(int(org_id)) == tenancy.primary_slug()
    # noqa: SILENT — fermé par défaut, et dit : l'org est écartée et comptée.
    except Exception:  # noqa: BLE001
        log.warning("%s : tenant de l'org %s illisible — org écartée", qui, org_id,
                    exc_info=True)
        return False


def _heures_calmes(maintenant: datetime) -> bool:
    from . import scheduler
    return scheduler.compute_scheduled_at(maintenant, None, None, False) is not None


def problemes(org_id: int, agents: list[dict], langue_: str, *, marque: str,
              sub: Optional[str]) -> list[dict]:
    """Les agents bloqués d'une org rangés par PROBLÈME, comme Slack (`grouper`) :
    `[{headline, todo, action, link, since, agents: [{id, label, url}]}]`, dans l'ordre du
    premier agent de chaque problème. Le texte vient de l'épisode vivant de l'agent
    (`message_du_probleme`) ; un agent sans épisode lisible reçoit la phrase par défaut
    — jamais son motif brut. `sub` = le destinataire : les liens sont ceux de SON tableau
    de bord."""
    from . import alertes_slack_agents as A
    from . import links
    from .db import sante_agents
    try:
        vivants = {int(e["trigger_id"]): e for e in sante_agents.episodes_vivants(org_id)}
    except Exception:  # noqa: BLE001 — ouvert : le courriel part, sur la phrase par défaut
        log.warning("alerte agents bloqués : épisodes de l'org %s illisibles — phrase par "
                    "défaut", org_id, exc_info=True)
        vivants = {}
    out: dict[tuple, dict] = {}
    for a in agents:
        tid = int(a["trigger_id"])
        ep = vivants.get(tid) or {"classe": None, "cote": "proprietaire"}
        if ep.get("cote") == "inconnu" and not (ep.get("message_client") or {}).get("headline"):
            # Pas encore compris, et le médecin n'a rien écrit : on ne dit pas au client
            # que c'est à lui (la phrase d'une classe le ferait) — comme l'onglet Santé.
            pb = {"headline": _PAS_COMPRIS[langue_], "todo": None, "action": None,
                  "link": None}
        else:
            pb = A.message_du_probleme(org_id, ep, langue_, marque=marque, sub=sub)
        cle = (pb["headline"], pb.get("todo"), pb.get("link"), pb.get("action"))
        p = out.setdefault(cle, {**pb, "agents": [], "since": None})
        depuis = str(a.get("depuis") or "")[:16] or None
        if depuis and (p["since"] is None or depuis < p["since"]):
            p["since"] = depuis
        p["agents"].append({"id": tid, "label": a.get("libelle") or f"#{tid}",
                            "url": links.link_for("agent", sub=sub, org=int(org_id),
                                                  id=tid)})
    return list(out.values())


#: Ce qui se dit d'un agent dont la panne n'est pas encore comprise et que le médecin
#: n'a pas encore dite (`cote = inconnu`, sans `message_client`) : la phrase de l'onglet
#: Santé (`capabilities/agent_health._A_NOUS`), jamais la phrase d'une classe.
_PAS_COMPRIS = {
    "fr": "Nous cherchons pourquoi il s'est arrêté. Nous vous dirons s'il y a quelque "
          "chose à faire.",
    "en": "We are looking into why it stopped. We will tell you if there is anything to "
          "do.",
}

#: Le courriel qui parle comme Slack, par langue (`problemes`).
_PAR_PROBLEME = {
    "fr": {"sujet": "Un agent programmé est bloqué",
           "intro": "Ces agents programmés de votre organisation ne fonctionnent plus :",
           "faire": "Le faire maintenant",
           "depuis": "En échec depuis le {depuis} UTC",
           "fin": "Ils continueront de partir à l'heure et d'échouer de la même façon tant "
                  "que ce n'est pas fait — ou coupez l'agent. C'est le seul message pour "
                  "cette panne."},
    "en": {"sujet": "A scheduled agent is blocked",
           "intro": "These scheduled agents of your organization have stopped working:",
           "faire": "Do it now",
           "depuis": "Failing since {depuis} UTC",
           "fin": "They will keep starting on schedule and fail the same way until this is "
                  "done — or turn the agent off. This is the only message for this "
                  "outage."},
}


def _courriel_par_probleme(m, to: str, blocs: list[dict], *, locale: Optional[str],
                           en: bool) -> bool:
    """Le courriel sous le réglage des alertes : un bloc par problème, qui nomme ses
    agents (chacun lien vers sa page quand l'instance la déclare), dit le problème, le
    geste, et son bouton. Tout texte est échappé : il vient du médecin ou d'un
    opérateur."""
    from . import email as _email
    from . import email_brand as _charte
    from .email_templates import _envoyer

    esc, attr = _email._esc, _email._esc_attr
    p = _PAR_PROBLEME["en" if en else "fr"]

    def _nom(a: dict) -> str:
        nom = f"<strong>{esc(a['label'])}</strong>"
        return f'<a href="{attr(a["url"])}">{nom}</a>' if a.get("url") else nom
    corps = [f'<p style="{_charte.PARA}">{esc(p["intro"])}</p>']
    for b in blocs:
        depuis = (f'<br>{esc(p["depuis"].format(depuis=b["since"]))}'
                  if b.get("since") else "")
        corps.append(f'<p style="{_charte.PARA}">{", ".join(_nom(a) for a in b["agents"])}'
                     f'{depuis}<br>{esc(b["headline"])}'
                     + (f'<br>{esc(b["todo"])}' if b.get("todo") else "") + "</p>")
        if b.get("link"):
            corps.append(_charte.bouton(m, b["link"], p["faire"]))
    corps.append(f'<p style="{_charte.PARA}">{esc(p["fin"])}</p>')
    return _envoyer(m, to, p["sujet"], _charte.page(
        m, "".join(corps), preheader=p["sujet"],
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))


def _courriel(to: str, agents: list[dict], *, brand: Optional[str],
              locale: Optional[str], org_id: Optional[int] = None,
              sub: Optional[str] = None, par_probleme: bool = False) -> bool:
    """UN message pour les agents bloqués d'une org. `par_probleme` (réglage des
    alertes allumé, `org_id` et `sub` du destinataire requis) : il parle comme Slack
    (`problemes`). Sinon, le courriel d'avant : lequel, depuis quand, combien de
    passages perdus, ce que dit le dernier refus — et les deux gestes possibles."""
    from . import email as _email
    from . import email_brand as _charte
    from .email_templates import _envoyer

    m = _charte.marque(brand)
    locale = locale or m.langue or None
    en = locale == "en"
    if par_probleme and org_id is not None:
        blocs = problemes(int(org_id), agents, "en" if en else "fr", marque=m.nom, sub=sub)
        return _courriel_par_probleme(m, to, blocs, locale=locale, en=en)
    esc = _email._esc
    items = []
    for a in agents:
        depuis = str(a.get("depuis") or "")[:16]
        motif = f" — « {esc(a['motif'])} »" if a.get("motif") else ""
        if en:
            items.append(f"<li><strong>{esc(a['libelle'])}</strong>: {a['travaux']} runs "
                         f"in a row have failed since {esc(depuis)} UTC{motif}</li>")
        else:
            items.append(f"<li><strong>{esc(a['libelle'])}</strong> : {a['travaux']} "
                         f"passages de suite en échec depuis le {esc(depuis)} UTC{motif}</li>")
    liste = f'<ul style="{_charte.PARA}">{"".join(items)}</ul>'
    if en:
        subject = "A scheduled agent is blocked"
        corps = (f'<p style="{_charte.PARA}">These scheduled agents of your organization '
                 "keep failing, run after run:</p>" + liste +
                 f'<p style="{_charte.PARA}">They will keep starting on schedule and fail '
                 "the same way until the cause is fixed (often a key refused by its "
                 "provider): fix it, or turn the agent off. This is the only message "
                 "for this outage.</p>")
    else:
        subject = "Un agent programmé est bloqué"
        corps = (f'<p style="{_charte.PARA}">Ces agents programmés de votre organisation '
                 "échouent passage après passage :</p>" + liste +
                 f'<p style="{_charte.PARA}">Ils continueront de partir à l\'heure et '
                 "d'échouer de la même façon tant que la cause n'est pas réparée (souvent "
                 "une clé refusée par son fournisseur) : réparez-la, ou coupez l'agent. "
                 "C'est le seul message pour cette panne.</p>")
    return _envoyer(m, to, subject, _charte.page(
        m, corps, preheader=subject,
        mention=_charte.mention_transactionnelle(m, locale), locale=locale))


def _soignes_par_nous(trigger_ids: list) -> set[int]:
    """Les agents dont la panne est du côté PLATEFORME (`medecin_agents.py`) ET que la
    boucle tient encore (épisode vu depuis moins de `medecin_agents.FRAICHEUR`) : leur
    titulaire n'y peut rien, on ne lui écrit pas. Une boucle éteinte ou arrêtée cesse de
    voir ses épisodes, et l'alerte reprend d'elle-même. Ouvert sur erreur — une lecture
    ratée laisse partir le courriel : prévenir pour rien vaut mieux que se taire à tort."""
    from . import medecin_agents
    from .db import episodes_agents
    try:
        return episodes_agents.agents_soignes_par_nous(
            trigger_ids, vus_depuis=medecin_agents.FRAICHEUR)
    except Exception:  # noqa: BLE001
        log.warning("alerte agents bloqués : épisodes illisibles — aucun agent écarté",
                    exc_info=True)
        return set()


def _annonces_sur_slack(trigger_ids: list) -> set[int]:
    """Les agents dont l'épisode vivant a déjà été annoncé sur le Slack de l'org
    (`alertes_slack_agents`) : le courriel serait le même message, en moins utile. Il
    reste le filet quand Slack n'est pas réglé ou a refusé (rien n'est alors marqué).
    L'appelant ne le demande que si l'instance a allumé ces alertes."""
    from .db._conn import _connect
    if not trigger_ids:
        return set()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT trigger_id FROM agent_episodes WHERE statut <> 'clos' "
            "AND slack_alerte_at IS NOT NULL AND trigger_id = ANY(%s)",
            ([int(t) for t in trigger_ids],)).fetchall()
    return {int(r["trigger_id"]) for r in rows}


def balayer(maintenant: Optional[datetime] = None) -> dict:
    """Un passage : relève les épisodes à annoncer, écrit un courriel par org, marque ce
    qui est parti. Synchrone (la boucle le pousse hors de l'event loop)."""
    from . import org_store
    from .db import alertes_agents
    from .db import users as db_users

    maintenant = maintenant or datetime.now(timezone.utc)
    if _heures_calmes(maintenant):
        return {"heures_calmes": True}
    bloques = alertes_agents.agents_bloques(SEUIL)
    soignes = _soignes_par_nous([a["trigger_id"] for a in bloques])
    from . import alertes_slack_agents
    par_probleme = alertes_slack_agents.actives()
    if par_probleme:
        soignes |= _annonces_sur_slack([a["trigger_id"] for a in bloques])
    par_org: dict[int, list[dict]] = {}
    for a in bloques:
        if int(a["trigger_id"]) in soignes:
            continue
        par_org.setdefault(int(a["org_id"]), []).append(a)
    envoyes, marques, ecartees, sans_destinataire = 0, 0, 0, 0
    for org_id, agents in sorted(par_org.items()):
        if not org_a_nous(org_id, qui="alerte agents bloqués"):
            ecartees += 1
            if org_id not in _ECARTEES_DITES:
                _ECARTEES_DITES.add(org_id)
                log.info("alerte agents bloqués : org %s d'un tenant tiers écartée "
                         "(%d agent(s) bloqué(s), aucun courriel)", org_id, len(agents))
            continue
        dest = destinataire_admin(org_id)
        if dest is None:
            # On ne marque PAS : le jour où l'org gagne un admin joignable, l'épisode part.
            sans_destinataire += 1
            continue
        sub, adresse = dest
        _base, marque = org_store.org_front(org_id)
        fiche = db_users.get_user(sub) or {}
        if _courriel(adresse, agents, brand=marque, locale=fiche.get("locale"),
                     org_id=org_id, sub=sub, par_probleme=par_probleme):
            envoyes += 1
            marques += alertes_agents.marquer_annonces([a["trigger_id"] for a in agents])
        else:
            log.warning("alerte agents bloqués : envoi refusé pour l'org %s — rejoué au "
                        "passage suivant", org_id)
    return {"orgs": len(par_org), "envoyes": envoyes, "marques": marques,
            "tenant_tiers_ecartes": ecartees, "sans_destinataire": sans_destinataire}


async def run_alerte_agents_loop(interval: int = INTERVALLE_S) -> None:
    """La boucle de fond (`boucles_de_fond.py`, `tiers=True`) : un passage par
    `interval`, hors de l'event loop. Un passage raté ne tue pas la boucle — il le dit."""
    log.info("alerte agents bloqués : démarrée (seuil %s, passage toutes les %ss).",
             SEUIL, interval)
    while True:
        try:
            bilan = await asyncio.to_thread(balayer)
            if bilan.get("envoyes"):
                log.info("alerte agents bloqués : %s", bilan)
        except Exception as e:  # noqa: BLE001 — un passage raté ne tue pas la boucle
            log.warning("alerte agents bloqués : passage en échec : %s", e)
        await asyncio.sleep(interval)
