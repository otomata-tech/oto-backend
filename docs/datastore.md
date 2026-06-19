# Datastore (spine natif PG, ADR 0016)

Stockage structuré léger par user, **substrat PostgreSQL natif** (plus Google
Sheets — ADR 0016). Un namespace = une ligne `user_datastores` ; les rows vivent
dans `datastore_rows` (un dict **JSONB** par row, types préservés nativement,
fin de la sentinelle `__j:`). Schéma libre. Trois champs auto-managés exposés à
plat : `_id` (uuid7-like), `_created_at`, `_updated_at`.

**Datastore = spine plateforme** (`provider=None`, ADR 0011), PAS un connecteur
Google : chargé explicitement dans `register_all` (à côté de meta/orgs/scout),
donc **hors gate d'activation** et **sans dépendance externe** — marche sans
connecter Google (plus de `412 google_not_connected`). Le partage est **DB-only**
(`datastore_shares` ; le destinataire lit via son propre `sub`, plus de
permission Drive). `data_url` renvoie un **deep-link dashboard** (`/console/data`),
pas une URL de Sheet. Code : `datastore.py` (`DatastorePg`) + `tools/datastore.py`
+ `api_routes_datastore.py` + fonctions `db.datastore_*`.

> **Export/sync vers un provider tiers** (Sheets/Docs/Notion — édition humaine,
> garantie de sortie) = projection optionnelle, **déférée à otomata#29**. C'est
> la raison d'être de l'unbundle, construite après.

> **Backfill** (Sheets → PG) : `scripts/migrate_datastore_to_pg.py` (idempotent,
> auto-suffisant pour la lecture Sheets). À lancer sur la box **après** le restart
> du code PG (brève fenêtre datastore-vide).

Surfaces :
- MCP tools `data_*` (`data_create_namespace`, `data_write`, `data_rows`,
  `data_delete_row`, `data_url`, `data_share`, etc.) — pour Claude.ai / Claude Code.
- REST `/api/datastore/*` — pour le CLI `oto data` + UI dashboard.

Auth :
- MCP tools : Logto JWT comme les autres tools.
- REST `/api/datastore/*` : Logto JWT **ou** API token long-lived (préfixe
  `oto_`, vérifié contre `user_api_tokens`).

OAuth Google per-user (Gmail + Tasks ; scopes Sheets/Drive latents pour l'export
#29 — **plus requis par le datastore**, ADR 0016 ; **multi-compte**) :
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
  `account=email`, `is_default` dans meta). Les tools `gmail_*`/`tasks_*`
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

## Setup GCP (one-shot, par projet)

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

## Env vars requises

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
