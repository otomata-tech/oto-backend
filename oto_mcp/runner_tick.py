"""Le tick des déclencheurs — une horloge qui ENFILE, jamais qui exécute (R3).

Le patron exact du scheduler d'emails : une boucle de fond au lifespan, le
travail par tranche en `asyncio.to_thread` (le serveur est mono-loop), un tick
raté ne tue jamais la boucle. Ce que le tick fait d'une échéance : gagner le
compare-and-swap (prod et preprod partagent la base — deux ticks, UN gagnant
par échéance), enfiler un job `start`, recalculer la prochaine échéance. C'est
tout. L'exécution appartient au worker (`oto-runner`), qui claime quand il veut.

Le calcul d'échéance vit ICI, à un seul endroit : `next_due(cron, tz)` — croniter
évalue DANS le fuseau du déclencheur (l'heure d'été ne décale pas une veille en
silence), et rend un instant UTC-aware que PG stocke tel quel.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
import os
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter

from . import db, org_suspension, procedure_retiree, runner_models
from .capabilities import _abonnement, _instruction, _limites_du_run, _projet_de_l_agent

log = logging.getLogger(__name__)

_POLL_INTERVAL_S = 60

# Deux échéances consécutives plus proches que ça = un cron d'arrosage, pas un
# déclencheur de runs (chaque run coûte des tours de modèle). Refusé à la pose.
MIN_SPACING_S = 300


def validate_cron(expr: str, tz: str) -> None:
    """Lève ValueError si l'expression ou le fuseau ne tiennent pas la route —
    le message nomme le fautif (le refus muet est interdit de séjour)."""
    try:
        zone = ZoneInfo(tz)
    except Exception:
        raise ValueError(f"fuseau inconnu : `{tz}` (forme attendue : Europe/Paris)")
    if not croniter.is_valid(expr):
        raise ValueError(f"expression cron invalide : `{expr}` (5 champs, "
                         "ex. `5 6 * * *` = tous les jours à 6h05)")
    base = datetime.datetime.now(zone)
    it = croniter(expr, base)
    d1 = it.get_next(datetime.datetime)
    d2 = it.get_next(datetime.datetime)
    if (d2 - d1).total_seconds() < MIN_SPACING_S:
        raise ValueError(
            f"cadence trop serrée : deux échéances à {(d2 - d1).total_seconds():.0f}s "
            f"d'écart pour un plancher de {MIN_SPACING_S}s — un run n'est pas un ping")


def next_due(expr: str, tz: str,
             apres: datetime.datetime | None = None) -> datetime.datetime:
    """La prochaine échéance APRÈS `apres` (défaut : maintenant), évaluée dans le
    fuseau du déclencheur, rendue tz-aware."""
    zone = ZoneInfo(tz)
    base = (apres or datetime.datetime.now(datetime.timezone.utc)).astimezone(zone)
    return croniter(expr, base).get_next(datetime.datetime)


def _occurrence_sautee(t: dict, issue: str = db.REFUSE_SUSPENDED,
                       exige: Optional[int] = None) -> None:
    """Trace d'une échéance consommée sans rien enfiler — org suspendue, procédure
    archivée, agent sans modèle, ou agent exigé sans succès du jour : un journal, et une
    livraison `refused_*` (sans créneau) visible sur la page de l'agent, comme pour un
    webhook refusé."""
    if issue == db.REFUSE_PROCEDURE_ARCHIVED:
        cause = "procédure archivée"
    elif issue == db.REFUSE_REQUIRES:
        cause = f"l'agent {exige} qu'il exige n'a pas réussi aujourd'hui"
    elif issue == db.REFUSE_NO_MODEL:
        cause = runner_models.motif_sans_famille(t.get("model"))
    else:
        cause = f"org {t['org_id']} suspendue"
    log.warning("déclencheur %s : %s, échéance %s sautée",
                t["id"], cause, t.get("next_due"))
    with db._connect() as conn:
        db.enregistrer(conn, t["id"], t["org_id"], issue, source="schedule")


def exigence_manquee(t: dict, maintenant: Optional[datetime.datetime] = None) -> Optional[int]:
    """L'agent que `t` EXIGE (lien `requires`) et dont le dernier run du jour n'a pas
    réussi — None quand toutes ses exigences sont tenues, ou qu'il n'en a aucune.

    « Du jour » = depuis minuit dans le fuseau de `t` : c'est sa journée à lui qu'on
    garde (un 09:00 Paris exige le 07:30 Paris de la même matinée). Un run de l'autre
    qui TOURNE encore ne compte pas comme une réussite : on saute plutôt que de partir
    sur une capacité qui n'est pas encore écrite. Un agent exigé en PAUSE est ignoré."""
    liens = db.runner_links.vers_l_agent(t["org_id"], t["id"], db.runner_links.EXIGE)
    if not liens:
        return None
    zone = ZoneInfo(t.get("tz") or "UTC")
    minuit = (maintenant or datetime.datetime.now(datetime.timezone.utc)).astimezone(zone) \
        .replace(hour=0, minute=0, second=0, microsecond=0)
    for lien in liens:
        # Un agent exigé EN PAUSE n'exige plus rien : la pause est un choix, le garde
        # existe pour les échecs. Sinon l'agent qui l'exige serait sauté chaque jour,
        # sans fin, sur la foi d'un agent que quelqu'un a éteint exprès.
        if lien.get("from_enabled") is False:
            continue
        dernier = db.runner_links.dernier_run_depuis(t["org_id"], lien["from_trigger_id"], minuit)
        if not dernier or dernier["status"] != "done":
            return int(lien["from_trigger_id"])
    return None


def cibles_du_relais(t: dict) -> list[dict]:
    """Les agents que `t` peut lancer (liens `starts` qui partent de lui), pour son
    instruction (`_instruction.relais`). Une lecture qui échoue rend [] : l'agent part
    alors sans la consigne de relais, ce qui ne lance rien — jamais l'inverse."""
    try:
        return db.runner_links.depuis_l_agent(t["org_id"], t["id"], db.runner_links.DEMARRE)
    except Exception as e:  # noqa: BLE001 — une consigne manquée ne lance rien
        log.warning("déclencheur %s : liens de relais illisibles (%s) — part sans eux",
                    t.get("id"), e)
        return []


def charge_du_declencheur(t: dict, echeance, origine: str = "planifié",
                          relais: Optional[list[dict]] = None) -> dict:
    """La charge du travail `start` d'une occurrence de `t` — UNE définition pour
    l'horloge (`_tick`) et le lancement à la demande (`runner.triggers op=run_now`).
    Un run lancé à la main et un run lancé par l'horloge doivent être le MÊME run :
    deux copies de cette charge finiraient par tester deux choses différentes.

    `echeance` = le moment que l'occurrence représente (l'échéance consommée pour
    l'horloge, maintenant pour un lancement à la main). Les champs None restent :
    l'appelant les retire à l'enfilage.

    `relais` = les agents que `t` peut lancer (`cibles_du_relais`) : la consigne s'ajoute
    à l'instruction QUAND l'agent porte `oto_trigger` dans ses outils — un lien borne ce
    qu'il peut lancer (`chain_not_linked`), il ne lui donne pas l'outil.
    ⚠️ Le lien n'ajoute JAMAIS `oto_trigger` : l'outil ne sert pas que `run_now` (il
    modifie, partage, supprime des agents), et un agent qui lit des données tierces
    l'aurait reçu sans que personne l'ait mis dans sa liste. Sans l'outil, aucune
    consigne : elle lui dirait d'appeler ce qu'il n'a pas."""
    outils = t.get("tools") or []
    if "oto_trigger" not in outils:
        relais = None
    return {
        "procedure": t["procedure"],
        # Le sien, sinon le premier de sa procédure (24/10/2026, `_projet_de_l_agent`) :
        # un agent travaille dans un des projets où sa procédure est rangée.
        "project_id": _projet_de_l_agent.de_l_agent(t),
        "tools": outils,
        # ⚠️ DÉRIVÉE quand le déclencheur n'en porte pas. Sans cette ligne
        # le travail part sans instruction de départ, et le worker le refuse
        # — il exécute une instruction, il n'en compose pas (_instruction.py,
        # tranché le 02/09). Constaté en production le 14/09 : les six
        # déclencheurs de l'org 196 portent `input = NULL`, donc CHACUNE de
        # leurs occurrences échouait, silencieusement, depuis qu'un défaut
        # vivait dans le runner et en a été retiré.
        #
        # `create` compose bien la sienne (`inp.input or derivee(...)`) —
        # mais c'est le SEUL site qui le fait : tout déclencheur né avant ce
        # jour-là n'en a jamais reçu, et aucune mise à jour ne lui en donne.
        # Réparer ici plutôt que par une migration couvre d'un coup les rangs
        # anciens ET le cas où quelqu'un vide l'invite depuis le produit.
        #
        # ⚠️ Toujours par `_instruction`, jamais rédigée sur place : une
        # variante écrite ici rouvrirait le « second domicile du métier » que
        # ce module existe pour fermer.
        #
        # Et l'occurrence se DATE (`horodatee`) : un agent hébergé n'a pas
        # d'horloge, il apprend ici quel jour et quelle heure il représente.
        "input": _instruction.relais(_instruction.horodatee(
            t.get("input") or _instruction.derivee(t["procedure"]),
            echeance, t["tz"]), relais or []),
        "label": t.get("label") or f"{origine} — {t['procedure']}",
        "max_steps": t.get("max_steps"),
        # Les limites du run déclarées sur l'agent — seulement si déclarées.
        **_limites_du_run.charge(t.get("max_tokens"), t.get("max_run_seconds")),
        "trigger_id": t["id"],
        # Le modèle DÉCLARÉ et sa famille — la famille route le travail vers un
        # worker qui la sert (`claim_next_job`). Sans modèle, on n'arrive pas ici.
        **runner_models.charge(t.get("model")),
    }


def _tick() -> int:
    """UN tour d'horloge. Rend le nombre de jobs enfilés (télémétrie du log)."""
    enfiles = 0
    for t in db.due_triggers():
        try:
            prochaine = next_due(t["cron"], t["tz"])
        except Exception as e:  # noqa: BLE001 — un cron devenu invalide (édité à la
            # main ?) ne doit pas bloquer les AUTRES déclencheurs du tour
            log.warning("déclencheur %s : cron inévaluable (%s)", t["id"], e)
            continue
        # Le CAS d'abord : si un tick concurrent (l'autre environnement, même base)
        # a déjà consommé cette échéance, on passe sans enfiler.
        if not db.consume_due(t["id"], t["next_due"], prochaine):
            continue
        # Org SUSPENDUE : l'échéance est consommée (elle ne s'accumule pas), rien
        # n'est enfilé. Un travail enfilé ne serait de toute façon jamais réservé
        # (`claim_next_job`), et sa péremption au tour suivant se raconterait
        # « aucun agent ne dessert cette organisation » — faux. L'occurrence sautée
        # laisse la MÊME trace qu'un webhook refusé (`refused_suspended`, sur la page
        # de l'agent) : sans elle, un agent planifié se tairait sans dire pourquoi.
        try:
            if org_suspension.etat(t["org_id"]):
                _occurrence_sautee(t)
                continue
        except Exception as e:  # noqa: BLE001 — la réservation garde de toute façon
            log.warning("déclencheur %s : état de suspension illisible (%s)", t["id"], e)
        # Procédure ARCHIVÉE : retirer une procédure arrête ce qui la déroule. Même
        # trace qu'une org suspendue, avec son motif. Une lecture qui échoue laisse
        # partir le travail (l'agent lit alors la procédure avec son `archived_at`).
        try:
            if procedure_retiree.archivee(t["org_id"], t.get("sub"), t.get("procedure")):
                _occurrence_sautee(t, db.REFUSE_PROCEDURE_ARCHIVED)
                continue
        except Exception as e:  # noqa: BLE001 — voir ci-dessus
            log.warning("déclencheur %s : état de la procédure illisible (%s)", t["id"], e)
        # Agent SANS MODÈLE (21/10/2026) : son travail n'aurait aucune famille, donc
        # aucun worker ne le servirait — la réservation l'arrêterait (`_SANS_MODELE`).
        # On n'enfile pas : l'échéance est consommée, la livraison refusée et motivée
        # (`refused_no_model`). Le déclencheur n'est PAS éteint : déclarer un modèle
        # suffit à ce que l'échéance suivante parte.
        if runner_models.motif_sans_famille(t.get("model")):
            _occurrence_sautee(t, db.REFUSE_NO_MODEL)
            continue
        # EXIGE un autre agent (lien `requires`) dont le dernier run du jour n'a pas
        # réussi : même trace qu'une procédure archivée, avec son motif. Une lecture
        # qui échoue laisse partir le travail, comme les deux gardes ci-dessus.
        try:
            exige = exigence_manquee(t)
        except Exception as e:  # noqa: BLE001 — voir ci-dessus
            log.warning("déclencheur %s : exigences illisibles (%s)", t["id"], e)
            exige = None
        if exige is not None:
            _occurrence_sautee(t, db.REFUSE_REQUIRES, exige=exige)
            continue
        payload = charge_du_declencheur(t, t["next_due"], relais=cibles_du_relais(t))
        # ⚠️ PÉRIMER AVANT D'ENFILER. Ce qui restait en attente pour ce
        # déclencheur n'a pas été pris dans son cycle : une veille quotidienne
        # exécutée treize jours plus tard ne rend pas un résultat en retard, elle
        # rend un résultat FAUX. Et si on ne périmait pas, le jour où des agents
        # arrivent enfin sur cette org, treize jours d'occurrences partiraient
        # d'un coup — avec la procédure et le contexte de leur époque.
        #
        # Constaté le 02/09 : 41 travaux jamais pris depuis treize jours, sur
        # quatre organisations, `attempts = 0`. Chaque pièce faisait exactement
        # son travail ; c'est leur COMPOSITION qui fabriquait le trou, et rien ne
        # le disait — un travail « en attente » ressemble à un travail qui va
        # partir.
        # ⚠️ La raison nomme la VRAIE cause (27/09/2026) : le texte
        # générique d'avant disait « aucun agent ne dessert cette organisation »
        # même quand un worker de la famille existait et que seule la connexion
        # PERSONNELLE du propriétaire (ou un pool en pause) manquait — deux
        # diagnostics qui n'envoient pas au même geste
        # (`_abonnement.raison_de_peremption`). La RÈGLE d'expiration ne bouge
        # pas d'un mot : seul le texte s'affine.
        #
        # ⚠️ Calculée à PART de l'appel qui périme, et jamais laissée l'empêcher :
        # un diagnostic qui casse (une lecture `runner_arme` en délicatesse) ne
        # doit pas faire manquer la péremption elle-même — ce serait le défaut
        # d'hygiène ci-dessous, pour un simple texte. `raison_kw` vide fait
        # retomber sur le texte générique de `perimer_travaux_du_declencheur`.
        raison_kw: dict = {}
        try:
            raison_kw["raison"] = _abonnement.raison_de_peremption(
                t.get("sub"), t["org_id"], runner_models.famille(t.get("model")),
                db.runner_arme(t["org_id"]), modele=t.get("model"))
        except Exception as e:  # noqa: BLE001 — un diagnostic manqué garde le texte générique
            log.warning("déclencheur %s : diagnostic de péremption illisible (%s) — "
                        "texte générique conservé", t["id"], e)
        try:
            perimes = db.perimer_travaux_du_declencheur(t["id"], t["org_id"], **raison_kw)
        except Exception as e:  # noqa: BLE001 — voir ci-dessous
            # ⚠️ La péremption est un geste d'HYGIÈNE, l'enfilage est le SERVICE.
            # Si elle casse, le service continue : l'inverse ferait qu'un défaut
            # d'entretien arrête les automatisations de tout le monde. Le pire
            # qu'on risque en la ratant est ce qu'on avait déjà — un travail de
            # trop en attente.
            log.warning("déclencheur %s : péremption impossible (%s) — l'enfilage "
                        "continue", t["id"], e)
            perimes = 0
        if perimes:
            # Le journal dit la MÊME cause que la raison écrite sur le travail : un
            # « aucun agent » fixe renverrait l'exploitant chercher un worker quand
            # il fallait une reconnexion.
            log.warning("déclencheur %s (org %s) : %d occurrence(s) périmée(s) — %s",
                        t["id"], t["org_id"], perimes,
                        raison_kw.get("raison",
                                      "aucun agent ne dessert cette organisation"))
        # ⚠️ L'identité du travail est celle du CRÉATEUR du déclencheur, pas
        # celle d'un compte de service : c'est en son nom que l'agent agira, et
        # c'est ce qui rend le worker mutualisable. Le tick n'a pas d'identité
        # propre — il est une horloge, pas un acteur.
        db.enqueue_job(t["org_id"], "start", sub=t.get("sub"),
                       payload={k: v for k, v in payload.items() if v is not None})
        enfiles += 1
    return enfiles


async def run_runner_tick_loop(interval: int = _POLL_INTERVAL_S) -> None:
    """Boucle de fond : enfile les déclencheurs dus. Ne meurt jamais sur un tick."""
    log.info("tick du runner démarré (intervalle %ss)", interval)
    while True:
        try:
            n = await asyncio.to_thread(_tick)
            if n:
                log.info("tick runner : %d job(s) enfilé(s)", n)
        except asyncio.CancelledError:
            log.info("tick runner arrêté")
            raise
        except Exception as e:  # noqa: BLE001 — un tick raté ne tue pas la boucle
            log.warning("tick runner échoué : %s", e)
        await asyncio.sleep(interval)


def enabled() -> bool:
    return os.environ.get("OTO_RUNNER_TICK_ENABLED", "1") != "0"
