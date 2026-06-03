# Deploy oto-mcp on tuls.me (mcp.oto.ninja)

Convention serveur : code dans `/opt/oto-mcp/`, port 9103 (slot MCP), Caddyfile
de vérité dans `/mnt/otomata-shared/infra/Caddyfile`. Origin Cert `*.oto.ninja` déjà
provisionné sur le serveur.

Auth : Logto (`auth.oto.zone`). Validation JWT côté backend (JWKS), audience =
`https://mcp.oto.ninja/mcp`. Pas de form `/login`, pas de mot de passe partagé.

## 1. DNS (Cloudflare)

```bash
export CLOUDFLARE_API_TOKEN=$(sops --decrypt --extract '["CLOUDFLARE_DNS_TOKEN"]' ~/.otomata/secrets/secrets/secrets.yaml)
ZONE_ID=REDACTED_CF_ZONE   # oto.ninja
curl -sS -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"A","name":"mcp.oto.ninja","content":"REDACTED_IP","ttl":1,"proxied":true}' | jq '.success,.errors'
```

## 2. Caddyfile

Le bloc est dans `/mnt/otomata-shared/infra/Caddyfile`. Pas de bearer-gate edge — le
backend renvoie déjà un 401 + `WWW-Authenticate: Bearer resource_metadata="…"`,
indispensable au discovery OAuth des clients MCP. Pousser :

```bash
scp -i ~/.ssh/alexis /mnt/otomata-shared/infra/Caddyfile root@REDACTED_IP:/etc/caddy/Caddyfile
ssh -i ~/.ssh/alexis root@REDACTED_IP "caddy-custom reload --config /etc/caddy/Caddyfile"
```

## 3. Logto — pré-créer la resource + l'app Claude

Logto self-hosted ne supporte pas DCR (`registration_endpoint` absent). Il faut
créer une fois pour toutes :

1. **API resource** (Logto admin → API resources → Create) :
   - Name : `oto MCP`
   - Indicator : `https://mcp.oto.ninja/mcp`  ← doit matcher `MCP_AUDIENCE`
2. **Application** type "Single Page App" pour Claude Web/Desktop :
   - Name : `Claude (oto MCP)`
   - Redirect URIs :
     - `https://claude.ai/api/mcp/auth_callback`
     - `https://claude.com/api/mcp/auth_callback`
   - CORS allowed origins : `https://claude.ai`, `https://claude.com`
   - Cocher la resource `oto MCP` dans l'onglet "API resources"
3. Récupérer le `client_id` (à coller dans le connector Claude).

## 4. Code + venv

```bash
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git \
  -e "ssh -i ~/.ssh/alexis" \
  /data/oto/mcp/ root@REDACTED_IP:/opt/oto-mcp/

ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cd /opt/oto-mcp && python3 -m venv .venv && ./.venv/bin/pip install -e ."
```

### Feature LinkedIn — dépendances browser (sinon `linkedin_*` + pairing VNC cassés)

- **Google Chrome (système)** : `apt-get install -y google-chrome-stable` (one-time, root). o-browser/patchright **auto-détecte le channel** (`_detect_channel()` → `google-chrome` présent ⇒ `channel="chrome"` ⇒ vrai Chrome `/opt/google/chrome`, **pas** le chromium bundled). Durable (apt, hors `~/.cache`). Fallback seulement si Chrome absent : `./.venv/bin/patchright install chromium`.
- **Stack VNC** (pairing self-service depuis oto.ninja/account) — paquets système à installer une fois (root) : `apt-get install -y xvfb x11vnc websockify novnc`. Caddy route `/vnc/* → localhost:6098`.
- Le service `oto-mcp` tourne en **root**. Les profils LinkedIn persistent dans `/opt/oto-mcp/data/browser-profiles/linkedin-<sub>/`.

## 5. .env

```bash
ssh -i ~/.ssh/alexis root@REDACTED_IP "cat > /opt/oto-mcp/.env" <<'EOF'
OTO_MCP_PUBLIC_URL=https://mcp.oto.ninja
LOGTO_ENDPOINT=https://auth.oto.zone
MCP_AUDIENCE=https://mcp.oto.ninja/mcp
EOF
ssh -i ~/.ssh/alexis root@REDACTED_IP "chmod 600 /opt/oto-mcp/.env"
```

## 6. systemd

```bash
ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cp /opt/oto-mcp/deploy/oto-mcp.service /etc/systemd/system/ \
   && echo '9103 oto-mcp' >> /opt/ports.conf \
   && systemctl daemon-reload \
   && systemctl enable --now oto-mcp \
   && systemctl status oto-mcp --no-pager"
```

## 7. Vérifier

```bash
# Resource metadata (200 attendu)
curl -sS https://mcp.oto.ninja/.well-known/oauth-protected-resource/mcp | jq

# Sans bearer (401 + WWW-Authenticate avec resource_metadata="…" attendu)
curl -sSI -X POST https://mcp.oto.ninja/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  --data-raw '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# Avec un vrai token Logto (200 + JSON-RPC)
TOKEN=...   # access_token Logto avec audience=https://mcp.oto.ninja/mcp
curl -sS -X POST https://mcp.oto.ninja/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  --data-raw '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | head
```

## 8. Brancher dans Claude.ai

Settings → Connectors → Add custom connector :
- Name : `oto`
- URL : `https://mcp.oto.ninja/mcp`
- Authentication : **OAuth** (obligatoire — sans, claude.ai retourne le
  message trompeur "Couldn't reach the MCP server" alors que c'est un 401)
- Advanced → Client ID : coller le `client_id` de la SPA Logto `Claude (oto MCP)`
  (Logto self-hosted ne supporte pas DCR, claude.ai ne peut pas s'enregistrer)
- Pas de Client Secret (SPA + PKCE)

Claude découvre l'auth server via `/.well-known/oauth-protected-resource/mcp`,
fait le flow OAuth contre Logto, l'utilisateur s'authentifie sur
`auth.oto.zone`, Claude récupère un access_token et l'envoie en Bearer sur
`/mcp`.

## Update

```bash
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git \
  -e "ssh -i ~/.ssh/alexis" \
  /data/oto/mcp/ root@REDACTED_IP:/opt/oto-mcp/
ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cd /opt/oto-mcp && ./.venv/bin/pip install -e . && systemctl restart oto-mcp"
```

> Le deploy CI (`.github/workflows/deploy.yml`) fait `pip install` + restart. Préférer un push sur `main` (ou `workflow_dispatch`) au déploiement manuel. (Le browser de la feature LinkedIn = `google-chrome-stable` apt, dép système one-time, pas re-provisionnée à chaque deploy.)
