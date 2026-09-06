"""DDL du domaine « orgs » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# table racine `orgs`, créée avant ses référents
ORGS = """
-- Palier organization (= périmètre / store serveur) — table RACINE, définie en
-- tête car des tables plus bas la référencent (`unipile_accounts` etc.) : sur une
-- base VIERGE, PostgreSQL crée les tables dans l'ordre du DDL et une FK vers une
-- table non encore créée échoue (`relation "orgs" does not exist`, #151). Détail
-- du palier (appartenance, credentials) au bloc org_members plus bas.
CREATE TABLE IF NOT EXISTS orgs (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    logo_url TEXT,
    domain TEXT,
    industry TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# appartenance et invitations
MEMBERSHIP = """
-- Palier organization : la table `orgs` est définie en TÊTE du schéma (près de
-- `users`, tables racines) — cf. la note là-bas (#151). Une org possède des
-- credentials propres (coffre `connector_credentials`, entity_type='org') et des
-- opérateurs (org_members) ; source de vérité de l'appartenance = ces tables,
-- résolues par `sub` — JAMAIS un claim du token Logto (le token MCP ne porte que
-- sub). Cf. project_oto_mcp_org_tier.

-- org_role : 'org_admin' | 'org_member' (validé en code, pas par CHECK, comme
-- users.role). is_active = org courante du sub (au plus une TRUE par sub,
-- garantie par l'index partiel + l'écriture ; même pattern que
-- user_google_oauth.is_default).
CREATE TABLE IF NOT EXISTS org_members (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    org_role TEXT NOT NULL DEFAULT 'org_member',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, sub)
);
CREATE INDEX IF NOT EXISTS idx_org_members_sub ON org_members(sub);
CREATE UNIQUE INDEX IF NOT EXISTS org_members_one_active ON org_members(sub) WHERE is_active;

-- Les credentials d'org (Attio, Pennylane, le token d'un bridge…) vivent dans le coffre
-- chiffré `connector_credentials` (entity_type='org'), pas dans une table dédiée.


-- Invitations (onboarding SaaS). Le token plaintext n'est jamais stocké
-- (seulement son hash, comme user_api_tokens). accepted_at NULL = en attente.
-- **Feature cascade plateforme/org/équipe** (comme les connecteurs) : le SCOPE est
-- dérivé des cibles → `org_id` NULL = invitation plateforme (onboarding pur) ;
-- `org_id` seul = invitation d'org ; `org_id`+`group_id` = invitation d'équipe
-- (colonnes `group_id`/`group_role` ajoutées par ALTER dans _init, après org_groups).
-- `org_id` NULLABLE (plateforme + héritage). `source` = provenance
-- ('org_admin' | 'group_admin' | 'platform_admin').
-- `email` NULLABLE : une invitation nominative cible un email, mais une émission
-- « code à partager soi-même » (sans envoi mail) peut être anonyme. `code` = code
-- court lisible (lien /invitation/<code>), saisi/partagé à la main ; c'est le
-- secret d'accès single-use (≠ token_hash legacy du lien mail).
-- `declined_at`/`declined_sub` = le REFUS de l'invité (oto-backend#654), symétrique
-- d'accepted_at/accepted_sub et surtout DISTINCT d'eux : une invitation refusée
-- quitte la file d'attente sans créer d'appartenance. Écrire son refus dans
-- `accepted_at` aurait rendu les deux gestes indiscernables partout — et
-- `_idempotent_accept` aurait resservi un succès « tu as rejoint l'org » à qui
-- vient de refuser. Colonnes ajoutées par ALTER dans _init pour les DB existantes.
CREATE TABLE IF NOT EXISTS org_invitations (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
    email TEXT,
    org_role TEXT NOT NULL DEFAULT 'org_member',
    token_hash TEXT NOT NULL UNIQUE,
    code TEXT,
    invited_by TEXT,
    source TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    accepted_sub TEXT,
    declined_at TIMESTAMPTZ,
    declined_sub TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_invitations_org ON org_invitations(org_id);
-- idx_org_invitations_code NON déclaré ici : `code` est ajouté par ALTER (DB
-- existantes) APRÈS ce _SCHEMA → l'index sur `code` vit dans le bloc migration,
-- après l'ADD COLUMN (sinon UndefinedColumn au boot sur une table préexistante).
"""

# équipes et leurs membres
GROUPS = """
-- Sous-palier GROUPE (= départements / équipes au sein d'une org, ADR 0012).
-- Une org se subdivise en groupes plats (pas de sous-groupes en v1) ; chaque
-- groupe a un chef d'équipe (group_role='group_admin'). Modèle de droits
-- hiérarchique unifié (platform_admin > org_admin > group_admin > member) :
-- la résolution effective vit dans `roles.py`, l'appartenance dans ces tables.
-- Un groupe GOUVERNE deux ressources, par DÉLÉGATION de l'org : la doctrine
-- (org_group_instructions) et des secrets partagés (coffre
-- `connector_credentials`, entity_type='group'). Source de vérité de
-- l'appartenance = ces tables, résolues par `sub`.
CREATE TABLE IF NOT EXISTS org_groups (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (org_id, name)
);
CREATE INDEX IF NOT EXISTS idx_org_groups_org ON org_groups(org_id);

-- group_role : 'group_admin' (chef d'équipe) | 'group_member' (validé en code,
-- pas par CHECK, comme org_members.org_role). is_active = groupe courant du sub
-- (au plus une TRUE par sub, garantie par l'index partiel — même pattern que
-- org_members.is_active). INVARIANT : le groupe actif appartient toujours à
-- l'org active du sub (posé par set_active_group ; effacé par set_active_org
-- quand l'org bascule).
CREATE TABLE IF NOT EXISTS org_group_members (
    group_id BIGINT NOT NULL REFERENCES org_groups(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    group_role TEXT NOT NULL DEFAULT 'group_member',
    is_active BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, sub)
);
CREATE INDEX IF NOT EXISTS idx_org_group_members_sub ON org_group_members(sub);
CREATE UNIQUE INDEX IF NOT EXISTS org_group_members_one_active
    ON org_group_members(sub) WHERE is_active;

-- (Les procédures d'ÉQUIPE vivent dans `org_instructions` avec owner_type='group'
--  depuis la fusion du chantier procédures — cadrage 10/07, Lot B/C. Les tables
--  jumelles org_group_instructions/+revisions sont DROPpées en Lot C.)
"""
