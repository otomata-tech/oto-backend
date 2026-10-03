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
from zoneinfo import ZoneInfo

from croniter import croniter

from . import db, org_suspension, runner_models
from .capabilities import _abonnement, _instruction, _limites_du_run

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
        # « aucun agent ne dessert cette organisation » — faux.
        try:
            if org_suspension.etat(t["org_id"]):
                continue
        except Exception as e:  # noqa: BLE001 — la réservation garde de toute façon
            log.warning("déclencheur %s : état de suspension illisible (%s)", t["id"], e)
        payload = {
            "procedure": t["procedure"],
            "project_id": t["project_id"],
            "tools": t.get("tools") or [],
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
            "input": t.get("input") or _instruction.derivee(t["procedure"]),
            "label": t.get("label") or f"planifié — {t['procedure']}",
            "max_steps": t.get("max_steps"),
            # Les limites du run déclarées sur l'agent — seulement si déclarées.
            **_limites_du_run.charge(t.get("max_tokens"), t.get("max_run_seconds")),
            "trigger_id": t["id"],
            # Le modèle DÉCLARÉ et sa famille — la famille route le travail vers un
            # worker qui la sert (`claim_next_job`). Sans modèle : rien ne part, et
            # n'importe quel worker le prend sur le sien, comme avant.
            **runner_models.charge(t.get("model")),
        }
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
                db.runner_arme(t["org_id"]))
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
