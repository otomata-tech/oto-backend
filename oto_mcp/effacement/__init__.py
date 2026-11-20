"""Effacer une org, entière et vérifiée (RGPD art. 17) — `docs/effacement-d-org.md`.

Le pendant destructeur de l'export par périmètre (`export_perimetre`) : il efface
exactement ce que l'export emporterait pour une org, plus ce que l'export laisse
derrière lui (ses tables `exclue` et `instance`), table par table, selon une décision
DÉCLARÉE pour chacune (`disposition`).
"""
