---
title: Contexte d'org & d'équipe
type: reference
description: >-
  Le seam unique `access.current_org(sub)` = session ?? consultation ?? maison, pourquoi il 
  est scopé sur l'ACTEUR courant (et le bug vécu quand on l'utilise pour un tiers), les troi
  s notions distinctes, le view-as USER en lecture seule et l'invariant groupe ⊂ org.
---

# Org/équipe : session vs maison vs consultation (ADR 0023)

> Extrait de `CLAUDE.md` le 2026-08-27 — le contenu n'a pas changé, seule sa place a bougé.
> La carte garde le résumé + le pointeur ; le détail (schémas, incidents datés et leurs
> leçons) vit ici.

## Le seam unique

Le pointeur unique « org active » est scindé en **3 notions**, résolues par le **seam unique `access.current_org(sub)`** (mirroir `access.current_group(sub)` pour l'équipe) = `jeton d'appel ?? org du run ?? consultation ?? maison` (« session » = le jeton d'appel depuis ADR 0038 ; l'étage **org du run** est daté du 30/08/2026, §ci-dessous). **TOUTE résolution d'action passe par ce seam** (`resolve_api_key`, visibilité `session_visibility`, field-filters, guide de groupe, `/api/me`, whoami, et l'injection `org_id` des règles d'autz `_authz`) — ne plus lire `org_store.get_active_org` en direct dans un chemin de résolution (**tripwire** `tests/test_org_seam_tripwire.py` : les call-sites légitimes de la maison sont figés en allowlist ; vécu 2026-07-02 — catalogue + toggles REST scopaient la maison, le switch d'org du dashboard était ignoré, fixé `25e9f22`. Pendant front : `orgScope.spec.ts` d'oto-dashboard interdit un `fetch` nu hors du client central qui injecte `X-Oto-Org`).

⚠️ **Ce seam est scopé sur l'ACTEUR courant** : session/consultation sont stockées **par requête**, le `sub` ne sert qu'au repli `home_org`. Donc `current_org(autre_sub)` renvoie le contexte du **requérant**, pas du tiers — **NE JAMAIS** l'utiliser (ni `status_for`/`has_option`/`credential_mode_for` qui en dérivent) pour calculer l'état d'un **tiers** (écran admin). Passer son org/groupe **explicitement** via le kwarg `org`/`group` (sentinelle `access._UNSET` = défaut `current_org`, self inchangé), source = `org_store.get_active_org(target)`. Bug vécu 2026-06-24 (fiche admin montrant l'option de l'org du requérant). L'état d'un user est par ailleurs souvent **per-org** (∈ N orgs) → préférer une vue par org (cf. `tools/unipile.admin_status_by_org`).

## Les trois notions

- **Org de session** (éphémère, MCP) — override posé par `oto_use_org`/`oto_clear_org` (devenus **session-scopés**, ne touchent plus la colonne) dans `session_org.py` (store sync keyé par `ctx.session_id` — `get_state` async est inutilisable depuis `resolve_api_key` sync). Meurt avec la conversation ; repose sur l'isolation des sessions claude.ai par conversation. **Pas de jeton rejoué par appel** (bracelet serveur, pas de discipline LLM).
- **Org maison** (`org_store.get_active_org`, ex-« active_org ») — défaut persistant des **nouvelles** conversations. Posée explicitement : `oto_set_home_org` (MCP) ou `PUT /api/me/active-org` (REST/dashboard) ; **jamais** par navigation dashboard.
- **Org de consultation** (REST, view-as) — header `X-Oto-Org` (équipe : `X-Oto-Group`), posé par le **middleware ASGI `api.routes.ViewAsMiddleware`** (brut, n'altère pas le streaming `/mcp`) APRÈS **validation d'appartenance** (anti-IDOR : `roles.is_org_member`/`can_read_group`) dans un contextvar lu par `current_org`. Le dashboard consulte n'importe quelle org **sans muter l'identité MCP** — mais « consultation » = **org de TRAVAIL de l'onglet, lecture ET écriture** (poser une clé, éditer les settings y atterrissent), gatée par le rôle réel dans l'org ciblée ; le seul mode read-only est le view-as USER ci-dessous.
- **« Voir en tant que » (axe USER, REST, lecture seule)** — header `X-Oto-View-As=<sub>` posé par le même `ViewAsMiddleware`, gaté **opérateur plateforme + cible existe + méthode GET** (mutations → 403 `view_as_read_only`). `_authenticate` renvoie alors le **sub cible** (param `apply_view_as`, contextvar `session_org.current_view_user`) → tout `/api/me/*` (capacités incluses) rend la vue de la cible. **REST-only** : le MCP ne lit jamais ce contextvar (zéro impersonation dans Claude). Front : bouton sur la fiche admin + bandeau `ViewAsBanner` (`lib/viewOrg.ts`).

## L'org du run (30/08/2026, #639 — amende ADR 0023 §3 et ADR 0038 §A)

**Sans axe `_org=`, un appel fait DANS un run se résout dans l'org du run
(`runs.org_id`), pas dans l'org maison du sub.** Mesuré en prod le 29/08 (#631/#638) :
un `data_write` sans `_org`, dans un run ouvert sur une org, résolu dans l'org maison
du sub et refusé « namespace inconnu » sur un tableau que la réservation du même
run venait de résoudre — 82 refus sur sept jours, 109 sur les sept suivants, tous des
`data_write` du runner ; et le journal stampait la maison, donc la vue filtrée par org ne
montrait pas l'appel (#630). Le contournement de #638 (résolution par la réservation)
reste : il couvre un run mal posé.

Ce que l'étage fait, et ne fait pas :

- **posé par le middleware, pas relu par le seam** — `run_org.pin_for_call` tourne
  APRÈS les axes de l'appel : une lecture de `runs` par run (cache mémoire, `runs.org_id`
  est immuable), une garde d'appartenance par appel (`roles.is_org_member`), hors
  boucle. `current_org` relit une ContextVar, il ne fait aucune requête ;
- **`_org=`/`_project=` explicites gardent la priorité** — l'agent multi-org (run
  ouvert dans une org, travail dans une autre avec l'axe) ne change pas. Mesuré sur
  sept jours : 32 115 appels en run, 30 021 déjà dans l'org du run, 1 873 ailleurs avec
  un axe (inchangés), **166 changeraient d'org** (borne haute : « résolu dans la maison »
  est la seule lecture possible de « sans axe » — dont les 109 refus `data_write`) ;
- **l'appartenance reste exigée** : un sub qui n'est pas (plus) membre de l'org du run
  est refusé, nommément (« le run se déroule dans l'org X, dont tu n'es pas membre… ») —
  jamais un repli silencieux sur la maison. Mesuré : 40 appels sur sept jours (un sub,
  deux runs ouverts dans une org dont il n'est plus membre) deviendraient ce
  refus ;
- **un run inconnu de `runs`, ou hors org, ne pose rien** : `_run_id` y reste ce qu'il
  était, un identifiant de corrélation ; l'appel se résout comme avant (maison) ;
- **le journal stampe l'org résolue**, donc l'org du run : la vue `op=calls org_id=X`
  retrouve ces appels par construction, et `hors_scope` (#630) cesse de les compter ;
- **le groupe suit l'invariant** : sous l'org du run, le `home_group` d'une autre org
  n'est pas rendu (niveau org), comme sous un jeton `_org=`.

Chemin servi prouvé contre PostgreSQL (`tests/test_org_du_run_639.py`), étage et garde
sans base (`tests/test_current_org_run_stage_639.py`), hors boucle
(`tests/middleware/test_no_blocking_db_in_middleware.py`).

## Invariant groupe ⊂ org

**Invariant groupe⊂org dérivé** : un override/consultation d'org **sans** groupe explicite ⇒ niveau org (jamais le `home_group` d'une autre org) ; toute bascule d'org de session retire l'override de groupe. `/api/me` expose `active_org`/`active_group` (effectifs) **et** `home_org`/`home_group` (défauts) distinctement. `oto_whoami` montre l'org effective + `scope: home|session`.
