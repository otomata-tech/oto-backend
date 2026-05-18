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
  `otomata-tech/oto` sur le serveur). Permet de propager les nouveaux connecteurs
  sans release PyPI — un `git pull` côté serveur suffit. La dépendance PyPI reste
  pour les déploiements fresh (premier install du venv).
- SQLite stdlib pour le state par utilisateur
- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)

## Architecture

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, registre les routes /api et les tools
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── api_routes.py     # /api/me, /api/settings/*, /api/admin/* (CORS oto.ninja)
├── access.py         # rôles guest/member/admin, resolve_api_key, quotas
├── db.py             # SQLite users + usage(sub, tool, day, count)
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
mais c'est sans risque pour les clés serveur grâce au modèle de rôles
(cf. `access.py`).

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`,
`OTO_MCP_ADMIN_SUB` (Logto sub d'Alexis pour bootstrap admin).

## Rôles + résolution de clé API

3 rôles user, source-of-truth = colonne `users.role` (Logto identifie, c'est
tout) :

- **guest** (défaut sign-up) : ne consomme JAMAIS les platform keys ; doit
  poser sa propre clé sur `/account` pour utiliser un tool API-keyed.
- **member** : platform key + quota daily (env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`,
  défauts dans `access.py`). User key bypass quota.
- **admin** : pas de quota, accès `/api/admin/*`. Bootstrap via env
  `OTO_MCP_ADMIN_SUB` (override forcé même si DB dit autre chose).

Tous les tools API-keyed (`serper_*`, `hunter_*`, `sirene_*`) appellent
`access.resolve_api_key(provider)` par appel — McpError actionnable pointant
vers `/account` en cas de blocage. LinkedIn et `recherche_entreprises_*` ne
sont pas concernés (cookie per-user / pas de clé).

## REST API (consommée par oto.ninja /account)

- `GET /api/me` — profil + role + statut LinkedIn + statut providers (mode/key/quota)
- `POST|DELETE /api/settings/linkedin` — cookie li_at + UA
- `POST|DELETE /api/settings/api-keys/{serper|hunter|sirene}` — user key
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- CORS limité aux origines `oto.ninja` + dev locales (`OTO_MCP_CORS_ORIGINS` override)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## LinkedIn cookies

Le couple `(li_at, user_agent)` est stocké par `sub` dans SQLite. Le UA
matche le browser d'origine (capturé via `navigator.userAgent` au moment du
save) — sinon LinkedIn flag rapidement les sessions cookie/UA mismatch.

Si le user n'a rien configuré, les tools `linkedin_*` lèvent une `McpError`
qui pointe vers `https://app.oto.ninja/`.

Pour les non-tech : extension Chrome Oto Companion (repo `oto-app/extension/`,
MV3) qui capture le couple `(li_at, user_agent)` et le push automatiquement
via `POST /api/settings/linkedin` (auth Logto PKCE). Auto-resync via
`chrome.cookies.onChanged` quand LinkedIn rotate la session.

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

## Conventions

- Nouveau connecteur = un fichier `tools/<service>.py` exposant `register(mcp)`,
  enregistré dans `tools/__init__.py`. Lazy imports pour ne pas faire crasher
  le serveur si un client a une dépendance optionnelle absente.
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- API keys serveur (`SERPER_API_KEY`, `HUNTER_API_KEY`, `SIRENE_API_KEY`)
  résolues via `oto.config.get_secret()` — ne pas re-faire l'env loading ici.
- LinkedIn nécessite Patchright + Chromium installés sur l'host
  (`patchright install chromium`).
- WhatsApp nécessite Node.js installé + `node_modules` dans
  `oto-cli/oto/tools/whatsapp/node/` (auto-installé au premier `WhatsAppClient()`).

## Commands

```bash
# Local stdio (Claude Code, sans auth)
OTO_MCP_DEV_SUB=alexis .venv/bin/oto-mcp

# Deploy update — propage à la fois oto-mcp (rsync local) et oto-cli (git pull)
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git --exclude data \
  -e "ssh -i ~/.ssh/alexis" /data/oto/mcp/ root@51.15.225.121:/opt/oto-mcp/
ssh -i ~/.ssh/alexis root@51.15.225.121 \
  "cd /opt/oto-cli && git pull origin main && \
   cd /opt/oto-mcp && ./.venv/bin/pip install -e . && systemctl restart oto-mcp"

# Logs
ssh -i ~/.ssh/alexis root@51.15.225.121 "journalctl -u oto-mcp -f"

# DB inspect
ssh -i ~/.ssh/alexis root@51.15.225.121 "sqlite3 /opt/oto-mcp/data/oto-mcp.sqlite 'SELECT * FROM users'"
```

## Infra

- Server: tuls.me (51.15.225.121), `/opt/oto-mcp/`, port 9103, User=root
- DNS: `mcp.oto.ninja` A proxied → tuls.me (zone CF `474add39245a72c0ff98749e677815d3`)
- TLS: Origin Cert `*.oto.ninja` (`/etc/caddy/origin-certs/oto-ninja.{pem,key}`)
- Caddyfile : source dans `/mnt/otomata-shared/infra/Caddyfile`
- DB SQLite : `/opt/oto-mcp/data/oto-mcp.sqlite` (override `OTO_MCP_DB_PATH`)

## Docs

- `README.md` — quickstart + tools catalog
- `deploy/DEPLOY.md` — procédure complète déploiement
- `docs/backlog.md` — initiatives à venir (issues GitHub pour le détail)
