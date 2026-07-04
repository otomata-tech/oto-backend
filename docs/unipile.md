# Unipile — messagerie hébergée (WhatsApp/Telegram/Instagram/LinkedIn)

> Extrait du CLAUDE.md (refactor 2026-07-02) — domicile du détail ; le CLAUDE.md garde le résumé + pointeur.


Tools `whatsapp_*` / `telegram_*` / `instagram_*` (`list_chats`/`read_chat`/
`send_message`) = messagerie **hébergée Unipile**, sous le connecteur `unipile`
(`modules`/namespaces = `unipile, whatsapp, telegram, instagram`). Générés par la
factory `tools/unipile.register_messaging_tools(mcp, channel)` — l'API `/chats`
d'Unipile est channel-agnostic ; chaque tool résout l'`account_id` du canal pour le
user (no-fallback, `tools/unipile.unipile_client(provider)`).

Connexion = hosted-auth Unipile (dashboard, `?channel=whatsapp|telegram|instagram`),
`account_id` per-membre dans `unipile_accounts` (PK `(sub, org_id, provider)` — scope
membre ADR 0033 B4 : le binding vaut dans l'org de contexte, un canal se connecte par
org). Même gate d'option par org que LinkedIn (comp admin `access.has_option` ; plus
de paiement).

> **Baileys archivé** (ex-WhatsApp self-hosted) : wrappers backend retirés
> (`tools/whatsapp.py` réécrit Unipile, `pairing.py` + routes `/api/whatsapp/pair/*`
> supprimés). L'engine Baileys survit dans **oto-core** (`oto/tools/whatsapp/` + Node)
> + la **CLI `oto whatsapp`** (fallback).

> **Mode plateforme unipile** (revente) : `auth_modes` inclut `platform` → la clé
> Unipile se partage en **clé plateforme + grant** (pas de copie par org) ;
> `access.unipile_api_key_for` a le fallback platform-grant. Le gate d'option reste
> par org (un grant donne la clé, ne débloque pas l'option). **Débloquer l'option
> = comp** : `db.set_option_comp("org", id, "unipile")` (débloque `access.has_option`).
> ⚠️ Les deux couches (clé=2, option=3) sont **orthogonales en base** mais l'**action
> admin les compose** (`capabilities/users_admin._set_option`) : `oto_admin_set_option`
> `on=true` sur un connecteur en mode plateforme **grant aussi la clé plateforme** (sinon
> `has_option`=true mais aucune clé → 404 au `/connect`, bouton « Connecter » inerte = état
> mort), `on=false` la révoque ; le champ `platform_key` du retour rend l'effet explicite
> (`granted`/`no_platform_key`/`byo_inert`/`revoked`). N'applique PAS à un connecteur keyed
> sans option (serpapi…) : lui se grant via la fiche admin (bouton « grant key » par
> provider, auto-résout la clé unique) ou `oto_admin_key_grant` (par `key_id`).

> **DSN par credential + sélecteur d'identité (ADR 0024).** Chaque clé Unipile est liée
> à SON sous-domaine `api<NN>.unipile.com:port` ; le DSN vit dans le `meta` du credential
> et voyage avec la clé via `resolve_credential` (défaut env `UNIPILE_DSN`=api25, instance
> plateforme). Une clé BYO porte N comptes → capacités génériques **`connectors.identities`/
> `set_default_identity`** (REST `/api/connectors/{c}/identities[/default]`, registre
> `connector_identities.py` ; unipile = `list_accounts` sur clé+DSN, **valide id∈liste**
> anti-binding, **BYO-only** — en revente la liste est vide, hosted-auth conservé). Vue admin
> **sièges clé plateforme** `GET /api/admin/unipile/seats` (super_admin, `db.unipile_account_owners`) :
> réconcilie les comptes de l'instance partagée ↔ leur owner oto (flag **orphelin**).

> **Compte partagé autorisé (otomata-private#55).** Le **propriétaire** d'un compte
> Unipile accorde à un **membre nommé** (d'une org commune, anti-IDOR `users_share_org`)
> le droit d'**opérer son compte** sur un canal — la **SEULE exception** au no-fallback
> anti-usurpation (#5). Table `connector_account_grants` (PK `(owner_sub, provider,
> grantee_sub)`, patron ADR 0025, `granted_by`/`granted_at` ; l'`account_id` stocké =
> snapshot d'audit, la résolution relit le handle **LIVE** → owner déconnecté = grant
> inerte). Le grantee bascule via le **sélecteur d'identité** (le compte accordé
> apparaît « compte de X » ; le select pose le **pointeur** `unipile_operated_accounts`,
> il n'écrase JAMAIS sa ligne `unipile_accounts`) ou un **pin projet** (garde étendue
> aux comptes accordés). Résolution : `connector_identities.resolve_operated_account_id`
> — pointeur **revalidé contre les grants vivants À CHAQUE appel** (révocation =
> effet immédiat) ; pointeur révoqué = **erreur explicite, jamais de repli** sur le
> compte propre. Capacité `capabilities/connectors_account_grants.py`
> (`oto_{list,grant,revoke}_account_*`, REST `/api/me/connector-accounts/*` ; autz
> `SUB_ONLY`, owner := ctx.sub par construction — pas d'escalade org_admin). ⚠️ La clé
> du grantee doit joindre le compte (clé partagée org/plateforme OK ; owner sur une clé
> BYO perso ≠ celle du grantee → 404 Unipile surfacé).

## API v1 / v2 (client sélectionnable, v1 par défaut)

Le client Unipile existe en **deux versions** dans oto-core
(`oto/tools/unipile/`) exposant la **même surface publique** (les wrappers
`tools/unipile.py` sont inchangés) — factory `make_unipile_client(api_version=…)` :

- **v1** (`client.UnipileClient`) — DSN par compte `apiXX.unipile.com:port/api/v1`,
  `account_id` en query, enveloppe `{items, cursor}`. **Défaut en prod.**
- **v2** (`client_v2.UnipileClientV2`) — API v2 Unipile : base `https://{dsn}/v2`,
  **`account_id` dans le path** (`/v2/{account_id}/…`), enveloppe `{data,
  total_count, next_cursor}` **normalisée** en `{items, cursor}` (aval inchangé),
  surface éclatée (search people/companies par produit, invitations =
  `users/me/relation-requests`, participants = ex-attendees, solde InMail =
  `inmail-credits`). **Beta Unipile** (nouveau compte + migration de données requis).

**Bascule** (`tools/unipile.unipile_client`) : `rc.config['api_version']` de la clé
résolue (la v2 impose un compte/clé dédiés → la version suit la clé), sinon env
`OTO_UNIPILE_API_VERSION` (bascule globale). Défaut `v1`.

**Fixes feedback intégrés au client v2** (la v2 seule ne les donne pas) : garde
**anti-mismatch** identifier↔réponse sur `get_profile`/`get_company` (rejette une
réponse qui ne correspond pas au membre/à la société demandé·e — bug de réponses
croisées #144-149/#153, retryable) ; erreurs réseau mappées proprement (#177) ;
**account_id caviardé** dans les messages d'erreur (#178). `react_message` exige le
`chat_id` en v2 (route sous le fil) — le wrapper ne le passe que s'il est fourni
(compat oto-core sans le kwarg).

> ⚠️ **Déploiement** : le client v2 vit dans oto-core (pin `pyproject`). Shipper la
> bascule v2 = tagger oto-core + bumper le pin ; tant que le pin n'est pas bumpé,
> seul le chemin v1 (défaut) tourne — donc merge sans risque prod.
