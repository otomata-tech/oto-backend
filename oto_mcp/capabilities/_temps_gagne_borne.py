"""Le temps gagné de la page Agents, calculé HORS de la requête : borné, partagé, mis en cache.

`db.formes_des_runs_conclus` relit, pour chaque run conclu de la fenêtre, ses appels du
journal (`tool_calls`). Sur une org chargée, ce sont des centaines de milliers de lignes
éparpillées dans une table de plusieurs gigaoctets : la lecture va au disque et dure des
minutes. Calculée DANS la requête de la page, sans borne, à chaque ouverture et pour
chaque personne, elle tenait une connexion du pool par ouverture — les ouvertures
s'empilaient plus vite qu'elles ne finissaient, et le pool entier y passait : workers,
MCP, facturation tombaient en `PoolTimeout`.

Ce module change QUI attend :

- **Hors de la requête.** Le calcul tourne dans un fil de fond. La page rend tout de
  suite le dernier résultat connu, même périmé ; sans lui, elle attend le calcul au plus
  `ATTENTE_S` (assez pour une org légère, servie comme avant), puis rend ce qu'elle a, ou
  rien — la page s'affiche alors sans le temps gagné
  (`time_saved_status: "pending"`), jamais l'inverse.
- **Borné.** Chaque requête du calcul porte `statement_timeout = DUREE_MAX_MS`
  (`lecture_bornee.lectures_bornees`). Au-delà, PostgreSQL l'annule.
- **Partagé.** Un seul calcul en vol par clé (org, fenêtre, agents vus) : dix ouvertures
  simultanées attendent le même. Et au plus `CALCULS_SIMULTANES` calculs pour tout le
  processus, les autres font la queue : le pool ne peut plus leur être livré entier.
- **En cache.** Un résultat sert `TTL_S` ; un échec (borne atteinte) n'est retenté
  qu'après `REESSAI_APRES_ECHEC_S`, pour ne pas relancer une lecture qu'on sait trop
  lourde à chaque ouverture.

Le cache est celui du PROCESSUS : un redémarrage le vide, et le premier affichage
recalcule. Deux processus (les deux couleurs d'une bascule) calculent chacun de leur
côté : au plus `CALCULS_SIMULTANES` par processus.

⚠️ **Des fils DÉMONS, une file bornée.** Un `ThreadPoolExecutor` rejoint ses fils à la
sortie de l'interpréteur ET vide sa file d'abord : l'arrêt d'une couleur aurait attendu
chaque calcul en queue, jusqu'à une minute par requête. Un fil démon meurt avec le
processus (sa connexion fermée, PostgreSQL annule la requête). Au-delà de
`EN_VOL_MAX` clés en vol ou en queue, aucune n'est ajoutée : la page sort sans le temps
gagné, l'ouverture suivante retente.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from ..db import lecture_bornee

logger = logging.getLogger(__name__)


#: Combien de temps un résultat sert avant d'être recalculé.
TTL_S = 600.0
#: Combien de temps la requête de la page attend un calcul en vol.
ATTENTE_S = 2.0
#: La borne de CHAQUE requête du calcul.
DUREE_MAX_MS = 60_000
#: Les calculs en vol pour tout le processus, toutes orgs confondues.
CALCULS_SIMULTANES = 2
#: Après un échec, le délai avant de retenter la même clé.
REESSAI_APRES_ECHEC_S = 1800.0
#: Au-delà de ce nombre d'entrées (résultats, échecs), les plus anciennes partent.
_TAILLE_MAX = 2000
#: Les clés en vol ou en queue, au plus : la file ne grossit pas sans fin.
EN_VOL_MAX = 32

Cle = tuple[int, int, tuple[int, ...]]

_verrou = threading.Lock()
_cache: dict[Cle, tuple[float, object]] = {}
_echecs: dict[Cle, float] = {}
_en_vol: dict[Cle, threading.Event] = {}
#: Les calculs FONT LA QUEUE derrière `CALCULS_SIMULTANES` places : une org légère attend
#: qu'une place se libère, au lieu d'être servie sans résultat parce que deux orgs
#: lourdes les tiennent.
_places = threading.BoundedSemaphore(CALCULS_SIMULTANES)
#: Monte à chaque `vider()` : un calcul lancé avant ne range pas son résultat après.
_generation = 0


def formes(org_id: int, jours: int, agents: list[int],
           calculer: Callable[[], object]) -> Optional[object]:
    """Le résultat de `calculer()` pour (org, fenêtre, agents), ou None s'il n'est pas
    (encore) connu. Ne tient jamais la requête plus de `ATTENTE_S`.

    ⚠️ Appelée depuis un handler SYNCHRONE (fil du pool de requêtes, REST comme MCP) :
    l'attente bornée ne touche jamais la boucle d'événements."""
    cle: Cle = (int(org_id), int(jours), tuple(int(a) for a in agents))
    maintenant = time.monotonic()
    with _verrou:
        connu = _cache.get(cle)
        if connu is not None and maintenant - connu[0] < TTL_S:
            return connu[1]
        evt = _en_vol.get(cle)
        if evt is None and _peut_relancer(cle, maintenant) and len(_en_vol) < EN_VOL_MAX:
            evt = threading.Event()
            _en_vol[cle] = evt
            threading.Thread(target=_calculer, args=(cle, calculer, evt, _generation),
                             name=f"temps-gagne-{cle[0]}", daemon=True).start()
    if connu is not None:
        # Périmé plutôt que rien, et tout de suite : un résultat d'il y a vingt minutes
        # vaut mieux qu'une attente, et le calcul frais est en route.
        return connu[1]
    if evt is not None and ATTENTE_S > 0:
        evt.wait(ATTENTE_S)
    with _verrou:
        connu = _cache.get(cle)
    return connu[1] if connu is not None else None


def _peut_relancer(cle: Cle, maintenant: float) -> bool:
    echec = _echecs.get(cle)
    return echec is None or maintenant - echec >= REESSAI_APRES_ECHEC_S


def _calculer(cle: Cle, calculer: Callable[[], object], evt: threading.Event,
              generation: int) -> None:
    _places.acquire()
    debut = time.monotonic()
    try:
        with lecture_bornee.lectures_bornees("temps gagné des agents", DUREE_MAX_MS):
            valeur = calculer()
        with _verrou:
            if generation == _generation:
                _cache[cle] = (time.monotonic(), valeur)
                _echecs.pop(cle, None)
                _elaguer()
    except Exception:
        logger.warning("temps gagné de l'org %s (%s j, %s agents) non calculé après %.1f s "
                       "— page servie sans, nouvel essai dans %.0f s", cle[0], cle[1],
                       len(cle[2]), time.monotonic() - debut, REESSAI_APRES_ECHEC_S,
                       exc_info=True)
        with _verrou:
            if generation == _generation:
                _echecs[cle] = time.monotonic()
                _elaguer()
    finally:
        _places.release()
        with _verrou:
            if _en_vol.get(cle) is evt:
                _en_vol.pop(cle, None)
        evt.set()


def _elaguer() -> None:
    """Sous `_verrou`. Garde le cache et la mémoire des échecs bornés : les entrées les
    plus anciennes partent."""
    if len(_cache) > _TAILLE_MAX:
        for cle, _v in sorted(_cache.items(),
                              key=lambda kv: kv[1][0])[: len(_cache) - _TAILLE_MAX]:
            _cache.pop(cle, None)
    if len(_echecs) > _TAILLE_MAX:
        for cle, _t in sorted(_echecs.items(),
                              key=lambda kv: kv[1])[: len(_echecs) - _TAILLE_MAX]:
            _echecs.pop(cle, None)


def vider() -> None:
    """Oublie tout (bancs d'essai). Un calcul encore en vol finit, mais ne range rien."""
    global _generation
    with _verrou:
        _generation += 1
        _cache.clear()
        _echecs.clear()
        _en_vol.clear()
