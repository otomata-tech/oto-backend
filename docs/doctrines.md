---
title: Doctrines & instructions d'org
type: reference
description: >-
  Référence du mécanisme de doctrine oto-backend : prose opératoire métier par org,
  structurée en skills identifiés par slug et versionnés dans org_instructions +
  org_instruction_revisions. Détaille les 4 tools MCP (oto_get_doctrine sans slug =
  call de début de session renvoyant base + index, avec slug = skill nommé ;
  oto_set_doctrine, oto_list_doctrines, oto_delete_doctrine), l'autz conditionnelle
  org_admin self-service vs platform_admin cross-org, le versioning append-only avec
  revert via from_version, et les gotchas (verrou advisory par org/slug, pas de cache,
  pas d'instruction par namespace d'outil). Aligne sur ADR 0006 (harnais sans état).
adr:
  - "0006"
---

# Doctrines & instructions d'org

Prose opératoire métier (workflows validés, règles, vocabulaire) pour les users qui pilotent
oto **sans produit applicatif dédié** (ex. un process avoir compta client
GoCardless → Pennylane → back-office, piloté directement depuis Claude sur un sous-ensemble
de tools). oto est la maison naturelle de cette prose faute de produit. Aligné
**ADR 0006** (harnais-vs-substrat, repo public `otomata-tech/oto`) : une org oto + sa
doctrine = un **harnais sans état** (étage zéro) ; le jour où un workflow doit persister un
pipeline/des statuts, il graduate en harnais à part.

**Modèle = skills, à la Claude Code.** Une org possède des **instructions markdown**
identifiées par `slug`, chacune versionnée :
- La **doctrine de base** (slug réservé **interne** `BASE_SLUG`, jamais vu de l'user) est servie
  d'office — accédée via `oto_get_doctrine()` **sans slug**.
- Les autres slugs = des **skills** chargés à la demande (progressive disclosure) : la
  doctrine de base ne porte que l'**index** (slug + titre + quand-l'utiliser), le détail
  se charge au besoin.

**Surface = 4 tools** (refacto 2026-06-18, ex-11 ; « moins d'outils, plus d'args »). Un `org_id`
optionnel **fond membre↔platform-admin** : absent = ton **org active** ; présent = une **autre org**
par id (réservé platform_admin). Autz conditionnelle dans `tools/orgs.py`
(`_resolve_org_read`/`_resolve_org_write`).
- **Lecture** : `oto_get_doctrine([slug, org_id, scope, version, with_history])` — sans `slug` =
  `{doctrine, group_doctrine, doctrines[]}` (base org + base groupe + index), le call de **DÉBUT DE
  SESSION** ; avec `slug` = le markdown d'une doctrine nommée. `oto_list_doctrines([query, org_id,
  scope])` = catalogue/recherche. Scopés à l'**org active** (+ groupe actif) — servis aux seuls
  membres. **Vide sans erreur** si pas d'org active (`_SERVER_INSTRUCTIONS` invite à `oto_get_doctrine()`).
- **Écriture** : `oto_set_doctrine([body_md, slug, org_id, title, desc, from_version])` (base = slug
  omis ; nommée sinon ; `from_version` = revert) + `oto_delete_doctrine(slug[, org_id])`. Autz :
  `org_id` absent → org active, **org_admin** requis (self-service MCP, NOUVEAU) ; présent → autre
  org, **platform_admin** requis (l'opérateur provisionne n'importe quelle org). La SPA dashboard
  édite aussi via REST `/api/me/instructions*` (org_admin de l'org active).
- **Versioning** : chaque écriture incrémente `version` (sur le courant) et archive un snapshot
  append-only. Revert = re-poser le corps d'une version → nouvelle version (jamais d'effacement
  d'historique sauf `delete`).
- **Store** : `org_instructions(org_id, slug PK partiel, title, description, body_md, version,
  set_by, created_at, updated_at)` + `org_instruction_revisions(org_id, slug, version PK, …)`
  (`db._SCHEMA`, palier org) ; accès dans `org_store.py` (`get/list/search/set/delete_instruction`,
  `list_instruction_versions`, `normalize_slug`, `BASE_SLUG`). **En clair** (prose, pas un
  credential → hors coffre chiffré). **Pas de cache** : lecture DB à l'appel. Écriture sérialisée
  par `(org, slug)` via verrou advisory (mirroir `add_org_member`).
- **Pas d'instruction par namespace d'outil** : un gotcha d'outil est vrai pour tout le monde et
  évolue avec le code du connecteur → sa place reste le repo (docstring, `_SERVER_INSTRUCTIONS`),
  versionné avec l'outil.
