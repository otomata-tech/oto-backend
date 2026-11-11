"""DDL du domaine « datastore » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# namespaces et lignes du datastore
DATASTORE = """
-- Datastore = spine natif PG (ADR 0016). `user_datastores` = registre de
-- namespaces ; les rows vivent dans `datastore_rows` (JSONB). Propriété portée par
-- `(owner_type, owner_id)` (ADR 0030 : user/org/group). Phase H (cadrage 10/07)
-- TERMINÉE : les reliques per-sub/Sheets (`sub`, `spreadsheet_id`, `owner_email`,
-- table `datastore_shares`) sont purgées du code (B1, promu prod) et DROPpées (B2).
-- ⚠️ Les INDEX sur owner_type/owner_id NE sont PAS créés ici : sur une base
-- existante, `CREATE TABLE IF NOT EXISTS` est un no-op et ces colonnes n'existent
-- pas encore quand `_SCHEMA` s'exécute (ajoutées plus bas par ALTER). Index +
-- contrainte d'unicité owner créés dans init_db APRÈS l'ALTER (couvre fresh ET existant).
CREATE TABLE IF NOT EXISTS user_datastores (
    id BIGSERIAL PRIMARY KEY,
    owner_type TEXT NOT NULL DEFAULT 'user',
    owner_id TEXT,
    namespace TEXT NOT NULL,
    -- Mode TYPÉ optionnel (ADR 0032 §6 / 0029) : NULL = table libre (colonnes
    -- découvertes des rows) ; sinon un schéma déclaré
    -- {fields:[{key,label?,type?,role?}]} où role ∈ title|badge|metric|status|
    -- qualif|note pilote le rendu en fiches. Soft : pas de validation à l'écriture.
    schema JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- L'org ACTIVE de l'appel qui a créé un tableau PERSONNEL (oto#160) : sa
    -- propriété est la personne, son contexte l'org où elle travaillait. NULL pour un
    -- tableau d'org ou d'équipe (contexte dérivé du propriétaire) et pour un personnel
    -- d'avant la colonne. Même sens que `projects.context_org_id`. Sur une base
    -- existante, elle vient de la révision `0017` ou du démarrage
    -- (`datastore_ns.DDL_COLONNE_CONTEXTE_ORG`, même forme).
    context_org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL
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
    -- File de travail (ADR 0046 D) : bail posé par data_claim_next (SKIP LOCKED).
    -- NULL = libre ; claimed_until < NOW() = bail expiré (row recyclable). Libéré
    -- par data_release ou par l'entrée dans un état terminal du cycle de vie.
    claimed_by TEXT,
    claimed_until TIMESTAMPTZ,
    -- Le RUN sous lequel la ligne a été réservée (#317). Trois choses en dépendent :
    --
    -- ① **la libération quand l'agent meurt** — la troisième voie du verrou, avec le
    --    release explicite et l'expiration. La pile de run est session-scopée (état
    --    FastMCP, aucune table) : elle ne survit ni au redémarrage ni à l'agent
    --    disparu, or c'est PRÉCISÉMENT lui qu'il s'agit de ramasser. Le lien doit
    --    donc être durable, et il l'est ICI plutôt que dans une table à part — le
    --    bail est une propriété de la LIGNE (0046 amendé par #317), et deux endroits
    --    où il vit seraient deux vérités à réconcilier ;
    -- ② **l'identification du titulaire à l'écriture** — écrire sous le run qui tient
    --    la ligne, c'est être le titulaire, sans rien avoir à déclarer ;
    -- ③ le point d'ancrage naturel du futur journal d'écriture (oto#19) : « qui a
    --    écrit quoi sous quel run » commence par « qui tient la ligne ».
    --
    -- Mesure qui a rendu ① nécessaire : UNE ligne en production portait un bail, tenu
    -- depuis 18 jours par un worker disparu — invisible de tous.
    claimed_run TEXT,
    -- Réservations SANS écriture depuis la dernière écriture réussie (#433). Une
    -- file peut tourner à vide : l'agent réserve, enquête, conclut sans rien
    -- écrire, la ligne revient, et le suivant refait le même faux départ. Le
    -- compteur est remis à 0 par toute écriture de la ligne ; au-delà du plafond
    -- déclaré (`lifecycle.max_claims`), la ligne passe dans `lifecycle.
    -- abandon_state` et `abandon_reason` porte le motif — non NULL = hors file,
    -- quel que soit le filtre du client.
    claims INTEGER NOT NULL DEFAULT 0,
    abandon_reason TEXT,
    -- Le run de la DERNIÈRE tentative d'une ligne abandonnée (oto-backend#491), posé
    -- avec `abandon_reason` et effacé avec lui : le motif dit ce que la tentative a
    -- rencontré, cette colonne-ci se rapproche du journal (`tool_calls.run_id`) sans
    -- relire une phrase. NULL sur une ligne abandonnée = réservée hors run. Base
    -- existante : révision `0038_abandon_run`.
    abandon_run TEXT,
    -- L'instant de la dernière PRISE de la ligne par la file (oto#101) — jamais
    -- effacé par le relâchement, l'expiration ni l'écriture. `claim_next` sert la
    -- ligne servie le moins récemment (`NULLS FIRST` : jamais servie d'abord), sans
    -- quoi une ligne relâchée revenait en tête et la file tournait sur elle-même.
    -- Index `idx_datastore_rows_file` posé par `_init.py` (colonne ajoutée à une
    -- table existante : cf. `docs/live-migrations.md`).
    claimed_at TIMESTAMPTZ,
    -- RÉVISION de la ligne (12/09/2026), servie `_revision` : la précondition
    -- `expected_revision` d'une écriture qui recalcule ce qu'elle a lu. Avancée par le
    -- déclencheur `datastore_rows_20_revision` (le premier du dépôt, `db/revision.py`),
    -- jamais par le code : une écriture de l'ancienne version, pendant la bascule
    -- bleu/vert sur cette base partagée, doit la faire avancer aussi. Il est posé par
    -- `_init.py`, pas ici. PostgreSQL exécute les déclencheurs BEFORE ROW d'une table
    -- dans l'ordre alphabétique de leur nom : `_10_` passe avant `_20_`.
    rev BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (ns_id, row_id)
);

-- (`datastore_shares` — legacy remplacée par `resource_grants`, ADR 0030 — DROPpée
--  en Phase H B2, 10/07.)
"""

# journal des révisions de ligne (oto#273)
REVISIONS = """
-- JOURNAL des révisions de ligne (oto#273, M1 : écriture fantôme, aucune lecture).
-- En ajout seul, écrit par les déclencheurs `datastore_rows_30_journal_*`
-- (`db/journal_revisions.py`), jamais par le code. `diff` = {champ: {avant, apres}},
-- valeurs entières ; un côté absent n'a pas sa clé. Pas de FK vers la LIGNE : les
-- révisions d'une ligne supprimée restent. FK vers le TABLEAU, en cascade : un tableau
-- supprimé emporte son historique, quel que soit le code qui le supprime — la même
-- raison qui fait écrire le journal par PostgreSQL. L'index `(ns_id, row_id, rev)` est
-- posé par `_init.py`, sous garde de catalogue : ici, `CREATE INDEX IF NOT EXISTS`
-- reprendrait à chaque boot un verrou SHARE qui bloque chaque écriture de ligne.
-- Estampille (acteur, run, source, geste) : M2, NULL en M1. `suppression` : la
-- révision que laisse la suppression de la ligne (tous ses champs en `avant`) ; sur
-- une base existante, la colonne vient de la révision `0016` ou du démarrage
-- (`journal_revisions.DDL_COLONNE_SUPPRESSION`, même forme). `outil`/`fournisseur` :
-- l'outil et le fournisseur qui ont trouvé la valeur (`journal_revisions.
-- DDL_COLONNES_ATTRIBUTION`, révision `20261111_1000_outil_revisions`).
CREATE TABLE IF NOT EXISTS datastore_row_revisions (
    id BIGSERIAL PRIMARY KEY,
    ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
    row_id TEXT NOT NULL,
    rev BIGINT NOT NULL,
    diff JSONB NOT NULL,
    acteur TEXT,
    run_id TEXT,
    source TEXT,
    geste_id TEXT,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    suppression BOOLEAN NOT NULL DEFAULT false,
    outil TEXT,
    fournisseur TEXT
);
"""

# commentaires de ligne : la conversation des personnes sur une ligne
COMMENTAIRES = """
-- COMMENTAIRES d'une ligne : ce que les personnes qui la lisent en disent, à côté de
-- ses valeurs (`docs/datastore.md` §Commentaires). Ce n'est PAS une colonne de la
-- ligne : commenter ne fait avancer ni sa `rev` ni son journal, et ne réveille aucun
-- agent qui surveille ses écritures. Pas de FK vers la LIGNE, comme le journal : les
-- commentaires d'une ligne supprimée restent, lisibles par qui gouverne le tableau.
-- FK vers le TABLEAU, en cascade : un tableau supprimé emporte ses commentaires.
-- `deleted_at` : un commentaire retiré ne se sert plus, il ne se détruit pas : toute
-- lecture le filtre, l'index des lignes est donc PARTIEL (le vivant seul). L'index
-- sur `auteur_sub` sert la fusion de comptes (`migrate_sub` repointe la colonne).
CREATE TABLE IF NOT EXISTS datastore_row_comments (
    id BIGSERIAL PRIMARY KEY,
    ns_id BIGINT NOT NULL REFERENCES user_datastores(id) ON DELETE CASCADE,
    row_id TEXT NOT NULL,
    auteur_sub TEXT,
    body TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_datastore_row_comments_ligne
    ON datastore_row_comments (ns_id, row_id, id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_datastore_row_comments_auteur
    ON datastore_row_comments (auteur_sub);
"""
