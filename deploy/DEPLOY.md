# Deploy oto-mcp on tuls.me (mcp.oto.ninja)

Convention serveur : code dans `/opt/oto-mcp/`, port 9103 (slot MCP), Caddyfile
de vérité dans `/mnt/odrive/infra/Caddyfile`. Origin Cert `*.oto.ninja` déjà
provisionné sur le serveur.

## 1. DNS (Cloudflare)

```bash
export CLOUDFLARE_API_TOKEN=$(pass shared/CLOUDFLARE_API_TOKEN)
ZONE_ID=REDACTED_CF_ZONE   # oto.ninja
curl -sS -X POST "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records" \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"type":"A","name":"mcp.oto.ninja","content":"REDACTED_IP","ttl":1,"proxied":true}' | jq '.success,.errors'
```

## 2. Caddyfile

Le bloc est déjà ajouté dans `/mnt/odrive/infra/Caddyfile`. Pousser :

```bash
scp -i ~/.ssh/alexis /mnt/odrive/infra/Caddyfile root@REDACTED_IP:/etc/caddy/Caddyfile
ssh -i ~/.ssh/alexis root@REDACTED_IP "caddy-custom reload --config /etc/caddy/Caddyfile"
```

## 3. Code + venv

```bash
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git \
  -e "ssh -i ~/.ssh/alexis" \
  /data/oto/mcp/ root@REDACTED_IP:/opt/oto-mcp/

ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cd /opt/oto-mcp && python3 -m venv .venv && ./.venv/bin/pip install -e ."
```

## 4. .env (secrets)

Générer un mot de passe partagé fort :

```bash
PASSWORD=$(openssl rand -hex 24)
ssh -i ~/.ssh/alexis root@REDACTED_IP "cat > /opt/oto-mcp/.env" <<EOF
OTO_MCP_PUBLIC_URL=https://mcp.oto.ninja
OTO_MCP_OAUTH_PASSWORD=$PASSWORD
EOF
ssh -i ~/.ssh/alexis root@REDACTED_IP "chmod 600 /opt/oto-mcp/.env"
echo "OAuth password: $PASSWORD   # à conserver dans pass / 1Password"
```

## 5. systemd

```bash
ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cp /opt/oto-mcp/deploy/oto-mcp.service /etc/systemd/system/ \
   && echo '9103 oto-mcp' >> /opt/ports.conf \
   && systemctl daemon-reload \
   && systemctl enable --now oto-mcp \
   && systemctl status oto-mcp --no-pager"
```

## 6. Vérifier

```bash
# Sur le serveur (sans bearer → 401 attendu, mais 405/406 sur autre méthode est OK)
ssh -i ~/.ssh/alexis root@REDACTED_IP "curl -sSI http://127.0.0.1:9103/mcp/ | head"

# De l'extérieur (sans bearer → 401 attendu côté Caddy)
curl -sSI https://mcp.oto.ninja/mcp/

# OAuth metadata (open, doit répondre 200)
curl -sS https://mcp.oto.ninja/.well-known/oauth-authorization-server | jq
```

## 7. Brancher dans Claude.ai

Settings → Connectors → Add custom connector :
- Name: oto
- URL: `https://mcp.oto.ninja/mcp`
- Auth: OAuth (Claude.ai déclenche /register dynamique puis /authorize)

Au /authorize, le navigateur s'ouvre sur `https://mcp.oto.ninja/login?nonce=...` —
saisir le mot de passe enregistré à l'étape 4.

## Update

```bash
rsync -avz --exclude .venv --exclude .env --exclude __pycache__ --exclude .git \
  -e "ssh -i ~/.ssh/alexis" \
  /data/oto/mcp/ root@REDACTED_IP:/opt/oto-mcp/
ssh -i ~/.ssh/alexis root@REDACTED_IP \
  "cd /opt/oto-mcp && ./.venv/bin/pip install -e . && systemctl restart oto-mcp"
```
