"""DDL du domaine « apps » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul.

**Une app est un écran bâti par un utilisateur sur la plateforme** (`oto_apps`,
`docs/apps.md`) : un manifeste (les tableaux, champs, agents qu'elle touche), du HTML, une
vignette. Elle appartient à un PROJET et en hérite l'accès, comme une page — mais ce
n'est PAS une page de mémoire : elle n'entre ni dans l'arbre des pages, ni dans la
recherche, ni dans la base de connaissance. Ses révisions sont à part
(`app_revisions`), comme celles des pages.
"""
from __future__ import annotations

APPS = """
-- Une app : un écran rangé dans un projet, dont il hérite l'accès.
CREATE TABLE IF NOT EXISTS apps (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    -- Ce que l'app peut toucher : tableaux et champs lus, écritures permises, agents
    -- lus ou lancés, dépôt de fichiers. Validé au serveur à chaque écriture.
    manifest JSONB NOT NULL DEFAULT '{}'::jsonb,
    html TEXT NOT NULL DEFAULT '',
    -- Image publiée sur le stockage média, ou couleur + emoji, et les réglages du
    -- dessin qui l'a produite. NULL = la tuile par défaut.
    thumbnail JSONB,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Le dernier auteur : celui dont les droits bornent l'app (auteur ∩ lecteur).
    updated_by TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    archived_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS apps_project_idx ON apps (project_id) WHERE archived_at IS NULL;

-- Chaque état ANTÉRIEUR d'une app, écrit avant chaque mise à jour : de quoi restaurer.
CREATE TABLE IF NOT EXISTS app_revisions (
    id BIGSERIAL PRIMARY KEY,
    app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    manifest JSONB NOT NULL,
    html TEXT NOT NULL,
    thumbnail JSONB,
    edited_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS app_revisions_app_idx ON app_revisions (app_id, id DESC);
"""

CONSENTEMENTS = """
-- Le consentement d'un LECTEUR à UN geste précis d'une app (écrire une valeur, ajouter
-- une ligne, lancer un agent), émis par le cadre parent hors de l'iframe, à usage
-- unique. Une fois brûlé, la ligne reste : c'est le journal des gestes d'app (qui,
-- quelle app, quoi, quand, sous quel geste d'écriture, quel travail lancé).
CREATE TABLE IF NOT EXISTS app_consents (
    id BIGSERIAL PRIMARY KEY,
    -- Empreinte SHA-256 du jeton : le jeton lui-même n'est jamais stocké.
    token_hash TEXT NOT NULL UNIQUE,
    app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    action TEXT NOT NULL,
    -- Empreinte des arguments du geste, calculée au serveur à l'émission ET à l'usage.
    fingerprint TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at TIMESTAMPTZ,
    -- Le geste d'écriture (`datastore_row_revisions.geste_id`) et le travail lancé.
    geste_id TEXT,
    job_id BIGINT
);
CREATE INDEX IF NOT EXISTS app_consents_app_idx ON app_consents (app_id, id DESC);
CREATE INDEX IF NOT EXISTS app_consents_sub_idx ON app_consents (sub);
"""

PROPRIETE = """
-- Une app a son PROPRIÉTAIRE, comme un tableau : une personne, une équipe ou une org
-- (`ownership`, kind `app`), et ses partages (`resource_grants`, resource_type `app`).
-- `context_org_id` : l'org où une app PERSO a été créée — là où vivent ses tableaux et
-- ses agents, là où elle se liste. `project_id` n'est plus lu : il ne donne plus
-- d'accès ; un projet RANGE une app (`app_projects`), sans rien ouvrir.
ALTER TABLE apps ADD COLUMN IF NOT EXISTS owner_type TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS owner_id TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS context_org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL;
ALTER TABLE apps ALTER COLUMN project_id DROP NOT NULL;
CREATE INDEX IF NOT EXISTS apps_owner_idx ON apps (owner_type, owner_id);
CREATE INDEX IF NOT EXISTS apps_context_org_idx ON apps (context_org_id);

-- Les projets où une app est RANGÉE (0..n) : elle s'affiche sur leur page. Ranger ne
-- donne aucun accès — ni à l'app, ni au projet.
CREATE TABLE IF NOT EXISTS app_projects (
    app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filed_by TEXT,
    filed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (app_id, project_id)
);
CREATE INDEX IF NOT EXISTS app_projects_project_idx ON app_projects (project_id);
"""

FICHIERS = """
-- Les fichiers qu'un LECTEUR dépose dans une app (un PDF, une image, un tableur) pour
-- qu'un agent les traite. Liés à l'app ET à qui les a déposés : seul lui — et les runs
-- qu'il lance, qui tournent en son nom — les lit. Les octets sont sur le stockage
-- média (`app-files/`), jamais en base.
CREATE TABLE IF NOT EXISTS app_files (
    id BIGSERIAL PRIMARY KEY,
    app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    s3_key TEXT NOT NULL,
    name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size BIGINT NOT NULL,
    -- Le texte extrait AU DÉPÔT (`file_extract`, borné) et son statut (`ok`, `empty`
    -- pour un scan sans texte, `unsupported` pour une image…) : un run le reçoit sans
    -- relire l'objet, et l'app peut dire tout de suite qu'un scan n'a pas de texte.
    text TEXT,
    text_status TEXT,
    -- `upload` : déposé par le lecteur dans l'app ; `agent` : rendu par un agent qu'il a
    -- lancé (`oto_apps op=put_file`), pour qu'il le récupère dans l'app.
    origin TEXT NOT NULL DEFAULT 'upload',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS app_files_app_sub_idx ON app_files (app_id, sub, id DESC);
CREATE INDEX IF NOT EXISTS app_files_sub_idx ON app_files (sub);
"""
