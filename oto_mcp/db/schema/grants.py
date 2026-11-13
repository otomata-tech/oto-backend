"""DDL du domaine « grants » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# partages de ressources possédées
GRANTS = """
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
    -- ADR 0048 : le grant porte un RÔLE (lecteur/éditeur/gérant). `permission` (read/write)
    -- reste la projection CONTENU appariée (viewer→read, editor/manager→write) — inchangée
    -- pour tout le SQL du plan contenu (max(g.permission)/g.permission='write') ; `role`
    -- porte en plus la GOUVERNANCE grantable (`manager` → can_govern). Source de vérité = role.
    permission TEXT NOT NULL DEFAULT 'write' CHECK (permission IN ('read', 'write')),
    -- `contributor` (13/11/2026) : une APP seulement — la voir et cliquer ses boutons
    -- (`apps_appel`), sans la modifier. Projection CONTENU : read.
    role TEXT NOT NULL DEFAULT 'editor' CHECK (role IN ('viewer', 'contributor', 'editor', 'manager')),
    granted_by TEXT,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- otomata-tech/oto#39 : l'échéance du partage. NULL = sans échéance. Échu, le
    -- partage ne donne plus rien : toute lecture de cette table filtre
    -- `db._partage_vivant` (garde : tests/test_partages_echeance_39.py). La ligne
    -- reste, visible du propriétaire dans `op=get`, comme un jeton expiré.
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (resource_type, resource_id, principal_type, principal_id)
);
CREATE INDEX IF NOT EXISTS idx_resource_grants_principal
    ON resource_grants(principal_type, principal_id, resource_type);

-- ─────────── L'accès par CHAÎNE de grants (blueprint ADR 0053, lot L4) ───────────
-- ⚠️ NE PAS CONFONDRE avec `resource_grants` juste au-dessus. Celle-là partage un
-- CONTENU (0030/0048 : un tableau, un projet, une page) à un principal, à PLAT — une
-- ligne, un droit. `grants` porte un autre plan : le droit d'UTILISER une ressource
-- opérante (v1 : une instance de connecteur), par une chaîne MATÉRIALISÉE qui
-- descend de la plateforme jusqu'à l'utilisateur en ne faisant que resserrer
-- (0053-D4/D5 : « min des contraintes le long de la chaîne »). Les deux coexistent
-- tant que la bascule (L5+) n'a pas eu lieu.
--
-- ⚠️ **Écrites par rien, lues par rien — et c'est le lot.** Le socle arrive AVANT
-- son premier usage : c'est ce qui rendra la bascule faisable connecteur par
-- connecteur (la clé plateforme d'abord) plutôt qu'en un grand soir. L'intention
-- est gardée par `tests/test_grants_l4_migration.py`, pas par de la vigilance : le
-- premier lecteur devra retirer ce test dans son propre commit.
CREATE TABLE IF NOT EXISTS grants (
    id BIGSERIAL PRIMARY KEY,
    -- La ressource accordée. `connector_instance` en v1 ; le champ EST le point
    -- d'extension prévu par 0053 §4 (« extensible : 'capability', … »).
    resource_kind TEXT NOT NULL DEFAULT 'connector_instance',
    -- ⚠️ ÉCART ASSUMÉ avec la forme du banc, qui portait `resource_id BIGINT` (son
    -- unique `resource_kind` étant une instance, donc un BIGSERIAL). Ici TEXT, pour
    -- la même raison que `resource_grants.resource_id` : dès que `resource_kind`
    -- varie, la forme de l'id varie avec lui. Un BIGINT n'aurait fermé le champ
    -- d'extension qu'en apparence — il aurait forcé le premier `resource_kind` non
    -- numérique à migrer la colonne.
    resource_id TEXT NOT NULL,
    -- Qui accorde, à qui. Le scope est POLYMORPHE (0053-D3 : « la plateforme est un
    -- scope propriétaire comme les autres ») ⟹ pas de clé étrangère possible ; le
    -- vocabulaire des cinq scopes est fermé par un CHECK, faute de FK.
    grantor_kind TEXT NOT NULL,
    -- ⚠️ TEXT, et pour la raison déjà écrite sur `nodes.owner_id` : un scope `user`
    -- est un `sub` Logto (`users.sub` EST la clé primaire, il n'existe aucun id
    -- numérique d'utilisateur) et `platform` n'a pas d'id du tout — la convention
    -- maison est la chaîne littérale 'platform' (cf. `guides.owner_id`). Un BIGINT
    -- obligerait à inventer un surrogate par utilisateur : une migration d'identité,
    -- sans rapport avec le modèle d'accès.
    grantor_id TEXT NOT NULL,
    grantee_kind TEXT NOT NULL,
    grantee_id TEXT NOT NULL,
    -- Les bornes portées par l'arête (0053-D4). Vocabulaire **FERMÉ** à cinq
    -- entrées : role | quota | budget | rate | expiration. (`périmètre` en a été
    -- retiré le 08/08 : un seul emploi réel, aucun cas qui le réclamait.) La
    -- fermeture est MÉCANIQUE — cf. le CHECK en pied de table — donc l'étendre est
    -- une migration, ce qui EST le sens de « extensible seulement par amendement ».
    constraints JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- La chaîne matérialisée, jamais reconstruite par inférence (0053-D4).
    -- NULL = grant racine du propriétaire. La révocation d'un parent emporte la
    -- descendance À LA LECTURE (la marche voit `revoked_at`), sans rien propager :
    -- c'est l'argument du banc contre tout cache (une racine touche 1 998 users).
    parent_id BIGINT REFERENCES grants(id),
    -- D'où vient l'arête : 'manual' | 'subscription:<x>' | 'template:<x>' | 'promo'.
    -- ⚠️ Le chemin d'appel ne LIT JAMAIS ce champ (0053-D6, premier interdit) : à
    -- l'évaluation un grant est un grant. Il n'existe que pour le producteur, qui
    -- diffe les grants DONT IL EST LA SOURCE (second interdit) — un webhook du PSP ne
    -- doit jamais reprendre ce qu'un admin a accordé à la main.
    source TEXT NOT NULL DEFAULT 'manual',
    created_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 0053-D7 : un grant révoqué ou remplacé s'ARCHIVE, il ne se supprime jamais —
    -- sinon un simple changement de plan efface la consommation, et personne ne le
    -- voit. C'est aussi pourquoi `grant_counters` ne porte PAS de ON DELETE CASCADE.
    revoked_at TIMESTAMPTZ,
    -- Contraintes NOMMÉES (docs/live-migrations.md) : un futur DROP CONSTRAINT ne
    -- peut pas viser autre chose que ce qu'il croit viser.
    CONSTRAINT grants_grantor_kind_check
        CHECK (grantor_kind IN ('platform', 'tenant', 'org', 'group', 'user')),
    CONSTRAINT grants_grantee_kind_check
        CHECK (grantee_kind IN ('platform', 'tenant', 'org', 'group', 'user')),
    -- Le vocabulaire fermé, rendu mécanique : ôter les cinq clés connues doit laisser
    -- l'objet VIDE. Attrape aussi le cas où `constraints` n'est pas un objet.
    CONSTRAINT grants_constraints_vocabulary CHECK (
        constraints - ARRAY['role', 'quota', 'budget', 'rate', 'expiration'] = '{}'::jsonb)
);
-- TROIS index, et ce sont des CONDITIONS, pas des détails (banc L0 du 07/08,
-- `oto-blueprint:docs/banc-resolution-grants.md`). Table et index naissent ensemble
-- ⟹ leur place est ici et pas dans `_init` (piège « CREATE INDEX d'une NOUVELLE
-- colonne », docs/live-migrations.md).
--
-- 1. Trouver les feuilles : la marche part TOUJOURS du bénéficiaire, et c'est ce qui
--    la rend plate (×134 de grants ⟹ +42 % de latence seulement).
--    (Le banc portait un `resource_kind` en 3e position, qu'aucune de ses requêtes
--    n'utilisait ; l'ADR et le plan retiennent les deux colonnes utiles.)
CREATE INDEX IF NOT EXISTS idx_grants_grantee
    ON grants(grantee_kind, grantee_id) WHERE revoked_at IS NULL;
-- 2. Remonter la chaîne jusqu'à la racine (profondeur bornée, 4 en pratique).
CREATE INDEX IF NOT EXISTS idx_grants_parent ON grants(parent_id);
-- 3. COMPTER. ⚠️⚠️ **Celui-ci est une CONDITION, pas une optimisation, et il doit
--    rester NON PARTIEL.** Mesuré : sans lui la lecture d'un quota coûte **73,8 ms**
--    (p99 104,5), avec lui **0,035 ms** — ×2000. Sur un serveur MONO-LOOP, 74 ms par
--    appel compté est un gel de boucle, le mode de panne exact que
--    `docs/event-loop-perf.md` documente déjà. La cause est structurelle et ne
--    s'améliorera pas : une clé mutualisée est un MOYEU (~52 000 arêtes entrantes en
--    moyenne, 68 364 au maximum à l'échelle mesurée), et sommer ses arêtes sans cet
--    index, c'est toutes les traverser pour n'en garder que deux ou trois.
--    ⚠️ **Ne PAS lui ajouter `WHERE revoked_at IS NULL` pour l'« harmoniser » avec
--    l'index 1.** Les deux lectures sont opposées : la résolution ignore les arêtes
--    révoquées, le comptage de D7 les compte — un quota se lit en sommant les arêtes
--    de la même (instance, bénéficiaire, fenêtre), **archivées comprises**, sans quoi
--    une bascule de plan remet le compteur à zéro. Un index partiel ne peut pas
--    servir une requête sans ce prédicat : PostgreSQL retomberait sur un scan, et les
--    74 ms reviendraient **en silence**, sans une ligne de diff qui les explique.
CREATE INDEX IF NOT EXISTS idx_grants_resource_grantee
    ON grants(resource_id, grantee_kind, grantee_id);

-- Le metering s'attache à l'ARÊTE (0053-D7) : la consommation free-tier se compte
-- contre le grant plateforme→org, et le plafond d'un partenaire white-label est le
-- même compteur un étage plus haut — aucune feature dédiée. L'arête porte la règle
-- et les incréments ; le PLAFOND, lui, s'évalue sur le couple (instance,
-- bénéficiaire) en sommant les arêtes (cf. l'index 3 ci-dessus).
--
-- ⚠️ Pas de `ON DELETE CASCADE` sur la FK, et c'est délibéré : D7 pose qu'un grant
-- s'archive et ne se supprime JAMAIS. Sans CASCADE, supprimer un grant déjà compté
-- devient impossible (la FK refuse) au lieu d'effacer silencieusement l'historique
-- de consommation — la règle est tenue par la base, pas par la mémoire du lecteur.
--
-- ⚠️ La colonne s'appelle `window_start` et non `window` : WINDOW est un mot RÉSERVÉ
-- de PostgreSQL (fonctions de fenêtrage), inutilisable sans guillemets — ce que le
-- banc avait déjà tranché ainsi.
--
-- Non mesuré, et à ne pas confondre avec le coût de LECTURE ci-dessus : le DÉBIT en
-- écriture, dont le risque est une CONTENTION (l'incrément tombe sur une ligne
-- partagée par tous les bénéficiaires sous l'arête, que PostgreSQL sérialise) et non
-- une latence. Il dépend d'un arbitrage non rendu — débiter la feuille, ou chaque
-- arête traversée.
CREATE TABLE IF NOT EXISTS grant_counters (
    grant_id BIGINT NOT NULL REFERENCES grants(id),
    window_start DATE NOT NULL,
    calls BIGINT NOT NULL DEFAULT 0,
    spend NUMERIC NOT NULL DEFAULT 0,
    CONSTRAINT grant_counters_pkey PRIMARY KEY (grant_id, window_start)
);

-- ── Le journal de la DOUBLE LECTURE de L7 (blueprint ADR 0053, lot L7) ────────
-- Pendant la fenêtre, la chaîne de grants CALCULE à côté et l'ancien chemin DÉCIDE.
-- Cette table est la matière du verdict qui autorise l'inversion : elle compte, par
-- jour et par (connecteur, org), les fois où les deux voies se sont accordées et les
-- fois où elles ont divergé — chaque divergence rangée dans une CLASSE.
--
-- ⚠️ **Un compteur en base, et pas un journal en WARN**, contrairement à la fenêtre
-- de L5. Deux faits de terrain l'imposent : le journal de la box ne remonte que
-- ~8 h, et une fenêtre d'observation se compte en JOURS ; et la préprod partage
-- cette base sans partager le trafic, donc le dénominateur ne peut venir que d'un
-- compteur écrit par le process qui sert.
--
-- ⚠️ **Jamais une écriture par appel.** L'accord — le cas nominal, donc le volume —
-- est accumulé en mémoire et versé au plus une fois par minute et par
-- (connecteur, org) ; seules les DIVERGENCES, rares par construction, s'écrivent à
-- l'occurrence. C'est ce qui garde la table hors du chemin chaud d'un serveur
-- mono-loop, et hors de la contention de ligne mesurée pour R8.
--
-- `sample` porte la PREMIÈRE occurrence du jour pour cette classe, et rien de
-- nominatif : le sub y est HACHÉ, seuls les paliers et l'équipe en cause sont en
-- clair (une équipe est ce sur quoi on agit, un sub ne l'est pas).
--
-- ⚠️ **Aucune clé primaire, et c'est voulu** : la clé porte aussi `origine` (posée par
-- `_init.py`), NULL quand l'environnement ne se nomme pas — ce qu'une clé primaire
-- refuse. Elle est l'index unique `access_shadow_l7_origine_key` … NULLS NOT DISTINCT,
-- posé au démarrage après la colonne (`db/access_shadow.CLE_ORIGINE`) ; une base servie
-- l'a reçu de la révision 0052, qui a retiré l'ancienne clé primaire.
CREATE TABLE IF NOT EXISTS access_shadow_l7 (
    day       DATE   NOT NULL,
    connector TEXT   NOT NULL,
    -- 0 = aucune org de contexte (la sentinelle du reste du schéma).
    org_id    BIGINT NOT NULL DEFAULT 0,
    classe    TEXT   NOT NULL,
    n         BIGINT NOT NULL DEFAULT 0,
    first_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sample    JSONB  NOT NULL DEFAULT '{}'::jsonb
);
"""

# demandes d'accès : ce qu'une personne bloquée demande, et qui le lui accorde
DEMANDES_D_ACCES = """
-- DEMANDES d'accès (`docs/demandes-d-acces.md`) : une personne bloquée (« demande à un
-- admin ») le DEMANDE, et qui peut le lui accorder l'accorde d'un geste. La demande
-- n'accorde rien elle-même : l'approbation rejoue la capacité qui accorde déjà (partage,
-- activation, outils d'un agent, équipe) SOUS L'IDENTITÉ de qui approuve, ses gardes
-- comprises. `kind` = le geste demandé ; `cible` = ce qu'il vise (JSONB) ; `cle_cible`
-- = sa forme canonique en texte, pour l'unicité : UNE demande en attente par
-- (org, demandeur, geste, cible) — redemander relance celle qui existe. L'org est
-- dans l'index et non dans `cle_cible` : la clé d'un connecteur (`connector:<nom>`)
-- ne la porte pas, et une personne membre de deux orgs y demande deux choses. `demande` = ce qui
-- est demandé (un rôle, des outils), `decision` = ce qui a été réellement accordé.
-- Une demande en attente dont `expires_at` est passée ne se sert plus en attente ; elle
-- passe `expired` quand la même demande est refaite (l'unicité la libère).
-- FK vers l'ORG en cascade : une org supprimée emporte ses demandes.
CREATE TABLE IF NOT EXISTS access_requests (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    requester_sub TEXT NOT NULL,
    kind TEXT NOT NULL,
    cible JSONB NOT NULL,
    cle_cible TEXT NOT NULL,
    demande JSONB NOT NULL DEFAULT '{}'::jsonb,
    note TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'declined', 'cancelled', 'expired')),
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    decision JSONB,
    decision_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reminded_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT now() + interval '14 days'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_access_requests_en_attente
    ON access_requests (org_id, requester_sub, kind, cle_cible) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_access_requests_org_en_attente
    ON access_requests (org_id, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_access_requests_demandeur
    ON access_requests (requester_sub, id);
CREATE INDEX IF NOT EXISTS idx_access_requests_decideur
    ON access_requests (decided_by);
"""
