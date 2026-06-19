# Rôles + résolution de clé API

> ⚠️ Le **stockage** des credentials est le **coffre chiffré unique `connector_credentials`** (cf. `docs/connector-vault.md`). Les colonnes legacy `users.<provider>_api_key`/`org_secrets`/`user_google_oauth` ont été **purgées** (DROP, 2026-06-11) ; chiffrement **obligatoire** (plus de plaintext). La résolution ci-dessous reste valide dans sa cascade, lit le coffre via `credentials_store`.

Le rôle (`users.role`) décide de l'accès à l'admin UI, sur **3 paliers**
(`ROLES = (member, admin, super_admin)`, cf. `access.py`/`roles.py`) :

- **super_admin** : le tout-puissant — escalade `org_admin` de TOUTE org +
  `group_admin` de TOUT groupe (`roles.is_platform_admin` = super), gestion des
  rôles plateforme, platform keys, émission de tokens, écriture + doctrine
  d'orgs tierces, création d'org, bypass namespace grant-only. Bootstrap env
  `OTO_MCP_ADMIN_SUB` → super_admin. Combinateur d'autz `SUPER_ADMIN`.
- **admin** (palier OPÉRATIONNEL intermédiaire) : supervision plateforme —
  monitoring, liste/fiche users, activation des connecteurs, refresh des mounts,
  lectures d'orgs — **SANS** escalade en masse vers les orgs tierces.
  Prédicat `access.is_platform_operator` (admin ∪ super) ; combinateur `PLATFORM_ADMIN`.
- **member** : défaut, pas d'effet sur l'accès aux tools (`guest` retiré
  2026-06-15, migré → member).

> Les `admin` historiques (= tout-puissants) ont été migrés → `super_admin`
> (`scripts/migrate_admin_to_super.py`). Un `admin` aujourd'hui = opérateur.

L'accès aux clés API se décide par `user_grants` explicites (admin grante
manuellement via `/api/admin/users/{sub}/grants/{key_id}`). Résolution par
appel (`resolve_api_key`) :

1. User key posée sur `/account` → prise directement, sans quota.
2. Grant explicite dans `user_grants` → platform key avec quota.
3. Ni l'un ni l'autre → McpError actionnable pointant vers `/account`.

Quota daily per-grant : colonne `user_grants.daily_quota` (posé par l'admin
au moment du grant). Si NULL, fallback sur env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`
ou `_QUOTA_DEFAULTS` dans `access.py`. User key bypass quota.

**Les platform keys vivent en DB uniquement** (coffre `platform_keys` — plus de
bootstrap SOPS/env au boot, oto-mcp#12). Poser/roter une clé = surface admin :
REST `POST /api/admin/platform-keys` ou meta-tool `oto_admin_set_platform_key`
(rotation = re-poser même provider+label ; label historique servi par
`resolve_api_key` = `env`). Poser ≠ granter : l'admin accorde l'accès au cas
par cas. Modèle : user key (prio, no quota) OU platform key + grant + quota OU
erreur. **Seuls les providers `platform`-éligibles au registre (`auth_modes`
inclut `platform` : `serper/hunter/sirene/kaspr`) peuvent avoir une clé
plateforme** — `resolve_api_key` **gate** le chemin platform-grant sur
`auth_modes` (audit 2026-06-11). Les comptes **privés / byo-only**
(`attio/lemlist/pennylane/fullenrich/slack`) **n'ont PAS de clé plateforme** :
les clés résiduelles du seed SOPS ont été supprimées, et le compte partagé de
l'**équipe Otomata** (attio/lemlist) vit en **credentials de l'org Otomata
(byo_org, org id 2)** — accès par appartenance, pas par grant plateforme.
**Slack** : pas de `SLACK_API_KEY`, le provider porte le **user token**
(`xoxp`) per-user — `slack_*` postent en `as_user` (mode bot viendra avec
l'OAuth install, issue #4).

**Débranchement SOPS (oto-mcp#12)** : l'unit pose `OTO_CONFIG_DISABLE_SOPS=1`
→ côté serveur, `oto.config.get_secret` ne résout QUE l'env du process (ni
SOPS ni `~/.otomata/secrets.env`), et tout `require_secret` résiduel échoue
fort. L'infra bootstrap (DATABASE_URL, Logto, OAuth Google, state secret)
reste en env de process (`/opt/oto-mcp/.env`).

Tous les tools API-keyed (`serper_*`, `hunter_*`, `sirene_*`, `fr_*`,
`attio_*`, `pennylane_*`, `slack_*`…) appellent `resolve_api_key(provider)`.
LinkedIn et WhatsApp ne sont pas concernés (cookie/session per-user) ; le
datastore non plus (spine PG, aucun credential — ADR 0016).
