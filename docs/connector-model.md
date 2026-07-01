---
title: Modèle de connecteur — les 3 couches
type: explanation
description: >-
  Carte conceptuelle canonique des trois couches orthogonales qui gouvernent tout
  connecteur oto (unipile, google, pennylane, sirene…) : disponibilité (connector_activation
  master ± override org + availability self_serve/platform_granted), authentification
  (cascade resolve_api_key BYO-user > groupe > org > clé plateforme), et option de
  connecteur (has_option = comp admin via option_comps ; plus de billing/Stripe).
  Explique aussi le RBAC interne org-connector-access (ADR 0025). À lire
  AVANT de toucher activation, clés ou options ; les autres docs (connector-vault,
  roles-and-resolution) sont le détail de chaque couche.
adr:
  - "0025"
---

# Modèle de connecteur — les 3 couches

> **Pourquoi ce doc.** Un connecteur (unipile, google, pennylane, sirene…) a son
> comportement gouverné par **trois couches indépendantes** qui se confondent vite.
> Cette page est la carte canonique : avant de toucher activation / clés / options,
> lire ici. Sources de vérité code : `connector_activation.py`, `access.resolve_api_key`,
> `access.has_option`.

Pour qu'un connecteur **marche** pour un utilisateur, les **trois** doivent être OK :

| # | Couche | Question | Substrat |
|---|--------|----------|----------|
| 1 | **Disponibilité** | le connecteur est-il exposé ? | `connector_activation` (master ± override org) + `availability` |
| 2 | **Authentification** | avec quelle clé appelle-t-il l'API ? | cascade `resolve_api_key` (user→groupe→org→clé plateforme) |
| 3 | **Option** *(options gatées only)* | l'option est-elle débloquée ? | `has_option(sub, option)` = comp admin (user\|org) |

La plupart des connecteurs n'ont que **1 + 2**. Seuls les **connecteurs à option gatée**
(unipile, linkedin hébergé) ont la couche **3**.

---

## Couche 1 — Disponibilité (le connecteur est-il exposé ?)

- **Master switch** : `connector_activation` ligne `org_id IS NULL` → activé/désactivé pour
  toute la plateforme. Deny-by-default.
- **Override par org** : `connector_activation` ligne `org_id=<X>` → force on/off pour cette
  org (sinon hérite du master).
- **`availability`** (registre `providers.py`) : `self_serve` (l'user l'installe lui-même, BYO
  possible) | `platform_granted` (deny-by-default, débloqué par un **grant de namespace** admin).
- Appliqué à la **visibilité par session** (middleware) + au catalogue `/api/connectors`.
- Surfaces : `/platform/connectors` (master + clé plateforme, super_admin) ; `/org/connectors`
  (override org).
- **RBAC interne à l'org (ADR 0025)** — grain plus fin que l'org entière : un org_admin réserve
  un connecteur à des **départements (groupes)** et/ou **membres** via `org_connector_access`
  (présence de ≥1 ligne ⟹ RESTREINT/deny-by-default ; absence ⟹ ouvert). **DUR** (réemploi du
  patron grant-only). **3 surfaces d'enforcement cohérentes** : (a) visibilité MCP (`session_visibility`
  masque les tools), (b) **marketplace dashboard** (`/api/me/connectors` via `connectors_selection._visible_catalog`
  → la page `/console/connectors` du membre, donc « voir en tant que » reflète l'effet réel), (c) **backstop
  call-time** `access.require_connector_access` dans `resolve_credential` → bloque **même avec une clé BYO**.
  super_admin bypasse ; fail-open sur erreur infra.
  Surface : `oto_{list,set,clear}_connector_access` / `/api/orgs/{id}/connectors/{acl,…/access}`
  (`ORG_ADMIN_OF`) + levier « accès » sur la carte `/org/connectors`.

## Couche 2 — Authentification (quelle clé ?)

`access.resolve_api_key(provider)` — cascade, **la plus spécifique gagne** :

```
clé perso (BYO user)  >  secret groupe (BYO groupe)  >  secret org (BYO org)  >  clé PLATEFORME (partagée)
```

Deux notions à ne **pas** confondre :
- **BYO** (*bring your own*) : l'entité **pose SA propre clé**, stockée chiffrée dans
  `connector_credentials` (`entity_type` user|group|org). Possible aux **3 niveaux**.
- **Partage de la clé PLATEFORME** : Otomata détient **une** clé (`platform_keys`), et on
  **prête son usage** (métré, **jamais révélée/copiée**) via un **grant**. ⚠️ Aujourd'hui le
  grant de clé plateforme est **per-USER uniquement** (`user_grants`, `access.get_active_grant`).
  **Pas** de partage de clé plateforme au niveau org (trou connu — cf. §Trous).
- `auth_modes` du registre déclare ce qui est permis : `byo_user`, `byo_org`, `platform`.
- Gate de défense : le chemin clé-plateforme n'est valide que si `platform ∈ auth_modes`.

Surfaces : fiche user `/platform/users/<sub>` carte « connector access » → **« grant key »**
(prête la clé plateforme à CET user, métré) ; `/account` (l'user pose sa BYO).

**Attribution côté système tiers (BYO partagé groupe/org).** Un secret **partagé** (byo_org)
= **une seule identité** côté tiers : c'est **oto** qui agit sous le **propriétaire du credential**
(compte de service), pas le membre qui déclenche l'action. Sans effet en **lecture** ; en
**écriture**, l'audit « créé par » est le compte de service, pas l'utilisateur. Mitigation quand
le tiers sépare audit et assignation : poser explicitement le champ **owner** par enregistrement
(map *user oto → user tiers*) — ex. **Zoho CRM** `Owner` (le lead **appartient** au bon
commercial, seul « Created By » reste le compte de service). Attribution **native par personne**
⇒ il faut du **per-user** (BYO user, ou OAuth/mount per-user — cf. fédération MCP), pas un secret
partagé. (Noté 2026-06-24 — pertinent pour l'automatisation d'écriture Zoho (CRM client).)

## Couche 3 — Option de connecteur (unipile, linkedin hébergé)

Certains connecteurs (messagerie hébergée) sont **gatés par une option** : ils consomment des
sièges sur la clé plateforme Otomata, donc l'accès est **accordé par un admin** (plus de paiement —
le modèle billing/Stripe a été retiré, la gouvernance de l'option est purement admin).
Seam unique : **`access.has_option(sub, option)`** — l'option est débloquée si **l'une** des deux :

1. **Comp admin sur l'user** — `option_comps (entity_type='user', entity_id=sub)`.
2. **Comp admin sur l'org active** — `option_comps (entity_type='org', entity_id=org)`.

- **Comp admin** : `option_comps`, **entity-keyé (user|org)**, posé par un super_admin
  (« accorder l'option »).
- Le gate `has_option` est le **seul** point lu par le runtime (`api_routes_connectors`) — ne
  jamais lire les sources en direct dans un nouveau chemin.
- BYO (clé Unipile propre user/groupe/org) court-circuite le gate : l'entité gère sa propre
  instance, pas de siège plateforme à protéger.

Surfaces : bouton **« accorder l'option »** (super_admin) sur la fiche **user** (`option_comps` user)
ET la fiche **org** (`option_comps` org).

---

## Récap — « activer unipile pour quelqu'un »

1. **Disponible** ? unipile master ON (✓ par défaut).
2. **Clé** ? il pose sa clé Unipile (BYO) **ou** un admin lui **grant la clé plateforme** (fiche user → « grant key »).
3. **Option débloquée** ? un admin **accorde l'option** (comp, fiche user ou org) — ou l'entité est en BYO.
4. Puis **lui** connecte son LinkedIn/WhatsApp (hosted-auth, `/console/connectors`).

## Trous connus (à combler)

- **Partage de clé plateforme org-level** : aujourd'hui le grant de clé plateforme est per-user
  seulement ; pas de « partager la clé plateforme à toute une org ». (couche 2)
