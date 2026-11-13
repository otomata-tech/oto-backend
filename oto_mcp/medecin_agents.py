"""Soigner un agent hébergé AVANT que son titulaire ne le remarque (27/10/2026).

L'alerte au titulaire (`alerte_agents_bloques.py`) annonce une panne au troisième
travail mort. Or relevé sur une semaine de production, presque toutes les pannes
étaient les NÔTRES — aucun worker ne servait la famille du modèle, un refus passager
du fournisseur, un défaut du runner qui conclut en échec un run réussi — et le
titulaire recevait un courriel sur une panne qu'il ne pouvait pas réparer.

Cette boucle passe toutes les cinq minutes, AVANT l'alerte :

1. elle RELÈVE les agents allumés qui ont un travail mort depuis leur dernier succès
   (`db/episodes_agents.pannes_en_cours`), dès le premier ;
2. elle RANGE chaque panne dans une classe (`classer`) qui dit QUI peut la réparer
   (`plateforme`, `proprietaire`, `inconnu`), et tient l'épisode (`agent_episodes`) ;
3. elle SOIGNE ce qui se soigne sans personne — et seulement ça :
   - un refus PASSAGER (`refus_passager`) : le travail mort est relancé une fois ;
   - une famille de modèle que personne ne servait : relancé une fois, et seulement
     quand un worker vivant la sert de nouveau pour cette org ;
   jamais plus de `RELANCES_MAX` relances par épisode, une seule par travail mort (garde
   en base, `noter_reparation`), et jamais quand une occurrence de l'agent attend déjà
   dans la file ou tourne ;
4. elle CLÔT les épisodes guéris (un succès après l'ouverture) ou dont l'agent a été
   éteint.

Ce qu'elle ne fait JAMAIS : toucher une clé, une identité, une procédure, un horaire,
ou une donnée ; relancer une panne qu'elle ne sait pas nommer (un run qui a agi avant de
mourir referait ses effets — courriels, écritures — une seconde fois). Un épisode du côté du TITULAIRE passe `escalade` : c'est l'alerte existante qui le lui dit.
Un épisode `inconnu` reste `ouvert` pour l'agent médecin (`oto_admin_agent_repair`).
Une panne plateforme qui résiste à `RELANCES_MAX` relances s'escalade chez NOUS : au
journal ET au suivi d'erreurs (`_alerte_exploitation`).

⚠️ Une org d'un tenant tiers est ÉCARTÉE, relevé comme relance — la même règle que
l'alerte (`alerte_agents_bloques.org_a_nous`, fermée sur erreur) : on ne refait pas agir
l'agent d'un client de partenaire sans règle déclarée par le tenant.

⚠️ Une relance est TENUE par la règle de péremption (#814) : si aucun worker ne la prend
avant l'occurrence suivante, le tick la périme. Relancer ne fabrique donc jamais
d'arriéré.

⚠️ L'alerte au titulaire se TAIT pour un épisode du côté plateforme
(`episodes_agents.agents_soignes_par_nous`) : le titulaire n'y peut rien. Une panne
plateforme qui ne guérit pas se lit dans `oto_admin_agent_repair op=list` et au journal,
pas dans la boîte du client. Elle ne se tait que tant que la boucle VOIT l'épisode
(`FRAICHEUR`) : boucle éteinte ou arrêtée, l'alerte reprend d'elle-même.

⚠️ Une classe posée à la main (`diagnose`) n'est plus réécrite par `classer`
(`agent_episodes.classe_manuelle`).

**Le catalogue des causes, en OMBRE** (13/11/2026) : chaque panne reçoit l'empreinte de son
motif (`causes_pannes.de`), posée sur l'épisode ; une empreinte neuve entre au catalogue
(`db/causes_agents.relever`, un lot par passage) avec la classe de la regex. Le catalogue
ne décide d'AUCUN geste : le bilan dit ce qu'il aurait décidé (`catalogue_*`) — combien de
pannes il reconnaît par une cause approuvée, combien d'empreintes neuves, et les
DÉSACCORDS (cause approuvée d'une autre classe que la regex), dits au journal. Une ombre
qui casse ne casse pas le passage : elle le dit.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

log = logging.getLogger("oto_mcp.medecin_agents")

#: La cadence : cinq minutes, trois fois plus serrée que l'alerte (quinze), pour qu'un
#: travail mort soit relancé avant que le troisième n'ouvre le courriel.
INTERVALLE_S = 300

#: Au plus deux relances par épisode. Au-delà, ce qui échoue encore n'est plus passager.
RELANCES_MAX = 2

#: Un travail mort plus vieux que ça ne se relance plus : son occurrence est passée.
AGE_MAX = timedelta(hours=24)

#: Un épisode que la boucle n'a pas vu depuis six passages n'est plus TENU par elle :
#: l'alerte au titulaire cesse de se taire pour lui (`agents_soignes_par_nous`). Six,
#: pour qu'un passage lent ou raté n'ouvre pas le courriel, et qu'une boucle éteinte
#: le rouvre en une demi-heure — moins que la cadence de l'alerte n'en met à trois
#: travaux morts.
FRAICHEUR = timedelta(seconds=6 * INTERVALLE_S)

#: Au plus tant de relances AUTOMATIQUES par passage, toutes orgs confondues, servies
#: à tour de rôle par org : une panne du fournisseur qui touche cent agents ne les
#: renvoie pas tous dans la même minute, et une grosse org ne passe pas avant les autres.
#: Ce qui n'est pas servi attend le passage suivant, sans rien dépenser de son plafond.
RELANCES_PAR_PASSAGE = 20

#: Chaque relance automatique part avec un délai tiré entre 0 et ce nombre de secondes :
#: les relances d'un passage n'arrivent pas en rafale chez le fournisseur.
GIGUE_MAX_S = 240

#: Une même classe du côté de la plateforme dans au moins `INCIDENT_ORGS` orgs ET au
#: moins `INCIDENT_PART` des orgs en panne à ce passage est un INCIDENT : une cause
#: commune, pas des pannes d'agents. La part compte : à mille clients, trois orgs avec un
#: refus passager au même passage est le bruit de fond, pas une panne du fournisseur — un
#: seuil absolu ouvrirait un incident qui ne se ferme jamais. Dit une fois chez nous
#: (journal + suivi d'erreurs) à son ouverture et au journal à sa fin, au lieu d'une
#: escalade par agent ; et ses relances se bornent à `INCIDENT_SONDES` par passage —
#: des sondes, le temps que la cause passe.
INCIDENT_ORGS = 3
INCIDENT_PART = 0.3
INCIDENT_SONDES = 2

ACTEUR = "medecin"

QUI = "médecin des agents"

#: Les refus de la trace (`noter_reparation` rend None), relus sur l'épisode pour être
#: DITS : seul le plafond escalade — un travail qu'un autre processus vient de relancer
#: n'est pas une panne qui résiste.
REFUS_CLOS = "épisode clos"
REFUS_DEJA_RELANCE = "ce travail a déjà été relancé"
REFUS_PLAFOND = "plafond de relances atteint"

#: Les orgs d'un tenant tiers déjà dites au journal par CE processus.
_ECARTEES_DITES: set[int] = set()

#: Les désaccords du catalogue déjà dits au journal par CE processus : `(cle, classe)`.
_DESACCORDS_DITS: set[tuple] = set()

#: Les incidents ouverts, vus par CE processus : la classe → les orgs touchées.
_INCIDENTS: dict[str, int] = {}

# Les classes, dans l'ordre où on les essaie : (classe, côté, motif reconnu). Un motif
# est TRONQUÉ à `MOTIF_TETE` caractères (`pannes_en_cours`) : chaque fragment tient
# dans la tête du vrai message (`tests/test_medecin_agents.py` les formate).
# ⚠️ `fin_anormale (success)` n'est PAS un défaut du runner : c'était un refus du
# fournisseur (`is_error` sous un `subtype` « success ») mal libellé par l'ancien runner,
# corrigé depuis. Il tombe en `inconnu`, jamais en relance automatique.
# ⚠️ `famille_non_servie` = le texte de `_abonnement._famille_non_servie` (et l'ancien
# générique), RIEN de plus large : « … ne tournent que sur la clé déposée par
# l'organisation » (`runner_jobs._SANS_CLE_DEPOSEE`) est une clé que le titulaire n'a
# pas déposée — classée plateforme, l'alerte se taisait sur une panne qu'il est seul à
# pouvoir réparer.
_CLASSES: tuple[tuple[str, str, "re.Pattern[str]"], ...] = (
    ("famille_non_servie", "plateforme", re.compile(
        r"aucun worker ne sert la famille|aucun agent ne dessert", re.I)),
    ("cle_refusee", "proprietaire", re.compile(
        # Les deux refus « clé non déposée » de la réservation : `runner_jobs.
        # _SANS_CLE_DEPOSEE` et `_cle_exigee.raison_du_refus`.
        r"ne tournent que sur la clé déposée|n'a pas déposé sa clé de modèle"
        r"|cle_sans_workspace|clé d'api invalide|invalid[ _]x-api-key|invalid_api"
        r"|authenticat|unauthori[sz]ed|forbidden|\bHTTP 40[13]\b|insufficient"
        r"|quota|crédit|credit|billing", re.I)),
    ("identite", "proprietaire", re.compile(
        r"identité invalide|ne nomme personne", re.I)),
)


def classer(motif: Optional[str]) -> tuple[str, str]:
    """`(classe, côté)` d'un motif d'échec. Le refus passager se lit par la règle de la
    file (`db.runner_jobs.refus_passager`), la même qui règle son recul : un motif que
    la file tient pour passager l'est ici aussi."""
    from .db.runner_jobs import refus_passager
    texte = motif or ""
    for classe, cote, motif_re in _CLASSES:
        if motif_re.search(texte):
            return classe, cote
    if refus_passager(texte):
        return "refus_passager", "plateforme"
    return "inconnu", "inconnu"


def _deja_relance(episode: dict, job_id) -> bool:
    return any(r.get("action") == "relance" and r.get("job_id") == job_id
               for r in (episode.get("reparations") or []))


def _alerte_exploitation(message: str, *args) -> None:
    """Une panne de la plateforme que la boucle ne guérit pas : au journal ET au suivi
    d'erreurs (Sentry), le canal d'exploitation des autres défauts dits hors requête
    (`tenancy._refus`, `guide_store._alerte`, `email._relais_refuse`) — le journal seul
    n'a pas de destinataire. Une issue par agent : le texte le nomme. Fail-open sur le
    suivi seulement : l'escalade est déjà écrite sur l'épisode."""
    log.warning(message, *args)
    try:
        import sentry_sdk
        with sentry_sdk.new_scope() as scope:
            scope.set_tag("oto.medecin_agents", "escalade")
            sentry_sdk.capture_message(message % args if args else message,
                                       level="error")
    # noqa: SILENT — le suivi d'erreurs indisponible n'empêche pas l'escalade écrite
    except Exception:
        pass


def _refus_de_la_trace(episode_id: int, job_id) -> str:
    """Pourquoi `noter_reparation` a refusé, relu sur l'épisode tel que la base le tient
    APRÈS le refus (un autre processus a pu l'écrire entre-temps)."""
    from .db import episodes_agents
    ep = episodes_agents.lire(int(episode_id))
    if ep is None or ep["statut"] == "clos":
        return REFUS_CLOS
    if _deja_relance(ep, job_id):
        return REFUS_DEJA_RELANCE
    return REFUS_PLAFOND


def _famille_servie(panne: dict) -> bool:
    from . import db, runner_models
    famille = runner_models.famille(panne.get("model"))
    if not famille:
        return False
    return famille in (db.runner_arme(int(panne["org_id"])).get("families") or [])


def _instant(v) -> Optional[datetime]:
    """`finished_at` en datetime CONSCIENT. La boucle le lit en datetime
    (`pannes_en_cours`), mais `oto_admin_agent_repair op=rerun` le reçoit de
    `db.get_job`, dont les lignes rendent les horodatages en CHAÎNES naïves
    (`_str_dict_row`, heure UTC de la base) : `maintenant - "2026-11-09 08:51:38"`
    levait `TypeError`, servi en « Internal server error » à chaque relance manuelle.
    Illisible → None ; `relancer` le refuse alors (âge inconnu ≠ âge permis)."""
    if v is None or isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def relancer(panne: dict, episode: dict, *, acteur: str = ACTEUR,
             maintenant: Optional[datetime] = None,
             delai_s: Optional[int] = None) -> Optional[str]:
    """Relance le dernier travail mort de l'agent, sous toutes les gardes. Rend None si
    la relance est partie, sinon la raison du refus — dite, jamais devinée par
    l'appelant. Partagé par la boucle et par l'agent médecin."""
    from . import db
    from .db import episodes_agents

    maintenant = maintenant or datetime.now(timezone.utc)
    job_id = panne.get("job_id")
    if job_id is None:
        return "aucun travail mort à relancer"
    if _deja_relance(episode, job_id):
        return REFUS_DEJA_RELANCE
    brut = panne.get("dernier")
    fini = _instant(brut)
    if brut is not None and fini is None:
        return "horodatage du travail mort illisible : âge inconnu, pas de relance"
    if fini is not None and maintenant - fini > AGE_MAX:
        return "travail mort trop ancien : son occurrence est passée"
    file = db.file_du_declencheur(int(panne["trigger_id"]), int(panne["org_id"]))
    if file["pending"] or file["held"] or file["claimed"]:
        return "une occurrence de l'agent attend déjà dans la file, ou tourne"
    # La trace d'abord, sous garde EN BASE (plafond, et une relance par `job_id`) : deux
    # processus sur le même épisode, un seul relance.
    if episodes_agents.noter_reparation(
            int(episode["id"]), action="relance", acteur=acteur, job_id=job_id,
            detail=f"relance du travail {job_id} ({episode.get('classe')})",
            maximum=RELANCES_MAX) is None:
        return _refus_de_la_trace(int(episode["id"]), job_id)
    db.enqueue_job(int(panne["org_id"]), panne.get("kind") or "start",
                   payload=dict(panne.get("payload") or {}), sub=panne.get("sub"),
                   delai_s=delai_s or None)
    return None


def _a_relancer(episode: dict) -> bool:
    """La panne est de celles que la boucle relance (refus passager, famille non servie
    — celle-ci seulement quand un worker la sert, ce que `_soigner` vérifie)."""
    return episode["cote"] != "proprietaire" and episode["classe"] in (
        "refus_passager", "famille_non_servie")


def _soigner(panne: dict, episode: dict, maintenant: datetime, *,
             delai_s: Optional[int] = None, en_incident: bool = False) -> Optional[str]:
    """Le geste automatique pour cette panne, ou None. Rend l'action faite. En incident,
    l'escalade au plafond s'écrit sur l'épisode sans alerte par agent : l'incident est
    dit une fois (`_incidents`)."""
    from .db import episodes_agents

    classe, statut = episode["classe"], episode["statut"]
    if episode["cote"] == "proprietaire":
        if statut != "escalade":
            episodes_agents.poser(int(episode["id"]), statut="escalade")
        return None
    if classe == "refus_passager" or (classe == "famille_non_servie"
                                       and _famille_servie(panne)):
        refus = relancer(panne, episode, maintenant=maintenant, delai_s=delai_s)
        if refus is None:
            return "relance"
        if refus == REFUS_PLAFOND and statut != "escalade":
            episodes_agents.poser(int(episode["id"]), statut="escalade",
                                  diagnostic=f"{RELANCES_MAX} relances sans guérison")
            if en_incident:
                return None
            _alerte_exploitation(
                "médecin des agents : agent %s (org %s) toujours en panne après %d "
                "relances — %s, escaladé (épisode %s)", panne["trigger_id"],
                panne["org_id"], RELANCES_MAX, classe, episode["id"])
    return None


def _ombre_du_catalogue(vues: list[tuple[str, str, str, str]]) -> dict:
    """Ce que le catalogue aurait décidé pour les pannes de ce passage — sans rien
    décider. `vues` = `(cle, empreinte, classe, cote)` de chaque panne rangée par la
    regex. Fail-open : une erreur du catalogue ne coûte que ce bilan, et se dit."""
    from .db import causes_agents
    bilan = {"catalogue_reconnues": 0, "catalogue_neuves": 0,
             "catalogue_desaccords": 0, "catalogue_sans_empreinte": 0}
    avec = [v for v in vues if v[0]]
    bilan["catalogue_sans_empreinte"] = len(vues) - len(avec)
    try:
        causes = causes_agents.relever(avec)
    except Exception as e:  # noqa: BLE001 — l'ombre ne fait jamais échouer le passage
        log.warning("médecin des agents : catalogue des causes illisible (%s)", e)
        bilan["catalogue_erreur"] = 1
        return bilan
    bilan["catalogue_neuves"] = sum(1 for c in causes.values() if c.get("neuve"))
    for cle_, _empreinte, classe, cote in avec:
        cause = causes.get(cle_) or {}
        if cause.get("statut") != "approuvee":
            continue
        bilan["catalogue_reconnues"] += 1
        # Un désaccord, c'est la regex qui A un avis et en donne un autre : une cause
        # approuvée là où la regex dit `inconnu` est ce que le catalogue AJOUTE.
        if classe != "inconnu" and (cause.get("classe"), cause.get("cote")) != (classe, cote):
            bilan["catalogue_desaccords"] += 1
            if (cle_, classe) not in _DESACCORDS_DITS:
                _DESACCORDS_DITS.add((cle_, classe))
                log.info("médecin des agents : désaccord du catalogue — cause %s approuvée "
                         "%s/%s, la regex dit %s/%s", cle_, cause.get("classe"),
                         cause.get("cote"), classe, cote)
    return bilan


def _incidents(plateforme: list[tuple[str, int]], *, orgs_en_panne: int) -> set[str]:
    """Les classes en incident à ce passage : `plateforme` = `(classe, org)` de chaque
    panne du côté de la plateforme, `orgs_en_panne` = les orgs qui ont au moins une panne
    à ce passage. Un incident neuf se dit une fois chez nous ; un incident qui n'est plus
    vu se dit fini au journal."""
    orgs: dict[str, set[int]] = defaultdict(set)
    for classe, org in plateforme:
        orgs[classe].add(org)
    seuil = max(INCIDENT_ORGS, INCIDENT_PART * max(orgs_en_panne, 1))
    actuels = {c for c, o in orgs.items() if len(o) >= seuil}
    for classe in actuels - set(_INCIDENTS):
        agents = sum(1 for c, _ in plateforme if c == classe)
        _alerte_exploitation(
            "médecin des agents : INCIDENT — %s touche %d orgs (%d agents) ; relances "
            "bornées à %d sondes par passage", classe, len(orgs[classe]), agents,
            INCIDENT_SONDES)
    for classe in set(_INCIDENTS) - actuels:
        log.info("médecin des agents : incident %s terminé", classe)
        _INCIDENTS.pop(classe, None)
    for classe in actuels:
        _INCIDENTS[classe] = len(orgs[classe])
    return actuels


def _relacher(candidats: list[tuple[dict, dict]], incidents: set[str],
              maintenant: datetime) -> tuple[int, int]:
    """Relance les candidats de ce passage sous le budget : à tour de rôle par org (la
    plus anciennement en panne d'abord), `RELANCES_PAR_PASSAGE` au plus, `INCIDENT_SONDES`
    par classe en incident, chacun avec sa gigue. Rend `(relances, différées)`. Une
    relance qui ne part pas (garde, famille pas encore servie) ne coûte rien."""
    par_org: dict[int, deque] = defaultdict(deque)
    for panne, ep in sorted(candidats, key=lambda c: str(c[1].get("ouvert_at") or "")):
        par_org[int(panne["org_id"])].append((panne, ep))
    budget, sondes = RELANCES_PAR_PASSAGE, {c: INCIDENT_SONDES for c in incidents}
    relances, differees = 0, 0
    tour = deque(par_org)
    while tour:
        org = tour.popleft()
        panne, ep = par_org[org].popleft()
        if par_org[org]:
            tour.append(org)
        classe = ep["classe"]
        if budget <= 0 or (classe in sondes and sondes[classe] <= 0):
            differees += 1
            continue
        try:
            fait = _soigner(panne, ep, maintenant, en_incident=classe in incidents,
                            delai_s=random.randint(0, GIGUE_MAX_S))
        except Exception as e:  # noqa: BLE001 — une relance qui casse ne bloque pas les autres
            log.warning("médecin des agents : relance de l'agent %s en échec (%s)",
                        panne.get("trigger_id"), e)
            continue
        if fait == "relance":
            relances += 1
            budget -= 1
            if classe in sondes:
                sondes[classe] -= 1
    return relances, differees


def balayer(maintenant: Optional[datetime] = None) -> dict:
    """Un passage : clôt les guéris, relève et range les pannes, soigne ce qui se soigne.
    Synchrone (la boucle le pousse hors de l'event loop). Une panne qui casse ne
    bloque pas les suivantes."""
    from . import causes_pannes
    from .alerte_agents_bloques import org_a_nous
    from .db import episodes_agents

    maintenant = maintenant or datetime.now(timezone.utc)
    vues: list[tuple] = []
    gueris = episodes_agents.clore_les_gueris()
    pannes, erreurs = 0, 0
    candidats: list[tuple[dict, dict]] = []
    plateforme: list[tuple[str, int]] = []
    a_nous: dict[int, bool] = {}
    ecartees: set[int] = set()
    en_panne: set[int] = set()
    for panne in episodes_agents.pannes_en_cours():
        org_id = int(panne["org_id"])
        if org_id not in a_nous:
            a_nous[org_id] = org_a_nous(org_id, qui=QUI)
        if not a_nous[org_id]:
            # Ni épisode ni relance : la panne n'est pas relevée du tout.
            ecartees.add(org_id)
            if org_id not in _ECARTEES_DITES:
                _ECARTEES_DITES.add(org_id)
                log.info("médecin des agents : org %s d'un tenant tiers écartée (aucun "
                         "épisode, aucune relance)", org_id)
            continue
        pannes += 1
        en_panne.add(org_id)
        try:
            classe, cote = classer(panne.get("motif"))
            # La note libre d'un agent qui s'est dit bloqué ne s'empreinte pas : chaque
            # note ferait sa cause, et un texte de l'agent n'entre pas au catalogue.
            empreinte = (None if panne.get("motif_de_l_agent")
                         else causes_pannes.de(panne.get("motif")))
            cle_ = causes_pannes.cle(empreinte) if empreinte else None
            vues.append((cle_, empreinte, classe, cote))
            episode = episodes_agents.ouvrir_ou_rafraichir(
                org_id=panne["org_id"], trigger_id=panne["trigger_id"], classe=classe,
                cote=cote, motif=panne.get("motif"), job_id=panne.get("job_id"),
                travaux=panne.get("travaux") or 0, empreinte=cle_)
            if episode["cote"] == "plateforme":
                plateforme.append((episode["classe"], org_id))
            if _a_relancer(episode):
                candidats.append((panne, episode))   # relâchés après le relevé
            else:
                _soigner(panne, episode, maintenant)
        except Exception as e:  # noqa: BLE001 — une panne illisible ne bloque pas les autres
            erreurs += 1
            log.warning("médecin des agents : agent %s illisible (%s)",
                        panne.get("trigger_id"), e)
    incidents = _incidents(plateforme, orgs_en_panne=len(en_panne))
    relances, differees = _relacher(candidats, incidents, maintenant)
    return {"pannes": pannes, "relances": relances, "relances_differees": differees,
            "incidents": sorted(incidents), "gueris": len(gueris),
            "gueris_apres_soin": sum(1 for g in gueris if g.get("reparations")),
            "erreurs": erreurs, "tenant_tiers_ecartes": len(ecartees),
            **_ombre_du_catalogue(vues)}


async def run_medecin_agents_loop(interval: int = INTERVALLE_S) -> None:
    """La boucle de fond (`boucles_de_fond.py`, `tiers=True` : une relance refait agir un
    agent chez des tiers). Un passage raté ne tue pas la boucle — il le dit."""
    log.info("médecin des agents : démarré (passage toutes les %ss, %d relances max).",
             interval, RELANCES_MAX)
    while True:
        try:
            bilan = await asyncio.to_thread(balayer)
            if (bilan.get("relances") or bilan.get("gueris") or bilan.get("erreurs")
                    or bilan.get("catalogue_neuves") or bilan.get("catalogue_desaccords")):
                log.info("médecin des agents : %s", bilan)
        except Exception as e:  # noqa: BLE001 — un passage raté ne tue pas la boucle
            log.warning("médecin des agents : passage en échec : %s", e)
        await asyncio.sleep(interval)
