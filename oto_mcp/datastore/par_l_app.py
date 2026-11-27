"""L'accès à un tableau À TRAVERS UNE APP seulement (`docs/apps.md` § Accès par l'app).

Une personne peut tenir un tableau par l'app SEULE (`app_table_access`, posé par
`oto_apps op=grant_through_app`) : elle n'en a aucun partage, et aucune porte ordinaire
ne la laisse y toucher — ni les outils `data_*`, ni la face REST des tableaux, ni
l'écran Tableaux : aucune ne lit cette table.

Le seul endroit où cet accès compte : la PORTÉE qu'ouvre l'appel d'une app
(`capabilities/apps_appel._par_l_app`), le temps de cet appel, pour les seuls tableaux
du manifeste RESSERRÉ au rôle du lecteur. Dans cette portée, `DatastorePg._trouver`
demande `droit(ns_id)` quand la résolution ordinaire ne trouve rien — ou ne donne pas
l'écriture —, et sert le tableau. Tout le reste de l'appel (manifeste, champs, valeurs,
rôle, auteur ∩ lecteur, consentement) reste jugé comme avant : la portée ne fait que
remplacer « le lecteur a un partage du tableau ».

Hors de la portée (valeur par défaut `None`) : rien, à aucun endroit.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator, Optional

class Portee:
    """La portée d'UN appel d'app : pour QUI (le lecteur — un store d'un autre compte
    n'y prend rien), le droit par tableau, et les tableaux effectivement servis par elle
    (une écriture servie ainsi ne se défait pas par les routes de tableau)."""

    def __init__(self, sub: str, droit: Callable[[str], Optional[str]]):
        self.sub = sub
        self._droit = droit
        self.servis: set[str] = set()

    def droit(self, ns_id: str) -> Optional[str]:
        return self._droit(str(ns_id))


_PORTEE: ContextVar[Optional[Portee]] = ContextVar("acces_par_l_app", default=None)


@contextmanager
def portee(p: Portee) -> Iterator[Portee]:
    jeton = _PORTEE.set(p)
    try:
        yield p
    finally:
        _PORTEE.reset(jeton)


# Le RUN d'un agent lancé depuis une app (`apps_appel._droit_du_run`, enregistré par la
# capacité : la couche datastore ne remonte pas vers les capacités). `(sub, ns_id) ->
# "read" | None`. Et ce qui note au journal des lectures d'app un tableau qu'un run a
# ouvert par cet accès (`apps_appel._noter_run`, `(sub, ns_id) -> None`).
_DU_RUN: Optional[Callable[[str, str], Optional[str]]] = None
_NOTER_RUN: Optional[Callable[[str, str], None]] = None


def enregistrer_run(f: Callable[[str, str], Optional[str]],
                    noter: Optional[Callable[[str, str], None]] = None) -> None:
    global _DU_RUN, _NOTER_RUN
    _DU_RUN = f
    _NOTER_RUN = noter


def peut_servir(sub: Optional[str]) -> bool:
    """Une portée PEUT-elle s'ouvrir pour `sub` ici — sans rien lire de la base : l'appel
    d'une app en cours pour lui, ou le travail hébergé de la requête est le sien. Faux
    dans tout autre chemin : le store n'y paie rien de plus."""
    if sub is None:
        return False
    p = _PORTEE.get()
    if p is not None:
        return sub == p.sub
    if _DU_RUN is None:
        return False
    from .. import verrou_org
    v = verrou_org.travail_de_la_requete()
    return v is not None and v.job_id is not None and v.sub == sub


def droit(sub: Optional[str], ns_id: str) -> Optional[str]:
    """Ce que la portée en cours ouvre à `sub` sur ce tableau — l'appel d'une app, ou le
    run d'un agent que `sub` a lancé depuis une app ; None sinon."""
    if sub is None:
        return None
    p = _PORTEE.get()
    if p is not None:
        return p.droit(str(ns_id)) if sub == p.sub else None
    return _DU_RUN(sub, str(ns_id)) if _DU_RUN is not None else None


def servi(sub: str, ns_id: str) -> None:
    """Le tableau vient d'être servi par l'accès de l'app : noté dans la portée de
    l'appel (le journal des lectures en tire `access = 'app'`), ou — hors d'elle, donc
    par le run d'un agent lancé depuis l'app — noté au journal comme une ouverture."""
    p = _PORTEE.get()
    if p is not None:
        p.servis.add(str(ns_id))
    elif _NOTER_RUN is not None:
        _NOTER_RUN(sub, str(ns_id))
