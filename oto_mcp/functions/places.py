"""Les places du bac à sable : combien d'exécutions tournent EN MÊME TEMPS (03/11/2026).

Chaque exécution est bornée (`isolement` : 1 Go, un cœur, 64 tâches, sa durée) ; rien ne
bornait leur NOMBRE. Tout compte qui lit une fonction peut la lancer, tout membre peut
éprouver du code : N exécutions simultanées pesaient N × 1 Go sur la box. Ce module tient
le compte des exécutions en cours :

- au plus `OTO_FUNCTIONS_MAX_SIMULTANEES` (défaut `MAX_DEFAUT`) dans le processus ;
- au plus `par_compte()` par compte = le plafond MOINS une réserve
  (`OTO_FUNCTIONS_RESERVE`, défaut `RESERVE_DEFAUT` = 1, au moins une place par compte) :
  un compte prend les places LIBRES, mais en laisse toujours une à un autre.

**Un « compte », c'est une personne DANS une org : la clé `(sub, org_id)`** (décision de
Julien, 28/11/2026). Une personne membre de plusieurs orgs n'est pas bornée d'une org à
l'autre : ses exécutions dans l'org A ne prennent rien de sa part dans l'org B. `org_id`
None (contexte personnel, sans org) est une clé à part entière, `(sub, None)`.

**Places partagées, part équitable, attente bornée (28/11/2026).** Jusque-là : UNE place
par compte, et un refus IMMÉDIAT au-delà. Mesuré en prod du 27/11 13:41 au 28/11 06:25 UTC
sur `oto_recipe` : 187 refus sur 200 venaient de la limite PAR COMPTE, 12 seulement du
plafond du serveur — et 149 des 200 venaient d'agents hébergés, qui tournent sous
l'identité de leur propriétaire : une org dont une personne possède les agents n'avait
qu'une place, même box vide. Désormais :

- **un compte prend les places libres**, jusqu'à `par_compte()` ;
- **quand plusieurs attendent, la place qui se libère va au compte qui en tient le
  MOINS, puis au plus ancien arrivé** (la part équitable de la prise des jobs du runner,
  `claim_next_job`) — un compte qui empile ne passe pas devant celui qui attend sa
  première ;
- **une demande ATTEND une place**, au plus `OTO_FUNCTIONS_ATTENTE_MAX_S` (défaut
  `ATTENTE_MAX_DEFAUT` = 25 s, la file des places de la ferme Claude) ; au-delà, le refus
  d'avant, `BacASableOccupe` (429 `sandbox_busy` + `Retry-After`).

⚠️ L'ancienne version refusait d'attendre : « une requête qui attend une place tient sa
connexion, et la file deviendrait la nouvelle charge ». L'attente est donc BORNÉE (25 s :
la connexion tenue est courte, et c'est le temps que la ferme tient déjà en production
sans incident), réglable, et `0` rend exactement l'ancien comportement (refus immédiat).
Une demande arrivée APRÈS d'autres qui attendent prend son tour : elle n'entre jamais
directement devant elles.

**Deux sortes d'appelants, une seule file.** L'aller simple (`executor.executer`) joue dans
un FIL (`run_in_threadpool`) : il attend sur un `threading.Event` — `place()`. Le pont des
recettes-code (`pont.executer_avec_pont`) joue dans la BOUCLE : il attend sur un futur
asyncio réveillé par `call_soon_threadsafe` — `place_async()` ; la boucle n'est jamais
bloquée. Les deux files n'en font qu'une : l'attribution (`_attribuer`) voit tous les
demandeurs, sous le même verrou.

**Annulation.** Un demandeur qui renonce (délai, client parti, tâche annulée) sort de la
file sous le verrou ; si une place lui avait été attribuée entre-temps, il la REND (et
elle part au suivant) : ni place ni entrée de file ne fuit.

**Le `Retry-After` dit quand une place se libère au plus tard** (25/11/2026). Chaque
place connaît le début de son exécution et la borne au-delà de laquelle elle est rendue
quoi qu'il arrive (`duree_max_s` : le délai de l'exécution plus ses grâces et l'arrêt
du processus). Refus par compte : ce qui reste à la première exécution de ce compte à
devoir finir ; refus du plafond : ce qui reste à la PREMIÈRE des exécutions en cours à
devoir finir. Une place sans borne connue, ou qui a dépassé la sienne (journalisée : ce
serait une place qui ne se rend pas), retombe sur `ATTENTE_S`.

**Un `threading.Lock`, pas un `asyncio.Semaphore`** : un sémaphore asyncio n'est ni sûr
entre fils ni utilisable hors de sa boucle ; un verrou de fil l'est des deux côtés. Il
n'est jamais tenu au-delà d'un compte (prendre, rendre, attribuer, sans attente ni E/S) :
la boucle ne l'attend pas.

⚠️ **Le plafond est PAR PROCESSUS.** Pendant une bascule bleu/vert, deux couleurs
tournent : le plafond effectif de la box double le temps de la bascule. Un compteur
commun à la box est un autre chantier (le plan « recettes : file et capacité », phase 3).
"""
from __future__ import annotations

import asyncio
import contextlib
import functools
import itertools
import logging
import math
import os
import threading
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Iterator, Optional

logger = logging.getLogger(__name__)

ENV_MAX = "OTO_FUNCTIONS_MAX_SIMULTANEES"
ENV_RESERVE = "OTO_FUNCTIONS_RESERVE"
ENV_ATTENTE = "OTO_FUNCTIONS_ATTENTE_MAX_S"
#: 4 × `isolement.MEMOIRE_MO` (1 Go) = 4 Go au pire, sous les ~6 Go que la box laisse
#: libres au service : il reste de quoi servir le reste pendant que quatre exécutions
#: tiennent leur plafond mémoire.
MAX_DEFAUT = 4
#: Les places qu'un compte laisse toujours aux autres : il en tient au plus
#: `plafond() - RESERVE`, et au moins une.
RESERVE_DEFAUT = 1
#: Ce qu'une demande attend une place avant le refus — la file des places de la ferme.
ATTENTE_MAX_DEFAUT = 25.0
#: Ce que le refus conseille d'attendre quand aucune place tenue n'a de borne connue
#: (ou qu'elle l'a dépassée) : sinon, `Retry-After` dit quand une place se libère.
ATTENTE_S = 10
#: Ce qu'une exécution échue laisse, au-delà de son délai, pour tuer son processus et
#: fermer ses tubes avant de rendre sa place : la borne de `duree_max_s` l'inclut.
MARGE_ARRET_S = 5.0


class ReglageInvalide(RuntimeError):
    """Un réglage des places illisible : levé au démarrage."""


class BacASableOccupe(RuntimeError):
    """Pas de place dans le délai (le processus, ou ce compte) : rien n'est lancé."""

    def __init__(self, message: str, retry_after: int = ATTENTE_S):
        super().__init__(message)
        self.retry_after = retry_after


def _entier(env: str, brut: Optional[str], defaut: int, minimum: int) -> int:
    if brut is None or brut.strip() == "":
        return defaut
    try:
        valeur = int(brut.strip())
    except ValueError:
        valeur = minimum - 1
    if valeur < minimum:
        raise ReglageInvalide(f"{env}={brut!r} illisible : un entier, au moins {minimum}.")
    return valeur


@functools.cache
def plafond() -> int:
    """Le plafond du processus, lu une fois. Les bancs repartent par `cache_clear()`."""
    brut = os.environ.get("OTO_FUNCTIONS_MAX_SIMULTANEES")
    return _entier(ENV_MAX, brut, MAX_DEFAUT, 1)


@functools.cache
def reserve() -> int:
    """Les places qu'un compte laisse aux autres, lu une fois (`cache_clear()`)."""
    brut = os.environ.get("OTO_FUNCTIONS_RESERVE")
    return _entier(ENV_RESERVE, brut, RESERVE_DEFAUT, 0)


def par_compte() -> int:
    """Ce qu'UN compte peut tenir à la fois : le plafond moins la réserve, au moins 1."""
    return max(1, plafond() - reserve())


@functools.cache
def attente_max_s() -> float:
    """L'attente d'une place avant le refus, lue une fois (`cache_clear()`). `0` = refus
    immédiat, le comportement d'avant le 28/11/2026."""
    brut = os.environ.get("OTO_FUNCTIONS_ATTENTE_MAX_S")
    if brut is None or brut.strip() == "":
        return ATTENTE_MAX_DEFAUT
    try:
        valeur = float(brut.strip())
    except ValueError:
        valeur = -1.0
    if not (valeur >= 0 and math.isfinite(valeur)):
        raise ReglageInvalide(f"{ENV_ATTENTE}={brut!r} illisible : des secondes, au moins 0.")
    return valeur


def verifier_configuration() -> None:
    """Au démarrage (`server.main`) : un réglage illisible refuse de démarrer ICI."""
    plafond()
    reserve()
    attente_max_s()


#: Ce que la part par compte compte : une personne dans une org, `(sub, org_id)`.
Compte = tuple[Optional[str], Optional[int]]


def compte(sub: Optional[str], org_id: Optional[int] = None) -> Compte:
    """La clé d'un compte : la personne ET l'org de l'exécution (cf. docstring)."""
    return (sub, None if org_id is None else int(org_id))


@dataclass(frozen=True)
class _Tenue:
    """Une place tenue : par qui, depuis quand, jusqu'à quand au plus tard."""
    compte: Compte
    debut: float
    #: L'instant (`time.monotonic`) où la place est rendue au plus tard ; None = inconnu.
    echeance: Optional[float]


@dataclass(eq=False)
class _Demande:
    """Un demandeur dans la file : qui, dans quel ordre, et comment le réveiller."""
    compte: Compte
    rang: int
    duree_max_s: Optional[float]
    #: Rempli par `_attribuer` (sous le verrou) quand une place lui revient.
    jeton: Optional[int] = None
    #: Le fil qui attend (aller simple).
    evenement: Optional[threading.Event] = None
    #: La tâche qui attend (pont) : son futur, et la boucle qui le possède.
    futur: Optional[asyncio.Future] = None
    boucle: Optional[asyncio.AbstractEventLoop] = field(default=None, repr=False)


_verrou = threading.Lock()
_par_compte: dict[Compte, int] = {}
_tenues: dict[int, _Tenue] = {}
_file: list[_Demande] = []
_jetons = itertools.count(1)
_rangs = itertools.count(1)
_total = 0


def en_cours() -> int:
    """Les exécutions qui tiennent une place."""
    return _total


def en_attente() -> int:
    """Les demandes qui attendent une place."""
    return len(_file)


def _attente(tenues: list[_Tenue], maintenant: float) -> tuple[int, list[_Tenue]]:
    """`(les secondes avant que l'une de ces places se libère au plus tard, celles qui
    ont dépassé leur borne)`. Sous `_verrou`, sans E/S : l'appelant journalise après.
    Une place sans borne, ou qui a dépassé la sienne, peut se libérer à tout instant ou
    ne dit plus rien : l'attente ne dépasse alors pas `ATTENTE_S`."""
    vivantes = [t for t in tenues if t.echeance is not None and t.echeance > maintenant]
    depassees = [t for t in tenues if t.echeance is not None and t.echeance <= maintenant]
    if not vivantes:
        return ATTENTE_S, depassees
    attente = max(1, math.ceil(min(t.echeance for t in vivantes) - maintenant))
    return (min(attente, ATTENTE_S) if len(vivantes) < len(tenues) else attente), depassees


def _signaler(depassees: list[_Tenue], maintenant: float) -> None:
    """Ni le compte ni les données : seulement ce qui dit qu'une place ne se rend pas."""
    for t in depassees:
        logger.warning("bac à sable : une place est tenue %.0f s après sa borne (prise "
                       "il y a %.0f s)", maintenant - t.echeance, maintenant - t.debut)


def _tenir(cle: Compte, duree_max_s: Optional[float], maintenant: float) -> int:
    """Compte une place pour le compte `cle`. Sous `_verrou`."""
    global _total
    _par_compte[cle] = _par_compte.get(cle, 0) + 1
    _total += 1
    jeton = next(_jetons)
    _tenues[jeton] = _Tenue(cle, maintenant, None if duree_max_s is None
                            else maintenant + duree_max_s)
    return jeton


def _liberer(cle: Compte, jeton: Optional[int]) -> None:
    """Décompte une place. Sous `_verrou`."""
    global _total
    reste = _par_compte.get(cle, 0) - 1
    if reste > 0:
        _par_compte[cle] = reste
    else:
        _par_compte.pop(cle, None)
    _total -= 1
    _tenues.pop(jeton, None)


def _attribuer(maintenant: float) -> None:
    """Donne les places libres aux demandeurs : à chaque place, celui dont le compte en
    tient le MOINS, puis le plus ancien ; un compte à `par_compte()` passe son tour.
    Sous `_verrou`, sans E/S : un réveil de futur passe par `call_soon_threadsafe`."""
    maximum, par_un = plafond(), par_compte()
    while _total < maximum and _file:
        eligibles = [d for d in _file if _par_compte.get(d.compte, 0) < par_un]
        if not eligibles:
            return
        elu = min(eligibles, key=lambda d: (_par_compte.get(d.compte, 0), d.rang))
        _file.remove(elu)
        elu.jeton = _tenir(elu.compte, elu.duree_max_s, maintenant)
        if elu.evenement is not None:
            elu.evenement.set()
        elif elu.futur is not None and elu.boucle is not None:
            elu.boucle.call_soon_threadsafe(_resoudre, elu.futur)


def _resoudre(futur: asyncio.Future) -> None:
    if not futur.done():
        futur.set_result(None)


def _refus(cle: Compte) -> BacASableOccupe:
    """Le refus servi au compte `cle` après son attente : lequel des deux plafonds le retient, et
    quand une place se libère au plus tard. Prend le verrou."""
    maximum, par_un = plafond(), par_compte()
    with _verrou:
        maintenant = time.monotonic()
        if _par_compte.get(cle, 0) >= par_un:
            siennes = [t for t in _tenues.values() if t.compte == cle]
            attente, depassees = _attente(siennes, maintenant)
            depuis = (f" (the first started {max(0, int(maintenant - min(t.debut for t in siennes)))} s ago)"
                      if siennes else "")
            refus = BacASableOccupe(
                f"sandbox_busy: this person already has {par_un} execution(s) running "
                f"in the sandbox in this org{depuis} — the per-person-per-org limit, which leaves "
                f"{maximum - par_un} place(s) to others; retry in {attente} s, once one "
                "has finished.", retry_after=attente)
        else:
            attente, depassees = _attente(list(_tenues.values()), maintenant)
            refus = BacASableOccupe(
                f"sandbox_busy: the sandbox is running its maximum of {maximum} "
                f"simultaneous executions and {len(_file)} more are waiting — the "
                f"server-wide limit, shared by all accounts; retry in {attente} s, when "
                "the first of them is due to end.", retry_after=attente)
    _signaler(depassees, maintenant)
    return refus


def _entrer(demande: _Demande) -> bool:
    """Met `demande` dans la file et attribue. True = une place lui est revenue tout de
    suite. Prend le verrou."""
    with _verrou:
        _file.append(demande)
        _attribuer(time.monotonic())
        return demande.jeton is not None


def _renoncer(demande: _Demande) -> None:
    """La demande renonce : hors de la file, et sa place rendue si elle en avait reçu
    une entre-temps (elle part alors au suivant). Prend le verrou."""
    with _verrou:
        if demande in _file:
            _file.remove(demande)
        if demande.jeton is not None:
            _liberer(demande.compte, demande.jeton)
            demande.jeton = None
            _attribuer(time.monotonic())


def _rendre(demande: _Demande) -> None:
    """La place d'une exécution finie, rendue et attribuée au suivant. Prend le verrou."""
    with _verrou:
        if demande.jeton is not None:
            _liberer(demande.compte, demande.jeton)
            demande.jeton = None
        _attribuer(time.monotonic())


def _delai(attente_s: Optional[float]) -> float:
    return attente_max_s() if attente_s is None else max(0.0, attente_s)


@contextlib.contextmanager
def place(sub: Optional[str], duree_max_s: Optional[float] = None,
          attente_s: Optional[float] = None, org_id: Optional[int] = None) -> Iterator[None]:
    """Une place pour UNE exécution de `sub` dans l'org `org_id`, depuis un FIL (l'aller simple). Attend son
    tour au plus `attente_s` (défaut `attente_max_s()`), puis `BacASableOccupe` sans rien
    avoir pris. La place est rendue en sortant, quoi qu'il arrive. `duree_max_s` : le
    temps au bout duquel elle est rendue au plus tard — ce que le `Retry-After` d'un refus
    annonce. ⚠️ Jamais depuis la boucle : elle attendrait bloquée — `place_async`."""
    cle = compte(sub, org_id)
    demande = _Demande(cle, next(_rangs), duree_max_s, evenement=threading.Event())
    if not _entrer(demande):
        servie = demande.evenement.wait(_delai(attente_s))
        if not servie:
            _renoncer(demande)
            # Une place a pu arriver entre la fin de l'attente et le renoncement : rendue
            # par `_renoncer`, elle n'est pas prise ici, et le refus reste juste.
            raise _refus(cle)
    try:
        yield
    finally:
        _rendre(demande)


@contextlib.asynccontextmanager
async def place_async(sub: Optional[str], duree_max_s: Optional[float] = None,
                      attente_s: Optional[float] = None,
                      org_id: Optional[int] = None) -> AsyncIterator[None]:
    """`place` depuis la BOUCLE (le pont des recettes-code) : l'attente est un futur
    asyncio, la boucle n'est jamais bloquée. Annulée pendant l'attente (client parti,
    tâche annulée), la demande sort de la file et rend ce qu'elle aurait reçu."""
    boucle = asyncio.get_running_loop()
    cle = compte(sub, org_id)
    demande = _Demande(cle, next(_rangs), duree_max_s, futur=boucle.create_future(),
                       boucle=boucle)
    if not _entrer(demande):
        try:
            await asyncio.wait_for(asyncio.shield(demande.futur), _delai(attente_s))
        except asyncio.TimeoutError:
            _renoncer(demande)
            raise _refus(cle) from None
        except BaseException:
            # Annulation (ou toute autre sortie) pendant l'attente : rien ne fuit.
            _renoncer(demande)
            raise
    try:
        yield
    finally:
        _rendre(demande)
