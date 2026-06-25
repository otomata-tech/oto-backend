"""PostgreSQL-backed user store (Scaleway managed `otomata-main`).

One row per Logto user (`sub` = primary key). Holds per-user settings :
- `role` — member (défaut) / admin (opérateur) / super_admin (tout-puissant)
  (cf. `access.py` ; `guest` retiré, migré en member ; validé en code, pas par CHECK)
- LinkedIn / Crunchbase session cookies + user-agent
- API keys par provider (serper/hunter/sirene/attio/lemlist) — plaintext,
  isolation par ACL réseau + creds en SOPS.
- Compteur d'usage `usage(sub, tool, day)` pour les quotas member.

Connexion via `DATABASE_URL` (postgresql://…?sslmode=require). Pool psycopg
géré au module ; toutes les fonctions restent sync.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timezone
from typing import Any, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import connectors

logger = logging.getLogger(__name__)


def _normalize_value(v: Any) -> Any:
    # Match the string shape SQLite returned ("YYYY-MM-DD HH:MM:SS") so downstream
    # JSONResponse + frontends keep working unchanged.
    if isinstance(v, datetime):
        return v.replace(tzinfo=None, microsecond=0).isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _str_dict_row(cursor):
    inner = dict_row(cursor)

    def make_row(values):
        d = inner(values)
        if d:
            for k, v in d.items():
                if isinstance(v, (datetime, date)):
                    d[k] = _normalize_value(v)
        return d

    return make_row


_SCHEMA = """
-- Identité seule. Les credentials (clés API, sessions linkedin/crunchbase,
-- OAuth Google) vivent TOUS dans le coffre chiffré `connector_credentials`.
CREATE TABLE IF NOT EXISTS users (
    sub TEXT PRIMARY KEY,
    email TEXT,
    name TEXT,
    role TEXT NOT NULL DEFAULT 'member',  -- member | admin (opérateur) | super_admin

    -- Accès plateforme & invitation virale (ADR 0013). access_status = gate doux
    -- (pending = waitlist, active = alpha, blocked). invite_quota = budget referral
    -- restant. invited_by = sub du parrain (arbre viral). Non appliqué tant que le
    -- flag OTO_ALPHA_GATE_ENABLED est off (barreaux ultérieurs).
    access_status TEXT NOT NULL DEFAULT 'pending',
    invite_quota INTEGER NOT NULL DEFAULT 0,
    invited_by TEXT,
    access_granted_at TIMESTAMPTZ,
    -- Code referral stable, partageable au réseau (lien /invitation/<code>).
    -- Non secret (destiné à être diffusé), lazy-généré à la 1re demande.
    referral_code TEXT,
    avatar_url TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage (
    sub TEXT NOT NULL,
    tool TEXT NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sub, tool, day)
);

-- Journal des appels MCP (monitoring admin). Une ligne par appel de tool,
-- posée par otomata_calllog.ToolCallLogger (succès comme échec). Schéma
-- CANONIQUE otomata-calllog (contrat inter-projets ogic/ytmusic/memento).
-- Volumétrie bornée par un prune au boot (cf. prune_tool_calls + init_db).
-- `sub` nullable : les appels stdio local non authentifiés n'ont pas d'identité.
CREATE TABLE IF NOT EXISTS tool_calls (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    server TEXT NOT NULL DEFAULT 'oto',
    sub TEXT,
    email TEXT,
    tool TEXT NOT NULL,
    args JSONB,
    ok BOOLEAN NOT NULL DEFAULT TRUE,
    error TEXT,
    duration_ms INTEGER,
    -- Corrélation (ADR 0017, extension OTO-LOCALE — PAS dans le contrat canonique
    -- otomata-calllog) : session_id = session mcp transport (grossier) ; run_id =
    -- déroulé/run (fin, posé par run_start, stampé ici). NULL hors run.
    session_id TEXT,
    run_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at ON tool_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_sub ON tool_calls(sub);
CREATE INDEX IF NOT EXISTS idx_tool_calls_server_tool ON tool_calls(server, tool, created_at);
-- idx_tool_calls_run (sur run_id) créé dans le bloc ALTER de init_db, APRÈS l'ADD
-- COLUMN run_id : sur une table existante, CREATE TABLE IF NOT EXISTS est un no-op
-- donc la colonne n'existe pas encore ici (sinon crash UndefinedColumn au boot).

-- Signaux d'usage volontaires (ADR 0017, barreau 3) : feedback de l'agent/humain
-- sur un outil + cas d'usage non couverts (gap). DURABLE (hors prune 30j de
-- tool_calls) : c'est le signal qui pilote révisions d'outils/doctrines + backlog.
-- Le face-agent est AUSSI un tool_call (auto-journalisé, corrélé run_id) ; cette
-- table porte le CONTENU durable. Table neuve → indexes inline sûrs.
CREATE TABLE IF NOT EXISTS usage_signals (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sub TEXT,
    org_id BIGINT,
    signal TEXT NOT NULL,        -- 'tool_feedback' | 'gap'
    kind TEXT NOT NULL,          -- feedback: bug|misleading_doc|wrong_result|praise|other ; gap: missing_tool|missing_doctrine|missing_data|other
    target TEXT,                 -- feedback: nom de l'outil ; gap: l'intention (ce qu'on voulait faire)
    body TEXT,                   -- description libre
    session_id TEXT,             -- corrélation session (face-agent) ; NULL côté humain
    source TEXT NOT NULL DEFAULT 'agent',  -- 'agent' (MCP) | 'human' (REST dashboard)
    resolved_at TIMESTAMPTZ,     -- NULL = ouvert ; date = signal traité
    resolved_by TEXT,            -- sub de l'opérateur ayant résolu
    resolution TEXT              -- note libre : ce qui a été fait
);
CREATE INDEX IF NOT EXISTS idx_usage_signals_signal ON usage_signals(signal, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_signals_target ON usage_signals(signal, target, created_at DESC);

-- Visibilité scopée par org (ADR 0015) : org_id=0 = profil perso/global (aucune
-- org active), >0 = profil de cette org. Une identité par (sub, org_id).
CREATE TABLE IF NOT EXISTS user_disabled_tools (
    sub TEXT NOT NULL,
    org_id BIGINT NOT NULL DEFAULT 0,
    tool_name TEXT NOT NULL,
    disabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id, tool_name)
);

-- Ensemble positif explicite : tools que l'user a activé alors qu'ils sont
-- masqués par défaut (DEFAULT_HIDDEN_TOOLS). Sans cette table, un tool
-- default-hidden ne pourrait jamais être rendu visible (le modèle de base
-- n'a qu'un ensemble négatif).
CREATE TABLE IF NOT EXISTS user_enabled_tools (
    sub TEXT NOT NULL,
    org_id BIGINT NOT NULL DEFAULT 0,
    tool_name TEXT NOT NULL,
    enabled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id, tool_name)
);

CREATE TABLE IF NOT EXISTS user_presets (
    sub TEXT NOT NULL,
    org_id BIGINT NOT NULL DEFAULT 0,
    name TEXT NOT NULL,
    enabled_tools TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, org_id, name)
);

-- Onboarding par utilisateur (fiche « situation avec oto »). `profile` = data
-- model libre nourri au fil du self-onboarding (qui est l'user, son métier, ses
-- objectifs, connecteurs voulus…) ; `onboarded` = booléan validé quand l'accueil
-- est terminé. Une ligne par sub, créée à la 1re lecture.
CREATE TABLE IF NOT EXISTS user_account_profile (
    sub TEXT PRIMARY KEY,
    onboarded BOOLEAN NOT NULL DEFAULT FALSE,
    profile JSONB NOT NULL DEFAULT '{}'::jsonb,
    onboarded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS platform_keys (
    id BIGSERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    label TEXT NOT NULL,
    api_key_enc TEXT,                      -- enveloppe AES-256-GCM (chiffrement obligatoire)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(provider, label)
);

CREATE TABLE IF NOT EXISTS user_grants (
    sub TEXT NOT NULL,
    platform_key_id BIGINT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by TEXT,
    PRIMARY KEY (sub, platform_key_id),
    FOREIGN KEY (platform_key_id) REFERENCES platform_keys(id) ON DELETE CASCADE
);

-- Grant de clé plateforme au niveau ORG (couche 2 du modèle de connecteur) : partager
-- la clé plateforme à TOUS les membres d'une org, sans grant per-user. Miroir de
-- user_grants au grain org ; résolu par access.resolve_api_key (cran org platform-grant,
-- après le grant user). quota = per-membre (réutilise get_usage_today(sub)).
CREATE TABLE IF NOT EXISTS org_grants (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    platform_key_id BIGINT NOT NULL REFERENCES platform_keys(id) ON DELETE CASCADE,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by TEXT,
    daily_quota INTEGER,
    PRIMARY KEY (org_id, platform_key_id)
);

-- RBAC connecteur INTERNE à l'org (ADR 0025) : l'org_admin réserve un connecteur à
-- un sous-ensemble de son org (départements et/ou membres). La PRÉSENCE de ≥1 ligne
-- pour (org_id, connector) ⟹ connecteur RESTREINT dans l'org (deny-by-default) ;
-- absence ⟹ ouvert à tous les membres. principal = un groupe (department) ou un user.
-- DUR : enforced en visibilité (session_visibility) + au call-time (resolve_credential
-- via access.require_connector_access). Ouvert par défaut = zéro disruption.
CREATE TABLE IF NOT EXISTS org_connector_access (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    connector TEXT NOT NULL,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('group', 'user')),
    principal_id TEXT NOT NULL,   -- group_id (en texte) ou sub
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, connector, principal_type, principal_id)
);

-- Grants de namespace sensible (deny-by-default). Un user non-admin ne voit/
-- n'appelle un tool d'un ADMIN_GRANT_ONLY_NAMESPACE que s'il a une ligne ici,
-- posée par un admin. Distinct de user_grants (qui porte une platform_key/quota).
CREATE TABLE IF NOT EXISTS user_namespace_grants (
    sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, namespace)
);

-- Datastore = spine natif PG (ADR 0016). `user_datastores` = registre de
-- namespaces ; les rows vivent dans `datastore_rows` (JSONB). `spreadsheet_id`/
-- `owner_email` sont des reliques Sheets (nullable, plus utilisées — DROP différé
-- post-backfill).
CREATE TABLE IF NOT EXISTS user_datastores (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    spreadsheet_id TEXT,
    owner_email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(sub, namespace)
);

-- Rows du datastore : un dict JSONB par row (types préservés nativement, fin de
-- la sentinelle `__j:`). `_id`/`_created_at`/`_updated_at` = colonnes, le reste
-- des champs user dans `data`. CASCADE sur la suppression du namespace.
CREATE TABLE IF NOT EXISTS datastore_rows (
    ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
    row_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ns_id, row_id)
);

CREATE TABLE IF NOT EXISTS datastore_shares (
    id BIGSERIAL PRIMARY KEY,
    owner_sub TEXT NOT NULL,
    namespace TEXT NOT NULL,
    spreadsheet_id TEXT,
    shared_with_sub TEXT NOT NULL,
    permission TEXT NOT NULL DEFAULT 'write',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(owner_sub, namespace, shared_with_sub)
);
CREATE INDEX IF NOT EXISTS idx_datastore_shares_recipient ON datastore_shares(shared_with_sub, namespace);

CREATE TABLE IF NOT EXISTS user_api_tokens (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    label TEXT NOT NULL DEFAULT 'cli',
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ  -- NULL = non-expirant (token CLI long-lived). Sinon rejeté passé l'échéance.
);
CREATE INDEX IF NOT EXISTS idx_user_api_tokens_sub ON user_api_tokens(sub);

-- Unipile : mapping per-user du compte LinkedIn connecté sous l'abonnement
-- Unipile (B3). La CLÉ Unipile est partagée (org secret) ; chaque user connecte
-- SON LinkedIn par hosted-auth → un `account_id` distinct sous la même clé. Ce
-- n'est PAS un secret (handle opaque), d'où une table en clair (≠ coffre chiffré).
-- `resolve` : clé partagée + account_id per-user → chacun agit comme lui-même.
CREATE TABLE IF NOT EXISTS unipile_accounts (
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    -- canal Unipile (LINKEDIN/WHATSAPP/TELEGRAM/INSTAGRAM/…) : un user a un
    -- account_id DISTINCT par canal, sous la même clé partagée.
    provider TEXT NOT NULL DEFAULT 'LINKEDIN',
    account_id TEXT NOT NULL,
    account_name TEXT,
    -- org dont l'abonnement Unipile (la clé) porte ce compte = org actif au connect.
    -- Source de vérité pour COMPTER et FACTURER par org (revendeur/passthrough).
    org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (sub, provider)
);
CREATE INDEX IF NOT EXISTS idx_unipile_accounts_org ON unipile_accounts(org_id);

-- Corrélation hosted-auth (B3, voie webhook) : le `name` posé sur le lien Unipile
-- ne revient PAS dans /accounts → on pose un **nonce** aléatoire comme `name` et on
-- le mappe au sub. Au succès, Unipile POST {name=nonce, account_id} sur le webhook ;
-- on résout nonce→sub. Le nonce (non devinable, courte vie) sécurise un webhook
-- non authentifié. Consommé à la résolution, pruné après expiration.
CREATE TABLE IF NOT EXISTS unipile_pending (
    nonce TEXT PRIMARY KEY,
    sub TEXT NOT NULL REFERENCES users(sub) ON DELETE CASCADE,
    org_id BIGINT,                       -- org actif au connect (porté au compte)
    provider TEXT NOT NULL DEFAULT 'LINKEDIN',  -- canal demandé (B1, multi-canal)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Palier organization (= périmètre / store serveur). Une org possède des
-- credentials propres (coffre `connector_credentials`, entity_type='org'), un
-- set de namespaces autorisés (org_entitlements), et des opérateurs
-- (org_members). Source de vérité de l'appartenance = ces tables, résolues par
-- `sub` — JAMAIS un claim du token
-- Logto (le token MCP ne porte que sub). Cf. project_oto_mcp_org_tier.
-- NB barreau 1 : tables seules, aucun helper ne les lit encore (canari de
-- déploiement). Le câblage (resolve_api_key, visibilité, meta-tools) suit.
CREATE TABLE IF NOT EXISTS orgs (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    logo_url TEXT,
    default_tools TEXT[],
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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

-- Les credentials d'org (Attio, Pennylane, MM token…) vivent dans le coffre
-- chiffré `connector_credentials` (entity_type='org'), pas dans une table dédiée.

-- Plafond de visibilité plateforme -> org : généralise
-- ADMIN_GRANT_ONLY_NAMESPACES + user_namespace_grants au niveau org. Débloque
-- un namespace gouverné (mm, gocardless) pour les membres de l'org.
CREATE TABLE IF NOT EXISTS org_entitlements (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    namespace TEXT NOT NULL,
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, namespace)
);

-- Invitations d'équipe (onboarding SaaS). Le token plaintext n'est jamais
-- stocké (seulement son hash, comme user_api_tokens). Une invitation vaut pour
-- un email donné ; l'acceptation exige un compte dont l'email vérifié Logto
-- matche (anti-transfert de lien). accepted_at NULL = en attente.
-- Invitation UNIFIÉE (ADR 0013) : org_id NULLABLE — renseigné = rejoindre cette
-- org (org-invite) ; NULL = referral alpha (l'invité crée sa propre org). Les
-- deux saveurs accordent l'accès plateforme à l'acceptation. `source` =
-- provenance (user_quota | admin_seed | org_admin).
-- `email` NULLABLE : une invitation nominative cible un email, mais une émission
-- « code à partager soi-même » (sans envoi mail) peut être anonyme. `code` = code
-- court lisible (lien /invitation/<carrier>/<code>), saisi/partagé à la main ;
-- c'est le secret d'accès single-use (≠ token_hash legacy du lien mail). Les
-- entrées par lien referral réutilisable sont journalisées ici (source
-- 'referral_link', accepted_*) pour l'arbre viral, sans pré-création.
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
    accepted_sub TEXT
);
CREATE INDEX IF NOT EXISTS idx_org_invitations_org ON org_invitations(org_id);
-- idx_org_invitations_code NON déclaré ici : `code` est ajouté par ALTER (DB
-- existantes) APRÈS ce _SCHEMA → l'index sur `code` vit dans le bloc migration,
-- après l'ADD COLUMN (sinon UndefinedColumn au boot sur une table préexistante).

-- Instructions markdown d'une org : doctrine de base + bibliothèque de skills.
-- Modèle unifié — chaque instruction est identifiée par `slug` ; le slug réservé
-- 'claude_md' = la doctrine de base servie d'office par oto_get_doctrine(), les
-- autres = des skills chargés à la demande (list/search/get). En CLAIR (prose,
-- pas un credential → hors coffre chiffré). Même principe d'accès que les
-- secrets d'org : résolu par l'org active du sub (get_active_org). `version` est
-- incrémenté à chaque écriture, qui archive un snapshot dans la table sœur.
CREATE TABLE IF NOT EXISTS org_instructions (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_org_instructions_org ON org_instructions(org_id);

-- Historique : un snapshot par version posée (revert + audit). Append-only.
CREATE TABLE IF NOT EXISTS org_instruction_revisions (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, slug, version)
);

-- Bibliothèque PUBLIQUE de doctrines (marketplace de skills/templates). Chaque
-- entrée = une doctrine publiée, avec un AUTEUR : 'otomata' (la plateforme) ou
-- 'org' (un créateur privé = une org). Preview + fork dans son org (copie vers
-- org_instructions sous un nouveau slug). En CLAIR (prose publiable, hors coffre).
-- Table NEUVE → ses index vivent ici (créés atomiquement) ; toute évolution
-- ULTÉRIEURE de colonne/index ira dans le bloc ALTER d'init_db (gotcha ADR 0017).
CREATE TABLE IF NOT EXISTS doctrine_library (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    author_kind TEXT NOT NULL,                -- 'otomata' | 'org' (validé en code)
    author_org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL,
    author_display TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    tags TEXT[] NOT NULL DEFAULT '{}',
    visibility TEXT NOT NULL DEFAULT 'public',-- 'public' | 'unlisted' (validé en code)
    source_org_id BIGINT,                     -- org dont la doctrine a été publiée
    source_slug TEXT,
    forked_from BIGINT REFERENCES doctrine_library(id) ON DELETE SET NULL,
    version INTEGER NOT NULL DEFAULT 1,        -- ré-publication = incrément
    published_by TEXT,                         -- sub du publieur
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (slug)
);
CREATE INDEX IF NOT EXISTS idx_doctrine_library_visibility ON doctrine_library(visibility);
CREATE INDEX IF NOT EXISTS idx_doctrine_library_author ON doctrine_library(author_kind, author_org_id);
CREATE INDEX IF NOT EXISTS idx_doctrine_library_category ON doctrine_library(category);

-- Sous-palier GROUPE (= départements / équipes au sein d'une org, ADR 0012).
-- Une org se subdivise en groupes plats (pas de sous-groupes en v1) ; chaque
-- groupe a un chef d'équipe (group_role='group_admin'). Modèle de droits
-- hiérarchique unifié (platform_admin > org_admin > group_admin > member) :
-- la résolution effective vit dans `roles.py`, l'appartenance dans ces tables.
-- Un groupe GOUVERNE trois ressources, par DÉLÉGATION de l'org : la doctrine
-- (org_group_instructions), un preset de toolset par défaut (default_tools), et
-- des secrets partagés (coffre `connector_credentials`, entity_type='group').
-- Source de vérité de l'appartenance = ces tables, résolues par `sub`.
CREATE TABLE IF NOT EXISTS org_groups (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    -- Preset de toolset par défaut du groupe (le chef le pose pour son équipe).
    -- NULL = pas de baseline (les membres gardent leur visibilité par défaut) ;
    -- non-NULL (même []) = baseline : seuls ces tools sont visibles par défaut,
    -- sauf override perso. N'élève JAMAIS un grant-only (anti-escalade).
    default_tools TEXT[],
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

-- Doctrine + skills d'un GROUPE (miroir d'org_instructions au grain groupe).
-- Servie en COMPLÉMENT de la doctrine d'org par oto_get_doctrine() quand l'user a
-- un groupe actif. Même modèle versionné (slug réservé 'claude_md' = base ;
-- autres = skills). En clair (prose, hors coffre chiffré).
CREATE TABLE IF NOT EXISTS org_group_instructions (
    group_id BIGINT NOT NULL REFERENCES org_groups(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_org_group_instructions_group ON org_group_instructions(group_id);

CREATE TABLE IF NOT EXISTS org_group_instruction_revisions (
    group_id BIGINT NOT NULL REFERENCES org_groups(id) ON DELETE CASCADE,
    slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (group_id, slug, version)
);

-- Coffre unique des credentials per-entité (user OU org OU group) : clés API,
-- sessions linkedin/crunchbase, OAuth Google multi-compte, platform keys.
-- entity_id = sub (user) | orgs.id::text (org) | org_groups.id::text (group) ;
-- toujours requêter (entity_type, entity_id) ENSEMBLE. Secret chiffré par
-- enveloppe AES-256-GCM dans `secret_enc` (obligatoire — pas de colonne
-- plaintext) ; déchiffrement JIT dans resolve_api_key. meta JSONB pour les
-- satellites (user_agent, scopes…).
CREATE TABLE IF NOT EXISTS connector_credentials (
    entity_type TEXT NOT NULL,            -- 'user' | 'org' | 'group'
    entity_id   TEXT NOT NULL,            -- users.sub | orgs.id::text | org_groups.id::text
    connector   TEXT NOT NULL,            -- nom de connecteur (registre)
    account     TEXT NOT NULL DEFAULT '', -- discriminant multi-compte ('' = mono ; ex. email Google)
    secret_enc  TEXT,                     -- enveloppe AES-256-GCM (obligatoire)
    secret_kind TEXT NOT NULL DEFAULT 'api_key',
    meta        JSONB NOT NULL DEFAULT '{}',
    set_by      TEXT,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_type, entity_id, connector, account)
);
CREATE INDEX IF NOT EXISTS idx_conn_cred_entity ON connector_credentials(entity_type, entity_id);

-- Palier billing : porte-monnaie de credits d'appel PAR ORGANISATION.
-- balance = compteur entier de "call credits" restants ; peut devenir NÉGATIF
-- (soft enforcement : on ne bloque JAMAIS un appel, cf. credits_store). base_granted
-- = le stock gratuit unique (OTO_MCP_FREE_CALLS) a déjà été crédité (idempotence du
-- don de bienvenue). Le débit par appel n'écrit QUE cette table (cf. ledger ci-dessous).
CREATE TABLE IF NOT EXISTS org_credits (
    org_id BIGINT PRIMARY KEY REFERENCES orgs(id) ON DELETE CASCADE,
    balance INTEGER NOT NULL DEFAULT 0,
    base_granted BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Grand livre des mouvements MONÉTAIRES (un delta par top-up Stripe / don de base /
-- ajustement admin). Volontairement PAS de ligne par appel débité (volumétrie : le
-- détail par appel vit déjà dans tool_calls, prunée). reason : 'stripe' | 'base_grant'
-- | 'admin_adjust'. stripe_event_id = id d'event Stripe, UNIQUE → idempotence webhook.
CREATE TABLE IF NOT EXISTS credit_transactions (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    stripe_event_id TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_credit_tx_org ON credit_transactions(org_id, created_at DESC);

-- Abonnements récurrents Stripe par org & par produit (ex. `unipile` = option
-- LinkedIn à €15/mois/siège). DISTINCT des credits d'appel (one-off) : ici c'est
-- du récurrent (mode=subscription). `status` reflète l'abonnement Stripe
-- (active/past_due/canceled…) → gate l'activation de l'option. `quantity` = nb de
-- sièges (comptes connectés). Source de vérité = Stripe, miroir local mis à jour
-- par les webhooks (et lu pour le gate sans appel Stripe par requête).
CREATE TABLE IF NOT EXISTS org_subscriptions (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    product TEXT NOT NULL,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    status TEXT NOT NULL DEFAULT 'inactive',
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, product)
);

-- Comps d'options admin (gratuit) — contrepartie NON-Stripe d'`org_subscriptions` :
-- une option payante (ex. `unipile`) offerte à une entité user|org par un admin, sans
-- passer par l'abonnement Stripe. `access.has_option` débloque l'option si comp OU
-- abonnement Stripe (cf. docs/connector-model.md, couche 3). Entity-keyé (user|org).
CREATE TABLE IF NOT EXISTS option_comps (
    entity_type TEXT NOT NULL,        -- 'user' | 'org'
    entity_id   TEXT NOT NULL,        -- sub (user) ou org_id en texte (org)
    option      TEXT NOT NULL,        -- 'unipile', …
    granted_by  TEXT,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (entity_type, entity_id, option)
);

-- Bascule de tenant Logto (B1, otomata#35) : alias ancien_sub → nouveau_sub. Posé
-- par migrate_sub au 1er login d'un compte sur le nouveau tenant (merge par email).
-- Sert à canonicaliser les tokens encore émis par l'ancien tenant pendant le drain
-- (sinon un vieux token re-créerait le compte supprimé). Vide hors fenêtre de bascule.
CREATE TABLE IF NOT EXISTS sub_aliases (
    old_sub TEXT PRIMARY KEY,
    new_sub TEXT NOT NULL,
    migrated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Schéma OBSERVÉ des connecteurs (rédaction de champs) : squelette clés+types dérivé
-- des VRAIES réponses des tools (JAMAIS de valeurs/PII). Source de vérité du schéma
-- affiché dans l'UI de rédaction — les sorties connecteurs sont des passthrough d'API
-- tierces qu'on ne possède pas, donc le schéma juste = ce qui transite. Alimenté par
-- `FieldRedactionMiddleware` (squelette par service, fusion incrémentale).
CREATE TABLE IF NOT EXISTS connector_schemas (
    service TEXT PRIMARY KEY,
    schema JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- File d'envoi d'email différé (« plus tard » / garde-fou quiet hours). Le HTML est
-- rendu et l'autorisation vérifiée AU MOMENT de email_send (snapshot) ; le worker
-- envoie body_html tel quel, sans re-render ni re-check. scheduled_at en UTC.
CREATE TABLE IF NOT EXISTS scheduled_emails (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
    created_by TEXT,
    to_email TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT NOT NULL,
    from_email TEXT,
    from_name TEXT,
    reply_to TEXT,
    transport TEXT NOT NULL,                  -- 'mailer' | 'resend'
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed | cancelled
    scheduled_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    sent_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sched_due ON scheduled_emails(scheduled_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_sched_org ON scheduled_emails(org_id, status, created_at DESC);

-- BOAMP (avis de marchés publics) : index local des avis DILA (france-opendata#3).
-- OpenDataSoft bloqué datacenter → on ingère le dump XML DILA en PG (petite table,
-- ~110k lignes sur 2 ans) plutôt qu'un parquet/DuckDB (réservé au monstre SIRENE).
-- Dates stockées en TEXT YYYY-MM-DD (identique à la source ; comparaisons lexicales OK).
CREATE TABLE IF NOT EXISTS boamp (
    idweb TEXT PRIMARY KEY,
    annee INTEGER,
    objet TEXT,
    organisme TEXT,
    date_publication TEXT,
    date_limite_reponse TEXT,
    date_fin_diffusion TEXT,
    dep_publication TEXT,
    nature_marche TEXT,
    type_procedure TEXT,
    type_avis_nature TEXT,
    type_avis_famille TEXT,
    statut TEXT,
    descripteurs_libelle TEXT,
    descripteurs_json TEXT,
    synthese TEXT,
    url TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_boamp_date ON boamp(date_publication DESC);
CREATE INDEX IF NOT EXISTS idx_boamp_dep ON boamp(dep_publication);

-- ACCO (accords d'entreprise) : index local de la base nationale des accords
-- collectifs (dump XML DILA, accords conclus depuis le 1er sept. 2017). Même
-- substrat que BOAMP : table PG (~387k lignes de métadonnées) plutôt qu'un
-- parquet/DuckDB (réservé au monstre SIRENE). Dates en TEXT YYYY-MM-DD (source ;
-- comparaisons lexicales OK). theme_codes = JSON array de codes (match par LIKE).
CREATE TABLE IF NOT EXISTS acco (
    id TEXT PRIMARY KEY,
    nature TEXT,
    numero TEXT,
    siret TEXT,
    raison_sociale TEXT,
    code_ape TEXT,
    code_idcc TEXT,
    secteur TEXT,
    date_texte TEXT,
    date_depot TEXT,
    date_effet TEXT,
    date_fin TEXT,
    date_maj TEXT,
    date_diffusion TEXT,
    conforme_version_integrale TEXT,
    theme_codes TEXT,
    themes_libelle TEXT,
    syndicats_libelle TEXT,
    code_postal TEXT,
    ville TEXT,
    titre TEXT,
    url TEXT,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_acco_siret ON acco(siret);
CREATE INDEX IF NOT EXISTS idx_acco_idcc ON acco(code_idcc);
CREATE INDEX IF NOT EXISTS idx_acco_date ON acco(date_texte DESC);
"""

# Providers supportés pour les user keys. DÉRIVÉ du registre source unique
# (`connectors.py`) — ne plus éditer ici, déclarer le connecteur dans le registre.
KEY_PROVIDERS = connectors.KEY_PROVIDERS


_pool: Optional[ConnectionPool] = None


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not set (managed PG connection string)")
    return url


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=_database_url(),
            min_size=1,
            max_size=int(os.environ.get("OTO_MCP_DB_POOL_MAX", "8")),
            kwargs={"row_factory": _str_dict_row},
            open=True,
        )
    return _pool


@contextmanager
def _connect() -> Iterator[psycopg.Connection]:
    pool = _get_pool()
    with pool.connection() as conn:
        yield conn


def init_db() -> None:
    with _connect() as conn:
        # AVANT _SCHEMA : renomme l'ancienne tool_call_log vers le schéma canonique
        # (sinon CREATE IF NOT EXISTS poserait une tool_calls vide à côté).
        _migrate_tool_call_log(conn)
        conn.execute(_SCHEMA)
        # Idempotent column adds — `CREATE TABLE IF NOT EXISTS` ne propage pas les
        # nouvelles colonnes sur les tables existantes.
        conn.execute("ALTER TABLE user_grants ADD COLUMN IF NOT EXISTS daily_quota INTEGER")
        # Corrélation des appels (ADR 0017, extension OTO-LOCALE de tool_calls).
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS session_id TEXT")
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS run_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, created_at) WHERE run_id IS NOT NULL")
        # Résolution des signaux d'usage (ADR 0017) : marquer un feedback/gap traité.
        # NULL = ouvert. resolution = note libre de l'opérateur (ce qui a été fait).
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolved_by TEXT")
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolution TEXT")
        # Unipile revendeur (org_id porté au compte + plafond par org).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unipile_accounts_org ON unipile_accounts(org_id)")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS org_id BIGINT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS unipile_account_limit INTEGER")
        # Multi-canal Unipile : un account_id par (sub, provider). Migration de la
        # PK sub → (sub, provider) ; les lignes existantes prennent 'LINKEDIN' (DEFAULT).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        conn.execute("ALTER TABLE unipile_accounts DROP CONSTRAINT IF EXISTS unipile_accounts_pkey")
        conn.execute("ALTER TABLE unipile_accounts ADD PRIMARY KEY (sub, provider)")
        # Horodatage du dernier sync du feed (miroir home, datastore linkedin-feed) :
        # gouverne la fraîcheur du cache (TTL) côté unipile_feed. NULL = jamais sync.
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS feed_synced_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        # Retrait du rôle `guest` (2026-06-15) : défaut → member + migration des
        # lignes existantes (guest était un alias sans effet, cf. access.py).
        conn.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'member'")
        conn.execute("UPDATE users SET role = 'member' WHERE role = 'guest'")
        # Accès plateforme & invitation virale (ADR 0013, barreau 1).
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invite_quota INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS invited_by TEXT")
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS access_granted_at TIMESTAMPTZ")
        # access_status : backfill ONE-SHOT à la création de la colonne — les comptes
        # existants sont pré-alpha → 'active'. Garder hors d'un ADD COLUMN ... DEFAULT
        # (qui poserait tout le monde 'pending') ET hors d'un UPDATE inconditionnel
        # rejoué à chaque boot (qui ré-activerait les pending et tuerait le gate).
        _has_access = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'access_status'"
        ).fetchone()
        if not _has_access:
            conn.execute("ALTER TABLE users ADD COLUMN access_status TEXT")
            conn.execute("UPDATE users SET access_status = 'active', access_granted_at = NOW()")
            conn.execute("ALTER TABLE users ALTER COLUMN access_status SET DEFAULT 'pending'")
            conn.execute("ALTER TABLE users ALTER COLUMN access_status SET NOT NULL")
        # L'admin bootstrap (OTO_MCP_ADMIN_SUB) ne tombe jamais en waitlist.
        _admin_sub = os.environ.get("OTO_MCP_ADMIN_SUB")
        if _admin_sub:
            conn.execute(
                "UPDATE users SET access_status = 'active', "
                "access_granted_at = COALESCE(access_granted_at, NOW()) "
                "WHERE sub = %s AND access_status <> 'active'",
                (_admin_sub,),
            )
        # Invitation unifiée (ADR 0013) : org_id nullable + source (idempotent pour
        # les DB créées avant). NULL = referral alpha, l'invité crée sa propre org.
        conn.execute("ALTER TABLE org_invitations ALTER COLUMN org_id DROP NOT NULL")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS source TEXT")
        # Lien referral réutilisable + invitation par code court partageable à la
        # main (2026-06-22). referral_code = code stable par user (diffusable) ;
        # org_invitations.code = code single-use d'une invitation nominative ;
        # email nullable (émission sans envoi mail = code à partager soi-même).
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_referral_code "
                     "ON users(referral_code) WHERE referral_code IS NOT NULL")
        conn.execute("ALTER TABLE org_invitations ALTER COLUMN email DROP NOT NULL")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_org_invitations_code "
                     "ON org_invitations(code) WHERE code IS NOT NULL")
        # Datastore multi-compte (oto-backend#9) : compte Google propriétaire du sheet.
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS owner_email TEXT")
        # Datastore = spine natif PG (ADR 0016) : `spreadsheet_id` devient une
        # relique Sheets (nullable, plus écrite). DROP des colonnes Sheets différé
        # post-backfill — ici on lève juste le NOT NULL pour que les créations PG
        # natives passent. Idempotent.
        conn.execute("ALTER TABLE user_datastores ALTER COLUMN spreadsheet_id DROP NOT NULL")
        conn.execute("ALTER TABLE datastore_shares ALTER COLUMN spreadsheet_id DROP NOT NULL")
        # Avatar utilisateur + logo d'org (2026-06-16) : URL publique (Scaleway
        # Object Storage), pas un secret → colonne en clair, hors coffre.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS logo_url TEXT")
        # Description libre de l'org (self-service org_admin) — prose, pas un secret.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
        # Baseline de toolset par org (ADR 0015) : preset de visibilité curé par
        # l'org_admin, miroir d'org_groups.default_tools. NULL = pas de baseline.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS default_tools TEXT[]")
        # Baseline de connecteurs proposés par l'org (ADR 0019, B2) : miroir de
        # default_tools au grain connecteur (« org propose »). NULL = pas de baseline.
        # Inerte tant que la capacité B7 ne la lit pas.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS default_connectors TEXT[]")
        # Redaction de champs par org (FieldFilter) : politique par connecteur,
        # gouvernée par l'org_admin. Forme JSONB :
        #   { "<service>": { "salt": str?, "rules": [ {fields, action, ...} ] } }
        # {} = aucune config → repli sur le défaut serveur (field_filter_defaults).
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS field_filters JSONB NOT NULL DEFAULT '{}'::jsonb")
        # Adresses expéditrices d'email de l'org, keyées PAR CONNECTEUR (scaleway/resend).
        #   { "<connector>": { "senders": [{email, name?, reply_to?}], "quiet_hours"?: {...} } }
        # {} = aucune adresse → email_send retombe sur la marque oto@otomata.tech (super_admin).
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS email_settings JSONB NOT NULL DEFAULT '{}'::jsonb")
        # Migration ONE-SHOT (idempotente, gardée sur le format PLAT = clé `senders` au
        # top-level) : {senders:[{...,transport}], quiet_hours} → keyé par connecteur.
        # transport 'resend'→'resend', sinon 'scaleway' ; transport retiré du sender ;
        # quiet_hours global recopié sur chaque connecteur recevant ≥1 sender.
        for _row in conn.execute(
                "SELECT id, email_settings FROM orgs WHERE email_settings ? 'senders'").fetchall():
            _flat = _row["email_settings"] or {}
            _qh = _flat.get("quiet_hours")
            _grouped: dict = {}
            for _s in _flat.get("senders", []):
                _cn = "resend" if (_s.get("transport") == "resend") else "scaleway"
                _grouped.setdefault(_cn, {"senders": []})["senders"].append(
                    {_k: _v for _k, _v in _s.items() if _k != "transport"})
            if _qh:
                for _blk in _grouped.values():
                    _blk["quiet_hours"] = _qh
            conn.execute("UPDATE orgs SET email_settings = %s::jsonb WHERE id = %s",
                         (json.dumps(_grouped), _row["id"]))
        # Archivage (soft-delete) d'une org : masquée de tous les listings, réversible
        # (NULL = active). Pas de hard-delete — les FK (membres, credentials, usage,
        # billing, invitations, groupes) restent intactes pour audit/restauration.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        # Identité par org (ADR 0015) : visibilité scopée par (sub, org_id) ; org_id=0
        # = profil perso/global. Migration ONE-SHOT (gardée sur l'absence d'org_id) :
        # ajoute la colonne (existants → 0 = perso), re-keye les PK, puis BACKFILL =
        # copie le profil perso de chacun vers son org active (on retrouve sa config
        # là où on est aujourd'hui). Idempotent (ON CONFLICT) + joué une seule fois.
        _has_vis_orgid = conn.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'user_disabled_tools' AND column_name = 'org_id'"
        ).fetchone()
        if not _has_vis_orgid:
            for _t in ("user_disabled_tools", "user_enabled_tools", "user_presets"):
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN org_id BIGINT NOT NULL DEFAULT 0")
                conn.execute(f"ALTER TABLE {_t} DROP CONSTRAINT IF EXISTS {_t}_pkey")
            conn.execute("ALTER TABLE user_disabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
            conn.execute("ALTER TABLE user_enabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
            conn.execute("ALTER TABLE user_presets ADD PRIMARY KEY (sub, org_id, name)")
            conn.execute(
                "INSERT INTO user_disabled_tools (sub, org_id, tool_name, disabled_at) "
                "SELECT d.sub, m.org_id, d.tool_name, d.disabled_at FROM user_disabled_tools d "
                "JOIN org_members m ON m.sub = d.sub AND m.is_active WHERE d.org_id = 0 "
                "ON CONFLICT DO NOTHING")
            conn.execute(
                "INSERT INTO user_enabled_tools (sub, org_id, tool_name, enabled_at) "
                "SELECT e.sub, m.org_id, e.tool_name, e.enabled_at FROM user_enabled_tools e "
                "JOIN org_members m ON m.sub = e.sub AND m.is_active WHERE e.org_id = 0 "
                "ON CONFLICT DO NOTHING")
            conn.execute(
                "INSERT INTO user_presets (sub, org_id, name, enabled_tools, created_at, updated_at) "
                "SELECT p.sub, m.org_id, p.name, p.enabled_tools, p.created_at, p.updated_at "
                "FROM user_presets p JOIN org_members m ON m.sub = p.sub AND m.is_active "
                "WHERE p.org_id = 0 ON CONFLICT DO NOTHING")
        # Coffre chiffré : colonnes courantes (idempotent pour les DB créées avant).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS secret_enc TEXT")
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE connector_credentials DROP CONSTRAINT IF EXISTS connector_credentials_pkey")
        conn.execute("ALTER TABLE connector_credentials ADD PRIMARY KEY (entity_type, entity_id, connector, account)")
        conn.execute("ALTER TABLE platform_keys ADD COLUMN IF NOT EXISTS api_key_enc TEXT")
        # TTL opt-in des tokens API (audit 2026-06-13) : NULL = non-expirant.
        conn.execute("ALTER TABLE user_api_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        _drop_legacy_plaintext_stores(conn)
        # Substrat « graphe de facts structurés » (ADR 0008) — schéma factgraph.
        from .factgraph import projection as _fg_projection
        from .factgraph import store as _fg_store
        _fg_store.init_schema(conn)
        _fg_projection.init_schema(conn)
        # Cran d'activation des connecteurs (ADR 0010, B1) — table + seed unique
        # (snapshot du registre courant à ON). Aucun lecteur encore (canari) :
        # le câblage catalogue/chargement suit en B2/B3.
        from . import connector_activation as _conn_act
        _conn_act.init_schema(conn)
        _conn_act.seed_initial(conn)
        # Sélection de connecteurs par membre (ADR 0019, B1) — table seule, aucun
        # lecteur encore (canari, no-behavior-change) ; le câblage lecture/mutation
        # (capacité connectors.me/select/pause) et le masquage pause au middleware
        # suivent en B3/B4/B5.
        from . import connector_selection as _conn_sel
        _conn_sel.init_schema(conn)
    # Borne la volumétrie du journal de monitoring (hors transaction schéma).
    try:
        prune_tool_calls(int(os.environ.get("OTO_MCP_CALL_LOG_RETENTION_DAYS", "30")))
    except Exception as e:
        logger.warning("prune_tool_calls failed: %s", e)


def _migrate_tool_call_log(conn: psycopg.Connection) -> None:
    """tool_call_log → tool_calls (schéma canonique otomata-calllog,
    2026-06-13) : renomme table + colonnes, ajoute server/email/args.
    Idempotent — no-op si l'ancienne table n'existe plus (ou jamais existé)."""
    exists = conn.execute(
        "SELECT to_regclass('tool_call_log') IS NOT NULL AND to_regclass('tool_calls') IS NULL AS go"
    ).fetchone()
    if not exists or not exists["go"]:
        return
    conn.execute("ALTER TABLE tool_call_log RENAME TO tool_calls")
    conn.execute("ALTER TABLE tool_calls RENAME COLUMN tool_name TO tool")
    conn.execute("ALTER TABLE tool_calls RENAME COLUMN called_at TO created_at")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS server TEXT NOT NULL DEFAULT 'oto'")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS email TEXT")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS args JSONB")
    logger.info("tool_call_log migrée vers tool_calls (schéma canonique)")


def _drop_legacy_plaintext_stores(conn: psycopg.Connection) -> None:
    """Purge des emplacements plaintext supersédés par le coffre chiffré
    `connector_credentials` (migration terminée + soak nullé en prod, cf.
    `project_oto_connector_vault`). Idempotent (IF EXISTS) — no-op sur une DB
    fraîche (on-prem). Le chiffrement est désormais obligatoire : plus aucun
    chemin plaintext (writers/reveal en chiffré-seul)."""
    # connector_credentials.secret (plaintext interne du coffre) + platform_keys.api_key
    conn.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS secret")
    conn.execute("ALTER TABLE platform_keys DROP COLUMN IF EXISTS api_key")
    # Colonnes legacy users.<provider>_api_key + sessions linkedin/crunchbase.
    for col in ("serper_api_key", "hunter_api_key", "sirene_api_key", "attio_api_key",
                "lemlist_api_key", "kaspr_api_key", "pennylane_api_key", "slack_api_key",
                "fullenrich_api_key", "linkedin_cookie", "linkedin_user_agent",
                "linkedin_cookie_set_at", "crunchbase_cookies", "crunchbase_user_agent",
                "crunchbase_set_at"):
        conn.execute(f"ALTER TABLE users DROP COLUMN IF EXISTS {col}")
    # Tables legacy entièrement foldées dans le coffre.
    conn.execute("DROP TABLE IF EXISTS org_secrets")
    conn.execute("DROP TABLE IF EXISTS user_google_oauth")


def upsert_user(sub: str, email: Optional[str] = None, name: Optional[str] = None,
                iss: Optional[str] = None) -> None:
    """Create the user row if missing, refresh email/name if known.

    Fédération de compte (otomata#16) : à la **première** création (vrai INSERT),
    on provisionne le compte memento correspondant par email (best-effort, non
    bloquant — cf. `memento_federation`). Le `(xmax = 0)` distingue insert/update
    sans SELECT préalable : 0 sur une ligne fraîchement insérée, ≠ 0 sur un UPDATE.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO users (sub, email, name)
            VALUES (%s, %s, %s)
            ON CONFLICT(sub) DO UPDATE SET
                email = COALESCE(EXCLUDED.email, users.email),
                name  = COALESCE(EXCLUDED.name,  users.name),
                updated_at = NOW()
            RETURNING (xmax = 0) AS inserted
            """,
            (sub, email, name),
        ).fetchone()
    if row and row.get("inserted") and email:
        # Réconciliation invitation↔signup (ADR 0013) : un invité qui s'inscrit
        # (par n'importe quel chemin, pas seulement le lien /invite) voit son
        # invitation en attente honorée par l'email vérifié → il saute la waitlist
        # au lieu d'y rester coincé avec une invitation orpheline. Synchrone (une
        # fois, au 1er insert) mais best-effort : un échec ne casse pas l'auth.
        try:
            from . import org_store
            org_store.reconcile_signup_with_invitation(sub, email)
        except Exception:
            pass
        # Import paresseux : la fédération est optionnelle (no-op sans secret), et
        # on ne veut pas de dépendance dure au boot. Jamais bloquant / jamais fatal.
        from . import memento_federation
        memento_federation.provision_async(sub, email)
    # Bascule de tenant (B1, otomata#35) : sur un login du NOUVEAU tenant, fusionner
    # l'ancien compte (même email) → ce sub. Gaté par env `OTO_MCP_TENANT_MIGRATION_ISS`
    # (dormant hors fenêtre de bascule). Idempotent, best-effort, à chaque login
    # new-tenant (pas que au 1er insert → couvre les retries / l'ordre des logins).
    # ⚠️ SÉCU (account takeover) : la décision de merge se prend sur l'email
    # AUTORITATIF lu de Logto (Management API), JAMAIS sur le claim email/email_verified
    # du token — un token forgé pourrait revendiquer l'email d'autrui pour absorber son
    # compte (rôle, coffre). reconcile_tenant_migration récupère lui-même cet email ;
    # le claim `email` n'est passé que comme PRÉ-FILTRE cheap (éviter un appel Logto à
    # chaque requête quand rien ne matche).
    if iss:
        _mig = os.environ.get("OTO_MCP_TENANT_MIGRATION_ISS", "").strip().rstrip("/")
        if _mig and iss.rstrip("/") == _mig:
            try:
                reconcile_tenant_migration(sub, email_hint=email)
            except Exception:
                pass


def get_user(sub: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub = %s", (sub,)).fetchone()
        return dict(row) if row else None


# --- Bascule de tenant Logto (B1, otomata#35) -------------------------------
# Inventaire des colonnes keyed-by-sub à repointer (issue oto-backend#56). Plain
# UPDATE : le nouveau sub est frais → aucun conflit de PK, SAUF user_account_profile
# (PK sub) et connector_credentials (coffre user), traités à part.
_SUB_COLUMNS = [
    # données de l'user
    ("usage", "sub"), ("tool_calls", "sub"), ("usage_signals", "sub"),
    ("user_disabled_tools", "sub"), ("user_enabled_tools", "sub"), ("user_presets", "sub"),
    ("user_grants", "sub"), ("user_namespace_grants", "sub"), ("user_datastores", "sub"),
    ("datastore_shares", "owner_sub"), ("datastore_shares", "shared_with_sub"),
    ("org_members", "sub"), ("org_group_members", "sub"),
    ("user_api_tokens", "sub"), ("unipile_accounts", "sub"), ("unipile_pending", "sub"),
    # attribution (soft)
    ("users", "invited_by"), ("user_grants", "granted_by"), ("user_namespace_grants", "granted_by"),
    ("orgs", "created_by"), ("org_entitlements", "granted_by"),
    ("org_invitations", "invited_by"), ("org_invitations", "accepted_sub"),
    ("org_groups", "created_by"), ("org_instructions", "set_by"),
    ("org_instruction_revisions", "set_by"), ("org_group_instructions", "set_by"),
    ("org_group_instruction_revisions", "set_by"), ("doctrine_library", "published_by"),
]


def resolve_sub(sub: str) -> str:
    """Canonicalise un sub via sub_aliases (vieux token d'un tenant en drain →
    compte migré). Renvoie le sub inchangé si pas d'alias (cas normal)."""
    if not sub:
        return sub
    try:
        with _connect() as conn:
            row = conn.execute("SELECT new_sub FROM sub_aliases WHERE old_sub=%s", (sub,)).fetchone()
        return row["new_sub"] if row else sub
    except Exception:
        return sub


_ROLE_RANK = {"member": 0, "admin": 1, "super_admin": 2}


def _stronger_role(a: Optional[str], b: Optional[str]) -> str:
    """Le plus haut des deux rôles (une fusion n'enlève pas un privilège)."""
    ra, rb = _ROLE_RANK.get(a or "member", 0), _ROLE_RANK.get(b or "member", 0)
    return (a if ra >= rb else b) or "member"


def _merge_access_status(a: Optional[str], b: Optional[str]) -> str:
    """Statut d'accès fusionné, sans rétrograder : `blocked` (deny explicite) prime,
    sinon `active` prime sur `pending`."""
    s = {a, b}
    if "blocked" in s:
        return "blocked"
    if "active" in s:
        return "active"
    return "pending"


def migrate_sub(old_sub: str, new_sub: str) -> bool:
    """MERGE transactionnel ancien→nouveau compte (bascule de tenant, issue #56).
    Hérite les champs d'accès de l'ancien, repointe TOUTES les tables keyed-by-sub
    (les 3 FK `ON DELETE CASCADE` incluses, AVANT de supprimer l'ancien → pas de
    cascade destructrice), supprime l'ancienne ligne users, pose l'alias. Idempotent
    (no-op si l'ancien sub n'existe plus). True si une migration a eu lieu."""
    if not old_sub or not new_sub or old_sub == new_sub:
        return False
    with _connect() as conn:
        old = conn.execute("SELECT * FROM users WHERE sub=%s", (old_sub,)).fetchone()
        if not old:
            return False  # déjà migré / inexistant
        # 1. fusionner les champs d'accès SANS JAMAIS RÉTROGRADER. Une fusion ne doit
        #    pas réduire l'accès : on prend le rôle le plus fort, le statut le plus
        #    permissif (active > pending ; blocked reste un deny explicite), le quota
        #    max. ⚠️ Le naïf « hérite de l'ancien » downgrade le nouveau si l'ancien est
        #    un stub frais (member/pending) re-fusionné par-dessus un compte établi
        #    (vécu 2026-06-23 : alexis super_admin/active repassé member/pending).
        new = conn.execute(
            "SELECT role, access_status, invite_quota FROM users WHERE sub=%s", (new_sub,)
        ).fetchone() or {}
        conn.execute(
            """UPDATE users SET
                 role = %(role)s, access_status = %(st)s, invite_quota = %(q)s,
                 invited_by = COALESCE(users.invited_by, %(ib)s),
                 access_granted_at = COALESCE(users.access_granted_at, %(ag)s),
                 avatar_url = COALESCE(users.avatar_url, %(av)s), updated_at = NOW()
               WHERE sub = %(new)s""",
            {"role": _stronger_role(old["role"], new.get("role")),
             "st": _merge_access_status(old["access_status"], new.get("access_status")),
             "q": max(old["invite_quota"] or 0, new.get("invite_quota") or 0),
             "ib": old.get("invited_by"), "ag": old.get("access_granted_at"),
             "av": old.get("avatar_url"), "new": new_sub},
        )
        # 2. user_account_profile (PK sub) : retirer le frais du new PUIS repointer
        #    l'ancien (garde l'historique d'onboarding). DELETE d'abord → pas de conflit PK.
        conn.execute("DELETE FROM user_account_profile WHERE sub=%s", (new_sub,))
        conn.execute("UPDATE user_account_profile SET sub=%s WHERE sub=%s", (new_sub, old_sub))
        # 3. repointer toutes les colonnes sub.
        for table, col in _SUB_COLUMNS:
            conn.execute(f"UPDATE {table} SET {col}=%s WHERE {col}=%s", (new_sub, old_sub))
        # coffre user (connector_credentials) : entité + auteur.
        conn.execute("UPDATE connector_credentials SET entity_id=%s WHERE entity_type='user' AND entity_id=%s", (new_sub, old_sub))
        conn.execute("UPDATE connector_credentials SET set_by=%s WHERE set_by=%s", (new_sub, old_sub))
        # 4. supprimer l'ancienne ligne users (enfants FK déjà repointés).
        conn.execute("DELETE FROM users WHERE sub=%s", (old_sub,))
        # 5. alias (drain des vieux tokens → compte canonique).
        conn.execute(
            "INSERT INTO sub_aliases (old_sub, new_sub) VALUES (%s,%s) "
            "ON CONFLICT (old_sub) DO UPDATE SET new_sub=EXCLUDED.new_sub, migrated_at=NOW()",
            (old_sub, new_sub),
        )
    logger.info("tenant migration: merged %s → %s (par email)", old_sub, new_sub)
    return True


def reconcile_tenant_migration(new_sub: str, email_hint: Optional[str] = None) -> bool:
    """Au login sur le nouveau tenant : récupère l'email AUTORITATIF du compte depuis
    Logto (Management API — le `primaryEmail` n'existe qu'après vérification, donc
    fiable même si le token ment) puis, si EXACTEMENT un autre compte partage cet email
    (l'ancien sub), le migre vers new_sub. No-op si email introuvable, 0 (rien à migrer)
    ou >1 (ambigu — on ne touche pas). Idempotent (l'ancien disparaît après migration).

    `email_hint` (claim email du token) n'est qu'un PRÉ-FILTRE pour éviter un appel
    Logto à chaque requête : si aucun autre compte ne porte cet email, rien à migrer →
    on ne sollicite pas Logto. Il n'entre JAMAIS dans la décision de merge (sécurité)."""
    if not new_sub:
        return False
    try:
        # Pré-filtre cheap sur le claim (non fiable) : court-circuite le cas courant
        # (déjà migré / rien à fusionner) sans round-trip Logto.
        if email_hint:
            with _connect() as conn:
                pre = conn.execute(
                    "SELECT 1 FROM users WHERE lower(email)=lower(%s) AND sub<>%s LIMIT 1",
                    (email_hint, new_sub),
                ).fetchone()
            if not pre:
                return False
        # Email AUTORITATIF (source de vérité) — la décision de merge se prend ici.
        from .oauth_facade import logto_user_primary_email
        email = logto_user_primary_email(new_sub)
        if not email:
            return False
        with _connect() as conn:
            rows = conn.execute(
                "SELECT sub FROM users WHERE lower(email)=lower(%s) AND sub<>%s",
                (email, new_sub),
            ).fetchall()
        if len(rows) != 1:
            return False
        return migrate_sub(rows[0]["sub"], new_sub)
    except Exception:
        logger.warning("reconcile_tenant_migration échoué pour %s", new_sub, exc_info=True)
        return False


# --- accès plateforme & quota d'invitation (ADR 0013) -----------------------

def grant_platform_access(sub: str, *, invited_by: Optional[str] = None,
                          quota: Optional[int] = None) -> None:
    """Passe le compte en 'active' (alpha). Idempotent sur access_granted_at et
    invited_by (COALESCE — ne réécrase pas un parrain déjà posé). `quota` crédite
    le budget referral (referral alpha) ; None = ne touche pas au quota (cas
    org-invite : le membre obtient l'accès mais pas de budget d'invitation)."""
    sets = ["access_status = 'active'",
            "access_granted_at = COALESCE(access_granted_at, NOW())",
            "updated_at = NOW()"]
    params: list = []
    if quota is not None:
        sets.append("invite_quota = %s")
        params.append(int(quota))
    if invited_by is not None:
        sets.append("invited_by = COALESCE(invited_by, %s)")
        params.append(invited_by)
    params.append(sub)
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE sub = %s", tuple(params))


def block_platform_access(sub: str) -> None:
    """Passe le compte en 'blocked' (rejet d'un cold signup indésirable). Le compte
    sort de la waitlist (qui ne liste que 'pending') et `session_visibility` le
    traite comme non-'active' (allowlist onboarding only). Réversible : un
    `grant_platform_access` ultérieur le repasse 'active'. Ne touche pas au quota."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET access_status = 'blocked', updated_at = NOW() WHERE sub = %s",
            (sub,),
        )


def consume_invite_quota(sub: str) -> bool:
    """Décrémente atomiquement le quota referral si > 0. True si consommé, False
    si épuisé (WHERE invite_quota > 0 → pas de course)."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE users SET invite_quota = invite_quota - 1, updated_at = NOW() "
            "WHERE sub = %s AND invite_quota > 0",
            (sub,),
        )
        return (cur.rowcount or 0) > 0


def refund_invite_quota(sub: str) -> None:
    """Re-crédite une invitation (rollback si la création échoue après consume)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET invite_quota = invite_quota + 1, updated_at = NOW() WHERE sub = %s",
            (sub,),
        )


def set_invite_quota(sub: str, quota: int) -> None:
    """Fixe le quota referral (admin top-up). Ne change pas l'access_status."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET invite_quota = %s, updated_at = NOW() WHERE sub = %s",
            (int(quota), sub),
        )


def list_waitlist() -> list[dict]:
    """Comptes en attente (cold signups non approuvés), du plus ancien au plus
    récent — la file d'attente est une vue dérivée, pas une table."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, email, name, created_at FROM users "
            "WHERE access_status = 'pending' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


def get_user_by_email(email: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = %s", (email,)).fetchone()
        return dict(row) if row else None


def list_users() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT sub, email, name, role, created_at, updated_at FROM users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


# --- role -------------------------------------------------------------------

def set_user_role(sub: str, role: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET role = %s, updated_at = NOW() WHERE sub = %s",
            (role, sub),
        )


# --- avatar -----------------------------------------------------------------

def set_avatar_url(sub: str, url: Optional[str]) -> None:
    """Pose (ou efface si url=None) l'URL publique de l'avatar du user.

    URL publique servie depuis l'Object Storage — pas un secret, colonne en
    clair (hors coffre chiffré)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET avatar_url = %s, updated_at = NOW() WHERE sub = %s",
            (url, sub),
        )


# --- LinkedIn ---------------------------------------------------------------

def set_unipile_account(sub: str, account_id: str, account_name: Optional[str] = None,
                        org_id: Optional[int] = None, provider: str = "LINKEDIN") -> None:
    """Associe (upsert) le compte Unipile `account_id` à `(sub, provider)` (B3,
    multi-canal). `org_id` = org dont l'abonnement porte ce compte (compté + facturé)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO unipile_accounts (sub, provider, account_id, account_name, org_id) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (sub, provider) DO UPDATE SET "
            "account_id = EXCLUDED.account_id, account_name = EXCLUDED.account_name, "
            "org_id = EXCLUDED.org_id, connected_at = NOW()",
            (sub, provider, account_id, account_name, org_id),
        )


def get_unipile_account_id(sub: str, provider: str = "LINKEDIN") -> Optional[str]:
    """`account_id` Unipile du user pour ce canal, ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT account_id FROM unipile_accounts WHERE sub = %s AND provider = %s",
            (sub, provider),
        ).fetchone()
    return row["account_id"] if row else None


def get_unipile_feed_synced_at(sub: str, provider: str = "LINKEDIN") -> Optional[str]:
    """Horodatage (string ISO via row factory) du dernier sync du feed, ou None
    si jamais synchronisé / compte absent."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT feed_synced_at FROM unipile_accounts WHERE sub = %s AND provider = %s",
            (sub, provider),
        ).fetchone()
    return row["feed_synced_at"] if row else None


def touch_unipile_feed_synced(sub: str, provider: str = "LINKEDIN") -> None:
    """Marque le feed comme synchronisé maintenant (pose `feed_synced_at = NOW()`)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE unipile_accounts SET feed_synced_at = NOW() WHERE sub = %s AND provider = %s",
            (sub, provider),
        )


def get_unipile_account(sub: str, provider: str = "LINKEDIN") -> Optional[dict]:
    """Statut de connexion Unipile d'un canal (pour /api/me / dashboard) ou None."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT provider, account_id, account_name, connected_at FROM unipile_accounts "
            "WHERE sub = %s AND provider = %s", (sub, provider)
        ).fetchone()
    return dict(row) if row else None


def list_unipile_accounts(sub: str) -> list[dict]:
    """Tous les comptes Unipile connectés du user, tous canaux confondus
    (`[{provider, account_id, account_name, org_id, connected_at}]`) — pour le dashboard.
    `org_id` = l'org dont l'abonnement porte le compte (ventilation par org, fiche admin)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT provider, account_id, account_name, org_id, connected_at FROM unipile_accounts "
            "WHERE sub = %s ORDER BY provider", (sub,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_unipile_account(sub: str, provider: str = "LINKEDIN") -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_accounts WHERE sub = %s AND provider = %s",
                     (sub, provider))


def count_unipile_accounts_for_org(org_id: int) -> int:
    """Nombre de comptes LinkedIn connectés portés par l'abonnement de cet org
    (base du plafond anti-dérapage + de la facturation par compte)."""
    with _connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM unipile_accounts WHERE org_id = %s", (org_id,)
        ).fetchone()["n"]


def list_unipile_accounts_by_org() -> list[dict]:
    """`[{org_id, provider, account_id, sub}]` de tous les comptes rattachés à un org
    (org_id non NULL) — itéré par la facturation récurrente."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT org_id, provider, account_id, sub FROM unipile_accounts WHERE org_id IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def unipile_account_owners() -> list[dict]:
    """TOUS les comptes unipile mappés → propriétaire (sub/email) + org. Pour la vue
    admin « sièges de la clé plateforme » : réconcilier les comptes présents sur
    l'instance partagée avec leurs propriétaires oto (account_id NON mappé = orphelin)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ua.account_id, ua.provider, ua.account_name, ua.sub, u.email, "
            "ua.org_id, o.name AS org_name, ua.connected_at "
            "FROM unipile_accounts ua "
            "LEFT JOIN users u ON u.sub = ua.sub "
            "LEFT JOIN orgs o ON o.id = ua.org_id"
        ).fetchall()
    return [dict(r) for r in rows]


def get_org_unipile_limit(org_id: int) -> Optional[int]:
    """Plafond de comptes Unipile de l'org (NULL = pas de plafond propre → défaut env)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT unipile_account_limit FROM orgs WHERE id = %s", (org_id,)
        ).fetchone()
    return row["unipile_account_limit"] if row else None


def set_org_unipile_limit(org_id: int, limit: Optional[int]) -> None:
    """Pose (ou efface, limit=None) le plafond de comptes Unipile d'un org."""
    with _connect() as conn:
        conn.execute(
            "UPDATE orgs SET unipile_account_limit = %s WHERE id = %s", (limit, org_id)
        )


# --- abonnements récurrents Stripe par org (option LinkedIn €15/mois/siège) ----

def get_org_subscription(org_id: int, product: str) -> Optional[dict]:
    """Miroir local de l'abonnement Stripe `product` de l'org (status/quantity/ids)
    ou None. Lu pour le gate d'activation (sans appel Stripe par requête)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id, product, stripe_customer_id, stripe_subscription_id, "
            "status, quantity, updated_at FROM org_subscriptions "
            "WHERE org_id = %s AND product = %s", (org_id, product)
        ).fetchone()
    return dict(row) if row else None


def upsert_org_subscription(org_id: int, product: str, *, status: str,
                            stripe_customer_id: Optional[str] = None,
                            stripe_subscription_id: Optional[str] = None,
                            quantity: Optional[int] = None) -> None:
    """Upsert le miroir d'abonnement (appelé par les webhooks Stripe). Les champs
    ids/quantity laissés à None ne sont pas écrasés s'ils existent déjà."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO org_subscriptions "
            "(org_id, product, status, stripe_customer_id, stripe_subscription_id, quantity, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, 0), NOW()) "
            "ON CONFLICT (org_id, product) DO UPDATE SET "
            "status = EXCLUDED.status, "
            "stripe_customer_id = COALESCE(EXCLUDED.stripe_customer_id, org_subscriptions.stripe_customer_id), "
            "stripe_subscription_id = COALESCE(EXCLUDED.stripe_subscription_id, org_subscriptions.stripe_subscription_id), "
            "quantity = COALESCE(%s, org_subscriptions.quantity), updated_at = NOW()",
            (org_id, product, status, stripe_customer_id, stripe_subscription_id,
             quantity, quantity),
        )


def get_org_by_subscription_id(stripe_subscription_id: str) -> Optional[dict]:
    """Retrouve `{org_id, product}` depuis l'id d'abonnement Stripe (webhooks dont
    le metadata serait absent)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT org_id, product FROM org_subscriptions WHERE stripe_subscription_id = %s",
            (stripe_subscription_id,)
        ).fetchone()
    return dict(row) if row else None


# Comps d'options (gratuit, posé par un admin) — contrepartie de `org_subscriptions`,
# lues par `access.has_option` (seam unique, couche 3 du modèle de connecteur).

def set_option_comp(entity_type: str, entity_id: str, option: str,
                    *, granted_by: Optional[str] = None) -> None:
    """Offre (comp gratuit) une option payante à une entité user|org. Idempotent."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO option_comps (entity_type, entity_id, option, granted_by) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (entity_type, entity_id, option) "
            "DO UPDATE SET granted_by = EXCLUDED.granted_by, granted_at = NOW()",
            (entity_type, str(entity_id), option, granted_by),
        )


def clear_option_comp(entity_type: str, entity_id: str, option: str) -> bool:
    """Retire un comp d'option. True si une ligne a été supprimée."""
    with _connect() as conn:
        n = conn.execute(
            "DELETE FROM option_comps WHERE entity_type=%s AND entity_id=%s AND option=%s",
            (entity_type, str(entity_id), option),
        ).rowcount
    return n > 0


def has_option_comp(entity_type: str, entity_id: str, option: str) -> bool:
    with _connect() as conn:
        return conn.execute(
            "SELECT 1 FROM option_comps WHERE entity_type=%s AND entity_id=%s AND option=%s",
            (entity_type, str(entity_id), option),
        ).fetchone() is not None


def list_option_comps(entity_type: str, entity_id: str) -> list[str]:
    """Options offertes (comp) à cette entité — pour l'affichage admin."""
    with _connect() as conn:
        return [r["option"] for r in conn.execute(
            "SELECT option FROM option_comps WHERE entity_type=%s AND entity_id=%s",
            (entity_type, str(entity_id)),
        )]


def create_unipile_pending(nonce: str, sub: str, org_id: Optional[int] = None,
                           provider: str = "LINKEDIN") -> None:
    """Mappe un `nonce` (posé comme `name` sur le lien hosted-auth) au `(sub, provider)`
    (+ org actif), pour corréler au retour du webhook. Prune les nonces expirés (> 1h)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute("DELETE FROM unipile_pending WHERE created_at < NOW() - INTERVAL '1 hour'")
        conn.execute(
            "INSERT INTO unipile_pending (nonce, sub, org_id, provider) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (nonce) DO NOTHING",
            (nonce, sub, org_id, provider),
        )


def resolve_unipile_pending(nonce: str) -> Optional[dict]:
    """Consomme un nonce → `{sub, org_id, provider}` (et le supprime), ou None si inconnu/expiré."""
    with _connect() as conn:
        row = conn.execute(
            "DELETE FROM unipile_pending WHERE nonce = %s "
            "AND created_at >= NOW() - INTERVAL '1 hour' RETURNING sub, org_id, provider",
            (nonce,),
        ).fetchone()
    return dict(row) if row else None


# Crunchbase = connecteur `personal_session` standard (coffre `crunchbase` via
# set_user_api_key / resolve_credential), le Context Browserbase tenant lieu de
# credential (ADR 0026). Plus de fonctions de session cookies+UA dédiées.


# --- onboarding / account profile -------------------------------------------

def get_account_profile(sub: str) -> dict:
    """Fiche d'onboarding de l'user : {onboarded, profile, onboarded_at, updated_at}.

    Jamais None — un sub sans ligne renvoie l'état par défaut (non onboardé,
    profile vide). Lecture seule (ne crée pas la ligne)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT onboarded, profile, onboarded_at, updated_at "
            "FROM user_account_profile WHERE sub = %s",
            (sub,),
        ).fetchone()
    if not row:
        return {"onboarded": False, "profile": {}, "onboarded_at": None, "updated_at": None}
    profile = row["profile"]
    if isinstance(profile, str):  # selon le driver, JSONB peut revenir en texte
        try:
            profile = json.loads(profile)
        except Exception:
            profile = {}
    return {
        "onboarded": bool(row["onboarded"]),
        "profile": profile or {},
        "onboarded_at": row["onboarded_at"],
        "updated_at": row["updated_at"],
    }


def update_account_profile(
    sub: str, fields: Optional[dict] = None, onboarded: Optional[bool] = None,
) -> dict:
    """Met à jour la fiche d'onboarding (upsert). `fields` est **shallow-mergé**
    dans le JSONB `profile` (clés existantes écrasées, les autres conservées).
    `onboarded` (si fourni) bascule le booléan + stampe `onboarded_at` au passage
    à vrai. Renvoie l'état résultant (comme `get_account_profile`)."""
    upsert_user(sub)
    patch = json.dumps(fields or {})
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_account_profile (sub, profile, onboarded, onboarded_at, updated_at)
            VALUES (
                %s,
                %s::jsonb,
                COALESCE(%s, FALSE),
                CASE WHEN %s IS TRUE THEN NOW() ELSE NULL END,
                NOW()
            )
            ON CONFLICT (sub) DO UPDATE SET
                profile = user_account_profile.profile || EXCLUDED.profile,
                onboarded = COALESCE(%s, user_account_profile.onboarded),
                onboarded_at = CASE
                    WHEN %s IS TRUE AND user_account_profile.onboarded_at IS NULL THEN NOW()
                    WHEN %s IS FALSE THEN NULL
                    ELSE user_account_profile.onboarded_at
                END,
                updated_at = NOW()
            """,
            (sub, patch, onboarded, onboarded, onboarded, onboarded, onboarded),
        )
    return get_account_profile(sub)


# --- user API keys ----------------------------------------------------------

def _check_provider(provider: str) -> None:
    if provider not in KEY_PROVIDERS:
        raise ValueError(f"Unknown provider {provider!r} (allowed: {KEY_PROVIDERS})")


def set_user_api_key(sub: str, provider: str, key: str) -> None:
    _check_provider(provider)
    upsert_user(sub)
    # Coffre chiffré, source unique. Import lazy (db ne doit pas importer
    # credentials_store au niveau module — cycle).
    from . import credentials_store
    credentials_store.set_credential("user", sub, provider, key, set_by=sub)


def clear_user_api_key(sub: str, provider: str) -> None:
    _check_provider(provider)
    from . import credentials_store
    credentials_store.clear_credential("user", sub, provider)


def get_user_api_key(sub: str, provider: str) -> Optional[str]:
    # Lit le coffre `connector_credentials` (déchiffre — chemin de RÉSOLUTION).
    # Import lazy (anti-cycle) ; require_keyed dans le store.
    from . import credentials_store
    return credentials_store.get_credential("user", sub, provider)


def has_user_api_key(sub: str, provider: str) -> bool:
    """Présence d'une clé perso SANS la déchiffrer (status_for / /api/me)."""
    from . import credentials_store
    return credentials_store.has_credential("user", sub, provider)


# --- usage counters ---------------------------------------------------------

def increment_usage(sub: str, tool: str) -> int:
    """Incrémente le compteur (sub, tool, today). Retourne la nouvelle valeur."""
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage (sub, tool, day, count)
            VALUES (%s, %s, CURRENT_DATE, 1)
            ON CONFLICT(sub, tool, day) DO UPDATE SET count = usage.count + 1
            RETURNING count
            """,
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


# --- MCP call monitoring (journal admin) ------------------------------------

def insert_tool_call(row: dict) -> None:
    """Sink otomata-calllog : insère un row canonique (server, sub, email, tool,
    args, ok, error, duration_ms) + corrélation OTO-LOCALE (session_id, run_id ;
    ADR 0017, absents du contrat canonique → enrichis par le sink). Best-effort
    côté middleware — jamais bloquant."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO tool_calls
                (server, sub, email, tool, args, ok, error, duration_ms, session_id, run_id)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                row.get("server") or "oto", row.get("sub"), row.get("email"),
                row["tool"], json.dumps(row.get("args")) if row.get("args") is not None else None,
                bool(row.get("ok")), row.get("error"), row.get("duration_ms"),
                row.get("session_id"), row.get("run_id"),
            ),
        )


# --- Signaux d'usage volontaires (ADR 0017, barreau 3) ----------------------

def insert_usage_signal(
    *, sub: Optional[str], org_id: Optional[int], signal: str, kind: str,
    target: Optional[str], body: Optional[str], session_id: Optional[str],
    source: str = "agent",
) -> int:
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO usage_signals
                (sub, org_id, signal, kind, target, body, session_id, source)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (sub, org_id, signal, kind, target, body, session_id, source),
        ).fetchone()
        return int(row["id"])


def list_usage_signals(
    signal: Optional[str] = None, target: Optional[str] = None, limit: int = 200,
    status: Optional[str] = None,
) -> list[dict]:
    """Signaux récents (récent d'abord), filtrables par type / cible / statut —
    base des projections (qualité d'outil, manques) du barreau 4.

    status: 'open' (resolved_at IS NULL) | 'resolved' (NOT NULL) | None (tous)."""
    limit = max(1, min(int(limit), 1000))
    sql = ("SELECT id, created_at, sub, org_id, signal, kind, target, body, "
           "session_id, source, resolved_at, resolved_by, resolution "
           "FROM usage_signals")
    clauses, params = [], []
    if signal:
        clauses.append("signal = %s"); params.append(signal)
    if target:
        clauses.append("target = %s"); params.append(target)
    if status == "open":
        clauses.append("resolved_at IS NULL")
    elif status == "resolved":
        clauses.append("resolved_at IS NOT NULL")
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def resolve_usage_signal(
    signal_id: int, *, resolved_by: Optional[str], note: Optional[str] = None,
    resolved: bool = True,
) -> Optional[dict]:
    """Marque un signal traité (ou le ré-ouvre si resolved=False). Renvoie la row
    mise à jour, ou None si l'id n'existe pas."""
    with _connect() as conn:
        if resolved:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET resolved_at = NOW(), resolved_by = %s, resolution = %s
                 WHERE id = %s
                RETURNING id, signal, kind, target, resolved_at, resolved_by, resolution
                """,
                (resolved_by, note, signal_id),
            ).fetchone()
        else:
            row = conn.execute(
                """
                UPDATE usage_signals
                   SET resolved_at = NULL, resolved_by = NULL, resolution = NULL
                 WHERE id = %s
                RETURNING id, signal, kind, target, resolved_at, resolved_by, resolution
                """,
                (signal_id,),
            ).fetchone()
        return dict(row) if row else None


# --- Projections « runs / usage » (ADR 0017, barreau 4) ----------------------
# Lecture seule, dérivées de tool_calls (run_id stampé) + usage_signals. Le
# `label`/`doctrine`/`outcome` d'un run viennent des appels run_start/run_finish.

def list_runs(limit: int = 100) -> list[dict]:
    """Runs récents (un par run_id ouvert via run_start) avec label/doctrine,
    acteur, bornes, outcome (si fermé) et nb d'appels du déroulé. `slug` (alias =
    doctrine sinon label) conservé pour compat dashboard."""
    limit = max(1, min(int(limit), 500))
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT s.run_id,
                   COALESCE(s.args->>'doctrine', s.args->>'label') AS slug,
                   s.args->>'label'    AS label,
                   s.args->>'doctrine' AS doctrine,
                   s.sub,
                   s.created_at      AS started_at,
                   f.created_at      AS finished_at,
                   f.args->>'outcome' AS outcome,
                   COALESCE(c.n_calls, 0) AS n_calls
            FROM tool_calls s
            LEFT JOIN LATERAL (
                SELECT created_at, args FROM tool_calls
                WHERE tool = 'run_finish' AND args->>'run_id' = s.run_id
                ORDER BY created_at DESC LIMIT 1
            ) f ON TRUE
            LEFT JOIN (
                SELECT run_id, count(*) AS n_calls FROM tool_calls
                WHERE run_id IS NOT NULL GROUP BY run_id
            ) c ON c.run_id = s.run_id
            WHERE s.tool = 'run_start' AND s.run_id IS NOT NULL
            ORDER BY s.created_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()]


def get_run(run_id: str) -> list[dict]:
    """Timeline d'un déroulé : tous les appels du run, dans l'ordre."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT created_at, tool, args, ok, error, duration_ms
            FROM tool_calls WHERE run_id = %s ORDER BY created_at
            """,
            (run_id,),
        ).fetchall()]


def aggregate_gaps(days: int = 30) -> list[dict]:
    """Manques agrégés (cas d'usage non couverts) — backlog produit dérivé."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT kind, target AS intent, count(*) AS n, max(created_at) AS last_at
            FROM usage_signals
            WHERE signal = 'gap' AND created_at > NOW() - make_interval(days => %s)
            GROUP BY kind, target ORDER BY n DESC, last_at DESC
            """,
            (int(days),),
        ).fetchall()]


def aggregate_tool_feedback(days: int = 30) -> list[dict]:
    """Qualité d'outil agrégée : feedback par (outil, kind)."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT target AS tool, kind, count(*) AS n, max(created_at) AS last_at
            FROM usage_signals
            WHERE signal = 'tool_feedback' AND created_at > NOW() - make_interval(days => %s)
            GROUP BY target, kind ORDER BY n DESC, last_at DESC
            """,
            (int(days),),
        ).fetchall()]


def list_tool_calls(
    limit: int = 200,
    sub: Optional[str] = None,
    tool_name: Optional[str] = None,
    errors_only: bool = False,
    since_days: Optional[int] = None,
) -> list[dict]:
    """Derniers appels MCP (récent d'abord), joints à l'email user pour l'UI."""
    limit = max(1, min(int(limit), 1000))
    clauses: list[str] = []
    params: list[Any] = []
    if sub:
        clauses.append("l.sub = %s")
        params.append(sub)
    if tool_name:
        clauses.append("l.tool = %s")
        params.append(tool_name)
    if errors_only:
        clauses.append("l.ok = FALSE")
    if since_days is not None:
        clauses.append("l.created_at >= NOW() - make_interval(days => %s)")
        params.append(int(since_days))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    with _connect() as conn:
        # Alias tool_name/called_at : compat avec l'UI admin existante.
        rows = conn.execute(
            f"""
            SELECT l.id, l.sub, u.email, u.name, l.tool AS tool_name, l.created_at AS called_at,
                   l.duration_ms, l.ok, l.error
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            {where}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT %s
            """,
            tuple(params),
        ).fetchall()
        return list(rows)


def instruction_usage(
    subs: list[str], tool: str, slug: Optional[str], days: int = 30
) -> dict:
    """Usage d'une doctrine dérivé de `tool_calls` (ADR 0014, « doctrine = process
    = log d'usage ») : combien de fois elle a été chargée par l'agent, par qui,
    et la distribution journalière sur `days` jours.

    `tool` = `oto_get_doctrine` (slug=None pour la base, sinon filtré par
    `args->>'slug'` pour une skill). Scopé aux `subs` (membres de
    l'org). Lecture pure ; renvoie {count, callers, daily{date:str -> n}}.
    """
    if not subs:
        return {"count": 0, "callers": [], "daily": {}}
    days = max(1, min(int(days), 365))
    slug_clause = " AND l.args->>'slug' = %s" if slug is not None else ""
    base_params: list[Any] = [subs, tool]
    if slug is not None:
        base_params.append(slug)
    with _connect() as conn:
        callers = conn.execute(
            f"""
            SELECT u.email, COUNT(*) AS n
            FROM tool_calls l LEFT JOIN users u ON u.sub = l.sub
            WHERE l.sub = ANY(%s) AND l.tool = %s{slug_clause} AND l.ok
            GROUP BY u.email ORDER BY n DESC
            """,
            tuple(base_params),
        ).fetchall()
        daily = conn.execute(
            f"""
            SELECT (l.created_at AT TIME ZONE 'UTC')::date AS d, COUNT(*) AS n
            FROM tool_calls l
            WHERE l.sub = ANY(%s) AND l.tool = %s{slug_clause} AND l.ok
              AND l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY d
            """,
            tuple(base_params + [days]),
        ).fetchall()
    return {
        "count": sum(int(r["n"]) for r in callers),
        "callers": [r["email"] for r in callers if r["email"]],
        "daily": {str(r["d"]): int(r["n"]) for r in daily},
    }


def tool_call_stats(since_days: int = 7) -> dict:
    """Agrégats pour le dashboard de monitoring sur les `since_days` derniers jours :
    total, échecs, ventilation par tool / par user / par jour."""
    since_days = max(1, min(int(since_days), 365))
    with _connect() as conn:
        totals = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   COUNT(DISTINCT sub) AS users
            FROM tool_calls
            WHERE created_at >= NOW() - make_interval(days => %s)
            """,
            (since_days,),
        ).fetchone() or {}
        by_tool = conn.execute(
            """
            SELECT tool AS tool_name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors,
                   ROUND(AVG(duration_ms))::int AS avg_ms
            FROM tool_calls
            WHERE created_at >= NOW() - make_interval(days => %s)
            GROUP BY tool
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_user = conn.execute(
            """
            SELECT l.sub, u.email, u.name,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT l.ok) AS errors
            FROM tool_calls l
            LEFT JOIN users u ON u.sub = l.sub
            WHERE l.created_at >= NOW() - make_interval(days => %s)
            GROUP BY l.sub, u.email, u.name
            ORDER BY calls DESC
            LIMIT 100
            """,
            (since_days,),
        ).fetchall()
        by_day = conn.execute(
            """
            SELECT to_char(created_at::date, 'YYYY-MM-DD') AS day,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE NOT ok) AS errors
            FROM tool_calls
            WHERE created_at >= NOW() - make_interval(days => %s)
            GROUP BY created_at::date
            ORDER BY created_at::date
            """,
            (since_days,),
        ).fetchall()
    return {
        "since_days": since_days,
        "total_calls": int((totals or {}).get("total") or 0),
        "error_count": int((totals or {}).get("errors") or 0),
        "active_users": int((totals or {}).get("users") or 0),
        "by_tool": list(by_tool),
        "by_user": list(by_user),
        "by_day": list(by_day),
    }


def prune_tool_calls(keep_days: int = 30) -> int:
    """Retire les lignes de journal plus vieilles que `keep_days`. Borne la
    volumétrie (appelé au boot dans init_db). Retourne le nombre de lignes
    supprimées."""
    keep_days = max(1, int(keep_days))
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM tool_calls WHERE created_at < NOW() - make_interval(days => %s)",
            (keep_days,),
        )
        return cur.rowcount or 0


# --- file d'envoi d'email différé (scheduled_emails) ------------------------

_SCHED_MAX_ATTEMPTS = 3


def enqueue_scheduled_email(*, org_id: Optional[int], created_by: Optional[str],
                            to_email: str, subject: str, body_html: str,
                            from_email: Optional[str], from_name: Optional[str],
                            reply_to: Optional[str], transport: str,
                            scheduled_at: datetime) -> int:
    """Met un email en file pour envoi différé (HTML déjà rendu, autz déjà vérifiée).
    `scheduled_at` doit être un datetime aware (UTC). Retourne l'id."""
    with _connect() as conn:
        row = conn.execute(
            """INSERT INTO scheduled_emails
                 (org_id, created_by, to_email, subject, body_html, from_email,
                  from_name, reply_to, transport, scheduled_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
               RETURNING id""",
            (org_id, created_by, to_email, subject, body_html, from_email,
             from_name, reply_to, transport, scheduled_at),
        ).fetchone()
        return int(row["id"])


def claim_due_scheduled_emails(limit: int = 50) -> list[dict]:
    """Réclame atomiquement les emails dus (pending & scheduled_at <= now), en
    incrémentant `attempts` (claim). `FOR UPDATE SKIP LOCKED` = sûr même si deux
    boucles tournaient. Retourne les lignes à envoyer."""
    with _connect() as conn:
        rows = conn.execute(
            """UPDATE scheduled_emails SET attempts = attempts + 1
               WHERE id IN (
                   SELECT id FROM scheduled_emails
                   WHERE status = 'pending' AND scheduled_at <= NOW()
                   ORDER BY scheduled_at ASC
                   FOR UPDATE SKIP LOCKED
                   LIMIT %s)
               RETURNING id, org_id, to_email, subject, body_html, from_email,
                         from_name, reply_to, transport, attempts""",
            (max(1, int(limit)),),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_scheduled_sent(email_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE scheduled_emails SET status = 'sent', sent_at = NOW(), error = NULL "
            "WHERE id = %s", (email_id,),
        )


def mark_scheduled_failed(email_id: int, error: str) -> None:
    """Échec d'une tentative : repasse en `pending` pour réessayer au prochain tick
    tant que `attempts < _SCHED_MAX_ATTEMPTS` ; sinon fige en `failed`."""
    with _connect() as conn:
        conn.execute(
            """UPDATE scheduled_emails
               SET status = CASE WHEN attempts >= %s THEN 'failed' ELSE 'pending' END,
                   error = %s
               WHERE id = %s""",
            (_SCHED_MAX_ATTEMPTS, error[:500], email_id),
        )


def list_scheduled_emails(org_id: int, status: str = "pending", limit: int = 100) -> list[dict]:
    """Emails programmés d'une org (par statut ; 'all' = tous). Sans le HTML."""
    where = "org_id = %s"
    params: list = [org_id]
    if status and status != "all":
        where += " AND status = %s"
        params.append(status)
    params.append(max(1, int(limit)))
    with _connect() as conn:
        rows = conn.execute(
            f"""SELECT id, to_email, subject, from_email, from_name, transport, status,
                       scheduled_at, attempts, sent_at, error, created_at, created_by
                FROM scheduled_emails WHERE {where}
                ORDER BY scheduled_at ASC LIMIT %s""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def cancel_scheduled_email(org_id: int, email_id: int) -> bool:
    """Annule un email encore `pending` de l'org. False si introuvable / déjà parti."""
    with _connect() as conn:
        cur = conn.execute(
            "UPDATE scheduled_emails SET status = 'cancelled' "
            "WHERE id = %s AND org_id = %s AND status = 'pending'",
            (email_id, org_id),
        )
        return (cur.rowcount or 0) > 0


# --- per-user disabled tools (scopés par org, ADR 0015 ; org_id=0 = perso) --
# Profil = (sub, org_id). org_id=0 = identité perso/globale (aucune org active) ;
# >0 = profil de cette org. Les méta-tools/REST/middleware passent l'org active.

def list_user_disabled_tools(sub: str, org_id: int = 0) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM user_disabled_tools WHERE sub = %s AND org_id = %s ORDER BY tool_name",
            (sub, org_id),
        ).fetchall()
        return [r["tool_name"] for r in rows]


def is_tool_disabled_for(sub: str, tool_name: str, org_id: int = 0) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 AS x FROM user_disabled_tools WHERE sub = %s AND org_id = %s AND tool_name = %s",
            (sub, org_id, tool_name),
        ).fetchone()
        return row is not None


def add_user_disabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (sub, org_id, tool_name),
        )


def remove_user_disabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_disabled_tools WHERE sub = %s AND org_id = %s AND tool_name = %s",
            (sub, org_id, tool_name),
        )


def replace_user_disabled_tools(sub: str, tool_names: list[str], org_id: int = 0) -> None:
    """Remplace l'ensemble des disabled_tools du profil (sub, org_id) par celui passé.

    Utilisé par `apply_user_preset` pour basculer en un appel atomique.
    """
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_disabled_tools WHERE sub = %s AND org_id = %s", (sub, org_id))
            if tool_names:
                conn.executemany(
                    "INSERT INTO user_disabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s)",
                    [(sub, org_id, t) for t in tool_names],
                )


# --- per-user enabled overrides (pour les tools masqués par défaut) ---------


def list_user_enabled_tools(sub: str, org_id: int = 0) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT tool_name FROM user_enabled_tools WHERE sub = %s AND org_id = %s ORDER BY tool_name",
            (sub, org_id),
        ).fetchall()
        return [r["tool_name"] for r in rows]


def add_user_enabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (sub, org_id, tool_name),
        )


def remove_user_enabled_tool(sub: str, tool_name: str, org_id: int = 0) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_enabled_tools WHERE sub = %s AND org_id = %s AND tool_name = %s",
            (sub, org_id, tool_name),
        )


def replace_user_enabled_tools(sub: str, tool_names: list[str], org_id: int = 0) -> None:
    """Remplace l'ensemble des enabled-overrides du profil (sub, org_id)."""
    upsert_user(sub)
    with _connect() as conn:
        with conn.transaction():
            conn.execute("DELETE FROM user_enabled_tools WHERE sub = %s AND org_id = %s", (sub, org_id))
            if tool_names:
                conn.executemany(
                    "INSERT INTO user_enabled_tools (sub, org_id, tool_name) VALUES (%s, %s, %s)",
                    [(sub, org_id, t) for t in tool_names],
                )


# --- per-user presets (scopés par org, ADR 0015) ---------------------------

def list_user_presets(sub: str, org_id: int = 0) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, enabled_tools, updated_at FROM user_presets "
            "WHERE sub = %s AND org_id = %s ORDER BY name",
            (sub, org_id),
        ).fetchall()
        return [
            {
                "name": r["name"],
                "enabled_tools": list(r["enabled_tools"] or []),
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]


def get_user_preset(sub: str, name: str, org_id: int = 0) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT name, enabled_tools, updated_at FROM user_presets "
            "WHERE sub = %s AND org_id = %s AND name = %s",
            (sub, org_id, name),
        ).fetchone()
        if not row:
            return None
        return {
            "name": row["name"],
            "enabled_tools": list(row["enabled_tools"] or []),
            "updated_at": row["updated_at"],
        }


def save_user_preset(sub: str, name: str, enabled_tools: list[str], org_id: int = 0) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            "INSERT INTO user_presets (sub, org_id, name, enabled_tools) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (sub, org_id, name) DO UPDATE SET "
            "enabled_tools = EXCLUDED.enabled_tools, updated_at = NOW()",
            (sub, org_id, name, enabled_tools),
        )


def delete_user_preset(sub: str, name: str, org_id: int = 0) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_presets WHERE sub = %s AND org_id = %s AND name = %s",
            (sub, org_id, name),
        )
        return (cur.rowcount or 0) > 0


def get_usage_today(sub: str, tool: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT count FROM usage WHERE sub = %s AND tool = %s AND day = CURRENT_DATE",
            (sub, tool),
        ).fetchone()
        return int(row["count"]) if row else 0


# --- platform keys (admin-managed) ------------------------------------------
#
# Chiffrement au repos (obligatoire) : miroir EXACT du pattern
# connector_credentials (cf. credentials_store). `api_key_enc` porte l'enveloppe
# AES-256-GCM ; pas de colonne plaintext. AAD = (provider, label) — stable sur
# l'UNIQUE(provider, label), anti-transplant.

def _pk_aad(provider: str, label: str) -> str:
    return f"platform_keys:{provider}:{label}"


def _pk_encrypt(provider: str, label: str, api_key: str) -> str:
    """Enveloppe AES-256-GCM à écrire. crypto.encrypt lève si master key absente
    (pas de stockage plaintext)."""
    from . import crypto
    return crypto.encrypt(api_key, _pk_aad(provider, label))


def _pk_reveal(row: dict, provider: str) -> Optional[str]:
    """api_key en clair depuis une ligne platform_keys : déchiffre `api_key_enc`.
    Chiffrement obligatoire (pas de plaintext) → un échec LÈVE, jamais de
    fallback silencieux."""
    enc = row.get("api_key_enc")
    if not enc:
        return None
    from . import crypto
    return crypto.decrypt(enc, _pk_aad(provider, row["label"]))


def list_platform_keys(provider: Optional[str] = None) -> list[dict]:
    """Liste les platform keys. **Inclut `api_key`** (déchiffré) — réservé à
    l'admin backend, jamais retourné via /api (la route admin masque ce champ).
    """
    sql = "SELECT id, provider, label, api_key_enc, created_at FROM platform_keys"
    params: tuple = ()
    if provider:
        sql += " WHERE provider = %s"
        params = (provider,)
    sql += " ORDER BY provider, created_at"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["api_key"] = _pk_reveal(d, d["provider"])
        d.pop("api_key_enc", None)
        out.append(d)
    return out


def get_platform_key(key_id: int) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, provider, label, api_key_enc, created_at "
            "FROM platform_keys WHERE id = %s",
            (key_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _pk_reveal(d, d["provider"])
    d.pop("api_key_enc", None)
    return d


def create_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée une platform key. Renvoie l'id ; lève ValueError sur (provider, label) duplicata."""
    _check_provider(provider)
    if not label or not api_key:
        raise ValueError("label et api_key requis")
    enc = _pk_encrypt(provider, label, api_key)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO platform_keys (provider, label, api_key_enc) "
                "VALUES (%s, %s, %s) RETURNING id",
                (provider, label, enc),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"({provider}, {label}) existe déjà") from e
        return int(row["id"])


def upsert_platform_key(provider: str, label: str, api_key: str) -> int:
    """Crée ou met à jour la clé pour (provider, label). Idempotent — utilisé
    par le bootstrap des env vars au démarrage.
    """
    _check_provider(provider)
    enc = _pk_encrypt(provider, label, api_key)
    with _connect() as conn:
        row = conn.execute(
            """
            INSERT INTO platform_keys (provider, label, api_key_enc)
            VALUES (%s, %s, %s)
            ON CONFLICT(provider, label) DO UPDATE SET
                api_key_enc = EXCLUDED.api_key_enc
            RETURNING id
            """,
            (provider, label, enc),
        ).fetchone()
        return int(row["id"])


def delete_platform_key(key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM platform_keys WHERE id = %s", (key_id,))


# --- grants -----------------------------------------------------------------

def grant_platform_key(
    sub: str,
    platform_key_id: int,
    granted_by: Optional[str] = None,
    daily_quota: Optional[int] = None,
) -> None:
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_grants (sub, platform_key_id, granted_at, granted_by, daily_quota)
            VALUES (%s, %s, NOW(), %s, %s)
            ON CONFLICT(sub, platform_key_id) DO UPDATE SET
                granted_at = NOW(),
                granted_by = EXCLUDED.granted_by,
                daily_quota = EXCLUDED.daily_quota
            """,
            (sub, platform_key_id, granted_by, daily_quota),
        )


def revoke_platform_key(sub: str, platform_key_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "DELETE FROM user_grants WHERE sub = %s AND platform_key_id = %s",
            (sub, platform_key_id),
        )


def list_grants_for_user(sub: str) -> list[dict]:
    """Grants détaillés d'un user — joint platform_keys pour ne pas exposer
    l'api_key brut côté API. Renvoie id/provider/label/granted_at."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.provider, pk.label,
                   ug.granted_at, ug.granted_by, ug.daily_quota
              FROM user_grants ug
              JOIN platform_keys pk ON pk.id = ug.platform_key_id
             WHERE ug.sub = %s
             ORDER BY pk.provider, ug.granted_at DESC
            """,
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_grant(sub: str, provider: str) -> Optional[dict]:
    """Grant à utiliser pour ce (user, provider) — le plus récemment granté
    s'il y en a plusieurs. Renvoie {platform_key_id, label, api_key} ou None.
    """
    _check_provider(provider)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.label, pk.api_key_enc,
                   ug.daily_quota
              FROM user_grants ug
              JOIN platform_keys pk ON pk.id = ug.platform_key_id
             WHERE ug.sub = %s AND pk.provider = %s
             ORDER BY ug.granted_at DESC
             LIMIT 1
            """,
            (sub, provider),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _pk_reveal(d, provider)   # déchiffre JIT (resolve_api_key)
    d.pop("api_key_enc", None)
    return d


def grant_org_platform_key(org_id: int, platform_key_id: int,
                           granted_by: Optional[str] = None,
                           daily_quota: Optional[int] = None) -> None:
    """Partage une clé plateforme à TOUTE l'org (couche 2). Miroir org de grant_platform_key."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_grants (org_id, platform_key_id, granted_at, granted_by, daily_quota)
            VALUES (%s, %s, NOW(), %s, %s)
            ON CONFLICT(org_id, platform_key_id) DO UPDATE SET
                granted_at = NOW(), granted_by = EXCLUDED.granted_by,
                daily_quota = EXCLUDED.daily_quota
            """,
            (org_id, platform_key_id, granted_by, daily_quota),
        )


def revoke_org_platform_key(org_id: int, platform_key_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM org_grants WHERE org_id = %s AND platform_key_id = %s",
                     (org_id, platform_key_id))


def list_org_grants(org_id: int) -> list[dict]:
    """Grants de clé plateforme d'une org (joint platform_keys, sans api_key brut)."""
    with _connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.provider, pk.label,
                   og.granted_at, og.granted_by, og.daily_quota
              FROM org_grants og JOIN platform_keys pk ON pk.id = og.platform_key_id
             WHERE og.org_id = %s ORDER BY pk.provider, og.granted_at DESC
            """,
            (org_id,),
        )]


def get_active_org_grant(org_id: int, provider: str) -> Optional[dict]:
    """Grant de clé plateforme de l'org pour `provider` (le plus récent), ou None.
    Miroir org de get_active_grant — résout la clé plateforme partagée à l'org."""
    _check_provider(provider)
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT pk.id AS platform_key_id, pk.label, pk.api_key_enc, og.daily_quota
              FROM org_grants og JOIN platform_keys pk ON pk.id = og.platform_key_id
             WHERE og.org_id = %s AND pk.provider = %s
             ORDER BY og.granted_at DESC LIMIT 1
            """,
            (org_id, provider),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["api_key"] = _pk_reveal(d, provider)
    d.pop("api_key_enc", None)
    return d


# ── RBAC connecteur interne à l'org (ADR 0025) ──────────────────────────────
def set_connector_access(org_id: int, connector: str, principal_type: str,
                         principal_id: str, granted_by: Optional[str] = None) -> None:
    """Autorise un principal (groupe/user) sur un connecteur dans l'org → le rend
    RESTREINT (deny-by-default) s'il ne l'était pas. Idempotent."""
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO org_connector_access (org_id, connector, principal_type, principal_id, granted_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (org_id, connector, principal_type, principal_id) DO NOTHING
            """,
            (org_id, connector, principal_type, str(principal_id), granted_by),
        )


def clear_connector_access(org_id: int, connector: str, principal_type: str,
                           principal_id: str) -> None:
    """Retire un principal. Quand la dernière ligne d'un (org, connector) part,
    le connecteur redevient OUVERT à toute l'org (absence ⟹ non restreint)."""
    with _connect() as conn:
        conn.execute(
            "DELETE FROM org_connector_access WHERE org_id = %s AND connector = %s "
            "AND principal_type = %s AND principal_id = %s",
            (org_id, connector, principal_type, str(principal_id)),
        )


def list_connector_access(org_id: int, connector: Optional[str] = None) -> list[dict]:
    """ACL connecteur de l'org : [{connector, principal_type, principal_id, granted_at}]."""
    sql = ("SELECT connector, principal_type, principal_id, granted_by, granted_at "
           "FROM org_connector_access WHERE org_id = %s")
    args: tuple = (org_id,)
    if connector is not None:
        sql += " AND connector = %s"
        args = (org_id, connector)
    sql += " ORDER BY connector, principal_type, principal_id"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def org_restricted_connectors(org_id: int) -> set:
    """Connecteurs RESTREINTS dans l'org (≥1 ligne d'ACL) — deny-by-default pour eux."""
    with _connect() as conn:
        return {r["connector"] for r in conn.execute(
            "SELECT DISTINCT connector FROM org_connector_access WHERE org_id = %s",
            (org_id,)).fetchall()}


def member_allowed_connectors(sub: str, org_id: int) -> set:
    """Connecteurs (restreints) auxquels `sub` a droit dans l'org : ligne user=sub
    OU groupe ∈ ses groupes de l'org. (Un connecteur non restreint n'est pas listé
    ici mais reste ouvert — cf. org_restricted_connectors.)"""
    with _connect() as conn:
        return {r["connector"] for r in conn.execute(
            """
            SELECT DISTINCT a.connector FROM org_connector_access a
             WHERE a.org_id = %s AND (
                   (a.principal_type = 'user' AND a.principal_id = %s)
                OR (a.principal_type = 'group' AND a.principal_id IN (
                       SELECT m.group_id::text FROM org_group_members m
                         JOIN org_groups g ON g.id = m.group_id
                        WHERE m.sub = %s AND g.org_id = %s)))
            """,
            (org_id, sub, sub, org_id)).fetchall()}


def list_users_with_grants() -> list[dict]:
    """Pour /api/admin/users — chaque user + ses grants (sans api_key)."""
    users = list_users()
    out = []
    for u in users:
        u = dict(u)
        u["grants"] = list_grants_for_user(u["sub"])
        out.append(u)
    return out


# --- namespace grants (deny-by-default pour namespaces sensibles) -----------

def grant_namespace(sub: str, namespace: str, granted_by: Optional[str] = None) -> None:
    """Accorde à `sub` l'accès au namespace sensible `namespace` (idempotent)."""
    upsert_user(sub)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO user_namespace_grants (sub, namespace, granted_by)
            VALUES (%s, %s, %s)
            ON CONFLICT(sub, namespace) DO UPDATE SET
                granted_at = NOW(),
                granted_by = EXCLUDED.granted_by
            """,
            (sub, namespace, granted_by),
        )


def revoke_namespace(sub: str, namespace: str) -> bool:
    """Révoque l'accès. Renvoie True si un grant existait."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_namespace_grants WHERE sub = %s AND namespace = %s",
            (sub, namespace),
        )
        return (cur.rowcount or 0) > 0


def list_user_granted_namespaces(sub: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT namespace FROM user_namespace_grants WHERE sub = %s ORDER BY namespace",
            (sub,),
        ).fetchall()
        return [r["namespace"] for r in rows]


def list_namespace_grants(namespace: Optional[str] = None) -> list[dict]:
    """Tous les grants de namespace (vue admin), filtrable par namespace."""
    sql = "SELECT sub, namespace, granted_by, granted_at FROM user_namespace_grants"
    params: tuple = ()
    if namespace:
        sql += " WHERE namespace = %s"
        params = (namespace,)
    sql += " ORDER BY namespace, granted_at DESC"
    with _connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# --- Google OAuth -----------------------------------------------------------

GOOGLE = "google"   # connecteur Google dans le coffre (account = email)


def _google_row(account: str, cur: dict) -> dict:
    """Reconstruit le dict legacy (contrat google_oauth.py) depuis une ligne coffre
    (cur = {secret, meta, set_at})."""
    m = cur["meta"]
    return {
        "google_email": account or None,
        "refresh_token": cur["secret"],
        "access_token": m.get("access_token"),
        "expires_at": m.get("expires_at"),
        "scopes": m.get("scopes"),
        "is_default": bool(m.get("is_default")),
        "granted_at": m.get("granted_at"),
        "updated_at": cur["set_at"],
    }


def set_google_oauth(
    sub: str,
    google_email: str,
    refresh_token: str,
    scopes: str,
    access_token: Optional[str] = None,
    expires_at: Optional[str] = None,
    make_default: Optional[bool] = None,
) -> None:
    """Upsert un compte Google dans le COFFRE (connector='google', account=email ;
    satellites — access_token/expires_at/scopes/is_default/granted_at — dans meta).

    `make_default` None → défaut si 1er compte. is_default conservé si déjà défaut
    (existing OR new). Claime la ligne mono pré-migration (account='').
    """
    upsert_user(sub)
    from . import credentials_store
    account = google_email or ""
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    n_named = sum(1 for a in accts if a["account"])
    prior = next((a for a in accts if a["account"] == account), None)
    if make_default is None:
        make_default = n_named == 0
    is_default = bool(prior and prior["meta"].get("is_default")) or make_default
    granted_at = (prior["meta"].get("granted_at") if prior else None) \
        or datetime.now(timezone.utc).isoformat()
    meta = {"access_token": access_token, "expires_at": expires_at, "scopes": scopes,
            "is_default": is_default, "granted_at": granted_at}
    with _connect() as conn:
        with conn.transaction():
            if account:   # claim l'éventuelle ligne mono pré-migration (account='')
                credentials_store.clear_credential("user", sub, GOOGLE, account="", conn=conn)
            if make_default:   # un seul défaut : retire le flag aux autres comptes
                conn.execute(
                    "UPDATE connector_credentials SET meta = jsonb_set(meta, '{is_default}', 'false') "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s AND account<>%s",
                    (sub, GOOGLE, account),
                )
            credentials_store.set_credential(
                "user", sub, GOOGLE, refresh_token, set_by=sub,
                meta=meta, account=account, conn=conn)


def update_google_access_token(
    sub: str, google_email: Optional[str], access_token: str, expires_at: str
) -> None:
    """Met à jour SEULEMENT l'access_token + expiry (sur refresh) — merge meta dans
    le coffre, SANS re-chiffrer le refresh_token. `google_email` None = compte mono
    (account='')."""
    from . import credentials_store
    account = google_email or ""
    credentials_store.update_meta(
        "user", sub, GOOGLE, account,
        {"access_token": access_token, "expires_at": expires_at})


def get_google_oauth(sub: str, account: Optional[str] = None) -> Optional[dict]:
    """Renvoie un compte Google du user depuis le COFFRE (déchiffre le
    refresh_token). `account` (email) cible un compte ; None = le défaut
    (meta.is_default), à défaut le plus ancien (granted_at)."""
    from . import credentials_store
    if account:
        cur = credentials_store.get_credential_with_meta("user", sub, GOOGLE, account=account)
        return _google_row(account, cur) if cur else None
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    if not accts:
        return None
    chosen = next((a for a in accts if a["meta"].get("is_default")), None) \
        or min(accts, key=lambda a: a["meta"].get("granted_at") or "")
    cur = credentials_store.get_credential_with_meta("user", sub, GOOGLE, account=chosen["account"])
    return _google_row(chosen["account"], cur) if cur else None


def list_google_accounts(sub: str) -> list[dict]:
    """Liste les comptes Google connectés (sans les tokens) — depuis le coffre."""
    from . import credentials_store
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    out = [{
        "google_email": a["account"] or None,
        "is_default": bool(a["meta"].get("is_default")),
        "scopes": a["meta"].get("scopes"),
        "granted_at": a["meta"].get("granted_at"),
        "updated_at": a["set_at"],
    } for a in accts]
    out.sort(key=lambda r: (not r["is_default"], r["granted_at"] or ""))
    return out


def set_default_google_account(sub: str, account: str) -> bool:
    """Marque `account` comme défaut (meta.is_default) dans le coffre. False si le
    compte n'existe pas."""
    from . import credentials_store
    accts = credentials_store.list_accounts("user", sub, GOOGLE)
    if not any(a["account"] == account for a in accts):
        return False
    with _connect() as conn:
        conn.execute(
            "UPDATE connector_credentials "
            "SET meta = jsonb_set(meta, '{is_default}', to_jsonb(account = %s)) "
            "WHERE entity_type='user' AND entity_id=%s AND connector=%s",
            (account, sub, GOOGLE),
        )
    return True


def delete_google_oauth(sub: str, account: Optional[str] = None) -> None:
    """Supprime un compte (account=email) ou tous (account=None) du coffre. Si on
    retire le défaut et qu'il reste des comptes, promeut le plus ancien."""
    from . import credentials_store
    with _connect() as conn:
        with conn.transaction():
            if account is None:
                conn.execute(
                    "DELETE FROM connector_credentials "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s", (sub, GOOGLE))
                return
            credentials_store.clear_credential("user", sub, GOOGLE, account=account, conn=conn)
            # promotion du défaut : lire le RESTANT dans CETTE transaction (voit le delete)
            rem = conn.execute(
                "SELECT account, meta FROM connector_credentials "
                "WHERE entity_type='user' AND entity_id=%s AND connector=%s", (sub, GOOGLE)).fetchall()
            if rem and not any((r["meta"] or {}).get("is_default") for r in rem):
                oldest = min(rem, key=lambda r: (r["meta"] or {}).get("granted_at") or "")["account"]
                conn.execute(
                    "UPDATE connector_credentials SET meta = jsonb_set(meta, '{is_default}', 'true') "
                    "WHERE entity_type='user' AND entity_id=%s AND connector=%s AND account=%s",
                    (sub, GOOGLE, oldest))


# --- Datastore namespaces ---------------------------------------------------

def create_datastore_namespace(sub: str, namespace: str) -> int:
    upsert_user(sub)
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO user_datastores (sub, namespace) VALUES (%s, %s) RETURNING id",
                (sub, namespace),
            ).fetchone()
        except psycopg.errors.UniqueViolation as e:
            raise ValueError(f"namespace `{namespace}` existe déjà") from e
        return int(row["id"])


def get_datastore_namespace(sub: str, namespace: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, sub, namespace, created_at FROM user_datastores "
            "WHERE sub = %s AND namespace = %s",
            (sub, namespace),
        ).fetchone()
        return dict(row) if row else None


def list_datastore_namespaces(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, namespace, created_at FROM user_datastores WHERE sub = %s ORDER BY namespace",
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_datastore_namespace(sub: str, namespace: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_datastores WHERE sub = %s AND namespace = %s",
            (sub, namespace),
        )
        return cur.rowcount > 0


# --- Datastore shares --------------------------------------------------------

def share_datastore_namespace(
    owner_sub: str, namespace: str, shared_with_sub: str, permission: str = "write",
) -> int:
    ns = get_datastore_namespace(owner_sub, namespace)
    if not ns:
        raise ValueError(f"namespace `{namespace}` not found for owner")
    with _connect() as conn:
        try:
            row = conn.execute(
                "INSERT INTO datastore_shares (owner_sub, namespace, shared_with_sub, permission) "
                "VALUES (%s, %s, %s, %s) RETURNING id",
                (owner_sub, namespace, shared_with_sub, permission),
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            conn.execute(
                "UPDATE datastore_shares SET permission = %s "
                "WHERE owner_sub = %s AND namespace = %s AND shared_with_sub = %s",
                (permission, owner_sub, namespace, shared_with_sub),
            )
            return 0
        return int(row["id"])


def unshare_datastore_namespace(owner_sub: str, namespace: str, shared_with_sub: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM datastore_shares WHERE owner_sub = %s AND namespace = %s AND shared_with_sub = %s",
            (owner_sub, namespace, shared_with_sub),
        )
        return cur.rowcount > 0


def get_shared_namespace(sub: str, namespace: str) -> Optional[dict]:
    """Le partage reçu par `sub` pour `namespace` (owner_sub + permission). La
    résolution du `ns_id` réel (pour lire les rows) se fait côté store via
    `get_datastore_namespace(owner_sub, namespace)`."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT owner_sub, namespace, permission, created_at FROM datastore_shares "
            "WHERE shared_with_sub = %s AND namespace = %s LIMIT 1",
            (sub, namespace),
        ).fetchone()
        return dict(row) if row else None


def list_shared_namespaces(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.namespace, s.owner_sub, s.permission, s.created_at, d.id "
            "FROM datastore_shares s "
            "JOIN user_datastores d ON d.sub = s.owner_sub AND d.namespace = s.namespace "
            "WHERE s.shared_with_sub = %s ORDER BY s.namespace",
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def list_namespace_shares(owner_sub: str, namespace: str) -> list[dict]:
    """Bénéficiaires d'un namespace possédé (email + permission), pour l'UI de
    gestion du partage. Email résolu par join sur `users` (None si user effacé)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT s.shared_with_sub, s.permission, s.created_at, u.email "
            "FROM datastore_shares s LEFT JOIN users u ON u.sub = s.shared_with_sub "
            "WHERE s.owner_sub = %s AND s.namespace = %s ORDER BY s.created_at",
            (owner_sub, namespace),
        ).fetchall()
        return [dict(r) for r in rows]


def rename_datastore_namespace(sub: str, old: str, new: str) -> bool:
    """Renomme un namespace possédé (l'`id` BIGSERIAL est conservé → URL/deeplink
    stables) et propage le nouveau nom aux partages (`datastore_shares` est keyé par
    nom). Lève si `new` existe déjà ou si `old` est introuvable."""
    new = (new or "").strip()
    if not new:
        raise ValueError("nouveau nom de namespace requis")
    if new == old:
        return True
    with _connect() as conn:
        with conn.transaction():
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE sub = %s AND namespace = %s",
                (sub, new),
            ).fetchone():
                raise ValueError(f"un namespace `{new}` existe déjà")
            cur = conn.execute(
                "UPDATE user_datastores SET namespace = %s WHERE sub = %s AND namespace = %s",
                (new, sub, old),
            )
            if (cur.rowcount or 0) == 0:
                raise ValueError(f"namespace `{old}` introuvable")
            conn.execute(
                "UPDATE datastore_shares SET namespace = %s WHERE owner_sub = %s AND namespace = %s",
                (new, sub, old),
            )
    return True


def transfer_datastore_namespace(owner_sub: str, namespace: str, new_owner_sub: str) -> bool:
    """Transfère la propriété d'un namespace à `new_owner_sub` (l'`id` est conservé)
    et **repasse l'ancien propriétaire en partage `write`** (« tu passes en partagé »).
    Lève si le destinataire possède déjà un namespace de ce nom, ou si introuvable."""
    if new_owner_sub == owner_sub:
        return True
    with _connect() as conn:
        with conn.transaction():
            if conn.execute(
                "SELECT 1 FROM user_datastores WHERE sub = %s AND namespace = %s",
                (new_owner_sub, namespace),
            ).fetchone():
                raise ValueError(f"le destinataire possède déjà un namespace `{namespace}`")
            cur = conn.execute(
                "UPDATE user_datastores SET sub = %s WHERE sub = %s AND namespace = %s",
                (new_owner_sub, owner_sub, namespace),
            )
            if (cur.rowcount or 0) == 0:
                raise ValueError(f"namespace `{namespace}` introuvable")
            # Repointer les partages existants vers le nouveau propriétaire.
            conn.execute(
                "UPDATE datastore_shares SET owner_sub = %s WHERE owner_sub = %s AND namespace = %s",
                (new_owner_sub, owner_sub, namespace),
            )
            # Le nouveau propriétaire ne peut pas être bénéficiaire de son propre ns.
            conn.execute(
                "DELETE FROM datastore_shares WHERE owner_sub = %s AND namespace = %s AND shared_with_sub = %s",
                (new_owner_sub, namespace, new_owner_sub),
            )
            # L'ancien propriétaire devient bénéficiaire en write.
            conn.execute(
                "INSERT INTO datastore_shares (owner_sub, namespace, shared_with_sub, permission) "
                "VALUES (%s, %s, %s, 'write') "
                "ON CONFLICT (owner_sub, namespace, shared_with_sub) DO UPDATE SET permission = 'write'",
                (new_owner_sub, namespace, owner_sub),
            )
    return True


# --- Datastore rows (substrat PG natif, ADR 0016) ---------------------------

def datastore_insert_row(ns_id: int, row_id: str, data: dict,
                         created_at: Optional[str] = None,
                         updated_at: Optional[str] = None) -> dict:
    """Insère une row. `created_at`/`updated_at` optionnels (override pour le
    backfill ; sinon NOW())."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, COALESCE(%s::timestamptz, NOW()), COALESCE(%s::timestamptz, NOW())) "
            "RETURNING row_id, created_at, updated_at, data",
            (ns_id, row_id, json.dumps(data), created_at, updated_at),
        ).fetchone()
        return dict(row)


def datastore_upsert_row(ns_id: int, row_id: str, data: dict) -> tuple[dict, bool]:
    """Insère OU met à jour une row par sa clé `(ns_id, row_id)`. Idempotent :
    re-poser le même `row_id` remplace `data` au lieu de dupliquer (sert la
    dédup par clé stable, ex. urn LinkedIn). Renvoie `(row, inserted)` où
    `inserted` est True si la row n'existait pas (ON CONFLICT non déclenché)."""
    with _connect() as conn:
        row = conn.execute(
            "INSERT INTO datastore_rows (ns_id, row_id, data, created_at, updated_at) "
            "VALUES (%s, %s, %s::jsonb, NOW(), NOW()) "
            "ON CONFLICT (ns_id, row_id) DO UPDATE SET data = EXCLUDED.data, updated_at = NOW() "
            "RETURNING row_id, created_at, updated_at, data, (xmax = 0) AS inserted",
            (ns_id, row_id, json.dumps(data)),
        ).fetchone()
        inserted = bool(row.pop("inserted"))
        return dict(row), inserted


def datastore_get_row(ns_id: int, row_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT row_id, created_at, updated_at, data FROM datastore_rows "
            "WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


def datastore_list_rows(ns_id: int, *, offset: int = 0, limit: Optional[int] = None,
                        order_by: Optional[str] = None, order_dir: str = "desc",
                        q: Optional[str] = None) -> list[dict]:
    """Page de rows d'un namespace. `order_by` : `_created_at`/`_updated_at`/`_id`
    (colonnes méta) ou un nom de champ user → `data->>field`. `q` : recherche
    plein-texte sur tout le JSON (`data::text ILIKE`). Tri/pagination/recherche
    côté SQL (server-side, ADR 0016). `limit=None` = toutes les rows (compat
    `store.list_rows` / MCP `data_rows` qui filtrent ensuite en Python)."""
    direction = "ASC" if str(order_dir).lower() == "asc" else "DESC"
    where = "WHERE ns_id = %s"
    params: list = [ns_id]
    if q:
        where += " AND data::text ILIKE %s"
        params.append(f"%{q}%")
    if order_by in (None, "", "_created_at"):
        order_sql = f"created_at {direction}, row_id {direction}"
    elif order_by == "_updated_at":
        order_sql = f"updated_at {direction}, row_id {direction}"
    elif order_by == "_id":
        order_sql = f"row_id {direction}"
    else:
        order_sql = f"data ->> %s {direction}, row_id {direction}"
        params.append(order_by)  # valeur paramétrée → pas d'injection
    tail = ""
    if limit is not None:
        tail = " LIMIT %s OFFSET %s"
        params.extend([limit, offset])
    with _connect() as conn:
        rows = conn.execute(
            "SELECT row_id, created_at, updated_at, data FROM datastore_rows "
            f"{where} ORDER BY {order_sql}{tail}",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def datastore_count_rows(ns_id: int, q: Optional[str] = None) -> int:
    """Nombre total de rows d'un namespace (pour la pagination), filtré par `q`."""
    where = "WHERE ns_id = %s"
    params: list = [ns_id]
    if q:
        where += " AND data::text ILIKE %s"
        params.append(f"%{q}%")
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM datastore_rows {where}", tuple(params)
        ).fetchone()
        return int(row["n"]) if row else 0


def datastore_update_row(ns_id: int, row_id: str, data: dict, updated_at: str) -> Optional[dict]:
    """Remplace `data` (le store a déjà fusionné le patch) + `updated_at`."""
    with _connect() as conn:
        row = conn.execute(
            "UPDATE datastore_rows SET data = %s::jsonb, updated_at = %s::timestamptz "
            "WHERE ns_id = %s AND row_id = %s "
            "RETURNING row_id, created_at, updated_at, data",
            (json.dumps(data), updated_at, ns_id, row_id),
        ).fetchone()
        return dict(row) if row else None


def datastore_delete_row(ns_id: int, row_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM datastore_rows WHERE ns_id = %s AND row_id = %s",
            (ns_id, row_id),
        )
        return (cur.rowcount or 0) > 0


# --- API tokens (CLI auth) --------------------------------------------------

_TOKEN_PREFIX = "oto_"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(sub: str, label: str = "cli", ttl_days: Optional[int] = None) -> str:
    """Génère un token, persiste son hash, renvoie le plaintext une seule fois.

    `ttl_days` : si fourni (>0), le token expire après ce délai et est rejeté
    par `verify_api_token`. None = non-expirant (défaut — token CLI long-lived
    stocké en SOPS). La révocation explicite reste `delete_api_token`.
    """
    upsert_user(sub)
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires = f"NOW() + INTERVAL '{int(ttl_days)} days'" if ttl_days and ttl_days > 0 else "NULL"
    with _connect() as conn:
        conn.execute(
            f"INSERT INTO user_api_tokens (sub, label, token_hash, expires_at) "
            f"VALUES (%s, %s, %s, {expires})",
            (sub, label, _hash_token(token)),
        )
    return token


def verify_api_token(token: str) -> Optional[str]:
    """Renvoie le sub du token, et met à jour last_used_at. None si inconnu ou expiré."""
    if not token or not token.startswith(_TOKEN_PREFIX):
        return None
    h = _hash_token(token)
    with _connect() as conn:
        row = conn.execute(
            "SELECT sub FROM user_api_tokens "
            "WHERE token_hash = %s AND (expires_at IS NULL OR expires_at > NOW())",
            (h,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE user_api_tokens SET last_used_at = NOW() WHERE token_hash = %s",
            (h,),
        )
        return row["sub"]


def list_api_tokens(sub: str) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, label, created_at, last_used_at, expires_at FROM user_api_tokens WHERE sub = %s ORDER BY created_at DESC",
            (sub,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_api_token(sub: str, token_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM user_api_tokens WHERE sub = %s AND id = %s",
            (sub, token_id),
        )
        return cur.rowcount > 0


# ── Schéma observé des connecteurs (rédaction de champs) ──────────────────────
def get_connector_schema(service: str) -> dict:
    """Squelette observé d'un connecteur (`{name: {type, paths:[...]}}`), {} si aucun."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT schema FROM connector_schemas WHERE service = %s", (service,)
        ).fetchone()
    return (row or {}).get("schema") or {}


def get_all_connector_schemas() -> dict:
    """Tous les schémas observés, `{service: {name: {type, paths}}}`."""
    with _connect() as conn:
        rows = conn.execute("SELECT service, schema FROM connector_schemas").fetchall()
    return {r["service"]: (r["schema"] or {}) for r in rows}


def upsert_connector_schema(service: str, schema: dict) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO connector_schemas (service, schema, updated_at) "
            "VALUES (%s, %s::jsonb, NOW()) "
            "ON CONFLICT (service) DO UPDATE SET schema = EXCLUDED.schema, updated_at = NOW()",
            (service, json.dumps(schema)),
        )


# --- BOAMP (avis de marchés publics, france-opendata#3) ----------------------

_BOAMP_COLS = [
    "idweb", "annee", "objet", "organisme",
    "date_publication", "date_limite_reponse", "date_fin_diffusion",
    "dep_publication", "nature_marche", "type_procedure",
    "type_avis_nature", "type_avis_famille", "statut",
    "descripteurs_libelle", "descripteurs_json", "synthese", "url",
]


def upsert_boamp(rows: list[dict]) -> int:
    """Insère/met à jour des avis BOAMP (clé idweb). Idempotent. Retourne le nb
    de lignes traitées. Conçu pour des batches (ingestion jour-par-jour)."""
    if not rows:
        return 0
    cols = ", ".join(_BOAMP_COLS)
    placeholders = ", ".join(["%s"] * len(_BOAMP_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _BOAMP_COLS if c != "idweb")
    sql = (
        f"INSERT INTO boamp ({cols}, ingested_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (idweb) DO UPDATE SET {updates}, ingested_at = NOW()"
    )
    data = [tuple(r.get(c) for c in _BOAMP_COLS) for r in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data)
    return len(data)


def _boamp_row(r: dict) -> dict:
    """Normalise une ligne BOAMP : descripteurs_json (TEXT) → liste `descripteurs`."""
    out = dict(r)
    raw = out.pop("descripteurs_json", None)
    if raw:
        try:
            out["descripteurs"] = json.loads(raw)
        except (ValueError, TypeError):
            pass
    out.pop("ingested_at", None)
    return out


def search_boamp(
    query: Optional[str] = None,
    descripteur: Optional[str] = None,
    departement: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    type_marche: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recherche d'avis BOAMP (table PG). Filtres AND. Renvoie {results, total_count}."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    clauses, params = ["1=1"], []
    if query:
        clauses.append("objet ILIKE %s"); params.append(f"%{query}%")
    if descripteur:
        clauses.append("descripteurs_libelle ILIKE %s"); params.append(f"%{descripteur}%")
    if departement:
        clauses.append("dep_publication = %s"); params.append(departement)
    if date_from:
        clauses.append("date_publication >= %s"); params.append(date_from)
    if date_to:
        clauses.append("date_publication <= %s"); params.append(date_to)
    if type_marche:
        clauses.append("nature_marche = %s"); params.append(type_marche.upper())
    where = " AND ".join(clauses)
    cols = ", ".join(_BOAMP_COLS)
    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM boamp WHERE {where}", tuple(params)
        ).fetchone()["n"]
        rows = conn.execute(
            f"SELECT {cols} FROM boamp WHERE {where} "
            "ORDER BY date_publication DESC NULLS LAST, idweb DESC "
            "LIMIT %s OFFSET %s",
            tuple(params) + (limit, offset),
        ).fetchall()
    return {"results": [_boamp_row(r) for r in rows], "total_count": int(total)}


def get_boamp(idweb: str) -> Optional[dict]:
    """Un avis BOAMP par idweb, ou None."""
    cols = ", ".join(_BOAMP_COLS)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM boamp WHERE idweb = %s LIMIT 1", (idweb,)
        ).fetchone()
    return _boamp_row(row) if row else None


def boamp_info() -> dict:
    """Métadonnées pour healthcheck : nb de lignes + plage de dates."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date_publication) AS dmin, "
            "MAX(date_publication) AS dmax FROM boamp"
        ).fetchone()
    return {"total_rows": int(r["n"]), "date_min": r["dmin"], "date_max": r["dmax"]}


def boamp_last_ingested_epoch() -> Optional[float]:
    """Epoch (s) du dernier upsert BOAMP, ou None si table vide. Sert de garde de
    fraîcheur au rafraîchissement in-process (ne pas recrawler si récent)."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(ingested_at)) AS e FROM boamp"
        ).fetchone()
    return float(r["e"]) if r and r["e"] is not None else None


# --- ACCO (accords d'entreprise, base nationale des accords collectifs) -------
# Colonnes alignées sur france_opendata.acco.COLUMNS (l'ingestion réutilise le parser).

_ACCO_COLS = [
    "id", "nature", "numero", "siret", "raison_sociale", "code_ape", "code_idcc",
    "secteur", "date_texte", "date_depot", "date_effet", "date_fin", "date_maj",
    "date_diffusion", "conforme_version_integrale", "theme_codes", "themes_libelle",
    "syndicats_libelle", "code_postal", "ville", "titre", "url",
]

# Colonnes triables (whitelist anti-injection : sort_by n'est jamais interpolé brut).
_ACCO_SORT = {
    "date": "date_texte", "date_depot": "date_depot",
    "date_diffusion": "date_diffusion", "date_maj": "date_maj",
}


def upsert_acco(rows: list[dict]) -> int:
    """Insère/met à jour des accords (clé id DILA). Idempotent. Pour batches."""
    if not rows:
        return 0
    cols = ", ".join(_ACCO_COLS)
    placeholders = ", ".join(["%s"] * len(_ACCO_COLS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _ACCO_COLS if c != "id")
    sql = (
        f"INSERT INTO acco ({cols}, ingested_at) "
        f"VALUES ({placeholders}, NOW()) "
        f"ON CONFLICT (id) DO UPDATE SET {updates}, ingested_at = NOW()"
    )
    data = [tuple(r.get(c) for c in _ACCO_COLS) for r in rows]
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.executemany(sql, data)
    return len(data)


def _acco_row(r: dict) -> dict:
    """Normalise une ligne ACCO : theme_codes (TEXT JSON) → liste `theme_codes`."""
    out = dict(r)
    raw = out.get("theme_codes")
    if raw:
        try:
            out["theme_codes"] = json.loads(raw)
        except (ValueError, TypeError):
            out["theme_codes"] = None
    out.pop("ingested_at", None)
    return out


def search_acco(
    query: Optional[str] = None,
    themes: Optional[list[str]] = None,
    nature: Optional[str] = None,
    siret: Optional[str] = None,
    idcc: Optional[str] = None,
    departement: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    latest_per_siret: bool = False,
    sort_by: str = "date",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Recherche d'accords d'entreprise (table PG) — primitive neutre, lignes brutes.

    Filtres AND (sauf `themes` : OR interne). `latest_per_siret` réduit à 1 ligne par
    entreprise (l'acte le plus récent) AVANT d'appliquer date_from/date_to (→ « dernier
    accord antérieur à X » = contrat dormant). Renvoie {results, total_count}."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    order_col = _ACCO_SORT.get(sort_by, "date_texte")
    order_dir = "ASC" if str(sort_dir).lower() == "asc" else "DESC"
    order = f"{order_col} {order_dir} NULLS LAST, id {order_dir}"

    # Filtres « population » (avant réduction par SIRET).
    pop, params = ["1=1"], []
    if query:
        pop.append("titre ILIKE %s"); params.append(f"%{query}%")
    if themes:
        ors = []
        for t in themes:
            ors.append("theme_codes LIKE %s"); params.append(f'%"{t}"%')
        pop.append("(" + " OR ".join(ors) + ")")
    if nature:
        pop.append("nature = %s"); params.append(nature.upper())
    if siret:
        pop.append("siret = %s"); params.append(siret)
    if idcc:
        pop.append("code_idcc = %s"); params.append(idcc)
    if departement:
        pop.append("code_postal LIKE %s"); params.append(f"{departement}%")
    pop_clause = " AND ".join(pop)

    # Filtres de date (sur la ligne retenue → après réduction si latest_per_siret).
    date_conds, date_params = [], []
    if date_from:
        date_conds.append("date_texte >= %s"); date_params.append(date_from)
    if date_to:
        date_conds.append("date_texte <= %s"); date_params.append(date_to)
    date_clause = (" AND " + " AND ".join(date_conds)) if date_conds else ""

    cols = ", ".join(_ACCO_COLS)
    if latest_per_siret:
        inner = (
            f"SELECT {cols}, ROW_NUMBER() OVER "
            "(PARTITION BY siret ORDER BY date_texte DESC NULLS LAST, id DESC) AS rn "
            f"FROM acco WHERE {pop_clause} AND siret IS NOT NULL"
        )
        base = f"SELECT {cols} FROM ({inner}) t WHERE rn = 1{date_clause}"
        qparams = params + date_params
    else:
        base = f"SELECT {cols} FROM acco WHERE {pop_clause}{date_clause}"
        qparams = params + date_params

    with _connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM ({base}) c", tuple(qparams)
        ).fetchone()["n"]
        rows = conn.execute(
            f"{base} ORDER BY {order} LIMIT %s OFFSET %s",
            tuple(qparams) + (limit, offset),
        ).fetchall()
    return {"results": [_acco_row(r) for r in rows], "total_count": int(total)}


def get_acco(id_or_numero: str) -> Optional[dict]:
    """Un accord par son id DILA (ACCOTEXT…) ou son numero (T…), ou None."""
    cols = ", ".join(_ACCO_COLS)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {cols} FROM acco WHERE id = %s OR numero = %s LIMIT 1",
            (id_or_numero, id_or_numero),
        ).fetchone()
    return _acco_row(row) if row else None


def acco_themes() -> list[dict]:
    """Catalogue des thèmes présents (code → libellé + nb d'accords). Découverte."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT code, libelle, COUNT(*) AS n FROM acco a, "
            "  UNNEST("
            "    ARRAY(SELECT json_array_elements_text(a.theme_codes::json)), "
            "    string_to_array(a.themes_libelle, ' | ')"
            "  ) AS t(code, libelle) "
            "WHERE a.theme_codes IS NOT NULL "
            "GROUP BY code, libelle ORDER BY n DESC"
        ).fetchall()
    return [{"code": r["code"], "libelle": r["libelle"], "count": int(r["n"])} for r in rows]


def acco_info() -> dict:
    """Métadonnées healthcheck : nb de lignes + plage de dates."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT COUNT(*) AS n, MIN(date_texte) AS dmin, MAX(date_texte) AS dmax FROM acco"
        ).fetchone()
    return {"total_rows": int(r["n"]), "date_min": r["dmin"], "date_max": r["dmax"]}


def acco_last_ingested_epoch() -> Optional[float]:
    """Epoch (s) du dernier upsert ACCO, ou None si table vide."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(ingested_at)) AS e FROM acco"
        ).fetchone()
    return float(r["e"]) if r and r["e"] is not None else None
