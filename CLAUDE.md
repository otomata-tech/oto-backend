# oto-mcp

MCP server (Streamable HTTP) qui expose les connecteurs **oto-core** (`oto.tools`,
importés directement — **plus aucune dép à la CLI**) comme tools, branchable dans
claude.ai et Claude Code. Public : `https://mcp.oto.ninja/mcp` (box Scaleway
dédiée — cf. §Infra).

**Positionnement : oto-mcp = le produit central, déployable** (SaaS hébergé OU
on-premise pour un client — image `Dockerfile`, config 100% par env). oto-cli =
façade locale basse priorité (fallback LinkedIn browser). Tout open source.

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=3.4.2` (plancher = dernier ; prod aligné au deploy via `pip install -e .`) + `mcp` SDK
- **`oto-core[browser]` PINNÉ sur un tag git** (`@ git+…@vX.Y.Z` dans `pyproject.toml`, plus `@main` flottant ni dép `oto-cli`) : une version déployée = coordonnée reproductible. ⚠️ **`pip` ne réinstalle PAS une dép VCS déjà présente** (`oto-core` "satisfait" quelle que soit sa version) → `pip install -e .` seul ne monte JAMAIS oto-core au tag bumpé. Le deploy **force-réinstalle** oto-core depuis le tag lu du `pyproject` (`pip install --force-reinstall …@$tag`). Bump connecteurs = tag oto-core + édit du pin + deploy (PAS de `git pull` box). Cf. ADR 0020. (⚠️ box `otomata-0` a un VIEUX oto-mcp décommissionné/stoppé avec un editable legacy `oto-cli` pré-split — ne pas s'y fier, le runtime live est la box dédiée.)
- `psycopg[binary]` + `psycopg-pool` (PostgreSQL managed Scaleway `otomata-main`, DB `oto_mcp`) pour le state par utilisateur — migré depuis SQLite le 2026-05-20. Row factory custom dans `db.py` (`_str_dict_row`) qui normalise `datetime`/`date` → strings "YYYY-MM-DD HH:MM:SS" : sinon `JSONResponse` crash sur `/api/me` car le code historique attend des strings comme avec SQLite.
- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)

## Architecture

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, _SERVER_INSTRUCTIONS, routes /api, tools
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── api_routes.py     # /api/me, /api/settings/*, /api/admin/* (CORS oto.ninja)
├── access.py         # rôles member/admin, resolve_api_key, quotas, status_for
├── db.py             # PG users + usage(sub, tool, day, count) — pool psycopg, DATABASE_URL
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

**Règle** : adaptateurs + runtime → dépendent du backend-core, **jamais l'inverse** ; et ils l'appellent **par interface** (`access.resolve_*`), pas par accès table croisé — pour qu'un seam puisse devenir un service (broker de credentials) sans réécriture. ✅ Le seam **résolution** (le candidat broker) est consolidé dans `access` : `resolve_api_key` / `resolve_remote_credential` / `resolve_crunchbase_session`. C'est la frontière qui doit rester nette (elle peut devenir un service). `tools/meta` (visibilité) et `tools/datastore` (partage) appellent `db` en direct, et **c'est OK** : par le principe ADR 0004 (« pas de discipline d'interface sans force ») ils ne sont pas des candidats-services → pas de reroute dogmatique.

### Couche capacité (`oto_mcp/capabilities/`, ADR 0009)

Pour les opérations exposées sur **deux faces** (MCP + REST), arrêter de câbler les adaptateurs 2× à la main (drift de surface + autz divergente — ex. `oto_use_org` jadis absent en REST, IDOR cross-org scout). Une **capacité** = un descripteur co-déclaré : `handler` core + `Input` pydantic (seule validation) + règle `authz` **obligatoire** + bindings `mcp`/`rest` (multi-binding possible). Les adaptateurs `_mcp_adapter`/`_rest_adapter` **bouclent** sur `registry.CAPABILITIES` et appliquent **validation → autz → handler** ; le refus est un `AuthzDenied` neutre traduit par chaque face (`McpError` / `json_error`+CORS). `authz` = combinateurs fermés (`SUB_ONLY`, `ORG_MEMBER`, `ORG_ADMIN`, `ORG_MEMBER_OF`, `PLATFORM_ADMIN`, `SUPER_ADMIN`, `NAMESPACE_GRANT`, `ORG_ADMIN_OF`, `GROUP_MEMBER_OF`, `GROUP_ADMIN_OF`) — `ORG_MEMBER`/`ORG_ADMIN` scopent l'**org active** (lecture/écriture self-service `/api/me/*`), les `*_OF(field)` une org/groupe ciblé par id de path. Schéma MCP **plat** via `apply_flat_signature` (gotcha pydantic single-param, cf. memory). Montés dans `server._build_mcp` + `api_routes.make_routes` (no-op si registre vide). **Domaines orgs + doctrine/instructions 100% migrés** : orgs (use_org, membres, secrets, create, entitlements, lectures) → `api_routes_orgs` réduit aux namespace-grants per-user ; doctrine (`capabilities/orgs_instructions.py` : get/list/set/delete/versions/revert/usage membre `/api/me/instructions*` + outils `oto_*_doctrine`, et palier admin cross-org `oto_admin_*_doctrine` / `/api/admin/orgs/{id}/instructions*`) — `tools/orgs.py` supprimé, bloc doctrine d'`api_routes.py` retiré. ⚠️ Handler async supporté par les deux adaptateurs (`inspect.isawaitable`) ; le manifeste `referenced_tools` (ADR 0014) résout l'instance FastMCP via `tool_registry.bind(instance)` (posé au boot dans `_build_mcp`). **Domaine user-admin migré** (`capabilities/users_admin.py`) : retrouver/lister un user (`oto_admin_list_users`, filtre `query`), fiche (`oto_admin_user_detail`, par email **ou** sub), rôle (`oto_admin_set_role`), grant de clé plateforme user **et** org (`oto_admin_grant_key`/`oto_admin_grant_org_key` + revoke), option payante comp (`oto_admin_set_option`) — les handlers REST écrits main correspondants ont été retirés d'`api_routes.py` (mêmes chemins servis par les capacités → dashboard inchangé). Donne la face MCP au **setup complet d'un compte depuis Claude**. Reste : autres domaines (tokens API, CRUD platform-keys). Forme de référence : `factgraph/` (scout, ADR 0008).

## Auth — Logto

JWT Logto **ES384** (défaut RS256 = tout rejeté), discovery RFC 9728 sur 401,
façade DCR self-service (`oauth_facade.py`) pour les clients sans DCR (Claude/ChatGPT/
Mistral). **Détail : `docs/auth-logto.md`** (gotchas, env, onboarding).

## Rôles + résolution de clé API

3 paliers `member < admin < super_admin` (accès admin UI). Résolution de clé par
appel : `user_key > group_secret > org_secret > platform_grant` (chemin platform
gaté sur `auth_modes`). **Détail : `docs/roles-and-resolution.md`** (paliers,
grants/quota, platform keys, providers byo-only).

**Seam substrat (ADR 0024)** : `access.resolve_credential(provider, want, sub?)` marche la cascade UNE fois → `ResolvedCredential{key, is_platform, mode, config, fields}` ; `resolve_api_key`/`resolve_credential_fields` = vues minces dessus (les ~15 tools keyed inchangés). `config` = **config non-secrète appariée à la clé gagnante** (endpoint/host : `dsn` unipile, `base_url` n8n/make, `data_center` zoho — `config_fields` `secret=False` ∪ meta public) → ne JAMAIS recâbler un résolveur d'endpoint par-connecteur. `access.credential_mode_for(sub, provider)` = le `mode` sans déchiffrer (détection BYO = `mode ∈ {user,group,org}`, jamais un check user-only).

## REST API (consommée par le dashboard / oto.ninja)

Endpoints `/api/*` (compte, settings, orgs, admin, billing, datastore…), même
`JWTVerifier` que `/mcp`. **Inventaire : `docs/rest-api.md`**.

## Browser automation — état réel : crunchbase in-process only (⚠️ délégation conteneur NON câblée)

⚠️ **La délégation à un conteneur o-browser-full (`OBROWSER_URL` / `RemoteBrowser` /
`ensure_session`) n'existe PAS dans ce backend** (`grep -r OBROWSER|RemoteBrowser
oto_mcp/` = 0). Elle a été décrite comme cible mais jamais portée sur la box dédiée
(le conteneur o-browser-full vit sur **tuls.me/otomata-0**, pas sur la box
`151.115.148.128`). État réel vérifié (2026-06-24) :

- **LinkedIn** : `tools/linkedin.py` **supprimé** — LinkedIn passe par **Unipile**
  (hébergé, connecteur `unipile`). Le browser LinkedIn local ne survit que dans
  oto-cli (fallback) et dans oto-core (`oto.tools.browser.linkedin/`).
- **Crunchbase** = **seul** connecteur browser (`providers.BROWSER_PROVIDERS={"crunchbase"}`),
  lancé **in-process** sur la box : `CrunchbaseClient(BrowserClient)` d'**o-browser**
  (`oto.tools.browser.crunchbase`, `headless=True`). Session (cookies JSON + UA) dans le
  **coffre chiffré**, résolue par `access.resolve_crunchbase_session()` → `db.get_crunchbase_session()`.
  ✅ **Réparé le 2026-06-24** : la box n'avait **aucun binaire navigateur** (crunchbase
  était cassé silencieusement). Installé **`google-chrome-stable`** (Chrome 149, `.deb`
  direct) → o-browser `_detect_channel()` renvoie `'chrome'`, lance le vrai Chrome.
  ⚠️ Install **manuelle, hors deploy** : un rebuild de box la perd → à câbler dans le
  provisioning (`apt-get install google-chrome-stable`, durable dans `/opt/google/chrome`).
  Chrome `--no-sandbox` requis (service `User=root`).
- **Conséquence** : harnais browser server-side = **browser in-process façon crunchbase**
  (la délégation conteneur reste à porter si l'isolation OOM devient nécessaire ;
  box 2 Go → préférer **browserless**/conteneur pour un browser permanent). ✅ **Une
  session privée cookie-bound SE transplante vers le serveur** (prouvé sur brevo le
  2026-06-24, cf. `tools/brevo.py`) : vrai Chrome box + **cookie d'auth httpOnly + UA
  d'origine** → **200**. C'est le modèle LinkedIn `li_at`. ⚠️ **Deux pièges** : (1) capter sur une
  session **vérifiée vivante** (cookie d'auth présent / fetch sanity = 200) — le faux
  négatif « auth cookie missing » venait d'une extraction sur **profil déconnecté** (un
  relancement à froid tombe sur `login.brevo.com`), PAS d'un souci de lecture httpOnly
  (`context.cookies()` les lit) ; le cookie d'auth de brevo=`auth` (httpOnly, **avec
  expiry**) ; (2) httpx ne marche **pas** (client non-navigateur → 403), transport =
  **browser-driven** (`page.evaluate(fetch())` / `RemoteBrowser`). Session d'origine potentiellement
  invalidée par l'usage serveur (li_at #5) → **session dédiée** conseillée.
- ✅ **Implémenté pour `brevo` via Browserbase** (`oto_mcp/browserbase.py`) : Chrome
  HÉBERGÉ (off-box, anti-OOM) + **Context** per-user (profil persistant = la session
  loguée, prouvé 200) + **Live View** pour le login interactif (SSO/captcha gérés par
  l'user) — pas d'export de cookie, pas de browser sur la box. Creds plateforme env
  `BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID`. Onboarding = `brevo_connect_start`/
  `brevo_connect_status`. C'est le substrat « pairing browser hébergé » réutilisable
  par tout connecteur d'API privée cookie-bound.

## LinkedIn cookies

⚠️ **Isolation de session (constaté 2026-06-04, issue #5 ouverte)** : injecter le
cookie `li_at` d'un user **côté serveur** (IP datacenter ≠ son IP) **déconnecte sa
propre session LinkedIn** (LinkedIn invalide/rotate le `li_at` partagé). Le vrai
Chrome règle l'empreinte TLS mais PAS ce partage de session. → l'outreach par un
user réel doit passer par une **session dédiée** (profil/VNC côté serveur, ou CLI
local sur son device), pas par son cookie injecté côté serveur. ✅ Le scraping
serveur est désormais **profil-only** (fallback cookie **supprimé**) et délégué au
conteneur (voir §Browser automation). #5 reste ouvert pour le pairing/CLI local.

Le couple `(li_at, user_agent)` est stocké par `sub` en PG. Le UA
matche le browser d'origine (capturé via `navigator.userAgent` au moment du
save) — sinon LinkedIn flag rapidement les sessions cookie/UA mismatch.

Si le user n'a rien configuré, les tools `linkedin_*` lèvent une `McpError`
qui pointe vers `https://app.oto.ninja/`.

Pour les non-tech : extension Chrome Oto Companion (repo `oto-app/extension/`,
MV3) qui capture le couple `(li_at, user_agent)` et le push automatiquement
via `POST /api/settings/linkedin` (auth Logto PKCE). Auto-resync via
`chrome.cookies.onChanged` quand LinkedIn rotate la session.

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

Spine plateforme de stockage structuré per-user (PG/JSONB natif, plus Google
Sheets). Surfaces : tools `data_*` (MCP) + REST `/api/datastore/*` ; OAuth Google
per-user (Gmail/Tasks, multi-compte) câblé ici. **Détail : `docs/datastore.md`**
(surfaces, OAuth multi-compte + scopes restricted/CASA, setup GCP, env vars).

## WhatsApp / Telegram / Instagram (messagerie via Unipile)

Tools `whatsapp_*` / `telegram_*` / `instagram_*` (`list_chats`/`read_chat`/
`send_message`) = messagerie **hébergée Unipile**, sous le connecteur `unipile`
(`modules`/namespaces = `unipile, whatsapp, telegram, instagram`). Générés par la
factory `tools/unipile.register_messaging_tools(mcp, channel)` — l'API `/chats`
d'Unipile est channel-agnostic ; chaque tool résout l'`account_id` du canal pour le
user (no-fallback, `tools/unipile.unipile_client(provider)`).

Connexion = hosted-auth Unipile (dashboard, `?channel=whatsapp|telegram|instagram`),
`account_id` per-user dans `unipile_accounts` (PK `(sub, provider)`). Même gate
d'abonnement par org que LinkedIn (cf. §Billing, prix gradué 15/10/7).

> **Baileys archivé** (ex-WhatsApp self-hosted) : wrappers backend retirés
> (`tools/whatsapp.py` réécrit Unipile, `pairing.py` + routes `/api/whatsapp/pair/*`
> supprimés). L'engine Baileys survit dans **oto-core** (`oto/tools/whatsapp/` + Node)
> + la **CLI `oto whatsapp`** (fallback).

> **Mode plateforme unipile** (revente) : `auth_modes` inclut `platform` → la clé
> Unipile se partage en **clé plateforme + grant** (pas de copie par org) ;
> `access.unipile_api_key_for` a le fallback platform-grant. Le gate abonnement reste
> par org (un grant donne la clé, ne bypasse pas le paiement). **« Offrir sans payer »
> = comp** : `db.set_option_comp("org", id, "unipile")` (débloque `access.has_option`).

> **DSN par credential + sélecteur d'identité (ADR 0024).** Chaque clé Unipile est liée
> à SON sous-domaine `api<NN>.unipile.com:port` ; le DSN vit dans le `meta` du credential
> et voyage avec la clé via `resolve_credential` (défaut env `UNIPILE_DSN`=api25, instance
> plateforme). Une clé BYO porte N comptes → capacités génériques **`connectors.identities`/
> `set_default_identity`** (REST `/api/connectors/{c}/identities[/default]`, registre
> `connector_identities.py` ; unipile = `list_accounts` sur clé+DSN, **valide id∈liste**
> anti-binding, **BYO-only** — en revente la liste est vide, hosted-auth conservé). Vue admin
> **sièges clé plateforme** `GET /api/admin/unipile/seats` (super_admin, `db.unipile_account_owners`) :
> réconcilie les comptes de l'instance partagée ↔ leur owner oto (flag **orphelin**).

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

## Onboarding (accueil au démarrage d'un compte)

`tools/onboarding.py` (spine, chargé explicitement dans `register_all`, hors gate
d'activation, **toujours visible** via `PROTECTED_TOOLS`) expose 2 méta-tools :
- `oto_onboarding()` (lecture) — sert l'explication d'Oto, l'**état découvert** du
  compte (`status_for` providers + memento + org + doctrine, best-effort), la fiche
  « situation avec oto » déjà remplie (`profile`) + les champs restants (`missing`),
  et un **script de self-onboarding** (`doctrine`) que l'agent déroule.
- `oto_onboarding_update(fields=…, onboarded=…)` — persiste les réponses (shallow-
  merge JSONB) et valide le booléan d'accueil.

`tools/whoami.py` (spine, chargé explicitement dans `register_all`, hors gate
d'activation, **toujours visible** via `PROTECTED_TOOLS`) expose `oto_whoami()`
(lecture) — l'**identité MCP courante** sous laquelle Claude agit : compte (`sub` +
email + rôle plateforme) × **org active** (id/name/rôle) × **groupe actif**, plus un
résumé des connecteurs configurés, l'état Memento et `onboarded`. C'est le pendant
agent du badge « identité MCP » du dashboard ; à appeler pour confirmer le contexte
avant une action sensible. Pour basculer : `oto_use_org`.

État en DB : table `user_account_profile(sub PK, onboarded bool, profile jsonb,
onboarded_at)` (`db.get_account_profile` / `db.update_account_profile`). Le booléan
gouverne « reprendre l'accueil à la session suivante ». Exposé sur `/api/me`
(`onboarding: {onboarded, updated_at}`, best-effort). Le serveur invite l'agent à
appeler `oto_onboarding()` en début de session tant que `onboarded` est faux
(`_SERVER_INSTRUCTIONS`). **Pas réservé aux comptes neufs** : un compte actif peut
le rappeler à tout moment (ré-explication, paramétrage), et rouvrir l'accueil via
`oto_onboarding_update(onboarded=False)`.

## Boucle d'usage (ADR 0017)

Flux d'événements de session unifié : calllog (involontaire) + feedback volontaire
d'agent (`feedback`, signal=tool_feedback|gap) + runs / déroulés (`run_start/finish`,
`doctrine` optionnel → doctrine nommée ou run one-shot). **Détail : `docs/usage-loop.md`**.

## Billing — credits d'appel par org (paiement Stripe)

Deux modèles **cumulables** : credits d'appel (1 appel = 1 credit, packs Stripe
one-off) + **abonnements récurrents par option** (`mode=subscription`, ex. messagerie
unipile, prix Stripe gradué). **Détail : `docs/billing.md`**.

## Visibility per-user

`UserDisabledToolsMiddleware` (`middleware.py`) applique au handshake `initialize` les visibility rules natives fastmcp (`disable_components` via `_visibility_rules` session state). Plus de filtrage manuel `on_list_tools`/`on_call_tool` — fastmcp émet `tools/list_changed` automatiquement quand les rules changent. Le **calcul** de la denylist `(sub, org active)` + son application vivent dans **`session_visibility.py`** (`compute_hidden_tools` / `apply_session_visibility(ctx, sub, *, reset=…)`), partagés entre le middleware (handshake) et le **refresh à chaud** post-bascule.

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). Table sœur `user_presets(sub, name, enabled_tools[])` pour les snapshots nommés.

**Masqués par défaut** (`is_default_hidden`) : invisibles par défaut sur la surface authentifiée, **self-activables** (≠ grant-only). Deux grains : `tool_visibility.py::DEFAULT_HIDDEN_TOOLS` (noms individuels) et `DEFAULT_HIDDEN_NAMESPACES` (namespaces entiers, **dérivé du registre** — champ `default_hidden` de `connectors.py`). Cas actuel : **`attio_*`** (le MCP Attio officiel est préféré ; code conservé pour implems custom). Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué-par-défaut > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève, `apply_preset` le réplique (même logique côté REST `/api/me/tools/{name}`). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Masquer un connecteur entier = poser `default_hidden=True` au registre ; un tool isolé = `DEFAULT_HIDDEN_TOOLS`.

Méta-tools exposés (`tools/meta.py`) : `oto_list_my_tools`, `oto_disable_tool`, `oto_enable_tool`, `oto_list_presets`, `oto_save_preset`, `oto_apply_preset`, `oto_delete_preset`. Le set protégé `{oto_list_my_tools, oto_enable_tool, oto_apply_preset}` reste toujours activé pour éviter le lock-out.

`oto_save_preset` (et `POST /api/me/presets/{name}`) accepte 2 modes : snapshot (par défaut, capture l'état courant) ou explicit (param `enabled_tools=[...]`, sauve sans altérer l'état courant — utile pour provisionner par script).

**Refresh à chaud de la toolbox sur bascule de profil** : une capacité qui change le profil de visibilité déclare `refresh_visibility=True` (`Capability`) ; l'adaptateur MCP (`capabilities/_mcp_adapter.py`) rejoue alors `apply_session_visibility(reset=True)` sur la session **courante** après le handler → `tools/list_changed` live. Posé sur `org.use_org`/`org.clear`/`org.create`/`org.set_home` + `group.use`/`group.clear`/`group.set_home`. Donc **`oto_use_org <org>` recharge la toolbox dans la conversation en cours** (les credentials, eux, basculent déjà — `resolve_api_key` relit l'org **via le seam `current_org`** à chaque appel, cf. §ADR 0023 ci-dessous).

**Limite connue** : ça ne vaut QUE pour la face MCP (même session). Un toggle/bascule via **REST** (dashboard) passe par une connexion séparée → ne notifie pas une conversation Claude déjà ouverte (visible à la prochaine session). Pousser dashboard→session MCP demanderait un registre `sub → sessions actives` + push hors-requête (non fait).

## Org/équipe : session vs maison vs consultation (ADR 0023, amende 0015)

Le pointeur unique « org active » est scindé en **3 notions**, résolues par le **seam unique `access.current_org(sub)`** (mirroir `access.current_group(sub)` pour l'équipe) = `session ?? consultation ?? maison`. **TOUTE résolution d'action passe par ce seam** (`resolve_api_key`, visibilité `session_visibility`, entitlements, field-filters, billing, doctrine de groupe, `/api/me`, whoami, et l'injection `org_id` des règles d'autz `_authz`) — ne plus lire `org_store.get_active_org` en direct dans un chemin de résolution.

⚠️ **Ce seam est scopé sur l'ACTEUR courant** : session/consultation sont stockées **par requête**, le `sub` ne sert qu'au repli `home_org`. Donc `current_org(autre_sub)` renvoie le contexte du **requérant**, pas du tiers — **NE JAMAIS** l'utiliser (ni `status_for`/`has_option`/`credential_mode_for` qui en dérivent) pour calculer l'état d'un **tiers** (écran admin). Passer son org/groupe **explicitement** via le kwarg `org`/`group` (sentinelle `access._UNSET` = défaut `current_org`, self inchangé), source = `org_store.get_active_org(target)`. Bug vécu 2026-06-24 (fiche admin montrant l'option de l'org du requérant). L'état d'un user est par ailleurs souvent **per-org** (∈ N orgs) → préférer une vue par org (cf. `tools/unipile.admin_status_by_org`).

- **Org de session** (éphémère, MCP) — override posé par `oto_use_org`/`oto_clear_org` (devenus **session-scopés**, ne touchent plus la colonne) dans `session_org.py` (store sync keyé par `ctx.session_id` — `get_state` async est inutilisable depuis `resolve_api_key` sync). Meurt avec la conversation ; repose sur l'isolation des sessions claude.ai par conversation. **Pas de jeton rejoué par appel** (bracelet serveur, pas de discipline LLM).
- **Org maison** (`org_store.get_active_org`, ex-« active_org ») — défaut persistant des **nouvelles** conversations. Posée explicitement : `oto_set_home_org` (MCP) ou `PUT /api/me/active-org` (REST/dashboard) ; **jamais** par navigation dashboard.
- **Org de consultation** (REST, view-as) — header `X-Oto-Org` (équipe : `X-Oto-Group`), posé par le **middleware ASGI `api_routes.ViewAsMiddleware`** (brut, n'altère pas le streaming `/mcp`) APRÈS **validation d'appartenance** (anti-IDOR : `roles.is_org_member`/`can_read_group`) dans un contextvar lu par `current_org`. Le dashboard consulte n'importe quelle org **sans muter l'identité MCP**.
- **« Voir en tant que » (axe USER, REST, lecture seule)** — header `X-Oto-View-As=<sub>` posé par le même `ViewAsMiddleware`, gaté **opérateur plateforme + cible existe + méthode GET** (mutations → 403 `view_as_read_only`). `_authenticate` renvoie alors le **sub cible** (param `apply_view_as`, contextvar `session_org.current_view_user`) → tout `/api/me/*` (capacités incluses) rend la vue de la cible. **REST-only** : le MCP ne lit jamais ce contextvar (zéro impersonation dans Claude). Front : bouton sur la fiche admin + bandeau `ViewAsBanner` (`lib/viewOrg.ts`).

**Invariant groupe⊂org dérivé** : un override/consultation d'org **sans** groupe explicite ⇒ niveau org (jamais le `home_group` d'une autre org) ; toute bascule d'org de session retire l'override de groupe. `/api/me` expose `active_org`/`active_group` (effectifs) **et** `home_org`/`home_group` (défauts) distinctement. `oto_whoami` montre l'org effective + `scope: home|session`.

## Doctrines & instructions d'org

Prose opératoire métier par org (skills à la Claude Code, slug + versionnée),
servie au début de session par `oto_get_doctrine()`. **Détail : `docs/doctrines.md`**.

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

Un groupe **gouverne 3 ressources** par délégation de l'org (pas les entitlements,
restés org-level) :
- **secrets partagés** — coffre `connector_credentials` (entity_type='group') ;
  cascade `resolve_api_key` = **user_key > secret groupe actif > secret org active > grant plateforme**.
- **doctrine & skills** — `org_group_instructions` (+ revisions) ; `oto_get_doctrine()`
  sert org **puis** groupe actif (complément, chaque skill taggée `scope`).
- **preset de toolset** — `org_groups.default_tools` (NULL = pas de baseline) ;
  baseline de visibilité au handshake (les toggles perso priment, **jamais**
  d'élévation d'un grant-only).

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
memento — systématique) vs **remote** (bridge data-driven ADR 0003, token M2M d'org,
pilote movinmotion). **Détail : `docs/federation.md`**.

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
  try/except (un connecteur cassé ne fait pas tomber le serveur). `meta`/`orgs`/`scout`
  (spine) + `remote`/`mount` (génériques) restent chargés explicitement. ⚠️ Le
  namespace déclaré doit matcher `namespace_of(tool)` (1er token avant `_`) — pas de
  namespace multi-mot (`culture_spectacle`→`culture`), sinon fail-open du gate.
  Le garde-fou `test_tools_module_derivation_matches_filesystem` (`tests/test_capabilities_drift.py`)
  est **auto-maintenu** (croise `tools/*.py` au registre) — ajouter un connecteur
  (fichier + entrée registre) le garde vert SANS rien y toucher ; il casse seulement
  sur un **fichier orphelin** (connecteur posé mais pas déclaré → dort invisible) ou un
  **module fantôme** (faute dans `modules=`/nom). Seul un **module spine** chargé
  explicitement (rare) s'ajoute à `_EXPLICIT_TOOL_MODULES`. ⚠️ **Aucune CI de test sur
  les PR** (`gh pr checks` = vide ; seul `deploy.yml` tourne sur push main) → un test
  rouge atterrit sur `main` sans rien bloquer. Lancer les tests à la main.
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
- **Connecteur client-sensible = JAMAIS de code ici** : connecteur **remote** défini
  par la DONNÉE (ADR 0003/0011) — un credential d'org avec `meta.base_url` (endpoint
  du bridge) suffit, **zéro nom client au registre** (plus de `_c("mm")`). Découvert
  au boot (`credentials_store.list_remote_namespaces`, gracieux si DB indispo), servi
  par le générique `tools/remote.py` (`<ns>_describe`/`<ns>_call`) ; le credential
  d'org **EST** le grant (`granted_namespaces_for` + grant-only runtime). Le bridge
  distant détient le credential client (token M2M). Pilote :
  movinmotion-backoffice-bridge. Cf. ADR 0003. **Et JAMAIS dans une surface anonyme** :
  les catalogues publics (`/api/connectors` sans bearer, `/api/mcp/catalog`
  → pages oto.ninja/tools) filtrent les `platform_granted`/grant-only
  (deny-by-default, miroir de la face MCP) — fuite vécue 2026-06-13
  (page marketing /tools/mm).
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

# Deploy — push main déclenche `.github/workflows/deploy.yml` : workflow unique
# CI/CD (job `test` pytest → job `deploy` `needs: test`). Deploy = SSH box dédiée :
# git reset --hard origin/main + pip install -e . + **force-reinstall oto-core
# depuis le tag pinné** (lu du pyproject ; pip saute sinon une dép VCS déjà
# présente) + restart + **smoke HTTP** (GET 200 /.well-known/oauth-authorization-server)
# + **rollback auto** vers le commit précédent si install/restart/smoke échoue. Le
# restart relance start-encrypted (refetch master key). ⚠️ start-encrypted.sh
# untracked → survit au git reset.
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

# ⚠️ Déchiffrer un credential ad-hoc (crypto.decrypt / _reveal / credential_status) :
# `OTO_MCP_MASTER_KEY` n'est PAS dans .env — start-encrypted.sh la fetch au boot
# depuis Scaleway Secret Manager. Un script qui ne source que .env voit
# `encryption_enabled()=False` → tous les déchiffrements lèvent RuntimeError (FAUX
# négatif, ≠ InvalidTag). Pour reproduire le runtime, répliquer le fetch :
#   set -a; . .env; . /etc/oto-mcp/scw.env; set +a
#   RESP=$(curl -s -H "X-Auth-Token: $SCW_SECRET_KEY" \
#     ".../secret-manager/v1beta1/regions/fr-par/secrets/<id>/versions/latest_enabled/access")
#   export OTO_MCP_MASTER_KEY=$(echo "$RESP" | python3 -c 'import json,sys,base64; print(base64.b64decode(json.load(sys.stdin)["data"]).decode())')
# Vécu 2026-06-22 (triage Sentry InvalidTag : 1 ligne memento corrompue, écrite
# avec une clé ≠ courante — les autres lignes déchiffraient → pas un souci de clé ;
# fix = purge → re-OAuth). `status_for` doit utiliser `credential_status` (présence
# sans déchiffrer), jamais `get_credential_with_meta`, pour ne pas 500 /api/me.
```

## Infra

Déployé sur une **box Scaleway dédiée** (ADR 0002, depuis 2026-06-11) : oto-backend isolé + Caddy + chiffrement du coffre actif, sert `mcp.oto.ninja`. **DB** = PostgreSQL managé partagé (`otomata-main`, DB `oto_mcp`). Le coffre `connector_credentials` est chiffré au repos (AES-256-GCM, master key en Secret Manager fetchée au boot, 0 plaintext). Object Storage S3 pour avatars/logos (`media_store.py`).

> **Détails machine = repo privé `otomata-tech/infra`** (IPs, IDs de secrets/zone/instance, systemd, runbook deploy, env de process) — pas ici (ce repo est public). Voir `infra/docs/oto-platform-state.md` + docs ciblés (`scaleway-managed-db.md`, `caddy.md`, `cloudflare.md`, `deploy-keys.md`). Toute intervention prod = skill `prod-init`.

## Docs

- `docs/connector-model.md` — **carte d'ensemble** : les **3 couches** d'un connecteur (disponibilité / authentification / abonnement), la matrice des niveaux (user/groupe/org/plateforme), le vocabulaire canonique, le seam `access.has_option`. **À lire en premier** avant de toucher activation/clés/abonnement (les autres docs ci-dessous = le détail par couche).
- `docs/connector-vault.md` — **archi centrale** : registre source unique (`connectors.py`), coffre chiffré unique `connector_credentials` (clés API + platform_keys + sessions linkedin/crunchbase/google multi-compte), enveloppe AES-256-GCM **obligatoire** (pas de plaintext), résolution + palier org. À lire avant de toucher credentials/registre/résolution.
- `docs/roles-and-resolution.md` — rôles (3 paliers) + cascade de résolution de clé / grants / platform keys.
- `docs/billing.md` — credits d'appel (packs) + abonnements récurrents par option (Stripe).
- `docs/doctrines.md` — doctrine & skills d'org (oto_get_doctrine, versionnée).
- `docs/auth-logto.md` — auth Logto ES384, discovery RFC 9728, façade DCR.
- `docs/rest-api.md` — inventaire des endpoints REST `/api/*`.
- `docs/federation.md` — fédération MCP : mount (per-user) vs remote/bridge (org).
- `docs/usage-loop.md` — boucle d'usage ADR 0017 (calllog + feedback + déroulés).
- `docs/monitoring.md` — monitoring des appels MCP (tool_call_log + surface admin).
- `docs/datastore.md` — datastore spine PG (`data_*`) + OAuth Google per-user (setup GCP, scopes).
- `docs/groups-and-roles.md` — groupes/départements & hiérarchie de droits (ADR 0012).
- `docs/redaction.md` — **rédaction de champs** : middleware unique (FieldRedactionMiddleware), rien par défaut + templates 1-clic, **schéma OBSERVÉ** (capture passive `connector_schemas` — passthrough d'API tierces → on observe au lieu de déclarer), dry-run preview, moteur `FieldFilter` (oto-core).
