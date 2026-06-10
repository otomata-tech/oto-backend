# Connector vault — registre + coffre chiffré + résolution

Substrat unique des connecteurs, credentials et accès d'oto-mcp. Déployé en prod (2026-06).
Chiffrement **dormant** tant que `OTO_MCP_MASTER_KEY` n'est pas posée (comportement = plaintext, identique à l'avant-vault).

## Registre — source unique (`connectors.py`)

Module pur (aucun import oto_mcp, comme `tool_visibility.py`). Une dataclass `Connector` par connecteur, 3 axes orthogonaux :
- **A. Disponibilité** : `availability` ∈ {`self_serve`, `platform_granted`} ; `in_default_bundle`. platform_granted = grant-only (deny-by-default, ex. `mm`, `gocardless`).
- **B. Visibilité** : `in_default_preset` (affiché+activé par le preset de base).
- **C. Credential** : `auth_modes` ⊆ {`byo_user`, `byo_org`, `platform`} ; `keyed` (résolu via `resolve_api_key`) ; `secret_kind` (api_key/refresh_token/oauth/cookie/none) ; `personal_session` ; `env_secret_name` ; `default_quota`.

**Tout dérive du registre** (mêmes symboles, ré-export) : `KEY_PROVIDERS`, `ORG_SHAREABLE_PROVIDERS`, `ADMIN_GRANT_ONLY_NAMESPACES`, `QUOTA_DEFAULTS`, `ENV_SECRET_NAMES`, `DEFAULT_BUNDLE/PRESET`. Plus de listes en dur parallèles. `GET /api/connectors` = vue publique.
Helpers : `require_keyed`, `is_byo_user`, `is_org_shareable`, `require_credential(entity_type, name)` (user→byo_user, org→org-partageable).

## Coffre — `connector_credentials` (table unique)

Remplace les 9 colonnes `users.<provider>_api_key` + `org_secrets` + les colonnes session (`users.linkedin_*`/`crunchbase_*`) + la table `user_google_oauth`.

```
connector_credentials(entity_type, entity_id, connector, account, secret, secret_enc,
                      secret_kind, meta JSONB, set_by, set_at,
                      PK(entity_type, entity_id, connector, account))
```
- `entity_type` ∈ {`user`,`org`} ; `entity_id` = `sub` | `org_id::text`. Toujours requêter `(entity_type, entity_id)` ENSEMBLE.
- `account` = discriminant **multi-compte** ('' = mono ; ex. email Google). 1 ligne par compte connecté.
- `secret` = clair (chiffrement OFF, ou soak) ; `secret_enc` = enveloppe chiffrée. `meta` = satellites (user_agent linkedin/crunchbase, access_token/expires_at/scopes/is_default google).

Store = `credentials_store.py` (calqué `org_store.py`, réutilise `db._connect`, jamais d'import circulaire) :
`get_credential` / `get_credential_with_meta` (secret+meta+set_at, déchiffre) / `credential_status` (présence+meta SANS déchiffrer, pour /api/me) / `has_credential` / `set_credential` / `clear_credential` / `update_meta` (merge JSONB sans re-chiffrer) / `list_accounts`. Dual-write legacy ATOMIQUE (1 transaction, conditionnel au chiffrement OFF) pour le rollback.

## Chiffrement au repos — `crypto.py`

Enveloppe **AES-256-GCM**. Master key **hors-DB** (env `OTO_MCP_MASTER_KEY`, hex64 ou base64-32o ; cible = Scaleway KMS unwrap-au-boot sur la box dédiée, cf. `../docs/adr/0002`). AAD = `connector_credentials:{entity_type}:{entity_id}:{connector}[:{account}]` (anti-transplant ; segment account omis si vide → compat ascendante mono-compte). Envelope = `key_ref(1o)‖nonce(12o)‖ct`.
- Déchiffrement **JIT** dans `resolve_api_key`/`get_credential` uniquement, jamais loggé ; `status_for` lit la présence (`has_credential`/`credential_status`), ne déchiffre pas.
- `platform_keys.api_key` aussi chiffré (`api_key_enc`, même pattern, AAD `platform_keys:{provider}:{label}`).
- **Inerte sans master key** : `encryption_enabled()`=False → écritures en clair, dual-write legacy actif.

### Runbook d'activation (box dédiée, délibéré)
1. générer master key 32o → Secret Manager + escrow (perte = perte totale des secrets) ;
2. poser `OTO_MCP_MASTER_KEY` → restart : `encrypt_existing_rows` + `_encrypt_existing_platform_keys` chiffrent en place (gardent le plaintext = soak), le dual-write cesse d'écrire du plaintext neuf ;
3. soak : vérifier que resolve déchiffre OK ;
4. poser `OTO_MCP_CRYPTO_DROP_PLAINTEXT=1` → restart : `_drop_plaintext_after_soak` (self-check decrypt puis null) nulle les 4 emplacements (connector_credentials.secret + 9 colonnes users + org_secrets + platform_keys.api_key + colonnes session) ;
5. retirer le flag. Pas de rotation de clé (key_ref réservé).

## Résolution + accès (`access.py`)

`resolve_api_key(provider) -> (api_key, is_platform)` : (1) user key (`get_user_api_key`→coffre) ; (2) org secret (si `byo_org` + org active) ; (3) platform grant + quota ; (4) McpError actionnable. `resolve_remote_credential(provider)` = résolution du bridge d'un connecteur **remote** (`mm`) : `(meta.base_url, secret=token M2M)` du credential de l'org active, raise si absent, **jamais de fallback SOPS serveur**.
`status_for` = miroir exact (modes user/org/platform/over_quota/forbidden). `granted_namespaces_for`/`require_namespace` = gate des namespaces grant-only (deny-by-default), source unique consommée par middleware + meta-tools + REST.

## Palier org

Tables `orgs`/`org_members`(index partiel `org_members_one_active`)/`org_entitlements` ; `org_store.py` ; 12 meta-tools `oto_admin_*` (`tools/orgs.py`). Entité = **user ET org, 2 niveaux** (perso prime sur org).

## Folds des secrets de session (cible : coffre unique)

- **LinkedIn / Crunchbase** : cookie = `secret`, UA dans `meta` ; `db.set/get/clear_linkedin_cookie`/`crunchbase_session` en dual-write + cutover lectures coffre ; statut /api/me via `credential_status` (sans déchiffrer). Colonnes `users.*` legacy nullées au soak.
- **Google OAuth multi-compte** : `connector='google'`, `account=email` ; refresh_token=`secret`, access_token/expires_at/scopes/is_default/granted_at dans `meta`. Les 6 fns db (`set/get/list/set_default/delete_google_oauth`, `update_google_access_token`) réécrites sur le coffre ; `update_google_access_token` = `update_meta` (merge, sans re-chiffrer). Flow OAuth `google_oauth.py` inchangé (seule la couche stockage change). access_token reste en clair dans meta même chiffré (bearer ~1h, dérivé) ; refresh_token chiffré.

## Connecteurs remote — bridges (ADR 0003, pilote mm)

`kind="remote"` au registre = **aucun code ni credential client dans oto** : un bridge (service HTTP distant, ex. repo privé `movinmotion-backoffice-bridge`) détient le credential du système client ; oto-mcp = middleware générique `tools/remote.py` (tools `<ns>_describe` + `<ns>_call`, forward bearer M2M + `X-Oto-Sub` pour l'audit côté bridge). Le credential d'org = `secret` = token M2M + `meta.base_url` = endpoint (posé via `oto_admin_set_org_secret(..., base_url=…)`). Gating inchangé : grant-only + `require_namespace` au call-time. Contrat bridge (`/healthz`, `/describe`, `/call`) : ADR 0003 du meta-repo. Le mount MCP-to-MCP (`otomata#16`, memento) = flavor complémentaire pour les remotes déjà-MCP.

## Validation

Pas de framework de tests dans le repo → validation manuelle sur **PG16 jetable (docker)** + revue adversariale par phase. Migrations idempotentes au boot (`init_db` : ALTER additifs, PK 4-col, backfills, encrypt-existing, drop-plaintext gaté).
