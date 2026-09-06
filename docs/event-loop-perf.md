# Perf event-loop — le serveur est MONO-LOOP (les 4 modes de gel)

> ⚠️ Ce titre a dit « les 2 modes » jusqu'au 2026-08-27 puis « les 3 » jusqu'au
> 2026-09-01, et c'était vrai à chaque écriture. Les deux premiers modes sont des
> **placements** d'I/O (un handler async sans await, puis un middleware) ; le n°3 est
> d'une autre nature (le placement est correct, la requête est lente) ; le n°4 est un
> placement de nouveau, mais à un endroit qu'aucun des trois garde-fous ne regardait —
> **le seam qui monte les capacités**, donc 285 handlers d'un coup.

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
  un `client_factory` awaité par FastMCP (`mount.factory`) reste async — « pas d'await » ne
  suffit pas, vérifier que c'est un handler, pas un callback. Bornes connexions PG posées au
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

⚠️ **Restent à traiter, MÊME classe, non couverts** (nommés plutôt que balayés en pleine
nuit d'incident) : `DynamicInstructionsMiddleware.on_list_tools` (org + index guide +
index guides, sync), `UserDisabledToolsMiddleware.on_initialize` →
`session_visibility.compute_hidden_tools` (`async def` qui fait 3 requêtes sync avant son
premier await), le combinateur d'autz `ORG_MEMBER` de `capabilities/_authz.py` appelé
depuis `_rest_adapter._handler` (`current_org` sync, relevé py-spy), et le sink du
calllog `server._calllog_sink` → `auth.hooks.current_user_sub_from_token` →
`db.upsert_user` (écriture + commit dans la boucle). Chacun est UNE requête ou trois,
là où la composition en faisait des dizaines — d'où l'ordre de traitement.

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
