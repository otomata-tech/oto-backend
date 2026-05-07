# oto-mcp

MCP server (Streamable HTTP) qui expose une sélection de connecteurs `oto-cli`
comme tools, branchable dans claude.ai et Claude Code. Public :
`https://mcp.oto.ninja/mcp` (tuls.me, port 9103).

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=2.0` (prod : 3.2.4) + `mcp` SDK
- `oto-cli[browser]` (PyPI) → import direct des `oto.tools.*` clients
- SQLite stdlib pour le state par utilisateur
- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)

## Architecture

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, registre les routes /api et les tools
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── api_routes.py     # /api/me, /api/settings/linkedin (POST/DELETE), CORS oto.ninja
├── db.py             # SQLite users(sub PK, linkedin_cookie, linkedin_user_agent…)
├── auth_hooks.py     # current_user_sub_from_token() pour le contexte tool
└── config.py         # require_env

deploy/
├── oto-mcp.service       # systemd, User=root, /opt/oto-mcp, port 9103
├── Caddyfile.snippet     # mcp.oto.ninja → 9103 (pas de bearer-gate, masquerait WWW-Authenticate)
└── DEPLOY.md             # procédure DNS + Caddy + systemd + Claude.ai
```

## Auth — Logto

Le backend valide les bearer JWT émis par `auth.oto.zone/oidc`. Sur 401, le
header `WWW-Authenticate` pointe vers `/.well-known/oauth-protected-resource/mcp`
(RFC 9728) ce qui amorce le discovery OAuth côté client MCP.

**Gotcha** : Logto self-hosted signe en `ES384` (P-384 ECDSA). Le default de
`JWTVerifier` est RS256 → tous les tokens rejetés. Vérifié sur
`GET /oidc/jwks`.

Logto self-hosted n'expose pas DCR → les apps Claude sont pré-créées dans le
tenant et leur `client_id` est collé à la main dans le connector Claude.

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`.

## REST API (consommée par oto.ninja /account)

- `GET /api/me` — profil + statut LinkedIn (configured, set_at, user_agent)
- `POST /api/settings/linkedin` — body `{cookie, user_agent?}` → upsert
- `DELETE /api/settings/linkedin` — clear
- CORS limité aux origines `oto.ninja` + dev locales (`OTO_MCP_CORS_ORIGINS` override)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## LinkedIn cookies

Le couple `(li_at, user_agent)` est stocké par `sub` dans SQLite. Le UA
matche le browser d'origine (capturé via `navigator.userAgent` au moment du
save) — sinon LinkedIn flag rapidement les sessions cookie/UA mismatch.

Si le user n'a rien configuré, les tools `linkedin_*` lèvent une `McpError`
qui pointe vers `https://oto.ninja/account`.

## Conventions

- Nouveau connecteur = un fichier `tools/<service>.py` exposant `register(mcp)`,
  enregistré dans `tools/__init__.py`. Lazy imports pour ne pas faire crasher
  le serveur si un client a une dépendance optionnelle absente.
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- API keys serveur (`SERPER_API_KEY`, `HUNTER_API_KEY`, `SIRENE_API_KEY`,
  `GROQ_API_KEY`) résolues via `oto.config.get_secret()` — ne pas re-faire
  l'env loading ici.
- LinkedIn nécessite Patchright + Chromium installés sur l'host
  (`patchright install chromium`).

## Commands

```bash
# Local stdio (Claude Code, sans auth)
OTO_MCP_DEV_SUB=alexis .venv/bin/oto-mcp

# Deploy update
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git --exclude data \
  -e "ssh -i ~/.ssh/alexis" /data/oto/mcp/ root@REDACTED_IP:/opt/oto-mcp/
ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cd /opt/oto-mcp && ./.venv/bin/pip install -e . && systemctl restart oto-mcp"

# Logs
ssh -i ~/.ssh/alexis root@REDACTED_IP "journalctl -u oto-mcp -f"

# DB inspect
ssh -i ~/.ssh/alexis root@REDACTED_IP "sqlite3 /opt/oto-mcp/data/oto-mcp.sqlite 'SELECT * FROM users'"
```

## Infra

- Server: tuls.me (REDACTED_IP), `/opt/oto-mcp/`, port 9103, User=root
- DNS: `mcp.oto.ninja` A proxied → tuls.me (zone CF `REDACTED_CF_ZONE`)
- TLS: Origin Cert `*.oto.ninja` (`/etc/caddy/origin-certs/oto-ninja.{pem,key}`)
- Caddyfile : source dans `/mnt/odrive/infra/Caddyfile`
- DB SQLite : `/opt/oto-mcp/data/oto-mcp.sqlite` (override `OTO_MCP_DB_PATH`)

## Docs

- `README.md` — quickstart + tools catalog
- `deploy/DEPLOY.md` — procédure complète déploiement
- `docs/backlog.md` — initiatives à venir (issues GitHub pour le détail)
