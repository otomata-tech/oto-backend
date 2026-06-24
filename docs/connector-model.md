# Modèle de connecteur — les 3 couches

> **Pourquoi ce doc.** Un connecteur (unipile, google, pennylane, sirene…) a son
> comportement gouverné par **trois couches indépendantes** qui se confondent vite.
> Cette page est la carte canonique : avant de toucher activation / clés / abonnement,
> lire ici. Sources de vérité code : `connector_activation.py`, `access.resolve_api_key`,
> `billing.py` + `access.has_option`.

Pour qu'un connecteur **marche** pour un utilisateur, les **trois** doivent être OK :

| # | Couche | Question | Substrat |
|---|--------|----------|----------|
| 1 | **Disponibilité** | le connecteur est-il exposé ? | `connector_activation` (master ± override org) + `availability` |
| 2 | **Authentification** | avec quelle clé appelle-t-il l'API ? | cascade `resolve_api_key` (user→groupe→org→clé plateforme) |
| 3 | **Abonnement** *(options payantes only)* | l'option payante est-elle souscrite ? | `has_option(sub, option)` = Stripe org **OU** comp admin |

La plupart des connecteurs n'ont que **1 + 2**. Seuls les **add-ons payants revendus**
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
  patron grant-only) : masquage visibilité + backstop call-time `access.require_connector_access`
  dans `resolve_credential` → bloque **même avec une clé BYO** ; super_admin bypasse ; fail-open infra.
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

## Couche 3 — Abonnement (options payantes : unipile, linkedin)

Distincte des **crédits d'appel** (1 appel = 1 crédit). L'abonnement paie l'**ACCÈS à l'option**.
Seam unique : **`access.has_option(sub, option)`** — l'option est débloquée si **l'une** des trois :

1. **Comp admin sur l'user** — `option_comps (entity_type='user', entity_id=sub)`.
2. **Comp admin sur l'org active** — `option_comps (entity_type='org', entity_id=org)`.
3. **Abonnement Stripe de l'org active** — `org_subscriptions` (status active/trialing/past_due).

- **Stripe** (payant) : org-only, géré par les webhooks (`billing.py`). Tarif dégressif 15/10/7 €
  par compte connecté. Source de vérité du *payé*.
- **Comp admin** (gratuit) : `option_comps`, **entity-keyé (user|org)**, posé par un super_admin
  (« offrir l'option »). N'a **pas** de `stripe_subscription_id` → les webhooks ne l'écrasent pas.
- Le gate `has_option` est le **seul** point lu par le runtime (`api_routes_connectors`) — ne
  jamais lire `has_active_unipile_subscription` en direct dans un nouveau chemin.

Surfaces : bouton **« offrir l'option »** (super_admin) sur la fiche **user** (`option_comps` user)
ET la fiche **org** (`option_comps` org) ; bouton Stripe **« activate · €15/mo »** côté user (payant).

---

## Récap — « activer unipile pour quelqu'un »

1. **Disponible** ? unipile master ON (✓ par défaut).
2. **Clé** ? il pose sa clé Unipile (BYO) **ou** un admin lui **grant la clé plateforme** (fiche user → « grant key »).
3. **Option souscrite** ? son org paie Stripe (€15/mo) **ou** un admin **offre l'option** (comp, fiche user ou org).
4. Puis **lui** connecte son LinkedIn/WhatsApp (hosted-auth, `/console/connectors`).

## Trous connus (à combler)

- **Partage de clé plateforme org-level** : aujourd'hui le grant de clé plateforme est per-user
  seulement ; pas de « partager la clé plateforme à toute une org ». (couche 2)
