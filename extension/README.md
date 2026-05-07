# Oto Companion — Chrome Extension

Capture la session LinkedIn (cookie `li_at` + user-agent) et la pousse sur le
serveur MCP oto à `https://mcp.oto.ninja/api/settings/linkedin`. Évite de
copier-coller le cookie depuis les DevTools côté `oto.ninja/account`.

Auth = OIDC PKCE Logto (`auth.oto.zone`), même tenant que le site.

## Architecture

```
extension/
├── manifest.json            # MV3, permissions: cookies, storage, identity, tabs, scripting
├── background.js            # service worker — message router, cookie watcher, badge
├── lib/
│   ├── config.js            # endpoints + override via storage
│   ├── auth.js              # Logto PKCE: launchWebAuthFlow → token endpoint → refresh
│   ├── pkce.js              # SHA-256 challenge via Web Crypto
│   ├── api.js               # authedFetch vers mcp.oto.ninja
│   ├── linkedin.js          # readLiAt + capture UA + push
│   └── badge.js             # green/amber/gray sur l'icône
├── popup/
│   ├── popup.html / .css / .js
└── icons/                   # 16, 48, 128 (logo oto.ninja)
```

## Flow utilisateur

1. Install (unpacked ou Chrome Web Store).
2. Click sur l'icône → "Se connecter" → Logto consent → tokens stockés.
3. Va sur linkedin.com (login si pas connecté). Icône passe en vert "OK".
4. Click "Synchroniser" → cookie+UA poussés au backend.
5. Si LinkedIn rotate le cookie : `chrome.cookies.onChanged` repush silencieusement.

## Setup Logto (à faire une fois côté admin)

Dans la console Logto (`auth.oto.zone`), créer une app **Native** :

- **Type** : Native (public client, PKCE, refresh tokens)
- **Redirect URI** : `https://<extension-id>.chromiumapp.org/`
  - L'extension ID dépend de la clé publique du manifest (`manifest.json["key"]`).
  - En dev unpacked sans clé fixe : Chrome assigne un ID stable basé sur le path.
  - Le popup affiche l'ID à utiliser dans Paramètres → "Redirect URI".
- **Token endpoint auth method** : `none`
- **Grant types** : `authorization_code`, `refresh_token`
- **Resources** (audience) : ajouter `https://mcp.oto.ninja/mcp`
- **Scopes** : `openid`, `profile`, `email`, `offline_access`

Récupérer l'**App ID** Logto, l'entrer dans le popup → ⚙ → "Logto app ID".

## Build / install dev

Pas de build step — vanilla JS modules.

```bash
# Dans Chrome : chrome://extensions
# 1. Activer "Developer mode"
# 2. "Load unpacked" → /data/oto/mcp/extension/
# 3. Noter l'ID de l'extension affichée
# 4. Click sur l'icône → ⚙ → coller l'App ID Logto + sauver
# 5. "Se connecter"
```

## Distribution

### Chrome Web Store (à venir)

```bash
# Pack pour upload
cd /data/oto/mcp
zip -r /tmp/oto-companion.zip extension \
  -x 'extension/.*' 'extension/key.pem' 'extension/README.md'
```

Upload sur https://chrome.google.com/webstore/devconsole.

### CRX self-hosted (alternative)

Pour déployer un CRX signé hors Web Store, générer une clé RSA et l'embarquer
dans le manifest pour avoir un extension-ID stable :

```bash
openssl genrsa -out key.pem 2048
# clé publique en base64 DER pour le manifest:
openssl rsa -in key.pem -pubout -outform DER 2>/dev/null | base64 -w0
# → coller comme valeur de "key" en haut de manifest.json
```

Garder `key.pem` hors du repo (déjà gitignored) et hors du paquet.

## Endpoints utilisés

- `POST https://auth.oto.zone/oidc/auth` — authorization code + PKCE (S256)
- `POST https://auth.oto.zone/oidc/token` — exchange + refresh
- `GET  https://mcp.oto.ninja/api/me` — statut profil + LinkedIn
- `POST https://mcp.oto.ninja/api/settings/linkedin` — push cookie+UA
- `DELETE https://mcp.oto.ninja/api/settings/linkedin` — clear

## Roadmap

- Support Sales Navigator session (même cookie `li_at`)
- Capture Apollo / Crunchbase / Sellsy une fois les endpoints backend prêts
- Migration manifest "key" stable + publication Chrome Web Store
