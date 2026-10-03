---
title: Groupes (départements) & hiérarchie de droits unifiée
type: explanation
description: >-
  Explique l'architecture des groupes (départements) dans oto-backend : hiérarchie
  de droits centralisée dans roles.py (platform_admin ⊇ org_admin ⊇ group_admin ⊇
  member, escalade descendante), les deux ressources gouvernées par délégation
  (secrets partagés dans connector_credentials entity_type='group', procédures dans
  org_instructions owner_type='group'), et la cascade
  de résolution user_key > secret groupe actif > secret org > grant plateforme (ADR 0012).
  Détaille le schéma DB (org_groups, org_group_members avec index partiel one_active),
  l'invariant groupe⊂org actif, et les surfaces MCP/REST
  via capacités groups*.py. À lire pour comprendre la délégation d'accès par équipe.
adr:
  - "0012"
---

# Groupes (départements) & hiérarchie de droits unifiée

> Statut : implémenté sur la branche `claude/group-principles-departments-k3qa6u`.
> À relier à un ADR du méta-repo (`otomata/docs/adr/0012-*`) au moment du merge.
> Voir aussi `connector-vault.md` (coffre) et `CLAUDE.md` §Visibility / §Guides.

## Pourquoi

Une org oto était une **liste plate** de membres avec deux rôles
(`org_admin`/`org_member`) + le rôle plateforme (`users.role` = `member`/`admin`).
Pour un client qui veut **structurer son org en départements** avec un **chef
d'équipe** par département, il manquait un palier intermédiaire. On l'ajoute sans
refaire l'autz à la main partout : on **centralise la hiérarchie de droits**.

## La hiérarchie unifiée (source unique : `roles.py`)

```
platform_admin   (users.role = 'admin')
   ⊇ org_admin      (org_members.org_role = 'org_admin')
       ⊇ group_admin    (org_group_members.group_role = 'group_admin')  ← chef d'équipe
           ⊇ member         (org_member / group_member)
```

**Escalade descendante** : un rôle supérieur *subsume* les inférieurs.
- `platform_admin` agit comme org_admin de TOUTE org et group_admin de TOUT groupe.
- `org_admin` d'une org agit comme group_admin de TOUS ses groupes.

Avant, cette escalade était recopiée dans chaque combinateur d'autz
(`role == ADMIN or org_store.get_org_role(...) == 'org_admin'`). Désormais elle
vit **uniquement** dans `roles.py` :

- `roles.is_org_admin(sub, org_id)` / `is_org_member(sub, org_id)`
- `roles.can_admin_group(sub, group_id)` — chef d'équipe, ou org_admin parent, ou platform
- `roles.can_read_group(sub, group_id)` — membre du groupe, ou les ci-dessus
- `roles.effective_group_role(sub, group_id)` — pour `/api/me` + l'UI

Les combinateurs de la couche capacité (`capabilities/_authz.py`) délèguent à
`roles` : `ORG_ADMIN_OF`, `ORG_MEMBER_OF`, `GROUP_ADMIN_OF`, `GROUP_MEMBER_OF`.
Ajouter un palier plus tard = un seul endroit à toucher.

### Un refus d'administration dit QUI le lève (oto#108)

Deux fabriques, une par palier, et toute garde d'administration passe par elles :
`_authz._refus_org_admin(org_id, sub=…)` et `_authz._refus_chef_d_equipe(group_id,
sub=…)` (règles `ORG_ADMIN*`, `GROUP_ADMIN_OF`, `GROUP_ADMIN_OPT`, et les paliers
`org`/`group` de la clé : `me.credential.get|clear`, `oto_identity`, sessions de
navigateur ; Salesforce et l'envoi différé disent la même chose par leur propre texte).
Le refus nomme le **rôle** et le **niveau** (l'org #N, ou l'équipe #N et son org) ; à un
**membre de cette org**, et à lui seul (`detenteurs.est_membre` = `roles.is_org_member`,
qui y range aussi l'admin plateforme), il nomme aussi **les personnes** :
administrateurs de l'org, chefs de l'équipe — **par leur nom seul, jamais leur adresse**
(décision produit), sans les comptes en pause, cinq au plus ; un détenteur sans nom est
compté, pas tu.
REST : `details = {required_role, scope, org_id[, group_id], holders}`, chaque
détenteur réduit à `{name}`. Un palier sans
personne joignable le dit. Une **option payante** refusée (`unipile_option_required`,
`paid_option_refusal`) dit ses deux portes : un administrateur de l'org souscrit (page de
facturation), ou l'équipe de la plateforme l'offre — `detenteurs.qui_leve_une_option`.
Le cas qui l'a fait naître : une simple membre, gestionnaire du compte d'un fournisseur,
refusée sur la clé d'équipe sans savoir à qui s'adresser — et un secret envoyé par un
lien externe pour qu'un administrateur le recolle.

## Ce qu'un groupe gouverne

Un groupe ≠ juste un label : il **gouverne deux ressources** par **délégation de
l'org** (le reste — entitlements de namespace gouverné — reste au niveau org).

| Ressource | Stockage | Résolution |
|-----------|----------|------------|
| **Procédures** | `org_instructions` (`owner_type='group'`) + revisions, en clair | `oto_procedure(op='get')` sert org **puis** groupe actif (complément) |
| **Secrets partagés** | coffre `connector_credentials` (entity_type='group') | cascade `resolve_api_key` |

### Cascade de résolution des secrets (ADR 0012)

```
user_key  >  secret du GROUPE actif  >  secret de l'ORG active  >  grant plateforme
```

Le secret de groupe est le plus spécifique. `is_platform=False` (coût fixe,
jamais métré). Un user sans groupe/org actif → comportement **identique à avant**.

### Visibilité des outils

Il n'y a plus de baseline de toolset de groupe/org (les presets de tools ont été
retirés). La visibilité effective (`tool_visibility.is_tool_visible`, ordre de
priorité) ne dépend que des défauts plateforme et des toggles perso :

1. **grant-only** : barrière d'entitlement inchangée.
2. **méta-tools protégés** (`PROTECTED_TOOLS`) → toujours visibles (anti-lockout).
3. override perso **positif** (`oto_enable_tool`) → visible.
4. perso **désactivé** (`oto_disable_tool`) → masqué.
5. masqué-par-défaut → masqué ; sinon visible.

## Groupe actif (mirroir de l'org active)

Un user a au plus **un groupe actif** (`org_group_members.is_active`, index
partiel unique par `sub`). **Invariant** : le groupe actif appartient à l'org
active.
- `set_active_group(sub, group_id)` pose AUSSI l'org active = org du groupe (atomique).
- `set_active_org(sub, …)` **efface** le groupe actif (il pointait l'ancienne org).
- Retirer un membre d'une org le retire de tous ses groupes.

`oto_use_group(group_id)` (MCP) / `PUT /api/me/active-group` (REST) basculent ;
`oto_clear_group` / `DELETE /api/me/active-group` reviennent au niveau org.

**Tenant opt-in — un membre d'équipe n'est jamais « sans équipe »**
(`OTO_EQUIPE_PAR_DEFAUT_TENANTS`, slugs séparés par des virgules ; absente = aucun) :
pour une org de ces tenants, là où `access.current_group` rendrait le niveau org faute
d'équipe désignée (consultation `X-Oto-Org`, jeton `_org=`, org du run, maison), il rend
l'équipe du sub DANS cette org — `group_store.default_group_in_org` : l'active si elle ⊂
org, sinon la première rejointe. Lecture seule. `X-Oto-Group: 0` garde le niveau org ;
`oto_clear_group` efface le défaut persisté et rend l'équipe effective.

## Schéma (db.py `_SCHEMA`)

- `org_groups(id, org_id→orgs, name, description, created_by, created_at, UNIQUE(org_id,name))`
- `org_group_members(group_id→org_groups, sub, group_role, is_active, joined_at, PK(group_id,sub))`
  + index `idx_org_group_members_sub` + partiel unique `org_group_members_one_active`
  ⚠️ Les procédures d'équipe **n'ont plus de table à elles** : `org_instructions`
  (`owner_type='group'`, `owner_id=group_id::text`), jumelle `org_group_instructions`
  DROPpée. Elles n'ont plus non plus de STORE à elles depuis le 31/08/2026 (#681) : un
  seul jeu de fonctions, `org_store.<fn>('group', group_id, …)`.
- secrets de groupe : `connector_credentials(entity_type='group', entity_id=group_id::text, …)`

Toutes les FK `ON DELETE CASCADE` vers `org_groups` / `orgs` ; les secrets de
groupe (hors FK) sont purgés explicitement par `delete_group`.

## Surfaces

### Capacités (ADR 0009 — REST + MCP co-déclarés)

`capabilities/groups*.py`, montées automatiquement (registre).

- **CRUD / actif** (`groups.py`) : `group.create` (org_admin), `group.list`
  (membre org, REST), `group.list_mine` (MCP `oto_list_groups`), `group.use`
  (`oto_use_group` + `PUT /api/me/active-group`), `group.clear`, `group.get`,
  `group.update`, `group.delete`.
- **membres** (`groups/members.py`) : `group.member.{add,set_role,remove}`
  (`GROUP_ADMIN_OF`, cible doit être membre de l'org). Les trois passent par la MÊME
  garde anti-lockout — **« une équipe a toujours quelqu'un qui peut l'administrer, le
  responsable d'organisation compris »** (#280) : retirer/rétrograder le dernier chef
  explicite est **autorisé** (l'org_admin administre toutes les équipes de son org) ;
  seul l'état sans personne — zéro chef ET zéro `org_admin` dans l'org — est refusé
  (409 `group_unadministrable`). `add` étant un upsert, il porte la garde comme
  `set_role` (avant #280 il rétrogradait ce que l'autre refusait).
- **secrets** (`groups/secrets.py`) : `group.secret.{set,delete}`.
- **procédures** (`groups/guide.py`) : `group.instruction.{list,get,set,delete,
  versions,revert}` — **la garde suit le VERBE** : `list`/`get`/`set`/`revert` =
  **membre** de l'équipe, `delete` = **chef**. Écrire et restaurer sont des gestes de
  travail et se défont (une version de plus) ; supprimer emporte l'historique sans
  corbeille. Édité par le dashboard via `REST /api/groups/{id}/instructions*`.
  ⚠️ Depuis #681, la console MCP `oto_procedure(op='set'|'delete', scope='group'[,
  group=N])` sert le MÊME palier avec les MÊMES gardes — c'est par là qu'un opérateur
  métier annote la procédure qu'il déroule, sans être ni administrateur de toute l'org
  ni chef de son équipe. Les deux transports écrivent la même ligne : leurs gardes se
  déplacent ensemble.
  ⚠️ Le bundle `list` sert **trois** droits, et il faut lire le bon : `can_edit` =
  `can_admin_group`, le droit d'ADMINISTRER l'équipe (readme, membres, secrets,
  suppression) — inchangé ; `can_write_instructions` et `can_delete_instructions` = les
  droits sur les PROCÉDURES, un par verbe. Les boutons d'une procédure se conditionnent
  aux deux derniers : `can_edit` sous-estimait l'écriture (`false` à une membre qui
  pouvait écrire) et l'élargir aurait affiché un bouton de suppression que le serveur
  refuse. Les mêmes deux noms sont servis par `GET /api/me/instructions` au palier org
  (où ils valent tous deux `org_admin` aujourd'hui), pour qu'un écran factorisé lise le
  même champ sur les deux pages. Chaque drapeau est calculé par la règle d'autz
  **déclarée** par la capacité qu'il nomme, jamais par une copie du critère.

### `/api/me`

Ajoute `active_group`, `active_group_name`, `group_role` (effectif) ;
`providers[].mode` peut valoir `group` ; `providers[].group_secret_configured`.

## Limites connues

- Sessions MCP déjà ouvertes au moment d'un changement de groupe via REST
  ne sont pas notifiées live (même limite que la visibilité per-user : le hook
  `on_initialize` ne tape qu'à la naissance d'une session).
- Pas de sous-groupes (groupes plats sous l'org) — décision produit v1.
- Les entitlements de namespace gouverné restent **org-level** (non délégués au
  groupe) — décision produit v1.

## Gouvernance de connecteur par l'équipe (ADR 0012 B1/B2, restrict-only)

Migré de la carte : le détail des paliers, du fail-open par palier et des
capacités n'a pas sa place dans un index.

**gouvernance de connecteur (ADR 0012 B1, restrict-only — 08/07/2026)** — le chef
  d'équipe peut, pour SON équipe, **couper** un connecteur (lignes scope 'group' de
  `connector_availability`, coupures seules).
  **INVARIANT MONOTONE** : l'équipe ne peut que RÉTRÉCIR ce que l'org expose, jamais élargir
  (platform ⊇ org ⊇ group). Dispo = **visibilité** (`session_visibility`, fail-open,
  `connector_activation.effective_for_group`/`group_cut_connectors`) **et appel**
  (`connectors/activation_gate.py`, refus `connector_disabled`, direct comme par `oto_call`,
  #1064).
  Capacités `connectors.activation.{group_list,set_group,clear_group}` (GROUP_*). REST
  `/api/groups/{id}/connectors[/{name}]/activation`.
  **Réserver un connecteur à des membres (ex-ADR 0012 B2) n'existe plus depuis le
  24/09/2026** (ADR 0053 D1) : on pose la clé au niveau de l'équipe, ou en clé perso.

## Ce que la carte en disait (migré le 2026-08-27)

Une org se subdivise en **groupes** (départements/équipes) avec un **chef
d'équipe** (`group_role='group_admin'`). La gestion des droits est **centralisée**
dans `roles.py` (escalade descendante, source unique) :

```
platform_admin ⊇ org_admin ⊇ group_admin (chef) ⊇ member
```

Les combinateurs d'autz (`capabilities/_authz.py`) délèguent à `roles`
(`is_org_admin`, `can_admin_group`, `can_read_group`, `effective_group_role`) —
plus d'escalade recopiée à la main. Combinateurs : `GROUP_ADMIN_OF`,
`GROUP_MEMBER_OF` (en plus de `ORG_*`).

Un groupe **gouverne 3 ressources** par délégation de l'org (⚠️ **substrat unifié le
10/07/2026** — chantiers du cadrage objets/visibilité : plus de tables jumelles par
grain, le scope est une COLONNE ; migrations vivantes sur la DB partagée = playbook
**`docs/live-migrations.md`**) :
- **secrets partagés** — coffre `connector_credentials` (entity_type='group') ;
  le secret de groupe passe **après** le personnel et **avant** celui de l'org ; l'ordre
  complet des barreaux (dont perso cross-org et tenant) est dans
  [`roles-and-resolution.md`](roles-and-resolution.md), qui l'énonce seul.
- **procédures** — table UNIFIÉE `org_instructions` (`owner_type='group'`,
  `owner_id=group_id`, `org_id`=org parente ; ex-jumelle `org_group_instructions`
  DROPpée) et, depuis le 31/08/2026 (#681), **store unifié** aussi :
  `org_store.<fn>('group', id, …)`, plus de `group_store.*_group_instruction*`.
  `oto_procedure(op='get'|'list')` sert org **puis** groupe actif (complément, chaque
  skill taggée `scope`) ; `op='set'|'delete'` avec `scope='group'` ÉCRIT ce palier.
  ⚠️ **La garde y suit le VERBE** : `set` = **membre** de l'équipe (celui qui déroule est
  celui qui améliore, et l'écriture se défait par `from_version`), `delete` = **chef**
  (il emporte l'historique, rien ne le défait). Mêmes paliers sur les routes
  `/api/groups/{id}/instructions*`. Les procédures d'équipe ont un `id` (ownership 0030), donc
  se déplacent d'un palier à l'autre en gardant leurs versions, leurs slots et leurs
  liens de projet (`oto_resource op=transfer`).
- **gouvernance de connecteur** — le chef d'équipe peut COUPER un connecteur et le
  RÉSERVER à des membres, pour son équipe seulement. **Invariant monotone** :
  l'équipe RÉTRÉCIT ce que l'org expose, jamais l'inverse (platform ⊇ org ⊇ group).
  Détail (paliers, fail-open indépendant, capacités) : `docs/groups-and-roles.md`.

**Groupe actif** : ≤1 par sub (`org_group_members.is_active`, index partiel),
**invariant** = appartient à l'org active. `set_active_group` pose aussi l'org
active ; `set_active_org` efface le groupe actif. `oto_use_group` /
`PUT /api/me/active-group` (+ `oto_clear_group` / `DELETE`).

Stores : `group_store.py` (miroir d'`org_store` au grain groupe — **hors procédures**,
  qui ont fusionné dans `org_store/instructions.py`). **Aucun module
du package `org_store/` n'importe `group_store`** : l'invariant org↔groupe est
tenu en SQL direct dans `org_store/members.py` (`remove_org_member` sort le membre
de tous les groupes de l'org, `set_active_org` invalide le groupe actif) → pas de
cycle. Règle **vérifiée** depuis la découpe du 2026-08-27 par
`tests/test_org_store_surface_frozen.py`, plus seulement tenue à la main. Surfaces : capacités `capabilities/groups*.py` (REST `/api/orgs/{id}/groups`,
`/api/groups/{id}*`, `/api/me/active-group` + MCP `oto_*_group*`). `/api/me`
expose `active_group`/`active_group_name`/`group_role` ; `providers[].mode` peut
valoir `group`. **Détails : `docs/groups-and-roles.md`.**
