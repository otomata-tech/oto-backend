"""DDL du store (chaîne SQL unique appliquée par `_init.init_db`).

Extrait de l'ex-monolithe `db.py` (barreau 2). `CREATE TABLE IF NOT EXISTS` —
les évolutions de colonnes sur tables existantes vivent dans `_init.init_db`.
"""
from __future__ import annotations

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
    -- Discriminateur d'événement (ADR 0017, « un seul flux ») : 'mcp' = invocation
    -- d'outil MCP (le cas historique, défaut) ; 'rest' = appel /api/* ; 'connector'
    -- = échec/événement de résolution de credential ou de connexion connecteur.
    -- `tool` porte alors l'identifiant d'événement (route REST, nom de provider…).
    -- Les lectures du monitoring d'outils filtrent kind='mcp' pour rester iso.
    kind TEXT NOT NULL DEFAULT 'mcp',
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
    run_id TEXT,
    -- Org sous laquelle l'appel a été émis (seam current_org au moment du call,
    -- extension OTO-LOCALE) — scope EXACT du journal d'audit org (#67). NULL hors org.
    org_id BIGINT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at ON tool_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_sub ON tool_calls(sub);
CREATE INDEX IF NOT EXISTS idx_tool_calls_server_tool ON tool_calls(server, tool, created_at);
-- idx_tool_calls_run (run_id) ET idx_tool_calls_org (org_id) créés dans le bloc
-- ALTER de init_db, APRÈS leur ADD COLUMN : sur une table existante, CREATE TABLE
-- IF NOT EXISTS est un no-op donc ces colonnes n'existent pas encore ici (un index
-- les référençant dans _SCHEMA = crash UndefinedColumn au boot, vécu le 2026-06-25).

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

-- Runs / déroulés (ADR 0017, amende le « state-only » du barreau 1-2) : la
-- métadonnée SÉMANTIQUE d'un run (label, doctrine, outcome) est désormais PERSISTÉE
-- — la pile session-scopée de `doctrine_run.py` reste la source du run ACTIF (pour
-- stamper `tool_calls.run_id`), mais elle meurt avec la conversation. Cette table
-- donne la trace durable « l'user a déroulé telle doctrine, terminée tel outcome »
-- → anticipation du contexte injecté (#50 bloc C) + boucle d'usage dashboard. Le
-- DÉTAIL des appels d'un run reste corrélé via `tool_calls.run_id`. Table neuve →
-- indexes inline sûrs. `org_id` NULL hors org ; `outcome` NULL = run encore ouvert.
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    sub TEXT,
    org_id BIGINT,
    project_id BIGINT,                          -- projet actif GELÉ au start (ADR 0032 §5/§6, B3) ; NULL hors projet
    label TEXT NOT NULL,
    doctrine TEXT,                              -- slug de la doctrine nommée ; NULL = run ad-hoc
    outcome TEXT,                               -- done|abandoned|failed|blocked ; NULL = ouvert
    note TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_sub_org ON runs(sub, org_id, started_at DESC);
-- idx_runs_project est créé dans `_init` APRÈS l'ADD COLUMN project_id : sur une table
-- `runs` préexistante, CREATE TABLE IF NOT EXISTS est un no-op → la colonne n'existe
-- pas encore ici, un index la référençant dans _SCHEMA crashe au boot (vécu 2026-06-30,
-- même gotcha que idx_tool_calls_run/org ci-dessus).

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
    -- Onboarding = un PROJET « Découverte » (ADR 0032 §7 B5c) : id du projet d'accueil
    -- de l'user (porteur du brief de self-onboarding). NULL tant qu'il n'a pas été créé.
    discovery_project_id BIGINT,
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

-- Datastore = spine natif PG (ADR 0016). `user_datastores` = registre de
-- namespaces ; les rows vivent dans `datastore_rows` (JSONB). Propriété portée par
-- `(owner_type, owner_id)` (ADR 0030 : user/org/group). `sub` est une relique de
-- l'ancien modèle per-sub (nullable, DROP différé Phase H) ; `spreadsheet_id`/
-- `owner_email` sont des reliques Sheets (nullable, DROP différé).
-- ⚠️ Les INDEX sur owner_type/owner_id NE sont PAS créés ici : sur une base
-- existante, `CREATE TABLE IF NOT EXISTS` est un no-op et ces colonnes n'existent
-- pas encore quand `_SCHEMA` s'exécute (ajoutées plus bas par ALTER). Index +
-- contrainte d'unicité owner créés dans init_db APRÈS l'ALTER (couvre fresh ET existant).
CREATE TABLE IF NOT EXISTS user_datastores (
    id BIGSERIAL PRIMARY KEY,
    sub TEXT,
    owner_type TEXT NOT NULL DEFAULT 'user',
    owner_id TEXT,
    namespace TEXT NOT NULL,
    spreadsheet_id TEXT,
    owner_email TEXT,
    -- Mode TYPÉ optionnel (ADR 0032 §6 / 0029) : NULL = table libre (colonnes
    -- découvertes des rows) ; sinon un schéma déclaré
    -- {fields:[{key,label?,type?,role?}]} où role ∈ title|badge|metric|status|
    -- qualif|note pilote le rendu en fiches. Soft : pas de validation à l'écriture.
    schema JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
    brief_md TEXT NOT NULL DEFAULT '',
    created_by TEXT,
    is_template BOOLEAN NOT NULL DEFAULT FALSE,
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
-- connecteur name, memento workspace). `label` dénormalisé pour l'affichage ; `role`
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
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_docs_project ON docs(project_id);
CREATE INDEX IF NOT EXISTS idx_docs_parent ON docs(parent_id);

-- Historique de versions d'un Doc (ADR 0032 §3, B4c) : à chaque mise à jour, l'état
-- ANTÉRIEUR (title + body_md) est snapshotté ici avant écriture → chaîne de versions
-- consultable. `edited_by` = qui a posé la nouvelle version (a remplacé ce snapshot).
-- CASCADE sur la suppression du doc. Pas de revue/validation (auto-accept, cf. réunion).
CREATE TABLE IF NOT EXISTS doc_revisions (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL DEFAULT '',
    edited_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_revisions_doc ON doc_revisions(doc_id, created_at DESC);

-- Demandes de modification d'un Doc (ADR 0032 §3, gap #4b réunion 30/06) : un
-- utilisateur en LECTURE SEULE propose un nouveau contenu ; le propriétaire (write)
-- accepte (→ applique via update_doc, qui snapshotte la version courante) ou refuse.
-- `status` ∈ pending|accepted|rejected. CASCADE sur la suppression du doc.
CREATE TABLE IF NOT EXISTS doc_change_requests (
    id BIGSERIAL PRIMARY KEY,
    doc_id BIGINT NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    requested_by TEXT,
    proposed_title TEXT,
    proposed_body_md TEXT NOT NULL DEFAULT '',
    message TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_doc_change_requests_doc ON doc_change_requests(doc_id, status, created_at DESC);

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

-- Primitive de ressource possédée (ADR 0030). Partage cross-type deny-by-default :
-- une ressource est identifiée par (resource_type, resource_id) ; chaque ligne
-- accorde une permission à un principal (user/group/org). L'OWNER de la ressource
-- vit sur la ressource elle-même (colonnes owner_type/owner_id), PAS ici — derive
-- don't duplicate. `resource_id` = l'id STABLE de la ressource (ex.
-- user_datastores.id::text), pas un nom (survit au renommage). Résolu par le seam
-- `ownership.py` (plan contenu can_access = owner∪grants ; plan gouvernance
-- can_govern = owner∪escalade roles.py).
CREATE TABLE IF NOT EXISTS resource_grants (
    resource_type TEXT NOT NULL,                               -- 'datastore_namespace' | …
    resource_id TEXT NOT NULL,                                 -- id stable de la ressource
    principal_type TEXT NOT NULL CHECK (principal_type IN ('user', 'group', 'org')),
    principal_id TEXT NOT NULL,                                -- sub | group_id | org_id (texte)
    permission TEXT NOT NULL DEFAULT 'write' CHECK (permission IN ('read', 'write')),
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (resource_type, resource_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_grants_principal
    ON resource_grants(principal_type, principal_id, resource_type);

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
-- credentials propres (coffre `connector_credentials`, entity_type='org') et
-- des opérateurs (org_members). Source de vérité de l'appartenance = ces tables, résolues par
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

-- Instructions injectées AU NIVEAU PLATEFORME (#50, bloc A « secret sauce » +
-- bloc B « onboarding »). Singleton par `key` ('secret_sauce' | 'onboarding').
-- Éditable seulement par l'admin plateforme (inviolable par l'org — frontière
-- plateforme/org nette). Seedé au boot depuis les constantes de `instructions.py`
-- (INSERT ON CONFLICT DO NOTHING) → le code reste le défaut/fallback, la DB porte
-- l'override éditable. En CLAIR (prose, pas un credential).
CREATE TABLE IF NOT EXISTS platform_instructions (
    key TEXT PRIMARY KEY,                       -- 'secret_sauce' (bloc A) | 'onboarding' (bloc B)
    body_md TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by TEXT
);

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
