"""Façade du store PostgreSQL.

⚠️ Transitoire (refactor de découpage). La plomberie est extraite en `_conn`,
la DDL en `_schema`, l'init/migrations en `_init` ; le reste des fonctions de
domaine vit encore dans `legacy.py` (réparti par domaine aux barreaux suivants).

Ce `__init__` ré-exporte l'intégralité du namespace de ces sous-modules pour que
la surface historique `db.<symbole>` (publics + privés consommés à l'extérieur
comme `db._connect` / `db._ds_filter_clauses`) reste identique. Le shim
disparaîtra quand `legacy.py` sera vide et les imports explicites.
"""
from __future__ import annotations

from . import _conn, _schema, _init, legacy

# Ré-export exhaustif (publics + privés à un underscore), garanti no-op vs l'ancien
# module monolithique. Les noms dunder sont laissés au package lui-même.
_g = globals()
for _mod in (_conn, _schema, _init, legacy):
    for _name in dir(_mod):
        if not _name.startswith("__"):
            _g[_name] = getattr(_mod, _name)
del _g, _mod, _name
