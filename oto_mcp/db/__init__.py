"""Façade du store PostgreSQL.

⚠️ Transitoire (refactor de découpage, barreau 1). Tout le code vit encore dans
`legacy.py` ; ce `__init__` ré-exporte l'intégralité de son namespace pour que la
surface historique `db.<symbole>` (≈216 noms, dont des privés consommés à
l'extérieur comme `db._connect` / `db._ds_filter_clauses`) reste identique.

Les barreaux suivants extrairont `legacy.py` en modules thématiques
(`_conn`, `_schema`, `users`, …) ; ce shim disparaîtra quand `legacy.py` sera vide.
"""
from __future__ import annotations

from . import legacy as _legacy

# Ré-export exhaustif (publics + privés à un underscore), garanti no-op vs l'ancien
# module monolithique. Les noms dunder sont laissés au package lui-même.
_g = globals()
for _name in dir(_legacy):
    if not _name.startswith("__"):
        _g[_name] = getattr(_legacy, _name)
del _g, _name
