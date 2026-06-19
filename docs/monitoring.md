# Monitoring des appels MCP

`CallMonitoringMiddleware` (`middleware.py`) journalise **chaque** appel de tool
via le hook `on_call_tool` (point d'interception unique) dans la table
`tool_call_log(id, sub, tool_name, called_at, duration_ms, ok, error)` : `sub`
JWT courant (nullable — stdio local non authentifié = NULL), durée, statut
succès/échec + message tronqué. Best-effort : une erreur d'écriture du journal
ne fait jamais échouer l'appel ni n'avale l'exception métier. Couvre les deux
formes d'échec fastmcp (exception propagée OU résultat `isError`).

Volumétrie bornée par un prune au boot (`prune_tool_call_log` dans `init_db`,
rétention `OTO_MCP_CALL_LOG_RETENTION_DAYS`, défaut 30j) — les restarts deploy
fréquents suffisent à garder la table petite.

Surface admin : `GET /api/admin/monitoring/summary?days=` (agrégats total /
échecs / users actifs + ventilation par tool / par user / par jour) et
`GET /api/admin/monitoring/calls` (journal brut, filtres `limit/sub/tool/errors/days`).
Consommé par le front `account/` (section admin « monitoring mcp »,
`AdminMcpMonitoring.vue` + store `admin.loadMonitoring`).
