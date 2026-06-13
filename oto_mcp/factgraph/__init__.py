"""Substrat « graphe de facts structurés » (ADR 0008).

Write-model générique (2 tables `fact`+`edge`, schéma PG `factgraph`) + facts
structurés (registre de schémas par `kind`, validés à l'écriture) + read-model
par cas d'usage (projections matérialisées, ex. prospection).

- `schemas`    : registre kind→schéma + règles d'arêtes (pur, sans DB).
- `store`      : le graphe générique, scopé par workspace (= org × cas d'usage).
- `projection` : read-model prospection (file priorisée scorée + claim atomique).
- `prospection`: couche service org-scopée (surface unique REST + MCP).
"""

from . import projection, prospection, schemas, store

__all__ = ["schemas", "store", "projection", "prospection"]
