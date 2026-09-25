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
    finished_at TIMESTAMPTZ,
    lignes_reservees INT                        -- rendues par la file ; NULL = non mesuré (cf. _init)
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
    -- 25/09/2026 : la durée murale d'un run (d'une ligne). NULL = celle de l'exécuteur.
    max_run_seconds INT,
    -- LA CIBLE : sur quoi les agents écrivent, et sur quelles lignes
    namespace TEXT,
    row_filter JSONB,
    -- LE CONTEXTE D'EXÉCUTION, uniforme sur le passage — porte l'attribution
    provider TEXT,
    model TEXT,
    -- La TEMPÉRATURE du passage, du même rang que `provider`/`model` et pour la
    -- même raison : c'est du contexte d'exécution, uniforme sur tout le passage,
    -- et il porte l'attribution de ce qui a été écrit. Deux passages du même
    -- texte à deux températures ne sont pas comparables — mesuré le 06/09/2026,
    -- le même banc donnait 11 à 18 sur 18 au défaut du fournisseur contre 14 à
    -- 16 à zéro : une journée d'itérations a comparé des versions dont l'écart
    -- était entièrement dans ce bruit.
    -- ⚠️ Déclarée par PASSAGE et non posée dans l'environnement : une variable
    -- d'env s'applique à tout le monde sans distinction et ne se lit nulle part.
    -- NULL = on n'envoie rien, le fournisseur applique son défaut.
    temperature REAL,
    -- Ce que l'agent LIT des outils (oto#241) : {defaut, entieres}, figé comme le reste du
    -- contexte d'exécution. NULL = rien ne part, le worker garde son défaut.
    descriptions_outils JSONB,
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
    -- QUI tient la campagne (21/09/2026) : l'identifiant que l'ordonnanceur DÉCLARE
    -- à `op=take`, stable à travers son redémarrage, distinct d'un ordonnanceur à
    -- l'autre. `sub` est le DÉCLARANT et `heartbeat_at` date un battement sans dire
    -- de qui : sans elle, un ordonnanceur qui redémarre ne sait pas s'il reprend SA
    -- campagne ou s'il en voit une qu'un autre tient encore. NULL = personne ne la
    -- tient (jamais prise, démarrée par le sondage, ou réarmée). ⚠️ Une base qui
    -- existe déjà la reçoit de la révision Alembic `0003_runner_fleets_preneur`,
    -- jouée à la main — pas du boot (ADR 0065).
    taken_by TEXT,
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
    --
    -- `held` (13/09/2026) : RETENU, pas perdu. Mettre un agent déclenché en pause
    -- gèle sa file au lieu de la périmer — un événement n'a pas de successeur, et
    -- personne ne le renverra. La réservation ne prend que `pending`, donc un
    -- travail retenu est invisible aux workers (l'ancien code de prod compris) ;
    -- rallumer le rend à `pending`. Même patron que `billing_invoices.held`.
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'held', 'claimed', 'done', 'failed', 'expired')),
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
-- Un travail est VIVANT tant qu'il reste `pending` ou `claimed` — c'est la
-- fenêtre où `claim_next_job` le cherche (op-backend#deadlock, mesuré 17/09/2026) :
-- un sondage jouait un `Seq Scan` complet, 72k lignes `done` écartées, 152 ms pour
-- zéro résultat, parce que le partiel `WHERE status = 'pending'` d'origine ne
-- couvrait pas `claimed` — la moitié de la condition de réservation
-- (`status = 'pending' OR (status = 'claimed' AND lease_until < NOW())`) obligeait
-- un parcours complet. L'index reste quasi vide en régime normal : peu de travaux
-- vivants à un instant donné, contre un historique `done` qui ne cesse de grossir.
-- ⚠️ **Remplace `idx_runner_jobs_claim`** (retiré ici, dans le MÊME lot que la
-- migration Alembic qui le construit CONCURRENTLY et dépose l'ancien sur une base
-- déjà peuplée — cf. `oto_mcp/db/migrations/versions/*_runner_jobs_index_vivant.py`
-- et `docs/live-migrations.md`). Sûr sur une base neuve : table et index naissent
-- ensemble, comme `idx_runner_jobs_claim` avant lui.
CREATE INDEX IF NOT EXISTS idx_runner_jobs_live
    ON runner_jobs(org_id, due_at) WHERE status IN ('pending', 'claimed');
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
    -- 25/09/2026 : les limites d'UN run déclarées par l'utilisateur
    -- (`capabilities/_limites_du_run.py`). NULL = celles de l'exécuteur.
    max_tokens INT,
    max_run_seconds INT,
    -- 12/09/2026 : le modèle que l'agent DÉCLARE (catalogue `runner_models`).
    -- NULL = aucun : n'importe quel worker le sert, sur son propre modèle.
    model TEXT,
    -- 12/09/2026 : CE QUI DÉCLENCHE. `schedule` = l'horloge (`cron`/`next_due`),
    -- `webhook` = un tiers qui POSTe. Le reste de la ligne — procédure, outils,
    -- modèle, identité — est le même objet : un agent ne change pas de nature
    -- parce que son coup d'envoi change.
    kind TEXT NOT NULL DEFAULT 'schedule',
    -- ⚠️ `cron` et `next_due` deviennent NULLABLES pour le webhook, et c'est
    -- SANS DANGER sur la base partagée : le tick de la prod (ancien code) lit
    -- `WHERE enabled AND next_due <= NOW()`, et NULL ne satisfait aucune
    -- comparaison — une ligne webhook lui est INVISIBLE, jamais mal traitée.
    -- Le sens inverse tient aussi : l'ancien code écrit toujours les deux.
    cron TEXT,
    tz TEXT NOT NULL DEFAULT 'Europe/Paris',
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    next_due TIMESTAMPTZ,
    last_enqueued_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- 25/09/2026 : COMMENT une source prouve qui elle est. `bearer` (défaut) =
    -- l'en-tête `Authorization: Bearer otoh_…` que nous générons ;
    -- `standard_webhooks` = la source SIGNE avec son propre secret (Granola,
    -- Svix…), et le porteur est alors REFUSÉ pour cet agent.
    hook_auth TEXT NOT NULL DEFAULT 'bearer',
    -- Le secret de signature fourni par la source, CHIFFRÉ (`crypto.encrypt`, AAD
    -- liée à la ligne). Jamais servi par une lecture : seule son existence l'est.
    hook_signing_secret_enc TEXT,
    -- Le PLAFOND de livraisons ACCEPTÉES sur 24 h glissantes, déclaré par
    -- l'utilisateur. NULL = aucun (le lissage `max_per_hour` retarde, il ne
    -- refuse jamais) : au-delà du plafond, 429.
    max_per_day INT,
    -- L'adresse PRIVÉE (`h_` + 128 bits aléatoires), optionnelle. Posée, elle
    -- REMPLACE l'adresse numérique `/api/hooks/{id}`, qui cesse d'ouvrir. Pas un
    -- credential : la preuve reste exigée derrière. ⚠️ Son index unique n'est
    -- pas ici (colonne née d'un ALTER du boot, #450) : `db/_init.py`.
    hook_slug TEXT
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

-- Les workers de PLATEFORME : ceux qui servent toutes les organisations à la
-- fois. Une table à part, et non un `org_id` nullable dans celle du dessus :
-- « présent pour l'org 12 » et « présent pour tout le monde » ne sont pas la
-- même information, et les mélanger obligerait chaque lecteur à connaître la
-- convention du NULL. Ici le nom de la table dit la nature de la ligne.
--
-- ⚠️ Lue par `runner_arme` EN PLUS du témoin par org : sans elle, une org
-- servie uniquement par un worker de plateforme se lirait « aucun runner »,
-- et son premier déclencheur serait refusé pour rien.
CREATE TABLE IF NOT EXISTS runner_platform_workers (
    worker_sub TEXT PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- 09/09/2026 : la table de présence devient la DÉCLARATION des workers. Un
-- worker n'est pas un compte : il n'a ni ligne dans `users`, ni org, ni
-- appartenance. Il porte un secret de machine (préfixe `otow_`, haché ici) et
-- TOUT le reste — org, jeton délégué, clé, procédure — lui est commandé par le
-- backend avec chaque travail. « Tout doit être paramétrique, en base et
-- depuis la commande du backend » (arbitrage du 09/09/2026). `revoked_at` : la révocation est
-- une date, pas une suppression — la ligne garde sa trace de présence.
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS label TEXT;
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS secret_hash TEXT UNIQUE;
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE runner_platform_workers ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ;

-- CHAQUE tentative garde son motif (22/09/2026). `last_error` porte la DERNIÈRE
-- et écrase les précédentes : un travail mort au bout de trois essais ne montrait
-- donc qu'un tiers de son histoire, et rien ne disait si les trois avaient échoué
-- pour la même raison. Mesuré sur un agent événementiel de production : trois
-- travaux, neuf tentatives, trois lignes lisibles.
--
-- ⚠️ Un JOURNAL, pas un état : on APPEND, on ne réécrit jamais. Une tentative
-- inscrite reste inscrite même si la suivante réussit — c'est précisément le cas
-- où l'écrasement faisait disparaître l'incident (un travail `done` au troisième
-- essai ne garde aujourd'hui aucune trace des deux premiers).
--
-- ⚠️ Même matière que `last_error` — un motif BORNÉ, jamais du contenu de fil,
-- jamais un secret. `last_error` reste : il est lu partout, et le remplacer par
-- une dérivation de ce tableau ferait d'une lecture chaude un parcours JSON.
ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS attempt_errors JSONB
    NOT NULL DEFAULT '[]'::jsonb;

-- Ce qu'un DÉCLENCHEUR WEBHOOK a reçu (12/09/2026) — une ligne par livraison,
-- acceptée ou non. Trois lecteurs, un seul écrivain (la route `/api/hooks`) :
--
--   1. le LISSAGE — lire les CRÉNEAUX déjà réservés (`due_at`) pour savoir si
--      celle-ci part tout de suite ou derrière la file ;
--   2. l'ÉCRAN — « qui m'a appelé, quand, et quel déroulé en est sorti » ; sans
--      lui, une source mal configurée est un mystère plutôt qu'un diagnostic
--      (même raison que `expired_count` sur un déclencheur programmé) ;
--   3. le DÉBOGAGE d'un secret périmé : le refus est muet pour l'appelant
--      (404 sans oracle), et VISIBLE ici pour le propriétaire.
--
-- ⚠️ AUCUNE contrainte d'unicité : la déduplication est hors de ce lot (décidé
-- le 12/09). La colonne qui la porterait n'existe pas encore — l'ajouter plus
-- tard est un ALTER plus un index, rien de ce qui est ici ne s'y oppose.
--
-- ⚠️ Le CORPS reçu n'est pas stocké ici. Il voyage dans la charge du travail
-- (`runner_jobs.payload`), qui est déjà le domicile de ce qu'un travail emporte
-- — le garder deux fois doublerait le volume et la surface de fuite.
CREATE TABLE IF NOT EXISTS runner_hook_deliveries (
    id BIGSERIAL PRIMARY KEY,
    trigger_id BIGINT NOT NULL,
    org_id BIGINT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- `queued` (enfilé, immédiat) | `delayed` (enfilé, retardé par le lissage)
    -- | `refused_paused` | `refused_too_large` | `refused_rate`
    -- (un mauvais secret ne s'écrit PAS : le journaliser ferait de la route un
    -- oracle sur les déclencheurs qui existent)
    outcome TEXT NOT NULL,
    job_id BIGINT,
    -- Le CRÉNEAU réservé : quand le travail de cette livraison peut partir. NULL
    -- pour un refus, et remis à NULL quand on éteint le déclencheur (ses travaux
    -- sont périmés, leurs créneaux rendus). C'est ce que le lissage lit — pas
    -- `received_at`, qui ne dit plus rien d'une file dès qu'un retard dépasse
    -- l'heure.
    due_at TIMESTAMPTZ,
    -- Ce que la source a dit d'elle-même (User-Agent, tronqué). Pas une garde :
    -- de quoi reconnaître l'appelant sur l'écran quand deux sources partagent
    -- un déclencheur.
    source TEXT,
    -- 25/09/2026 : l'identifiant de livraison que la source SIGNE (`webhook-id`,
    -- Standard Webhooks). NULL pour une source au porteur. C'est la clé de
    -- déduplication : une retentative d'une livraison déjà acceptée ne refait
    -- pas de déroulé.
    external_id TEXT
);
-- L'index de l'ÉCRAN : les livraisons récentes d'un déclencheur, comptées sur 24 h.
CREATE INDEX IF NOT EXISTS idx_hook_deliveries_fenetre
    ON runner_hook_deliveries(trigger_id, received_at DESC);
-- L'index du LISSAGE : les créneaux réservés d'un déclencheur, du plus lointain au
-- plus proche. La seule lecture sur le chemin chaud — elle tourne à chaque POST, et
-- `LIMIT debit` la borne quelle que soit la longueur de la file.
CREATE INDEX IF NOT EXISTS idx_hook_deliveries_creneaux
    ON runner_hook_deliveries(trigger_id, due_at DESC) WHERE due_at IS NOT NULL;
-- ⚠️ L'index de DÉDUPLICATION (`idx_hook_deliveries_externe`) n'est PAS ici : sa
-- colonne `external_id` naît d'un ALTER du boot sur une base existante, et ce DDL
-- tourne AVANT lui (#450). Il est posé dans `db/_init.py`, juste après l'ALTER.


-- 12/09/2026 : la présence d'un worker de plateforme PAR FAMILLE de modèle — le
-- dépôt qu'il nomme au claim (`anthropic`, `mistral`). Une table à part, et non
-- une colonne de la déclaration : plusieurs processus partagent UN secret, donc
-- une ligne, et ne servent pas forcément la même famille. Une colonne garderait
-- la dernière famille vue et effacerait l'autre à chaque sondage.
-- Lue par `runner_arme` (`families`) : un agent qui déclare un modèle ne se pose
-- que si une famille vivante le sert.
CREATE TABLE IF NOT EXISTS runner_platform_depots (
    worker_sub TEXT NOT NULL,
    depot TEXT NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (worker_sub, depot)
);

-- L'ABONNEMENT d'une personne à un fournisseur de modèles (OTO-130) : son bac à
-- sable, et ce qu'on sait de son état. Une LIGNE PAR PERSONNE, jamais par org :
-- un abonnement Claude appartient à qui l'a payé, et la plateforme ne le partage
-- à personne (un agent d'org ne tourne pas sur l'abonnement d'un membre).
--
-- ⚠️ **Aucune colonne ne porte de secret, et il n'y en aura jamais.** La session
-- Claude vit dans le bac à sable de la personne, écrite là par le programme
-- officiel au terme de SA propre procédure de connexion. La plateforme n'a pas le
-- droit de la collecter, de la stocker ni de la relayer (politique Anthropic,
-- « developers may not collect, store, or intermediate Claude.ai credentials or
-- session tokens ») — et n'a pas non plus à le faire : elle lance le programme
-- DANS le bac à sable, qui lit sa session lui-même.
--
-- ⚠️ Ni l'adresse e-mail ni l'org Anthropic de la personne : `claude auth status`
-- les rend, on n'en garde RIEN. Le palier (`plan`) sert l'écran ; la méthode de
-- connexion (`method`) dit si c'est bien un abonnement et pas une clé d'API.
CREATE TABLE IF NOT EXISTS user_model_subscriptions (
    -- ⚠️ Sans `REFERENCES users` : la réservation (`claim_next_job`) lit cette table, donc
    -- elle vit dans le fragment `runs`, que des bancs jouent seul sur une base vierge —
    -- et ce fragment ne porte aucune FK vers l'extérieur (`runner_jobs.sub` non plus).
    -- Une fusion de comptes la suit par `migrate_sub` (`_PK_SUB_TABLES`).
    sub TEXT NOT NULL,
    -- La FAMILLE de modèles servie par cet abonnement (`runner_models`) —
    -- `claude_subscription` aujourd'hui, `openai_subscription` le jour où Codex
    -- suit le même chemin.
    famille TEXT NOT NULL,
    -- Le bac à sable de la personne, tel que l'infrastructure le nomme. Sa
    -- DURABILITÉ est ce qui porte la connexion : le détruire déconnecte.
    sandbox_id TEXT,
    -- `connected` | `needs_login` | `paused_limit` | `disconnected`.
    statut TEXT NOT NULL DEFAULT 'disconnected',
    -- Ce que `claude auth status` rend et que l'écran montre : « Max », « pro »…
    plan TEXT,
    -- « claude.ai » — donc un abonnement. Toute autre valeur n'en est pas un.
    method TEXT,
    -- Quand le plafond du forfait se relâche. Tant qu'elle est dans le futur et
    -- que le statut est `paused_limit`, la réservation SAUTE les travaux de cette
    -- personne (`claim_next_job`) : ils attendent, ils n'échouent pas.
    limit_reset_at TIMESTAMPTZ,
    -- Dernière preuve que la connexion tenait : une exécution qui n'a pas fini
    -- sur « déconnecté ».
    last_ok_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Le plafond PERSO de consommation, en % de l'usage total du compte (fenêtres
    -- cinq heures et sept jours). NULL = aucun : seul celui de l'org s'applique. Il
    -- ne peut que RESSERRER celui de l'org (`_abonnement.seuil` prend le min). Sur une
    -- base existante : révision `0019` ou le démarrage
    -- (`user_subscriptions.DDL_COLONNE_LIMITE`, même forme).
    limite_pct SMALLINT CHECK (limite_pct BETWEEN 1 AND 100),
    PRIMARY KEY (sub, famille)
);
"""

# le plafond de consommation des abonnements, réglé par l'org
MODEL_SUBSCRIPTION_LIMITS = """
-- Le PLAFOND de consommation qu'une org pose sur les abonnements personnels de ses
-- membres (famille `claude_subscription`) : la part maximale, en %, de l'usage TOTAL
-- du compte du fournisseur (fenêtres cinq heures et sept jours, usage perso compris)
-- au-delà de laquelle les travaux de l'org attendent la réinitialisation. Sans ligne,
-- le défaut du code s'applique (`_abonnement.DEFAUT_LIMITE_PCT`). Une personne peut
-- poser plus bas pour elle-même (`user_model_subscriptions.limite_pct`), jamais plus
-- haut : le seuil effectif est le min des deux.
--
-- ⚠️ Constante À PART de `RUNS`, assemblée en queue (après `orgs`, qu'elle
-- référence) : `RUNS` se joue seul sur une base vierge et ne porte aucune FK vers
-- l'extérieur. La réservation ne lit pas cette table — la conclusion d'un travail
-- (`_abonnement.noter_rapport`) seule la lit.
CREATE TABLE IF NOT EXISTS org_model_subscription_limits (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    famille TEXT NOT NULL,
    limite_pct SMALLINT NOT NULL CHECK (limite_pct BETWEEN 1 AND 100),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Qui l'a réglé (un admin de l'org).
    updated_by TEXT,
    PRIMARY KEY (org_id, famille)
);
"""

# le POOL d'org des abonnements : le mode de l'org et les prêts de ses membres
MODEL_SUBSCRIPTION_POOL = """
-- Le MODE d'une org pour une famille d'abonnement. Sans ligne : `personnel` — chaque
-- travail tourne sur l'abonnement de SON demandeur. `pool` : les travaux de l'org
-- tournent sur l'abonnement d'un membre qui l'a PRÊTÉ (table suivante), flottes
-- comprises. Réglé par un admin de l'org.
--
-- ⚠️ Une table À PART du plafond (`org_model_subscription_limits`), et non une
-- colonne de plus : sa `limite_pct` est NOT NULL (une ligne = un plafond réglé), et un
-- mode posé sans plafond l'aurait rendue nullable — une ligne à `limite_pct` NULL,
-- écrite par ce code sur la base PARTAGÉE, fait lever le code d'avant qui lit la même
-- table (`seuil`, la route d'org). Deux tables neuves, rien de relâché.
CREATE TABLE IF NOT EXISTS org_model_subscription_modes (
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    famille TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('personnel', 'pool')),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Qui l'a réglé (un admin de l'org).
    updated_by TEXT,
    PRIMARY KEY (org_id, famille)
);

-- Le PRÊT d'un abonnement au pool d'UNE org : opt-in explicite, par membre ET par
-- org — une personne de deux orgs choisit laquelle son forfait sert. Sans ligne,
-- rien n'est prêté. Retirer la ligne vaut pour le travail SUIVANT (le run en cours
-- finit). Inerte tant que l'org n'est pas en mode `pool`, que la personne n'en est
-- plus membre, ou que son abonnement n'est pas connecté : la réservation joint les
-- trois (`claim_next_job`).
--
-- ⚠️ Sans clé étrangère vers `user_model_subscriptions` : une fusion de comptes
-- (`migrate_sub`) repointe les deux tables chacune de son côté, et la cascade d'une
-- FK composite effacerait le prêt au dédoublonnage de l'abonnement. `oublier` (le
-- sandbox effacé) retire les prêts dans la même transaction.
CREATE TABLE IF NOT EXISTS user_model_subscription_loans (
    sub TEXT NOT NULL,
    famille TEXT NOT NULL,
    org_id BIGINT NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- La dernière fois que ce prêt a SERVI un travail du pool (posée à la réservation,
    -- sur tous les prêts de l'abonnement) : le pool prend le prêteur servi le MOINS
    -- récemment. NULL = jamais servi, pris en premier.
    servi_at TIMESTAMPTZ,
    PRIMARY KEY (sub, famille, org_id)
);
CREATE INDEX IF NOT EXISTS idx_user_model_subscription_loans_org
    ON user_model_subscription_loans(org_id, famille);
"""
