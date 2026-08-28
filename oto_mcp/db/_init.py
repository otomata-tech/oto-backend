"""Initialisation du schéma + migrations idempotentes au boot.

Extrait de l'ex-monolithe `db.py` (barreau 2). `init_db()` applique `_SCHEMA`
puis les ALTER/backfill idempotents. Appelé une fois au démarrage du serveur.
"""
from __future__ import annotations

import json
import logging
import os
import time

import psycopg

from . import connector_instances
from ._conn import _connect
from ._schema import _SCHEMA

logger = logging.getLogger(__name__)

# Clé d'advisory lock (arbitraire mais FIXE) sérialisant init_db entre instances
# qui bootent en parallèle sur la MÊME base (partagée canari/prod). Cf. init_db().
_INIT_DB_LOCK_ID = 0x0704_0D0B  # « oto init_db », 117_449_483

def init_db() -> None:
    """Applique le schéma, en RÉESSAYANT sur contention de lock.

    L'advisory lock ci-dessous sérialise deux MIGRATEURS entre eux, mais rien ne
    protège du **trafic applicatif** : la migration tient des AccessExclusiveLock sur
    des dizaines de tables dans une seule transaction, pendant qu'une requête en vol
    en verrouille d'autres → cycle possible, donc `DeadlockDetected` (Sentry, 2026-07-30 :
    un `ALTER TABLE user_api_tokens` au boot canari contre un `/api/fr/accords/search`
    en prod, DB partagée). PG choisit sa victime au hasard : si c'est nous, le boot
    échouait sec. Deux crans, dans cet ordre :
      1. `lock_timeout` (posé APRÈS l'advisory lock, qui lui doit attendre sans borne)
         → le DDL abandonne vite au lieu de camper en tête de file d'attente, où il
         bloque tout le trafic sur la table derrière lui ;
      2. retry de la transaction entière — la DDL est idempotente, un rejeu est sûr.
    """
    attempts = max(1, int(os.environ.get("OTO_MCP_INIT_DB_ATTEMPTS", "3")))
    for attempt in range(1, attempts + 1):
        try:
            _init_db_once()
            return
        except (psycopg.errors.DeadlockDetected, psycopg.errors.LockNotAvailable) as e:
            if attempt == attempts:
                raise
            delay = 2 * attempt
            logger.warning(
                "init_db: %s (tentative %d/%d) — nouvel essai dans %ds",
                type(e).__name__, attempt, attempts, delay,
            )
            time.sleep(delay)


    # HORS de tout bloc transactionnel : la reconstruction des index de clé métier
    # passe par `CREATE INDEX CONCURRENTLY`, que PostgreSQL REFUSE dans une
    # transaction (« cannot run inside a transaction block », vérifié). Elle ouvre
    # donc ses propres connexions autocommit (#318).
    migrate_business_key_indexes()

def _init_db_once() -> None:
    with _connect() as conn:
        # Un SEUL migrateur à la fois. La DB est PARTAGÉE canari/prod : deux instances
        # qui bootent en même temps exécutaient CETTE MÊME transaction de DDL (DROP/
        # ALTER/CREATE INDEX sur les mêmes tables + leurs FK) et s'interbloquaient sur
        # les locks catalogue (DeadlockDetected in init_db, Sentry). Un advisory lock
        # de TRANSACTION — pris en PREMIER, relâché au commit du `with` — sérialise le
        # boot : le 2e attend la fin du 1er, puis traverse la DDL idempotente (devenue
        # no-op) SEUL, sans entrelacement de locks. Doit précéder tout DDL (y compris
        # les _migrate_* ci-dessous). Le waiter est ACTIF (pas idle-in-tx) → non coupé
        # par idle_in_transaction_session_timeout ; il attend le commit du 1er.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_INIT_DB_LOCK_ID,))
        # APRÈS l'advisory lock (qui, lui, doit pouvoir attendre le migrateur d'en face
        # sans borne) : borne l'ATTENTE d'acquisition de chaque lock de table du DDL.
        # `true` = LOCAL à la transaction. Ne borne PAS la durée d'exécution (un gros
        # CREATE INDEX reste libre de prendre son temps une fois le lock obtenu) — donc
        # sans risque pour les migrations lourdes, contrairement à statement_timeout.
        conn.execute(
            "SELECT set_config('lock_timeout', %s, true)",
            (os.environ.get("OTO_MCP_INIT_DB_LOCK_TIMEOUT_MS", "5000"),),
        )
        # AVANT _SCHEMA : renomme l'ancienne tool_call_log vers le schéma canonique
        # (sinon CREATE IF NOT EXISTS poserait une tool_calls vide à côté).
        _migrate_tool_call_log(conn)
        # AVANT _SCHEMA : droppe l'org_subscriptions du modèle Stripe retiré
        # (sinon CREATE IF NOT EXISTS saute et l'index 0043 explose au boot).
        _drop_legacy_org_subscriptions(conn)
        # Recherche sémantique (lot 3) : pgvector requis par la table doc_embeddings
        # (halfvec/hnsw) créée dans _SCHEMA → l'extension DOIT précéder. Idempotent ;
        # no-op si déjà installée. pgvector 0.8.2 dispo sur otomata-main (spike 20/07).
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(_SCHEMA)
        # ADR 0044 §F R5 : clés plateforme + grants migrés en instances du coffre unifié
        # (connector_credentials scope PLATFORM + share_down/meta.rate_limit) → DROP des 3
        # tables legacy (plus ni lues ni écrites). Idempotent, no-op après le 1er boot.
        conn.execute("DROP TABLE IF EXISTS user_grants")
        conn.execute("DROP TABLE IF EXISTS org_grants")
        conn.execute("DROP TABLE IF EXISTS platform_keys")
        # Idempotent column adds — `CREATE TABLE IF NOT EXISTS` ne propage pas les
        # nouvelles colonnes sur les tables existantes.
        # ADR 0052 (lot L1) — rattacher les orgs à un tenant. L'ORDRE compte : le
        # tenant 1 doit exister AVANT la colonne, sinon la FK échoue sur la première
        # org. Purement additif ⟹ sûr sur la base partagée preprod/prod (même
        # argument que `embed_dirty` plus bas) ; PG ≥ 11 range le DEFAULT au catalogue,
        # donc pas de réécriture de table. **Rien ne lit encore cette colonne** :
        # l'existant est NOMMÉ, pas déplacé — le lot se défait par un `drop`.
        conn.execute("INSERT INTO tenants (id, slug, name) VALUES (1, 'oto', 'Oto') "
                     "ON CONFLICT (id) DO NOTHING")
        # La séquence ne bouge pas sur un INSERT à id explicite : sans ce recalage, le
        # prochain tenant naîtrait sur l'id 1 et casserait (cf. « ids fusionnés = la
        # MÊME séquence », docs/live-migrations.md).
        conn.execute("SELECT setval(pg_get_serial_sequence('tenants','id'), "
                     "GREATEST((SELECT MAX(id) FROM tenants), 1))")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS tenant_id BIGINT "
                     "NOT NULL DEFAULT 1 REFERENCES tenants(id)")
        # ADR 0052 (lot L2) — le tenant PORTE son émetteur, et les domaines qui le
        # désigneront en L3. Contrairement à L1, aucun ordre à tenir vis-à-vis des
        # données : colonnes nullables ou defaultées, aucune FK à satisfaire, donc
        # rien qu'une ligne existante puisse violer. `ADD COLUMN IF NOT EXISTS …
        # UNIQUE` est rejouable — colonne déjà là ⟹ la clause entière est sautée,
        # contrainte comprise (et sur `tenants`, l'index unique se construit sur une
        # poignée de lignes).
        # Le tenant `oto` garde `issuer` NULL : son émetteur est l'env, pas la base
        # (cf. le commentaire de `_schema`). NULL n'entre pas dans l'unicité.
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS issuer TEXT UNIQUE")
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS jwks_uri TEXT")
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS hosts JSONB "
                     "NOT NULL DEFAULT '[]'::jsonb")
        # (lot L3) Le client OAuth que la FAÇADE d'enregistrement sert sur les hosts de
        # ce tenant. Logto self-hosted ne sait pas enregistrer un client à la volée :
        # notre façade rend un client_id PRÉPARÉ, et il doit être celui de l'annuaire
        # vers lequel on envoie l'utilisateur — sinon le client s'authentifie chez l'un
        # avec l'identité de l'autre. NULL = aucun host servi pour ce tenant.
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS oauth_client_id TEXT")
        # Chantier runner R5 (fusion flotte) : le worker déclare le RÉSULTAT d'un job
        # à sa conclusion (usage_tokens, stopped, steps…) — c'est ce qui rend le coût
        # lisible par un ordonnanceur de flotte (garde budget) sans parser une note.
        conn.execute("ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS result JSONB")
        # (lot L3) L'adresse du tableau de bord DE CE TENANT. Les liens qu'on rend à
        # ses utilisateurs — un tableau, un retour de connexion, une page partagée —
        # portaient notre domaine : un client d'un partenaire recevait des liens vers
        # un produit qui n'est pas le sien. NULL = les nôtres.
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS dashboard_url TEXT")
        # (lot L3) Les CHEMINS de ce tenant, par type de lien — `{"doc": "/network/
        # {org}/knowledge/{id}", …}`. Une adresse ne suffit pas : les chemins d'un
        # partenaire ne ressemblent pas aux nôtres, et certaines de nos vues n'ont
        # aucun équivalent chez lui. Type absent = AUCUN lien de ce type (cf. `links`).
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS link_paths JSONB "
                     "NOT NULL DEFAULT '{}'::jsonb")
        # Le PRÉFIXE des outils montrés aux comptes de ce tenant. La liste d'outils
        # d'un partenaire annonçait `oto_doc`, `oto_project`… dans SON produit — le
        # même défaut que le socle et les liens, mais sur l'identifiant affiché à
        # chaque appel. NULL = les noms canoniques (l'état d'avant, et le défaut).
        conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tool_prefix TEXT")
        # #117 — discriminant PAR APPEL. Trois colonnes nullables : rien à réécrire sur
        # une table volumineuse (une colonne sans défaut ne touche pas les lignes
        # existantes), et les lignes d'avant restent lisibles avec des NULL — elles
        # n'ont simplement pas de discriminant, ce qui est la vérité.
        # ⚠️ PAS d'index ici : la lecture qui compte (`effective_sub IS DISTINCT FROM
        # sub`) est une requête d'enquête, pas un chemin chaud — et un index de plus sur
        # `tool_calls` se paie à CHAQUE appel journalisé. À poser le jour où on enquête
        # souvent, pas d'avance.
        for _col in ("request_id", "call_uid", "effective_sub"):
            conn.execute(f"ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS {_col} TEXT")
        # Soft-disconnect unipile : la ligne de binding survit (preuve de propriété
        # durable du compte hébergé → rebind déterministe à la reconnexion).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS disconnected_at TIMESTAMPTZ")
        # Lien journal → traceback Sentry (investigation) : l'event id capturé pour
        # l'appel. Additif, NULL sur tout l'historique (non reconstructible).
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS sentry_event_id TEXT")
        # #493 : le journal de paiement porte le customer Mollie de la tentative. Le
        # miroir `org_subscriptions` n'est posé qu'à `confirm` — entre deux clics de
        # souscription il n'y avait donc RIEN à relire, et un second customer Mollie
        # naissait à chaque tentative (vécu le 25/08). Additif, NULL sur l'historique.
        conn.execute("ALTER TABLE billing_payments ADD COLUMN IF NOT EXISTS customer_id TEXT")
        # #486 : la décomposition fiscale de CHAQUE tentative de débit. `amount` porte
        # désormais le TTC réellement passé au PSP ; le HT, le taux (en points de base)
        # et la TVA sont figés à côté. Additif et NULLABLE — surtout pas de backfill :
        # les encaissements antérieurs au 28/08/2026 ont réellement été débités du HT
        # sans TVA, et leur inventer une décomposition ferait mentir le journal sur ce
        # que le PSP a pris. `amount_ht IS NULL` = « ligne d'avant la règle ».
        for _col, _type in (("amount_ht", "INTEGER"), ("vat_rate_bps", "INTEGER"),
                            ("vat_amount", "INTEGER"), ("country_code", "TEXT"),
                            ("vat_scheme", "TEXT")):
            conn.execute(f"ALTER TABLE billing_payments "
                         f"ADD COLUMN IF NOT EXISTS {_col} {_type}")
        # ADR 0032 §2 : le lien projet→entité porte un `role` (pourquoi cette entité est ici).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS role TEXT")
        # ADR 0032 §4 (B2) : surcharge contextuelle préfaite du lien (connecteur → identité/instructions).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'")
        # ADR 0032 §3 (B4b) : un « Autre document » peut être partagé publiquement.
        conn.execute("ALTER TABLE project_files ADD COLUMN IF NOT EXISTS public BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE project_files ADD COLUMN IF NOT EXISTS public_url TEXT")
        # Recherche sémantique (lot 3) : outbox d'indexation — `embed_dirty` marque les
        # pages à (ré)indexer ; le worker embed_worker draine hors event loop. Backfill :
        # tout ce qui n'a pas encore d'embedding est marqué dirty (idempotent).
        conn.execute("ALTER TABLE docs ADD COLUMN IF NOT EXISTS embed_dirty BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_docs_embed_dirty ON docs(id) WHERE embed_dirty")
        conn.execute("UPDATE docs SET embed_dirty = TRUE "
                     "WHERE embed_dirty = FALSE AND id NOT IN (SELECT doc_id FROM doc_embeddings)")
        # Sémantique AUSSI sur briefs (projects) + guides on-demand (#6 C) : même outbox.
        # Additif (ADD COLUMN IF NOT EXISTS) → sûr sur la base partagée. Backfill dirty.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS embed_dirty BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_embed_dirty ON projects(id) WHERE embed_dirty")
        conn.execute("ALTER TABLE guides ADD COLUMN IF NOT EXISTS embed_dirty BOOLEAN NOT NULL DEFAULT TRUE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_guides_embed_dirty ON guides(id) WHERE embed_dirty")
        conn.execute("UPDATE projects SET embed_dirty = TRUE "
                     "WHERE embed_dirty = FALSE AND id NOT IN (SELECT ref FROM aux_embeddings WHERE kind='brief')")
        conn.execute("UPDATE guides SET embed_dirty = TRUE "
                     "WHERE embed_dirty = FALSE AND id NOT IN (SELECT ref FROM aux_embeddings WHERE kind='guide')")
        # Sémantique OPT-IN des LIGNES de datastore (#67 V2.2) : flag par namespace
        # (`semantic_search`, défaut FALSE = jamais systématique — coût variable maîtrisé)
        # + outbox `embed_dirty` sur les rows (défaut FALSE, PAS de backfill massif : une
        # row n'est marquée qu'à l'écriture dans un namespace opt-in, ou au passage du flag
        # à ON qui re-dirty ses rows). Index partiel sur les seules rows dirty.
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS semantic_search BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS embed_dirty BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_datastore_rows_embed_dirty "
                     "ON datastore_rows(ns_id, row_id) WHERE embed_dirty")
        # Lot 3 Ship 3 : propositions de CRÉATION (doc_id nullable + project_id +
        # emplacement proposé + CHECK). Le CHECK valide sur l'existant (toutes les
        # lignes ont doc_id). Idempotent.
        conn.execute("ALTER TABLE doc_change_requests ALTER COLUMN doc_id DROP NOT NULL")
        conn.execute("ALTER TABLE doc_change_requests ADD COLUMN IF NOT EXISTS project_id BIGINT REFERENCES projects(id) ON DELETE CASCADE")
        conn.execute("ALTER TABLE doc_change_requests ADD COLUMN IF NOT EXISTS proposed_parent_id BIGINT REFERENCES docs(id) ON DELETE SET NULL")
        conn.execute("ALTER TABLE doc_change_requests ADD COLUMN IF NOT EXISTS proposed_kind TEXT")
        conn.execute("DO $$ BEGIN "
                     "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='dcr_target') THEN "
                     "ALTER TABLE doc_change_requests ADD CONSTRAINT dcr_target "
                     "CHECK (doc_id IS NOT NULL OR project_id IS NOT NULL); END IF; END $$")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dcr_requester ON doc_change_requests(requested_by, resolved_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dcr_project ON doc_change_requests(project_id, status)")
        # Lot 3 Ship 2 : chapô + ordre curé des pages. Backfill des positions par
        # fratrie (entiers espacés ×16, ordre historique = title) — idempotent
        # (ne touche que les NULL ; ensuite create_doc/move_doc posent toujours).
        conn.execute("ALTER TABLE docs ADD COLUMN IF NOT EXISTS description TEXT")
        conn.execute("ALTER TABLE docs ADD COLUMN IF NOT EXISTS position INTEGER")
        conn.execute("""
            UPDATE docs d SET position = s.rn * 16
            FROM (SELECT id, ROW_NUMBER() OVER (
                      PARTITION BY project_id, parent_id ORDER BY title, id) AS rn
                  FROM docs WHERE position IS NULL) s
            WHERE d.id = s.id AND d.position IS NULL
        """)
        # Lot 3 Ship 1 : index FTS de la recherche transverse (GIN d'expression —
        # PAS de colonne STORED, qui réécrirait la table sous ACCESS EXCLUSIVE).
        # Source unique des expressions : db/search.py (index ↔ requête identiques).
        # pg_trgm requis par les index TRIGRAMME (#67 : substring indexé « syl »→« Sylvie »
        # en plus de la FTS tokenisée) → l'extension DOIT précéder.
        conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        from . import search as _search
        # Colonnes de vecteur de CLASSEMENT (#318) — AVANT les index, parce qu'elles
        # doivent exister quand une requête les lit. `ADD COLUMN <tsvector>` nullable
        # ne réécrit pas la table (PG 11+) : instantané, aucun verrou long. Elles
        # naissent VIDES et se remplissent hors de ce chemin (boucle de fond) : le
        # remplissage au boot aurait rendu le démarrage tributaire du volume, sous le
        # healthcheck du deploy.
        for ddl in _search.rank_column_ddl():
            conn.execute(ddl)
        for ddl in _search.index_ddl():
            conn.execute(ddl)
        # Lot 3 chantier 0.4 : purge du type de lien `doc` (pointeur
        # manuel vers une page, subsumé par les backlinks [[…]] de Ship 4). 4 liens en
        # prod au comptage du 17/07. Idempotent (0 row ensuite).
        conn.execute("DELETE FROM project_links WHERE target_type = 'doc'")
        # Lot 3 chantier 0.3 : le projet KB est ancré PAR ID (fin de l'identification
        # par nom — renommable, transférable, 2 appels concurrents = 2 KB). ON DELETE
        # SET NULL : un hard-delete du projet vide l'ancre, kb.py recrée.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS kb_project_id BIGINT "
                     "REFERENCES projects(id) ON DELETE SET NULL")
        # Backfill one-shot par nom (l'ancien marqueur) : pour chaque org sans ancre,
        # le PLUS ANCIEN projet org-owned vivant nommé « Base de connaissance ».
        conn.execute("""
            UPDATE orgs o SET kb_project_id = p.id
            FROM (SELECT DISTINCT ON (owner_id) owner_id, id FROM projects
                  WHERE owner_type = 'org' AND name = 'Base de connaissance'
                    AND archived_at IS NULL
                  ORDER BY owner_id, id) p
            WHERE o.kb_project_id IS NULL AND p.owner_id = o.id::text
        """)
        # ADR 0032 §7 (B5a) : un projet peut être publié comme MODÈLE (template) copiable.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_template BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_template ON projects(is_template) WHERE is_template")
        # ADR 0032 (amende #44) : publication d'un projet en endpoint MCP dédié
        # `<mcp_slug>.mcp.oto.cx` — anonyme (toolset figé, sans login) ou authed per-org.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_slug TEXT")
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_access TEXT NOT NULL DEFAULT 'off'")
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_tools TEXT[] NOT NULL DEFAULT '{}'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_projects_mcp_slug ON projects(mcp_slug) WHERE mcp_slug IS NOT NULL")
        # Opt-in explicite : exposer les tools `data_*` (datastore de l'org propriétaire)
        # sur un endpoint `secret` sans login — l'endpoint AGIT alors sous l'autorité de
        # l'org propriétaire (pas de sub). Défaut FALSE (le datastore reste privé). JAMAIS
        # honoré en `anonymous` (endpoint public listé) : cf. set_project_mcp_publication.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_expose_datastore BOOLEAN NOT NULL DEFAULT FALSE")
        # Opt-in ADDITIONNEL, séparé de la lecture (#193) : l'ÉCRITURE du datastore
        # (data_write/data_set_schema) sur l'endpoint partagé. Défaut FALSE (lecture seule).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_expose_datastore_write BOOLEAN NOT NULL DEFAULT FALSE")
        # Opt-in : exposer les pages du projet (oto_doc) sur un endpoint partagé.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_expose_docs BOOLEAN NOT NULL DEFAULT FALSE")
        # Prose servie au destinataire d'un endpoint publié — ≠ brief_md (interne).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS mcp_instructions_md TEXT")
        # ADR 0043 : id du mandat (mdt_xxx Mollie) sur l'abonnement — la table
        # existait déjà (B1) quand la colonne est arrivée.
        conn.execute("ALTER TABLE org_subscriptions ADD COLUMN IF NOT EXISTS mandate_id TEXT")
        # ADR 0043 bascule Stancer→Mollie (2026-07-24) : réaligne l'index partiel
        # de la file de réconciliation sur les statuts terminaux MOLLIE (le
        # prédicat doit matcher billing_payments.TERMINAL_PAYMENT_STATUSES pour
        # que l'index serve). Tables billing dormantes → drop/recreate sans coût.
        conn.execute("DROP INDEX IF EXISTS idx_billing_payments_open")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_billing_payments_open "
            "ON billing_payments(created_at) "
            "WHERE status NOT IN ('paid', 'failed', 'canceled', 'expired')")
        # ADR 0046 D (datastore v2) : bail de claim de la file de travail sur les rows
        # (data_claim_next / data_release ; NULL = libre, bail expiré = recyclable).
        conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS claimed_by TEXT")
        conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS claimed_until TIMESTAMPTZ")
        # #317 : le run sous lequel la ligne est réservée. `ADD COLUMN` nullable sans
        # défaut = instantané (PG 11+), aucune réécriture, aucun verrou long — la
        # base est partagée avec la production.
        conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS claimed_run TEXT")
        # Plafond de reprises par ligne (#433) : compteur de réservations sans
        # écriture + motif d'abandon. `DEFAULT` sur table existante ne réécrit rien
        # (PG >= 11) — la fenêtre de healthcheck n'en voit pas la couleur, et aucun
        # backfill n'est requis (0 est bien l'état d'une ligne jamais réservée).
        conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS claims INTEGER NOT NULL DEFAULT 0")
        conn.execute("ALTER TABLE datastore_rows ADD COLUMN IF NOT EXISTS abandon_reason TEXT")
        # #317 : le rôle `title` devient une PRÉSENTATION (`display`). Conversion
        # ADDITIVE — le `role` reste en place, seuls les lecteurs changent de source ;
        # son retrait est l'étape suivante du dossier, une fois la bascule vérifiée.
        # Idempotent : ne touche que les champs qui n'ont pas encore leur `display`.
        # Population mesurée avant conversion : 57 tableaux, un titre chacun, aucun
        # conflit — la conversion est mécanique, un pour un.
        conn.execute("""
            UPDATE user_datastores d SET schema = jsonb_set(
                d.schema, '{fields}',
                (SELECT jsonb_agg(
                    CASE WHEN f->>'role' = 'title' AND f->>'display' IS NULL
                         THEN f || '{"display": "title"}'::jsonb ELSE f END
                    ORDER BY ord)
                   FROM jsonb_array_elements(d.schema->'fields')
                        WITH ORDINALITY AS t(f, ord)))
             WHERE jsonb_typeof(d.schema->'fields') = 'array'
               AND EXISTS (SELECT 1 FROM jsonb_array_elements(d.schema->'fields') x
                            WHERE x->>'role' = 'title' AND x->>'display' IS NULL)
        """)
        # « Ajouter à mon Oto » (otomata-private, canal d'acquisition) : un projet forké
        # depuis un partage public garde le pointeur vers sa source → import IDEMPOTENT
        # (on RÉCUPÈRE la copie déjà présente dans l'org au lieu d'en refaire une).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS copied_from BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_copied_from "
                     "ON projects(owner_type, owner_id, copied_from) WHERE copied_from IS NOT NULL")
        # Scope MEMBRE (ADR 0030 amendé 2026-07-17) : un projet naît possédé par
        # (owner_type='user', owner_id=sub) — PRIVÉ au créateur — MAIS dans le CONTEXTE
        # d'une org de travail. `context_org_id` porte cette org : elle sépare la PROPRIÉTÉ
        # (qui = la personne) du CONTEXTE (où = l'org, pour la résolution des credentials
        # et le scope de liste). L'identité de la plateforme est toujours `(moi, org)`.
        # NULL = projet non-perso (contexte dérivé de l'owner org/group) OU perso legacy
        # (résolution repli sur l'org perso, ancien comportement préservé).
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS context_org_id BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_member_context "
                     "ON projects(owner_id, context_org_id) "
                     "WHERE owner_type = 'user' AND archived_at IS NULL")
        # Emoji facultatif d'un projet (repère visuel) — additif, NULL par défaut.
        conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS icon TEXT")
        # Retrait du partage public CHIFFRÉ zero-knowledge (`/p/p`), supplanté par le
        # partage NAVIGABLE live sur `<slug>.share.oto.cx` (share_ui). La table ne stockait
        # que du ciphertext irrécupérable (clé jamais côté serveur) → drop sûr, pas de legacy.
        conn.execute("DROP TABLE IF EXISTS project_public_shares")
        # ADR 0032 §7 : l'onboarding n'est plus un mode spécial mais un projet « Découverte »
        # (semé à la création de l'org perso). On retire la machinerie d'accueil de la fiche
        # « situation avec oto » — il ne reste que le data model `profile`, relu à chaque session.
        conn.execute("ALTER TABLE user_account_profile DROP COLUMN IF EXISTS discovery_project_id")
        conn.execute("ALTER TABLE user_account_profile DROP COLUMN IF EXISTS onboarded")
        conn.execute("ALTER TABLE user_account_profile DROP COLUMN IF EXISTS onboarded_at")
        conn.execute("DELETE FROM platform_instructions WHERE key = 'onboarding'")
        # ADR 0042 (barreau 1) : `guides` unifie la PROSE d'instruction sur deux
        # livraisons — 'on-demand' (how-to `oto_guide`) et 'init' (readme injecté au
        # handshake). Colonne `delivery` (existants → 'on-demand' par DEFAULT = les
        # guides B5 restent des how-to) + backfill des readmes init platform + user
        # depuis les ex-tables (org/group suivent au barreau 2). ON CONFLICT DO NOTHING
        # = idempotent, ne réécrit jamais une ligne guides déjà posée.
        conn.execute("ALTER TABLE guides ADD COLUMN IF NOT EXISTS delivery TEXT NOT NULL DEFAULT 'on-demand'")
        conn.execute(
            "INSERT INTO guides (scope, owner_id, slug, delivery, body_md, created_at, updated_at) "
            "SELECT 'platform', 'platform', key, 'init', body_md, "
            "       COALESCE(updated_at, NOW()), COALESCE(updated_at, NOW()) "
            "FROM platform_instructions WHERE key = 'secret_sauce' "
            "ON CONFLICT (scope, owner_id, slug) DO NOTHING")
        # (Le backfill jumeau `user_agent_readme` → guides est RETIRÉ avec la table
        # elle-même — cf. le DROP plus bas. Il a tourné à chaque boot du 06/07 au 28/07 ;
        # le laisser ferait échouer le premier boot post-drop, exactement le piège noté
        # au barreau 2 ci-dessous.)
        #
        # === Lot M1 (blueprint ADR 0054/0063) : les guides deviennent des NŒUDS ====
        # L'ORDRE compte, et c'est le seul risque du lot : la conversion doit suivre
        # TOUTE écriture de `guides` faite par ce boot — sinon le readme plateforme
        # que la ligne juste au-dessus vient de semer n'arriverait dans `nodes` qu'au
        # boot d'après (même famille de piège que le seed-avant-colonne de L1).
        #
        # Copie legacy→cible à CHAQUE boot, gardée `to_regclass`
        # (docs/live-migrations.md) : tant que `guides` existe on recopie — la PROD
        # tourne encore l'ancien code sur CETTE MÊME base et y écrit pendant la
        # fenêtre de promotion ; après son DROP (lot suivant), la garde rend ceci
        # no-op et aucun boot ne casse, quel que soit l'ordre des déploiements.
        # Purement ADDITIF : rien n'est modifié ni supprimé dans `guides`, qui reste
        # lisible telle quelle par la prod. Le lot se défait en repointant la façade.
        #
        # Ce que la conversion FAIT DISPARAÎTRE, au-delà du déménagement de lignes :
        # le concept de guide. Une couche de contexte EST une page (0055-D4) → toutes
        # ces lignes deviennent `kind='page'`, et `delivery` (injecté / à la demande)
        # descend au rang de PROPRIÉTÉ. Détail et forme : `db/guides.py`, `_schema`.
        if conn.execute("SELECT to_regclass('guides') AS t").fetchone()["t"]:
            from .guides import CONVERT_GUIDES_TO_NODES_SQL
            conn.execute(CONVERT_GUIDES_TO_NODES_SQL)
        # Outbox sémantique des couches de contexte (#282) : `nodes` ne porte pas de
        # colonne `embed_dirty` (la forme de la table est mesurée, 0063-D3 garde-fou 1)
        # → le marqueur est une clé de `props`, et son index est PARTIEL sur elle seule
        # (l'équivalent d'`idx_guides_embed_dirty`, quelques lignes indexées).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_nodes_embed_dirty ON nodes(id) "
                     "WHERE (props->>'embed_dirty') = 'true'")
        # Backfill : ce qui a été converti au lot M1 n'a pas d'embedding sous le
        # nouveau keying (kind='node', ref=nodes.id) — on le remet dans l'outbox.
        from .aux_embed import MARK_NODES_TO_EMBED_SQL
        conn.execute(MARK_NODES_TO_EMBED_SQL)
        # Barreau 2 : readmes d'org + d'équipe (slug réservé claude_md) sortis de
        # `*_instructions` vers `guides` — backfills one-shot RETIRÉS (cadrage 10/07,
        # chantier procédures B1) : ils ont tourné en prod à chaque boot depuis le
        # 06/07, et celui d'équipe lisait `org_group_instructions` (jumelle vouée au
        # DROP — un boot post-drop aurait cassé).
        # ADR 0032 §6 / 0029 (B6) : mode typé optionnel d'un namespace de datastore.
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS schema JSONB")
        # gap #4a : partage public d'un doc (token de lien public, lookup indexé).
        conn.execute("ALTER TABLE docs ADD COLUMN IF NOT EXISTS public_token TEXT")
        # ADR 0032 (« stop using slug ») : id surrogate stable + globalement unique pour
        # les doctrines. `org_instructions` garde (org_id, slug) comme clé naturelle
        # interne ; l'`id` devient l'identité PUBLIQUE (URL, project_links, runs). Backfill
        # des lignes existantes via une séquence (idempotent).
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS id BIGINT")
        conn.execute("CREATE SEQUENCE IF NOT EXISTS org_instructions_id_seq OWNED BY org_instructions.id")
        conn.execute("UPDATE org_instructions SET id = nextval('org_instructions_id_seq') WHERE id IS NULL")
        conn.execute("ALTER TABLE org_instructions ALTER COLUMN id SET DEFAULT nextval('org_instructions_id_seq')")
        conn.execute("ALTER TABLE org_instructions ALTER COLUMN id SET NOT NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_org_instructions_id ON org_instructions(id)")
        # Archivage (soft-delete) d'une procédure : masquée de tous les listings —
        # y compris ceux que l'IA lit (`skills_index_md`, `oto_procedure op=list`,
        # l'index de doctrine) — mais la ligne ET son historique de révisions
        # restent intacts. C'est l'alternative NON destructive à `delete`, qui lui
        # supprime `org_instruction_revisions` avec. NULL = vivante. Même forme
        # que `projects.archived_at` / `orgs.archived_at`.
        #
        # Lot A (additif pur, cf. docs/live-migrations.md) : canari et prod
        # partagent la base, mais une colonne nullable qu'aucun code prod ne lit
        # ne peut rien casser. Fenêtre assumée le temps de la promotion : une
        # procédure archivée depuis canari reste visible en prod jusqu'à ce que
        # prod tourne ce code — elle n'est pas perdue, juste pas encore masquée.
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        # B3 : migrer les liens projet→procédure de slug vers l'id de doctrine (org-owned ;
        # les projets user-owned gardent le slug, résolu à la lecture côté front). Idempotent
        # (guard `!~ '^[0-9]+$'` = pas déjà un id ; JOIN = seulement si la doctrine existe).
        conn.execute("""
            UPDATE project_links pl SET target_ref = oi.id::text
            FROM projects p JOIN org_instructions oi ON oi.org_id = p.owner_id::bigint
            WHERE pl.project_id = p.id AND pl.target_type = 'procedure'
              AND p.owner_type = 'org' AND oi.slug = pl.target_ref
              AND pl.target_ref !~ '^[0-9]+$'
        """)
        # ADR 0035 (B1) : slots de procédure — déclaration d'entités requises (JSON propre),
        # référencées par nom dans la prose (<slot:name>). Transportée par revisions +
        # copy/fork/publish. Canari no-op : aucune résolution runtime avant B3.
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE org_instruction_revisions ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE doctrine_library ADD COLUMN IF NOT EXISTS slots JSONB NOT NULL DEFAULT '[]'::jsonb")
        # Chantier procédures (cadrage 10/07) B1 : la procédure devient une ressource
        # possédée par un SCOPE — colonnes owner_type/owner_id sur la table ET ses
        # revisions ('org' aujourd'hui ; les lignes GROUP arrivent en B2 par fusion
        # d'org_group_instructions, ids tirés d'org_instructions_id_seq). Le nouvel
        # ARBITRE d'unicité est (owner_type, owner_id, slug) : le code écrit dessus dès
        # B1 ; la PK legacy (org_id, slug) ne tombe qu'en B2, une fois ce code PROMU en
        # prod (DB partagée canari/prod — la prod actuelle fait ON CONFLICT (org_id, slug)).
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'org'")
        conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS owner_id TEXT")
        conn.execute("UPDATE org_instructions SET owner_id = org_id::text WHERE owner_id IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_org_instructions_owner_slug "
                     "ON org_instructions(owner_type, owner_id, slug)")
        conn.execute("ALTER TABLE org_instruction_revisions ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'org'")
        conn.execute("ALTER TABLE org_instruction_revisions ADD COLUMN IF NOT EXISTS owner_id TEXT")
        conn.execute("UPDATE org_instruction_revisions SET owner_id = org_id::text WHERE owner_id IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_org_instruction_revisions_owner "
                     "ON org_instruction_revisions(owner_type, owner_id, slug, version)")
        # Chantier procédures B2 (le B1 est PROMU prod — la prod écrit sur l'arbitre
        # owner) : la PK legacy (org_id, slug) tombe → les lignes GROUP peuvent
        # coexister avec les lignes org de la même org parente. Puis FUSION de la
        # jumelle : copie des procédures d'équipe (ids tirés d'org_instructions_id_seq
        # — la MÊME séquence, zéro collision avec les refs project_links/grants), à
        # CHAQUE boot tant que la jumelle existe (newer-wins : rattrape les écritures
        # prod de la fenêtre) ; slots='[]' (colonne absente côté équipe). Le DROP de
        # la jumelle = Lot C, une fois CE code promu (la prod Lot A la lit encore).
        conn.execute("ALTER TABLE org_instructions ALTER COLUMN owner_id SET NOT NULL")
        conn.execute("ALTER TABLE org_instruction_revisions ALTER COLUMN owner_id SET NOT NULL")
        conn.execute("ALTER TABLE org_instructions DROP CONSTRAINT IF EXISTS org_instructions_pkey")
        conn.execute("ALTER TABLE org_instruction_revisions DROP CONSTRAINT IF EXISTS org_instruction_revisions_pkey")
        if conn.execute("SELECT to_regclass('org_group_instructions') AS t").fetchone()["t"]:
            conn.execute("""
                INSERT INTO org_instructions
                    (id, org_id, owner_type, owner_id, slug, title, description,
                     body_md, slots, version, set_by, created_at, updated_at)
                SELECT nextval('org_instructions_id_seq'), og.org_id, 'group',
                       g.group_id::text, g.slug, g.title, g.description, g.body_md,
                       '[]'::jsonb, g.version, g.set_by, g.created_at, g.updated_at
                  FROM org_group_instructions g JOIN org_groups og ON og.id = g.group_id
                ON CONFLICT (owner_type, owner_id, slug) DO UPDATE SET
                    title = EXCLUDED.title, description = EXCLUDED.description,
                    body_md = EXCLUDED.body_md, version = EXCLUDED.version,
                    set_by = EXCLUDED.set_by, updated_at = EXCLUDED.updated_at
                 WHERE EXCLUDED.updated_at > org_instructions.updated_at
            """)
        if conn.execute("SELECT to_regclass('org_group_instruction_revisions') AS t").fetchone()["t"]:
            conn.execute("""
                INSERT INTO org_instruction_revisions
                    (org_id, owner_type, owner_id, slug, version, title, description,
                     body_md, slots, set_by, created_at)
                SELECT og.org_id, 'group', r.group_id::text, r.slug, r.version, r.title,
                       r.description, r.body_md, '[]'::jsonb, r.set_by, r.created_at
                  FROM org_group_instruction_revisions r
                  JOIN org_groups og ON og.id = r.group_id
                ON CONFLICT (owner_type, owner_id, slug, version) DO NOTHING
            """)
        # Lot C (le Lot B est PROMU prod — plus aucun code ne lit/écrit les jumelles) :
        # DROP, juste après la copie finale ci-dessus (même boot → la dernière écriture
        # prod de la fenêtre est rattrapée). Les copies gardées to_regclass restent :
        # no-op permanents, filet des environnements en retard.
        conn.execute("DROP TABLE IF EXISTS org_group_instruction_revisions")
        conn.execute("DROP TABLE IF EXISTS org_group_instructions")
        # ADR 0042 §Convergence des surfaces (28/07) — `user_agent_readme`, DERNIER
        # vestige du vocabulaire « agent readme ». Lot final de la danse : le lot
        # précédent (v1.22.0, PROMU prod) a retiré tout le code qui la touchait — son
        # backfill de boot, le repointage `migrate_sub`, sa DDL — donc un rollback vers
        # lui boote sans elle. Le DROP ne peut plus casser en arrière. Table contrôlée
        # VIDE avant (prod ET preprod : 0 ligne, aucune écriture depuis le gel du 06/07).
        conn.execute("DROP TABLE IF EXISTS user_agent_readme")
        # ADR 0035 (B2) : un lien peut BINDER un slot par NOM — vocabulaire DU PROJET
        # (deux procédures liées partageant `sortie` partagent le binding). Unicité
        # (projet, slot) = zéro ambiguïté par nommage explicite, refusée au link (409).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS slot TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_project_links_slot "
                     "ON project_links(project_id, slot) WHERE slot IS NOT NULL")
        # ADR 0032 §4 amendé (#57) : un projet peut lier N fois le même connecteur, chaque
        # binding distingué par une IDENTITÉ → une ligne par binding. Colonne `identity_ref`
        # (NULL = binding par défaut, rétro-compat), clé élargie NULLS NOT DISTINCT (PG15+ :
        # deux NULL = même binding par défaut, un seul autorisé).
        conn.execute("ALTER TABLE project_links ADD COLUMN IF NOT EXISTS identity_ref TEXT")
        conn.execute("ALTER TABLE project_links DROP CONSTRAINT IF EXISTS project_links_project_id_target_type_target_ref_key")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_project_links_binding "
                     "ON project_links (project_id, target_type, target_ref, identity_ref) NULLS NOT DISTINCT")
        # B3 : l'identité épinglée quitte `config.identity_id` pour la clé de binding
        # `identity_ref` (fin du doublon). config ne garde que instructions_md. Idempotent
        # (les lecteurs legacy reçoivent identity_id re-dérivé de identity_ref, cf. list_project_links).
        conn.execute("""
            UPDATE project_links
            SET identity_ref = config->>'identity_id', config = config - 'identity_id'
            WHERE target_type = 'connecteur' AND identity_ref IS NULL
              AND COALESCE(config->>'identity_id', '') <> ''
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_public_token ON docs(public_token) WHERE public_token IS NOT NULL")
        # ADR 0032 §5/§6 (B3) : un run est rattaché au projet actif gelé à son ouverture.
        conn.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS project_id BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at DESC)")
        # Discriminateur d'événement (ADR 0017, « un seul flux ») : 'mcp' (défaut,
        # cas historique) / 'rest' / 'connector'. Les lignes existantes deviennent
        # 'mcp' par le DEFAULT → les lectures kind='mcp' restent iso (canari no-op).
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'mcp'")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_kind ON tool_calls(kind, created_at DESC)")
        # Corrélation des appels (ADR 0017, extension OTO-LOCALE de tool_calls).
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS session_id TEXT")
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS run_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, created_at) WHERE run_id IS NOT NULL")
        # ⚠️ La CLÔTURE d'un run se retrouve par `args->>'run_id'`, pas par la colonne
        # (`_run_closure` explique pourquoi : `run_finish` n'est pas corrélable, sa
        # colonne `run_id` est vide sur tout l'historique). Sans index d'expression,
        # chaque run reconstruit coûtait un parcours COMPLET du journal — 639 ms et
        # 911 882 lignes filtrées l'unité, mesuré le 2026-08-27 : × 9 350 runs, la
        # lecture tenait la boucle 185 s et gelait la plateforme entière, tenants
        # tiers compris. Avec l'index : 0,05 ms. Partiel (9 k lignes, 624 kB).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_run_finish_ref "
                     "ON tool_calls ((args->>'run_id'), created_at DESC) "
                     "WHERE tool = 'run_finish'")
        # Org de l'appel (#67, scope d'audit exact) — extension OTO-LOCALE.
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS org_id BIGINT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_calls_org ON tool_calls(org_id, created_at DESC) WHERE org_id IS NOT NULL")
        # Application OAuth cliente porteuse du grant (`azp` du JWT — claude.ai,
        # Claude Code, ChatGPT…) : axe de télémétrie par surface, extension OTO-LOCALE.
        conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS client_id TEXT")
        # Résolution des signaux d'usage (ADR 0017) : marquer un feedback/gap traité.
        # NULL = ouvert. resolution = note libre de l'opérateur (ce qui a été fait).
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolved_by TEXT")
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS resolution TEXT")
        # ARBITRAGE en 4 états (#450) — deux ne suffisaient pas : « ouvert »
        # confondait le non-lu et le lu-sans-décision, et refuser était indicible.
        # Additive et idempotente : la colonne naît à 'open', le backfill donne
        # 'resolved' à ce que l'ancien modèle appelait résolu. Aucune ligne ne change
        # de SENS au passage — c'est la même information, enfin nommée (vérifié sur la
        # base servie avant le lot : 0 ligne où `status='resolved'` contredirait
        # `resolved_at IS NOT NULL`).
        #
        # ⚠️ Le `AND status = 'open'` du backfill n'est PAS une simple garde
        # d'idempotence : il rend la migration AUTO-RÉPARATRICE, et c'est ce qui
        # permet de livrer en preprod avant le tag prod. Prod et preprod partagent la
        # base : pendant la fenêtre où la prod tourne encore sur l'ancien code, une
        # résolution qu'elle écrit pose `resolved_at` sans toucher `status` — le
        # signal paraîtrait ouvert. Le boot suivant le rattrape. La clause protège
        # aussi ce qu'elle ne doit pas toucher : `acknowledged` et `declined` portent
        # eux aussi une date d'arbitrage, et un backfill sans elle les promouvrait
        # tous en « traité » — un refus deviendrait un traitement, silencieusement.
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open'")
        conn.execute("UPDATE usage_signals SET status = 'resolved' "
                     "WHERE resolved_at IS NOT NULL AND status = 'open'")
        # La contrainte APRÈS le backfill : posée avant, elle passerait quand même
        # (le défaut est valide) mais l'ordre dit l'intention — on ne contraint
        # jamais une colonne dont on n'a pas encore réparé le contenu.
        conn.execute("DO $$ BEGIN "
                     "IF NOT EXISTS (SELECT 1 FROM pg_constraint "
                     "WHERE conname='usage_signals_status') THEN "
                     "ALTER TABLE usage_signals ADD CONSTRAINT usage_signals_status "
                     "CHECK (status IN ('open','acknowledged','declined','resolved')); "
                     "END IF; END $$")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_signals_status "
                     "ON usage_signals(status, created_at DESC)")
        # Le RETOUR à celui qui a signalé (#451). Les 331 signaux DÉJÀ arbitrés avant
        # ce lot sont marqués comme annoncés : leurs auteurs n'ont effectivement rien
        # reçu, mais les rattraper enverrait des nouvelles de décisions vieilles de
        # deux mois — et en masse, puisque 3 personnes portent 168 des 204 signaux
        # encore ouverts (dont 2 externes, à 51 et 53). Le retour vaut pour ce qu'on
        # arbitre À PARTIR D'ICI. Le rattrapage voulu — un récapitulatif par personne
        # sur la pile en cours — n'a pas besoin d'un mode à part : il tombe du
        # REGROUPEMENT, une fois la pile arbitrée.
        conn.execute("ALTER TABLE usage_signals ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ")
        # ⚠️ BORNÉ DANS LE TEMPS, et la borne est le cœur du correctif. Sans elle
        # ce rattrapage re-tourne à CHAQUE démarrage et marque « déjà annoncé » tout
        # ce qui vient d'être arbitré sans avoir encore été envoyé — il a mangé les
        # 62 premiers retours réels, en silence, entre l'arbitrage et l'envoi. Un
        # backfill est un geste UNIQUE : il doit se reconnaître à ce qu'il rattrape,
        # pas à l'état où il met les lignes, sinon il devient une purge permanente.
        # 2026-08-20 : au-dessus du dernier arbitrage historique (15/08) et sous le
        # premier arbitrage de ce lot (27/08 19:59) — la frontière est franche, il
        # n'y a rien entre les deux.
        conn.execute("UPDATE usage_signals SET notified_at = resolved_at "
                     "WHERE resolved_at IS NOT NULL AND notified_at IS NULL "
                     "AND resolved_at < TIMESTAMPTZ '2026-08-20 00:00:00+00'")
        # Unipile revendeur (org_id porté au compte + plafond par org).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS org_id BIGINT REFERENCES orgs(id) ON DELETE SET NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_unipile_accounts_org ON unipile_accounts(org_id)")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS org_id BIGINT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS unipile_account_limit INTEGER")
        # Multi-canal Unipile : un account_id par (sub, provider). Migration de la
        # PK sub → (sub, provider) ; les lignes existantes prennent 'LINKEDIN' (DEFAULT).
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        # ⚠️ Le cycle de vie du PK d'unipile_accounts appartient à
        # db.backfill_unipile_member_scope() (ADR 0033 B4) — l'ex re-pose
        # inconditionnelle du PK (sub, provider) écraserait la migration.
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS platform_seat BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS platform_seat BOOLEAN NOT NULL DEFAULT FALSE")
        # Horodatage du dernier sync du feed (miroir home, datastore linkedin-feed) :
        # gouverne la fraîcheur du cache (TTL) côté unipile_feed. NULL = jamais sync.
        conn.execute("ALTER TABLE unipile_accounts ADD COLUMN IF NOT EXISTS feed_synced_at TIMESTAMPTZ")
        conn.execute("ALTER TABLE unipile_pending ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'LINKEDIN'")
        # ADR 0044 (B0) : l'entrée du coffre devient une INSTANCE de connecteur (config
        # possédée). Colonnes DORMANTES — non lues par la résolution avant B2/B3 (canari
        # additif). `version` = verrou optimiste (B1) ; `share_down` = ouverture au
        # sous-arbre, mono-scope ; `share_side` = prêts nominatifs à des pairs.
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1")
        # share_down = ALLOWLIST deny-by-default ([] = ouvert au sous-arbre ; restreint aux
        # scopes listés) ; share_side = EXTENSION (prêts nominatifs à des pairs). Dormantes
        # jusqu'à l'enforcement (deny-check cascade + garde pin).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_down JSONB NOT NULL DEFAULT '[]'::jsonb")
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_side JSONB NOT NULL DEFAULT '[]'::jsonb")
        # ADR 0044 §F : polarité de partage explicite. 'open' = share_down vide → ouvert au
        # sous-arbre (comportement historique, défaut inchangé pour tous les scopes BYO) ;
        # 'closed' = share_down vide → personne (allow-list stricte, requise par le scope
        # plateforme dont le « sous-arbre » = tout le monde). Additif, no-op tant que 'open'.
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS share_mode TEXT NOT NULL DEFAULT 'open'")
        # GIN sur share_side pour la projection « partagé avec moi » (jsonb_exists_any /
        # `?|` = scan indexé au lieu d'un seq scan de tout le coffre).
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conn_cred_share_side ON connector_credentials USING gin (share_side)")
        # Retrait du rôle `guest` (2026-06-15) : défaut → member + migration des
        # lignes existantes (guest était un alias sans effet, cf. access/scope.py).
        conn.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'member'")
        conn.execute("UPDATE users SET role = 'member' WHERE role = 'guest'")
        # Retrait de la waitlist alpha + referral (ADR 0013 supersédé) : inscription
        # libre, tout compte est actif d'office. On droppe les colonnes d'accès/quota
        # (idempotent). L'org_id de org_invitations reste nullable (héritage) ; les
        # invitations vivantes ciblent toutes une org.
        conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS access_status")
        conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS invite_quota")
        conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS invited_by")
        conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS access_granted_at")
        conn.execute("DROP INDEX IF EXISTS idx_users_referral_code")
        conn.execute("ALTER TABLE users DROP COLUMN IF EXISTS referral_code")
        # Invitation d'org : org_id nullable + source + code court partageable à la
        # main (idempotent pour les DB créées avant). email nullable (émission sans
        # envoi mail = code à partager soi-même).
        conn.execute("ALTER TABLE org_invitations ALTER COLUMN org_id DROP NOT NULL")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS source TEXT")
        conn.execute("ALTER TABLE org_invitations ALTER COLUMN email DROP NOT NULL")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS code TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_org_invitations_code "
                     "ON org_invitations(code) WHERE code IS NOT NULL")
        # Invitation = feature cascade plateforme/org/équipe : le scope est dérivé des
        # cibles (org_id NULL = plateforme, org_id seul = org, org_id+group_id = équipe).
        # `org_groups` existe déjà (créée par _SCHEMA ci-dessus) → la FK passe. Ajout via
        # ALTER (comme `code`), pas dans _SCHEMA (ordre de création des tables).
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS group_id BIGINT "
                     "REFERENCES org_groups(id) ON DELETE CASCADE")
        conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS group_role TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_org_invitations_group "
                     "ON org_invitations(group_id) WHERE group_id IS NOT NULL")
        # Primitive de ressource possédée (ADR 0030) : scope d'ownership porté par la
        # ressource (`owner_type` défaut 'user', `owner_id` = sub | org.id | group.id).
        # Phase H (cadrage 10/07) — B1 (promu prod 10/07) a purgé toute référence aux
        # reliques du cutover ; **B2** (ci-dessous) droppe les objets morts. Idempotent,
        # sûr sur la DB partagée : plus aucun code (canari NI prod) ne les lit.
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS owner_type TEXT NOT NULL DEFAULT 'user'")
        conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS owner_id TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_datastores_owner "
                     "ON user_datastores(owner_type, owner_id)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_user_datastores_owner_ns "
                     "ON user_datastores(owner_type, owner_id, namespace)")
        # Phase H B2 : DROP des reliques per-sub/Sheets (ADR 0016/0030, différé depuis
        # le cutover). `datastore_shares` = remplacée par resource_grants (backfillée
        # à chaque boot depuis 0030, données longtemps migrées).
        conn.execute("ALTER TABLE user_datastores DROP COLUMN IF EXISTS sub")
        conn.execute("ALTER TABLE user_datastores DROP COLUMN IF EXISTS spreadsheet_id")
        conn.execute("ALTER TABLE user_datastores DROP COLUMN IF EXISTS owner_email")
        conn.execute("DROP TABLE IF EXISTS datastore_shares")
        # ADR 0048 — le grant possède un RÔLE (viewer/editor/manager). Ajout de la colonne
        # + backfill depuis `permission` (read→viewer, write→editor ; jamais manager en
        # backfill : la gouvernance grantable est un acte explicite). `permission` reste
        # dérivée du rôle à l'écriture (grant_resource) — le plan contenu est inchangé.
        conn.execute("ALTER TABLE resource_grants ADD COLUMN IF NOT EXISTS role TEXT "
                     "NOT NULL DEFAULT 'editor'")
        conn.execute("UPDATE resource_grants SET role = "
                     "CASE permission WHEN 'read' THEN 'viewer' ELSE 'editor' END "
                     "WHERE role NOT IN ('viewer', 'manager')")
        # Préférence de langue de l'UI dashboard (2026-07-07) : NULL = pas de
        # préférence explicite (le front retombe sur la langue du navigateur).
        # Validée à 'en'|'fr' en amont (capacité me.locale.set) ; colonne libre.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS locale TEXT")
        # Avatar utilisateur + logo d'org (2026-06-16) : URL publique (Scaleway
        # Object Storage), pas un secret → colonne en clair, hors coffre.
        conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS logo_url TEXT")
        # Description libre de l'org (self-service org_admin) — prose, pas un secret.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT ''")
        # Profil d'org (2026-07-02) : donner du corps à l'entreprise. `domain` =
        # domaine de marque (acme.com, normalisé org_store._normalize_domain) —
        # sert AUSSI à dériver le logo via logo.dev quand aucun logo n'est uploadé
        # (org_store.effective_logo_url, même CDN que le catalogue connecteurs).
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS domain TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS industry TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS location TEXT NOT NULL DEFAULT ''")
        # Baseline de toolset par org (ex-ADR 0015) RETIRÉE — les presets de tools
        # ont été supprimés : drop de la colonne si présente (idempotent).
        conn.execute("ALTER TABLE orgs DROP COLUMN IF EXISTS default_tools")
        # Baseline de connecteurs proposés par l'org (ADR 0019, B2) : liste de
        # connecteurs recommandés (« org propose »). NULL = pas de baseline.
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
        # invitations, groupes) restent intactes pour audit/restauration.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ")
        # Org PERSO (suppression du perso) : `personal_of` = sub dont c'est l'espace
        # privé mono-membre (NULL = org partagée). Unicité : 1 org perso par user.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS personal_of TEXT")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_orgs_personal_of "
                     "ON orgs(personal_of) WHERE personal_of IS NOT NULL")
        # MFA obligatoire par org (voie « org Logto miroir », ADR 0044/sécu-auth).
        #   require_mfa   = l'org impose le 2ᵉ facteur à ses membres (toggle org_admin).
        #   logto_org_id  = l'organization Logto MIROIR créée derrière l'org quand le
        #                   MFA est activé (isMfaRequired=true + membres synchronisés
        #                   par sub) ; NULL tant que le MFA n'est pas activé.
        # Source de vérité (org, membres, droits) = CE PG ; l'org Logto n'est qu'un
        # miroir d'enforcement MFA au login (aucune autorité). Voir docs/auth-logto.md.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS require_mfa BOOLEAN NOT NULL DEFAULT FALSE")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS logto_org_id TEXT")
        # Front qui héberge l'org — oto-backend sert PLUSIEURS produits depuis une
        # instance (oto, Tulina). NULL = oto (le défaut, l'écrasante majorité) ; posé
        # = l'org vit sous un front tiers, dont les liens sortants et la marque des
        # mails doivent porter SES couleurs, pas les nôtres :
        #   front_base_url = base des liens publics (https://app.tulina.ai)
        #   front_brand    = marque écrite dans les mails ("tulina")
        # Dérivé de l'org, JAMAIS déclaré par l'appelant : une invitation ne peut pas
        # prétendre venir d'un front auquel l'org n'appartient pas.
        # ⚠️ Provisoire assumé : cette information appartient au TENANT (ADR 0052),
        # pas à chaque org. Tenable tant que les orgs sous front tiers se comptent sur
        # les doigts ; à la migration, ces deux colonnes remontent d'un cran et les
        # lignes se vident — le code qui les lit ne bouge pas.
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS front_base_url TEXT")
        conn.execute("ALTER TABLE orgs ADD COLUMN IF NOT EXISTS front_brand TEXT")
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
            for _t in ("user_disabled_tools", "user_enabled_tools"):
                conn.execute(f"ALTER TABLE {_t} ADD COLUMN org_id BIGINT NOT NULL DEFAULT 0")
                conn.execute(f"ALTER TABLE {_t} DROP CONSTRAINT IF EXISTS {_t}_pkey")
            conn.execute("ALTER TABLE user_disabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
            conn.execute("ALTER TABLE user_enabled_tools ADD PRIMARY KEY (sub, org_id, tool_name)")
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
        # Suppression du perso : les profils de visibilité `org_id=0` (perso/global)
        # ont été copiés vers l'org active ci-dessus et ne sont plus jamais relus
        # (`session_visibility` lit l'org active, toujours posée). Purge des orphelins
        # (idempotent : no-op une fois vide ; plus aucune écriture en org_id=0).
        for _t in ("user_disabled_tools", "user_enabled_tools"):
            conn.execute(f"DELETE FROM {_t} WHERE org_id = 0")
        # Presets de tools (snapshots nommés) RETIRÉS : drop de la table si présente.
        conn.execute("DROP TABLE IF EXISTS user_presets")
        # Baseline de toolset d'équipe (ex-ADR 0012) RETIRÉE avec les presets : drop.
        conn.execute("ALTER TABLE org_groups DROP COLUMN IF EXISTS default_tools")
        # Coffre chiffré : colonnes courantes (idempotent pour les DB créées avant).
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS secret_enc TEXT")
        conn.execute("ALTER TABLE connector_credentials ADD COLUMN IF NOT EXISTS account TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE connector_credentials DROP CONSTRAINT IF EXISTS connector_credentials_pkey")
        conn.execute("ALTER TABLE connector_credentials ADD PRIMARY KEY (entity_type, entity_id, connector, account)")
        # TTL opt-in des tokens API (audit 2026-06-13) : NULL = non-expirant.
        conn.execute("ALTER TABLE user_api_tokens ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
        # Portée opt-in d'un jeton API (`token_scopes.py`) : NULL = jeton non porté
        # (pleins pouvoirs du sub) → additif pur, aucun jeton existant n'est touché.
        conn.execute("ALTER TABLE user_api_tokens ADD COLUMN IF NOT EXISTS scopes JSONB")
        _drop_legacy_plaintext_stores(conn)
        # Décommission du substrat « fact graph » (ex-ADR 0008/0027) : le schéma
        # factgraph et toutes ses tables sont supprimés (idempotent). Plus aucune
        # capacité/outil/vue ne s'y adosse.
        conn.execute("DROP SCHEMA IF EXISTS factgraph CASCADE")
        # Cran d'activation des connecteurs (ADR 0010, B1) — table + seed unique
        # (snapshot du registre courant à ON). Aucun lecteur encore (canari) :
        # le câblage catalogue/chargement suit en B2/B3.
        from ..connectors import activation as _conn_act
        _conn_act.init_schema(conn)
        _conn_act.seed_initial(conn)
        # Chantier ACL (cadrage 10/07, B1) : copie legacy → `connector_acl` unifiée,
        # à CHAQUE boot tant que les tables legacy existent (fenêtre canari/prod :
        # la prod écrit encore les legacy jusqu'à promotion). Gardée `to_regclass` :
        # après le DROP (B2), no-op. Grants immutables → DO NOTHING suffit (une
        # révocation prod pendant la fenêtre ressuscite jusqu'à promotion — assumé,
        # fenêtre de quelques minutes).
        if conn.execute("SELECT to_regclass('org_connector_access') AS t").fetchone()["t"]:
            conn.execute(
                "INSERT INTO connector_acl (scope_type, scope_id, connector, "
                "                           principal_type, principal_id, granted_by, granted_at) "
                "SELECT 'org', org_id::text, connector, principal_type, principal_id, "
                "       granted_by, granted_at FROM org_connector_access "
                "ON CONFLICT DO NOTHING")
        if conn.execute("SELECT to_regclass('group_connector_access') AS t").fetchone()["t"]:
            conn.execute(
                "INSERT INTO connector_acl (scope_type, scope_id, connector, "
                "                           principal_type, principal_id, granted_by, granted_at) "
                "SELECT 'group', group_id::text, connector, 'user', principal_sub, "
                "       granted_by, granted_at FROM group_connector_access "
                "ON CONFLICT DO NOTHING")
        # Chantier ACL B2 (le B1 est PROMU prod — plus AUCUN code ne lit les 4 tables
        # legacy, les copies ci-dessus sont gardées to_regclass) : DROP. Dernière
        # copie exécutée juste avant, même boot — rien ne se perd.
        conn.execute("DROP TABLE IF EXISTS org_connector_access")
        conn.execute("DROP TABLE IF EXISTS group_connector_access")
        conn.execute("DROP TABLE IF EXISTS connector_activation")
        conn.execute("DROP TABLE IF EXISTS group_connector_activation")
        # Sélection de connecteurs par membre (ADR 0019, B1) — table seule, aucun
        # lecteur encore (canari, no-behavior-change) ; le câblage lecture/mutation
        # (capacité connectors.me/select/pause) et le masquage pause au middleware
        # suivent en B3/B4/B5.
        from ..connectors import selection as _conn_sel
        _conn_sel.init_schema(conn)
        # ADR 0050 : passage au régime nominal « non-sélectionné = masqué ».
        # Backfill ONE-SHOT (sentinelle) des (sub, org) pré-existants avec ce
        # qu'ils VOYAIENT (exposé − ex-default_hidden) — zéro changement de
        # toolbox pour l'existant ; les pairs créés ensuite reçoivent le SOCLE
        # curé au seed lazy (session_visibility).
        _conn_sel.backfill_preexisting(conn)
        # ⚠️ **`rename_selection(conn, "linkedin", "aiark")` A ÉTÉ RETIRÉ ICI le
        # 2026-08-28, et le retirer était OBLIGATOIRE — pas un nettoyage.**
        #
        # Ce geste datait du 2026-08-10 : `linkedin` (AI Ark en app-credits) était
        # déposé au profit d'`aiark`, et #295 avait appris qu'une sélection restée
        # sur un nom mort fait disparaître la toolbox en silence. Il a fait son
        # travail — en prod, plus une ligne ne porte `linkedin` de ce côté-là.
        #
        # Mais `linkedin` n'est plus un nom mort : depuis aujourd'hui, c'est la
        # SESSION hébergée, et le fan-out plus bas CRÉE des lignes `linkedin` à
        # chaque boot. Laisser le renommage, c'était donc programmer une bombe à
        # retardement d'un boot de décalage : le boot N crée les sélections
        # LinkedIn, le boot N+1 les déplace toutes vers `aiark` avant que le
        # fan-out ne les recrée… et ainsi de suite, chaque redémarrage rejouant le
        # déménagement. Une migration de boot n'est sûre QUE tant que son nom source
        # reste mort ; reprendre un nom déposé oblige à relire toutes celles qui le
        # nomment. Tripwire : `tests/connectors/test_connector_selection_rename.py`.
        #
        # `aiark` n'a besoin d'AUCUN renommage : le connecteur existe toujours, il a
        # seulement perdu son préfixe `linkedin_` de tools (rebranding annulé). Le
        # NOM de connecteur, lui, n'a jamais changé — les sélections sont donc déjà
        # au bon endroit.
        # --- SPLIT `unipile` → le compte + ses six CONNEXIONS (2026-08-28) -------
        # Le connecteur `unipile` portait sept namespaces : le sien et les six
        # canaux hébergés. Chacun est désormais un connecteur à part entière, ce qui
        # lui donne activation, ACL, sélection et visibilité PROPRES — mais le rend
        # aussi INCONNU de toutes les tables de gouvernance, où seul `unipile`
        # existe. Trois fan-out, dans cet ordre, et chacun corrige un fail-* qui
        # penche du mauvais côté :
        #   · availability — un connecteur sans ligne platform est OFF
        #     (deny-by-default) : sans ce geste, la messagerie hébergée s'éteint
        #     pour TOUT LE MONDE au premier boot du split ;
        #   · ACL          — une ACL vide est OUVERTE (ADR 0025) : sans ce geste,
        #     une org qui avait réservé la messagerie à une équipe l'ouvre à tous ;
        #   · sélection    — non-sélectionné = masqué (ADR 0050) : sans ce geste,
        #     les membres qui avaient installé unipile perdent la surface.
        # Les trois sont idempotents (ON CONFLICT DO NOTHING) et ne TOUCHENT PAS
        # `unipile`, qui survit comme compte fournisseur — c'est ce qui distingue un
        # split d'un renommage, et pourquoi `rename_selection` ne convenait pas.
        #
        # Les tables de COMPTES (`unipile_accounts`, `connector_account_grants`,
        # `unipile_operated_accounts`, `unipile_pending`) ne bougent PAS : leur
        # colonne `provider` a toujours porté le CANAL (LINKEDIN/WHATSAPP/…), jamais
        # le connecteur. Le split les rejoint, il ne les migre pas.
        from ..connectors import activation as _conn_act_split
        _CANAUX_UNIPILE = ("linkedin", "whatsapp", "telegram",
                           "instagram", "messenger", "twitter")
        # ⚠️ AVANT le fan-out : purger la gouvernance FOSSILE de l'ancien `linkedin`.
        # Le connecteur `linkedin` déposé le 10/08 (données achetées AI Ark) a laissé
        # ses lignes d'exposition et d'ACL en base — rien ne nettoie un connecteur
        # déposé. Elles dormaient tant que le nom restait mort ; le nom revient
        # aujourd'hui à la session hébergée, et le fan-out pose ses lignes en
        # ON CONFLICT DO NOTHING (à dessein : un réglage d'admin déjà pris gagne).
        # La ligne fossile GAGNERAIT donc : une org qui avait coupé l'AI Ark-LinkedIn
        # verrait la session LinkedIn coupée, sans que personne ne l'ait décidé et
        # sans que rien n'échoue. One-shot (marque en base) — rejouée, la purge
        # effacerait les décisions prises DEPUIS sur le nouveau connecteur.
        _conn_act_split.purge_connecteur_depose(conn, "linkedin")
        _conn_act_split.fanout_availability(conn, "unipile", _CANAUX_UNIPILE)
        _conn_act_split.fanout_acl(conn, "unipile", _CANAUX_UNIPILE)
        _conn_sel.fanout_selection(conn, "unipile", _CANAUX_UNIPILE)
        # Proposition d'org (`orgs.default_connectors`, consultatif) : une org qui
        # RECOMMANDAIT unipile recommande ses canaux. `array_cat` + déduplication,
        # gardé sur la présence de `unipile` → rejeu sans effet.
        conn.execute(
            "UPDATE orgs SET default_connectors = ("
            "  SELECT ARRAY(SELECT DISTINCT unnest(default_connectors || %s::text[]))"
            ") WHERE default_connectors @> ARRAY['unipile']::text[] "
            "   AND NOT default_connectors @> %s::text[]",
            (list(_CANAUX_UNIPILE), list(_CANAUX_UNIPILE)),
        )
        # === Lot M2 (blueprint ADR 0054/0063, #287) : projets et pages → NŒUDS ===
        # PLACÉ EN FIN DE TRANSACTION, et c'est la même règle qu'au lot M1 : la
        # conversion doit suivre TOUTE écriture de sa table source dans CE boot.
        # Ici la liste est longue — `projects` et `docs` gagnent leurs colonnes par
        # `ALTER` plus haut (`icon`, `context_org_id`, `is_template`, `description`,
        # `position`, `public_token`) et `docs.position` reçoit même un backfill.
        # Convertir avant, c'est lire des colonnes qui n'existent pas encore sur une
        # base ancienne (boot KO) ou projeter un état que le boot vient de corriger.
        #
        # Copie legacy→cible à CHAQUE boot, gardée `to_regclass`
        # (docs/live-migrations.md) : `projects`/`docs` restent la source de vérité
        # et la cible des écritures de ce lot — la conversion est une PROJECTION,
        # pas une bascule. Purement ADDITIF : rien n'est modifié ni supprimé côté
        # legacy, la prod (qui tourne l'ancien code sur CETTE MÊME base) ne voit
        # strictement rien. Le lot se défait en retirant ces deux appels.
        #
        # Ce que la conversion fait disparaître, au-delà du déménagement de lignes :
        # **le projet en tant qu'objet** (0054-D5). Il devient une ÉPINGLE — un
        # drapeau `props->>'pinned'` sur un nœud ordinaire, dont le brief est le
        # corps et dont les pages seront l'arbre. Détail : `db/nodes.py`.
        #
        # ⚠️ L'ORDRE des deux conversions n'est pas cosmétique : une page de premier
        # niveau se rattache au NŒUD de son projet — s'il n'existe pas encore, la
        # jointure ne rend rien et la page reste orpheline jusqu'au boot suivant.
        # Un arbre à moitié posé, qu'aucune erreur ne signale.
        if conn.execute("SELECT to_regclass('projects') AS t").fetchone()["t"]:
            from .nodes import (convert_docs, convert_doctrines, convert_projects,
                                convert_rows, convert_tables)
            convert_projects(conn)
            if conn.execute("SELECT to_regclass('docs') AS t").fetchone()["t"]:
                convert_docs(conn)
            # === Lot ⑧ : les PROCÉDURES → nœuds ===
            # Indépendante des trois autres — une procédure n'a ni parent ni enfant
            # dans l'arbre, elle est possédée par un scope et c'est tout. D'où sa
            # place ici, sans contrainte d'ordre : rien ne la lit, rien ne dépend
            # d'elle.
            #
            # Elle ferme un trou VISIBLE depuis la naissance du rail : un partage
            # direct de procédure ne désignait aucun nœud, donc n'entrait pas dans
            # la section « Partagé ». On le comptait plutôt que de le taire — cette
            # conversion fait tomber ce compteur à zéro.
            #
            # ⚠️ Son propre garde : `org_instructions` peut ne pas exister sur une
            # base fraîche, et sa colonne `id` est posée par une migration de CE
            # module — la conversion filtre donc `id IS NOT NULL` plutôt que de
            # supposer que le backfill a déjà tourné.
            if conn.execute(
                    "SELECT to_regclass('org_instructions') AS t").fetchone()["t"]:
                convert_doctrines(conn)
            # === Lot M3 (#301) : les TABLEAUX → nœuds-tableaux ===
            # Le namespace devient une POSITION dans l'arbre (0054-D4) : sous le
            # nœud du projet qui lie le tableau, sinon à la racine de son
            # propriétaire — d'où la place SOUS `convert_projects`, dont il lit les
            # nœuds. Et le schéma de colonnes descend dans `props` : c'est la
            # dimension de 0054-D4, ce qui fait d'un nœud un tableau.
            #
            # ⚠️ Sous le MÊME garde `projects` que les pages, et ce n'est pas un
            # raccourci : le rattachement se résout par `project_links`, qui meurt
            # avec `projects` (clé étrangère CASCADE). Le jour où ces tables
            # disparaissent, il n'y a plus de rattachement legacy à projeter — la
            # place d'un tableau vivra dans `nodes`, et cette conversion n'aura
            # plus d'objet.
            #
            # === Lot M4 (#308) : les LIGNES → nœuds-lignes ===
            # Le volume, en dernier (0063-D4) : 43 584 lignes, soixante fois tout le
            # reste du contenu réuni. Sous le MÊME garde que les tableaux, et
            # immédiatement APRÈS eux — une ligne se rattache au nœud de son tableau
            # par une jointure interne, donc un tableau non encore converti fait
            # simplement disparaître ses lignes de la projection de ce boot-ci.
            #
            # ⚠️ Le **bail** de la file de travail ne bouge toujours pas : il vit sur
            # `datastore_rows`, qui reste la table de vérité jusqu'à la bascule de
            # lecture (0063-D3). La projection ne le copie pas — un bail change sans
            # passer par un boot, une colonne projetée mentirait entre deux.
            if conn.execute(
                    "SELECT to_regclass('user_datastores') AS t").fetchone()["t"]:
                convert_tables(conn)
                convert_rows(conn)
                # L'index d'ownership NU cède la place au partiel posé par `_SCHEMA`
                # juste avant (`idx_nodes_owner_scoped`) — sans ce DROP, la base
                # porterait les DEUX et paierait quand même le volume, ce qui vide
                # le lot M-f de son objet. Après la conversion et pas avant : c'est
                # l'ordre qui rend le remplacement invisible en production.
                #
                # Sûr malgré la base partagée avec la prod (docs/live-migrations.md) :
                # la seule requête qui l'utilisait porte `kind = 'page'`, donc le
                # partiel la couvre — vérifié au plan, pas supposé. L'autre lecture
                # d'ownership de `nodes` (`db/aux_embed`) joint par clé primaire et
                # ne s'en sert pas.
                conn.execute("DROP INDEX IF EXISTS idx_nodes_owner")
        # === Lot L5 (blueprint ADR 0053) : les grants de clé plateforme deviennent
        # === des ARÊTES, un connecteur à la fois.
        # EN FIN DE TRANSACTION, comme les conversions M2/M3 et pour la même raison :
        # la migration LIT `connector_credentials`, que ce boot vient de faire évoluer
        # (colonnes du coffre, PK recomposée plus haut). Additive et idempotente — elle
        # ne fait qu'INSÉRER dans `grants` ; aucune ligne du coffre n'est touchée, donc
        # la prod qui tourne l'ancien code sur CETTE MÊME base ne voit rien.
        _seed_platform_grants_as_edges(conn)
        # === Lot L6 (blueprint ADR 0053-D9 · R1 tranché le 27/08 : « la table à
        # === côté ») : chaque ligne de coffre reçoit une INSTANCE, donc un id stable.
        # DERNIER, et pour la raison des conversions M2/M3 et de L5 : il LIT
        # `connector_credentials`, que ce boot vient de faire évoluer. Purement
        # ADDITIF — il n'INSÈRE que dans `connector_instances`, aucune ligne du
        # coffre n'est touchée, aucun secret déchiffré, l'AAD ne bouge pas ⟹ la prod
        # qui tourne l'ancien code sur CETTE MÊME base ne voit rien. **Rien ne lit
        # encore ces instances côté résolution** : l'existant est NOMMÉ, pas déplacé.
        posees = connector_instances.name_vault_rows_as_instances(conn)
        if posees:
            logger.info("L6 instances: %d instance(s) nommée(s) — inventaire %s",
                        posees, connector_instances.connector_instance_counts(conn))
    # Lot M2 : le corps des nœuds se parse en BLOCS (0054-D2/0063-D2). HORS de la
    # transaction de schéma, et pour deux raisons : le parse lit les nœuds que la
    # conversion vient de COMMITER, et c'est du Python sur du texte — pas du DDL.
    # Fail-open : ces blocs ne sont lus par aucune surface, faire tomber un boot de
    # production pour un markdown biscornu serait hors de proportion. No-op dès que
    # rien n'a changé (marqueur `props->>'blocks_md5'`, filtré en SQL).
    try:
        from .blocks import backfill_node_blocks
        parsed = backfill_node_blocks()
        if parsed:
            logger.info("blocs : %d nœud(s) parsé(s)", parsed)
    except Exception as e:
        logger.warning("backfill_node_blocks failed: %s", e)
    # Borne la volumétrie du journal de monitoring (hors transaction schéma).
    try:
        from .usage import prune_tool_calls
        prune_tool_calls(int(os.environ.get("OTO_MCP_CALL_LOG_RETENTION_DAYS", "30")))
    except Exception as e:
        logger.warning("prune_tool_calls failed: %s", e)
    # Le FIL des runs hébergés naît AVEC sa purge (chantier runner R1, ADR 0064-D3 :
    # l'effacer n'ampute pas le run — le journal porte l'audit). Rétention distincte
    # du journal : le fil est un état d'exécution, pas une vérité.
    try:
        from .run_thread import prune_run_messages
        n_fil = prune_run_messages(int(os.environ.get("OTO_MCP_RUN_THREAD_RETENTION_DAYS", "30")))
        if n_fil:
            logger.info("fil des runs : %d tour(s) purgé(s)", n_fil)
    except Exception as e:
        logger.warning("prune_run_messages failed: %s", e)
    # #109 ch.3 : matérialise les clés métier déclarées en contrainte (hors
    # transaction schéma — DDL par namespace, fail-open).
    _ensure_datastore_key_indexes()


def _ensure_datastore_key_indexes() -> None:
    """#109 ch.3 — pour chaque namespace dont le schéma déclare `key`, pose l'index
    UNIQUE partiel s'il manque, en résorbant D'ABORD les doublons hérités (merge
    chronologique dans la row la plus ancienne = ce que l'upsert applicatif aurait
    produit sans les courses ; les doublons SONT des artefacts du bug d'unicité).
    Fail-open PAR namespace : un tableau récalcitrant est loggé, ne bloque ni le
    boot ni les autres (son chemin d'écriture reste l'applicatif historique)."""
    from . import datastore as ds
    try:
        targets = ds.datastore_namespaces_with_key()
    except Exception:
        logger.warning("key-index migration: énumération échouée", exc_info=True)
        return
    for ns in targets:
        try:
            if ds.datastore_has_key_index(ns["id"]):
                continue
            removed = ds.datastore_merge_key_duplicates(ns["id"], ns["key"])
            ds.datastore_ensure_key_index(ns["id"], ns["key"])
            if removed:
                logger.info("key-index ns=%s key=%s : %d doublon(s) résorbé(s)",
                            ns["id"], ns["key"], removed)
        except Exception:
            logger.warning("key-index migration ns=%s : échec (fail-open)",
                           ns.get("id"), exc_info=True)


def _drop_legacy_org_subscriptions(conn: psycopg.Connection) -> None:
    """org_subscriptions du modèle Stripe (retiré par oto-backend#82 le
    2026-07-01 — le code est parti, la table est restée en prod) : forme
    incompatible avec l'ADR 0043 (PK (org_id, product), colonnes stripe_*) et
    données mortes avec le modèle → DROP, _SCHEMA recrée la forme 0043.
    Détection par la colonne `stripe_subscription_id`, jamais présente dans la
    forme 0043 — idempotent, no-op sur une table déjà migrée ou absente.
    Vécu 2026-07-06 : boot KO `column "next_billing_at" does not exist`
    (l'index partiel 0043 sur la table legacy), rollback auto du deploy."""
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'org_subscriptions' "
        "AND column_name = 'stripe_subscription_id'"
    ).fetchone()
    if row:
        conn.execute("DROP TABLE org_subscriptions")
        logger.warning(
            "org_subscriptions legacy (modèle Stripe, #82) droppée — "
            "recréée à la forme ADR 0043 par _SCHEMA"
        )


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
    # connector_credentials.secret (plaintext interne du coffre)
    conn.execute("ALTER TABLE connector_credentials DROP COLUMN IF EXISTS secret")
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


def _seed_platform_grants_as_edges(conn: psycopg.Connection) -> None:
    """Lot L5 (blueprint ADR 0053) — chaque grant de clé plateforme existant devient
    une ARÊTE, pour les seuls connecteurs basculés (`grants_chain.CHAIN_CONNECTORS`).

    **Le mapping, champ par champ** (source = la ligne du coffre scope PLATFORM) :

    | ancien | nouveau |
    |---|---|
    | `entity_type/entity_id/connector` | `resource_id = 'platform:<label>:<connector>'` |
    | (le propriétaire, implicite) | `grantor = platform/platform` (0053-D3) |
    | une entrée de `share_down` **ou** une clé de `meta.rate_limit_by` | `grantee_kind/grantee_id` |
    | `rate_limit_by[scope]`, à défaut `meta.rate_limit` | `constraints = {'quota': N}` |
    | (rien) | `parent_id = NULL` — racine du propriétaire, chaîne de profondeur 1 |
    | (rien) | `source = 'manual'` — un humain les a posés ; le réconciliateur billing
      (L9) ne touche QUE ses propres grants (0053-D6), celui-ci ne les reprendra donc pas |

    **Les deux sources de scopes, et pourquoi les deux.** `share_down` porte les
    grants d'une clé FERMÉE ; `rate_limit_by` porte les quotas — y compris ceux d'une
    clé free-tier, où accorder ne posait QUE le quota (le pansement de l'incident du
    31/07, oto-backend#245). Ne lire que `share_down` raterait donc tous les grants
    des connecteurs `platform_key_open`, c'est-à-dire précisément ceux qu'on bascule.

    **Idempotence** : une arête n'est posée que si le couple (instance, bénéficiaire)
    n'en porte AUCUNE — révoquée comprise. Rejouer ne duplique pas, et surtout ne
    RESSUSCITE pas un accès retiré à la main entre deux boots.

    **Ce qui n'est pas migré, et se voit dans les logs** : un scope hors `user:`/`org:`
    (`group:<id>`, ou le `org` nu « tout le monde »). L'ancien chemin ne les résout de
    toute façon jamais sur une clé plateforme (`access._platform_grantee_scope` ne
    connaît que user et org), et le vocabulaire de 0053 n'a pas de scope « tout le
    monde ». Les inventer ici serait décider à la place de l'ADR.
    """
    from .. import grants_chain
    from . import grants as db_grants

    for provider in sorted(grants_chain.CHAIN_CONNECTORS):
        rows = conn.execute(
            "SELECT entity_id AS label, share_down, meta FROM connector_credentials "
            "WHERE entity_type = 'platform' AND connector = %s AND account = ''",
            (provider,)).fetchall()
        for row in rows:
            meta = row["meta"] or {}
            rate_limit_by = meta.get("rate_limit_by") or {}
            ref = grants_chain.instance_ref(row["label"], provider)
            # Ordre déterministe : deux boots concurrents sur la base partagée
            # posent les mêmes arêtes dans le même ordre (l'advisory lock les
            # sérialise déjà, ceci rend le diff des logs lisible).
            scopes = sorted(set(row["share_down"] or []) | set(rate_limit_by))
            for scope in scopes:
                kind, _, ident = str(scope).partition(":")
                if kind not in ("user", "org") or not ident:
                    logger.warning(
                        "L5 grants: scope %r sur %s ignoré (hors vocabulaire "
                        "user:/org: — l'ancien chemin ne le résout pas non plus)",
                        scope, ref)
                    continue
                if db_grants.edge_exists(ref, kind, ident, conn=conn):
                    continue
                quota = rate_limit_by.get(scope, meta.get("rate_limit"))
                db_grants.insert_grant(
                    resource_id=ref, grantor_kind="platform", grantor_id="platform",
                    grantee_kind=kind, grantee_id=ident,
                    constraints={"quota": int(quota)} if quota else {},
                    source="manual", created_by="migration:l5", conn=conn)
                logger.info("L5 grants: arête posée %s → %s:%s (quota=%s)",
                            ref, kind, ident, quota)


def migrate_business_key_indexes() -> int:
    """Refait les index d'unicité de clé métier sur l'expression POLYMORPHE (#318).

    Un index d'expression posé sur `data->>clé` compare l'objet entier dès que la
    colonne porte des sous-champs : deux lignes du même SIREN, l'une nue l'autre
    enveloppée, ne collisionneraient pas — doublon silencieux. La nouvelle expression
    lit la valeur dans les deux formes, donc l'unicité redevient vraie.

    D'UN COUP sur tous les namespaces à clé, et c'est délibéré : migrer au fil de
    l'eau créerait une période où certains tableaux acceptent les sous-champs sur
    leur clé et d'autres non — un état à expliquer, à tester et à garder en tête.
    Là, il n'existe jamais.

    Idempotent (l'index est reconstruit à l'identique s'il l'est déjà) et sans trou
    d'unicité : `datastore_ensure_key_index` crée en CONCURRENTLY avant de déposer.
    Mesuré à 40 ms par index sur 50 000 lignes — le coût de boot est le nombre de
    namespaces à clé, pas leur volume.
    """
    from .datastore import datastore_ensure_key_index
    n = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, schema->>'key' AS k FROM user_datastores "
            "WHERE schema->>'key' IS NOT NULL AND schema->>'key' <> ''").fetchall()
    for r in rows:
        try:
            datastore_ensure_key_index(int(r["id"]), r["k"])
            n += 1
        except Exception:  # noqa: BLE001
            # Un namespace dont les données portent DÉJÀ un doublon sur la clé refuse
            # l'index — c'est un fait à voir, pas un boot à casser. Les autres passent.
            logger.exception("clé métier: index non migré pour le namespace %s", r["id"])
    logger.info("clé métier: %s index migrés en expression polymorphe", n)
    return n

