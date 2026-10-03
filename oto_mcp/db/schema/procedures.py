"""DDL du domaine « procédures » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# Procédures d'org, révisions, bibliothèque publique.
#
# ⚠️ Le DDL ci-dessous est FIGÉ au lot A de #519 : la base est PARTAGÉE
# prod/preprod, donc `doctrine_library` et ses index gardent leur nom servi.
# Alias de compatibilité — renommage additif (vue puis bascule) au lot B.
PROCEDURES = """
-- Procédures (doctrines/skills) — table UNIQUE, possédée par un SCOPE (chantier
-- procédures, cadrage 10/07) : `owner_type/owner_id` ('org' = procédure d'org,
-- 'group' = procédure d'équipe à la fusion B2 d'org_group_instructions ; `org_id`
-- reste l'org PARENTE dans les deux cas — dénormalisé, FK + prédicats). Chaque
-- procédure est identifiée par `slug` dans son scope ; l'unicité vivante =
-- (owner_type, owner_id, slug) (index unique posé par _init ; la PK legacy
-- (org_id, slug) tombe en B2). En CLAIR (prose, hors coffre). `version` est
-- incrémenté à chaque écriture, qui archive un snapshot dans la table sœur.
-- (Le readme `claude_md` vit dans `guides`, ADR 0042 — plus une ligne d'ici.)
CREATE TABLE IF NOT EXISTS org_instructions (
    -- NULL pour une procédure PERSONNELLE (ADR 0068) : une personne n'a pas
    -- d'org parente, et y ranger son org de CONTEXTE ferait cascader la
    -- suppression de l'org sur un contenu qui ne lui appartient pas.
    org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
    owner_type TEXT NOT NULL DEFAULT 'org',
    owner_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    -- ADR 0035 : slots = entités requises déclarées ({name, type, description?,
    -- connector?}), référencées par nom dans la prose (<slot:name>). Le binding
    -- nom→instance vit dans le projet (project_links), jamais ici.
    slots JSONB NOT NULL DEFAULT '[]'::jsonb,
    version INTEGER NOT NULL DEFAULT 1,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- PK NOMMÉE : le DROP de la PK legacy (org_id, slug) dans _init cible
    -- `org_instructions_pkey` — un nom distinct protège l'install fraîche.
    CONSTRAINT org_instructions_owner_pkey PRIMARY KEY (owner_type, owner_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_org_instructions_org ON org_instructions(org_id);

-- Historique : un snapshot par version posée (revert + audit). Append-only.
-- Porte le même scope owner que la table vivante (unicité vivante :
-- (owner_type, owner_id, slug, version), index unique posé par _init).
CREATE TABLE IF NOT EXISTS org_instruction_revisions (
    -- NULL pour une procédure PERSONNELLE (ADR 0068) : une personne n'a pas
    -- d'org parente, et y ranger son org de CONTEXTE ferait cascader la
    -- suppression de l'org sur un contenu qui ne lui appartient pas.
    org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
    owner_type TEXT NOT NULL DEFAULT 'org',
    owner_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    body_md TEXT NOT NULL,
    slots JSONB NOT NULL DEFAULT '[]'::jsonb,
    set_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT org_instruction_revisions_owner_pkey PRIMARY KEY (owner_type, owner_id, slug, version)
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
    slots JSONB NOT NULL DEFAULT '[]'::jsonb, -- ADR 0035 : voyage avec la doctrine au publish/fork
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
"""


# Partage d'UNE procédure par lien (`/p/<token>`) — « See who's reading ».
#
# Une procédure d'org se publie par un jeton aléatoire : sans compte on lit sa
# VITRINE (titre, description, connecteurs, forme du graphe — jamais le corps,
# cf. le retrait des vitrines anonymes de oto#84), connecté on lit le corps et
# on est noté LECTEUR. Le corps servi est toujours la version COURANTE de la
# procédure : le partage pointe la procédure vivante, il ne la recopie pas.
#
# ⚠️ `instruction_id` est un lien LOGIQUE, sans clé étrangère : la colonne
# `org_instructions.id` naît dans `_init` (ALTER), APRÈS cet assemblage — une FK
# échouerait sur une base vierge. Une procédure supprimée laisse un partage
# orphelin, que toutes les lectures traitent comme introuvable (404).
#
# `preview_shape` = la FORME du graphe (déclencheur, puis par étape son genre et
# ses connecteurs), calculée par le front du propriétaire, validée à l'écriture
# par un schéma FERMÉ : énumérations et slugs seulement, aucun texte libre ne
# peut y entrer. `shape_version` = la version de procédure dont elle vient ; la
# vitrine ne la sert que si elle égale la version courante.
PROCESS_SHARES = """
CREATE TABLE IF NOT EXISTS process_shares (
    id BIGSERIAL PRIMARY KEY,
    instruction_id BIGINT NOT NULL,
    org_id BIGINT REFERENCES orgs(id) ON DELETE CASCADE,
    token TEXT NOT NULL UNIQUE,
    show_readers BOOLEAN NOT NULL DEFAULT TRUE,
    preview_shape JSONB,
    shape_version INTEGER,
    preview_views JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    revoked_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_process_shares_actif
    ON process_shares(instruction_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_process_shares_instruction
    ON process_shares(instruction_id);
CREATE INDEX IF NOT EXISTS idx_process_shares_created_by
    ON process_shares(created_by);

CREATE TABLE IF NOT EXISTS process_share_readers (
    share_id BIGINT NOT NULL REFERENCES process_shares(id) ON DELETE CASCADE,
    reader_sub TEXT NOT NULL,
    first_read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_read_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reads INTEGER NOT NULL DEFAULT 1,
    recorded BOOLEAN NOT NULL,
    copied_at TIMESTAMPTZ,
    copied_instruction_id BIGINT,
    digested_at TIMESTAMPTZ,
    PRIMARY KEY (share_id, reader_sub)
);
CREATE INDEX IF NOT EXISTS idx_process_share_readers_sub
    ON process_share_readers(reader_sub);

CREATE TABLE IF NOT EXISTS process_readers_digest_optouts (
    sub TEXT PRIMARY KEY,
    source TEXT NOT NULL DEFAULT 'link',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""
