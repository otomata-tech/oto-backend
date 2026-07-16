---
title: Rôles + résolution de clé API
type: reference
description: >-
  Référence des 3 paliers de rôles plateforme oto-backend (member < admin < super_admin,
  définis dans access.py/roles.py, bootstrap via OTO_MCP_ADMIN_SUB) et de la cascade
  de résolution de clé API par appel : clé membre BYO scopée (sub, org) [ADR 0033] > grant explicite user_grants
  (quota daily) > McpError actionnable. Détaille les platform_keys en DB uniquement
  (plus de SOPS, rotation via oto_admin_set_platform_key), le gate auth_modes pour
  les providers platform-éligibles (serper/hunter/sirene/kaspr), les providers byo-only
  (attio/lemlist/pennylane), le cas Slack (token xoxp per-user), et le débranchement
  SOPS (OTO_CONFIG_DISABLE_SOPS=1 en prod). À consulter pour diagnostiquer un accès
  refusé, ajouter un grant, ou comprendre qui peut quoi sur la plateforme.
adr:
  - "0016"
---

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

Résolution par appel (`resolve_api_key` / `resolve_credential`) :

1. Clé **membre** (sub, org de contexte — ADR 0033) → directe, sans quota.
2. Instance **personnelle cross-org** (#172, providers `personal_cross_org`).
3. Secret d'**équipe active**, puis d'**org active** (providers org-shareables).
4. Instance **plateforme** (grant/free-tier, ADR 0044 §F) avec quota.
5. Rien → McpError actionnable + **instances à portée** (voir walker ci-dessous).

## Walker de cascade — source unique (2026-07-16)

La cascade ci-dessus vit dans **`access.walk_cascade`** (générateur paramétré
par sonde : `PRESENCE_PROBE` sans déchiffrement pour `/api/me`, `FETCH_PROBE`
qui ne déchiffre que le gagnant). Les 6 consommateurs (`_resolve_credential_impl`,
`credential_mode_for`, `status_for` ×2, `_resolve_credential_anon`,
`connector_resolvable_for_org`) en sont des traductions minces — **ne jamais
recopier la cascade dans un call-site** : chaque copie divergeait et faisait
mentir une surface (vécu 16/07). Contrat gardé par `tests/test_cascade_walker.py`
(ordre des barreaux, gates, accord présence/fetch). Les pins (`instance=`/
`project=`, ADR 0038) court-circuitent AVANT la marche ; `group` se passe en
lazy (callable) côté fetch. Échec « rien ne résout » : l'erreur remonte les
**instances à portée** (`access.reachable_instances` — équipes dont le sub est
membre, autres orgs) avec le geste per-call en tête (`group=`/`org=`/`instance=`) ;
le drawer reçoit le même signal via `status_for.team_key_group`.

Quota daily per-grant : colonne `user_grants.daily_quota` (posé par l'admin
au moment du grant). Si NULL, fallback sur env `OTO_MCP_QUOTA_<PROVIDER>_DAILY`
ou `_QUOTA_DEFAULTS` dans `access.py`. User key bypass quota.

**Les platform keys vivent en DB uniquement** (coffre `platform_keys` — plus de
bootstrap SOPS/env au boot, oto-mcp#12). Poser/roter une clé = surface admin :
REST `POST /api/admin/platform-keys` ou meta-tool `oto_admin_set_platform_key`
(rotation = re-poser même provider+label ; label historique servi par
`resolve_api_key` = `env`). Poser ≠ granter : l'admin accorde l'accès au cas
par cas. Modèle : clé membre (sub, org de contexte — ADR 0033, prio, no quota) OU platform key + grant + quota OU
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

## Scope MEMBRE (ADR 0033, 2026-07-02)

> **Scope MEMBRE (ADR 0033, 2026-07-02).** Plus de credential per-user org-agnostique :
> la clé BYO d'un membre est keyée **(sub, org)** — coffre `entity_type='member'`,
> `entity_id="{org}:{sub}"` (AAD dérivé → liée crypto à son org). Posée dans l'org A,
> elle ne résout PAS depuis l'org B (fini « ta clé perso te suit partout et écrase la
> clé d'org »). L'org de scope = seam `current_org` (0023), à la pose (`/api/settings/
> api-keys`, sessions browser) comme à la résolution ; helpers `db.{get,has,set,clear}_
> member_api_key(sub, org_id, provider)` — la couche db ne lit JAMAIS `current_org`
> elle-même. Valeurs de contrat inchangées (`mode="user"`, `user_key_configured`) —
> seule la sémantique change. **B3 (google)** : comptes Google multi-comptes scopés
> (sub, org) — `db/google.py` en entité member, l'org du DÉMARRAGE du flow OAuth voyage
> dans le **state HMAC** jusqu'au callback (qui vient de Google, sans headers de
> consultation). **B4 (unipile)** : `unipile_accounts` au grain **(sub, org_id, provider)**
> — `org_id` = org de CONTEXTE du binding (la facturation des sièges plateforme a sa
> colonne `platform_seat` ; les BYO ne comptent pas dans le plafond) ; migration PK
> one-shot `db.backfill_unipile_member_scope()` (⚠️ le cycle de vie du PK lui appartient,
> pas à `_init.py`). **Seuls les mounts oauth fédérés** (memento/atlassian/folkmcp)
> restent scope `('user', sub)` ; tripwire `test_member_credential_scope.py` interdit
> toute autre écriture scope user. Migration coffre = `credentials_store.
> backfill_member_scope()` au boot (re-chiffrement — l'AAD change, pas d'UPDATE ;
> destination = org maison ; ligne indéchiffrable laissée inerte).
