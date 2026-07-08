# oto-mcp

MCP server (Streamable HTTP) qui expose les connecteurs **oto-core** (`oto.tools`,
importés directement — **plus aucune dép à la CLI**) comme tools, branchable dans
claude.ai et Claude Code. Public **prod** = `https://mcp.oto.cx/mcp` (box Scaleway
dédiée ; `mcp.oto.ninja` = **preprod** depuis le cutover ADR 0040 — cf. §Auth « CUTOVER »).

**Positionnement : oto-mcp = le produit central, déployable** (SaaS hébergé OU
on-premise pour un client — image `Dockerfile`, config 100% par env). oto-cli =
façade locale basse priorité (fallback LinkedIn browser). Tout open source.

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=3.4.2` (plancher = dernier ; prod aligné au deploy via `pip install -e .`) + `mcp` SDK
- **`oto-core[browser]` PINNÉ sur un tag git** (`@ git+…@vX.Y.Z` dans `pyproject.toml`, plus `@main` flottant ni dép `oto-cli`) : une version déployée = coordonnée reproductible. ⚠️ **`pip` ne réinstalle PAS une dép VCS déjà présente** (`oto-core` "satisfait" quelle que soit sa version) → `pip install -e .` seul ne monte JAMAIS oto-core au tag bumpé. Le deploy **force-réinstalle** oto-core depuis le tag lu du `pyproject` (`pip install --force-reinstall …@$tag`). Bump connecteurs = tag oto-core + édit du pin + deploy (PAS de `git pull` box). Cf. ADR 0020. (⚠️ box `otomata-0` a un VIEUX oto-mcp décommissionné/stoppé avec un editable legacy `oto-cli` pré-split — ne pas s'y fier, le runtime live est la box dédiée.) ⚠️ **Le pin est un champ que TOUTES les sessions // éditent → régressions silencieuses récurrentes** : vécu 2026-07-07, un commit concurrent a réécrit le pin `v1.18.0→v1.17.0` et **cassé un tool déployé SANS erreur** (le tool était enregistré, sa méthode absente de l'ancien oto-core → `AttributeError` seulement à l'appel). Toujours bumper en **superset** (tag haut ⊇ tags bas) ; à la moindre divergence de pin en merge/rebase, **garder la version haute**.
- `psycopg[binary]` + `psycopg-pool` (PostgreSQL managed Scaleway `otomata-main`, DB `oto_mcp`) pour le state par utilisateur — migré depuis SQLite le 2026-05-20. Row factory custom dans `db/_conn.py` (`_str_dict_row`) qui normalise `datetime`/`date` → strings "YYYY-MM-DD HH:MM:SS" : sinon `JSONResponse` crash sur `/api/me` car le code historique attend des strings comme avec SQLite. ⚠️ **Les rows sont des DICTS (accès par nom de colonne `r["col"]`), JAMAIS positionnel `r[0]`** (→ `KeyError: 0`). Vécu 2026-06-25 : deux fonctions RBAC en `r[0]` plantaient à chaque appel, **masqué** par leur fail-open + des tests qui stubbaient ces fonctions → bug invisible jusqu'à un seed réel. Leçon : un **fail-open silencieux + des tests stubbés cachent un bug de forme de row** ; exercer le vrai chemin (cf. [[feedback_verify_empirically]]).
- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)

## Architecture

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, _SERVER_INSTRUCTIONS, routes /api, tools
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── api_routes.py     # /api/me, /api/settings/*, /api/admin/* (CORS oto.ninja)
├── access.py         # rôles member/admin, resolve_api_key, quotas, status_for
├── db/               # store PG (package) : _conn (pool/connexion), _schema (DDL), _init (migrations) + 1 module/domaine (users, keys, usage, datastore, projects, opendata…). Surface plate `db.<fn>` via __init__

├── auth_hooks.py     # current_user_sub_from_token() pour le contexte tool
└── config.py         # require_env

deploy/
├── oto-mcp.service       # systemd, User=root, /opt/oto-mcp, port 9103
├── Caddyfile.snippet     # mcp.oto.ninja → 9103 (pas de bearer-gate, masquerait WWW-Authenticate)
└── DEPLOY.md             # procédure DNS + Caddy + systemd + Claude.ai

```

L'extension Chrome (Oto Companion) vit dans `oto-app/extension/` (repo
`otomata-tech/oto-app`, monorepo des fronts). Elle parle au backend via REST :
`POST /api/settings/linkedin` + endpoints `/api/whatsapp/pair/*` (SSE).

## Couches (ADR 0004 — topologie réversible)

oto-mcp porte aujourd'hui 4 métiers ; ils sont des **couches à frontière à sens unique** (ADR 0004) :

- **backend-core** (le centre) : `db`, `credentials_store`, `org_store`, `access`, `crypto`, `connectors`, `auth_hooks`. Identité (`sub`), coffre, orgs, grants/quotas, résolution.
- **adaptateur MCP** : `server`, `tools/*`, `middleware`, `tool_visibility`.
- **adaptateur REST** : `api_routes`.
- **runtime connecteurs** : `tools/*` (in-process) + `tools/remote` (forward bridges).

**Règle** : adaptateurs + runtime → dépendent du backend-core, **jamais l'inverse** ; et ils l'appellent **par interface** (`access.resolve_*`), pas par accès table croisé — pour qu'un seam puisse devenir un service (broker de credentials) sans réécriture. ✅ Le seam **résolution** (le candidat broker) est consolidé dans `access` : `resolve_api_key` / `resolve_credential_fields` / `resolve_crunchbase_session`. C'est la frontière qui doit rester nette (elle peut devenir un service). `tools/meta` (visibilité) et `tools/datastore` (partage) appellent `db` en direct, et **c'est OK** : par le principe ADR 0004 (« pas de discipline d'interface sans force ») ils ne sont pas des candidats-services → pas de reroute dogmatique.

### Couche capacité (`oto_mcp/capabilities/`, ADR 0009)

Pour les opérations exposées sur **deux faces** (MCP + REST), arrêter de câbler les adaptateurs 2× à la main (drift de surface + autz divergente — ex. `oto_use_org` jadis absent en REST, IDOR cross-org). Une **capacité** = un descripteur co-déclaré : `handler` core + `Input` pydantic (seule validation) + règle `authz` **obligatoire** + bindings `mcp`/`rest` (multi-binding possible). Les adaptateurs `_mcp_adapter`/`_rest_adapter` **bouclent** sur `registry.CAPABILITIES` et appliquent **validation → autz → handler** ; le refus est un `AuthzDenied` neutre traduit par chaque face (`McpError` / `json_error`+CORS). `authz` = combinateurs fermés (`SUB_ONLY`, `ORG_MEMBER`, `ORG_ADMIN`, `ORG_MEMBER_OF`, `PLATFORM_ADMIN`, `SUPER_ADMIN`, `ORG_ADMIN_OF`, `GROUP_MEMBER_OF`, `GROUP_ADMIN_OF`) — `ORG_MEMBER`/`ORG_ADMIN` scopent l'**org active** (lecture/écriture self-service `/api/me/*`), les `*_OF(field)` une org/groupe ciblé par id de path. Schéma MCP **plat** via `apply_flat_signature` (gotcha pydantic single-param, cf. memory). **Écho `_org`** (2026-07-02) : `_mcp_adapter` injecte `_org` `{id, name}` dans tout payload dict de capacité org-sensible (`ctx.org_id` posé) — le client voit l'org effective à chaque réponse (désambiguïse post-`oto_use_org` ; face MCP seulement, le REST connaît son contexte). Montés dans `server._build_mcp` + `api_routes.make_routes` (no-op si registre vide). **Domaines orgs + doctrine/instructions 100% migrés** : orgs (use_org, membres, secrets, create, lectures) → 100% en capacités, `api_routes_orgs.py` supprimé ; doctrine (`capabilities/orgs_instructions.py` : get/list/set/delete/versions/revert/usage membre `/api/me/instructions*` + outils `oto_*_doctrine`, et palier admin cross-org `oto_admin_*_doctrine` / `/api/admin/orgs/{id}/instructions*`) — `tools/orgs.py` supprimé, bloc doctrine d'`api_routes.py` retiré. ⚠️ Handler async supporté par les deux adaptateurs (`inspect.isawaitable`) ; le manifeste `referenced_tools` (ADR 0014) résout l'instance FastMCP via `tool_registry.bind(instance)` (posé au boot dans `_build_mcp`). **Domaine user-admin migré** (`capabilities/users_admin.py`) : retrouver/lister un user (`oto_admin_list_users`, filtre `query`), fiche (`oto_admin_user_detail`, par email **ou** sub), rôle (`oto_admin_set_role`), grant de clé plateforme user **et** org (`oto_admin_grant_key`/`oto_admin_grant_org_key` + revoke), option payante comp (`oto_admin_set_option`) — les handlers REST écrits main correspondants ont été retirés d'`api_routes.py` (mêmes chemins servis par les capacités → dashboard inchangé). Donne la face MCP au **setup complet d'un compte depuis Claude**.

**Console admin consolidée par concept (`*_op`, 2026-06-25, commit 92462fe).** Les outils admin ci-dessus sont fusionnés de **36 → 12 `oto_admin_*`** — un outil par objet métier, verbe en param `op` : `oto_admin_{org,org_member,user,access,key_grant}`. Les handlers de domaine sont **réutilisés tels quels** (zéro duplication ; `capabilities/admin_console.py` construit l'`Input` spécifique et appelle `_create_org`/`_add_member`/…). Quand les paliers d'autz divergent dans un même outil (ex. `org` : create=`SUPER_ADMIN`, list=`PLATFORM_ADMIN`), le **combinateur op-aware `ADMIN_BY_OP({op: règle})`** (`_authz.py`) choisit la règle fermée selon `inp.op` → l'autz reste **déclarée au niveau capacité**, jamais redescendue dans le handler (esprit ADR 0009 préservé). ⚠️ Les faces **REST restent par-verbe** (idiomatique + dashboard) → l'autz d'un verbe fusionné est désormais déclarée 2× (MCP op-aware + route REST), même combinateur/handler dessous. **Règle de design — secret brut jamais en argument MCP** (il transiterait dans le contexte LLM) : la **pose** de secret (`set_org_secret`, `delete_org_secret`, `set_platform_key`, `set_quota`) est **dashboard-only** (binding `mcp` retiré, REST conservé) ; le MCP ne porte que les **droits/grants** (`oto_admin_key_grant`).

## Auth — Logto

JWT Logto **ES384** (défaut RS256 = tout rejeté), discovery RFC 9728 sur 401,
façade DCR self-service (`oauth_facade.py`) pour les clients sans DCR (Claude/ChatGPT/
Mistral). **Détail : `docs/auth-logto.md`** (gotchas, env, onboarding).

> **⚠️ CUTOVER ADR 0040 (2026-07-06) — `.ninja`↔`.cx` inversés.** Désormais **PROD =
> `mcp.oto.cx`** (:9103, audience canonique `mcp.oto.cx/mcp`, dashboard `manage.oto.cx`) et
> **PREPROD = `mcp.oto.ninja`** (:9105, audience `mcp.oto.ninja/mcp`, dashboard `manage.oto.ninja`).
> DB découplée (backends inchangés, seuls domaines/audiences/dashboards ont basculé ; prod
> reste sur `otomata-main`). ⚠️ **Logto = 2 instances** : la vraie prod/preprod = **`auth.oto.ninja`**
> (creds SOPS `LOGTO_NINJA_MGMT_*`), PAS `auth.oto.zone`. Les mentions `mcp.oto.ninja=prod`
> ailleurs dans ce fichier sont **antérieures au cutover**.
>
> **Coexistence multi-domaine (2026-07-02, contexte pré-cutover)** : `mcp.oto.cx/mcp` servait le
> MCP en plus de `mcp.oto.ninja` — env **`MCP_AUDIENCE_ALT`** (audiences canoniques secondaires,
> vide = no-op), resource Logto dédiée, PRM Host-aware (`config.mcp_audience_alt_hosts`).
> DNS mcp.oto.cx = grey+ACME direct box.

## Rôles + résolution de clé API

3 paliers `member < admin < super_admin` (accès admin UI). Résolution de clé par
appel : `clé membre (sub, org) > group_secret > org_secret > platform_grant` (chemin
platform gaté sur `auth_modes`). **Détail : `docs/roles-and-resolution.md`** (paliers,
grants/quota, platform keys, providers byo-only).

> **Scope MEMBRE (ADR 0033)** : plus de credential per-user org-agnostique — la clé
> BYO est keyée `(sub, org)` (coffre `entity_type='member'`, AAD lié à l'org ; google
> + unipile inclus, seuls les mounts oauth fédérés restent scope user). L'org de scope
> = seam `current_org`, à la pose comme à la résolution.
> **Détail (helpers db, state HMAC google, migration) : `docs/roles-and-resolution.md` §Scope MEMBRE**.

**Seam substrat (ADR 0024)** : `access.resolve_credential(provider, want, sub?)` marche la cascade UNE fois → `ResolvedCredential{key, is_platform, mode, config, fields}` ; `resolve_api_key`/`resolve_credential_fields` = vues minces dessus (les ~15 tools keyed inchangés). `config` = **config non-secrète appariée à la clé gagnante** (endpoint/host : `dsn` unipile, `base_url` n8n/make, `data_center` zoho — `config_fields` `secret=False` ∪ meta public) → ne JAMAIS recâbler un résolveur d'endpoint par-connecteur. `access.credential_mode_for(sub, provider)` = le `mode` sans déchiffrer (détection BYO = `mode ∈ {user,group,org}`, jamais un check user-only).

## REST API (consommée par le dashboard / oto.ninja)

Endpoints `/api/*` (compte, settings, orgs, admin, datastore…), même
`JWTVerifier` que `/mcp`. **Inventaire : `docs/rest-api.md`**.

## Browser automation & LinkedIn — substrat hébergé Browserbase (ADR 0026)

Plus AUCUN browser sur la box : les connecteurs d'**API privée cookie-bound**
(`brevo`, `crunchbase`, `pennylaneged`) passent par **Browserbase** (Chrome hébergé,
Context per-user = la session loguée au coffre, Live View pour le login interactif,
`run_fetch` same-origin). Connexion = dashboard (`browser_session.py`, un seul corps
REST+MCP, `login_url` obligatoire au register). LinkedIn = **Unipile** (tools/linkedin
supprimé) ; l'injection de cookie `li_at` côté serveur déconnecte l'user (#5).
**Détail (substrat, connecteurs, sécu, leçons empiriques) : `docs/browser-automation.md`**.

## SIRENE stock (DuckDB sur parquet INSEE — lu depuis S3/httpfs)

Stock complet (~43M établissements, parquet ~2GB) interrogé via DuckDB :
- **Source = Object Storage** (ADR 0002 résolu 2026-06-22) : la box dédiée n'est PAS
  co-localisée avec le parquet → `SIRENE_STOCK_PARQUET_PATH=s3://oto-media/sirene/StockEtablissement.parquet`,
  lu en **httpfs** (range reads, pruning de row groups). Creds DuckDB via env
  `SIRENE_STOCK_S3_{ENDPOINT,REGION,KEY_ID,SECRET,URL_STYLE}` (url_style=`path` pour
  Scaleway — `vhost` 3× plus lent). Le module accepte aussi un chemin local ou une URL
  `https://` publique. **Perfs box (2 vCPU)** : lookup point ~2s, scan filtré ~20-30s.
  ⚠️ Pour CHERCHER des boîtes (secteur/zone/taille), préférer **`fr_search`**
  (API recherche-entreprises indexée, <1s, filtre `categorie_entreprise` PME/ETI/GE) ;
  le parquet = lookups ponctuels + **bulk** (cf. ci-dessous) + énumération exhaustive >10k.
- Refresh : data.gouv republie mensuellement (URL datée → `deploy/refresh_sirene_stock_s3.sh`
  résout l'URL via l'API data.gouv puis push S3, à lancer sur otomata-0 ; **cron non installé** —
  le parquet bouge lentement, refresh manuel quand ça compte).
- Query layer : `france_opendata.sirene_stock` (lib PyPI `france-opendata[stock]`, **>=0.11** = support s3:///httpfs).
- MCP tools `fr_stock_*` (ex-`sirene_stock_*`, fusionnés dans le connecteur `sirene` le 2026-06-22 — même domaine entreprises FR, namespace `fr`) : **`fr_stock_enrich(sirens=[...])`** (bulk — sièges d'une LISTE en UN scan), `fr_stock_siege`, `fr_stock_etablissements`, `fr_stock_siret`, `fr_stock_search` (`sieges_only=True` = siège strict). Pendant parquet des `fr_*` live.
- REST `/api/sirene/{headquarters(POST,batch),siege,etablissements,siret,search,info}` (noms de routes **inchangés** — `oto-cli`/`oto-core` en dépendent ; orthogonaux aux noms MCP).
- Consommé par `oto-cli` (`SireneStock` HTTP client, oto-core >=1.8 — `get_headquarters_addresses` = 1 POST batch, plus N appels) — voir ADR 0001 + 0002 dans le privé `otomata-private`.

## Datastore (spine natif PG, ADR 0016)

Spine plateforme de stockage structuré (PG/JSONB natif, plus Google
Sheets). Surfaces : tools `data_*` (MCP) + REST `/api/datastore/*` ; OAuth Google
per-user (Gmail/Tasks, multi-compte) câblé ici. **Détail : `docs/datastore.md`**
(surfaces, OAuth multi-compte + scopes restricted/CASA, setup GCP, env vars).

## Propriété de ressource — primitive `ownership` (ADR 0030)

`ownership.py` = seam unique : ressource possédée par `(owner_type∈{user,group,org},
owner_id)` + partages `resource_grants` (deny-by-default). **Deux plans jamais
confondus** : `can_access` (contenu, privacy by default) vs `can_govern` (gouvernance,
escalade roles.py). ⚠️ **Une LISTE de contenu scope sur `active_owner(current_org)`,
JAMAIS `owner_pairs()`** (union de toutes les orgs = fuite fail-open ; tripwire
`test_owner_scope_tripwire.py`). Plus de « perso » : tout user a une org perso dédiée
(`orgs.personal_of`), défauts de création = org active.
**Détail (datastore pilote, oto_resource, migration, abolition du perso) : `docs/ownership.md`**.

## Projet — couche d'organisation (ADR 0030/0032)

Conteneur de travail **possédé** : brief + liens typés (`project_links` : tableau/
procédure/connecteur/**doc** — `doc` = une page Documents attachée, ex-memento
base/page retirés le 2026-07-03) + docs en arbre. Capacités `oto_project`/`oto_doc` ;
partage/transfert via `oto_resource`. S'y greffent : **livraison client cascade**
(#52), **endpoint MCP + partage navigable par projet** — un projet publié est servi sur
son sous-domaine dédié, modes **anonymous** (`<slug>.mcp.oto.cx`, sans login + listé) /
**secret** (`<slug>.share.oto.cx`, URL non devinable = **UI navigable** lecture seule des
procédures/tableaux/docs, rendu server-side `share_ui`, + MCP au path `/mcp`) / **org**
(authentifié) ; sonde credential-less **non bloquante** → `mcp_unresolvable_tools` en
warning ; annuaire oto.ninja/apps. (Le partage public **chiffré** `/p/p` a été retiré,
supplanté par ce partage navigable live.) La page navigable (`share_ui`) est un **canal
d'acquisition** : hero « brancher », connecteurs en pastilles (logo + tooltip + lien fiche),
tableau riche (recherche/tri/filtres), et CTA **« Ajouter à mon Oto »** → capacité
`me.import_project` (`POST /api/me/projects/import`) qui **forke un projet publié par slug**
dans l'org active (structure only, jamais de credentials ; idempotent via `projects.copied_from`).
**Détail : `docs/projects.md`**.

## Messagerie & LinkedIn (Unipile)

Tools `whatsapp_*`/`telegram_*`/`instagram_*` + `unipile_*` = **Unipile hébergé**
(factory channel-agnostic, `account_id` per-membre `(sub, org_id, provider)` ADR 0033,
no-fallback anti-usurpation). Mode plateforme (clé partagée + grant + option comp),
DSN par credential, sélecteur d'identité, **comptes partagés autorisés** (#55, grants
revalidés à chaque appel, jamais de repli silencieux).
**Détail : `docs/unipile.md`**.

> **Version API v1/v2 = propriété de la CLÉ (« selon la BYO »), pas un connecteur ni un
> flag global (2026-07-07).** v2 est un compte/clé Unipile **distincts** (beta) : une clé
> v1 ne marche pas en v2. La version est portée par `meta.api_version` du credential
> (`{api_version:"v2", dsn:"api.unipile.com"}`) → `resolve_credential.config` → `unipile_client()`
> + `unipile_connect.hosted_auth_url` routent v1/v2. **UN seul connecteur `unipile`** (surface
> identique, `client_v2.UnipileClientV2` iso `UnipileClient`) ; absence de meta = **v1 défaut**.
> Pose de la version : chemin **member** (`POST /api/settings/api-keys/unipile`, param `api_version`)
> ET **org** (`org.secret.set`, param `api_version`) → dashboard : sélecteur sur le form clé d'org
> + section « ma clé perso » du widget hosted (clé member prime sur org, cascade `resolve_credential`).
> Deltas API v2 (base fixe `api.unipile.com/v2`, account_id-in-path, enveloppe, inbox model,
> posts keyés URN…) dans les **docstrings de `client_v2.py`** (oto-core ≥v1.19.0). Migration = `#63`.

> **Couche 3 « option » = source unique `access.option_open(sub, connector, org, group)` (2026-07-07).**
> « L'option payante est-elle levée ? » était recopiée à 3 endroits (`connectors_selection.option_ok`
> + `unipile.status_for.subscribed` self & admin) → divergence (le **BYO ouvre l'option** — l'user
> gère sa propre instance — était oublié dans un seul) → carte incohérente « clé d'org (vert) +
> Bloqué (rouge) ». Règle : pas d'option ⟹ ouvert ; sinon **BYO** OU `has_option` (comp/abonnement).
> Le **front est backend-driven** (rend `option_ok`/`subscribed`, 0 RBAC recodée client) → il devient
> durable car il lit un flag cohérent. **Ne jamais recoder une règle d'accès côté front** : ajouter
> un flag backend. Le gate DUR (qui peut utiliser) reste `require_connector_access` (ADR 0025, couvre
> le BYO — « pas de clé perso qui contourne ») ; il gate aussi la **pose** (`api_key_save` → 403).

## Monitoring des appels MCP

`ToolCallLogger` (lib otomata-calllog) journalise chaque appel dans `tool_calls`
(`db.insert_tool_call`, best-effort, identité = `sub` du JWT via
`current_user_sub_from_token`) ; surface admin `/api/admin/monitoring/{summary,calls}`.
**Détail : `docs/monitoring.md`**. ⚠️ **Ne trace QUE les invocations d'outils MCP** —
pas la connexion du connecteur, pas le `tools/list`, pas les appels REST/dashboard.
Donc **compte actif ≠ usage** : un user qui a un compte (table `users`) mais 0 ligne
`tool_calls` n'a jamais déclenché d'outil (connecté-mais-idle, OU handshake OAuth du
connecteur jamais réussi → diagnostiquer via `journalctl` 401). Vécu 2026-06-22 (JB,
Julien : comptes actifs, 0 appel ; le monitoring marchait, eux n'avaient rien invoqué).

## Error tracking (Sentry)

Exceptions backend → **Sentry SaaS** (gaté `OTO_SENTRY_DSN`, no-op si absent →
le serveur boote sans). Deux captures : **500 des routes REST `/api/*`** via
l'intégration Starlette (auto) ; **exceptions des tools MCP** via
`SentryToolErrorMiddleware` (`sentry_setup.py`) — une erreur de tool est une erreur
JSON-RPC en **HTTP 200**, invisible à l'intégration Starlette, donc capturée là où
l'exception est vivante (vrai traceback, tag `mcp.tool` + `user.id=sub`). RGPD :
`send_default_pii=False`, **jamais** les args d'appel dans l'event. `before_send`
**droppe les 4xx amont** (`HTTP 4xx` d'une API tierce = input rejeté, pas un bug
backend). Env box : `OTO_SENTRY_{DSN,ENV,RELEASE,TRACES_SAMPLE_RATE}` ; région **EU**
`de.sentry.io` (org slug `otomata-vz`). Surveillance/triage = doctrine oto
`surveillance-erreurs` (token API en SOPS `sentry_api_token`).

## Onboarding = un projet « Découverte » (ADR 0032 §7)

**Plus de mode d'accueil spécial** (retiré le 2026-07-01) : pas de booléen `onboarded`,
pas de checklist dashboard, pas de tool d'onboarding scripté. L'onboarding est **un
projet** comme un autre — un projet « Découverte » porteur d'un brief d'accueil, **semé
à la création de l'org perso** (`discovery.seed_for_org`, appelé par
`org_store.ensure_personal_org` sur la branche création, best-effort). Il remonte à
l'agent via la ligne « Projets récents » du bloc C des instructions (`instructions.py`) ;
l'agent l'ouvre (`oto_use_project`) et déroule l'accueil depuis son brief.

**La fiche « situation avec oto » reste** (qui est l'user, son métier, ses objectifs, son
CRM, les connecteurs voulus, son ton) — découplée de l'accueil, c'est un data model libre
relu à chaque session :
- `tools/profile.py` expose `oto_profile(op="get"|"update", fields=…)` (spine, hors gate,
  **toujours visible** via `PROTECTED_TOOLS`) — l'agent l'entretient au fil de l'eau.
- DB : table `user_account_profile(sub PK, profile jsonb, created_at, updated_at)`
  (`db.get_account_profile` / `db.update_account_profile`). **Injectée au handshake**
  (bloc C, section « Ce que tu sais de l'utilisateur ») → enfin utilisée, plus seulement
  collectée. N'est plus exposée sur `/api/me` (le bloc `onboarding` a été retiré).

`tools/whoami.py` (spine, chargé explicitement dans `register_all`, hors gate
d'activation, **toujours visible** via `PROTECTED_TOOLS`) expose `oto_whoami()`
(lecture) — l'**identité MCP courante** sous laquelle Claude agit : compte (`sub` +
email + rôle plateforme) × **org active** (id/name/rôle) × **groupe actif**, plus un
résumé des connecteurs configurés et l'état Memento. C'est le pendant agent du badge
« identité MCP » du dashboard ; à appeler pour confirmer le contexte avant une action
sensible. Pour basculer : `oto_use_org`.

## Boucle d'usage (ADR 0017)

Flux d'événements de session unifié : calllog (involontaire) + feedback volontaire
d'agent (`feedback`, signal=tool_feedback|gap) + runs / déroulés (`run_start/finish`,
`doctrine` optionnel → doctrine nommée ou run one-shot). **Détail : `docs/usage-loop.md`**.

> **Runs persistés (#50, amende le « state-only » d'ADR 0017).** La métadonnée
> sémantique d'un run (label / doctrine / outcome) vit désormais dans la table `runs`
> (`db.insert_run`/`finish_run`/`recent_runs`) — la pile session-scopée de
> `doctrine_run.py` reste la **source du run actif** (stampe `tool_calls.run_id`),
> `run_start`/`run_finish` y ajoutent la trace durable (best-effort, off-loop). Sert
> l'anticipation du contexte injecté (instructions bloc C) + la boucle d'usage dashboard.

## Email (envoi per-org, par connecteur)

Deux connecteurs **BYO-org** : `scaleway` (API TEM directe, fields — domaine garanti
par Scaleway) + `resend` (BYOK). `email_send` = spine qui route
`sender→connecteur→transport` (`EMAIL_CONNECTOR_TRANSPORT`) ; config
`orgs.email_settings` par connecteur (senders + quiet hours) ; envoi différé
(`scheduler.py`, quiet hours 20h–8h défaut). **Détail : `docs/email.md`**.

## Visibility per-user

`UserDisabledToolsMiddleware` (`middleware.py`) applique au handshake `initialize` les visibility rules natives fastmcp (`disable_components` via `_visibility_rules` session state). Plus de filtrage manuel `on_list_tools`/`on_call_tool` — fastmcp émet `tools/list_changed` automatiquement quand les rules changent. Le **calcul** de la denylist `(sub, org active)` + son application vivent dans **`session_visibility.py`** (`compute_hidden_tools` / `apply_session_visibility(ctx, sub, *, reset=…)`), partagés entre le middleware (handshake) et le **refresh à chaud** post-bascule.

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). **Les presets de tools (snapshots nommés + baselines org/équipe) ont été retirés** — la visibilité ne dépend plus que des défauts plateforme et des toggles perso.

**Masqués par défaut** (`is_default_hidden`) : invisibles par défaut sur la surface authentifiée, **self-activables**. Deux grains : `tool_visibility.py::DEFAULT_HIDDEN_TOOLS` (noms individuels) et `DEFAULT_HIDDEN_NAMESPACES` (namespaces entiers, **dérivé du registre** — champ `default_hidden` de `connectors.py`). Cas actuel : **`attio_*`** (le MCP Attio officiel est préféré ; code conservé pour implems custom). Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué-par-défaut > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève (même logique côté REST `/api/me/tools/{name}`). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Masquer un connecteur entier = poser `default_hidden=True` au registre ; un tool isolé = `DEFAULT_HIDDEN_TOOLS`.

Méta-tools exposés (`tools/meta.py`) : `oto_list_my_tools`, `oto_disable_tool`, `oto_enable_tool`, `oto_call`, `oto_tool_schema`. **`PROTECTED_TOOLS`** (`tool_visibility.py`, source unique) = quatre familles jamais masquables (default-hidden inclus) **ni désactivables** : méta-toolset + identité (`oto_list_my_tools`/`oto_enable_tool`/`oto_whoami`/`oto_profile`), échappatoires de contexte (`oto_use_org`/`oto_clear_org`/`oto_list_orgs`/`oto_use_group`/`oto_clear_group` — anti-lockout, vécu Sentry 2026-06-30), boucle d'usage (`feedback`/`run_start`/`run_finish` — mandatés par les instructions plateforme ADR 0017 : un toggle qui les masque rend le gap invisible), **dispatch universel** (`oto_call`/`oto_tool_schema` — ADR 0036 : appeler par son nom un outil NON listé (FOD, connecteur non activé) le temps d'un appel, sans muter la visibilité ; exécution par `Tool.run` HORS middleware → gates call-time intactes + rédaction ré-appliquée via `redaction.py`). Garde des deux faces (2026-07-02) : `oto_disable_tool` refuse, `POST /api/me/tools/{name}` → 400 `protected_tool` ; `GET /api/me/tools` expose `protected:bool` (toggle inerte dashboard).

**Refresh à chaud de la toolbox sur bascule de profil** : une capacité qui change le profil de visibilité déclare `refresh_visibility=True` (`Capability`) ; l'adaptateur MCP (`capabilities/_mcp_adapter.py`) rejoue alors `apply_session_visibility(reset=True)` sur la session **courante** après le handler → `tools/list_changed` live. Posé sur `org.use_org`/`org.clear`/`org.create`/`org.set_home` + `group.use`/`group.clear`/`group.set_home`. Donc **`oto_use_org <org>` recharge la toolbox dans la conversation en cours** (les credentials, eux, basculent déjà — `resolve_api_key` relit l'org **via le seam `current_org`** à chaque appel, cf. §ADR 0023 ci-dessous).

**Limite connue** : ça ne vaut QUE pour la face MCP (même session). Un toggle/bascule via **REST** (dashboard) passe par une connexion séparée → ne notifie pas une conversation Claude déjà ouverte (visible à la prochaine session). Pousser dashboard→session MCP demanderait un registre `sub → sessions actives` + push hors-requête (non fait).

## Org/équipe : session vs maison vs consultation (ADR 0023, amende 0015)

Le pointeur unique « org active » est scindé en **3 notions**, résolues par le **seam unique `access.current_org(sub)`** (mirroir `access.current_group(sub)` pour l'équipe) = `session ?? consultation ?? maison`. **TOUTE résolution d'action passe par ce seam** (`resolve_api_key`, visibilité `session_visibility`, field-filters, doctrine de groupe, `/api/me`, whoami, et l'injection `org_id` des règles d'autz `_authz`) — ne plus lire `org_store.get_active_org` en direct dans un chemin de résolution (**tripwire** `tests/test_org_seam_tripwire.py` : les call-sites légitimes de la maison sont figés en allowlist ; vécu 2026-07-02 — catalogue + toggles REST scopaient la maison, le switch d'org du dashboard était ignoré, fixé `25e9f22`. Pendant front : `orgScope.spec.ts` d'oto-dashboard interdit un `fetch` nu hors du client central qui injecte `X-Oto-Org`).

⚠️ **Ce seam est scopé sur l'ACTEUR courant** : session/consultation sont stockées **par requête**, le `sub` ne sert qu'au repli `home_org`. Donc `current_org(autre_sub)` renvoie le contexte du **requérant**, pas du tiers — **NE JAMAIS** l'utiliser (ni `status_for`/`has_option`/`credential_mode_for` qui en dérivent) pour calculer l'état d'un **tiers** (écran admin). Passer son org/groupe **explicitement** via le kwarg `org`/`group` (sentinelle `access._UNSET` = défaut `current_org`, self inchangé), source = `org_store.get_active_org(target)`. Bug vécu 2026-06-24 (fiche admin montrant l'option de l'org du requérant). L'état d'un user est par ailleurs souvent **per-org** (∈ N orgs) → préférer une vue par org (cf. `tools/unipile.admin_status_by_org`).

- **Org de session** (éphémère, MCP) — override posé par `oto_use_org`/`oto_clear_org` (devenus **session-scopés**, ne touchent plus la colonne) dans `session_org.py` (store sync keyé par `ctx.session_id` — `get_state` async est inutilisable depuis `resolve_api_key` sync). Meurt avec la conversation ; repose sur l'isolation des sessions claude.ai par conversation. **Pas de jeton rejoué par appel** (bracelet serveur, pas de discipline LLM).
- **Org maison** (`org_store.get_active_org`, ex-« active_org ») — défaut persistant des **nouvelles** conversations. Posée explicitement : `oto_set_home_org` (MCP) ou `PUT /api/me/active-org` (REST/dashboard) ; **jamais** par navigation dashboard.
- **Org de consultation** (REST, view-as) — header `X-Oto-Org` (équipe : `X-Oto-Group`), posé par le **middleware ASGI `api_routes.ViewAsMiddleware`** (brut, n'altère pas le streaming `/mcp`) APRÈS **validation d'appartenance** (anti-IDOR : `roles.is_org_member`/`can_read_group`) dans un contextvar lu par `current_org`. Le dashboard consulte n'importe quelle org **sans muter l'identité MCP** — mais « consultation » = **org de TRAVAIL de l'onglet, lecture ET écriture** (poser une clé, éditer les settings y atterrissent), gatée par le rôle réel dans l'org ciblée ; le seul mode read-only est le view-as USER ci-dessous.
- **« Voir en tant que » (axe USER, REST, lecture seule)** — header `X-Oto-View-As=<sub>` posé par le même `ViewAsMiddleware`, gaté **opérateur plateforme + cible existe + méthode GET** (mutations → 403 `view_as_read_only`). `_authenticate` renvoie alors le **sub cible** (param `apply_view_as`, contextvar `session_org.current_view_user`) → tout `/api/me/*` (capacités incluses) rend la vue de la cible. **REST-only** : le MCP ne lit jamais ce contextvar (zéro impersonation dans Claude). Front : bouton sur la fiche admin + bandeau `ViewAsBanner` (`lib/viewOrg.ts`).

**Invariant groupe⊂org dérivé** : un override/consultation d'org **sans** groupe explicite ⇒ niveau org (jamais le `home_group` d'une autre org) ; toute bascule d'org de session retire l'override de groupe. `/api/me` expose `active_org`/`active_group` (effectifs) **et** `home_org`/`home_group` (défauts) distinctement. `oto_whoami` montre l'org effective + `scope: home|session`.

## Agent readme (cumulable) & procédures — ex-« doctrines & instructions d'org »

Vocabulaire produit (unbundle 2026-07) : **agent readme** = prose libre **injectée à
chaque session**, cumulée du général au spécifique — **plateforme** (bloc A) → **org**
(`org_instructions` slug réservé `claude_md`) → **équipe active** (`org_group_instructions`
slug `claude_md`, désormais VRAIMENT injecté au handshake, plus seulement servi par
`oto_get_doctrine`) → **user** (table `user_agent_readme(sub PK, body_md)`, NOUVEAU —
capacité `me.agent_readme.{get,set}`, REST-only `GET/PUT /api/me/agent-readme`, éditée
dashboard `/account` ; repointée par `migrate_sub`). Chaque niveau passe par `_apply_vars`
({{org}}/{{user}}/{{équipe}}/{{connecteurs_actifs}}). **Procédure** = doctrine nommée
(skill), chargée à la demande — les identifiants de code (`oto_get_doctrine`, tables,
`docs/doctrines.md`) gardent le mot doctrine. Prose opératoire versionnée par org,
**détail : `docs/doctrines.md`**.

> **Livraison au LLM = injection, plus un appel d'outil (otomata-private#49 puis #50, amende ADR 0014).**
> Le canal FIABLE de bootstrap = les `instructions` du `initialize` (FastMCP les relit par
> session ; Claude rehandshake par conversation). `DynamicInstructionsMiddleware.on_initialize`
> (`middleware.py`) **remplace** `result.instructions` par `instructions.compose_session(sub, org_id)`
> — un **artefact composé de 2 blocs** (`instructions.py`, #50 ; l'ex-bloc B onboarding a été
> retiré le 2026-07-01 — l'onboarding est un projet, ADR 0032 §7) :
> - **bloc A « secret sauce »** (posture + boucle d'usage + **catalogue de namespaces** dérivé) —
>   prose en DB `platform_instructions['secret_sauce']`, éditable admin plateforme, **inviolable par
>   l'org**, toujours injecté (seedé depuis la constante = fallback) ; le catalogue est appendé à la composition ;
> - **bloc C « contexte dynamique »** par-(sub, org) — section de contexte résolu (org / équipe /
>   connecteurs actifs / N derniers projets / derniers déroulés via `db.recent_runs` / fiche profil
>   « situation avec oto » de l'user) + **agent readme cumulés** org → équipe active → user
>   (`_format_org_readme`/`_format_group_readme`/`_format_user_readme`), chacun avec substitution
>   `{{org}}`/`{{user}}`/`{{équipe}}`/`{{connecteurs_actifs}}`.
>
> Donc **ne plus prescrire « appelle `oto_get_doctrine()` au démarrage »** — la doctrine est injectée.
> Les **doctrines nommées (skills)** ne sont pas des outils → absentes de `tools/list` → `on_list_tools`
> **enrichit la description de `oto_get_doctrine`** avec leur index per-org (`instructions.skills_index_md`,
> Tool non-frozen → `model_copy`). `render()` reste la surface STATIQUE (boot / fallback, sans DB).
> Tout **fail-open** (pas de sub/org/doctrine/DB → surface statique). Édition des blocs A/B : capacité
> `oto_admin_platform_instructions` (+ REST `/api/admin/platform-instructions`, `PLATFORM_ADMIN`) →
> éditeur dashboard `/platform/instructions`. Transparence : `/api/me/agent-context` rend le même
> artefact composé. **Reste (#54)** : anticipation **pilotée** (message proactif amorcé par l'admin).

> **Slots de procédure (ADR 0035, B1–B3 déployés).** Une procédure déclare ses **entités
> à instance** (quel tableau, quel compte de connecteur, quelle page Documents) en **JSON propre** :
> colonne `org_instructions.slots` JSONB (`{name, type ∈ tableau|connecteur|doc,
> description?, connector?}`), la prose les référence **par nom** via `<slot:name>` (même
> famille que `<tool:slug>` 0014 ; le binding nom→instance vit dans le PROJET,
> `project_links.slot` — vocabulaire DU projet, unicité `(project_id, slot)` → 409
> `slot_taken` au link). Module `slots.py` = source unique (validation dure
> `validate_slots`/`normalize_name` + check croisé non bloquant `slots_check` : refs
> mortes, slots jamais cités, cohérence connecteurs déclarés ↔ refs `<tool:>`, suggestion
> quand un connecteur à identités est référencé sans slot). Écriture : `oto_set_doctrine`/
> `PUT /api/me/instructions/{slug}` (param `slots`, warnings en réponse) ; transport
> revisions + revert + `copy_instruction_to_org` + publish/fork bibliothèque +
> `duplicate_project`. **Runtime (B3)** : les tools `data_*` acceptent
> `namespace='slot:<name>'` → `access.resolve_slot_tableau` résout contre les bindings du
> **projet actif** ; pas de projet / slot non bindé / binding pendouillant = **McpError
> actionnable, jamais de fallback** (bracelet serveur 0023) ; `data_create_namespace`
> refuse le préfixe (un slot binde un tableau existant). Bloc A : §« Slots » (⚠️ prose
> seedée en DB — une évolution du texte passe par `oto_admin_platform_instructions`, pas
> seulement la constante). Grandfathering : procédure sans slots / nom nu = inchangés.
> Restent B4 (inventaire dérivé) + B5 (vérifications) — épic otomata-private#59.

## Groupes (départements) & hiérarchie de droits (ADR 0012)

Une org se subdivise en **groupes** (départements/équipes) avec un **chef
d'équipe** (`group_role='group_admin'`). La gestion des droits est **centralisée**
dans `roles.py` (escalade descendante, source unique) :

```
platform_admin ⊇ org_admin ⊇ group_admin (chef) ⊇ member
```

Les combinateurs d'autz (`capabilities/_authz.py`) délèguent à `roles`
(`is_org_admin`, `can_admin_group`, `can_read_group`, `effective_group_role`) —
plus d'escalade recopiée à la main. Combinateurs : `GROUP_ADMIN_OF`,
`GROUP_MEMBER_OF` (en plus de `ORG_*`).

Un groupe **gouverne 3 ressources** par délégation de l'org :
- **secrets partagés** — coffre `connector_credentials` (entity_type='group') ;
  cascade `resolve_api_key` = **user_key > secret groupe actif > secret org active > grant plateforme**.
- **doctrine & skills** — `org_group_instructions` (+ revisions) ; `oto_get_doctrine()`
  sert org **puis** groupe actif (complément, chaque skill taggée `scope`).
- **gouvernance de connecteur (ADR 0012 B1/B2, restrict-only — 08/07/2026)** — le chef
  d'équipe peut, pour SON équipe : **couper** un connecteur (`group_connector_activation`,
  coupures seules) et **réserver** un connecteur à des membres (`group_connector_access`).
  **INVARIANT MONOTONE** : l'équipe ne peut que RÉTRÉCIR ce que l'org expose, jamais élargir
  (platform ⊇ org ⊇ group). Dispo = **visibilité** (`session_visibility`, fail-open,
  `connector_activation.effective_for_group`/`group_cut_connectors`). Accès = **gate DUR** :
  seam `access.group_rbac_denied_connectors` (mirror de `rbac_denied_connectors`, bypass
  super/org_admin/group_admin) ; `require_connector_access` = `org_block OR grp_block` à
  **fail-open INDÉPENDANT par palier** (un hoquet DB d'équipe ne désactive pas l'org).
  Capacités `connectors.activation.{group_list,set_group,clear_group}` +
  `connectors.acl.{group_list,group_grant,group_revoke}` (GROUP_*). REST
  `/api/groups/{id}/connectors[/{name}]/activation` + `.../access`.

**Groupe actif** : ≤1 par sub (`org_group_members.is_active`, index partiel),
**invariant** = appartient à l'org active. `set_active_group` pose aussi l'org
active ; `set_active_org` efface le groupe actif. `oto_use_group` /
`PUT /api/me/active-group` (+ `oto_clear_group` / `DELETE`).

Stores : `group_store.py` (miroir d'`org_store` au grain groupe). `org_store`
n'importe PAS `group_store` (SQL direct pour l'invariant org↔groupe → pas de
cycle). Surfaces : capacités `capabilities/groups*.py` (REST `/api/orgs/{id}/groups`,
`/api/groups/{id}*`, `/api/me/active-group` + MCP `oto_*_group*`). `/api/me`
expose `active_group`/`active_group_name`/`group_role` ; `providers[].mode` peut
valoir `group`. **Détails : `docs/groups-and-roles.md`.**

## Fédération MCP & comptes (otomata#16)

Deux mécanismes : **mount** (MCP distant fédéré, token OAuth per-user, pilote
memento) vs **remote** (bridge data-driven ADR 0003, token M2M d'org, pilote = un
connecteur remote client). **Plus aucun mount monté d'office** (memento sorti de
`_DEFAULT_ENABLED_MOUNTS` le 2026-07-02 — fédération en sommeil, masters
memento/atlassian/justicelibre OFF en prod) : un mount suit le régime commun
d'activation (DB `connector_activation` ∪ env `OTO_MCP_MOUNTS_ENABLED`).
**Détail : `docs/federation.md`**.

## MCP Apps — UI rendue (SEP-1865)

Certains tools renvoient une **interface rendue** (carte/table dans un iframe
sandbox côté host : claude.ai, VS Code…) au lieu de JSON brut, via l'extension
MCP Apps (SEP-1865, stable). Implémenté avec **`prefab_ui`** (extra
`fastmcp[apps]`, déclaré dans `pyproject.toml` → installé par le `pip install -e .`
du deploy) : un tool `@mcp.tool(app=True)` renvoie un composant `prefab_ui`
(`Card`/`Column`/`Heading`/`Text`/`DataTable`) que le host peint ; dégradation
gracieuse en texte pour les clients sans support.

**Convention** : variantes **flagship `*_app`** (≠ remplacer les tools JSON), où
un visuel aide vraiment l'utilisateur. Les tools JSON équivalents restent la voie
par défaut/agent (« si le rendu échoue, utiliser le tool JSON équivalent »).
L'import de `prefab_ui` est **optionnel et guardé** dans le module (si l'extra
manque, les `*_app` ne s'enregistrent pas, les tools JSON restent). Premier jeu :
`tools/foncier.py` → `foncier_site_app` (fiche site : géocodage + parcelle +
bâti), `foncier_comparables_app` (ventes comparables DVF autour d'une adresse),
`foncier_prix_m2_app` (stats €/m² d'une commune). Mêmes clients open-data que les
tools JSON ; rendu **défensif** (colonnes dérivées des clés réelles) pour ne pas
dépendre d'un nom de champ. Gatés par le connecteur (namespace `foncier`).

## Conventions

- Nouveau connecteur = (1) un fichier `tools/<service>.py` exposant `register(mcp)`,
  (2) une **entrée au registre `providers.py`**. `register_all` (`tools/__init__.py`)
  **DÉRIVE le chargement du registre** (#24, fin de la liste hardcodée) : il boucle
  sur les providers `kind="tools"` et importe `Connector.modules` (défaut = nom du
  provider ; renseigner `modules` si module ≠ nom, ou plusieurs modules par provider —
  ex. `sirene`→`fr`, `google`→`gmail`/`datastore`/`tasks`). Chaque import en
  try/except (un connecteur cassé ne fait pas tomber le serveur). `meta`/`orgs`
  (spine) + `remote`/`mount` (génériques) restent chargés explicitement. ⚠️ Le
  namespace déclaré doit matcher `namespace_of(tool)` (1er token avant `_`) — pas de
  namespace multi-mot (`culture_spectacle`→`culture`), sinon fail-open du gate.
  Le garde-fou `test_tools_module_derivation_matches_filesystem` (`tests/test_capabilities_drift.py`)
  est **auto-maintenu** (croise `tools/*.py` au registre) — ajouter un connecteur
  (fichier + entrée registre) le garde vert SANS rien y toucher ; il casse seulement
  sur un **fichier orphelin** (connecteur posé mais pas déclaré → dort invisible) ou un
  **module fantôme** (faute dans `modules=`/nom). Seul un **module spine** chargé
  explicitement (rare) s'ajoute à `_EXPLICIT_TOOL_MODULES`. Le job `test` de
  `deploy.yml` tourne **sur les PR ET sur push main** (`on: pull_request` + `push`,
  required check de branch protection) et installe oto-core **au tag épinglé** (runner
  neuf → pin du pyproject) : un test rouge bloque le merge ET le deploy (`deploy` a
  `needs: test`). Garde-fou anti-version-skew : `test_tools_client_methods_exist.py`
  vérifie STATIQUEMENT que chaque `_client().<m>()` d'un tool existe sur la classe
  oto-core épinglée (un tool en avance de phase sur son oto-core casse la PR au lieu
  d'atteindre la prod — leçon `folk_get_user`).
- **PERF — le serveur est MONO-LOOP : aucun I/O bloquant dans la boucle.** Un handler
  de tool qui n'`await` rien doit être `def` sync (threadpool) ; du DB sync dans un
  middleware = même règle (`run_in_threadpool`). Deux modes de gel vécus + garde-fous
  CI (`test_no_blocking_async_handlers`), pool borné (`timeout=5`), observabilité
  (loop_watch/aiodebug, py-spy box, Kuma timeout 30s).
  **Détail (incidents, recettes de diagnostic) : `docs/event-loop-perf.md`**.
- **Cran d'activation (ADR 0010/0011)** : déclarer un connecteur ne l'expose PAS —
  gate DB `connector_activation.py` (master global ± override org, deny-by-default).
  Gate à la **VISIBILITÉ par session** (`UserDisabledToolsMiddleware` + `connector_
  activation`, **fail-open**) : `register_all` charge tout inconditionnellement, le
  middleware masque les tools d'un connecteur non activé pour l'org → (dés)activer
  prend effet à la session suivante **sans restart**, override par org OK. Filtre
  aussi `/api/connectors` (catalogue) ; overlays catalogue `family` (dérivée) +
  `category` (curée) + `publisher` (curé, `_PUBLISHER_BY_CONNECTOR`) + `logo_url`
  (dérivé du **CDN logo.dev** par `Connector.logo_url_for` : domaine de marque curé
  `_LOGO_DOMAIN_BY_CONNECTOR` + token publishable `LOGODEV_TOKEN` en env ; pas de S3,
  pas de seed. open-data/maison sans domaine → pas de logo, monogramme côté UI).
  Surface admin `/api/admin/connectors/activation`
  (`api_routes_connectors.py`) + écran dashboard « connector activation ».
- **Connecteur client-sensible = JAMAIS de code ici** : pont via le connecteur
  **`bridge` universel** (ADR 0034, amende 0003/0011) — UNE entrée générique au
  registre (`kind="remote"`), tools fixes `bridge_describe`/`bridge_call`
  (`tools/remote.py`). L'identité du service ponté vit dans la **CONFIG d'org**
  (champs standard `base_url`/`token`/`label`, `resolve_credential_fields`),
  **jamais dans le namespace** → montrable au catalogue sans nom client (l'ex-fuite
  /tools/mm venait du namespace-par-client). Le bridge distant détient le
  credential métier (contrat ADR 0003 §4 inchangé : `/describe`+`/call`, bearer
  M2M, lecture seule bornée côté bridge, audit `X-Oto-Sub`). Visibilité = régime
  commun (activation × masque, `default_hidden` → self-activable) ; sans
  credential, l'exécution lève proprement. Pilote : le bridge back-office
  Movinmotion (repo privé), migré du legacy per-namespace `mm_*` le 2026-07-02
  (découverte `meta.base_url`, règle de visibilité dédiée et
  `resolve_remote_credential` retirés en B4).
- **Tool API-keyé = déclarer le connecteur dans le registre `connectors.py`**
  (avec `keyed=True` + `auth_modes`) — `KEY_PROVIDERS` et tout le reste en
  dérivent. Le coffre `connector_credentials` est générique (pas de colonne
  par provider) : aucune migration de schéma à ajouter. Sinon `resolve_api_key`
  lève `Unknown provider` à l'appel. Puis poser la clé plateforme en DB via
  `oto_admin_set_platform_key` (plus de bootstrap SOPS — le provider sans clé
  DB n'a simplement pas de mode plateforme).
- **Credential = champs déclarés (modèle générique multi-champs, ADR 0011)** : un
  provider porte `credential_fields` (`CredentialField` name/label/secret/reveal) ou
  les dérive de `secret_kind` (`api_key`=1 champ, `basic_auth`=2). Le coffre encode
  les champs dans l'unique `secret_enc` via `credentials_store.pack_secret`/
  `unpack_secret` (3 formats : valeur brute 1 champ / base64 `email:password` /
  json ≥2). L'endpoint `/api/settings/api-keys/{provider}`, le formulaire dashboard
  et `status_for` bouclent sur `secret_fields` — **zéro branche par connecteur** ;
  un nouveau connecteur multi-secrets = une déclaration. Résolution : `resolve_api_key`
  (1 clé keyed + platform/quota) **ou** `resolve_credential_fields` (byo multi-champs
  sans quota, ex. `silae` : client_id/client_secret/subscription_key). `cookie`/`oauth`
  (linkedin/google/memento) ont des flux dédiés → `secret_fields` vide.
- **Sonde « tester la connexion » par connecteur** (`connector_verify.py`, registre
  calqué sur `browser_session.register`) : un connecteur enregistre une `_verify(fields)`
  qui **lève sur échec** (le message d'exception = le retour d'erreur). Capacité unique
  `connectors.verify` (MCP `oto_verify_connector` + REST `POST /api/me/connectors/{provider}/verify`,
  `authz=ORG_MEMBER`, `level` auto|org) → `{ok, error, elapsed_ms}`, jamais un 500 ;
  `providers.public_catalog` expose `verifiable: connector_verify.supports(name)` (front
  gate le bouton). **Une bonne sonde teste l'auth ET les scopes**, pas juste l'auth :
  seed Zoho (`tools/zoho.py::_verify`) fait un refresh OAuth brut (valide client/secret/
  refresh/région d'un coup + capte le `scope` accordé) PUIS une **lecture réelle**
  (`ZohoClient.list_records` sur Contacts/Deals/Accounts/Leads, `per_page=1`) — une clé
  qui authentifie mais n'a **aucun scope CRM** (ex. clé Zoho **Analytics** posée par erreur
  sur le connecteur CRM) est rejetée avec le scope réel dans le message. ⚠️ Gotchas Zoho
  empiriques : le refresh renvoie **HTTP 200 + body `{"error":"invalid_client"}`** (région/
  client faux) ou `invalid_code`/`invalid_grant` (refresh mort) ; l'API CRM **v7 exige un
  param `fields`** (une lecture nue → 400, pas un scope-mismatch) → sonder via `list_records`
  (qui fournit les `DEFAULT_FIELDS`), pas un `GET /crm/v7/{module}` brut.
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- **Aucune résolution de secret côté serveur hors DB/env de process** : pas de
  `get_secret`/`require_secret` oto.config dans le code serveur (l'unit pose
  `OTO_CONFIG_DISABLE_SOPS=1`, tout résidu échoue fort).
- LinkedIn nécessite le **vrai Google Chrome système** (`google-chrome-stable`, apt)
  sur l'host — PAS le Chromium bundlé Patchright (empreinte TLS ≠ Chrome de bureau
  → bloqué par LinkedIn). `_require_chrome_channel` (`tools/linkedin.py`) force
  `channel="chrome"` et lève une erreur si absent.
- WhatsApp/Telegram/Instagram = messagerie **Unipile** (cf. §WhatsApp) — aucune dép
  Node côté backend. Le Baileys Node (`oto-core/.../whatsapp/node/`) ne sert plus
  qu'à la CLI `oto whatsapp` (fallback archivé).
- Attio (`tools/attio.py`) expose CRUD complet : records (companies/people/deals),
  notes (sauf update body, limite API), tasks, lists, entries, workspace_members,
  comments, threads, meetings, call_recordings + meta (objects, attributes). Pas
  de quota plateforme — chaque user pose sa clé sur `/account`. **Gotcha** :
  `attio_list_threads` renvoie 400 sans `parent_object`/`parent_record_id` —
  toujours filtrer par parent.

## Commands

```bash
# Transport stdio RETIRÉ (2026-06-13) : oto-mcp ne se sert qu'en streamable_http
# (toujours authentifié Logto). Usage local = CLI `oto`. Pour un serveur local,
# lancer en http avec les LOGTO_* et taper avec un bearer.

# Tests — le venv .venv N'A PAS pytest (extra `dev` non installé) et `uv run pytest`
# crée un env éphémère SANS les deps projet (piège, ModuleNotFoundError). Recette :
uv pip install --python .venv/bin/python "pytest>=8.0" "pytest-asyncio>=0.24"
.venv/bin/python -m pytest -q

# Tester un CLONE scratchpad (livraison par PR, tree /data/oto stale) SANS réinstaller les
# deps : réutiliser le venv local (deps+pytest présents) en forçant PYTHONPATH sur le clone
# → `oto_mcp` résolu depuis le clone (PYTHONPATH prime sur l'editable install) = ton code :
#   PYTHONPATH=<clone> OTO_CONFIG_DISABLE_SOPS=1 /data/oto/backend/.venv/bin/python -m pytest -q <clone>/tests/...
# Convention : tester la LOGIQUE PURE (helpers hors DB, ex. `effective_for_group`,
# `_connector_blocked`/seams) + les gardes de capacité par stub ; le chemin SQL est vérifié
# au déploiement (le job `test` du CI tourne le vrai suite avec toutes les deps).

# Deploy — push main déclenche `.github/workflows/deploy.yml` : workflow unique
# CI/CD (job `test` pytest → job `deploy` `needs: test`). Deploy = SSH box dédiée :
# git reset --hard origin/main + pip install -e . + **force-reinstall oto-core
# depuis le tag pinné** (lu du pyproject ; pip saute sinon une dép VCS déjà
# présente) + restart + **smoke HTTP** (GET 200 /.well-known/oauth-authorization-server)
# + **rollback auto** vers le commit précédent si install/restart/smoke échoue. Le
# restart relance start-encrypted (refetch master key). ⚠️ start-encrypted.sh
# untracked → survit au git reset.
#
# ⚠️ CANARI (ADR 0040) : le travail se pousse sur `canari` (→ preprod). Un push
# canari déclenche PLUSIEURS workflows — le VRAI deploy preprod est **« Deploy
# canari »** (job `deploy-preprod`). Le workflow **« CI/CD »** a un job `deploy`
# GATÉ sur `main` → il apparaît **`skipped`** sur un push canari : NE PAS le
# confondre avec un deploy raté. Suivre `gh run watch` sur le run *Deploy
# canari*, pas *CI/CD* (ni *guard-main*).
git push origin main

# Logs
ssh -i ~/.ssh/alexis root@<box> "journalctl -u oto-mcp -f"

# DB inspect (PG managed) — depuis la box (env du process inclut DATABASE_URL via .env)
# ⚠️ `psql` n'est PAS installé sur la box dédiée → passer par le venv + psycopg :
ssh -i ~/.ssh/alexis root@<box> 'cd /opt/oto-mcp && set -a; . .env; set +a; ./.venv/bin/python -c "
import os, psycopg
with psycopg.connect(os.environ[\"DATABASE_URL\"]) as c:
    for r in c.execute(\"SELECT sub, email, role FROM users\"): print(r)
"'

# ⚠️ Déchiffrer un credential ad-hoc : OTO_MCP_MASTER_KEY n'est PAS dans .env
# (fetchée au boot depuis Secret Manager) → recette complète + pièges (RuntimeError
# ≠ InvalidTag ; status_for = credential_status, jamais get_credential_with_meta) :
# docs/connector-vault.md §Déchiffrer un credential ad-hoc.
```

## Infra

Déployé sur une **box Scaleway dédiée** (ADR 0002, depuis 2026-06-11) : oto-backend isolé + Caddy + chiffrement du coffre actif, sert `mcp.oto.ninja`. **DB** = PostgreSQL managé partagé (`otomata-main`, DB `oto_mcp`). Le coffre `connector_credentials` est chiffré au repos (AES-256-GCM, master key en Secret Manager fetchée au boot, 0 plaintext). Object Storage S3 pour avatars/logos (`media_store.py`).

> **Détails machine = repo privé `otomata-tech/infra`** (IPs, IDs de secrets/zone/instance, systemd, runbook deploy, env de process) — pas ici (ce repo est public). Voir `infra/docs/oto-platform-state.md` + docs ciblés (`scaleway-managed-db.md`, `caddy.md`, `cloudflare.md`, `deploy-keys.md`). Toute intervention prod = skill `prod-init`.

## Docs

- `docs/connector-model.md` — **carte d'ensemble** : les **3 couches** d'un connecteur (disponibilité / authentification / option de connecteur), la matrice des niveaux (user/groupe/org/plateforme), le vocabulaire canonique, le seam `access.has_option`. **À lire en premier** avant de toucher activation/clés/options (les autres docs ci-dessous = le détail par couche).
- `docs/connector-vault.md` — **archi centrale** : registre source unique (`connectors.py`), coffre chiffré unique `connector_credentials` (clés API + platform_keys + sessions linkedin/crunchbase/google multi-compte), enveloppe AES-256-GCM **obligatoire** (pas de plaintext), résolution + palier org. À lire avant de toucher credentials/registre/résolution.
- `docs/roles-and-resolution.md` — rôles (3 paliers) + cascade de résolution de clé / grants / platform keys.
- `docs/doctrines.md` — doctrine & skills d'org (oto_get_doctrine, versionnée).
- `docs/auth-logto.md` — auth Logto ES384, discovery RFC 9728, façade DCR.
- `docs/rest-api.md` — inventaire des endpoints REST `/api/*`.
- `docs/federation.md` — fédération MCP : mount (per-user) vs remote/bridge (org).
- `docs/usage-loop.md` — boucle d'usage ADR 0017 (calllog + feedback + déroulés).
- `docs/monitoring.md` — monitoring des appels MCP (tool_call_log + surface admin).
- `docs/datastore.md` — datastore spine PG (`data_*`) + OAuth Google per-user (setup GCP, scopes).
- `docs/groups-and-roles.md` — groupes/départements & hiérarchie de droits (ADR 0012).
- `docs/browser-automation.md` — substrat Browserbase (Context/Live View/run_fetch), connecteurs brevo/crunchbase/pennylaneged, LinkedIn isolation de session.
- `docs/projects.md` — projet (liens typés, docs), livraison client cascade, endpoint MCP + partage navigable par projet (`<slug>.{mcp,share}.oto.cx`).
- `docs/unipile.md` — messagerie hébergée : mode plateforme, DSN, sélecteur d'identité, comptes partagés (#55).
- `docs/ownership.md` — primitive de ressource possédée (can_access/can_govern, tripwire owner_pairs, abolition du perso).
- `docs/email.md` — envoi per-org par connecteur (scaleway BYO TEM + resend), différé/quiet hours.
- `docs/event-loop-perf.md` — les 2 modes de gel mono-loop + protections + recettes py-spy/aiodebug.
- `docs/redaction.md` — **rédaction de champs** : middleware unique (FieldRedactionMiddleware), rien par défaut + templates 1-clic, **schéma OBSERVÉ** (capture passive `connector_schemas` — passthrough d'API tierces → on observe au lieu de déclarer), dry-run preview, moteur `FieldFilter` (oto-core).
