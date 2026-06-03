# oto-mcp

MCP server (Streamable HTTP) qui expose une sélection de connecteurs `oto-cli`
comme tools, branchable dans claude.ai et Claude Code. Public :
`https://mcp.oto.ninja/mcp` (tuls.me, port 9103).

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=2.0` (prod : 3.2.4) + `mcp` SDK
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
├── access.py         # rôles guest/member/admin, resolve_api_key, quotas
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

Le rôle (`users.role`) ne sert qu'à décider qui voit l'admin UI :

- **admin** : accès `/api/admin/*`. Bootstrap via env `OTO_MCP_ADMIN_SUB`.
- **guest** / **member** : alias historiques, pas d'effet sur l'accès aux tools.

L'accès aux clés API se décide par `user_grants` explicites (admin grante
manuellement via `/api/admin/users/{sub}/grants/{key_id}`). Résolution par
appel (`resolve_api_key`) :

1. User key posée sur `/account` → prise directement, sans quota.
2. Grant explicite dans `user_grants` → platform key avec quota.
3. Ni l'un ni l'autre → McpError actionnable pointant vers `/account`.

Quota daily per-grant : colonne `user_grants.daily_quota` (posé par l'admin
au moment du grant). Si NULL, fallback sur env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`
ou `_QUOTA_DEFAULTS` dans `access.py`. User key bypass quota.

Au boot, `bootstrap_env_keys` importe les clés env en `platform_keys` (label
`env`) mais ne les grante à personne — l'admin décide qui a accès. Modèle
identique pour tous les providers : user key (prio, no quota) OU platform
key + grant + quota OU erreur. `serper/hunter/sirene` sont les seuls
réellement partagés (commodité) ; `attio/lemlist/kaspr/pennylane/slack` sont des
comptes privés → clé plateforme présente mais grantée à personne (sauf
équipe Otomata). **Slack** = cas particulier : pas de `SLACK_API_KEY`, le
provider porte le **user token** (`xoxp`) du user. La clé plateforme est
bootstrappée depuis `SLACK_USER_TOKEN` (override dans `_bootstrap_env_keys`),
et les tools `slack_*` postent/lisent en `as_user` (le mode bot `xoxb` n'est
pas exposé en multi-tenant — viendra avec l'OAuth install, issue #4).

**Gotcha bootstrap** : `_bootstrap_env_keys` (server.py) itère sur TOUT
`KEY_PROVIDERS` et importe chaque `<PROVIDER>_API_KEY` trouvé en SOPS comme
clé plateforme (sauf override de nom de secret, cf. `slack`→`SLACK_USER_TOKEN`).
Donc ajouter un provider perso à `KEY_PROVIDERS` crée
involontairement une clé plateforme depuis ta clé perso. Importer ≠ partager
(inaccessible sans grant), mais à surveiller : vérifier `/api/admin/platform-keys`
après ajout, et ne grant que l'équipe pour les comptes privés.

Tous les tools API-keyed (`serper_*`, `hunter_*`, `sirene_*`, `fr_*`,
`attio_*`, `pennylane_*`, `slack_*`…) appellent `resolve_api_key(provider)`.
LinkedIn, WhatsApp et Datastore ne sont pas concernés (cookie/session/oauth
per-user).

## REST API (consommée par oto.ninja /account)

- `GET /api/me` — profil + role + statut LinkedIn + statut providers (mode/key/quota)
- `POST|DELETE /api/settings/linkedin` — cookie li_at + UA
- `POST|DELETE /api/settings/api-keys/{serper|hunter|sirene}` — user key
- `GET /api/me/tools` + `POST|DELETE /api/me/tools/{name}` — toggle individuel d'un tool MCP
- `GET /api/me/presets` + `GET|POST|DELETE /api/me/presets/{name}` + `POST /api/me/presets/{name}/apply` — presets nommés de toolset (cf. §Visibility)
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- `POST /api/admin/users/{sub}/grants/{key_id}` body `{daily_quota}` — set/update quota par grant (admin only)
- `GET|POST /api/admin/users/{sub}/tokens` + `DELETE /api/admin/users/{sub}/tokens/{token_id}` — issue/list/revoke tokens API on behalf of a user (admin only)
- CORS hardcoded : `oto.ninja`, `app.oto.ninja`, `localhost:5173/4173/5182/5184` (override via `OTO_MCP_CORS_ORIGINS`)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## LinkedIn cookies

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
- Query layer : `oto_mcp/sirene_duckdb.py`
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
- Scopes : `spreadsheets` + `drive.file` + `gmail.modify`.
- Multi-compte : table `user_google_oauth` clé `(sub, google_email)` +
  `is_default`. Le datastore et les tools `gmail_*` sans param `account`
  utilisent le compte par défaut. **Migration mono→multi** : les anciennes
  lignes (clé `sub`, `google_email` NULL) restent servies comme défaut et
  sont claimées proprement au prochain consentement (cf. `db.set_google_oauth`).
- Refresh token stocké en plaintext dans `user_google_oauth` (même modèle
  que les autres secrets per-user dans cette DB, accès root only).

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
     `.../auth/gmail.modify`
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

## Visibility per-user

`UserDisabledToolsMiddleware` (`middleware.py`) applique au handshake `initialize` les visibility rules natives fastmcp (`disable_components` via `_visibility_rules` session state). Plus de filtrage manuel `on_list_tools`/`on_call_tool` — fastmcp émet `tools/list_changed` automatiquement quand les rules changent.

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). Table sœur `user_presets(sub, name, enabled_tools[])` pour les snapshots nommés.

**Masqués par défaut** (`tool_visibility.py::DEFAULT_HIDDEN_TOOLS`, ex. `gocardless_*`) : namespaces sensibles/mission invisibles par défaut sur la surface authentifiée, ré-activables. Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué-par-défaut > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève, `apply_preset` le réplique (`enabled ∩ DEFAULT_HIDDEN`). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Ajouter un tool au set masqué = éditer `DEFAULT_HIDDEN_TOOLS`.

Méta-tools exposés (`tools/meta.py`) : `oto_list_my_tools`, `oto_disable_tool`, `oto_enable_tool`, `oto_list_presets`, `oto_save_preset`, `oto_apply_preset`, `oto_delete_preset`. Le set protégé `{oto_list_my_tools, oto_enable_tool, oto_apply_preset}` reste toujours activé pour éviter le lock-out.

`oto_save_preset` (et `POST /api/me/presets/{name}`) accepte 2 modes : snapshot (par défaut, capture l'état courant) ou explicit (param `enabled_tools=[...]`, sauve sans altérer l'état courant — utile pour provisionner par script).

**Limite connue** : sessions MCP déjà ouvertes au moment d'un toggle via REST (`/account`) ne sont pas notifiées live — visible au prochain refresh ou nouvelle session, parce que le hook `on_initialize` ne tape qu'à la naissance d'une session.

## Conventions

- Nouveau connecteur = un fichier `tools/<service>.py` exposant `register(mcp)`,
  enregistré dans `tools/__init__.py`. Lazy imports pour ne pas faire crasher
  le serveur si un client a une dépendance optionnelle absente.
- **Tool API-keyé = déclarer le provider à 3 endroits sinon `resolve_api_key`
  lève `Unknown provider` à l'appel** (le tool peut être écrit + enregistré et
  planter quand même) : (1) tuple `KEY_PROVIDERS` dans `db.py`, (2) colonne
  `<provider>_api_key` dans `_SCHEMA`, (3) `ALTER TABLE … ADD COLUMN IF NOT
  EXISTS` dans `init_db()`. Si le secret env n'est pas `<PROVIDER>_API_KEY`,
  ajouter un override dans `_bootstrap_env_keys` (cf. `slack`→`SLACK_USER_TOKEN`).
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- API keys serveur (`SERPER_API_KEY`, `HUNTER_API_KEY`, `SIRENE_API_KEY`)
  résolues via `oto.config.get_secret()` — ne pas re-faire l'env loading ici.
- LinkedIn nécessite Patchright + Chromium installés sur l'host
  (`patchright install chromium`).
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
# Local stdio (Claude Code, sans auth)
OTO_MCP_DEV_SUB=alexis .venv/bin/oto-mcp

# Deploy — push main déclenche `.github/workflows/deploy.yml` (git reset --hard
# origin/main + pip install -e . + systemctl restart oto-mcp). Idem côté
# oto-cli qui a son propre workflow déclenchant `systemctl restart oto-mcp`
# pour propager les nouveaux modules (`pip install -e` ne re-collecte pas les
# imports déjà en mémoire).
git push origin main

# Logs
ssh -i ~/.ssh/alexis root@REDACTED_IP "journalctl -u oto-mcp -f"

# DB inspect (PG managed)
ssh -i ~/.ssh/alexis root@REDACTED_IP 'set -a; . /opt/oto-mcp/.env; set +a; psql "$DATABASE_URL" -c "SELECT sub, email, role FROM users"'
```

## Infra

- Server: tuls.me (REDACTED_IP), `/opt/oto-mcp/`, port 9103, User=root
- DNS: `mcp.oto.ninja` A proxied → tuls.me (zone CF `REDACTED_CF_ZONE`)
- TLS: Origin Cert `*.oto.ninja` (`/etc/caddy/origin-certs/oto-ninja.{pem,key}`)
- Caddyfile : source dans `/mnt/otomata-shared/infra/Caddyfile`
- DB : PostgreSQL managed Scaleway `otomata-main` (instance `REDACTED_DB_INSTANCE…`, endpoint `REDACTED_IP:27996`, DB `oto_mcp`, user `oto_mcp`). Connection string en SOPS `projects/oto-mcp.yaml` (DATABASE_URL), injectée dans `/opt/oto-mcp/.env`. Backup quotidien Scaleway (rétention 7j) + snapshots SQLite legacy dans `/opt/oto-mcp/data/oto-mcp.sqlite.bak-*` (à drop après 1 semaine).

## Docs

- `README.md` — quickstart + tools catalog
- `deploy/DEPLOY.md` — procédure complète déploiement
- `docs/backlog.md` — initiatives à venir (issues GitHub pour le détail)
