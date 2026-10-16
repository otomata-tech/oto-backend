"""DDL du domaine « projects » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# projets, pages, révisions, liens
PROJECTS = """
-- Projet = couche d'organisation (modèle produit 2026-06-27). Conteneur de travail
-- POSSÉDÉ (owner_type/owner_id, ADR 0030) : nom + brief (doc d'entrée inline pour
-- l'instant ; le Doc arborescent + les liens vers tableaux/procédures/connecteurs/
-- bases = incréments suivants). Partage/transfert via resource_grants
-- (resource_type='project'). `archived_at` = soft-delete. Table fraîche → index posé
-- inline (les colonnes existent dès le CREATE, contrairement à user_datastores).
CREATE TABLE IF NOT EXISTS projects (
    id BIGSERIAL PRIMARY KEY,
    owner_type TEXT NOT NULL DEFAULT 'user',
    owner_id TEXT NOT NULL,
    name TEXT NOT NULL,
    -- Emoji facultatif : repère visuel du projet (listes, sidebar, en-tête).
    -- NULL = pas d'icône, l'UI retombe sur son rendu par défaut.
    icon TEXT,
    -- Dossier d'affichage facultatif : les listes regroupent les projets au même
    -- libellé. NULL = hors dossier. Aucune hiérarchie, aucune règle d'accès.
    folder TEXT,
    brief_md TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    is_template BOOLEAN NOT NULL DEFAULT FALSE,
    -- Publication d'un projet en endpoint MCP dédié `<mcp_slug>.mcp.oto.cx` (ADR 0032,
    -- amende #44). `mcp_access` ∈ {off (défaut, non publié) | anonymous (aucun login,
    -- toolset figé servi par la clé de l'org propriétaire) | org (JWT Logto, épingle
    -- l'org)}. `mcp_tools` = allowlist figée du preset (les seuls tools exposés sur le
    -- sous-domaine). `mcp_slug` UNIQUE = le label de sous-domaine (regex ^[a-z0-9-]{3,}$).
    mcp_slug TEXT UNIQUE,
    mcp_access TEXT NOT NULL DEFAULT 'off',
    mcp_tools TEXT[] NOT NULL DEFAULT '{}',
    -- Opt-in : exposer les tools `data_*` (datastore de l'org propriétaire) sur un
    -- endpoint `secret` sans login — l'endpoint agit alors sous l'autorité de l'org
    -- propriétaire. Défaut FALSE (datastore privé) ; jamais honoré en `anonymous`.
    mcp_expose_datastore BOOLEAN NOT NULL DEFAULT FALSE,
    -- Opt-in ADDITIONNEL, séparé de la lecture (#193) : autoriser l'ÉCRITURE du datastore
    -- (data_write/data_set_schema) sur l'endpoint partagé. Défaut FALSE (lecture seule).
    mcp_expose_datastore_write BOOLEAN NOT NULL DEFAULT FALSE,
    -- Opt-in : exposer les PAGES du projet (oto_doc en lecture) sur un endpoint
    -- `secret`. Séparé du datastore : les pages portent des notes internes.
    mcp_expose_docs BOOLEAN NOT NULL DEFAULT FALSE,
    -- Prose servie au DESTINATAIRE de l'endpoint publié (le client qui branche l'URL).
    -- Distincte de `brief_md`, qui est interne (gotchas, arbitrages, noms) : publier le
    -- brief tel quel serait une fuite. NULL = pas de guidage publié.
    mcp_instructions_md TEXT,
    -- Projet forké depuis un partage public (« Ajouter à mon Oto ») : pointeur vers la
    -- source, pour un import IDEMPOTENT par org (idx_projects_copied_from, créé dans `_init`
    -- après l'ADD COLUMN — même gotcha que is_template sur une table préexistante).
    copied_from BIGINT,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_type, owner_id);
-- ADR 0032 §7 (B5a) : un projet publié comme MODÈLE (template) est copiable (op=copy).
-- idx_projects_template est créé dans `_init` APRÈS l'ADD COLUMN is_template (même
-- gotcha que idx_runs_project : table projects préexistante → colonne absente ici).

-- Liens d'un Projet vers les entités qu'il regroupe (incrément 2). Pointeur TYPÉ,
-- pas un FK cross-store : `target_type` ∈ {tableau, procedure, connecteur, base} et
-- `target_ref` = l'id/slug/nom dans le store d'origine (datastore.id, doctrine slug,
-- connecteur name). `label` dénormalisé pour l'affichage ; `role`
-- = pourquoi cette entité est ici / ce qu'elle apporte au projet — le « pourquoi » vit
-- sur le LIEN, pas sur l'entité (ADR 0032 §2). Le caractère cross-projet n'est PAS
-- stocké : il est DÉRIVÉ (même (target_type, target_ref) dans ≥2 projets). `config` =
-- surcharge contextuelle PRÉFAITE de l'entité dans CE projet (ADR 0032 §4, B2) — pour
-- un `connecteur` : {identity_id?, instructions_md?} (quel compte + instructions de
-- surcharge en prose, lues par l'agent au chargement, jamais déclarées à la volée).
-- CASCADE sur la suppression du projet ; unicité (projet, type, ref) → lien idempotent.
CREATE TABLE IF NOT EXISTS project_links (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_type TEXT NOT NULL,
    target_ref TEXT NOT NULL,
    label TEXT,
    role TEXT,
    -- ADR 0035 (B2) : nom de slot BINDÉ par ce lien — vocabulaire DU PROJET (unicité
    -- (project_id, slot) via index partiel, posé dans le bloc ALTER d'init_db).
    slot TEXT,
    config JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(project_id, target_type, target_ref)
);
CREATE INDEX IF NOT EXISTS idx_project_links_project ON project_links(project_id);

-- Doc = page markdown d'un projet, en ARBRE (incrément 3). `parent_id` NULL = page
-- de 1er niveau sous le projet (le `brief_md` du projet reste la page d'entrée, pas
-- une ligne ici). `kind` ∈ {doc (humain), note (agent), source (import)}. CASCADE sur
-- la suppression du projet ET du parent (sous-arbre). Pas d'ownership propre : un Doc
-- hérite de l'accès de SON projet (ownership.can_access sur le projet).
CREATE TABLE IF NOT EXISTS docs (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id BIGINT REFERENCES docs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'doc',
    -- Lot 3 Ship 2 : chapô (sous-titre curé, fallback dérivé à la lecture) + ordre
    -- curé de la fratrie (entiers espacés ×16, réindexés atomiquement au move).
    description TEXT,
    position INTEGER,
    -- Partage public (gap #4a) : NULL = privé ; sinon un token aléatoire qui sert
    -- de lien public en lecture seule (/api/public/docs/{token}). Index unique créé
    -- dans `_init` après l'ADD COLUMN (jamais ici — table docs préexistante).
    public_token TEXT,
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- oto#274 : le compte de la dernière modification, posé avec `updated_at` (création,
    -- écriture ; NULL après un déplacement ou un écrivain inconnu). Base existante :
    -- révision Alembic `0013_pages_versions_regroupees`, qui le remplit.
    updated_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_docs_project ON docs(project_id);
CREATE INDEX IF NOT EXISTS idx_docs_parent ON docs(parent_id);

-- Historique de versions d'un Doc (ADR 0032 §3, B4c) : à chaque mise à jour, l'état
-- ANTÉRIEUR (title + body_md) est snapshotté ici avant écriture → chaîne de versions
-- consultable. `edited_by` = qui a posé la nouvelle version (a remplacé ce snapshot),
-- `face` = par quelle porte ('mcp' | 'rest' ; NULL = hors des deux, ou antérieur à
-- oto#274). Une rafale d'enregistrements du dashboard (même compte, face REST, moins de
-- 5 min depuis l'instantané qui l'a ouverte) n'en ajoute pas (`db.projects.update_doc`).
-- CASCADE sur la suppression du doc. Pas de revue/validation (auto-accept, cf. réunion).
CREATE TABLE IF NOT EXISTS doc_revisions (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL DEFAULT '',
    edited_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    face TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_revisions_doc ON doc_revisions(doc_id, created_at DESC);

-- Demandes de modification d'un Doc (ADR 0032 §3, gap #4b réunion 30/06) : un
-- utilisateur en LECTURE SEULE propose un nouveau contenu ; le propriétaire (write)
-- accepte (→ applique via update_doc, qui snapshotte la version courante) ou refuse.
-- `status` ∈ pending|accepted|rejected. CASCADE sur la suppression du doc.
CREATE TABLE IF NOT EXISTS doc_change_requests (
    id BIGSERIAL PRIMARY KEY,
    -- Ship 3 : `doc_id` NULL = proposition de CRÉATION (la page n'existe pas encore) ;
    -- `project_id` porte alors le projet cible + `proposed_parent_id`/`proposed_kind`.
    doc_id BIGINT REFERENCES docs(id) ON DELETE CASCADE,
    project_id BIGINT REFERENCES projects(id) ON DELETE CASCADE,
    proposed_parent_id BIGINT REFERENCES docs(id) ON DELETE SET NULL,
    proposed_kind TEXT,
    requested_by TEXT,
    proposed_title TEXT,
    proposed_body_md TEXT NOT NULL DEFAULT '',
    message TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT dcr_target CHECK (doc_id IS NOT NULL OR project_id IS NOT NULL)
);

-- Backlinks [[…]] (lot 3 Ship 4) — graphe LÉGER de pages qui se citent. Table
-- DÉRIVÉE (reconstructible par re-parse des bodies) : `from_doc` cite `to_doc`.
-- Résolue contre le projet courant + la KB de l'org (précédence projet > KB) ;
-- purgée en cascade au delete d'un des deux docs.
CREATE TABLE IF NOT EXISTS doc_links (
    from_doc BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    to_doc   BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    PRIMARY KEY (from_doc, to_doc)
);
CREATE INDEX IF NOT EXISTS idx_doc_links_to ON doc_links(to_doc);
"""

# activité et fichiers d'un projet
# ⚠️ Ce fragment s'OUVRE sur l'index de `doc_change_requests`, dont la table est
# déclarée ~90 lignes plus haut (dans PROJECTS) : ce n'est pas une faute de frappe,
# c'est la position exacte qu'il occupait dans le littéral d'origine, entre les
# embeddings et `project_activity`. La découpe par domaine était un déplacement PUR
# — le remonter auprès de sa table changerait la chaîne servie, donc le DDL figé
# (`tests/schema_gele/`, `tests/test_schema_assembly_frozen.py`). Cf. docs/migrations-versionnees.md §2.7.
PROJECT_FILES = """CREATE INDEX IF NOT EXISTS idx_doc_change_requests_doc ON doc_change_requests(doc_id, status, created_at DESC);

-- Journal d'activité d'un projet (incrément 5) : qui a fait quoi, quand. Alimenté
-- best-effort par les capacités projet/doc sur les mutations. `action` = verbe court
-- (project.create, doc.update…), `detail` = libellé libre.
CREATE TABLE IF NOT EXISTS project_activity (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sub TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_activity_project ON project_activity(project_id, created_at DESC);

-- Fichiers bruts d'un projet — carte « Autre document » (ADR 0032 §3). PDF/HTML/etc.
-- stockés en Object Storage DURABLE+privé (media_store.upload_object → `s3_key`
-- persistée, presigned à la lecture). `title`/`description` = la coquille légère
-- décrite en réunion (consommable par l'agent) ; `summary` = résumé IA, rempli plus
-- tard. CASCADE sur la suppression du projet ; pas d'ownership propre (hérite du projet).
CREATE TABLE IF NOT EXISTS project_files (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    s3_key TEXT NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT,
    size_bytes BIGINT,
    title TEXT,
    description TEXT,
    summary TEXT,
    public BOOLEAN NOT NULL DEFAULT FALSE,    -- partagé publiquement (ACL public-read, ADR 0032 §3)
    public_url TEXT,                          -- URL publique permanente quand public ; NULL sinon
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_project_files_project ON project_files(project_id);

-- Texte EXTRAIT d'un fichier déposé (#298) — pour qu'on le retrouve par ce qu'il
-- CONTIENT, et plus seulement par son nom. Jusqu'ici un PDF de trente pages était,
-- du point de vue de la recherche, un nom de fichier : mal nommé, introuvable.
--
-- ⚠️ **Table DÉDIÉE, et pas trois colonnes de plus sur `project_files`.** Celle-ci
-- est une table de MÉTADONNÉES, lue à chaque listing de projet ; le texte d'un
-- document pèse des dizaines de kilo-octets et n'est lu que par la recherche. Les
-- coller ensemble ferait payer le poids du contenu à toutes les lectures de
-- catalogue (0063-D3, garde-fou 1 : chaque colonne se paie autant de fois qu'on lit
-- la ligne). ⚠️ Et surtout **PAS dans `project_files.summary`**, colonne morte : un
-- texte extrait n'est pas un résumé, et le nom mentirait — le défaut qu'on passe la
-- semaine à corriger ailleurs.
--
-- **La clé est le fichier lui-même**, simple et empruntée telle quelle. C'est
-- délibéré et c'est la leçon du lot M4 : une clé COMPOSITE (là-bas `(ns_id, row_id)`)
-- casse le patron de conversion des lots précédents et se découvre trop tard. Le
-- texte extrait rejoindra un jour le modèle de nœuds ; avec un id de séquence, la
-- transposition est le `legacy_id::bigint` qui marche déjà trois fois.
--
-- **L'absence de ligne EST la file de travail** : un fichier sans ligne ici est un
-- fichier à extraire. Pas de drapeau `dirty` sur `project_files`, donc pas d'état à
-- réconcilier — le manque se lit par une jointure, et un fichier supprimé emporte sa
-- ligne (CASCADE) sans laisser de tâche fantôme.
CREATE TABLE IF NOT EXISTS project_file_texts (
    file_id BIGINT PRIMARY KEY REFERENCES project_files(id) ON DELETE CASCADE,
    -- ok | unsupported | encrypted | empty | too_large | rejected_dtd | failed.
    -- ⚠️ Le statut est TERMINAL sauf `failed` : un fichier chiffré ou d'un format
    -- non supporté ne changera pas d'avis au prochain passage du worker. Le stocker
    -- (plutôt qu'un booléen) est ce qui permet à l'interface de dire « format non
    -- supporté » au lieu de « en cours », et à un futur lot d'OCR de retrouver
    -- exactement la population `empty` d'une requête.
    status TEXT NOT NULL,
    -- Vide sauf sur `ok`. Le texte ENTIER (borné à l'extraction), pas seulement ses
    -- vecteurs : les extraits de résultat en ont besoin, et le garder évite de
    -- retélécharger le fichier à chaque affichage.
    extracted_text TEXT NOT NULL DEFAULT '',
    pages INTEGER,
    -- De quoi comprendre sans rouvrir le fichier (« tronqué à … », le type d'erreur).
    -- Jamais un extrait du contenu : un message de lib peut recracher des octets du
    -- document, qui n'ont rien à faire dans un champ de diagnostic.
    detail TEXT NOT NULL DEFAULT '',
    -- Nombre de tentatives : seul `failed` se retente, et pas indéfiniment.
    attempts INTEGER NOT NULL DEFAULT 1,
    extracted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Un seul index, et il sert la FILE : « les fichiers encore à traiter ». Partiel,
-- donc il ne porte QUE la population reprenable — les lignes terminales (l'immense
-- majorité en régime établi) n'y entrent jamais. `status` est une colonne, le
-- prédicat est immuable.
-- ⚠️ Pas d'index de recherche ici : la FTS des fichiers se décide avec la requête
-- qu'elle sert (barreau suivant), jamais « par symétrie » avec les autres sources.
CREATE INDEX IF NOT EXISTS idx_project_file_texts_retry
    ON project_file_texts(file_id) WHERE status = 'failed';
"""
