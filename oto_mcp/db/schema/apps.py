"""DDL du domaine « apps » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul.

**Une app est un écran bâti par un utilisateur sur la plateforme** (`oto_apps`,
`docs/apps.md`) : un manifeste (les tableaux, champs, agents qu'elle touche), du HTML, une
vignette. Elle appartient à un PROJET et en hérite l'accès, comme une page — mais ce
n'est PAS une page de mémoire : elle n'entre ni dans l'arbre des pages, ni dans la
base de connaissance (`oto_search` la trouve par son titre seulement). Ses révisions sont à part
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
-- `owner_id` EN TÊTE : la fusion de comptes repointe la colonne d'un bloc
-- (`db/users._SUB_COLUMNS`, `owner_id = ANY(…)`), l'index ci-dessus ne lui sert pas.
CREATE INDEX IF NOT EXISTS apps_owner_id_idx ON apps (owner_id);
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
-- Colonne-compte repointée d'un bloc par la fusion de comptes : indexée.
CREATE INDEX IF NOT EXISTS app_projects_filed_by_idx ON app_projects (filed_by);
"""

FICHIERS = """
-- Les fichiers qu'un LECTEUR dépose dans une app (un PDF, un tableur, un document
-- Word) pour qu'un agent les traite. Liés à l'app ET à qui les a déposés : seul lui — et
-- les runs qu'il lance, qui tournent en son nom — les lit. Les octets sont sur le
-- stockage média (`app-files/`), jamais en base.
CREATE TABLE IF NOT EXISTS app_files (
    id BIGSERIAL PRIMARY KEY,
    -- `SET NULL`, pas `CASCADE` : une app supprimée laisse ses lignes SANS app, que la
    -- purge de maintenance retire (l'objet du stockage d'abord) — jamais un objet
    -- orphelin que plus aucune ligne ne désigne. Une ligne sans app n'est lue par
    -- personne : toutes les lectures portent `app_id = …`.
    app_id BIGINT REFERENCES apps(id) ON DELETE SET NULL,
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
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS app_files_app_sub_idx ON app_files (app_id, sub, id DESC);
CREATE INDEX IF NOT EXISTS app_files_sub_idx ON app_files (sub);
"""

FICHIERS_ORIGINE = """
-- D'où vient un fichier d'app : `upload` = déposé par le lecteur dans l'app ; `agent` =
-- rendu par un agent qu'il a lancé (`oto_apps op=put_file`), pour qu'il le récupère dans
-- l'app. Colonne AJOUTÉE (révision `20261030_1400_app_files_origin`) : le fragment
-- `FICHIERS` a déjà été joué là où elle manque.
ALTER TABLE app_files ADD COLUMN IF NOT EXISTS origin TEXT NOT NULL DEFAULT 'upload';
"""

VERSIONS = """
-- Brouillon et versions publiées d'une app (`docs/apps.md` § Brouillon et publication).
-- `drafts_enabled` : les modifications du manifeste et du HTML vont au BROUILLON, que
-- seuls ses éditeurs ouvrent ; `oto_apps op=publish` le rend vivant pour tous. Faux par
-- défaut : une app d'avant reste publiée à chaque écriture, comme avant.
ALTER TABLE apps ADD COLUMN IF NOT EXISTS drafts_enabled BOOLEAN NOT NULL DEFAULT FALSE;
-- L'état vivant est-il une version PUBLIÉE (`op=publish`, `op=restore`), et sa note.
ALTER TABLE apps ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS version_note TEXT;
-- Le brouillon, sur la ligne de l'app : publier verrouille une seule ligne. NULL = aucun.
-- Le titre et la vignette n'en ont pas : ce sont la carte, écrite en direct.
ALTER TABLE apps ADD COLUMN IF NOT EXISTS draft_manifest JSONB;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS draft_html TEXT;
-- L'auteur du brouillon : celui dont les droits bornent son aperçu (auteur ∩ lecteur).
ALTER TABLE apps ADD COLUMN IF NOT EXISTS draft_by TEXT;
ALTER TABLE apps ADD COLUMN IF NOT EXISTS draft_at TIMESTAMPTZ;
-- Une révision garde si l'état remplacé était une version publiée, et sa note : les
-- versions publiées échappent à la purge des `MAX_REVISIONS` (bornées à part).
ALTER TABLE app_revisions ADD COLUMN IF NOT EXISTS published_at TIMESTAMPTZ;
ALTER TABLE app_revisions ADD COLUMN IF NOT EXISTS note TEXT;
-- Quand l'état gardé a été ÉCRIT (son `updated_at`) — `created_at` dit quand il a été
-- remplacé. NULL pour les révisions d'avant cette colonne.
ALTER TABLE app_revisions ADD COLUMN IF NOT EXISTS saved_at TIMESTAMPTZ;
"""

ROLES = """
-- Le rôle d'APP d'un partage (`docs/apps.md` § Rôles d'une app) : la clé d'un des rôles
-- que le manifeste déclare (`manifest.roles`), posée par qui gouverne l'app
-- (`oto_apps op=set_role`). Il RESSERRE ce que le manifeste ouvre à ce lecteur
-- (tableaux, champs, valeurs, ajouts, agents, fichiers) — jamais plus. NULL = le rôle
-- par défaut du manifeste (`default_role`). Sur le partage même : il part avec lui
-- (retrait, échéance — un partage échu puis repartagé repart du rôle par défaut, comme
-- un partage neuf), suit la fusion de comptes et l'export comme lui. Colonne AJOUTÉE
-- (révision `20261127_1200_app_roles`) : le fragment `grants.GRANTS` a déjà été joué.
ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS app_role TEXT;
-- L'IDENTITÉ d'un partage (`docs/apps.md` § Accès par l'app) : un numéro jamais repris,
-- tiré à sa pose (`db.grant_resource`). Changer son rôle ou son échéance tant qu'il est
-- vivant le garde ; le reposer après son échéance, ou après un retrait, en tire un
-- neuf. Ce qui ne vaut que sous CE partage (`app_table_access.share_id`) ne revit donc
-- jamais sous un autre. NULL = un partage d'avant la colonne, numéroté à sa prochaine
-- pose ou au premier accès par l'app ouvert sous lui. Nullable sans défaut : catalogue
-- seul (un `DEFAULT nextval` réécrirait la table).
CREATE SEQUENCE IF NOT EXISTS resource_grants_share_id_seq;
ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS share_id BIGINT;
-- Le PÉRIMÈTRE DE LIGNES d'un partage d'app (`docs/apps.md` § Périmètre de lignes) :
-- `{"<numéro du tableau>": {"<champ>": <valeur>}}`, posé par qui gouverne l'app ET
-- peut partager chaque tableau nommé (`oto_apps op=set_scope`). Dans l'appel de l'app,
-- ce lecteur ne voit, n'écrit et n'ajoute que les lignes dont chaque champ porte sa
-- valeur ; c'est aussi la valeur que lit un rôle qui déclare `rows: {…: "$share"}`.
-- NULL = aucun périmètre posé. Rangé par NUMÉRO de tableau, jamais par le nom que lui
-- donne le manifeste : un renommage ne l'éteint pas. Part avec le partage, suit la
-- fusion de comptes comme lui (UPDATE de `principal_id`).
ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS app_scope JSONB;
"""

ACCES_PAR_L_APP = """
-- Un tableau ouvert à quelqu'un À TRAVERS UNE APP SEULEMENT (`docs/apps.md` § Accès par
-- l'app) : il le lit — et l'écrit, `permission = 'write'`, par les seuls gestes de
-- l'app — dans l'appel de CETTE app, borné par son manifeste et son rôle d'app. Nulle
-- part ailleurs : aucune lecture de `resource_grants` ne voit cette table, ni les
-- outils `data_*`, ni la face REST des tableaux, ni l'écran Tableaux. Posé par qui
-- gouverne l'app ET le tableau (`oto_apps op=grant_through_app`). Ne vaut que tant que
-- le même principal tient, VIVANT, le partage de l'app sous lequel il a été ouvert
-- (`share_id` = `resource_grants.share_id`) : il part avec lui, et ne revient pas avec
-- un partage reposé (après retrait, échéance, transfert…) — il se rouvre.
CREATE TABLE IF NOT EXISTS app_table_access (
    app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'org')),
    principal_id TEXT NOT NULL,
    permission TEXT NOT NULL CHECK (permission IN ('read', 'write')),
    share_id BIGINT NOT NULL,
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (app_id, ns_id, principal_type, principal_id)
);
-- La fusion de comptes repointe `principal_id` et `granted_by` d'un bloc : indexés, sinon
-- un balayage complet de la table (garde `tests/test_repointer_residus_alias.py`).
CREATE INDEX IF NOT EXISTS app_table_access_principal_idx ON app_table_access (principal_id);
CREATE INDEX IF NOT EXISTS app_table_access_granted_by_idx ON app_table_access (granted_by);
"""

LECTURES = """
-- Le JOURNAL DES LECTURES d'app (`docs/apps.md` § Journal des lectures) : qui a lu quel
-- tableau par quelle app, quel jour — des COMPTEURS, pas une ligne par lecture (une app
-- ouvre son écran par huit à quinze lectures et se rafraîchit). Écrit APRÈS la lecture,
-- hors de son emprunt au pool (`capabilities/apps_appel._call_journalise`). Lu par qui
-- gouverne l'app ou le tableau (`oto_apps op=reads`). Purgé au-delà de 180 jours par
-- `oto-mcp maintenance app-reads`.
-- `access` : par où le lecteur atteignait le tableau — `own` (son propre partage),
-- `app` (À TRAVERS l'app seulement, `app_table_access`), `run` (un agent lancé depuis
-- l'app, par cet accès : `row_reads` y compte les ouvertures du tableau, `rows_served`
-- reste 0, le run lisant par les outils de tableau).
-- `day` : le jour UTC.
CREATE TABLE IF NOT EXISTS app_reads (
    app_id BIGINT NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    day DATE NOT NULL,
    ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
    sub TEXT NOT NULL,
    access TEXT NOT NULL CHECK (access IN ('own', 'app', 'run')),
    row_reads INTEGER NOT NULL DEFAULT 0,
    aggregate_reads INTEGER NOT NULL DEFAULT 0,
    rows_served BIGINT NOT NULL DEFAULT 0,
    first_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (app_id, day, ns_id, sub, access)
);
-- Le gérant d'un tableau lit par lui ; la purge, par le jour.
CREATE INDEX IF NOT EXISTS app_reads_ns_idx ON app_reads (ns_id, day);
CREATE INDEX IF NOT EXISTS app_reads_day_idx ON app_reads (day);
-- La fusion de comptes repointe `sub` d'un bloc.
CREATE INDEX IF NOT EXISTS app_reads_sub_idx ON app_reads (sub);
"""
