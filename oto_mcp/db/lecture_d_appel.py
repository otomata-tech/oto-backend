"""Une lecture faite UNE fois par appel en lecture seule (oto-backend#1148).

`GET /api/me/connectors` et `access.status_for` posent la même question à la base des
dizaines de fois dans un seul appel : l'org active (28 fois), l'équipe active (14), les
comptes Google du membre (une fois par service Google), les réglages des connecteurs
(une fois par service Microsoft), le rôle et la pause de chaque prêteur… Chaque module
du chemin relit ce dont il a besoin, sans savoir qu'un voisin vient de le lire. À
3,14 ms l'aller-retour vers la base managée, ~200 lectures font ~0,8 s par appel —
l'essentiel de la médiane mesurée en production (1,75 s).

Le remède le plus étroit : une fonction de lecture décorée par `lecture_d_appel` rend,
dans une portée `db.reuse_connection()`, la réponse qu'elle a déjà donnée POUR LES MÊMES
ARGUMENTS dans cette même portée. Hors portée, elle lit la base à chaque appel, comme
avant.

Ce n'est PAS un cache, et c'est ce qui le rend sûr :

- **sa vie est celle de l'appel** : la mémoire est portée par l'emprunt de
  `reuse_connection()` et meurt avec lui, à la sortie du bloc — l'invalidation est la
  fin de l'appel, rien ne survit à la requête suivante ;
- **le chemin ne fait que lire** : c'est déjà la condition de `reuse_connection()`
  (autocommit), gardée par un banc qui relève chaque requête du chemin — aucune
  écriture de l'appel ne peut donc rendre périmée une lecture du même appel ;
- **jamais une référence partagée** : chaque appelant reçoit sa COPIE profonde (la
  ligne d'une table est un dict modifiable ; deux appelants qui se la passeraient se
  répondraient l'un à l'autre au premier `.update()`).

L'appel voit donc la base telle qu'elle était à sa première lecture de chaque fait —
une vue plus cohérente que celle d'avant, où deux modules du même appel pouvaient lire
deux états différents de la même ligne.

⚠️ Ne décorer qu'une lecture PURE de la base : même arguments ⟹ même requête, et rien
d'autre (ni variable de contexte, ni horloge, ni paramètre `conn`). Un argument que la
clé ne sait pas figer lève : une lecture qu'on croirait mémorisée et qui ne le serait
pas ne se verrait qu'à la mesure.
"""
from __future__ import annotations

import copy
import functools
from typing import Any, Callable, TypeVar

from ._conn import _emprunt_partage

F = TypeVar("F", bound=Callable[..., Any])


def _figer(valeur: Any) -> Any:
    """La forme hachable d'un argument : une liste (`emails_by_subs(['x'])`)
    devient un tuple. Ce qui reste non hachable LÈVE au hachage de la clé."""
    if isinstance(valeur, (list, tuple)):
        return tuple(_figer(v) for v in valeur)
    if isinstance(valeur, (set, frozenset)):
        return frozenset(_figer(v) for v in valeur)
    if isinstance(valeur, dict):
        return tuple(sorted((k, _figer(v)) for k, v in valeur.items()))
    return valeur


def lecture_d_appel(fn: F) -> F:
    """Décorateur : cf. le module. `conn=` explicite ⟹ lecture directe (la requête
    appartient alors à la transaction de l'appelant, qui peut écrire)."""
    nom = f"{fn.__module__}.{fn.__qualname__}"

    @functools.wraps(fn)
    def _lue(*args, **kwargs):
        emprunt = _emprunt_partage.get()
        if emprunt is None or kwargs.get("conn") is not None:
            return fn(*args, **kwargs)
        cle = (nom, _figer(args), _figer(kwargs))
        hash(cle)
        if cle not in emprunt.lectures:
            emprunt.lectures[cle] = copy.deepcopy(fn(*args, **kwargs))
        return copy.deepcopy(emprunt.lectures[cle])
    return _lue  # type: ignore[return-value]
