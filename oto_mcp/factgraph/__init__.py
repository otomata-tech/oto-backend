"""Substrat « graphe de facts structurés » (ADR 0008, amendé ADR 0027).

Write-model générique (2 tables `fact`+`edge`, schéma PG `factgraph`) + facts
structurés (registre de schémas par `kind`, validés à l'écriture). Le **vertical
prospection** (read-model `prospect` scoré + cockpit) a été retiré (ADR 0027) ;
ne reste que le substrat générique, exposé par la capacité `facts` + la vue
dashboard « Fact graph ».

- `schemas` : registre kind→schéma + rôles de rendu + règles d'arêtes (pur, sans DB).
- `store`   : le graphe générique, scopé par workspace (= org × cas d'usage).
"""

from . import schemas, store

__all__ = ["schemas", "store"]
