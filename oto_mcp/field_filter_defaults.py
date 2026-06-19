"""Politiques de redaction de champs par défaut, côté serveur.

`~/.otomata/config.yaml` (la source de `FieldFilter.from_config`) est absente du
serveur ; on pose donc ici un **plancher PII explicite** par connecteur. Ce défaut
ne s'applique que tant que l'org n'a **rien** configuré pour le service : dès que
l'org_admin pose une politique (via le dashboard / `oto_set_org_field_filters`),
elle devient autoritaire (décision « contrôle total org »).

Forme = celle d'un bloc `field_filters.<service>` :
    { "salt": str?, "rules": [ {fields, action, ...} ] }
"""
from __future__ import annotations

# Silae (paie FR) : masque les coordonnées bancaires (rarement utiles à un agent
# d'analyse), garde noms/montants. Hérité du `_REDACT` jadis hardcodé dans
# tools/silae.py.
SERVER_DEFAULTS: dict[str, dict] = {
    "silae": {
        "rules": [
            {"fields": ["iban", "bic", "rib"], "action": "mask", "keep_last": 4},
        ],
    },
}
