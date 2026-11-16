"""DDL du domaine « connectors » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# coffre des credentials (ADR 0002/0033)
CREDENTIALS = """
-- Coffre unique des credentials per-entité (membre, user, org, group, tenant,
-- plateforme) : clés API, sessions linkedin/crunchbase, OAuth Google multi-compte,
-- platform keys, et depuis le 2026-08-29 la clé partagée d'un TENANT (L-clés PR 1).
-- entity_id = sub (user) | orgs.id::text (org) | org_groups.id::text (group) |
-- tenants.slug (tenant) ;
-- toujours requêter (entity_type, entity_id) ENSEMBLE. Secret chiffré par
-- enveloppe AES-256-GCM dans `secret_enc`, NOT NULL : une ligne du coffre
-- DÉTIENT un secret, « détenir une clé » = la ligne existe (oto-backend#521 ;
-- base existante : révision `0009_coffre_secret_obligatoire`). Pas de colonne
-- plaintext ; déchiffrement JIT dans resolve_api_key. meta JSONB pour les
-- satellites (user_agent, scopes…).
CREATE TABLE IF NOT EXISTS connector_credentials (
    entity_type TEXT NOT NULL,            -- 'member' | 'user' | 'org' | 'group' | 'tenant' (L-clés) | 'platform' (ADR 0044 §F)
    entity_id   TEXT NOT NULL,            -- member:'org:sub' | user:sub | org/group:id::text | tenant:slug | platform:label
    connector   TEXT NOT NULL,            -- nom de connecteur (registre)
    account     TEXT NOT NULL DEFAULT '', -- discriminant multi-compte ('' = mono ; ex. email Google)
    secret_enc  TEXT NOT NULL,            -- enveloppe AES-256-GCM (#521)
    secret_kind TEXT NOT NULL DEFAULT 'api_key',
    meta        JSONB NOT NULL DEFAULT '{}',
    set_by      TEXT,
    set_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- ADR 0044 : l'entrée du coffre EST une instance de connecteur (config possédée).
    version     INTEGER NOT NULL DEFAULT 1,   -- verrou optimiste (B1) vs last-writer-wins
    share_down  JSONB NOT NULL DEFAULT '[]',  -- grantees des instances PLATFORM uniquement (§F) — le cran BYO « restreindre sous le niveau » est RETIRÉ (2026-07-08 : restreindre = poser l'instance au bon niveau)
    share_side  JSONB NOT NULL DEFAULT '[]',  -- EXTENSION : prêts NOMINATIFS à des pairs (liste de refs de principaux)
    share_mode  TEXT NOT NULL DEFAULT 'open', -- ADR 0044 §F : polarité du vide de share_down. 'open' = vide→sous-arbre (BYO) ; 'closed' = vide→personne (plateforme)
    share_services JSONB NOT NULL DEFAULT '{}', -- les SERVICES prêtés, par cible du share_side (`{"user:<sub>": ["google_ads"]}`) — compte Google ou Microsoft seulement (access/prets.PORTEURS_A_SERVICES)
    PRIMARY KEY (entity_type, entity_id, connector, account)
);
CREATE INDEX IF NOT EXISTS idx_conn_cred_entity ON connector_credentials(entity_type, entity_id);
"""

# schémas d'outils mis en cache
SCHEMAS = """
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
"""

# l'instance de connecteur comme OBJET (blueprint ADR 0053-D9, lot L6)
INSTANCES = """
-- L'INSTANCE de connecteur, OBJET (blueprint ADR 0053-D9, lot L6 — arbitrage **R1
-- prononcé par Alexis le 27/08 : « la table à côté »**).
--
-- **Pourquoi À CÔTÉ, et pas dans le coffre.** `credentials_store._aad` lie le
-- ciphertext aux QUATRE colonnes d'identité de SA ligne
-- (`connector_credentials:{entity_type}:{entity_id}:{connector}[:{account}]`), pas à
-- la table qui la porte. Une table posée à côté, la ligne du secret gardant ses
-- quatre colonnes, donne donc l'identifiant stable **sans un octet de
-- rechiffrement**. C'est ce qui a retiré R2 (le rechiffrement) de l'équation et
-- laissé R1 se décider sur ses vrais mérites : le modèle prévoit des instances SANS
-- secret — une instance `api` qui ne fige qu'une `base_url` et des champs (0057),
-- une sous-instance qui ne pose qu'une **détermination** (« le compte de Jane »,
-- 0053-D9-3). Un objet qui n'est plus un credential n'a rien à faire dans une table
-- qui l'est.
--
-- **Le lien vers le coffre est la CLÉ à quatre colonnes portée ici**
-- (`owner_type`/`owner_id`/`connector`/`account` = `entity_type`/`entity_id`/
-- `connector`/`account` du coffre), et c'est une clé étrangère **LOGIQUE** — pas de
-- FK déclarée, délibérément : le coffre n'a AUCUNE clé de substitution (sa PK EST ce
-- quadruplet), donc rien à quoi un `credential_id` pourrait pointer ; et une vraie
-- FK interdirait le jour même les instances sans secret que le modèle attend.
-- Le sens du pointeur suit la même lecture : un `instance_id` ajouté SUR le coffre
-- serait perdu à chaque renommage (`credentials_store.rename_account` = `_upsert`
-- d'une ligne neuve + `_delete` de l'ancienne, et la liste de colonnes de l'INSERT
-- ne le nommerait pas), donc réparable seulement en éditant les primitives
-- d'écriture du coffre — exactement ce que « le coffre ne bouge pas » exclut.
--
-- ⚠️ **Rien ne lit encore cette table** : ni la cascade (`access.walk_cascade`), ni
-- la résolution (`access.resolve_credential`), ni le coffre. L'existant est NOMMÉ,
-- pas déplacé — l'intention est gardée par `tests/test_connector_instances_l6.py`,
-- pas par de la vigilance, et le premier lecteur de résolution devra en retirer le
-- garde-fou dans son propre commit.
-- ─────────── Propriétés de connecteur SURCHARGEABLES (L6 pièce 2 c2) ───────────
-- Le patron du bloc d'instructions, appliqué aux propriétés de connecteur : la
-- CONSTANTE du registre est le défaut, une ligne ici est la SURCHARGE. Tranché par
-- Alexis le 2026-08-27 — « la base peut primer sur le défaut du code », pour qu'un
-- élargissement ne demande pas un déploiement.
--
-- Première `key` : `cardinality` (`mono` | `multi`) ; d'autres l'ont rejointe depuis
-- (`personal_key`, `access_level`…). La table est générique parce que la décision
-- l'est (« les propriétés de connecteur »), pas parce qu'on anticipait : une colonne
-- `cardinality` aurait fait une migration au deuxième besoin.
--
-- ⚠️ Ce qu'elle protège : **aucune lecture sur le chemin CHAUD d'un appel d'outil**.
-- La cardinalité est consultée jusqu'à quatre fois par appel, sur un serveur
-- MONO-LOOP, contre une base managée distante (docs/event-loop-perf.md) — d'où son
-- instantané en mémoire, chargé au boot et sur rechargement explicite. Même patron
-- que le registre d'émetteurs — et même conséquence, à dire dans la doc : le
-- rechargement est PAR PROCESS.
--
-- Une lecture FROIDE et HORS BOUCLE y satisfait, et n'a pas besoin de l'instantané :
-- `tools/planity_session.py` lit ses trois coordonnées à la construction d'un client
-- (au plus une fois par credential et par TTL de pool, via `asyncio.to_thread`), pas
-- à chaque appel. Elle y gagne ce que l'instantané ne donne pas : une écriture que
-- toutes les couleurs déployées voient sans `op=reload` chacune.
--
-- La question à se poser en ajoutant un lecteur n'est donc pas « boot ou appel ? »
-- mais **« à quelle fréquence, et dans la boucle ? »**. Deux politiques se lisent à
-- l'APPEL, hors boucle et mémorisées pour l'appel (`personal_key`, `access_level`) :
-- une restriction posée sur une couleur doit valoir sur toutes au prochain appel.
CREATE TABLE IF NOT EXISTS connector_settings (
    -- Le scope de la surcharge. `platform` vaut pour tout le monde ; `org` ne vaut que
    -- dans le contexte de CETTE org — la même lecture que partout ailleurs, l'org du
    -- REQUÉRANT, jamais son appartenance ; `group` ne vaut que sous CETTE équipe active
    -- (le niveau d'accès, `connectors.access_levels`, qui ne fait que restreindre l'org).
    scope_type TEXT NOT NULL,
    scope_id TEXT NOT NULL,          -- 'platform' (convention maison) | id d'org | id d'équipe
    connector TEXT NOT NULL,         -- le nom du PORTEUR du credential (délégation résolue)
    key TEXT NOT NULL,               -- 'cardinality' | 'personal_key' | 'access_level' | …
    value TEXT NOT NULL,             -- son sens appartient au module qui lit la clé
    set_by TEXT,
    set_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Contraintes NOMMÉES (docs/live-migrations.md).
    -- Domaine ouvert à `group` le 14/10/2026 : une base existante le reçoit par
    -- `_poser_domaine` au boot (`db/_init.py`), le CREATE TABLE ne touchant pas une table
    -- déjà là.
    CONSTRAINT connector_settings_scope_type_check
        CHECK (scope_type IN ('platform', 'org', 'group')),
    CONSTRAINT connector_settings_pkey
        PRIMARY KEY (scope_type, scope_id, connector, key)
);
-- Pas d'index de plus : la table se lit ENTIÈREMENT (une poignée de lignes), au boot
-- et au rechargement. Un index par `key` servirait une requête qui n'existe pas.

CREATE TABLE IF NOT EXISTS connector_instances (
    -- L'identifiant STABLE, et la raison d'être du lot : il survit à ce qui casse un
    -- ref composé (renommage de label, de compte), et il donne aux sous-instances un
    -- parent DÉSIGNABLE. Sa forme de fil est `inst:{id}` (`instance_refs`).
    id BIGSERIAL PRIMARY KEY,
    connector TEXT NOT NULL,
    -- Le propriétaire, au vocabulaire du coffre + `tenant`, **prévu et INERTE** :
    -- l'entité `tenant` du coffre est le lot L-clés, pas celui-ci. `member` est le
    -- régime courant depuis 0033 ; `user` ne survit que pour les mounts OAuth.
    owner_type TEXT NOT NULL,
    -- `entity_id` du coffre, à l'octet près : sub (user) | 'org:sub' (member) |
    -- id::text (org/group) | label de la clé (platform, ADR 0044 §F).
    owner_id TEXT NOT NULL,
    -- ⚠️ **NOT NULL DEFAULT '' — et surtout pas nullable.** `''` est le marqueur
    -- mono-compte DU COFFRE (le compte nommé de #409 est une valeur, son absence
    -- est la chaîne vide) : le reprendre tel quel est ce qui rend le quadruplet
    -- comparable colonne à colonne. Une colonne nullable rendrait de surcroît
    -- l'index unique ci-dessous AVEUGLE (`NULL` n'entre en conflit avec rien), donc
    -- muet exactement sur les lignes qu'il existe pour protéger.
    account TEXT NOT NULL DEFAULT '',
    -- Nom donné par le propriétaire. **Posée VIDE par le lot** : le nom affiché
    -- reste DÉRIVÉ de `meta.label` du coffre (`connectors_instances._instance_name`).
    -- Le backfiller ici ferait un second domicile pour une donnée que personne ne
    -- lit encore — la recopie se fera dans le lot qui déplace la lecture.
    label TEXT,
    -- La config NON SECRÈTE appariée (ADR 0038). **Posée VIDE, elle aussi** : la
    -- config publique vit aujourd'hui dans `connector_credentials.meta`, et les
    -- `config_fields` packés (ex. `data_center` zoho) vivent DANS le ciphertext —
    -- les sortir est un lot à part entière (il faut déchiffrer pour dépacker).
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- R9, tranché le 27/08 : **la visibilité est une propriété de l'INSTANCE**,
    -- dérivée de la chaîne (découvrable par les scopes sous son propriétaire dans la
    -- MÊME org, jamais cross-org), avec surcharge explicite par le propriétaire.
    -- La colonne porte la SURCHARGE ; `inherited` (le défaut) dit « laisse la
    -- dérivation décider ». **La dérivation n'est pas écrite** — c'est un lot.
    visibility TEXT NOT NULL DEFAULT 'inherited',
    -- Sous-instances (0053-D9-3) : une instance enfant AJOUTE des crans à son parent
    -- — « le LinkedIn de Jane » sous la clé Unipile plateforme. Rien n'en crée
    -- encore. ⚠️ Le jour où une sous-instance REDÉFINIT ce que `account` contient au
    -- lieu de s'y superposer, les lignes multi-comptes du coffre sont à rechiffrer
    -- (`account` EST une entrée d'AAD) : c'est le seul vrai candidat au
    -- rechiffrement, et il porte son propre lot (L6bis).
    parent_id BIGINT REFERENCES connector_instances(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Une instance s'ARCHIVE, elle ne se supprime pas — même doctrine que `grants`
    -- (0053-D7) : une consommation, un partage ou un binding qui la désignent
    -- doivent pouvoir la relire après son retrait.
    revoked_at TIMESTAMPTZ,
    -- POURQUOI elle a été archivée. Sans ce mot, un archivage est muet six mois plus
    -- tard : impossible de distinguer « l'utilisateur a retiré sa clé » (le cas normal)
    -- de « on a réparé une orpheline d'avant le lot » (un geste de maintenance, jamais
    -- une décision d'utilisateur). NULLABLE et sans CHECK : les valeurs sont posées par
    -- ce dépôt seul (`credential_removed`, `renamed_onto_existing`, `vault_row_missing`)
    -- et un vocabulaire fermé ici n'ajouterait qu'une migration au prochain motif.
    revoked_reason TEXT,
    -- Contraintes NOMMÉES (docs/live-migrations.md) : un futur DROP CONSTRAINT ne
    -- peut pas viser autre chose que ce qu'il croit viser.
    CONSTRAINT connector_instances_owner_type_check
        CHECK (owner_type IN ('platform', 'tenant', 'org', 'group', 'member', 'user')),
    CONSTRAINT connector_instances_visibility_check
        CHECK (visibility IN ('inherited', 'hidden', 'org'))
);
-- **Une instance vivante par ligne de coffre**, et c'est la base qui le tient, pas
-- le backfill : l'unicité porte sur le quadruplet du coffre. PARTIEL sur les
-- vivantes, pour qu'une instance archivée n'interdise pas à jamais d'en poser une
-- neuve sur la même ligne — le refus de RESSUSCITER, lui, est une garde du backfill
-- (`WHERE NOT EXISTS`, révoquées comprises), pas de l'index : même partage des rôles
-- qu'entre `idx_grants_grantee` et `db.grants.edge_exists` au lot L5.
-- Son préfixe `(owner_type, owner_id)` sert aussi les lectures par propriétaire — pas
-- de second index pour ça (derive don't duplicate).
CREATE UNIQUE INDEX IF NOT EXISTS idx_connector_instances_vault
    ON connector_instances(owner_type, owner_id, connector, account)
    WHERE revoked_at IS NULL;
-- Le JUMEAU NON PARTIEL de l'index unique, et il n'est pas un doublon : le backfill
-- de boot demande « existe-t-il une instance pour cette ligne de coffre, **archivée
-- comprise** ? » — c'est ce qui l'empêche de RESSUSCITER une instance retirée à la
-- main entre deux boots (la leçon de `db.grants.edge_exists` au lot L5). Un index
-- partiel ne peut pas servir une requête qui n'a pas son prédicat : PostgreSQL
-- retomberait sur un parcours complet, une fois par ligne du coffre. Même partage
-- des rôles qu'entre `idx_grants_grantee` (partiel, la résolution) et
-- `idx_grants_resource_grantee` (non partiel, le comptage) — et la même consigne :
-- ⚠️ ne PAS l'« harmoniser » avec l'unique en lui ajoutant `WHERE revoked_at IS NULL`.
CREATE INDEX IF NOT EXISTS idx_connector_instances_vault_all
    ON connector_instances(owner_type, owner_id, connector, account);
-- Descendre aux sous-instances d'un parent (0053-D9-3). Table et index naissent
-- ENSEMBLE ⟹ leur place est ici et pas dans `_init` (piège « CREATE INDEX d'une
-- NOUVELLE colonne », docs/live-migrations.md).
CREATE INDEX IF NOT EXISTS idx_connector_instances_parent
    ON connector_instances(parent_id);
"""

# Réception des téléphones révélés par Apollo — `apollo_receiver.py`
APOLLO_PHONE_REVEALS = """
-- Un reveal de téléphone Apollo commandé par oto, et ce qu'Apollo a POSTé en retour.
-- La ligne naît AVANT l'appel à Apollo (le POST peut arriver avant que l'appel ne
-- rende la main) ; `request_id` la rejoint dès qu'Apollo l'a rendu, `payload` quand
-- Apollo livre. Le jeton de l'URL de réception n'est JAMAIS stocké en clair : seule
-- son empreinte SHA-256 l'est, comme un secret.
-- Aucune clé étrangère vers `orgs` : une FK y prendrait un verrou à la création, pour
-- des lignes qui vivent trente jours (la rétention d'Apollo) et que la maintenance
-- purge (`oto-mcp maintenance apollo-phones`).
CREATE TABLE IF NOT EXISTS apollo_phone_reveals (
    token_hash TEXT PRIMARY KEY,
    org_id BIGINT,
    sub TEXT NOT NULL,
    -- La portée de la clé Apollo qui a PAYÉ le reveal (`niveau:entité:compte`) : seul
    -- qui résout cette même clé lit le résultat — l'accès au connecteur, pas l'org.
    cle_portee TEXT NOT NULL,
    -- L'entier signé 64 bits d'Apollo, en CHAÎNE (au-delà de la précision JSON).
    request_id TEXT,
    payload JSONB,
    -- Combien de POST sont arrivés : Apollo réessaie, seul le premier est gardé.
    deliveries INTEGER NOT NULL DEFAULT 0,
    received_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT NOW() + INTERVAL '30 days'
);
-- (portée de la clé, request_id) : la clé de lecture de `apollo_reveal_phone_result`.
-- ⚠️ Délibérément NON unique. L'unicité d'une livraison est tenue par le JETON (une
-- commande = une ligne) ; un index unique ici ferait échouer le POST d'Apollo en 500
-- le jour où un `request_id` reviendrait — et Apollo réessaie un 500 sans fin. Sur
-- une collision, la lecture prend la commande livrée la plus récente.
CREATE INDEX IF NOT EXISTS idx_apollo_phone_reveals_request
    ON apollo_phone_reveals(cle_portee, request_id)
    WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_apollo_phone_reveals_expires
    ON apollo_phone_reveals(expires_at);
"""

# le journal des prêts de compte (`oto_instance op=lend`, docs/partage-de-compte.md)
LOAN_EVENTS = """
-- Qui a prêté quel compte, à qui, quand — et qui l'a repris. Une ligne par pose ou
-- retrait EFFECTIF d'une cible du `share_side` (ADR 0044), append-only : rien ne la
-- modifie ni ne la supprime. Le compte est désigné par sa ligne du coffre (celle du
-- prêteur : `entity_type`, `entity_id`, `connector`, `account`), jamais par son secret ;
-- `target` est la cible telle que le `share_side` l'écrit : `user:<sub>`, `group:<id>`
-- ou `org:<id>`. Aucune clé étrangère : une FK vers `orgs` y prendrait un verrou.
CREATE TABLE IF NOT EXISTS connector_loan_events (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    actor_sub TEXT NOT NULL,
    org_id BIGINT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    connector TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT '',
    target TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('lend', 'revoke'))
);
-- La fusion de comptes repointe `actor_sub` d'un bloc : sans index, un balayage complet.
CREATE INDEX IF NOT EXISTS idx_loan_events_actor ON connector_loan_events (actor_sub);
-- La lecture par ligne du coffre (`pour_la_ligne`) et son suivi au rechiffrement.
CREATE INDEX IF NOT EXISTS idx_loan_events_ligne
    ON connector_loan_events (entity_type, entity_id, connector, account);
"""

# les règles d'accès des comptes de connecteur, et leur journal (oto-backend#1499, étape 3)
ACCOUNT_RULES = """
-- Une RÈGLE d'accès sur un compte de connecteur (une instance), pour UN sujet : une
-- personne, une équipe ou l'org entière — le modèle d'oto-core (`access_rules` sur un
-- compte), transposé (docs/partage-de-compte.md § Les règles de comptes).
-- ⚠️ Rien ne lit encore cette table : la résolution passe toujours par les prêts
-- (`share_side`, prêts Unipile). La bascule est un lot à part.
CREATE TABLE IF NOT EXISTS connector_account_rules (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    instance_id BIGINT NOT NULL REFERENCES connector_instances(id) ON DELETE CASCADE,
    -- Le sujet : exactement un des trois (contrainte nommée ci-dessous).
    subject_user TEXT REFERENCES users(sub) ON DELETE CASCADE,
    subject_group BIGINT REFERENCES org_groups(id) ON DELETE CASCADE,
    subject_org BOOLEAN NOT NULL DEFAULT FALSE,
    level TEXT NOT NULL,
    -- Propre au backend : un prêt de compte Google ou Microsoft ne prête qu'une LISTE
    -- de services (`access/prets.PORTEURS_A_SERVICES`). NULL = le compte entier.
    services TEXT[],
    -- `reprise` : recalculée depuis les anciens partages (`db/regles_de_comptes`) ;
    -- `manuelle` : posée par un geste. La reprise ne touche jamais une règle manuelle.
    origine TEXT NOT NULL DEFAULT 'manuelle',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT connector_account_rules_level_check
        CHECK (level IN ('none', 'read', 'write', 'sensitive', 'manage')),
    CONSTRAINT connector_account_rules_one_subject
        CHECK (num_nonnulls(subject_user, subject_group) + subject_org::int = 1),
    CONSTRAINT connector_account_rules_origine_check
        CHECK (origine IN ('manuelle', 'reprise')),
    -- Une règle par (compte, sujet). NULLS NOT DISTINCT : deux règles d'org sur le même
    -- compte (sujets NULL) seraient sinon toutes deux admises.
    CONSTRAINT connector_account_rules_instance_subject_key
        UNIQUE NULLS NOT DISTINCT (instance_id, subject_user, subject_group, subject_org)
);
CREATE INDEX IF NOT EXISTS idx_account_rules_org ON connector_account_rules (org_id);
-- La fusion de comptes repointe `subject_user` et `created_by` d'un bloc.
CREATE INDEX IF NOT EXISTS idx_account_rules_subject_user
    ON connector_account_rules (subject_user) WHERE subject_user IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_account_rules_created_by
    ON connector_account_rules (created_by) WHERE created_by IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_account_rules_subject_group
    ON connector_account_rules (subject_group) WHERE subject_group IS NOT NULL;

-- Le JOURNAL des partages : chaque règle posée, dont le niveau change, ou retirée. Écrit
-- par un DÉCLENCHEUR sur `connector_account_rules` (`db/regles_de_comptes`), dans la
-- transaction du geste ; append-only. Aucune clé vers les orgs, les équipes ni les
-- personnes : la ligne survit à son sujet. Elle part avec son compte.
CREATE TABLE IF NOT EXISTS connector_account_share_events (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    org_id BIGINT NOT NULL,
    instance_id BIGINT NOT NULL REFERENCES connector_instances(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT,              -- le sub ou l'id d'équipe ; NULL pour l'org
    level TEXT NOT NULL,
    previous_level TEXT,
    services TEXT[],
    actor_sub TEXT,               -- `oto.acteur` de la transaction, sinon NULL (geste hors serveur)
    CONSTRAINT connector_account_share_events_action_check
        CHECK (action IN ('set', 'removed')),
    CONSTRAINT connector_account_share_events_subject_kind_check
        CHECK (subject_kind IN ('user', 'group', 'org'))
);
CREATE INDEX IF NOT EXISTS idx_account_share_events_instance
    ON connector_account_share_events (instance_id, at DESC);
CREATE INDEX IF NOT EXISTS idx_account_share_events_org
    ON connector_account_share_events (org_id);
-- La fusion de comptes repointe `actor_sub` d'un bloc.
CREATE INDEX IF NOT EXISTS idx_account_share_events_actor
    ON connector_account_share_events (actor_sub) WHERE actor_sub IS NOT NULL;
"""
