# Connector vault — registre + coffre chiffré + résolution

Substrat unique des connecteurs, credentials et accès d'oto-mcp. Déployé en prod (2026-06).
Chiffrement **obligatoire** : tous les secrets vivent chiffrés (`secret_enc`), plus aucune colonne plaintext ni dual-write (purge legacy 2026-06-11). Un serveur sans `OTO_MCP_MASTER_KEY` boote mais tout write de credential échoue fort.

## Registre — source unique (`connectors.py`)

Module pur (aucun import oto_mcp, comme `tool_visibility.py`). Une dataclass `Connector` par connecteur, 3 axes orthogonaux :
- **A. Disponibilité** : `availability` ∈ {`self_serve`, `platform_granted`} ; `in_default_bundle`. platform_granted = grant-only (deny-by-default, ex. `mm`, `gocardless`).
- **B. Visibilité** : `in_default_preset` (affiché+activé par le preset de base).
- **C. Credential** : `auth_modes` ⊆ {`byo_user`, `byo_org`, `platform`} ; `keyed` (résolu via `resolve_api_key`) ; `secret_kind` (api_key/refresh_token/oauth/cookie/none) ; `personal_session` ; `env_secret_name` ; `default_quota`.

**Tout dérive du registre** (mêmes symboles, ré-export) : `KEY_PROVIDERS`, `ORG_SHAREABLE_PROVIDERS`, `ADMIN_GRANT_ONLY_NAMESPACES`, `QUOTA_DEFAULTS`, `ENV_SECRET_NAMES`, `DEFAULT_BUNDLE/PRESET`. Plus de listes en dur parallèles. `GET /api/connectors` = vue publique.
Helpers : `require_keyed`, `is_byo_user`, `is_org_shareable`, `require_credential(entity_type, name)` (user→byo_user, org→org-partageable).

## Coffre — `connector_credentials` (table unique)

A remplacé (et les a fait DROP, purge 2026-06-11) les 9 colonnes `users.<provider>_api_key`, `org_secrets`, les colonnes session (`users.linkedin_*`/`crunchbase_*`) et la table `user_google_oauth`. `init_db._drop_legacy_plaintext_stores` exécute les `DROP … IF EXISTS` (idempotent, no-op sur DB fraîche on-prem).

```
connector_credentials(entity_type, entity_id, connector, account, secret_enc,
                      secret_kind, meta JSONB, set_by, set_at,
                      PK(entity_type, entity_id, connector, account))
```
- `entity_type` ∈ {`user`,`org`} ; `entity_id` = `sub` | `org_id::text`. Toujours requêter `(entity_type, entity_id)` ENSEMBLE.
- `account` = discriminant **multi-compte** ('' = mono ; ex. email Google). 1 ligne par compte connecté.
- `secret_enc` = enveloppe chiffrée (pas de colonne plaintext). `meta` = satellites NON-secrets (user_agent linkedin/crunchbase, access_token/expires_at/scopes/is_default google).

Store = `credentials_store.py` (calqué `org_store.py`, réutilise `db._connect`, jamais d'import circulaire) :
`get_credential` / `get_credential_with_meta` (secret+meta+set_at, déchiffre) / `credential_status` (présence+meta SANS déchiffrer, pour /api/me) / `has_credential` / `set_credential` (chiffre) / `clear_credential` / `update_meta` (merge JSONB sans re-chiffrer) / `list_accounts`.

## Chiffrement au repos — `crypto.py`

Enveloppe **AES-256-GCM**, **obligatoire** (`set_credential`/`_pk_encrypt` chiffrent toujours ; `crypto.encrypt`/`decrypt` lèvent si master key absente — pas de stockage ni lecture plaintext). Master key **hors-DB** (env `OTO_MCP_MASTER_KEY`, hex64 ou base64-32o ; en prod fetchée de Scaleway Secret Manager au boot, cible KMS unwrap, cf. `../docs/adr/0002`). AAD = `connector_credentials:{entity_type}:{entity_id}:{connector}[:{account}]` (anti-transplant ; segment account omis si vide → compat ascendante mono-compte). Envelope = `key_ref(1o)‖nonce(12o)‖ct`.
- Déchiffrement **JIT** dans `resolve_api_key`/`get_credential` uniquement, jamais loggé ; `status_for` lit la présence (`has_credential`/`credential_status`), ne déchiffre pas. Échec de déchiffrement = LÈVE (pas de fallback silencieux).
- `platform_keys` : secret dans `api_key_enc` (même pattern, AAD `platform_keys:{provider}:{label}`).
- Dump Postgres = **ciphertext only**. Pas de rotation de clé (key_ref réservé). Perte de master key = perte totale → Secret Manager versionné + escrow.

## Résolution + accès (`access.py`)

`resolve_api_key(provider) -> (api_key, is_platform)` : (1) user key (`get_user_api_key`→coffre) ; (2) org secret (si `byo_org` + org active) ; (3) platform grant + quota ; (4) McpError actionnable. `resolve_remote_credential(provider)` = résolution du bridge d'un connecteur **remote** (`mm`) : `(meta.base_url, secret=token M2M)` du credential de l'org active, raise si absent, **jamais de fallback SOPS serveur**.
`status_for` = miroir exact (modes user/org/platform/over_quota/forbidden). `granted_namespaces_for`/`require_namespace` = gate des namespaces grant-only (deny-by-default), source unique consommée par middleware + meta-tools + REST.

## Palier org

Tables `orgs`/`org_members`(index partiel `org_members_one_active`)/`org_entitlements` ; `org_store.py` ; 12 meta-tools `oto_admin_*` (`tools/orgs.py`). Entité = **user ET org, 2 niveaux** (perso prime sur org).

## Folds des secrets de session (cible : coffre unique)

- **LinkedIn / Crunchbase** : cookie chiffré dans `secret_enc`, UA dans `meta` ; `db.set/get/clear_linkedin_cookie`/`crunchbase_session` sur le coffre ; statut /api/me via `credential_status` (sans déchiffrer).
- **Google OAuth multi-compte** : `connector='google'`, `account=email` ; refresh_token chiffré, access_token/expires_at/scopes/is_default/granted_at dans `meta`. Les 6 fns db (`set/get/list/set_default/delete_google_oauth`, `update_google_access_token`) sur le coffre ; `update_google_access_token` = `update_meta` (merge, sans re-chiffrer). Flow OAuth `google_oauth.py` inchangé (seule la couche stockage change). ⚠️ access_token reste en **clair dans `meta`** (bearer ~1h, dérivé) ; seul le refresh_token (`secret_enc`) est chiffré.

## Connecteurs remote — bridges (ADR 0003, pilote mm)

`kind="remote"` au registre = **aucun code ni credential client dans oto** : un bridge (service HTTP distant, ex. repo privé `movinmotion-backoffice-bridge`) détient le credential du système client ; oto-mcp = middleware générique `tools/remote.py` (tools `<ns>_describe` + `<ns>_call`, forward bearer M2M + `X-Oto-Sub` pour l'audit côté bridge). Le credential d'org = `secret` = token M2M + `meta.base_url` = endpoint (posé via `oto_admin_set_org_secret(..., base_url=…)`). Gating inchangé : grant-only + `require_namespace` au call-time. Contrat bridge (`/healthz`, `/describe`, `/call`) : ADR 0003 du meta-repo. Le mount MCP-to-MCP (`otomata#16`, memento) = flavor complémentaire pour les remotes déjà-MCP.

## Validation

Pas de framework de tests dans le repo → validation manuelle sur **PG16 jetable (docker)** + revue adversariale par phase. Migrations idempotentes au boot (`init_db` : ALTER additifs, PK 4-col, backfills, encrypt-existing, drop-plaintext gaté).
