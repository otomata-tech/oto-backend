"""DDL du domaine « runs » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# runs, fil de messages, jobs et déclencheurs
RUNS = """
-- Runs / déroulés (ADR 0017, amende le « state-only » du barreau 1-2) : la
-- métadonnée SÉMANTIQUE d'un run (label, doctrine, outcome) est désormais PERSISTÉE
-- — la pile session-scopée de `guide_run.py` reste la source du run ACTIF (pour
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

-- Le FIL d'un run HÉBERGÉ (chantier runner R1 — ADR 0064 du blueprint) : l'état
-- d'exécution, PAS le journal. La reprise canonique inter-agents reste le journal ;
-- le fil sert à CONTINUER le même run (le worker le recharge, le dashboard le lit).
-- Il est EFFAÇABLE sans amputer le run — purge courte au boot (_init), et AUCUNE
-- fonction du produit ne doit l'exiger. Deux étages par tour : `content` = la
-- projection NEUTRE (ce que l'UI et l'API lisent, indépendante du fournisseur de
-- modèle) ; `provider_raw` = le tour provider exact (blocs de thinking inclus, à
-- réémettre verbatim pour une continuation fidèle) — NULL pour un message humain.
-- Hors recherche PAR CONSTRUCTION : jamais déclaré comme source (même règle que les
-- sous-arbres de run, 0058-D2). UNIQUE(run_id, seq) porte l'index de lecture.
CREATE TABLE IF NOT EXISTS run_messages (
    id BIGSERIAL PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq INT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content JSONB NOT NULL,
    provider_raw JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (run_id, seq)
);

-- Les FLOTTES du runner (chantier R4) : la CONFIGURATION DÉCLARÉE d'un passage —
-- quelle procédure, sur quel tableau, dans quel périmètre, jusqu'où. Rien ne
-- s'exécute ici : une flotte est ce que l'ordonnanceur LIT pour fabriquer des
-- jobs, et ce qu'un opérateur INTERROGE pour savoir où en est son passage.
-- Elle précède `runner_jobs` dans ce fragment parce que les jobs la référencent —
-- l'ordre de ce fichier est une contrainte d'exécution, pas une mise en page.
--
-- ⚠️ POURQUOI une configuration déclarée plutôt qu'un verbe qui lance librement :
-- c'est l'endroit où les GARDES vivent. Un lancement qui prend un tableau en
-- argument n'a nulle part où accrocher une cible, un périmètre ni une borne — et
-- la mise au point d'août 2026 a montré que ce qui a évité le désastre n'était
-- pas la plateforme mais la discipline de l'équipe qui l'opérait. Déclarer n'est
-- donc pas restreindre une fonctionnalité : c'est donner un domicile aux gardes,
-- pour que le prochain opérateur reçoive la puissance AVEC les leçons.
--
-- `namespace` + `row_filter` = la CIBLE, constatée au lancement et jamais
-- supposée — et FIGÉE ensuite. Une cible mutable ne se contente pas d'ouvrir un
-- geste dangereux : elle rend toute mesure INATTRIBUABLE, puisqu'un relevé de
-- coût ou d'avancement ne dit plus sur quoi il a porté. Viser autre chose se fait
-- en DUPLIQUANT la flotte, jamais en la faisant basculer. Un tableau copié hérite du périmètre de sa source : l'ordonnanceur
-- annonce alors « plus de ligne à réserver » sur cent lignes disponibles — le
-- message le plus trompeur de la chaîne, il décrit une file vide quand la file
-- est pleine et la porte fermée.
-- `provider`/`model` = le contexte d'exécution, UNIFORME sur tout le passage.
-- C'est lui qui porte l'attribution d'une ligne écrite (quel modèle l'a produite) :
-- l'agent ne sait pas ce qui le fait tourner et ne peut donc pas l'estampiller
-- lui-même — un harnais qui écrirait par-dessus lui le pourrait, mais il ment
-- dès qu'il se trompe. L'attribution appartient au passage, pas à l'agent.
-- ⚠️ Ce n'est pas un champ de confort : c'est le PREMIER endroit où la plateforme
-- CONNAÎT le modèle au lieu de se le faire RAPPORTER. Estampiller depuis ce que
-- l'agent déclare de lui-même reviendrait à recopier sa parole en y ajoutant le
-- sceau du serveur — une valeur qu'on rejoint ne dérive pas, une valeur qu'on
-- recopie dérive.
-- Les bornes sont d'EXPLOITATION (volume, jetons du passage, échecs enchaînés,
-- plafond de jetons par ligne), jamais de métier : ce que vaut un enrichissement se juge sur
-- la DONNÉE produite, pas ici. `max_tokens_per_row` est de PREMIER RANG et pas une
-- conséquence du budget : un agent qui part en boucle sur une ligne consomme le
-- budget de tout le passage avant qu'aucune autre borne ne s'en aperçoive.
-- ⚠️ `heartbeat_at` distingue le VIVANT du RÉSIDU, et cette colonne vaut une
-- garde : une flotte `running` qui ne bat plus n'est pas une concurrence à
-- attendre, c'est un reste de passage mort. Sans elle, un second passage se
-- heurte à un refus que rien ne justifie, quelqu'un désarme à la main — et
-- désarmer devient le geste normal.
CREATE TABLE IF NOT EXISTS runner_fleets (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL,
    sub TEXT NOT NULL,                   -- qui a déclaré la flotte (audit)
    label TEXT NOT NULL,                 -- le nom qu'on prononce en exploitation
    procedure TEXT NOT NULL,
    project_id BIGINT,
    tools JSONB NOT NULL,
    input TEXT,
    max_steps INT,
    -- LA CIBLE : sur quoi les agents écrivent, et sur quelles lignes
    namespace TEXT,
    row_filter JSONB,
    -- LE CONTEXTE D'EXÉCUTION, uniforme sur le passage — porte l'attribution
    provider TEXT,
    model TEXT,
    -- LES BORNES : ce qui arrête un passage, et rien d'autre. ⚠️ Le budget se
    -- compte en JETONS, jamais en monnaie — les tarifs changent, diffèrent par
    -- fournisseur, et une valeur monétaire figée en base devient fausse sans que
    -- rien ne le dise. (Un NUMERIC ne se sérialise même pas en JSON : la flotte
    -- serait illisible dès qu'elle porte une borne.)
    workers INT NOT NULL DEFAULT 1,
    -- Combien de lignes VISAIENT le passage au moment de l'armement. Un compte,
    -- pas une borne : `max_rows` est un plafond déclaré, celui-ci est ce que la
    -- table contenait vraiment. Sans lui, l'avancement n'a pas de dénominateur —
    -- « 1 240 lignes faites » ne se lit pas, et diviser par `max_rows` a déjà
    -- produit un coût par ligne faux d'un facteur 46 sur un passage de démo.
    -- Réécrit à CHAQUE armement (un passage relancé vise une table qui a bougé).
    -- NULL = pas de cible déclarée, ou le compte n'a pas pu être lu : « inconnu »,
    -- jamais zéro — un zéro se lirait « la table est vide ».
    rows_at_launch INT,
    max_rows INT,
    max_tokens BIGINT,
    max_consecutive_failures INT,
    max_tokens_per_row INT,
    -- L'ÉTAT, et il compte SEPT valeurs parce que deux d'entre elles séparent une
    -- INTENTION d'un FAIT. ⚠️ Une intention déclarée et un fait constaté ne
    -- partagent jamais une colonne — c'est la même règle que « trois états,
    -- jamais deux », appliquée au pilotage d'un passage :
    --
    --   draft     déclarée, personne n'a demandé qu'elle tourne
    --   armed     quelqu'un a DEMANDÉ qu'elle tourne (op=launch)   ← intention
    --   running   un ordonnanceur l'a PRISE et donne signe          ← fait observé
    --   stopping  l'arrêt est DEMANDÉ (op=stop)                     ← intention
    --   stopped   l'ordonnanceur a ACCUSÉ réception                 ← fait observé
    --   done      la file s'est vidée · failed  arrêt anormal
    --
    -- Sans `armed`, `running` voudrait dire « on a cliqué » ET « ça tourne » : une
    -- flotte armée que personne n'a prise se lirait comme un passage en cours.
    -- Sans `stopping`, un arrêt demandé se lirait comme un arrêt EFFECTIF — et
    -- croire qu'on a coupé une dépense qui continue est pire que croire qu'on a
    -- lancé un passage qui ne tourne pas : dans un cas on attend, dans l'autre on
    -- part tranquille pendant que ça brûle.
    -- ⚠️ Et l'écart entre les deux est le DIAGNOSTIC : un `stopping` qui ne
    -- devient jamais `stopped`, ou un `armed` que personne ne réclame, désignent
    -- un ordonnanceur mort. Fondus dans un seul état, ces cas ressemblent à un
    -- succès.
    -- `stop_reason` reste ÉCRIT, jamais déduit du statut.
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'armed', 'running', 'stopping', 'stopped',
                          'done', 'failed')),
    stop_reason TEXT,
    -- Quand l'INTENTION a été posée, distinct de quand le fait s'est produit :
    -- `armed_at` → `started_at` mesure l'attente d'un ordonnanceur ;
    -- `stopping_at` → `stopped_at` mesure le délai d'obéissance. Les deux
    -- écarts sont le seul moyen de voir un ordonnanceur qui ne répond plus.
    armed_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    stopping_at TIMESTAMPTZ,
    heartbeat_at TIMESTAMPTZ,
    stopped_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_runner_fleets_org
    ON runner_fleets(org_id, status);

-- La file d'EXÉCUTIONS du runner (chantier runner R2) — de la PLOMBERIE plateforme,
-- PAS une donnée du client : la file de LIGNES d'une campagne vit dans le datastore
-- de l'org (namespace client, `data_claim_next`) ; mélanger les deux ferait de la
-- plomberie une donnée visible du client. Même mécanique de bail (SKIP LOCKED),
-- table distincte — les deux baux coexistent sans se connaître.
-- `claimed_by` = le SUB du worker : l'audit d'un job (qui l'a pris, qui l'a fini)
-- en dépend. Le claim est SCOPÉ à l'org (V1 : un worker = un jeton d'org — le pool
-- multi-org attend l'arbitrage compte-de-service, ADR 0064 §5-1).
-- Un job à bout de tentatives est MARQUÉ `failed` (visible), jamais rejoué en
-- boucle : refuser-et-marquer, pas tourner.
CREATE TABLE IF NOT EXISTS runner_jobs (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('start', 'continue')),
    run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,  -- NULL : start pas encore lié à son run
    payload JSONB,                       -- références SEULEMENT (procédure, projet, message) — jamais un secret
    -- ⚠️ `expired` n'est PAS `failed` : un travail périmé n'a jamais tourné.
    -- Les confondre effacerait la seule distinction qui compte au diagnostic —
    -- « ça a échoué » envoie lire une erreur qui n'existe pas, quand le fait est
    -- « personne n'est venu le prendre ». Trois états, jamais deux.
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'claimed', 'done', 'failed', 'expired')),
    attempts INT NOT NULL DEFAULT 0,
    max_attempts INT NOT NULL DEFAULT 3,
    claimed_by TEXT,
    lease_until TIMESTAMPTZ,
    last_error TEXT,
    due_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    -- La FLOTTE dont ce job fait partie, quand il en vient d'une : c'est ce qui
    -- rend un passage LISIBLE d'un bout à l'autre (avancement, coût, travaux morts)
    -- sans corréler des horodatages à la main. NULL = job isolé (déclencheur, appel
    -- direct) — la file sert les deux et ne les distingue qu'ici.
    fleet_id BIGINT REFERENCES runner_fleets(id) ON DELETE SET NULL,
    -- QUI a demandé ce travail. C'est l'identité que l'agent porte en
    -- l'exécutant : par défaut celle du créateur du déclencheur, paramétrable
    -- vers un autre membre (direction du 02/09).
    --
    -- ⚠️ **C'est le préalable du worker MUTUALISÉ.** Tant que l'identité vient du
    -- jeton que le worker présente, il faut un worker par organisation — ce n'est
    -- pas un choix d'architecture, c'est un empêchement, et c'est lui qui a laissé
    -- 41 travaux sans personne pour les prendre. Un travail qui porte son identité
    -- dispense le worker d'en avoir une par organisation.
    --
    -- ⚠️ NULLABLE, et ça le restera : les travaux enfilés avant le 02/09 n'ont pas
    -- de créateur connu. Écrire un sub par défaut leur inventerait un demandeur —
    -- et un `NULL` qui dit « on ne sait pas » vaut mieux qu'un nom faux, qu'on
    -- lirait comme un fait.
    sub TEXT,
    -- Le RÉSULTAT déclaré par le worker à la conclusion (usage_tokens, stopped,
    -- steps…) : c'est ce qui rend le coût d'un job LISIBLE par un ordonnanceur
    -- de flotte (garde budget) sans parser la note libre d'un run.
    result JSONB
);
CREATE INDEX IF NOT EXISTS idx_runner_jobs_claim
    ON runner_jobs(org_id, due_at) WHERE status = 'pending';
-- Le comptage des occurrences PERDUES est lu à chaque `runner.triggers op=list`,
-- donc à chaque ouverture de l'écran des automatisations. Sans cet index il
-- balaye toute la file — une lecture d'affichage qui grossit avec l'historique
-- de la plateforme entière. L'index partiel ne coûte que les lignes périmées,
-- qui sont par construction rares.
-- ⚠️ Sûr sur une base existante, contrairement au piège du 20/07 : il porte sur
-- `status` et `org_id`, deux colonnes du CREATE TABLE d'origine, pas sur une
-- colonne née d'un ALTER.
CREATE INDEX IF NOT EXISTS idx_runner_jobs_expired
    ON runner_jobs(org_id) WHERE status = 'expired';
-- ⚠️ PAS d'index sur `fleet_id` ici : la colonne naît d'un ALTER dans `_init`, et
-- sur une base qui existe déjà le CREATE TABLE ci-dessus est SAUTÉ — l'index
-- s'exécuterait alors sur une colonne absente et tuerait le boot (piège du
-- 20/07, `docs/live-migrations.md`). Il vit avec son ALTER.


-- Les DÉCLENCHEURS du runner (chantier R3) : la CONFIG utilisateur qui FABRIQUE des
-- jobs — le tick les enfile à l'échéance (jamais d'exécution ici), le worker les
-- claime. `sub` = qui a posé le déclencheur (audit) ; le run tournera sous le
-- worker. `cron` s'évalue DANS `tz` (défaut Europe/Paris, ÉCRIT — « tous les
-- matins à 8h » doit dire quel 8h, sinon l'heure d'été décale toutes les veilles
-- d'une heure sans un mot). ⚠️ next_due se consomme par COMPARE-AND-SWAP : prod
-- et preprod partagent la même base, DEUX ticks tournent — un seul doit gagner
-- chaque échéance, l'autre voit le CAS échouer et passe.
CREATE TABLE IF NOT EXISTS runner_triggers (
    id BIGSERIAL PRIMARY KEY,
    org_id BIGINT NOT NULL,
    sub TEXT NOT NULL,
    label TEXT,
    procedure TEXT NOT NULL,
    project_id BIGINT,
    tools JSONB NOT NULL,
    input TEXT,
    max_steps INT,
    cron TEXT NOT NULL,
    tz TEXT NOT NULL DEFAULT 'Europe/Paris',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_due TIMESTAMPTZ NOT NULL,
    last_enqueued_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_runner_triggers_due
    ON runner_triggers(next_due) WHERE enabled;


-- Les WORKERS VUS : la présence d'un runner armé pour une org, constatée à chaque
-- sondage de la file. Elle existe pour qu'on cesse de PROMETTRE une exécution que
-- personne n'assure — un déclencheur posé dans une org sans worker s'enfile tous
-- les matins et n'est jamais joué, sans une erreur (vécu : org 196, un
-- déclencheur désactivé le 26/08 dont le seul témoignage tient dans son LIBELLÉ,
-- « oto_trigger jobs do not execute »).
--
-- ⚠️ Pourquoi une table, et pas une lecture de `runner_jobs.claimed_by`. Un claim
-- sur file VIDE n'écrit rien : « aucun job n'a jamais été claimé » ne distingue
-- pas « aucun worker » de « un worker qui n'a rien eu à faire ». Et surtout elle
-- se BOUCLE au démarrage — aucun job ne peut exister avant un déclencheur, aucun
-- déclencheur ne pourrait alors se poser. Le SONDAGE, lui, prouve la présence
-- même à vide : c'est le seul signal qui parle avant le premier job.
--
-- Clé (org, worker) plutôt qu'org seule : « un worker = un jeton d'org » en V1,
-- mais N processus font la batterie (cf. oto-runner) — compter les pairs armés
-- est gratuit ici et impossible à reconstituer après coup.
CREATE TABLE IF NOT EXISTS runner_workers (
    org_id BIGINT NOT NULL,
    worker_sub TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (org_id, worker_sub)
);
"""
