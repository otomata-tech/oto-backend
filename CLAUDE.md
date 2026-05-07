# oto-mcp

MCP server (Streamable HTTP) qui expose une sélection de connecteurs `oto-cli`
comme tools, branchable dans claude.ai et Claude Code.

Public : `https://mcp.oto.ninja/mcp` — déployé sur tuls.me, port 9103.

## Stack

- Python 3.10 (cible `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=2.0` (en prod : 3.2.4) + `mcp` SDK
- `oto-cli` (PyPI) → import direct des `oto.tools.*` clients
- `starlette` + `uvicorn` pour le HTTP transport
- Auth = JWT Logto (RemoteAuthProvider + JWTVerifier sur JWKS), audience
  = `https://mcp.oto.ninja/mcp`

## Architecture

```
oto_mcp/
├── server.py        # FastMCP entrypoint, transports stdio + streamable_http
├── tools.py         # @mcp.tool() qui wrappent oto.tools.<service>
└── config.py        # require_env

deploy/
├── oto-mcp.service       # systemd, User=root, /opt/oto-mcp, port 9103
├── Caddyfile.snippet     # mcp.oto.ninja avec edge bearer-gate sur /mcp*
└── DEPLOY.md             # full procedure
```

## Tools v0 (data.gouv.fr "Recherche Entreprises")

- `recherche_entreprises_search` — full-text + NAF/dept/postal/commune/employees/CA
- `recherche_entreprises_get` — par SIREN
- `recherche_entreprises_directors`
- `recherche_entreprises_finances`

Backed by `oto.tools.sirene.entreprises.EntreprisesClient` — pas de clé API requise.

## Conventions

- Un nouveau connecteur = un bloc `@mcp.tool()` dans `tools.py` qui importe
  `oto.tools.<service>`. Pas de fichier par tool.
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- Pas de cache, pas d'état. Les clients oto-cli sont stateless (HTTP requests).

## Auth — Logto

`RemoteAuthProvider(token_verifier=JWTVerifier(jwks_uri=…), authorization_servers=[…])` :
le backend valide la signature + issuer + audience des bearer JWT émis par
`auth.oto.zone`. Sur 401, le header `WWW-Authenticate` pointe vers
`/.well-known/oauth-protected-resource/mcp` (RFC 9728), ce qui amorce le
discovery OAuth côté client MCP.

Logto self-hosted n'expose pas DCR → l'app Claude est pré-créée dans le
tenant Logto et son `client_id` est collé manuellement dans le connector
Claude. Voir `deploy/DEPLOY.md` étape 3.

Env requis : `LOGTO_ENDPOINT`, `MCP_AUDIENCE`, `OTO_MCP_PUBLIC_URL`.

## Commands

```bash
# Local stdio (Claude Code)
.venv/bin/oto-mcp

# Deploy update
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git \
  -e "ssh -i ~/.ssh/alexis" /data/oto/mcp/ \
  root@51.15.225.121:/opt/oto-mcp/
ssh -i ~/.ssh/alexis root@51.15.225.121 \
  "cd /opt/oto-mcp && ./.venv/bin/pip install -e . && systemctl restart oto-mcp"

# Logs
ssh -i ~/.ssh/alexis root@51.15.225.121 "journalctl -u oto-mcp -f"
```

## Infra

- Server: tuls.me (51.15.225.121), `/opt/oto-mcp/`, port 9103, User=root
- DNS: `mcp.oto.ninja` A proxied → 51.15.225.121 (zone CF `474add39245a72c0ff98749e677815d3`)
- TLS: Origin Cert `*.oto.ninja` (déjà provisionné, `/etc/caddy/origin-certs/oto-ninja.{pem,key}`)
- Caddyfile: source de vérité dans `/mnt/odrive/infra/Caddyfile`, simple reverse_proxy
  (pas de bearer-gate — il masquerait le `WWW-Authenticate` resource_metadata)
- PORTS.md: `/mnt/odrive/infra/PORTS.md`

## Docs

- `README.md` — quickstart + tools catalog
- `deploy/DEPLOY.md` — procédure complète DNS + Caddy + systemd + Claude.ai
- `docs/backlog.md` — auth Logto, futurs connecteurs
