# oto-mcp

MCP server (Streamable HTTP) qui expose les connecteurs **oto-core** (`oto.tools`,
importés directement — **plus aucune dép à la CLI**) comme tools, branchable dans
claude.ai et Claude Code. Public : `https://mcp.oto.ninja/mcp` (box dédiée
`oto-platform` REDACTED_IP, port 9103 — cf. §Infra).

**Positionnement : oto-mcp = le produit central, déployable** (SaaS hébergé OU
on-premise pour un client — image `Dockerfile`, config 100% par env). oto-cli =
façade locale basse priorité (fallback LinkedIn browser). Tout open source.

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=3.4.2` (plancher = dernier ; prod aligné au deploy via `pip install -e .`) + `mcp` SDK
- `oto-cli[browser]` — déclaré comme dépendance PyPI dans `pyproject.toml`, mais en
  prod le venv est overridden par `pip install -e /opt/oto-cli/` (clone du repo
  `otomata-tech/oto-cli` sur le serveur). Permet de propager les nouveaux connecteurs
  sans release PyPI — un `git pull` côté serveur suffit. La dépendance PyPI reste
  pour les déploiements fresh (premier install du venv).
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

oto-mcp porte aujourd'hui 4 métiers ; ils sont des **couches à frontière à sens unique** ([ADR 0004](../docs/adr/0004-layered-reversible-topology.md)) :

- **backend-core** (le centre) : `db`, `credentials_store`, `org_store`, `access`, `crypto`, `connectors`, `auth_hooks`. Identité (`sub`), coffre, orgs, grants/quotas, résolution.
- **adaptateur MCP** : `server`, `tools/*`, `middleware`, `tool_visibility`.
- **adaptateur REST** : `api_routes`.
- **runtime connecteurs** : `tools/*` (in-process) + `tools/remote` (forward bridges).

**Règle** : adaptateurs + runtime → dépendent du backend-core, **jamais l'inverse** ; et ils l'appellent **par interface** (`access.resolve_*`), pas par accès table croisé — pour qu'un seam puisse devenir un service (broker de credentials) sans réécriture. ✅ Le seam **résolution** (le candidat broker) est consolidé dans `access` : `resolve_api_key` / `resolve_remote_credential` / `resolve_crunchbase_session`. C'est la frontière qui doit rester nette (elle peut devenir un service). `tools/meta` (visibilité) et `tools/datastore` (partage) appellent `db` en direct, et **c'est OK** : par le principe ADR 0004 (« pas de discipline d'interface sans force ») ils ne sont pas des candidats-services → pas de reroute dogmatique.

### Couche capacité (`oto_mcp/capabilities/`, [ADR 0009](../docs/adr/0009-couche-capacite.md))

Pour les opérations exposées sur **deux faces** (MCP + REST), arrêter de câbler les adaptateurs 2× à la main (drift de surface + autz divergente — ex. `oto_use_org` jadis absent en REST, IDOR cross-org scout). Une **capacité** = un descripteur co-déclaré : `handler` core + `Input` pydantic (seule validation) + règle `authz` **obligatoire** + bindings `mcp`/`rest` (multi-binding possible). Les adaptateurs `_mcp_adapter`/`_rest_adapter` **bouclent** sur `registry.CAPABILITIES` et appliquent **validation → autz → handler** ; le refus est un `AuthzDenied` neutre traduit par chaque face (`McpError` / `json_error`+CORS). `authz` = 6 combinateurs fermés (`SUB_ONLY`, `ORG_MEMBER`, `ORG_MEMBER_OF`, `PLATFORM_ADMIN`, `NAMESPACE_GRANT`, `ORG_ADMIN_OF`). Schéma MCP **plat** via `apply_flat_signature` (gotcha pydantic single-param, cf. memory). Montés dans `server._build_mcp` + `api_routes.make_routes` (no-op si registre vide). **Domaine orgs 100% migré** (use_org, membres, secrets, create, entitlements, lectures) → `api_routes_orgs` réduit aux namespace-grants per-user ; reste : doctrine/instructions + autres domaines. Forme de référence : `factgraph/` (scout, ADR 0008).

## Auth — Logto

Le backend valide les bearer JWT émis par `auth.oto.zone/oidc`. Sur 401, le
header `WWW-Authenticate` pointe vers `/.well-known/oauth-protected-resource/mcp`
(RFC 9728) ce qui amorce le discovery OAuth côté client MCP.

**Gotcha** : Logto self-hosted signe en `ES384` (P-384 ECDSA). Le default de
`JWTVerifier` est RS256 → tous les tokens rejetés. Vérifié sur
`GET /oidc/jwks`.

Logto self-hosted n'expose pas DCR → les apps Claude sont pré-créées dans le
tenant et leur `client_id` est collé à la main dans le connector Claude.

**Onboarding actuel = self-serve ouvert.** Le tenant a sign-up activé par
email magic link, sans allowlist. Quiconque trouve l'URL peut s'inscrire,
mais c'est sans risque pour les clés serveur car les platform keys ne sont
accessibles qu'avec un grant explicite (cf. `access.py`).

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`,
`OTO_MCP_ADMIN_SUB` (Logto sub d'Alexis pour bootstrap admin).

## Rôles + résolution de clé API

> ⚠️ Le **stockage** des credentials est le **coffre chiffré unique `connector_credentials`** (cf. `docs/connector-vault.md`). Les colonnes legacy `users.<provider>_api_key`/`org_secrets`/`user_google_oauth` ont été **purgées** (DROP, 2026-06-11) ; chiffrement **obligatoire** (plus de plaintext). La résolution ci-dessous reste valide dans sa cascade, lit le coffre via `credentials_store`.

Le rôle (`users.role`) ne sert qu'à décider qui voit l'admin UI :

- **admin** : accès `/api/admin/*`. Bootstrap via env `OTO_MCP_ADMIN_SUB`.
- **member** : défaut, pas d'effet sur l'accès aux tools (`guest` retiré 2026-06-15, migré → member ; `ROLES = (member, admin)`).

L'accès aux clés API se décide par `user_grants` explicites (admin grante
manuellement via `/api/admin/users/{sub}/grants/{key_id}`). Résolution par
appel (`resolve_api_key`) :

1. User key posée sur `/account` → prise directement, sans quota.
2. Grant explicite dans `user_grants` → platform key avec quota.
3. Ni l'un ni l'autre → McpError actionnable pointant vers `/account`.

Quota daily per-grant : colonne `user_grants.daily_quota` (posé par l'admin
au moment du grant). Si NULL, fallback sur env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`
ou `_QUOTA_DEFAULTS` dans `access.py`. User key bypass quota.

**Les platform keys vivent en DB uniquement** (coffre `platform_keys` — plus de
bootstrap SOPS/env au boot, oto-mcp#12). Poser/roter une clé = surface admin :
REST `POST /api/admin/platform-keys` ou meta-tool `oto_admin_set_platform_key`
(rotation = re-poser même provider+label ; label historique servi par
`resolve_api_key` = `env`). Poser ≠ granter : l'admin accorde l'accès au cas
par cas. Modèle : user key (prio, no quota) OU platform key + grant + quota OU
erreur. **Seuls les providers `platform`-éligibles au registre (`auth_modes`
inclut `platform` : `serper/hunter/sirene/kaspr`) peuvent avoir une clé
plateforme** — `resolve_api_key` **gate** le chemin platform-grant sur
`auth_modes` (audit 2026-06-11). Les comptes **privés / byo-only**
(`attio/lemlist/pennylane/fullenrich/slack`) **n'ont PAS de clé plateforme** :
les clés résiduelles du seed SOPS ont été supprimées, et le compte partagé de
l'**équipe Otomata** (attio/lemlist) vit en **credentials de l'org Otomata
(byo_org, org id 2)** — accès par appartenance, pas par grant plateforme.
**Slack** : pas de `SLACK_API_KEY`, le provider porte le **user token**
(`xoxp`) per-user — `slack_*` postent en `as_user` (mode bot viendra avec
l'OAuth install, issue #4).

**Débranchement SOPS (oto-mcp#12)** : l'unit pose `OTO_CONFIG_DISABLE_SOPS=1`
→ côté serveur, `oto.config.get_secret` ne résout QUE l'env du process (ni
SOPS ni `~/.otomata/secrets.env`), et tout `require_secret` résiduel échoue
fort. L'infra bootstrap (DATABASE_URL, Logto, OAuth Google, state secret)
reste en env de process (`/opt/oto-mcp/.env`).

Tous les tools API-keyed (`serper_*`, `hunter_*`, `sirene_*`, `fr_*`,
`attio_*`, `pennylane_*`, `slack_*`…) appellent `resolve_api_key(provider)`.
LinkedIn, WhatsApp et Datastore ne sont pas concernés (cookie/session/oauth
per-user).

## REST API (consommée par oto.ninja /account)

- `GET /api/me` — profil + role + statut LinkedIn + statut providers (mode/key/quota) + `active_org`/`active_org_name`/`org_role`
- `POST|DELETE /api/settings/linkedin` — cookie li_at + UA
- `POST|DELETE /api/settings/api-keys/{serper|hunter|sirene}` — user key
- `GET /api/me/tools` + `POST|DELETE /api/me/tools/{name}` — toggle individuel d'un tool MCP
- `GET /api/me/presets` + `GET|POST|DELETE /api/me/presets/{name}` + `POST /api/me/presets/{name}/apply` — presets nommés de toolset (cf. §Visibility)
- `GET /api/me/instructions` (doctrine de base meta + index) + `GET|PUT|DELETE /api/me/instructions/{slug}` + `GET /api/me/instructions/{slug}/versions` + `POST /api/me/instructions/{slug}/revert` — doctrine & instructions de l'**org active** (cf. §Doctrines). Lecture = membre ; écriture = `org_admin` (ou platform admin). Édité par la SPA `account/` (section « doctrine »).
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- `POST /api/admin/users/{sub}/grants/{key_id}` body `{daily_quota}` — set/update quota par grant (admin only)
- `GET|POST /api/admin/users/{sub}/tokens` + `DELETE /api/admin/users/{sub}/tokens/{token_id}` — issue/list/revoke tokens API on behalf of a user (admin only)
- `GET /api/admin/monitoring/summary?days=` + `GET /api/admin/monitoring/calls?limit=&sub=&tool=&errors=&days=` — journal des appels MCP, agrégats + brut (admin only, cf. §Monitoring)
- **Palier org** (`api_routes_orgs.py`, projection 1:1 des meta-tools `oto_admin_*org*` / `oto_list_orgs`) :
  - self-service : `GET|POST /api/me/orgs` (**`POST` = `org.create` self-serve**, créateur→org_admin, cap `OTO_MCP_MAX_ORGS_PER_USER`) ; `GET /api/orgs/{id}` ; `POST|DELETE /api/orgs/{id}/members[/{sub}]` + `PUT|DELETE /api/orgs/{id}/secrets/{provider}` (org_admin)
  - **invitations** (onboarding SaaS) : `POST|GET /api/orgs/{id}/invitations` + `DELETE …/{inv}` (org_admin) ; `POST /api/me/invitations/accept` (`SUB_ONLY`, match email vérifié + expiry). Email via `oto_mcp/email.py` (otomata-mailer `mailer.oto.zone/api/send`, env `OTO_MAILER_SEND_BEARER`, best-effort → `invite_url` en repli ; **plus de Resend**).
  - **fiche admin user** : `GET /api/admin/users/{sub}` = identité + accès effectif par provider (`status_for`) + grants + namespaces + orgs (membership).
  - platform admin : `GET|POST /api/admin/orgs`, `GET /api/admin/orgs/{id}` (+ entitlements), `…/members*`, `…/secrets/{provider}`, `POST|DELETE /api/admin/orgs/{id}/entitlements/{namespace}`, `GET /api/admin/namespace-grants`, `POST|DELETE /api/admin/users/{sub}/namespace-grants/{namespace}`
  - secrets : jamais la clé en réponse (provider/base_url/set_at/set_by) ; providers per-user (slack/linkedin/google/whatsapp) refusés en `400` ; listing lu du coffre canonique `credentials_store` (legacy `org_secrets` plus dual-written sous chiffrement). Gating org_admin/membre via `org_store.get_org_role` (platform admin toujours autorisé). Révocation lazy sur sessions MCP ouvertes. Contrat front : `oto-app/docs/ORG_API_CONTRACT.md`.
- CORS : `oto.ninja`, `app.oto.ninja`, `dashboard.oto.ninja` (+ localhosts dev) — défaut dans `_allowed_origins`, override `OTO_MCP_CORS_ORIGINS`. `account.oto.zone` retiré (surface compte décommissionnée → dashboard.oto.ninja)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## Browser automation — délégué à o-browser-full (issue oto-app#11)

- oto-mcp **ne lance plus Chrome in-process**. `tools/linkedin.py` délègue au conteneur **o-browser-full** (Docker, `OBROWSER_URL` défaut `http://127.0.0.1:8080`, cappé `--memory 2.5g` sur la même box) → un OOM browser ne touche pas `/api/me` (découplage **cgroup**, pas machine).
- Flux : `RemoteBrowser.ensure_session(OBROWSER_URL, "linkedin-<sub>")` → `cdp_url` → `LinkedInClient(cdp_url=…)`. Session fermée après chaque scrape (`DELETE /api/sessions/current`) — **option A** : 1 Chrome/conteneur, verrou global `_BROWSER_LOCK`.
- Profils dans le **volume conteneur** `/var/lib/o-browser/profiles/linkedin-<sub>` (override `OBROWSER_PROFILES_DIR`), partagé avec le pairing. `linkedin_pairing.has_profile` (check **FS**, suit les symlinks) = source de vérité de `/api/me` (le `GET /api/profiles` du conteneur, lui, filtre les symlinks).
- Dépend de **`o-browser>=0.4.0`** (RemoteBrowser `profile`/`ensure_session`). Publier o-browser = tag `vX.Y.Z` → CI PyPI (trusted publishing).
- Reste in-process (à migrer) : **Crunchbase** (`tools/crunchbase.py`) et le **pairing** LinkedIn (rare, supervisé).

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

## SIRENE stock (DuckDB sur parquet INSEE)

Stock complet (~35M établissements, parquet ~2GB) accessible via DuckDB :
- Path canonique : `/opt/oto-mcp/data/sirene/StockEtablissement.parquet` (env `SIRENE_STOCK_PARQUET_PATH`)
- Refresh mensuel via `deploy/refresh_sirene_stock.sh` (cron sur tuls.me)
- Query layer : `france_opendata.sirene_stock` (lib partagée PyPI `france-opendata[stock]`, ex-`oto_mcp/sirene_duckdb.py` — déplacé pour être consommé aussi par les apps co-localisées, ex. tuls)
- 4 MCP tools `sirene_stock_*` (siege, etablissements, siret, search)
- 5 REST endpoints `/api/sirene/{siege,etablissements,siret,search,info}`
- Consommé par `oto-cli` (`SireneStock` HTTP client) — voir [ADR 0001](../docs/adr/0001-sirene-stock-served-via-mcp.md) dans le meta-repo `otomata`

## Datastore (Google Sheets per-user)

Stockage structuré léger par user. Backend = un Google Sheet par "namespace"
(timetrack, todos, courses…) dans le Drive du user. Schéma libre : les
colonnes apparaissent quand de nouveaux champs sont écrits. Les 3 premières
colonnes sont auto-managées (`_id` UUID v7-like, `_created_at`, `_updated_at`).

Surfaces :
- MCP tools `data_*` (`data_create_namespace`, `data_append`, `data_list`,
  `data_get`, `data_update`, `data_delete_row`, `data_url`, etc.) — pour
  Claude.ai / Claude Code.
- REST `/api/datastore/*` — pour le CLI `oto data` + future UI.

Auth :
- MCP tools : Logto JWT comme les autres tools.
- REST `/api/datastore/*` : Logto JWT **ou** API token long-lived (préfixe
  `oto_`, vérifié contre `user_api_tokens`).

OAuth Google per-user (flow unifié Sheets+Drive+Gmail, **multi-compte**) :
- `GET /api/google/oauth/start` (Logto auth) → renvoie `{auth_url}` à
  ouvrir dans le browser. `prompt=consent select_account` → l'user choisit
  quel compte Google connecter (rejouer le flow ajoute un 2e compte).
- `GET /api/google/oauth/callback?code=…&state=…` — Google redirige ici, on
  échange, dérive l'email du compte via le profil Gmail, persiste, puis
  redirige vers `app.oto.ninja/?datastore=connected`.
- `GET /api/google/oauth/status` → `{connected, accounts:[{email,is_default,scopes,granted_at}], …}`.
- `POST /api/google/oauth/default` body `{account}` → choisit le compte par défaut.
- `DELETE /api/google/oauth[?account=<email>]` → révoque un compte (ou tous).
- Scopes : `spreadsheets` + `drive.file` + `gmail.modify` + `tasks`.
- Multi-compte : dans le coffre `connector_credentials` (connector='google',
  `account=email`, `is_default` dans meta). Le datastore et les tools `gmail_*`
  sans param `account` utilisent le compte par défaut (cf. `db.set_google_oauth`,
  `docs/connector-vault.md`).
- Refresh token **chiffré** (`secret_enc`) dans le coffre. access_token reste en
  clair dans `meta` (bearer ~1h, dérivé).

**Pourquoi un client OAuth séparé du connecteur Logto Google** : Logto
gère l'**identité** (scopes `openid email profile`), pas la délégation
d'accès aux ressources Google. Donc deux clients OAuth distincts dans le
même projet GCP — séparation propre identité ≠ délégation.

⚠️ **Conséquence de l'ajout de Gmail** : `gmail.modify` est un scope
**restricted** Google (contrairement à `drive.file`, non-sensible). Tant que
l'écran de consentement est en mode *Testing* (test users only), pas de
contrainte. S'il passe en *published/external*, Google impose un audit
sécurité annuel (CASA). Le flow étant unifié, **tout** user qui connecte
Google pour le datastore se voit aussi demander l'accès Gmail. Choix assumé
(substrat unique vs deux flows séparés).

### Setup GCP (one-shot, par projet)

1. **Console GCP** → choisir/créer un projet (peut être le même que celui
   qui héberge le connecteur Logto Google).
2. **APIs & Services → Library** : enable
   - `Google Sheets API`
   - `Google Drive API`
   - `Gmail API`
3. **APIs & Services → OAuth consent screen** :
   - User type : `External` (sauf Workspace)
   - App name : `Oto Datastore` (visible aux users sur le consent)
   - Support email : alexis@otomata.tech
   - Authorized domains : `oto.ninja`
   - **Scopes** : `.../auth/spreadsheets`, `.../auth/drive.file`,
     `.../auth/gmail.modify`, `.../auth/tasks`
   - **API à activer** : ajouter aussi `Google Tasks API` dans APIs & Services → Library
   - **Test users** (si en mode "Testing") : ajouter les emails autorisés
     tant que l'app n'est pas publiée. ⚠️ `gmail.modify` est un scope
     **restricted** → en mode Testing c'est OK, mais publier l'app en
     External imposerait un audit sécurité CASA annuel (cf. section OAuth
     ci-dessus). `drive.file` reste non-sensible ; c'est Gmail qui ajoute
     la contrainte.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID** :
   - Application type : **Web application** (pas "Desktop")
   - Name : `oto-mcp datastore`
   - Authorized redirect URIs :
     - `https://mcp.oto.ninja/api/google/oauth/callback` (prod)
     - `http://localhost:9103/api/google/oauth/callback` (dev, optionnel)
5. Copier `client_id` + `client_secret` → SOPS.
6. Générer le state secret :
   ```bash
   python3 -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

### Env vars requises

À poser dans le `.env` systemd (ou SOPS exporté au boot) :

- `GOOGLE_DATASTORE_CLIENT_ID` / `GOOGLE_DATASTORE_CLIENT_SECRET` — issus
  de l'étape 5.
- `OTO_MCP_OAUTH_STATE_SECRET` — étape 6, HMAC anti-CSRF du state.
- `OTO_MCP_PUBLIC_URL` — déjà utilisée pour Logto (base du redirect URI).
- `OTO_APP_URL` (optionnel, défaut `https://app.oto.ninja`) — base où on
  redirige l'user après le callback OAuth. À override en dev local
  (`http://localhost:5174`).

Bootstrap d'un token CLI (pour Alexis) :
```bash
ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cd /opt/oto-mcp && ./.venv/bin/python -m scripts.issue_token <SUB> cli"
# → imprime un `oto_…` à stocker dans SOPS comme OTO_API_KEY
```

## WhatsApp

Tools `whatsapp_*` wrappent `oto.tools.whatsapp.WhatsAppClient` (Baileys via
subprocess Node.js). Session per-user dans `<OTO_MCP_DATA_DIR>/whatsapp/<sub>/`
— on override `client.auth_dir` après instantiation, pas besoin de patcher
oto-cli. `asyncio.to_thread()` pour ne pas bloquer le event loop.

**Pairing QR via l'extension Chrome** (`pair/pair.html`). Endpoints :
- `GET /api/whatsapp/status` → `{paired, active_pairing}`
- `POST /api/whatsapp/pair/start` → `{session_id, status}`
- `GET /api/whatsapp/pair/stream?session_id=` → SSE `{type: qr|paired|failed}`
- `POST /api/whatsapp/pair/cancel`

`oto_mcp/pairing.py` gère les sessions in-memory (1 par sub). Bridge thread
parse le NDJSON émis par `whatsapp.mjs --json-events` et pousse dans une
asyncio.Queue.

Tools accessibles à tout user **dont l'auth_dir contient `creds.json`** (pas
de gating role). Le pairing crée ce fichier.

## Monitoring des appels MCP

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

## Billing — credits d'appel par org (paiement Stripe)

Monétisation à l'usage : **1 appel MCP = 1 credit**, débité du wallet de l'**org active**
du caller. **Pas d'abonnement récurrent** — chaque org reçoit un **stock de base unique
gratuit** (`OTO_MCP_FREE_CALLS`, défaut 1000), puis recharge par **packs Stripe**.

- **Modèle** : portefeuille **par org** (`credits_store.py`, couche backend-core). `balance` =
  compteur entier d'appels restants, **peut passer négatif** — **soft enforcement, on ne bloque
  JAMAIS un appel**. Drapeau `low` (alerte UI) seulement. Don de base posé **paresseusement**
  (`ensure_wallet`, au 1er débit OU 1re lecture), idempotent (`base_granted`).
- **Débit** : greffé sur `server._calllog_sink` (le hook calllog, **point d'interception unique**)
  → `credits_store.debit_for_call(sub)`. S'exécute **après** l'exécution du tool (non-bloquant par
  construction), best-effort (avale tout), no-op sans sub / sans org active. **Tous les appels
  comptent** (méta-tools + échecs inclus). Le débit n'écrit QUE `org_credits.balance` (pas de ligne
  ledger — volumétrie ; le détail par appel vit dans `tool_calls`).
- **Tables** (`db._SCHEMA`) : `org_credits(org_id PK, balance, base_granted, …)` + ledger
  `credit_transactions(id, org_id, delta, reason, stripe_event_id UNIQUE, …)` — le ledger ne porte
  que les mouvements **monétaires** (`stripe`/`base_grant`/`admin_adjust`).
- **Stripe** (`billing.py`, SDK **lazy-import** — absent = seuls les endpoints billing cassent, pas
  le boot) : catalogue `PACKS` en code (prix ad-hoc `price_data`, remise volume = la dégressivité
  1ct→0,1ct). `create_checkout_session(org_id, pack_id, sub)` (`metadata={org_id,calls,…}`) →
  `{checkout_url}`. Webhook `POST /api/billing/webhook` (**route brute** dans `make_routes`, NON
  authentifié mais **signature-vérifié** sur le **corps brut**, pas de capability/CORS) → sur
  `checkout.session.completed`, `credits_store.credit(...)`. **Idempotent** sur `event["id"]`
  (`UNIQUE` + `ON CONFLICT`) ; renvoie 500 sur erreur interne → Stripe rejoue sans double-crédit.
- **Surfaces** (capacités `capabilities/billing.py`, montage auto MCP+REST) : `billing.balance`
  (`ORG_MEMBER`, MCP `billing_balance` + `GET /api/me/billing`), `billing.transactions`
  (`GET /api/me/billing/transactions`), `billing.packs` (`SUB_ONLY`, `GET /api/billing/packs`),
  `billing.checkout` (`ORG_MEMBER`, `POST /api/me/billing/checkout`). **Qui paie = tout membre**
  de l'org (recharge le wallet partagé, bénin). `/api/me` expose un bloc `billing` (`{balance, low,
  base_granted}`, `null` si pas d'org active). Front : dashboard `/console/billing`.
- **Env** (`/opt/oto-mcp/.env`, cf. DEPLOY.md) : `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`,
  `OTO_MCP_FREE_CALLS` (déf. 1000), `OTO_DASHBOARD_URL`, `OTO_MCP_LOW_BALANCE_THRESHOLD` (déf. 50).
- **Gotcha** : un caller **sans org active** n'est **pas facturé** (no-op) — le metering exige
  l'appartenance org (cas limite : user tout neuf avant sa 1re org). Ne jamais déduire « a eu le
  stock de base » du solde (il peut être négatif) → lire `base_granted`.

## Visibility per-user

`UserDisabledToolsMiddleware` (`middleware.py`) applique au handshake `initialize` les visibility rules natives fastmcp (`disable_components` via `_visibility_rules` session state). Plus de filtrage manuel `on_list_tools`/`on_call_tool` — fastmcp émet `tools/list_changed` automatiquement quand les rules changent.

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). Table sœur `user_presets(sub, name, enabled_tools[])` pour les snapshots nommés.

**Masqués par défaut** (`is_default_hidden`) : invisibles par défaut sur la surface authentifiée, **self-activables** (≠ grant-only). Deux grains : `tool_visibility.py::DEFAULT_HIDDEN_TOOLS` (noms individuels) et `DEFAULT_HIDDEN_NAMESPACES` (namespaces entiers, **dérivé du registre** — champ `default_hidden` de `connectors.py`). Cas actuel : **`attio_*`** (le MCP Attio officiel est préféré ; code conservé pour implems custom). Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué-par-défaut > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève, `apply_preset` le réplique (même logique côté REST `/api/me/tools/{name}`). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Masquer un connecteur entier = poser `default_hidden=True` au registre ; un tool isolé = `DEFAULT_HIDDEN_TOOLS`.

Méta-tools exposés (`tools/meta.py`) : `oto_list_my_tools`, `oto_disable_tool`, `oto_enable_tool`, `oto_list_presets`, `oto_save_preset`, `oto_apply_preset`, `oto_delete_preset`. Le set protégé `{oto_list_my_tools, oto_enable_tool, oto_apply_preset}` reste toujours activé pour éviter le lock-out.

`oto_save_preset` (et `POST /api/me/presets/{name}`) accepte 2 modes : snapshot (par défaut, capture l'état courant) ou explicit (param `enabled_tools=[...]`, sauve sans altérer l'état courant — utile pour provisionner par script).

**Limite connue** : sessions MCP déjà ouvertes au moment d'un toggle via REST (`/account`) ne sont pas notifiées live — visible au prochain refresh ou nouvelle session, parce que le hook `on_initialize` ne tape qu'à la naissance d'une session.

## Doctrines & instructions d'org

Prose opératoire métier (workflows validés, règles, vocabulaire) pour les users qui pilotent
oto **sans produit applicatif dédié** (ex. Celeste, mission Movinmotion — process avoir
GoCardless → Pennylane → back-office, piloté directement depuis Claude sur un sous-ensemble
de tools). oto est la maison naturelle de cette prose faute de produit. Aligné
[ADR 0006](../docs/adr/0006-harnais-vs-substrat.md) (harnais-vs-substrat) : une org oto + sa
doctrine = un **harnais sans état** (étage zéro) ; le jour où un workflow doit persister un
pipeline/des statuts, il graduate en harnais à part (chemin blitz → scout).

**Modèle = skills, à la Claude Code.** Une org possède des **instructions markdown**
identifiées par `slug`, chacune versionnée :
- Le slug réservé **`claude_md`** = la **doctrine de base**, servie d'office.
- Les autres slugs = des **skills** chargés à la demande (progressive disclosure) : la
  doctrine de base ne porte que l'**index** (slug + titre + quand-l'utiliser), le détail
  se charge au besoin.

- **Service (membre)** : `get_claude_md()` (non préfixé `oto_` — convention cross-écosystème,
  comme Blitz/GR/Ogic) renvoie `{doctrine, instructions[]}` (base + index). Puis
  `oto_list_instructions()`, `oto_get_instruction(slug[, version])`, `oto_search_instructions(query)`.
  Tous scopés à l'**org active** du token (`org_store.get_active_org`) — **même principe d'accès
  que les org_secrets** : servis aux seuls membres. **Vide sans erreur** si pas d'org active /
  rien posé (`_SERVER_INSTRUCTIONS` invite à appeler `get_claude_md()` en début de session).
- **Écriture (platform admin, `_require_admin`)** : `oto_admin_set_doctrine(org_id, body_md)`
  (la base), `oto_admin_set_instruction(org_id, slug, body_md[, title, description])` (une skill ;
  `claude_md` réservé), `oto_admin_list_instructions`, `oto_admin_get_instruction(…[, version])`,
  `oto_admin_list_instruction_versions`, `oto_admin_revert_instruction(…, version)` (restaure une
  vieille version comme nouvelle, historique conservé), `oto_admin_delete_instruction`.
- **Écriture self-service (org_admin)** : la SPA `account/` (section « doctrine », `DoctrineView.vue`)
  édite la doctrine + les skills de l'**org active** via REST `/api/me/instructions*` (lecture =
  membre, écriture = `org_admin` de l'org active, gate `can_edit` renvoyé par l'API). C'est l'éditeur
  Phase 8 (oto-app#29) — l'org_admin édite sans agent.
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
  versionné avec l'outil. La doctrine de prospection de scout ne passe pas par ce mécanisme —
  elle vit chez scout (son propre `get_claude_md()`).

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
- **doctrine & skills** — `org_group_instructions` (+ revisions) ; `get_claude_md()`
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

## Conventions

- Nouveau connecteur = un fichier `tools/<service>.py` exposant `register(mcp)`,
  enregistré dans `tools/__init__.py`. Lazy imports pour ne pas faire crasher
  le serveur si un client a une dépendance optionnelle absente.
- **Cran d'activation (ADR 0010/0011)** : déclarer un connecteur ne l'expose PAS —
  gate DB `connector_activation.py` (master global ± override org, deny-by-default).
  Gate à la **VISIBILITÉ par session** (`UserDisabledToolsMiddleware` + `connector_
  activation`, **fail-open**) : `register_all` charge tout inconditionnellement, le
  middleware masque les tools d'un connecteur non activé pour l'org → (dés)activer
  prend effet à la session suivante **sans restart**, override par org OK. Filtre
  aussi `/api/connectors` (catalogue) ; overlays catalogue `family` (dérivée) +
  `category` (curée). Surface admin `/api/admin/connectors/activation`
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
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- **Aucune résolution de secret côté serveur hors DB/env de process** : pas de
  `get_secret`/`require_secret` oto.config dans le code serveur (l'unit pose
  `OTO_CONFIG_DISABLE_SOPS=1`, tout résidu échoue fort).
- LinkedIn nécessite le **vrai Google Chrome système** (`google-chrome-stable`, apt)
  sur l'host — PAS le Chromium bundlé Patchright (empreinte TLS ≠ Chrome de bureau
  → bloqué par LinkedIn). `_require_chrome_channel` (`tools/linkedin.py`) force
  `channel="chrome"` et lève une erreur si absent.
- WhatsApp nécessite Node.js installé + `node_modules` dans
  `oto-cli/oto/tools/whatsapp/node/` (auto-installé au premier `WhatsAppClient()`).
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

# Deploy — push main déclenche `.github/workflows/deploy.yml` (SSH la box dédiée
# REDACTED_IP : git reset --hard origin/main + pip install -e . + systemctl
# restart oto-mcp). Idem côté oto-cli (workflow restart oto-mcp). Le restart
# relance le wrapper start-encrypted (la master key est refetchée). ⚠️ start-
# encrypted.sh est untracked → survit au git reset.
git push origin main

# Logs
ssh -i ~/.ssh/alexis root@REDACTED_IP "journalctl -u oto-mcp -f"

# DB inspect (PG managed) — depuis la box (env du process inclut DATABASE_URL via .env)
ssh -i ~/.ssh/alexis root@REDACTED_IP 'set -a; . /opt/oto-mcp/.env; set +a; psql "$DATABASE_URL" -c "SELECT sub, email, role FROM users"'
```

## Infra

⚠️ **Migré sur box dédiée (2026-06-11, ADR 0002)** — oto-mcp **ne tourne plus sur tuls.me**.
- Server: **`oto-platform`** (Scaleway DEV1-S, fr-par-2, **`REDACTED_IP`**), `/opt/oto-mcp/`, port 9103, User=root. tuls.me oto-mcp **décommissionné** (stop+disable, code gardé pour rollback). SSH `root@REDACTED_IP` (clé `alexis`).
- **Chiffrement coffre ACTIF** : master key en Secret Manager (secret `REDACTED_SM_ID`), fetchée au boot par le wrapper `ExecStart=/opt/oto-mcp/start-encrypted.sh` (clé IAM scopée `/etc/oto-mcp/scw.env` → curl SM → env du process, **jamais sur disque**). Drop-in `/etc/systemd/system/oto-mcp.service.d/encryption.conf`. **0 plaintext** en base.
- DNS: `mcp.oto.ninja` A proxied → `REDACTED_IP` (zone CF `474add…`, zone hors tokens SOPS standard → minter un token éphémère via `CLOUDFLARE_ADMIN_TOKEN` sur `/user/tokens`). Rollback noté otomata#18.
- Caddy sur la box (standard, user `caddy` → cert key en `chgrp caddy chmod 640`) : `mcp.oto.ninja` → :9103, Origin Cert `oto-ninja.{pem,key}` copié de tuls.me.
- DB : PostgreSQL managed Scaleway `otomata-main` (instance `REDACTED_DB_INSTANCE…`, endpoint `REDACTED_IP:27996`, DB `oto_mcp`). ACL whiteliste tuls.me **et** la box (`REDACTED_IP/32`). DATABASE_URL en SOPS + `/opt/oto-mcp/.env`. Backup quotidien Scaleway 7j.
- **Restes ADR 0002** (non bloquants) : KMS-wrap master key (SM-direct pour l'instant), Terraform control-plane, deploy par registry. Cf. otomata#18.

## Docs

- `docs/connector-vault.md` — **archi centrale** : registre source unique (`connectors.py`), coffre chiffré unique `connector_credentials` (clés API + platform_keys + sessions linkedin/crunchbase/google multi-compte), enveloppe AES-256-GCM **obligatoire** (pas de plaintext), résolution + palier org. À lire avant de toucher credentials/registre/résolution.
- `README.md` — quickstart + tools catalog
- `deploy/DEPLOY.md` — procédure complète déploiement
- `docs/backlog.md` — initiatives à venir (issues GitHub pour le détail)
