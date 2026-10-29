---
title: Perf event-loop — le serveur est MONO-LOOP (les 5 modes de gel)
type: explanation
---

# Perf event-loop — le serveur est MONO-LOOP (les 5 modes de gel)

> ⚠️ Ce titre a dit « les 2 modes » jusqu'au 2026-08-27, « les 3 » jusqu'au
> 2026-09-01 et « les 4 » jusqu'au 2026-09-09, et c'était vrai à chaque écriture. Les
> deux premiers modes sont des **placements** d'I/O (un handler async sans await, puis
> un middleware) ; le n°3 est d'une autre nature (le placement est correct, la requête
> est lente) ; le n°4 est un placement de nouveau, mais à un endroit qu'aucun des trois
> garde-fous ne regardait — **le seam qui monte les capacités**, donc 285 handlers d'un
> coup ; le n°5 n'est pas un placement mais une **répétition** : une valeur juste, lue
> au bon endroit, redemandée dix fois par appel parce que personne ne la retient.

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.

**PERF — un handler de tool fait du I/O bloquant ⟹ il est `def` SYNC, jamais `async def`.**
  Le serveur est **mono-event-loop** (`uvicorn.run(app)`, pas de `workers=`). FastMCP route
  un `def` sync en **threadpool** (`call_sync_fn_in_threadpool`) mais exécute un `async def`
  **dans la boucle**. Nos connecteurs appellent des libs **synchrones** (`requests` via
  france_opendata, DuckDB, clients HTTP sync) → un `async def` **sans `await`** gèle TOUTE la
  boucle le temps de l'appel (vécu 2026-06-25 : `/health` à 110 s, p95 `fr_stock_search` 218 s ;
  fix `async`→`def` sur `fr.py`/`fr_stock.py` → `/health` ~0,1 s). Règle : un handler `tools/*.py`
  qui n'`await` rien doit être `def`. Ne garder `async def` que s'il `await` réellement (httpx
  async, etc.). NE PAS ajouter de workers uvicorn (état de session streamable_http en mémoire).
  **Lot connecteurs bouclé le 2026-06-29** (361 handlers convertis ; cause re-vue = un flot
  de `serper_scrape` gelant la boucle, `/.well-known` à 1,4–10,5 s sur une box à 0,2 de load).
  **CI-enforcé** : `tests/test_no_blocking_async_handlers.py` casse si un `@mcp.tool` async
  n'`await` rien dans son **propre scope** (AST own-scope, auto-maintenu, pas de whitelist) ;
  ⚠️ **la règle vise les HANDLERS, pas tout ce qui est async** : un *callback* awaité par
  FastMCP reste légitimement async — le cas d'école était le `client_factory` de la
  fédération MCP (`mount.factory`), partie le 2026-09-09 (ADR 0069). « Pas d'await » ne
  suffit donc pas à conclure : vérifier que c'est un handler, pas un callback. Bornes connexions PG posées au
  passage (`db._connect_options` : `idle_in_transaction_session_timeout` anti-zombie-lock).
  **2ᵉ mode de gel identifié + corrigé (2026-07-02, py-spy en flagrant délit)** : du DB
  sync dans un MIDDLEWARE de la loop (`_authenticate`, gate ViewAs) × un blip de la RDB
  (SSL eof) → `pool.getconn()` attendait 30s en gelant le serveur ENTIER (2 downs).
  Protections : `ConnectionPool(timeout=5)` (`OTO_MCP_DB_POOL_TIMEOUT`) + chemin d'auth
  en `run_in_threadpool`. Observabilité posée : `loop_watch.py` (aiodebug — tout callback
  bloquant ≥1s est nommé au journal, ≥10s → event Sentry), py-spy sur la box
  (`py-spy dump --pid $(systemctl show -p MainPID --value oto-mcp)` PENDANT un gel),
  moniteur Kuma timeout 30s (timeout=0 = aveugle aux gels). RDB upgradée pico→nano.

## Le mode n°1 a lui aussi une porte dérobée : le handler qui `await`… plus bas

Le garde-fou AST `test_no_blocking_async_handlers` demande « ce handler `async def`
`await`-t-il quelque chose dans son propre scope ? ». Un handler qui fait du I/O
**synchrone d'abord** et n'`await` qu'**ensuite** répond oui — et gèle quand même la
boucle pendant tout le début.

Cas vécu, trouvé le 28/08 en instruisant le signal #491 : `web_read` (`tools/web.py`)
n'`await` que son cran ③ (le navigateur hébergé, opt-in), tout en bas ; ses crans ①
(`requests`) et ② (`SerperClient`, qui s'auto-limite par un `time.sleep`) sont
entièrement synchrones et tournaient dans la boucle. Journal de prod du 17/08 : **371
lectures, 11 au-delà de 30 s, une à 57,5 s** — autant de boucle tenue pour tous les
utilisateurs à la fois, sans que le garde-fou ait rien à redire.

**Deux leçons, et la seconde est la plus chère :**
- **« Le handler await » ne veut pas dire « le handler ne bloque pas ».** Le critère
  utile est *où* se trouve le premier await par rapport au I/O — ce qu'un AST
  own-scope ne peut pas décider. D'où, comme pour les middlewares, un test qui
  **observe le thread** plutôt que le source (`test_les_crans_bloquants_ne_tournent_pas_dans_la_boucle`).
- **Un timeout par socket n'est PAS un timeout de requête.** `_TIMEOUT = (10, 30)`
  borne chaque connexion et chaque lecture de socket, jamais la lecture entière : six
  sauts de redirection valent six fois le budget, et une boucle de streaming n'est
  bornée par rien du tout. C'est ce qui produit les 57 s. Le remède est un **budget
  global** vérifié entre les sauts ET pendant la lecture, qui rabote au passage le
  timeout de socket sur ce qu'il reste — et dont le verdict **dit ce qu'il a tenté**
  (combien de sauts, où il en était), sans quoi l'agent ne peut pas décider s'il
  réessaie.

⚠️ **À vérifier sur tout `async def` de `tools/`** dont l'await est conditionnel ou en
fin de corps : c'est exactement la forme qui passe le garde-fou.

### Même porte dérobée, côté capacités cette fois (oto-backend#867, 04/09)

Le correctif du seam (mode n°4, plus bas) range les handlers **sync** hors boucle sur
`inspect.iscoroutinefunction(cap.handler)` — un handler **async** reste dans la boucle,
À DESSEIN (34 capacités le sont réellement). Mais `connectors.identities` (`_list`) est
`async def` et appelait NÛMENT `connector_identities.list_identities(...)`, qui pour
Unipile faisait 1 (liste) + N (statut live, un par compte hébergé) appels HTTP
`requests` — synchrones, timeout de lecture **120 s par appel** (`oto/tools/unipile/
const.py`, jamais exposé au-dessus). Résultat mesuré le 04/09 : **87,8 s de gel total
de la production** (MCP + REST + sondes de veille) pour l'ouverture d'une seule page du
dashboard, et un appel ≥ 20 s CHAQUE jour où la page est ouverte depuis fin août — le
seam ne pouvait rien y voir, la porte est dans le CORPS du handler async, pas dans sa
forme.

**Le remède, backend seul (le client oto-core n'expose pas de `timeout` par appel) :**
`_call_unipile` (`oto_mcp/connectors/identities.py`) enveloppe chaque appel Unipile
synchrone en `asyncio.to_thread` (hors boucle) **et** `asyncio.wait_for` borné à **25 s**
— mesuré défendable contre les deux populations observées (2-4 s en temps normal,
19,9-23,3 s les jours lents qui répondaient quand même, 46,2-87,9 s les jours qui ont
gelé) : large marge sur le nominal, coupe ferme les pathologiques. Le thread lancé
continue jusqu'à sa vraie fin (impossible d'interrompre un `requests` en cours), mais
l'APPELANT reçoit `TimeoutError` — converti par la capacité (`_list`/`_set_default`,
`oto_mcp/capabilities/connectors/identities.py`) en `AuthzDenied(502,
"unipile_list_failed", …)`, même vocabulaire que `unipile_seats.py`. **Un Unipile lent
rend désormais une erreur nommée, plus un gel.** `_unipile_select` (jumeau exact,
`oto_identity op=set`) reçoit le même traitement.

Effet de bord assumé : la liste BYO n'avale plus silencieusement une panne Unipile en
`accounts = []` (l'ancien `except Exception` portait, à tort, le commentaire d'une
sonde de statut annexe) — c'est ICI la liste elle-même, un `[]` muet valait le défaut
d'oto#42 (un agent concluait « aucun compte » au lieu de « Unipile n'a pas répondu »).
Le fail-soft de la sonde de statut live (`_unipile_live_status_map` → `{}`/« ok »),
LUI, reste inchangé — documenté, intentionnel, hors du périmètre de ce lot.

⚠️ **Reste ouvert (lot 2, non traité ici)** : les 34 sondes de connecteur (`verify.py`),
la ligne `unipile_connect.py:244` (jumelle non protégée de `hosted_auth_link` juste à
côté), le segment Browserbase (`start_session`/`release_session`), `web.py` cran ③, et
trois callbacks OAuth + deux DCR côté REST — même classe, chemins distincts. Lot 3 (le
garde-fou qui ferme la classe entière) non plus.

### Neuf routes REST FOD, même classe (oto-backend#867 lot 2, 04/09)

`api/sirene.py` (6 routes) et `api/accords.py` (3 routes) sont des routes Starlette
`async def` qui appelaient le client FOD (`fod/http.py`, `httpx.Client` **synchrone**,
read timeout **100s partagé par tous les clients FOD** — un scan SIRENE légitime peut
en avoir besoin, donc **non modifié**) nûment. Même porte dérobée que la liste
d'identités Unipile : `async def` visible, appel bloquant plus bas, invisible à un
`grep` du fichier (le client HTTP vit dans un module partagé, pas dans `api/sirene.py`).

**Le remède, local aux deux fichiers d'API (`_fod`, calqué sur `_call_unipile`
du lot 1)** : `run_in_threadpool` (même primitive qu'`api/zoho.py:85`, le contre-exemple
qui montrait déjà la discipline) + `asyncio.wait_for`, avec **deux paliers** plutôt
qu'un délai unique — les neuf routes n'ont pas le même profil :
- **`_FOD_TIMEOUT_FICHE_S = 20`** — fiche unique (siege/siret/etablissements/info côté
  SIRENE, themes/get_one côté accords) : ne scanne jamais plus d'un enregistrement,
  doit répondre en une fraction de seconde en fonctionnement normal.
- **`_FOD_TIMEOUT_SCAN_S = 60`** — recherche paginée et surtout `headquarters` (jusqu'à
  10 000 SIREN en UN scan) : un vrai lot volumineux peut légitimement approcher les
  dizaines de secondes, une borne à 20s l'aurait cassé pour de vrai.

Au-delà du délai, `asyncio.TimeoutError` devient un `504 fod_timeout` nommé (pas de
gel, pas de 500 générique). Banc `tests/test_fod_rest_routes_hors_boucle.py` : neuf
routes, deux crans chacune (thread observé + timeout → 504), plus le même contrôle
qui mord qu'au lot 1 (neutralisation vérifiée empiriquement).

### Le rafraîchissement du jeton Google, sous douze tools (oto-backend#867 lot 2)

`gmail.py`/`drive.py`/`sheets.py`/`tasks.py`/`calendar.py`/`chat.py` (deux tools
`async def` chacun) appellent tous `_client_for_user(account)` — qui résout les
credentials Google (`auth/google.py::credentials_for`) et, si l'access token stocké
est expiré, fait un `_refresh_access_token` **synchrone** (`requests.post`, 15s)
avant de construire le client. Les appels à l'API Google, EUX, sont déjà en
`asyncio.to_thread` dans chacun des 12 tools — c'est précisément ce qui a fait
écarter ces chemins d'un premier balayage naïf ; seule la construction du client
(donc le refresh, quand il a lieu) tournait encore nûment dans la boucle.

**Remède, dupliqué dans les six fichiers** (comme `_client_for_user` lui-même
l'est déjà — même granularité, pas de module partagé nouveau) : un
`_client_for_user_async` qui enveloppe l'appel sync en `asyncio.to_thread` +
`asyncio.wait_for(20s)`. 20s et non un nouveau timeout plus court : le socket
timeout existant (15s, `auth/google.py::_refresh_access_token`) est déjà court —
le remède est d'abord « sortir de la boucle », le délai REST ne fait que ne pas
préempter l'échec naturel de la requête. Chaque fichier garde son propre
vocabulaire d'erreur (`_bad(...)` où il existe, `McpError(ErrorData(...))` sinon,
cf. `calendar.py`/`sheets.py`) plutôt que d'en introduire un nouveau.

Banc `tests/test_google_token_refresh_hors_boucle.py` : six modules × deux crans,
neutralisation vérifiée empiriquement — même méthode que les deux lots précédents.

## Le mode n°2 a une SECONDE porte : les middlewares MCP (incident du 15/08)

Même mode de gel (DB sync dans la boucle), autre famille de call-sites — et celle-là
passait sous les deux garde-fous. Sous la charge d'une campagne (~8 clients lourds :
3 workers runner, 4 agents locaux, un appariement qui écrit), 502 en rafale sur
`mcp.oto.cx` + 807 « Unexpected ASGI message after response already completed » en
20 min, **CPU calme** (load 0,31) — signature du gel de LOOP, pas de la saturation.

> ⚠️ **Un des trois indices ci-dessus a été mal attribué, et il faut le savoir avant de
> le réutiliser.** Les 807 « after response already completed » ne viennent PAS du gel :
> c'est la signature de la race terminate-vs-POST documentée plus bas (#352), qui tournait
> en parallèle et dont on ignorait alors l'existence. Le gel, lui, est bien établi par le
> py-spy ci-dessous — ça, ça tient. Ne garder de cette ligne que les 502 et le CPU calme.

py-spy sur la box, MainThread, **3 relevés sur 6** (dont ≥4 s consécutives) :

```
psycopg execute ← has_credential (credentials_store.py) ← has_member_api_key
  ← walk_cascade (access/cascade.py) ← status_for ← _resolve_context (instructions.py)
  ← _c_layers ← session_layers ← compose_session
  ← on_initialize (middleware/…)          ← LE chemin async
```

`DynamicInstructionsMiddleware.on_initialize` composait l'artefact A/C **dans la
boucle**, à CHAQUE handshake. Or `compose_session` marche `access.status_for`, donc la
cascade de résolution de **tous** les connecteurs : plusieurs requêtes par connecteur,
sur une DB distante. Un `initialize` = un gel de tout le serveur pendant la composition.
Correctif minimal : la composition (et celle du projet publié) part en
`run_in_threadpool` — les ContextVars sont propagées (anyio `copy_context`), patron déjà
en place dans le même fichier pour `_reachable_suffix`.

**Pourquoi `test_no_blocking_async_handlers` ne pouvait PAS l'attraper** — deux raisons
indépendantes, à connaître avant de croire un chemin couvert :
1. il énumère `m.list_tools()` : un middleware n'est pas un tool, il n'est jamais regardé ;
2. son critère est « contient un `await` dans son propre scope ». Un hook de middleware
   **doit** `await call_next(context)` → il passerait le critère même énuméré. Le blocage
   arrive APRÈS cet await, dans le même scope.

D'où un garde-fou de nature différente, `tests/middleware/test_no_blocking_db_in_middleware.py` :
il n'analyse pas le source, il **observe le thread**. Le seam unique d'emprunt de
connexion (`db._conn._get_pool`) est remplacé par un mouchard qui note le thread
appelant puis refuse ; le chemin est vert ssi il a **réellement** tenté d'atteindre la
base (sinon la garde est inerte — le vert ne vaudrait rien) et qu'aucune tentative ne
vient du thread de la boucle. Profondeur quelconque, aucune whitelist. Un test de
contrôle vérifie que la garde MORD (le même travail appelé nûment dans la boucle est
bien attrapé).

⚠️ **Reste à traiter, MÊME classe, non couvert** : le combinateur d'autz `ORG_MEMBER` de
`capabilities/_authz.py` appelé depuis `_rest_adapter._handler` (`current_org` sync,
relevé py-spy) — UNE requête, là où la composition en faisait des dizaines, d'où l'ordre
de traitement qui l'a laissé pour la fin.

✅ **Traité le 2026-09-09** (`DynamicInstructionsMiddleware.on_list_tools`, org + index
guide + index guides, sync) et le sink du calllog `server._calllog_sink` →
`auth.hooks.current_user_sub_from_token` → `db.upsert_user` (écriture + commit dans la
boucle). ⚠️ Ce reste-à-traiter était **sous-estimé d'un facteur cinq** : le sink n'était
qu'UN des sept demandeurs de la même identité dans le même appel. Cf. le mode n°5.

✅ **Traité le 2026-09-14** : `UserDisabledToolsMiddleware.on_initialize` →
`session_visibility.compute_hidden_layers` (`async def` qui faisait, DE PART ET D'AUTRE
de son unique `await` réel — `ctx.fastmcp.list_tools`, laissé dans la boucle,
légitimement async —, une bonne dizaine de lectures/écritures PG synchrones : toggles
perso, rôle plateforme, denylists admin/équipe, activation connecteur, RBAC org et
équipe, sélection marketplace avec son seed en écriture, option bêta). Rejoué sur un
`initialize` complet plutôt qu'un appel isolé, py-spy aurait montré le même patron que
`compose_session` le 15/08 : chaque handshake gèle le serveur entier le temps du calcul.
Remède identique au reste du mode n°2 : le corps sync est coupé en deux fonctions pures
(`_resolve_toggle_context`, `_compute_couches`), appelées via `run_in_threadpool` de part
et d'autre du seul `await` — aucune des deux ne lit ni n'écrit de ContextVar (vérifié :
`access.current_org`/`current_group` ne font que LIRE celles de `session_org.py`,
`connector_selection.seed_active` ÉCRIT en base, jamais en ContextVar). Garde-fou :
`tests/middleware/test_no_blocking_db_in_middleware.py` (nouveau test,
`test_user_disabled_tools_on_initialize_ne_touche_pas_la_base_dans_la_boucle`) — 6 accès
DB depuis le thread de la boucle avant correctif, 0 après.

✅ **Traité le 2026-09-15** : `SentryToolErrorMiddleware.on_call_tool`
(`oto_mcp/sentry_setup.py`) — pas du DB cette fois, mais le même mode : `capture_exception`
tournait nûment dans la boucle sur le chemin d'erreur de **chaque** appel de tool.
Diagnostiqué sur les timeouts urllib3/`requests` de `linkedin_aiark_person` (2 tentatives
de 30 s, `_appel_avec_reprise`) : sur 30 échecs en production, les 30 portaient un
`sentry_event_id` (donc 30 captures dans la boucle), mais seulement 5 dans la même fenêtre
ont mesuré un gel ≥1 s — le coût de la capture VARIE, il fallait le chiffrer plutôt que le
supposer. Chiffré sur la VRAIE chaîne d'exception (un socket qui pend pour de vrai,
`requests.exceptions.ReadTimeout` dont `__context__` est un authentique
`urllib3.exceptions.ReadTimeoutError` — vérifié, pas fabriqué à la main), Sentry en DSN
vide (`transport=None`, donc `Client.capture_event` construit l'event ENTIER — pile,
filtrage in-app, `linecache` pour le contexte source de chaque frame — et ne court-circuite
que la dernière ligne, l'envoi) : médiane 6-9 ms, un appel « à froid » (1ᵉʳ après boot,
`linecache` lit chaque fichier une fois) jusqu'à ~50-70 ms sur
`tests/test_sentry_capture_hors_boucle.py`. **Ce chiffre, à lui seul, n'explique pas des
gels ≥1 s** — l'ordre de grandeur est trop petit ; les gels mesurés en prod tiennent
probablement à la coïncidence avec d'autre I/O sync sur la même boucle au même instant.
Mais c'est quand même de l'I/O disque synchrone posée nûment dans la boucle, sur le
chemin d'erreur de CHAQUE tool — la règle mono-loop ne se négocie pas au chiffre moyen.

Remède : seul `sentry_sdk.capture_exception(e)` part en `run_in_threadpool` — la création
du scope et la pose des tags/user (`mcp.tool`, `mcp.client`, `user.id`) restent dans le
contexte ASYNC, avant l'entrée dans le thread. Vérifié EMPIRIQUEMENT (pas supposé) que le
scope Sentry — porté par une ContextVar interne au SDK — survit à la copie de contexte que
fait `run_in_threadpool` (anyio) : un event réel, capturé DANS un thread du pool via un
transport collecteur (aucun réseau), porte bien les tags posés dans le contexte appelant
avant l'`await`
(`tests/test_sentry_capture_hors_boucle.py::test_le_tag_pose_avant_le_thread_est_sur_levent_capture_dans_le_thread`).
`_LAST_EVENT_ID.set(...)` (la ContextVar OTO, jamais celle du SDK) reste posée dans le
contexte ASYNC, après le retour du thread — même piège que documenté pour
`session_visibility.py` la veille : une écriture faite DANS le thread ne remonterait pas.
Garde-fou rouge→vert :
`tests/test_sentry_capture_hors_boucle.py::test_capture_exception_tourne_hors_du_thread_de_la_boucle`
(même mouchard que les autres — thread observé, pas le source), plus son contrôle qui mord.

## Mode n°3 — la requête est au BON endroit, mais elle est lente (incident du 27/08)

Les deux modes ci-dessus sont des erreurs de **placement** : du I/O sync là où il ne
devait pas être. Celui-ci n'en est pas une — le handler est `def` sync, donc routé en
threadpool comme la règle l'exige — et il gèle quand même, parce qu'**une requête assez
lente gèle depuis n'importe où**. Le threadpool borne la concurrence, pas la durée : sous
charge, les threads occupés par la même requête lente refluent sur la boucle (attente de
connexion au pool, sérialisation des callbacks), et `loop_watch` nomme la boucle tenue.

**Ce que ça donnait** (prod, 08:28→08:47 le 2026-08-27) : `mcp.oto.cx` et l'hôte d'un tenant tiers
injoignables, gels de **185 s toutes les ~3 min** — donc gelé en continu —, `PoolTimeout:
couldn't get a connection after 5.00 sec` en cascade, 29 connexions en attente d'accept
sur le socket, CPU calme. Les workers runner timeoutaient à 60 s et redémarraient, ce qui
**rejouait l'appel coûteux** : la panne s'auto-entretenait.

**Pourquoi la table de discrimination du §suivant n'aide pas ici** : ce mode a
exactement la signature du n°2 — `loop_watch` parle, py-spy montre MainThread dans
`psycopg execute`. Le seul moyen de les séparer est de **lire la stack jusqu'à la
requête** et d'aller l'`EXPLAIN` :

```
wait (psycopg/connection.py) ← execute ← project_run_stats (db/usage.py)
  ← audit_project ← _project (capabilities/projects.py)   ← handler SYNC, bien placé
```

**La cause** : `_runs_from_journal` retrouve la clôture d'un run par `args->>'run_id'` —
une **expression**, qu'aucun index ne portait. Chaque run reconstruit valait donc un
parcours complet de `tool_calls` : 639 ms et 911 882 lignes filtrées l'unité, × 9 350
runs. Remède = `idx_tool_calls_run_finish_ref` (index partiel d'expression, 624 kB) :
185 s → 268 ms sur le pire projet, 639 ms → 0,05 ms sur la sonde unitaire.

**Trois leçons, dans l'ordre où elles coûtent cher :**
- **Un JSONB interrogé dans un `WHERE` est un index d'expression qui manque.** La colonne
  homonyme (`tool_calls.run_id`) existait juste à côté et donnait l'illusion de couvrir le
  chemin — elle sert l'autre LATERAL, pas celui-là.
- **Sortir le I/O de la boucle ne corrige PAS ce mode**, il le déguise : le serveur
  répondrait, la lecture resterait à 185 s. Le réflexe des modes 1 et 2 est ici un
  contresens.
- **La lenteur suit le VOLUME, donc elle arrive sans qu'on ait rien changé.** Le coût
  était proportionnel au journal entier ; la campagne pilote a porté un projet à 96 % des
  runs de la plateforme, et le seuil a été franchi un matin, sans déploiement. Une lecture
  dont le coût ne se borne pas au scope demandé est une panne à retardement — d'où le
  second correctif du même lot : les deux lectures par projet poussent leur filtre DANS le
  CTE, au lieu de filtrer le résultat d'une reconstruction déjà faite pour tout le monde.

## Mode n°4 — le SEAM des capacités : un adaptateur async, 285 handlers sync dedans (incident du 2026-09-01)

**13 minutes de production coupée**, dont **12 min 48 s** sans une seule réponse : les
connexions étaient acceptées par le noyau et jamais traitées — **376 empilées** dans la
file d'attente. Pas un crash, pas de CPU, pas de 502 : le silence.

py-spy sur le processus vivant, sans le tuer :

```
wait (psycopg/connection.py:484)              ← bloqué sur la base
datastore_ensure_key_index (oto_mcp/db/datastore.py)
set_schema (oto_mcp/datastore/schema_ops.py)
patch_schema (oto_mcp/datastore/schema_ops.py)
_patch_schema (oto_mcp/capabilities/datastore/columns.py)   ← handler SYNC… dans la boucle
```

**Le piège, et il vaut d'être su hors de ce dépôt : une simple LECTURE peut geler la
production.** Une requête d'analyse lancée à la main tournait depuis 47 minutes. Elle ne
posait aucun verrou gênant — mais `CREATE INDEX CONCURRENTLY` attend, *par conception*,
la fin de toute transaction ouverte avant lui (`WaitForOlderSnapshots`) avant sa passe de
validation. La lecture retenait donc l'index, et l'index retenait la boucle. Reproduit
sur base jetable : lecture de 20 s ⟹ la pose rend la main à 19,6 s ; à 47 min, 47 min.

⚠️ Une transaction simplement `idle in transaction` en READ COMMITTED, elle, ne retient
PAS le CIC (elle ne tient plus de snapshot) — c'est la requête qui **tourne**, ou une
transaction REPEATABLE READ, qui le retient. Utile à savoir avant d'accuser la mauvaise
session dans `pg_stat_activity`.

### Pourquoi les trois garde-fous précédents étaient aveugles

Le tool MCP n'est pas écrit à la main : il est **fabriqué** par
`capabilities/_mcp_adapter._make_tool`, et son jumeau REST par
`_rest_adapter._make_handler`. Ces deux fabriques rendent un `async def` — il leur faut
la boucle pour les 34 capacités réellement asynchrones et pour le refresh de visibilité.
FastMCP et Starlette les laissent donc **dans la boucle**… et tout ce qu'elles appelaient
nûment avec : la règle d'autz (qui marche la cascade des rôles), le handler sync, et
l'écho d'org (deux lectures de plus sur chaque appel org-scopé).

- `test_no_blocking_async_handlers` : critère « ce `async def` contient-il un `await`
  dans son propre scope ? ». Le tool fabriqué en contient — c'est la porte dérobée déjà
  documentée plus haut pour `web_read`, sauf qu'ici elle ne concerne pas un handler mais
  **le moule de tous les handlers** ;
- `middleware/test_no_blocking_db_in_middleware` : n'observe que les middlewares ;
- la règle « un handler d'I/O est `def`, jamais `async def` » : **respectée**. Les 285
  handlers de capacité SONT sync. C'est justement pour ça qu'ils gelaient : la règle
  suppose que FastMCP route les sync en threadpool, ce qu'il fait pour un `@mcp.tool`
  qu'on lui donne — pas pour un sync appelé nûment depuis un `async def`.

Deux appels avaient déjà été rapiécés un par un (`node_view._node`, `node_edit.edit`
passent par `run_in_threadpool` depuis leur propre handler) : le symptôme était donc
connu, jamais son axe. Et `docs/` nommait depuis le 15/08 le combinateur d'autz
`ORG_MEMBER` « appelé depuis `_rest_adapter._handler` » comme reste-à-traiter — c'était
le même seam, vu par un seul de ses côtés.

### Le remède, en deux temps qui ne se remplacent pas

**① Sortir le travail de la boucle, AU SEAM.** Les deux adaptateurs rangent désormais
`autz + handler sync + écho d'org` dans `run_in_threadpool`. Un seul endroit, les deux
faces, les 285 capacités — plutôt que 285 rustines dont chaque nouvelle capacité
oublierait la sienne. Un handler `async def` reste dans la boucle (un thread n'a pas de
boucle où jouer une coroutine) ; le tri se fait au **montage**
(`inspect.iscoroutinefunction`). Les ContextVars traversent : `run_in_threadpool` (anyio)
exécute sur une **copie** du contexte, donc l'axe `_org`, le `view_as` et l'empreinte
client sont lus normalement depuis le thread — vérifié. Ce qu'une copie ne rend pas,
c'est une **écriture** de ContextVar faite dans le thread ; aucune capacité n'en fait
aujourd'hui.

**② Borner le DDL à chaud.** Sortir de la boucle ne suffit pas, et c'est le contresens à
éviter : hors boucle, le même `CREATE INDEX CONCURRENTLY` aurait tenu **un thread du
threadpool pendant 47 minutes** et laissé son appelant pendu. `_conn._connect_autocommit`
pose donc `lock_timeout` (5 s) et `statement_timeout` (60 s) — l'attente derrière une
transaction plus ancienne est une attente de VERROU (`ShareLock` sur le VXID), donc
`lock_timeout` la coupe ; mesuré 5,3 s au lieu de 19,6 s, et 0 s quand rien n'est devant.

Ce qui suit une borne, c'est **ce qu'on fait du travail resté à faire**, sans quoi on a
juste déplacé le problème :

- `datastore_ensure_key_index` lève `KeyIndexUnavailable` (typée) au lieu d'un 500
  opaque, et **nettoie l'index temporaire** que le CIC coupé laisse derrière lui — un
  unique INVALIDE continue d'imposer sa contrainte aux écritures suivantes ;
- `set_schema` / le provisionnement de slot **écrivent quand même le schéma** et le
  DISENT en avertissement : le chemin d'écriture applicatif continue de rapprocher les
  lignes sur la clé, seule la garantie anti-course manque ;
- `oto-mcp maintenance key-indexes` (timer, déjà en place) la repose au tir suivant —
  il balaie précisément les namespaces à clé déclarée dont l'index manque. Il appelle
  donc `bornee=False` : un travail de FOND a le droit d'attendre son tour, et le borner
  garantirait qu'un index sur une table très occupée ne se pose **jamais**.

### Le garde-fou

`tests/test_capacites_hors_boucle.py` — il n'analyse pas le source, il **observe le
thread** (même parti que celui des middlewares). Deux crans : le **seam** (ce que les
fabriques font d'un handler sync, énoncé général valable pour les 285 sans liste à
tenir) et un **cas réel sur une vraie base** — `data_patch_schema` du registre, joué en
entier jusqu'au `CREATE INDEX CONCURRENTLY`, avec l'assertion d'inertie (« le DDL a-t-il
seulement été atteint ? ») sans laquelle le vert ne vaudrait rien. Plus un contrôle qui
mord : le même travail appelé nûment dans la boucle EST attrapé. Rejoué contre le commit
d'avant le correctif : **5 rouges sur 8**, dont le DDL montré sur `MainThread`.

⚠️ **Ce que ce lot ne couvre pas** : un handler `async def` qui ferait de l'I/O sync
avant son premier `await` (la porte dérobée du mode n°1) reste possible sur les 34
capacités asynchrones — le seam les laisse dans la boucle, à dessein.

## Mode n°5 — la même valeur redemandée dix fois par appel (mesuré le 2026-09-09)

Les quatre premiers modes disent **où** l'I/O est posée. Celui-ci ne conteste le
placement de personne : il compte **combien de fois** la même valeur est reconstruite
dans un seul appel. Une requête au bon endroit qui part dix fois pour un résultat
identique coûte autant que dix mauvais placements.

`auth.hooks.current_user_sub_from_token()` canonicalise le `sub` du jeton dès que le
drain d'alias est armé (il l'est, et il le reste — `tenant_migration.py`) : un `SELECT`
sur `sub_aliases`, puis un `INSERT … ON CONFLICT` sur `users`, donc un **COMMIT**. Deux
requêtes synchrones, dans la boucle, **sans aucune mémoire** : chaque appelant les
repaie. Or sept intermédiaires redemandent la MÊME identité dans le MÊME appel — le nom
d'outil du tenant (`middleware/alias`), le refus de compte en pause
(`middleware/account_suspended`), le contexte d'appel (`middleware/call_context`), le
filtrage per-user (`middleware/disabled_tools`), les instructions d'org
(`middleware/dynamic_instructions`), et deux fois le journal (`server._calllog_identity`
et `_calllog_sink`).

**Mesuré sur la chaîne réellement servie**, faux pool comptant chaque requête et le
thread qui la lance, un `tools/call` en régime permanent : **10 allers-retours PG
d'identité, dont 5 COMMIT, tous depuis le thread de la boucle** — sur 14 allers-retours
au total pour l'appel. Soit **71 % du SQL d'un appel d'outil pour une seule valeur**.
`py-spy` sur la production le même jour : 38 % du temps de boucle occupé, dont les deux
tiers en SQL synchrone. ⚠️ Le journal, lui, n'en déclarait que 4 à 7 % — son témoin ne
compte rien sous une seconde (`loop_watch`, seuil 1 s) : **ne jamais conclure sur
l'ampleur d'un gel à partir des warnings**.

### Le remède, et sa borne

`middleware/identity_scope.py`, enregistré **le plus externe des nôtres** (au-dessus même
de `ToolAlias`, qui est le premier à demander l'identité ; `fastmcp` pose le sien avant
tout enregistrement, et il ne lit aucune identité) : il ouvre une portée par
message et la garnit **hors boucle** (`asyncio.to_thread`) — exactement le geste que
`_calllog_sink` faisait déjà pour son insertion, deux lignes plus bas, et qui avait
manqué l'identité juste au-dessus. **Après : 2 allers-retours par `tools/call`, 0 dans
la boucle.**

⚠️ **La borne est une frontière d'identité, et c'est elle qui compte.** Un cache
d'identité mal borné servirait le compte d'un utilisateur à un autre — infiniment pire
que le gel qu'on corrige. Trois barrières indépendantes : la **clé EST l'identité** (le
`sub` brut du jeton ; demander A ne peut pas rendre B), la portée pose un dictionnaire
**neuf** par message (jamais un défaut de module, et un `set` de ContextVar n'est pas vu
d'une tâche sœur), et **hors portée il n'existe pas** (défaut `None` ⟹ le chemin d'avant
à l'octet près). Un refus (`AliasNonResolvable`, `CompteEnPause`) n'est jamais mémorisé :
il se lève pour chaque demandeur, comme avant.

⚠️ **La pré-résolution ne lève rien.** Elle est plus externe qu'`ErrorEnvelopeMiddleware` :
son exception partirait sans l'enveloppe contractuelle. Un échec la laisse donc passer,
et le chemin paresseux d'avant se rejoue là où il se rejouait.

### Le garde-fou

`tests/middleware/test_identite_une_fois_par_message.py` — même parti que les deux
autres : on **observe**, on ne lit pas le source. Deux crans (la chaîne servie, puis la
MÊME chaîne privée de sa portée, qui doit repayer les dix) et la garde d'identité à
part. Épreuve de chute jouée le 2026-09-09 : **8 mutations, 8 rouges**, dont la clé de
cache rendue constante — la forme exacte de la fuite d'identité.

### La face REST n'était pas concernée

`api/base._authenticate` fait déjà les deux mêmes requêtes **par `run_in_threadpool`**,
et un handler REST qui invoque un tool pose un `sub_override` que
`current_user_sub_from_token` rend avant de regarder quoi que ce soit. Le défaut était
MCP-seul ; le correctif aussi.

## Mode n°6 — le travail est bien placé, bien borné… et refait pour rien (oto#82, 11/09)

Les modes précédents disent **où** l'I/O est posée et **combien de fois**. Celui-ci ne
conteste ni l'un ni l'autre : le geste est au bon endroit, dans sa borne, et il ne
devrait **pas avoir lieu du tout**. Le coût n'est pas sa durée, c'est le **verrou qu'il
demande sur une table partagée**.

`set_schema` décidait de reposer l'index d'unicité de clé métier sur `if new_key:` — donc
sur **ce qui existe**, jamais sur **ce qui a changé**. L'ancien schéma était bien relu
juste au-dessus, mais il ne servait qu'au relevé d'effacement : **jamais comparé à la
clé**. Conséquence, mesurée en comparant l'identifiant de relation de `ds_bkey_<ns>`
avant et après : changer un **libellé** reconstruisait l'index (DROP + CREATE
CONCURRENTLY + RENAME), précédé d'un `GROUP BY … HAVING COUNT(*) > 1` sur **toutes** les
lignes du tableau. Coût de la seule pose relevé dans l'issue : ≈ 0,55 s pour 400 000
lignes. Et `patch_schema` recopie la clé telle quelle dans le schéma résultant : **tout**
patch passait par là.

**Ce qui rend ce gaspillage dangereux, et pas seulement cher :** les index de clé sont
partiels par `ns_id`, mais la **table ne l'est pas** — tous les tableaux vivent dans
`datastore_rows`. Un `DROP INDEX` y prend un `AccessExclusiveLock`, et une demande de
verrou exclusif **se met en file DEVANT** les lecteurs et écrivains suivants. Modifier un
libellé sur un tableau arrêtait donc le voisinage, qui n'a aucun rapport avec le geste.
Un interblocage réel en est sorti le 2026-09-05 à 23:46 UTC, sur un appel qui ne portait
qu'un booléen de tête.

**Le remède tient en une comparaison** — la clé d'hier contre celle d'aujourd'hui — avec
deux moitiés qui ne se déduisent pas l'une de l'autre :

- « inchangée » ne veut pas dire « présente » : si l'index **manque** (borne coupée au
  tir précédent), il se repose quand même ;
- le **retrait** se décide sur l'**existant**, pas sur la déclaration d'hier : un index
  qu'aucun schéma ne déclare plus imposerait son unicité à une colonne que
  `data_get_schema` ne montre pas — un refus d'écriture que personne ne peut relier à sa
  cause.

**Et le retrait n'était pas borné.** La pose l'a été après l'incident du 2026-09-01 ; le
retrait est resté sur le pool de requête, où `lock_timeout` n'existe pas et
`statement_timeout` vaut 0. Mesuré : **5,72 s d'attente derrière une simple lecture de
6 s**, borne réglée à 300 ms et ignorée. Il passe donc sur la connexion bornée, comme la
pose, et lève `KeyIndexStillEnforced` quand il renonce — sœur de `KeyIndexUnavailable` et
son contraire : là une contrainte manquait, ici une contrainte **survit** à la clé qui la
déclarait. Le schéma est déjà écrit dans les deux cas, donc les deux se **disent** en
avertissement plutôt qu'en 500, et celui du retrait **nomme la clé** qui reste imposée.

### Le garde-fou

`tests/test_pose_index_bornee.py` — bancs à vraie base qui mesurent l'**identifiant de
relation** de l'index avant et après, seule mesure qui distingue « reposé » de « laissé
en place ». Rejoué contre le corps d'avant : **un rouge**, l'index reconstruit sur une
pose de libellé. Et chaque moitié de la condition a sa propre chute, simulée en mémoire :
neutraliser « et si l'index manque » rougit le banc de l'index absent, réduire le retrait
au souvenir de la déclaration rougit celui de l'index orphelin.

### Ce que ce lot ne couvre pas

- `oto-mcp maintenance key-indexes` ne balaie que les index **MANQUANTS** des tableaux à
  clé déclarée. Un index **orphelin** — clé retirée, retrait coupé par sa borne — n'est
  repris par personne ; seule une nouvelle pose du même schéma le retire. C'est pourquoi
  l'avertissement servi dit « repose le même schéma », et ne promet pas la maintenance ;
- le nom de l'index temporaire reste **déterministe et partagé par tableau**
  (`ds_bkey_<ns>_v2`) : deux poses concurrentes qui changent **toutes les deux** la clé
  visent encore le même objet. La fenêtre est désormais étroite (il faut deux
  changements de clé simultanés sur le même tableau) mais elle existe, et rien ne
  sérialise ce chemin ;
- `DROP INDEX CONCURRENTLY` (`ShareUpdateExclusiveLock` au lieu d'`AccessExclusiveLock`)
  retirerait la cause du verrou exclusif plutôt que sa fréquence. Non pris dans ce lot :
  il laisse un index INVALIDE quand il est interrompu, donc il demande son propre
  nettoyage et sa propre preuve.

## Un 502 en rafale n'est pas forcément un gel — la 2ᵉ cause (#352, nuit du 15-16/08)

⚠️ **À lire avant de conclure « c'est encore le gel ».** Ce document a servi, du 15/08 au
16/08, à attribuer au gel mono-loop des 502 qui n'en venaient pas. Les deux causes
partagent la moitié de leur signature — 502 en rafale, CPU calme, « after response
already completed » au journal — et se distinguent nettement sur un point : **la durée
des 502**.

| | gel mono-loop (ci-dessus) | race terminate-vs-POST (#352) |
|---|---|---|
| durée des 502 | **longue** (la requête attend la boucle) | **~0,2 s** — la connexion meurt aussitôt |
| loop_watch | callbacks ≥1 s nommés au journal | **muet** (la boucle tourne) |
| py-spy pendant | MainThread dans `psycopg execute` | MainThread **idle** |
| remède | sortir le I/O de la boucle | compléter la réponse ASGI |

**La mécanique, en DEUX étages** — et le premier n'est pas celui qui casse. Un POST
`/mcp` est en vol quand la session streamable-http se termine (le DELETE du client ferme
le stream mémoire du transport) :

1. `writer.send()` lève `anyio.BrokenResourceError`. **Le SDK l'attrape et la logue**
   (`ERROR mcp.server.streamable_http: Error handling POST request`) — elle ne s'échappe
   jamais. C'est du bruit, pas la panne ;
2. son `except Exception` tente alors d'écrire un 500 **par-dessus le 202 déjà envoyé**
   (`streamable_http.py:654`). h11 a clos la réponse → `RuntimeError: Unexpected ASGI
   message 'http.response.start' sent, after response already completed`. **C'est CELLE-LÀ
   qui remonte jusqu'à uvicorn**, et rien d'autre.

Mesuré sur 8,4 h de journal (15/08) : 2744 `BrokenResourceError` loguées, 1433
« Exception in ASGI application » — **toutes** le `RuntimeError` ci-dessus. Zéro
`ExceptionGroup`, et zéro « ASGI callable returned without completing response ». Le
piège de diagnostic est là : on cherche `BrokenResourceError` en haut de pile, elle n'y
est jamais — elle n'apparaît que dans le message logué par le SDK.

uvicorn **ferme alors le transport**. Et c'est là qu'est le vrai dégât : Caddy
tenait cette connexion pour réutilisable dans son **pool keep-alive**. Elle meurt sous
lui → 502 sur elle, **et sur les requêtes voisines qui en héritent** — d'où des 502 sur
des `claim`/`extend`/`thread_append` de workers qui n'ont jamais parlé à `/mcp` (4-5 runs
tués vers 00:05 UTC le 16/08). Le 502 ne frappe pas que le client parti.

**Le remède, dans NOTRE couche** : `oto_mcp/client_disconnect_guard.py`, posé par
`server.build_root_app` en couche la plus externe (entre uvicorn et le dispatch par
Host). Il complète la réponse à la place du client parti — 202 vide si les en-têtes ne
sont pas partis, fin de corps sinon — et **n'attrape que la classe « déconnexion client »**
(`error_taxonomy._is_client_disconnect`, le même prédicat que le drop Sentry) : toute
autre exception traverse intacte, ce que `tests/test_client_disconnect_guard.py` fige.
C'est la condition pour qu'une garde qui avale une exception soit acceptable.

**Pas de fix upstream à attendre** (vérifié le 16/08) : le site fautif de
`mcp/server/streamable_http.py` est identique de 1.27.2 à 1.29.0, à `v1.x` HEAD et à
`main`/2.0.0 — aucune version publiée ne garde ce `writer.send`. La PR upstream qui le
ferait (#2983) est ouverte depuis juin, jamais mergée ; le backport du fix voisin sur la
ligne 1.x a été refusé (`not_planned`, #3142), et `v1.x` est en « security fixes only ».
**Bumper `mcp` ne rapporte pas ce fix** — et 2.0.0 est hors d'atteinte tant que
`fastmcp-slim` pin `mcp<2.0`.

### La source de la churn : c'est NOTRE workload, pas un tiers

Les 3 IP Azure de l'incident portent `User-Agent: MistralAI-MCPClient/1.0` (+ un en-tête
`X-Internal-Service` qui nomme un service interne de l'appelant) et sont
**pleinement authentifiées** — zéro 401, zéro 403 sur 16 122 requêtes en 8,4 h. C'est le
client MCP hébergé de Mistral, exécutant notre propre charge d'enrichissement (38 782
appels sur 48 h : `data_write`, `data_rows`, `data_claim_next`, `serper_*`, `fr_*`).

**⇒ Ni intrusion, ni abus : aucun rate-limit à poser.** En throttler serait throttler la
campagne. Ce qui coûte, c'est le patron du client : il ouvre une session neuve **par
tour** puis la DELETE aussitôt — **16 213 `initialize` en 48 h**, durée de vie médiane
**339 ms** (p10 237 ms, min 211 ms), ~4,3 requêtes HTTP par session. Chaque `initialize`
paie tout le handshake. Le levier est la réutilisation de session côté client, pas le
reverse proxy.

⚠️ **Angle mort d'observabilité relevé au passage** : `/etc/caddy/Caddyfile` n'a **aucune
directive `log`** → pas d'access log Caddy. Les statuts ne se lisent que dans l'access log
uvicorn (`journalctl -u oto-mcp`), qui ne remonte qu'à ~8 h (cap 183 Mo, cron d'hygiène
disque) : **la rafale de nuit n'était déjà plus lisible au matin**, seule la base l'a
gardée. Compter des 502 côté edge est aujourd'hui impossible.

Deux corrélations qui NE marchent pas, à ne pas retenter : `Mcp-Session-Id` (32 hex, id
de transport MCP) ≠ `tool_calls.session_id` (UUID 36, session applicative) — espaces
disjoints, intersection nulle ; et le `X-Request-Id` de l'Envoy client ≠
`tool_calls.request_id`. Le pont qui marche est le **profil horaire** (ratio ~4,3:1 entre
requêtes HTTP et `initialize`) plus le `clientInfo` du handshake, stocké dans
`tool_calls.args` — noter que le connecteur Mistral ne surcharge pas le clientInfo du SDK
Python, donc la base ne montre qu'un générique `client_name='mcp'` : **le nom du produit
ne se lit que dans le User-Agent HTTP**.

## Corollaire de méthode (27/08)

Le 3ᵉ mode n'est pas un I/O mal placé mais une **requête lente** (JSONB sans index
d'expression : 185 s de boucle tenue, prod + tenant tiers à terre) — même signature py-spy
que le 2ᵉ, remède opposé : indexer, pas déplacer. Corollaire de méthode : **une lecture dont
le coût suit le VOLUME TOTAL et non le scope demandé est une panne à retardement, qui se
déclenche sans déploiement.**

**Ajout du 2026-09-01 (mode n°4).** Deux corollaires de plus, et le second est celui qui
a coûté treize minutes :

- **Un garde-fou qui énumère des objets écrits à la main ne voit pas ce qui est
  fabriqué.** Les trois gardes existants regardaient des handlers, des middlewares, des
  tools — jamais le MOULE qui en produit 285. Quand un lot introduit une fabrique,
  c'est la fabrique qu'il faut garder, pas ses produits.
- **Chercher l'axe, pas le call-site.** Le correctif « rapiéce ce handler-là » avait
  déjà été appliqué deux fois (`node_view`, `node_edit`) sans que personne ne remonte
  d'un cran. Deux rustines au même endroit sont le signal qu'on répare un cas là où vit
  une classe.

## Procédures : dispatch mixte sync/async

Une console `async` peut choisir une branche synchrone : les lectures, listes et
écritures de procédures faisaient encore du SQL dans la boucle, même après le
correctif des adaptateurs. `capabilities/_execution.execute` exécute désormais la
préparation et le handler synchrone dans le même thread ; un résultat awaitable
est attendu dans la boucle de l'appel. Les deux adaptateurs et les consoles de
procédures empruntent ce seuil, sans répéter l'autorisation.

Les handlers de lecture/écriture séparent leur I/O synchrone de l'enrichissement
asynchrone du manifeste d'outils. Les ContextVars déjà posées par l'appel sont
copiées vers le thread ; les faits produits par l'autorisation reviennent dans
`ResolvedCtx`, pas par une mutation de ContextVar dans le thread.
`tests/orgs/test_instruction_execution.py` exerce les quatre opérations admin
sur les deux faces, compte une autorisation, observe le thread des stores et
celui du manifeste, et vérifie le contexte et les refus. Il ne mesure aucune
latence de production.

## Observabilité — capturer la VRAIE pile d'un blocage, pendant qu'il a lieu (`hang_watch.py`, 15/09)

`loop_watch.py` (aiodebug `log_slow_callbacks`) nomme un callback bloquant et sa durée,
mais jamais QUELLE LIGNE de code tenait la boucle — le nom rendu est générique
(`TaskHandle._run_coro()`), inutilisable pour diagnostiquer. La raison tient au patron
lui-même : `on_slow_callback` est appelé **après** que le callback a fini — la pile
intéressante s'est déjà déroulée, `sys._current_frames()` à ce moment-là ne montrerait
que la sonde elle-même.

`aiodebug` porte un second mécanisme, `hang_inspection`, jamais branché ici. Il résout
le problème par un **thread séparé** qui surveille un timestamp partagé, mis à jour par
une tâche minuscule de la boucle : si le délai dépasse le seuil, la boucle est bloquée
**en ce moment**, et c'est ce thread — qui tourne PENDANT le blocage, pas après — qui
dump la vraie pile. Adapté dans `oto_mcp/hang_watch.py`, sur le même patron mais pas la
même sortie ni le même filtre :

- **journalise** (logger `oto_mcp.loop`, comme `loop_watch.py`) plutôt qu'écrire des
  fichiers `stacktrace-*.txt` sur la box (le patron d'origine) ;
- la pile complète du **thread principal** (celui qui fait tourner la boucle asyncio),
  puis — depuis le 30/09, §« La pile de tous les fils » plus bas — une ligne compacte
  pour **tous les fils** du process, sous le même identifiant de gel ; **depuis le
  12/10, une seule entrée par gel** qui porte la pile de tous les fils, boucle en tête
  (§« Le relevé d'un gel, en une entrée » plus bas) ;
- **aucune variable locale** (cadres lus par `f_code`/`f_lineno`, jamais `f_locals` ni
  `format_exc`) — même
  famille de risque que le fix Sentry du 2026-09-15 (#564, `include_local_variables=
  False`) : un secret déchiffré qui traînerait dans une frame ne doit jamais atteindre
  un journal ;
- **débit limité** : un état d'ÉPISODE (armé tant que le délai reste dépassé, réarmé à
  la résorption) garantit UN dump par blocage continu, et un plafond de **3 dumps par
  minute toutes causes confondues** évite qu'une salve de blocages courts et rapprochés
  (nuit du 14-15/09 : ~6 en une minute, suite à une bascule) ne remplisse le journal de
  piles redondantes du même incident — au-delà du plafond, le refus se DIT
  (« dump ignoré (plafond de N/min atteint) »), jamais en silence ;
- même seuil que l'existant : `OTO_SLOW_CALLBACK_WARN` (déf. 1.0s), pas un second réglage
  à tenir à jour séparément ;
- **interrupteur dédié `OTO_HANG_WATCH_ENABLED`** (déf. **actif**) : coupe le mécanisme
  par un redémarrage du process, sans redéployer. Actif par défaut parce que le
  mécanisme EST la réponse aux gels non identifiés de ce document — le désactiver par
  défaut reviendrait à se priver de la preuve le jour où elle sert ; défendable
  seulement parce que le coût est mesuré, pas supposé (ci-dessous). Nom aligné sur le
  patron des boucles de fond (`OTO_SCHEDULER_ENABLED`…), pas sur celui des seuils.

**Câblage** : `hang_watch.run_heartbeat_loop` est déclarée comme une `Boucle` de
`boucles_de_fond.py` (`tiers=False`, armée par `hang_watch.enabled()`) plutôt que
composée à la main dans `server.main` — pas parce qu'elle drainerait du travail en
base (elle n'en draine aucun), mais parce que `server.main` ne démarre **rien** qui ne
passe par ce registre (`tests/test_boucles_de_fond.py::test_server_main_ne_compose_
rien_hors_du_module`) : ajouter une tâche de fond, c'est l'ajouter là, un seul endroit
pour lire le roster complet de ce qui tourne. `tiers=False` sans nuance : elle ne
touche jamais un tiers, donc elle tourne dans TOUS les environnements — préprod
comprise, exactement là où le prochain gel non identifié peut survenir.

**Coûts, mesurés** (`tests/test_hang_watch.py`, doctrine du projet : chiffrer, pas
supposer) :
- le geste côté boucle (`beat()`, une écriture de flottant) : **~0,1 µs/appel** —
  sans commune mesure avec un tour de boucle asyncio ;
- le réveil périodique du thread watchdog lui-même, sous une charge SYNTHÉTIQUE qui
  sature la boucle (pas un repos total) : le coût SUIT LA FRÉQUENCE DE RÉVEIL — pas
  du bruit de machine, un coût réel et attendu du mécanisme à haute fréquence.
  `tests/test_hang_watch.py::test_cout_du_thread_watchdog_sous_charge_simulee` pose
  volontairement un intervalle resserré (10 ms, donc un réveil toutes les 5 ms —
  `interval/2`) pour exagérer la fréquence et rendre le coût mesurable, très loin du
  seuil de prod. Mesuré isolément à ce réveil de 10 ms : **12,7 % de CPU pour le
  thread sur 10s, et le débit de la boucle perd 9 %** ;
- **le même coût, au seuil réel de prod** (`/proc/self/task/<tid>/stat`, demande de
  la session de déploiement le 15/09/2026, avant d'activer par défaut en prod) :
  `OTO_SLOW_CALLBACK_WARN=1.0s` (ni surchargé ni absent des `.env` vérifiés), soit un
  réveil toutes les 500 ms — 100× moins fréquent qu'au banc ci-dessus —, fenêtre de
  180s, 360 réveils du watchdog — **0,020s de CPU (utime+stime) sur 180s, soit
  0,011 % d'un cœur**. Le coût suit la fréquence de réveil : moins de réveils par
  seconde, moins de travail pour le thread, rien de mystérieux — et c'est CE
  chiffre-là, au seuil qui tourne réellement en prod, qui est sans commune mesure
  avec le coût d'un aller-retour réseau ou DB qu'un vrai handler paierait de toute
  façon (cf. le chiffrage `capture_exception` du même jour, 6-9 ms).

**Limite connue : le watchdog détecte un délai, pas une cause — la pile capturée
peut être un témoin innocent.** Une boucle SATURÉE par de nombreux petits callbacks
qui s'enchaînent sans qu'aucun ne dépasse seul le seuil retarde le battement du
timestamp exactement comme le ferait un vrai blocage long : le délai cumulé finit
par dépasser `OTO_SLOW_CALLBACK_WARN`, un dump part — mais la pile qu'il montre est
celle du callback qui tourne AU MOMENT du dump, pas la somme des callbacks qui ont
saturé la boucle avant lui. Ce callback-là n'est coupable de rien ; il était juste
de passage. Ce n'est pas hypothétique : c'est le régime probable au palier de
charge actuel de la prod (0,8-0,9 cœur), où plusieurs handlers courts peuvent
s'enchaîner sans qu'aucun ne soit individuellement lent. Le lecteur d'un prochain
dump doit se poser la question avant de désigner un coupable : la pile capturée
EST-elle la cause d'un seul long blocage, ou seulement le témoin pris dans un
embouteillage de callbacks courts qui, ensemble, ont fait dériver le battement
au-delà du seuil ?

**Preuve empirique du garde-fou** (pas un banc vert de circonstance) :
`tests/test_hang_watch.py` provoque un VRAI blocage (`time.sleep` synchrone dans la
boucle, pas une exception fabriquée) et vérifie que le dump journalisé montre la
ligne exacte du `time.sleep`, sans aucune variable locale ; qu'un fonctionnement
normal (boucle occupée mais jamais bloquée au-delà du seuil) ne déclenche jamais de
dump ; qu'un blocage continu observé à plusieurs cycles de vérification ne produit
qu'UN dump ; que deux blocages séparés par une vraie résorption en produisent bien
DEUX (le réarmement) ; et qu'une salve au-delà du plafond par minute est refusée en
le disant.

## Le convoi du GIL — quand le travail est déjà bien threadpoolé (oto-backend#980, 16/09)

Différent des cinq modes ci-dessus : ici le travail est déjà correctement sorti de la
boucle (`run_in_threadpool`), et pourtant deux requêtes concurrentes sur le même
tableau se dégradent bien au-delà du linéaire. Remonté par scout : une lecture
paginée complète du datastore (8 910 lignes, ~90 colonnes) prend **2-3 s en solo**,
mais **54 s** à deux requêtes concurrentes sur le même token — pas un simple partage
de bande passante.

Reproduit sur la box (script isolé, contre la vraie base) : la même lecture prend
3,3 s seule, et **15 à 24 s** dès qu'UN SEUL thread de calcul tourne à côté, à
l'intervalle de bascule par défaut de CPython (5 ms — `sys.getswitchinterval()`).
C'est le **convoi du GIL** : à un intervalle de 5 ms, un thread qui vient de le
céder attend en moyenne bien plus longtemps pour le reprendre que le travail réel
ne le justifierait, sous contention. Resserré à **1 ms**
(`sys.setswitchinterval(0.001)`, posé dans `server.main()`, au tout début du
démarrage — jamais au niveau module), la même lecture retombe à **8,1-8,4 s**. En
dessous de 1 ms (testé à 0,5 ms), plus aucun gain : le plancher ×2 est déjà atteint.

Lot minimal (Alexis, 16/09) : ce seul réglage. Les pistes structurelles qui
resteraient à instruire si la contention redevenait un problème au palier de charge
suivant — lecture des grosses colonnes en `data::text` plutôt qu'en objets Python
détaillés, ou un modèle multi-processus — restent sur `oto-backend#980`, pas
attaquées ici. Preuve : `tests/test_switch_interval_980.py`, qui vérifie que `main()`
pose la valeur avant tout autre sous-système (capturée à l'appel de
`logging.basicConfig`, le tout premier après le réglage) — pas une mesure de perf
rejouée à chaque run (coûteuse, contre une vraie base), juste le fait qui compte :
la valeur est posée, et posée tôt.

**Suite (30/09) : moins de reprises du GIL, plutôt qu'une reprise plus rapide.** Le
convoi coûte `nombre d'attentes de socket × intervalle`. Ligne à ligne, libpq lit
par tranches de ~10-16 Ko (son tampon ne grandit que pour un message plus gros que
lui) et psycopg rend le GIL à chaque tranche : ~260 attentes pour une page de 500
lignes de ~4 Ko. Les lectures PAGINÉES du datastore (`datastore_list_rows` avec
`limit`, `datastore_list_rows_after`, `datastore_page_with_stats`) agrègent donc la
page côté base en UN `json` (`db/datastore._page_en_un_message`) : un message long,
que libpq lit d'une traite — 3 à 7 attentes par page quand le transport le livre d'un
bloc, une par rafale quand l'expéditeur est plus lent que le lecteur (~64 Ko, 10 à 16
pour une page de ~650 Ko, vu en CI). Reproduit en local (8 900
lignes × 90 colonnes, ramassage complet par la vraie route REST, intervalle à 1 ms) :
seul, inchangé (~2,8 s) ; à côté d'un thread de calcul, **9,6-11,2 s → 6,2-6,5 s**.
Ce qui reste est du calcul (décodage, `_row_to_dict`, rendu JSON) partagé avec le
voisin, pas de l'attente. Un ramassage sans borne (`limit=None`, copie de projet) reste
ligne à ligne : décoder tout un tableau d'un bloc tiendrait le GIL d'un bout à
l'autre. ⚠️ Le rendu de la réponse (`JSONResponse`) tourne dans la boucle, mais le
sortir au thread ne gagnerait rien : `json.dumps` en C garde le GIL de bout en bout
(mesuré : un autre thread attend toute la durée du rendu). Preuve :
`tests/datastore/test_page_un_message_980.py` : une page servie est UNE requête dont
PostgreSQL rend UNE ligne. Ni une durée, ni un compte d'attentes de socket : ce compte
suit le transport et l'ordonnanceur (la lecture ligne à ligne y est passée de 75 à 3
attentes sur un cœur partagé avec la base), il a fait rougir la CI à correctif en place.

## Mode n°1, encore : le handler `async` qui lit la base « juste avant » (`me.agent_context`, 21/09)

Coupure de prod d'environ **140 s** (relayée par oto cd). Cause trouvée dans
`capabilities/agent_context.py` : `_agent_context` est un `async def` et appelait
`_instructions.session_layers(...)` **nûment** — `_resolve_context` y fait du SQL
psycopg synchrone. `_execution.execute` ne met en thread que le handler **sync** ; pour un
`async def`, appeler le handler dans le thread ne fait que *construire la coroutine*, dont
le corps tourne ensuite sur la boucle. Le SQL tenait donc tout le process, MCP et REST.

**Correctif** : `await run_in_threadpool(_instructions.session_layers, sub, org_id)` — la
même discipline que `compute_hidden_layers` et `_get_guide`, qu'`_agent_context` appelle
juste à côté et qui, eux, étaient déjà protégés (d'où le trou : deux appels protégés
et un troisième oublié dans la même fonction).

**Preuve** : `tests/test_agent_context_hors_boucle.py` observe la boucle plutôt que le
source — pendant une lecture qui dort 0,5 s, une tâche incrémente un compteur toutes les
10 ms. Avant le correctif : **0 battement** ; après : ≥ 20. (Rejoué sur le clone avec
`PYTHONPATH=<clone>` : sans lui, l'*editable install* sert l'arbre partagé et le test
« passe » ou « échoue » sur le mauvais code — cf. `docs/commands.md`.)

**Balayage** (script AST jetable, 21/09) : les `async def` de `capabilities/`, `api/`,
`tools/` qui appellent, hors `run_in_threadpool`/`to_thread`, un module SQL (`db`,
`credentials_store`, `org_store`, `group_store`, `access`…). **71 appels directs dans 30
handlers.** Écartés à la main : 4 faux positifs (`access.current_user_sub_or_raise`, pure)
et les appels `access.current_project` / `account_noun` (purs eux aussi) ; `access.current_org`
et `current_group`, eux, descendent à la base (repli `get_active_org`, `is_member`). Corrigés
ici (une instruction chacun, patron identique) : `tools_me` `_list`/`_detail`,
`unipile_seats._list_seats`, `connectors/connect._connect`. Le reste est listé dans la PR
et **reste ouvert** — c'est le trou que ce document annonçait pour le mode n°1.

⚠️ **Ce que le balayage dit de la garde manquante** : un test qui rougit dès qu'un `async
def` appelle `db.*`/`credentials_store.*`/`org_store.*`/`group_store.*` hors threadpool est
faisable avec très peu de faux positifs **si `access.*` en est exclu** (mélange de pur et de
SQL — c'est là que naissent les faux positifs). Mais le stock existant est de ~26 handlers :
il faudrait un cliquet (liste de dettes qui ne peut que se réduire), pas un rouge sec.
La transitivité (`session_layers` → `_resolve_context` → SQL, exactement notre cas) n'est
pas décidable par nom sans graphe d'appels : un garde purement direct **n'aurait pas
attrapé ce gel-ci**.

## La garde d'exécution : `_connect()` sait s'il est appelé depuis la boucle (21/09)

Le gel du 21/09 (`me.agent_context`) est passé sous quatre garde-fous parce qu'aucun ne
regardait **où** s'exécute un accès base : l'AST « async sans await » ne voit pas un handler
qui `await` ailleurs, le seam `execute()` ne juge que le handler, `loop_watch` nomme après
coup. Le point de passage obligé de toute requête est `db._conn._connect()` (le pool n'est
ouvert nulle part ailleurs) : `oto_mcp/db/_hors_boucle.py` y pose la seule question qui
compte — **y a-t-il une boucle asyncio qui tourne dans CE thread ?** Un thread du
threadpool n'en a pas (`get_running_loop` lève), un `async def` en a une : le chemin
indirect (assistant sync appelé par un handler async, comme `session_layers`) est attrapé
sans rien savoir du code appelant. `_connect_autocommit` (DDL à chaud) est gardé pareil.

- **Site** = la coroutine `oto_mcp` la plus interne de la pile (`module::nom`, `co_name`) : c'est
  elle qu'il faut décharger, quel que soit l'assistant qui touche la base. Une coroutine de
  test n'est pas un site ; un thread sans boucle (démarrage, `init_db`, timers, scripts) non plus.
- **Production** : un `logger.warning` par site et par process, `db.hors_boucle site=… ; pile :
  …`, jamais une exception (un site en défaut est une lenteur, la lever en ferait une panne).
  Aucune variable d'environnement, aucun schéma. **Aucune exception ne sort de la garde** :
  un défaut interne est journalisé une fois (`db.hors_boucle.defaut`, avec sa pile) et l'accès
  base se poursuit — l'observateur ne casse jamais le chemin observé.
- **Tests** : `HorsBoucle` est levée pour tout site hors du **stock gelé**
  (`tests/_stock_db_hors_boucle.py`, posé par `tests/conftest.py`). La suite a très peu de base
  réelle : la levée rattrape ce qu'un banc à PostgreSQL ferait passer par un chemin indirect.

**Le cliquet statique** (`tests/_appels_db_hors_boucle.py` + `test_db_hors_boucle.py`) est
la moitié qui couvre la suite sans base : un graphe d'appels par nom / module importé /
`self.` / fermeture locale (les façades à ré-export plat `db` et `org_store` comprises)
liste tout `async def` qui atteint `_connect` par des appels **synchrones**. Il coupe aux
`await`, à `run_in_threadpool(f, …)` / `to_thread(f, …)` (`f` est passée, pas appelée) et
aux `lambda`. Le test échoue dans les DEUX sens : un site nouveau (il faut le décharger, pas
l'ajouter au stock), et un site du stock qui n'est plus fautif (il faut retirer sa ligne, sinon
le stock tolérerait sa régression). Ce qu'il ne voit pas — répartition dynamique, `getattr`,
callbacks — est le travail de la garde d'exécution ; ni l'un ni l'autre ne suffit seul.

**Ce que la mesure a trouvé de plus que les 71 appels directs du 21/09** : le balayage
direct s'arrêtait aux modules nommés `db.*`. En suivant les assistants, il y a **82 sites**
au moment de la pose — les façades `org_store` et `access` (`roles.*`, `current_org`,
`current_group`, `resolve_credential`…) cachent la base derrière un nom neutre, et 25 sites
(tous les middlewares MCP, les axes `_org`/`_project`, plusieurs outils) n'y arrivent que par
`current_user_sub_from_token`, qui ne lit la base **que tant que le drain d'alias est armé**
(`[dormant]` dans le stock : un interrupteur, pas un gel d'aujourd'hui).

### Lot 1 de décharge (21/09) : les routes sans jeton d'abord, puis les deux handlers les plus lourds

Demande d'oto cd : ce qui est atteignable **sans authentification** passe en premier, parce
que c'est ce qu'un tiers peut marteler. **82 → 62 sites** :

- `api/public.py` — toutes les routes publiques qui lisent la base (pages de partage
  `public_doc`/`public_doc_view`, désinscriptions `outreach_unsubscribe`/`digest_unsubscribe`,
  vitrines `guide(s)_library_public[_get]`, `invite_preview`, `connectors_catalog` — dont la
  branche anonyme). Les corps redeviennent des `def` synchrones sous un décorateur
  `api.base.en_thread` : l'objet reste un `async def` pour Starlette, pour `route.endpoint is …`
  et pour les bancs qui font `asyncio.run(route(req))`.
- le dispatch par Host, à CHAQUE requête : `subdomain_project.HostDispatch._http` et
  `subdomain_org.SubdomainOrgMiddleware` passent par `resolve_project_async` /
  `org_id_for_host_async`, qui ne paient le saut de thread que s'il y a de la base à lire (un
  host canonique n'a pas de slug ; un slug d'org en cache est servi de la mémoire).
- `_tls_check` (Caddy `ask`), l'annuaire public des projets MCP, la métadonnée de ressource
  protégée (`prm`, `valid_org_audience` seulement quand les deux crans gratuits n'ont pas
  tranché), le retour OAuth Salesforce (`callback` + `persist_token`, qui était un `async def`
  sans `await`).
- `runner.triggers` : le SQL de toutes les opérations (~40 appels) part au threadpool **en un
  bloc** (`_triggers_sync`) ; seuls les avertissements d'outils, asynchrones, restent dans la boucle.
- `me.credential.set` : lecture (`_set_preparer`) et écriture (`_set_ecrire`) au threadpool,
  la sonde du connecteur, asynchrone, entre les deux.
- `unipile_seats._list_seats` : `_platform_client()` (lecture du coffre) — le reliquat du
  correctif du matin, que la garde a trouvé.

- `oto_connector` (`connectors/console._connector`) : `list`/`select`/`pause`/`unselect`/
  `recommend` au threadpool. **Trouvé par la garde d'exécution en CI, pas par le balayage** :
  l'alias local `sel = connectors_selection` cachait les appels (le balayage suit désormais
  ces alias) — c'est la preuve que les deux détecteurs se recouvrent sans se remplacer.

Preuve : `tests/test_lot1_sql_hors_boucle.py` — compteur de boucle pendant une lecture de
0,5 s, **0 battement avant, ≥ 20 après**, sur neuf cas. **Reste 62 sites** dans
`tests/_stock_db_hors_boucle.py` (dont 25 `[dormant]`) ; les plus exposés qui restent :
`_IatGatedVerifier.verify_token` (audience d'un endpoint de projet), les outils
`tools/meta` (`oto_call`, `oto_list_my_tools`…), `api/media`, `api/projects`.

### 22/09/2026 — la garde tue la prod en 3.10, la CI ne le voit pas (revert de #1039)

La première version (#1039, v1.325.0) lisait `code.co_qualname` — **Python 3.11+** — alors que
la prod tourne en **3.10** (`requires-python = ">=3.10"`). `AttributeError` à *chaque* accès base
depuis la boucle : 186 en ~3 min, jusqu'au revert. Pire : `access.resolve_field_filter` passe par
`_connect()`, et `redaction.redact_payload` retombait alors en passe-through pour tout service
sans défaut serveur (aucune exposition constatée, mais un contournement de la rédaction ;
corrigé à part, #1045 : la résolution en échec retient désormais la sortie). **La CI (3.12) n'avait rien vu** : `syntaxe-plancher` compile en
3.10 mais n'*exécute* rien, et `co_qualname` est un accès d'attribut, pas de la syntaxe.

Trois corrections : la clé de site est `module::co_name` (portable ; `configurer` ramène les
clés du stock, écrites en `module::qualname` par le balayage, à cette forme) ; la garde
enveloppe son propre code et ne lève plus que la violation, en mode test ; et un job CI
**`La garde s'exécute sous le plancher Python`** (3.10) exécute l'import complet de l'arbre et
les bancs de la garde. Un banc statique (`test_la_garde_n_emploie_aucune_api_posterieure_a_python_3_10`)
nomme les API 3.11+ dans le module, et une trame factice sans `co_qualname` prouve que `_site`
n'en demande pas.

### 23/09/2026 — une violation avalée n'échappe plus à la suite ; le journal d'appels sort de la boucle

La levée de `HorsBoucle` ne suffisait pas : un `except Exception` fail-soft sur le chemin
l'avalait (`subdomain_org._resolve_slug` rend `None`, le journal d'appels laisse la ligne
anonyme) et le banc restait vert. Chaque violation est désormais aussi **notée** par la garde,
et `tests/conftest.py` appelle `_hors_boucle.exiger_aucune_violation()` en fin de session : une
violation notée fait échouer la suite en nommant ses sites, quoi qu'en ait fait l'appelant.
Sous xdist, chaque worker juge les siennes. Les bancs de la garde, qui provoquent des
violations exprès, travaillent sur un relevé isolé.

Premier fruit : `ToolCallLogger.on_call_tool` et `on_initialize` résolvaient l'identité de la
ligne (`server._calllog_identity` → `current_user_sub_from_token`) en synchrone, donc dans la
boucle — sous drain d'alias, deux requêtes PG dès que la portée d'identité du message est vide
(pré-résolution en échec, refus non mémorisé). Le banc qui l'exerce,
`test_la_meme_chaine_privee_de_sa_portee_repaie_les_dix`, arme le drain par `monkeypatch` et
retire la portée exprès : ce n'est pas une fuite d'état, et la violation sortait sur le seul
worker xdist qui le jouait. Le contrat d'`identity` est maintenant **asynchrone**, et
`_calllog_identity` passe par `run_in_threadpool` (la portée voyage avec le contexte copié). Le
journal ne casse toujours pas le service sur un échec d'identité, mais re-lève `HorsBoucle`.
Preuve : `tests/test_calllog_identite_hors_boucle.py`, `test_db_hors_boucle.py` (relevé).

## 30/09/2026 — des gels d'une seconde en continu : la recherche, et un fichier qui revenait à chaque tour

Constat d'infra sur la couleur active : **126 gels ≥ 1 s en 11 h** (7 à 12 par heure, jour et
nuit), tous juste au-dessus du seuil du `hang_watch` (« sans battement depuis 1,2-1,3 s »).
Réparti par dernier cadre `oto_mcp` de la pile du thread principal :

| cadres | lecture | verdict |
|---|---|---|
| 76 × `selectors.select` / `accept` | la boucle attend le GIL, un autre thread le tient | **témoin** : cause hors du thread principal |
| 17 × `db/search.py` `_run` ← `search_docs_fts` ← `search.search` ← `me.search` | FTS **synchrone dans la boucle** | **cause**, corrigée ici |
| 6 × `mcp_accueil.__call__` (`await self.app(...)`), 4 × `HostDispatch._http` (`return await self.authed(...)`) | ligne de passage ASGI : ce qui tourne est plus bas, hors `oto_mcp` | témoin (CPU de bibliothèque ou pause), pas d'E/S à nous |
| 4 × `api/base._authenticate`, 2 × `server._verify_api_token` | la ligne est `await run_in_threadpool(db.verify_…)` : déjà hors boucle | témoin : la boucle rendait la main au pool quand le GIL lui a manqué |

**La recherche.** `me.search` est `async def` (pour l'embedding de la requête) et appelait
`search_mod.search` nûment — la FTS des pages, des briefs, des fichiers, les tableaux, les
libellés de projet, tout en SQL synchrone. Le commentaire la disait « indexée, courte » ; la
prod l'a prise 17 fois dans la boucle en 11 h. Correctif : le SQL d'avant
l'embedding (visibilité du projet, catalogue de connecteurs) et la recherche elle-même partent
au threadpool (`capabilities/search.py`), le site sort du stock gelé. Preuve :
`tests/test_recherche_hors_boucle.py` (0 battement avant, ≥ 20 après, sur une FTS qui dort 0,5 s).

**Le fichier qui revenait à chaque tour.** `file_extract_worker` journalisait à chaque tour
« fichier #… ignoré : PostgreSQL text fields cannot contain NUL (0x00) bytes » pour les
**cinq mêmes fichiers** : pypdf rend un `\x00` pour un glyphe codé 0, psycopg refuse
l'écriture (`DataError`), aucune ligne n'est posée — et l'absence de ligne EST la file. Les
cinq, les plus récents, occupaient tout le lot (`_BATCH = 5`, tri par date décroissante) :
retéléchargés et réextraits toutes les 30 s, **et plus rien derrière eux n'était extrait**.
Deux gestes : le NUL est purgé par `file_extract.extract` (un artefact, pas une donnée) ; et
tout fichier dont le traitement lève **écrit** son échec avec sa raison — `unstorable`
(terminal) quand la base refuse la valeur (`DataError`, texte non encodable), `failed`
(reprenable, borné par `attempts`) sinon. Preuve : `tests/test_file_extract_worker.py` (file
tenue comme PostgreSQL la sert, deux tours), `tests/test_project_file_texts.py` (vraie base).

**L'extraction tient-elle le GIL ?** Mesuré sur douze PDF réels (1 à 17 Mo, jusqu'à 102
pages, 1,2-1,8 s d'extraction), extraction dans un thread, intervalle de bascule à 1 ms
comme en prod : la boucle voisine prend **au pire 23 ms** de retard. pypdf est du Python pur,
il rend le GIL à chaque bascule. Le worker réparé cesse un travail inutile, mais **cette
mesure ne l'accuse pas des 76 gels « select »** : il faut un code C qui garde le GIL une
seconde d'un bloc (rendu JSON d'une grosse réponse dans un thread, validation pydantic-core,
décodage d'un gros résultat) ou une collecte `gc` de génération 2 sur un gros tas — cette
dernière colle aussi aux cadres « de passage » du tableau, qui sont des sites d'allocation du
chemin de chaque requête. Seul un relevé natif le tranche (`py-spy record --native --gil`
pendant les gels). ⚠️ Le `hang_watch` a besoin du GIL pour vérifier : sous famine, il ne
dumpe qu'**après** que le fautif l'a rendu, d'où le thread principal encore dans `select`.

### La pile de tous les fils (30/09, suite)

Relevé `py-spy` d'infra sur 15 min de prod : aucun fil ne tient le GIL au détriment de la
boucle (MainThread à 78 % des échantillons GIL, aucun autre au-dessus de 2 %), pas de `gc`,
pas de vol de CPU — mais **un seul gel** dans la fenêtre : la cause des gels « select »
reste sans nom. Le `hang_watch` apporte donc lui-même les deux preuves qui manquaient.

**Ce qu'il journalisait** (format remplacé le 12/10 par une entrée unique, § suivant ; la
lecture du retard et du CPU ci-dessous vaut toujours). Au seuil (`OTO_SLOW_CALLBACK_WARN`, 1 s), sous un identifiant de
gel commun (`gel=<6 hex>`, repris par « dump ignoré » et « débloquée après ») :

1. la pile complète du thread principal, comme avant ;
2. **une ligne** `gel=… fils — chien en retard de R s, CPU du process C s sur les W s depuis
   son réveil précédent — MainThread(boucle)[…] | <fil>[…] | au repos N : …`, qui porte :
   - pour chaque fil actif, son **nom** (`threading.enumerate()`) et ses **5 cadres les plus
     profonds** (`sys._current_frames()`, `chemin:ligne fonction`, du plus profond au plus
     haut) — ni variable locale ni texte source (pas de `linecache`) ;
   - les fils **au repos** (cadre le plus profond dans `selectors.select`, `threading.wait`
     — donc `Event.wait`, `Queue.get` des pools anyio et psycopg —, `queue.get`,
     `concurrent.futures` `_worker`) regroupés en fin de ligne par nom et point d'attente ;
   - **le retard du chien** `R` : il devait se réveiller `interval/2` après le réveil
     précédent, tout ce qui dépasse, il l'a passé sans GIL (ou le process était arrêté) ;
   - **le CPU du process** `C` (`time.process_time()`, tous fils) sur la même fenêtre `W`.

Le chien a lui-même besoin du GIL. D'où une **seconde branche de déclenchement** : un chien
en retard de plus que le seuil rapporte un gel **même si la boucle a battu entre-temps**
(« event loop et chien à l'arrêt pendant … s, la boucle a repris la première ») — seule la
ligne des fils part, la pile de la boucle ne montrerait que son travail ordinaire. Sans
elle, le gel dépend d'une course : à la libération du GIL, si la boucle le reprend avant le
chien, elle bat, et le délai de battement ne voit plus rien.

**Mesuré** (poste de dev, Python 3.13, intervalle de bascule 1 ms comme en prod, seuil
1 s, boucle au repos qui bat toutes les 0,5 s) :

| scénario | avant (battement seul) | après | ce que dit la ligne |
|---|---|---|---|
| un fil tient le GIL dans du C 3,7 s (`re`, retour arrière exponentiel) | **1 gel vu sur 4** | **8 sur 8** (3 relevés, 5 « dump ignoré » au plafond de 3/min) | boucle dans `select`, le fil nommé dans sa fonction (sur la ligne du C ou juste après) ; chien en retard ≈ 3,2-3,4 s ; **CPU ≈ 3,8 s sur 3,7-3,9 s** |
| le process entier suspendu 2 s (`SIGSTOP`/`SIGCONT`) | **0 sur 1** | **3 sur 3** | boucle dans `select`, aucun autre fil actif ; chien en retard 1,5 s ; **CPU 0,00 s sur 2,01 s** |

**Lire une ligne.**

| retard du chien | CPU / fenêtre | lecture |
|---|---|---|
| ≈ le gel | ≈ 1 cœur | un fil tenait le GIL en calculant dans du C : chercher le fil **actif** de la ligne, hors boucle — il est dans la fonction qui a appelé le C, ou déjà un peu plus loin |
| ≈ le gel | ≈ 0 | **personne ne calculait** : process suspendu, étranglé (quota CPU du cgroup), en swap, ou VM en pause — rien dans notre code |
| ≈ 0 | ≈ 1 cœur | la boucle calcule elle-même en Python (le chien a eu le GIL par la bascule) : la pile principale est le coupable |
| ≈ 0 | ≈ 0 | la boucle attend un appel bloquant qui a rendu le GIL (E/S synchrone, verrou) : la pile principale est le coupable ; un fil qui tient ce verrou apparaît dans la ligne |

**Ce qu'elle ne dit pas.**

- **Qui tenait le GIL, mais seulement qui est encore là.** Le relevé est pris quand le
  chien reprend le GIL, donc **après** que le fautif l'a rendu : un fil qui a fini sa tâche
  et est retourné attendre dans son pool est résumé « au repos », sans trace. La ligne nomme
  le fautif s'il est encore dans sa fonction (le cas mesuré), pas autrement ; dans ce cas,
  `CPU ≈ fenêtre` sans fil actif reste la signature d'un calcul en C hors boucle.
- **Pas le code C lui-même** : le cadre Python le plus profond est l'appelant
  (`json.dumps`, `re.match`, une méthode pydantic-core…), jamais l'intérieur — c'est le
  rôle de `py-spy --native`.
- **Pas une cause unique quand la boucle est saturée** : la limite du témoin innocent
  (§ Observabilité) vaut aussi pour la pile principale.
- Le CPU est celui **du process**, tous fils confondus : un fil de fond qui calcule en
  Python pendant qu'un autre tient le GIL ne se distingue pas — seuls ses cadres le font.
- Au plafond (3 relevés/min), les gels suivants ne sont que **comptés** (« dump ignoré »),
  avec leur `gel=` mais sans ligne de fils.

Coût : rien de plus sur la boucle (`beat()` inchangé) ; le chien lit en plus
`time.process_time()` à chaque réveil (sans effet mesurable sur
`test_cout_isole_du_thread_watchdog_par_reveil`, qui plafonne à la résolution du jiffy),
et le relevé des fils ne tourne qu'au dump. Preuves : `tests/test_hang_watch.py` —
un fil qui tient un verrou que la boucle attend donne **une ligne qui nomme les deux
fils** sous le même `gel=`, sans le chien ni une variable locale ; un fil qui tient le GIL
dans `re` est nommé quel que soit le gagnant de la course ; la branche « la boucle a repris
la première » rejouée sans course.


### Le relevé d'un gel, en une entrée (12/10/2026, #1108)

Après le 30/09, il restait **~39 gels ≥ 1 s par jour, au plus 3,6 s, en grappes** — et le
journal de prod ne permettait pas de les attribuer. Deux raisons de forme, pas de fond :

- le journal de la box est **journald**, qui range **chaque ligne de stderr à part** : la
  pile principale (une ligne d'en-tête puis des `File "…", line …` sans identifiant) se
  dispersait en entrées anonymes, et un `grep gel=` ne rendait que les en-têtes ;
- la ligne des fils ne gardait que **5 cadres** par fil et **taisait la pile des fils au
  repos** — or un fil qui vient de rendre le GIL est souvent déjà retourné attendre.

Décision : au gel, **la pile de tous les fils**, en **une** entrée. `hang_watch.
releve_des_fils`, appelé par le chien (son fil à lui, jamais la boucle), écrit un seul
enregistrement `WARNING` du logger `oto_mcp.loop` :

```
event loop sans battement depuis 0.3s (gel=186772) — chien en retard de 0.00s, CPU du process 0.13s sur les 0.13s depuis son réveil précédent — 3 fils relevés, cadres du plus profond au plus haut
gel=186772 » « MainThread » ident=126459718453120 tid=2493557 démon=non état=S cpu=0.59s BOUCLE appel C
gel=186772     tests/test_hang_watch.py:105 _bloque_avec_une_variable_secrete
gel=186772     tests/test_hang_watch.py:638 test_releve_boucle_bloquee_et_fil_actif
gel=186772     events.py:88 _run
gel=186772     base_events.py:1999 _run_once
gel=186772     …
gel=186772 » « fil-qui-calcule » ident=126459601483456 tid=2493631 démon=oui état=R cpu=0.30s en Python
gel=186772     tests/test_hang_watch.py:611 _calcule_en_python
gel=186772     threading.py:1012 run
gel=186772     threading.py:1075 _bootstrap_inner
gel=186772     threading.py:1032 _bootstrap
gel=186772 » « fil-qui-dort-en-c » ident=126459593090752 tid=2493632 démon=non état=R cpu=0.00s appel C
gel=186772     tests/test_hang_watch.py:617 _dort_en_c
gel=186772     …
gel=186772 fin du relevé : 3 fils
```

(relevé réel de `tests/test_hang_watch.py::test_releve_boucle_bloquee_et_fil_actif`, seuil
de test 0,25 s, la boucle bloquée par un `time.sleep`, élagué. Un groupe au repos s'écrit
`gel=… » ×37 au repos (threading.wait), appel C : « AnyIO worker thread » ident=… ; …`, suivi
de ses trois cadres d'attente.)

- **L'en-tête** garde le libellé d'avant (« sans battement depuis », ou « à l'arrêt
  pendant … la boucle a repris la première ») : un comptage des gels par ce libellé reste
  juste. La durée est celle **au moment du relevé** ; la durée totale part toujours à la
  résorption (« débloquée après X s (gel=…) »).
- **Un bloc par fil** : `» « nom » ident=<threading> tid=<noyau, celui de py-spy et de
  /proc> démon=oui|non`, son **état** ordonnanceur (`R` en cours ou prêt, `S` endormi, `D`
  E/S disque) et son **CPU cumulé depuis sa naissance** (`/proc/self/task/<tid>/stat`,
  Linux), puis ses **20 derniers cadres**, `chemin:ligne fonction`, **du plus profond au
  plus haut**. Le fil de la boucle est en tête, marqué `BOUCLE`. Ni variable locale ni
  texte source (pas de `linecache`, aucune lecture disque pendant que le chien tient le
  GIL).
- **`appel C` / `en Python`** : l'instruction où s'est arrêté le cadre le plus profond du
  fil (`f_lasti`). Sur un appel (`CALL…`, entrée d'un `with`), le fil est **dans du code
  natif** : un appel bloquant qui a rendu le GIL (`sleep`, `acquire`, `select`, E/S) ou un
  calcul C qui **vient** de le rendre. Ailleurs, il s'est arrêté **entre deux
  instructions Python** : il calculait en Python et a cédé le GIL à la bascule — il
  le **veut**.
- **Les fils au repos** (cadre le plus profond dans `selectors.select`, `threading.wait`,
  `queue.get`, `concurrent.futures` `_worker`) sont **regroupés par pile identique**
  (`×N`), leurs 3 cadres d'attente une fois, chaque membre nommé avec son ident ; ils
  passent après les fils actifs (en Python d'abord, puis en C).
- **Chaque ligne après la première porte `gel=<id>`** : un `grep` rend l'entrée entière,
  dans l'ordre, même découpée par journald.
- **Plafond** : 32 000 caractères par relevé (`_PLAFOND_RELEVE` ; mesuré : 61 fils, dont 20
  actifs à 20 cadres et un pool de 40 au repos, font ~20 Ko). Au-delà, les derniers blocs tombent (les groupes au repos les
  premiers) et la dernière ligne le **dit** : `… relevé tronqué au plafond de 32000
  caractères : N fil(s) non montré(s)`. Sinon elle dit `fin du relevé : N fils` — une
  entrée sans l'une ou l'autre est une entrée coupée en route. Le plafond borne aussi le fil
  d'Ariane que la LoggingIntegration de Sentry tire de ce `WARNING` (le chien n'envoie
  aucun événement Sentry à lui).
- **Un relevé par gel** : l'épisode (un relevé tant que le battement manque, réarmé à la
  résorption) et le plafond de 3 relevés/min sont inchangés ; au-delà, « dump ignoré ».

**Ce qu'il ne sait toujours pas dire : qui TENAIT le GIL.** Le chien est un fil Python : il
ne relève qu'une fois le GIL repris, donc **après** que le fautif l'a rendu, et aucune API
sans dépendance ne nomme le détenteur. Ce qu'on en déduit :

| relevé | lecture |
|---|---|
| retard du chien ≈ gel, CPU du process ≈ fenêtre, un fil actif hors boucle en `appel C` | ce fil sort d'un calcul C qui gardait le GIL (`json.dumps`, `re`, pydantic-core…) : son cadre le plus profond est l'appelant |
| idem, un fil actif `en Python` | ce fil calculait en Python et monopolisait la bascule (rare : il rend le GIL toutes les 1 à 5 ms) |
| idem, aucun fil actif hors boucle | le fautif est déjà retourné attendre : regarder les groupes au repos et leur `cpu=` ; sinon un `gc` de génération 2 (aucun cadre à lui) |
| CPU du process ≈ 0 | personne ne calculait : process suspendu, cgroup étranglé, swap (cf. table du 30/09) |
| `BOUCLE` sur un appel C hors `select`, retard du chien ≈ 0 | la boucle elle-même est dans un appel bloquant : son cadre le plus profond est le coupable |

Le `cpu=` est **cumulé depuis la naissance du fil**, pas mesuré sur le gel : le mesurer sur
la fenêtre demanderait une lecture par fil à chaque réveil du chien, un coût hors gel
refusé. Il départage tout de même un fil de pool qui a beaucoup calculé de ses voisins
identiques, et deux relevés successifs d'un même `ident` donnent l'écart.

**Coût.** Hors gel : **nul** — `beat()` et le réveil du chien (`_check`) sont inchangés,
rien n'est lu tant que le battement est à l'heure. Pendant un gel : le relevé tourne dans
le fil du chien, une fois ; mesuré à **~2 ms de CPU** (médiane sur 30, 4,3 ms au pire)
pour 61 fils dont 20 à 30 cadres de profondeur, relevé de 19,6 Ko. Le chien rend le GIL à
la bascule comme tout fil Python : le relevé ne peut retarder la boucle que de ce qu'il
calcule, jamais du gel lui-même.

**Extraire les relevés en prod** (lecture seule, unités colorées — pas `oto-mcp`) :

```bash
# les gels de la journée, un par ligne (en-têtes seulement)
journalctl -u 'oto-mcp@*' --since today -o cat --no-pager \
  | grep -E 'sans battement depuis|à l.arrêt pendant|dump ignoré|débloquée après'
# un relevé entier, par son identifiant
journalctl -u 'oto-mcp@*' --since today -o cat --no-pager | grep -F 'gel=186772'
# tous les relevés de la journée dans un fichier, pour les lire hors de la box
journalctl -u 'oto-mcp@*' --since today -o short-iso --no-pager | grep -F 'gel=' > releves.txt
```

Le journal de la box ne remonte qu'à quelques heures (plafond de taille) : une semaine
d'observation, c'est une extraction par jour au moins.

Preuves : `tests/test_hang_watch.py` — la boucle bloquée par un `time.sleep` donne **une**
entrée qui porte la boucle (sur la ligne du sleep, `appel C`), un fil qui calcule
(`en Python`) et un fil qui dort en C, chacun avec ident, tid, démon, état, CPU ; un gel de
huit seuils ne donne qu'un relevé et une résorption sous le même `gel=` ; quarante fils
profonds tiennent sous le plafond, la troncature dite et la boucle gardée ; cinq fils qui
attendent au même endroit tiennent en un bloc qui les nomme tous.

### Sentry : le routage refait à chaque requête — essayé, retiré (01/10/2026)

L'intégration Starlette de sentry-sdk refait la correspondance de route à chaque requête
REST, dans la boucle (~140 µs en médiane sur ~800 routes, relevé py-spy du 30/09).
`transaction_style="endpoint"` l'évite jusqu'à sentry-sdk 2.68 ; **à partir de 2.69,
l'intégration rebalaie la table quel que soit le style** (pour poser `http.route`), et la
dépendance n'est pas plafonnée (décision du 01/10 : pas de plafond). Le réglage a donc été
retiré. Pour s'en débarrasser un jour : retirer l'intégration Starlette et ne poser que
`SentryAsgiMiddleware` (perte : capture des `HTTPException` 5xx gérées, infos de requête).
Le rendu JSON de `api/base._json` reste dans la boucle : le sortir au thread ne rend pas la
main à la boucle (`json.dumps` en C garde le GIL ; ~10 ms par Mo mesurés).


## Le pool rend ce qu'il n'utilise plus ; le hors-pool a un plafond (#1141, 04/10/2026)

La base managée partagée a basculé six fois dans la matinée du 04/10 faute de mémoire :
chaque connexion PostgreSQL, même inactive, en tient. Deux bornes manquaient côté
processus.

- **`max_idle`** (`OTO_MCP_DB_POOL_MAX_IDLE`, 60 s) : une connexion inactive au-delà de
  `min_size` est rendue. Le défaut de psycopg_pool (600 s) gardait ouvert ce qu'un pic
  avait ouvert, tant qu'une tâche de fond la réveillait. `0` rend la borne de psycopg_pool.
- **Plafond des connexions hors pool** (`OTO_MCP_DB_HORS_POOL_MAX`, 2 par processus) :
  `_connect_autocommit` prend une place avant d'ouvrir. Le chemin servi (`bornee=True`)
  attend au plus `OTO_MCP_DB_POOL_TIMEOUT` puis lève `HorsPoolSature`, que le datastore
  traduit en ses refus d'index déjà nommés (`KeyIndexUnavailable`,
  `KeyIndexStillEnforced`) — le schéma est écrit, la maintenance reprend le geste. Le
  travail de fond (`bornee=False`) attend son tour.

⚠️ **Une connexion tenue pendant un appel réseau** : le seul cas du code est la
réservation d'échéance (`db/billing_reservation.py`, verrou consultatif de session), et
elle n'a plus d'appelant servi (le prélèvement `billing_runner._charge_one` est sans
appelant, la boucle refuse chaque échéance). Elle passe désormais par le plafond hors
pool ; sa réécriture n'a pas d'objet tant que ce chemin reste mort.

La protection côté REST d'une route qui tient le pool trop souvent est la garde des
routes lourdes (`docs/rest-api.md` §Routes lourdes). Preuves :
`tests/db/test_pool_hors_pool_1141.py`.

## Une lecture par ligne, un emprunt par lecture (#1148, 05/10/2026)

Deux routes de page restaient lentes après #1145 — `GET /api/me/connectors` (médiane
3,0 s, p95 15,7 s) et `POST /api/me/runner/triggers` (3,6 s, 11,9 s), les 03-04/10. Ni
réseau sortant ni verrou sur leur chemin : des centaines de lectures unitaires, chacune
sur son propre emprunt au pool (`BEGIN`, requête, `COMMIT` : trois allers-retours), et
chaque emprunt attend son tour quand le pool est disputé — le nombre d'emprunts
multiplie la contention, d'où la queue.

- **`connectors.me`** (~160 lectures : sélection, coffre, cascade, options, apps
  OAuth) passe sous `db.reuse_connection()` comme `access.status_for` : UN emprunt par
  appel (176 → 1, mesuré en local). Condition de l'enveloppe, gardée par un banc : le
  chemin ne fait que lire. L'option payante se juge une fois par (option, porteur du
  credential) au lieu d'une par canal.
- **`runner.triggers op=list`** : le catalogue d'outils des avertissements
  (`_avec_tool_warnings`) se calcule une fois par org et par réponse, plus une fois par
  ligne (~13 lectures et ~12 ms de BOUCLE pour ~870 outils, par ligne) ; pertes,
  livraisons et file d'un webhook se lisent en une requête par mesure pour toute la
  liste (`db.comptages_perimes`, `comptages_livraisons`, `files_des_declencheurs`).
  20 agents dont 10 webhooks sur 100 000 travaux : 302 emprunts et 551 ms → 18 et 43 ms.
  ⚠️ La file d'un webhook (`status IN ('pending','held')`) n'a pas d'index pour `held` :
  elle parcourt `runner_jobs`, désormais une fois par liste au lieu d'une par webhook.

Preuves : `tests/test_connecteurs_me_une_connexion_1148.py`,
`tests/test_declencheurs_liste_une_passe_1148.py`.

### Une lecture par fait et par appel (#1148, 09/10/2026)

Une connexion par appel ne suffisait pas : `GET /api/me/connectors` restait à 1,75 s de
médiane (p95 4,41 s, 87 appels sur trois jours). Le profil, sur un jeu réaliste (trois
orgs, trois équipes, des clés à tous les paliers, des prêts, Google et Microsoft liés,
réseau coupé : aucun appel sortant) : **~200 requêtes par appel**, dont 157 dans
`access.status_for`. Chaque module du chemin relit ce dont il a besoin sans savoir qu'un
voisin vient de le lire — l'org active 28 fois, l'équipe active 14, les comptes Google
une fois par service Google, les réglages des connecteurs une fois par service
Microsoft, le rôle et la pause du prêteur à chaque passage. À 3,14 ms l'aller-retour
vers la base managée, ~0,77 s de requêtes à elles seules.

- **`db.lecture_d_appel`** (`db/lecture_d_appel.py`) : une lecture PURE de la base,
  décorée, ne se fait qu'une fois par arguments dans une portée `db.reuse_connection()`.
  Sa mémoire est portée par l'emprunt de la portée et meurt avec lui — l'invalidation
  est la fin de l'appel ; la portée ne fait que lire (la condition de
  `reuse_connection`, gardée par un banc), donc aucune écriture de l'appel ne la rend
  périmée ; chaque appelant reçoit sa copie profonde. Une transaction explicite
  (`conn=`) lit toujours la base. Décorées : `get_active_org`, `get_org_role`,
  `get_active_group`, `get_group`, `list_groups_for_user`, `get_user`,
  `get_suspension`, `list_connector_settings`, `list_shared_with`, `list_accounts`,
  `list_credentials`, `has_credential`, `list_member_orgs_for`.
- **Sessions navigateur** dans `status_for` : lues dans l'inventaire de chaque palier
  (`credentials_store.statuts_mono_compte`, la lecture déjà faite par la sonde) au lieu
  d'une requête par connecteur et par palier.

Mesuré sur le jeu, aller-retour simulé à 3,14 ms, médiane de 20 appels : liste verbeuse
199 requêtes et 773 ms → 44 et 180 ms ; `status_for` (le chemin commun de `/api/me`,
`oto_connector op=list` et `oto_list_my_tools`) 159 et 617 ms → 25 et 102 ms. Réponses
identiques octet pour octet. Preuves : `tests/test_connecteurs_me_rapide_1148.py`.

## Le convoi du GIL, encore : douze fils sur le même compte (#1108, 20/10/2026)

Une semaine de relevés (la pile de tous les fils, ci-dessus) : 850 gels de 1 à 2,2 s, la
boucle au repos dans `select`, le processus à 1,5 cœur. Dans 83 % d'entre eux, **8 à 14
fils du pool** exécutent la MÊME chaîne : le sondage de l'ordonnanceur
(`runner_jobs` → `db/runner_fleets.campagne_a_servir` → `_ordre_de_service.ordonner` →
`_lignes_reservables.lignes_reservables` → `tableau_vise`), puis le contrôle d'accès
(`ownership.visible_in_org` → `owner_of` → `_datastore_owner`), qui relisait la ligne
entière du tableau et décodait son `schema` jsonb. Rien n'est dans la boucle ; c'est le
convoi : douze fils qui calculent la même chose se disputent le GIL, et la boucle attend
son tour.

Lot au plus petit (Alexis, 20/10) :
- **un seul calcul par campagne** — le cache de 15 s n'avait pas de verrou : à
  l'échéance, chaque fil recalculait. Un verrou de processus par campagne (`threading`,
  les appels venant des fils du pool) ; le cache est relu une fois le verrou pris, les
  autres fils repartent avec le compte du premier. TTL, échec gardé (`_ECHEC`) et
  signature inchangés. Les verrous se prennent dans l'ordre des identifiants (deux
  sondages aux listes croisées ne s'attendent pas en cercle) ;
- **l'appartenance sans le schéma** — `_datastore_owner` lit `db.get_datastore_owner`
  (`owner_type`, `owner_id`), plus `get_datastore_by_id`.

Critère d'observation après la prod : plus aucun relevé de gel où 8 fils ou plus portent
cette chaîne. Preuves : `tests/test_lignes_reservables_un_seul_calcul_1108.py` (douze fils
ratent le cache ensemble, un seul calcul — douze sans le verrou), et
`tests/test_ownership.py` (l'appartenance ne lit ni ne sélectionne le schéma).

## Avant le handler : une écriture par requête, deux authentifications par appel (29/10/2026)

Une requête REST empruntait la base 8 à 11 fois avant son handler, sur un pool de 24
connexions, dont des ÉCRITURES. Deux causes, sur le chemin de toutes les requêtes :

- **`upsert_user` réécrivait la ligne `users` à chaque requête authentifiée** (REST,
  JWT) et à chaque message MCP : `INSERT … ON CONFLICT DO UPDATE SET …, updated_at =
  NOW()`, donc un verrou de ligne et un COMMIT, même à valeurs égales — les appels
  parallèles d'un même compte se sérialisaient sur sa ligne. Il n'écrit plus que si
  `(email, name, locale)` change (`IS DISTINCT FROM`), en UNE requête à deux CTE
  (l'UPDATE conditionnel, l'INSERT si la ligne manque) ; sinon c'est une lecture pure.
  ⚠️ Pas `ON CONFLICT DO UPDATE … WHERE` : PostgreSQL y verrouille la ligne en conflit
  même quand la condition est fausse. ⚠️ `users.updated_at` date désormais le dernier
  changement de la ligne, plus la dernière requête : aucun lecteur ne la lisait comme
  activité (l'activité se lit dans `tool_calls` — relance, entonnoir, adoption) ; son
  seul lecteur, la liste d'admin `GET /api/admin/users`, la sert telle quelle. Les
  gardes de naissance (chaîne d'alias vers un compte en pause, tenant désactivé) sont
  sous le même gate `inserted`, inchangées.
- **`ViewAsMiddleware` authentifiait, puis le handler ré-authentifiait** dès qu'un
  en-tête de consultation (`X-Oto-Org`, `X-Oto-Group`, `X-Oto-View-As`) était posé —
  c'est-à-dire sur presque chaque appel du tableau de bord. Le premier `_authenticate`
  dépose le porteur dans le `scope` (`api.base.CLE_PORTEUR`), après toutes ses gardes ;
  le second le reprend à options IDENTIQUES seulement, repose les variables de contexte
  (portée du jeton, verrou d'org) depuis cette mémoire, et n'applique que la vue. Un
  refus ne se mémorise pas.

Preuves : `tests/test_upsert_user_n_ecrit_que_ce_qui_change_live.py` (version de ligne
inchangée sans changement, appel qui aboutit pendant qu'une autre transaction tient la
ligne) et `tests/api/test_une_authentification_par_requete.py` (une vérification, une
garde, un upsert par requête ; pause, appartenance, options plus strictes et verrou de
délégation éprouvés à l'envers).
