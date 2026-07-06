---
title: Boucle d'usage (ADR 0017 — déroulés + feedback volontaire)
type: reference
description: >-
  Référence du flux d'événements de session unifié dans oto-backend (ADR 0017) :
  corrélation session_id + run_id sur tool_calls (colonnes OTO-locales, index dans
  ALTER pas dans _SCHEMA), tools spine run_start/run_finish (tools/doctrine_run.py,
  pile session FastMCP, runs imbriqués OK), et capacité feedback (signal
  tool_feedback|gap → table durable usage_signals hors prune 30j). Détaille les
  projections admin /api/admin/usage/* (runs, gaps, tool-quality, signals filtrables
  open/resolved) et la résolution de signaux via oto_admin_resolve_signal. À charger
  pour comprendre comment tracer un déroulé d'agent, remonter un gap d'outil, ou
  déboguer un run_id manquant sur les appels.
adr:
  - "0017"
---

# Boucle d'usage (ADR 0017 — déroulés + feedback volontaire)

Un **flux d'événements de session** unifie le calllog (involontaire) + le feedback
volontaire d'agent + les runs / déroulés. Détail : ADR 0017 (repo public
`otomata-tech/oto`). Surfaces livrées (B1–B6) :

- **Corrélation** : `tool_calls` gagne 2 colonnes **OTO-LOCALES** (hors contrat canonique
  otomata-calllog) `session_id` (session mcp transport) + `run_id` (déroulé). Stampées
  par `server._calllog_sink` qui lit `get_context().session_id` + le run actif. ⚠️ piège
  rattrapé : l'index sur `run_id` va dans le bloc **ALTER** d'`init_db` (après l'ADD COLUMN),
  **jamais** dans `_SCHEMA` (no-op sur table existante → `UndefinedColumn` au boot).
- **Runs / déroulés** : tools spine `run_start`/`run_finish` (`tools/doctrine_run.py`) ;
  `run_start(label, doctrine?)` ouvre une doctrine nommée (`doctrine`=slug) **ou** un run
  one-shot (sans `doctrine`), même trace. Le `run_id` vit dans une **pile en état de
  session FastMCP** (`doctrine_run.py`, runs imbriqués OK), stampé sur chaque appel côté
  serveur — l'agent ne thread rien.
- **Signaux volontaires** : capacité MCP+REST unique (`capabilities/usage.py`) `feedback`
  — axe explicite `signal` ∈ `tool_feedback | gap` → table **durable** `usage_signals`
  (hors prune 30j). `gap` = cas d'usage non couvert (l'agent capte la demande non satisfaite).
- **Projections** (opérateur) : `/api/admin/usage/{runs,runs/{id},gaps,tool-quality,signals}`
  (`capabilities/usage.py`, PLATFORM_ADMIN) → vue dashboard `UsageView.vue` (« usage & déroulés »).
  `signals` filtrable par `status` (`open|resolved`) ; faces MCP `oto_admin_list_signals`.
- **Résolution** : un signal se marque traité via `POST /api/admin/usage/signals/{id}/resolve`
  (MCP `oto_admin_resolve_signal`, PLATFORM_ADMIN) — colonnes `resolved_at/resolved_by/resolution`
  (NULL = ouvert) ; `resolved=false` ré-ouvre. Le backlog vivant = `signals?status=open`.
- **Harnais impératif** : `_SERVER_INSTRUCTIONS` pousse l'agent à réflexer oto, encadrer
  par `run_start/finish` et émettre `feedback`.
- Déféré (otomata#32) : `why`-par-appel.
