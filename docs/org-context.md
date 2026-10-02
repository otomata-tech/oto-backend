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
- **Org maison** (`org_store.get_active_org`, ex-« active_org ») — défaut persistant des **nouvelles** conversations. Posée explicitement : `oto_set_home_org` (MCP) ou `PUT /api/me/active-org` (REST/dashboard) ; **jamais** par navigation dashboard. Les seules écritures implicites sont **conditionnelles**, et la règle est écrite UNE fois, dans `add_org_member` : elle pose la maison à qui n'en a aucune, ou promeut l'espace perso silencieux vers une org réelle (ADR 0030/0033) — **jamais par-dessus une maison réelle établie**. **Accepter une invitation n'en est plus une** (oto#161, 10/09/2026) : `_accept_invitation_row` appelait `set_active_org` sans condition, y compris **sans aucun clic** par `reconcile_signup_with_invitation` (simple correspondance d'email vérifié). Mesuré en prod le 10/09 : 7 personnes déplacées d'une org réelle vers une autre, et 8 dont les clés membre — rangées sous `{maison}:{sub}`, **jamais migrées** quand la maison change — sont devenues injoignables par défaut (« non configuré », sans que rien ne dise que la clé existe ailleurs). Le palier équipe suit la même règle : `set_active_group` écrit lui aussi `org_members.is_active` (invariant ADR 0012), il n'est donc appelé à l'acceptation que si la maison est déjà l'org du groupe.
- **Org de consultation** (REST, view-as) — header `X-Oto-Org` (équipe : `X-Oto-Group`), posé par le **middleware ASGI `api.routes.ViewAsMiddleware`** (brut, n'altère pas le streaming `/mcp`) APRÈS **validation d'appartenance** (anti-IDOR : `roles.is_org_member`/`can_read_group`) dans un contextvar lu par `current_org`. Le dashboard consulte n'importe quelle org **sans muter l'identité MCP** — mais « consultation » = **org de TRAVAIL de l'onglet, lecture ET écriture** (poser une clé, éditer les settings y atterrissent), gatée par le rôle réel dans l'org ciblée. **Exception : l'inspection par un opérateur plateforme** (`admin` ou `super_admin`) **sans rôle RÉEL dans l'org** — l'org consultée, ou l'org parente de l'équipe consultée — est en **LECTURE SEULE** (mutations → 403 `view_as_read_only`, lectures op-aware permises : `POST {op}` passe si l'op est dans `api.routes._READ_OPS` — liste par NOM, commune à toutes les routes, où chaque op de chaque capacité op-aware est classée lecture ou écriture par `tests/test_readonly_op_guard.py`, qui rougit sur une op neuve non classée ; `fleets op=state` y manquait, oto#221). C'est la règle même d'`active_org_readonly` servi par `/api/me` : l'écran et le middleware ne doivent jamais diverger, y compris pour un super_admin qui passe `can_read_group` par escalade (inspection ≠ escalade). L'autre mode read-only est le view-as USER ci-dessous.
- **« Voir en tant que » (axe USER, REST, lecture seule)** — header `X-Oto-View-As=<sub>` posé par le même `ViewAsMiddleware`, gaté **opérateur plateforme + cible existe + lecture** (GET, ou ops de lecture `_READ_OPS` comme ci-dessus ; mutations → 403 `view_as_read_only`). `_authenticate` renvoie alors le **sub cible** (param `apply_view_as`, contextvar `session_org.current_view_user`) → tout `/api/me/*` (capacités incluses) rend la vue de la cible. Comme la cible a ses rôles réels, `active_org_readonly` y vaut faux : c'est **`view_as_read_only`**, servi par `/api/me` (vrai ssi la vue est APPLIQUÉE, après les gardes ; oto#212), qui annonce la lecture seule de ce mode — un front calcule son droit d'écrire sur les deux flags. **REST-only** : le MCP ne lit jamais ce contextvar (zéro impersonation dans Claude). Front : bouton sur la fiche admin + bandeau `ViewAsBanner` (`lib/viewOrg.ts`).
- **Écrire en « voir en tant que » : seulement après un geste d'acceptation** (24/09/2026) — une mutation portant `X-Oto-View-As` passe ssi elle porte AUSSI `X-Oto-View-As-Write: 1` **et** que l'opérateur est **super_admin** ; sans l'en-tête, 403 `view_as_read_only` inchangé ; avec l'en-tête mais le seul rôle `admin` (supervision), 403 `view_as_write_forbidden`. L'écriture s'exécute sous l'identité de la **cible**, qui doit être membre de l'org (et de l'équipe) consultée — sinon 403 `forbidden` (une lecture y reste permise en inspection). L'opérateur n'est jamais effacé : le contextvar `session_org.current_view_as_operator` le porte pour la requête (une clé perso posée ainsi garde `set_by` = la cible et `meta.set_by_operator` = l'opérateur), et le journal REST écrit `sub` = l'opérateur, `view_as_sub` = la cible, `args.view_as_write = true`, sous l'org où l'écriture a agi (celle de la consultation, sinon l'org de contexte de la cible). L'org de la cible les lit : `GET /api/orgs/{id}/monitoring/view-as` (org admin, capacité `org.monitoring.view_as_writes`) — qui, en tant que qui, quelle route, quand, jamais un argument ni un secret (le journal REST n'écrit pas le corps). `/api/me.view_as_read_only` suit l'acceptation : vrai en vue appliquée, **faux quand l'écriture est acceptée** — même jugement que celui qui laisse passer l'écriture (`ViewAsMiddleware` publie `session_org.view_as_write_accepted` sur toute requête, lecture comprise ; en-tête absent, autre que `1`, ou opérateur non super_admin → vrai). Le front relit `/api/me` quand il pose ou retire l'en-tête (oto#212). Le MCP reste hors de ce mode.
- **« Voir en tant que » ouvert à l'org_admin, BORNÉ à son org** (oto#270) — même `ViewAsMiddleware`, même garde de lecture seule, même journal (`tool_calls.view_as_sub`), même `view_as_read_only` servi par `/api/me`. Un non-opérateur qui pose `X-Oto-View-As` ouvre une vue **bornée à l'org O** si : O est donné (`X-Oto-Org`, ou l'org de `X-Oto-Group` — sinon 400 `view_as_org_required`), il en est **admin RÉEL** (colonne, sinon 403 `forbidden` : un org_member est refusé), la cible en est **membre RÉEL** et n'est pas opérateur plateforme (sinon 403 `view_as_hors_org`), l'équipe consultée est lisible par la cible, ni sous-domaine d'une autre org ni `X-Oto-Run` (qui placeraient la requête hors de O). **Jamais d'écriture** : avec `X-Oto-View-As-Write: 1`, 403 `view_as_write_forbidden` (l'écriture acceptée reste au super_admin). **Liste fermée de lectures** (`api.routes._LECTURES_VUE_BORNEE`, plus toute lecture `GET /api/orgs/{id}/…` et `/api/groups/{id}/…` **épinglée sur O**) : tout le reste répond 403 `view_as_hors_org`, jamais servi « au cas où » — refusées nommément, parce que « compte entier » : jetons API, grants de comptes connecteurs, tableaux « partagés avec moi », abonnements de modèles, instances de connecteurs (portent les clés membre des autres orgs), facturation, légal, bibliothèques, `/api/resources`, alias `/api/datastore/namespaces/*`, admin ; et `POST /api/me/projects op=runs` sans projet (runs ouverts toutes orgs). Dans la requête, `session_org.current_view_as_bound_org()` = O, et **tout le bornage se lit là, jamais ailleurs** : (1) l'adaptateur REST des capacités refuse (`view_as_hors_org`) toute org résolue par la règle d'autz ≠ O — en-tête, chemin, champ d'entrée ou projet visé ; (2) le seam `ownership` : `visible_in_org` n'a d'autre contexte que O, une ressource **perso** du membre n'y est visible que si elle **descend dans O** (son kind la range dans une org — `context_org`, ou celle de son projet pour une page — et c'est O ; un kind qui ne range pas, tableau ou procédure perso, suit la personne partout, O compris), un partage **personnel** reçu ne compte pas ; `can_access` = `visible_in_org(O)` ∩ la règle ordinaire ; `accessor_scope` ne connaît que O et ses équipes ; `borner_a_la_vue` / `partages_dans_la_vue` appliquent la même règle aux listes qui n'y passaient pas (tableaux accordés, recherche, partages du rail et des nœuds, pages reçues, projets reçus) ; (3) `/api/me/orgs` ne rend que O, `/api/me` masque `home_org`/`home_group` hors de O. Une ressource hors de O visée par id reçoit le refus ordinaire de sa route pour « hors de portée » (404 pour un projet, un tableau ou un run), sans jamais nommer l'org où elle vit. Ce que voit l'org_admin : la vue d'org et d'équipe du membre dans O, et la part de son palier user qui descend dans O (guides et readme perso, procédures et fonctions perso, statut de ses clés et OAuth dans O — jamais un secret —, projets perso rangés dans O, tableaux perso — ouvrables par leur identifiant ; depuis le 28/09/2026 aucune LISTE d'une org qui n'est pas l'org perso du membre ne les rend, `docs/ownership.md`). **Hors vue bornée, chaque fonction suit son chemin d'avant à l'identique** (`tests/api/test_vue_bornee_org_admin.py`, témoin contre PostgreSQL ; gardes du middleware : `tests/api/test_vue_bornee_middleware.py`).
- **Ce que `/api/me` dit de la vue bornée, pour que le front ne devine rien** (oto#270 suite) —
  trois champs, TOUJOURS présents (jamais omis) :
  - **`view_as_bound_org`** — l'org O quand CETTE requête est une vue bornée (posée par
    `ViewAsMiddleware` via `session_org.current_view_as_bound_org()`), `null` sinon —
    **y compris en vue d'OPÉRATEUR plateforme**, qui ne borne rien (`view_as_read_only`
    peut y valoir `true` alors que `view_as_bound_org` reste `null` : les deux
    mécanismes ne se confondent pas). C'est la SEULE source ; un front qui déduirait la
    vue bornée d'un état tenu localement (l'en-tête qu'il vient d'envoyer, par exemple)
    mentirait dès qu'une garde du middleware refuse la vue en amont.
  - **`view_as_refused_prefixes`** — en vue bornée SEULEMENT, les préfixes REST refusés
    (`403 view_as_hors_org`) DANS cette vue ; `null` hors vue bornée. **Dérivé, jamais
    recopié** (`api.routes.refused_prefixes_vue_bornee`, qui rejoue `_lecture_vue_bornee`
    — la fonction que le middleware applique réellement — sur les gabarits `GET` du
    registre de capacités) : un changement de `_LECTURES_VUE_BORNEE`, ou une capacité
    REST neuve, change ce que ce champ rend sans qu'aucun front n'ait à tenir sa propre
    copie de la règle. Portée volontairement limitée aux lectures `GET` — une lecture
    POST « op-aware » (`{"op":"list"}`…) hors liste blanche est elle aussi refusée par
    le middleware mais n'entre pas dans ce champ (7 gabarits sur ~200 dans
    `_LECTURES_VUE_BORNEE`, l'exception documentée plutôt que la règle). Le préfixe
    rendu est le chemin tronqué AVANT son premier `{paramètre}`, **slash final gardé**
    (`/api/connectors/`, pas `/api/connectors`) — sinon il couvrirait par erreur une
    route EXACTE homonyme et whitelistée (`/api/connectors`, catalogue public). `/api/orgs/{id}/…`
    et `/api/groups/{id}/…` sont exclus d'office (épinglés sur O, jamais refusés en bloc).
    Gardes anti-dérive dans les deux sens : `tests/api/test_vue_bornee_middleware.py
    ::test_refused_prefixes_couvre_toute_lecture_refusee` (une route refusée non
    couverte fait rougir le test) et `::test_refused_prefixes_ne_couvre_aucune_lecture_ouverte`.
  - **`OrgMemberEntry.is_platform_operator`** (`GET /api/orgs/{id}`, capacité `org.get`) —
    même prédicat qu'utilise `_juger_vue_bornee` pour refuser une cible opératrice
    (`access.is_operator_role`, équivalent d'`access.is_platform_operator` sur un rôle
    déjà en main), calculé en **une seule requête pour toute la liste**
    (`org_store.list_org_members`) — **zéro requête ajoutée par membre** : le rôle
    plateforme est DÉJÀ dans la ligne `users` que `_members` lit pour email/name/avatar,
    ce champ la réutilise plutôt que de refetcher. Le front s'en sert pour ne pas
    proposer « voir en tant que » sur un membre que la vue refuserait. **Confidentialité :
    servi au seul org_admin de cette org** (et à un opérateur plateforme, déjà au
    courant) — `null` pour un membre ordinaire, dont la fiche org n'a pas à annoncer le
    statut plateforme d'un tiers.

## L'org du run (30/08/2026, #639 — amende ADR 0023 §3 et ADR 0038 §A)

**Sans axe `_org=`, un appel fait DANS un run se résout dans l'org du run
(`runs.org_id`), pas dans l'org maison du sub.** Mesuré en prod le 29/08 (#631/#638) :
un `data_write` sans `_org`, dans un run ouvert sur une org, résolu dans l'org maison
du sub et refusé « datastore inconnu » sur un tableau que la réservation du même
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

## Jeton de délégation : l'org de son travail, et elle seule

Un travail du runner agit au nom de son porteur avec un jeton `kind="delegation"`
(`capabilities/runner_jobs._delegue`). Ce jeton porte désormais son travail et l'org de
ce travail (`user_api_tokens.job_id`, `verrou_org`, `verrou_org_id`, révision 0032), et
toute résolution du porteur se borne à cette org (`oto_mcp/verrou_org.py`) :

- `roles.effective_org_role` — donc `is_org_member`, `is_org_admin` et
  `ownership.can_access` — ne rend aucun rôle hors de l'org du travail, escalade
  plateforme comprise ; le rôle d'équipe direct est gardé de même ;
- `session_org.set_call_org` / `set_call_run_org`, où aboutit tout jeton d'appel qui pose
  une org (`_org`, `_project`, `_group`, `_instance`, l'org d'un run), refusent une autre
  org avec le code `org_out_of_job` — jamais une résolution silencieuse ailleurs ;
- `access.current_org` rend l'org du travail avant tout le reste, jamais la maison du
  porteur ; `ownership.accessor_scope` ne compte que les partages reçus dans cette org ;
- un travail sans org n'a accès à aucune org (portée personnelle seule).

Le verrou ne vise que le porteur : une lecture sur un tiers garde son chemin. Il est posé
pour la requête par le bord — l'authentification REST et `middleware/verrou_org.py` côté
MCP — depuis la ligne du jeton. `OTO_VERROU_ORG_DELEGATION` : `enforce` (défaut),
`report` (journalise et laisse passer), `off`. Un jeton émis avant la révision n'est pas
verrouillé jusqu'à la fin de son bail (quelques minutes). Hors périmètre : la portée
personnelle du porteur (ses ressources et comptes personnels) et les outils hors de la
liste d'outils du déclencheur.
