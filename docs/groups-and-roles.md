# Groupes (départements) & hiérarchie de droits unifiée

> Statut : implémenté sur la branche `claude/group-principles-departments-k3qa6u`.
> À relier à un ADR du méta-repo (`otomata/docs/adr/0012-*`) au moment du merge.
> Voir aussi `connector-vault.md` (coffre) et `CLAUDE.md` §Visibility / §Doctrines.

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

## Ce qu'un groupe gouverne

Un groupe ≠ juste un label : il **gouverne trois ressources** par **délégation de
l'org** (le reste — entitlements de namespace gouverné — reste au niveau org).

| Ressource | Stockage | Résolution |
|-----------|----------|------------|
| **Doctrine & skills** | `org_group_instructions` (+ revisions), en clair | `get_claude_md()` sert org **puis** groupe actif (complément) |
| **Preset de toolset** | `org_groups.default_tools TEXT[]` (NULL = pas de baseline) | baseline de visibilité au handshake (middleware) |
| **Secrets partagés** | coffre `connector_credentials` (entity_type='group') | cascade `resolve_api_key` |

### Cascade de résolution des secrets (ADR 0012)

```
user_key  >  secret du GROUPE actif  >  secret de l'ORG active  >  grant plateforme
```

Le secret de groupe est le plus spécifique. `is_platform=False` (coût fixe,
jamais métré). Un user sans groupe/org actif → comportement **identique à avant**.

### Preset de toolset (visibilité)

Le chef pose `default_tools` = la baseline visible par défaut pour l'équipe.
Règle effective (`tool_visibility.is_tool_visible`, ordre de priorité) :

1. **grant-only** : barrière inchangée (entitlement) — la baseline ne révèle
   JAMAIS un grant-only (**anti-escalade**, vérifié par test).
2. override perso **positif** (`oto_enable_tool`) → visible.
3. perso **désactivé** (`oto_disable_tool`) → masqué.
4. **baseline de groupe** (si posée) : `dans la baseline → visible` (révèle même
   un masqué-par-défaut), `hors baseline → masqué`.
5. masqué-par-défaut → masqué ; sinon visible.

Les préférences perso priment toujours sur la baseline (le membre garde la main).

## Groupe actif (mirroir de l'org active)

Un user a au plus **un groupe actif** (`org_group_members.is_active`, index
partiel unique par `sub`). **Invariant** : le groupe actif appartient à l'org
active.
- `set_active_group(sub, group_id)` pose AUSSI l'org active = org du groupe (atomique).
- `set_active_org(sub, …)` **efface** le groupe actif (il pointait l'ancienne org).
- Retirer un membre d'une org le retire de tous ses groupes.

`oto_use_group(group_id)` (MCP) / `PUT /api/me/active-group` (REST) basculent ;
`oto_clear_group` / `DELETE /api/me/active-group` reviennent au niveau org.

## Schéma (db.py `_SCHEMA`)

- `org_groups(id, org_id→orgs, name, description, default_tools TEXT[], created_by, created_at, UNIQUE(org_id,name))`
- `org_group_members(group_id→org_groups, sub, group_role, is_active, joined_at, PK(group_id,sub))`
  + index `idx_org_group_members_sub` + partiel unique `org_group_members_one_active`
- `org_group_instructions(group_id, slug, …, version, PK(group_id,slug))` + `…_revisions`
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
- **membres** (`groups_members.py`) : `group.member.{add,set_role,remove}`
  (`GROUP_ADMIN_OF`, garde « dernier chef », cible doit être membre de l'org).
- **secrets + preset** (`groups_secrets.py`) : `group.secret.{set,delete}`,
  `group.preset.set` (`tools=null` efface).
- **doctrine** (`groups_doctrine.py`) : `group.instruction.{list,get,set,delete,
  versions,revert}` — lecture = membre, écriture = chef. Édité par le dashboard
  via `REST /api/groups/{id}/instructions*`.

### `/api/me`

Ajoute `active_group`, `active_group_name`, `group_role` (effectif) ;
`providers[].mode` peut valoir `group` ; `providers[].group_secret_configured`.

## Limites connues

- Sessions MCP déjà ouvertes au moment d'un changement de groupe/preset via REST
  ne sont pas notifiées live (même limite que la visibilité per-user : le hook
  `on_initialize` ne tape qu'à la naissance d'une session).
- Pas de sous-groupes (groupes plats sous l'org) — décision produit v1.
- Les entitlements de namespace gouverné restent **org-level** (non délégués au
  groupe) — décision produit v1.
