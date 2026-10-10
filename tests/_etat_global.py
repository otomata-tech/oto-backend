"""L'état GLOBAL du processus qu'un test ne doit pas laisser changé derrière lui (#1111).

Un réglage de la bibliothèque standard vaut pour tout le processus : `csv.field_size_limit`,
la limite de récursion, le délai par défaut des sockets, `logging.disable`, le répertoire
courant, le masque de création de fichiers, la locale, le contexte `decimal`, le fuseau,
`sys.path`. Un test (ou le code qu'il appelle) qui en change un SANS le rendre change le
comportement des tests joués après lui dans le même worker — et seulement dans ceux-là.

Vécu le 09/10/2026 : un banc relisait une archive par `deploy/archive_tool_calls.py`, qui
relevait `csv.field_size_limit` à 10 Mo sans la rendre ; dans la part où `test_csv_tolerant`
passait APRÈS lui sur le même worker, la garde « cellule trop grosse » ne se déclenchait plus
(3 rouges au tag, déploiement bloqué). Jouer chaque fichier seul ne pouvait pas le voir.

`tests/conftest.py` relève cet état avant chaque test et le compare après : un écart est
une ERREUR qui nomme le test et le réglage, et l'état est remis pour ne pas propager la
fuite au test suivant. Les outils qui restaurent d'eux-mêmes (`monkeypatch.chdir`,
`monkeypatch.syspath_prepend`, `monkeypatch.setattr`) sont démontés avant la comparaison.

Hors du relevé, délibérément : les filtres `warnings` (pytest les isole déjà test par test),
l'état de `random` (il bouge dès qu'un test tire un nombre — un relevé crierait à chaque
test sans rien prouver).
"""
from __future__ import annotations

import csv
import decimal
import locale
import logging
import os
import socket
import sys
import time

_ABSENT = "<répertoire courant supprimé>"


def _cwd() -> str:
    try:
        return os.getcwd()
    except FileNotFoundError:
        return _ABSENT


def _umask() -> int:
    courant = os.umask(0)
    os.umask(courant)
    return courant


def _decimal() -> tuple:
    c = decimal.getcontext()
    return (c.prec, c.rounding, c.Emin, c.Emax, c.capitals, c.clamp,
            tuple(sorted(t.__name__ for t, actif in c.traps.items() if actif)))


def releve() -> dict:
    return {
        "csv.field_size_limit": csv.field_size_limit(),
        "sys.getrecursionlimit": sys.getrecursionlimit(),
        "socket.getdefaulttimeout": socket.getdefaulttimeout(),
        "logging.disable": logging.root.manager.disable,
        "os.getcwd": _cwd(),
        "os.umask": _umask(),
        "locale.setlocale": locale.setlocale(locale.LC_ALL, None),
        "decimal.getcontext": _decimal(),
        "time.tzname": (time.tzname, time.timezone),
        "sys.path": tuple(sys.path),
    }


def _remettre(nom: str, valeur, contexte_decimal) -> None:
    if nom == "csv.field_size_limit":
        csv.field_size_limit(valeur)
    elif nom == "sys.getrecursionlimit":
        sys.setrecursionlimit(valeur)
    elif nom == "socket.getdefaulttimeout":
        socket.setdefaulttimeout(valeur)
    elif nom == "logging.disable":
        logging.disable(valeur)
    elif nom == "os.getcwd" and valeur != _ABSENT:
        os.chdir(valeur)
    elif nom == "os.umask":
        os.umask(valeur)
    elif nom == "locale.setlocale":
        locale.setlocale(locale.LC_ALL, valeur)
    elif nom == "decimal.getcontext":
        decimal.setcontext(contexte_decimal)
    elif nom == "time.tzname":
        time.tzset()
    elif nom == "sys.path":
        sys.path[:] = list(valeur)


def contexte_decimal():
    return decimal.getcontext().copy()


#: Remis après chaque test, mais NON reprochés — avec la raison. `sys.path` : le code
#: testé l'allonge légitimement dans son propre processus (la configuration Alembic du
#: dépôt, `prepend_sys_path = .` ; les scripts de mesure qui importent l'arbre qu'ils
#: mesurent). Ce qui compte pour l'axe — qu'aucun test n'hérite de l'allongement d'un
#: autre — est tenu par la remise. Un import qui ne marcherait que grâce à un voisin se
#: voit en jouant le fichier seul.
REMIS_SANS_REPROCHE = {"sys.path"}


def ecarts_et_remise(avant: dict, contexte_decimal_avant) -> list[str]:
    """Compare à `avant`, REMET chaque réglage changé, rend les écarts REPROCHÉS."""
    apres = releve()
    ecarts = []
    for nom, valeur in avant.items():
        if apres[nom] != valeur:
            _remettre(nom, valeur, contexte_decimal_avant)
            if nom not in REMIS_SANS_REPROCHE:
                ecarts.append(f"{nom} : {valeur!r} avant, {apres[nom]!r} après")
    return ecarts
