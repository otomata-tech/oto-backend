"""DDL du domaine « usage » — fragment du schéma assemblé par `db/_schema.py`.

Ce module ne porte QUE du DDL, en chaînes SQL, et n'est jamais exécuté seul :
`_schema._SCHEMA` concatène tous les domaines dans un ordre FIGÉ (les FK en
dépendent — une table référencée doit être créée avant celle qui la référence).
Changer l'ordre, c'est éditer `_schema.ASSEMBLAGE`, pas ce fichier.

Les évolutions de colonnes sur tables EXISTANTES ne vivent pas ici mais dans
`_init.init_db` (ALTER idempotents) — cf. `docs/live-migrations.md`, en
particulier le piège du `CREATE INDEX` sur une colonne ajoutée par migration.
"""
from __future__ import annotations

# compteurs, journal d'appels, signaux d'usage
USAGE = """
CREATE TABLE IF NOT EXISTS usage (
    sub TEXT NOT NULL,
    tool TEXT NOT NULL,
    day DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sub, tool, day)
);

-- Journal des appels MCP (monitoring admin). Une ligne par appel de tool,
-- posée par calllog.ToolCallLogger (succès comme échec). Schéma CANONIQUE
-- calllog (contrat inter-projets, domicile = socle otomata-mcp/logging.py ;
-- l'ex-lib otomata-calllog est décommissionnée, otomata-calllog#1).
-- Volumétrie bornée par le timer `oto-journal-archive` (export S3 du mois PUIS
-- suppression, `OTO_JOURNAL_RETENTION_DAYS`) — plus par un prune au boot, qui
-- supprimait sans archiver et vidait d'avance ce que l'archive devait prendre
-- (ADR 0065 lot 0, oto-backend#426).
-- `sub` nullable : les appels stdio local non authentifiés n'ont pas d'identité.
CREATE TABLE IF NOT EXISTS tool_calls (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    server TEXT NOT NULL DEFAULT 'oto',
    -- Discriminateur d'événement (ADR 0017, « un seul flux ») : 'mcp' = invocation
    -- d'outil MCP (le cas historique, défaut) ; 'rest' = appel /api/* ; 'connector'
    -- = échec/événement de résolution de credential ou de connexion connecteur ;
    -- 'protocol' = événement PROTOCOLAIRE MCP (handshake `initialize`) — mesure la
    -- cadence de re-handshake par client (`client_id`) et le churn de `session_id`,
    -- dont dépendent la visibilité des tools et l'injection des blocs A/C.
    -- `tool` porte alors l'identifiant d'événement (route REST, nom de provider,
    -- méthode protocolaire…).
    -- Les lectures du monitoring d'outils filtrent kind='mcp' pour rester iso.
    kind TEXT NOT NULL DEFAULT 'mcp',
    sub TEXT,
    email TEXT,
    tool TEXT NOT NULL,
    args JSONB,
    ok BOOLEAN NOT NULL DEFAULT TRUE,
    error TEXT,
    duration_ms INTEGER,
    -- Taille du texte SERVI à l'appelant, en caractères (oto-backend#340).
    -- ⚠️ La seule mesure qui manquait pour savoir quels outils coûtent du
    -- CONTEXTE : la durée dit ce qu'un appel a pris au serveur, jamais ce qu'il
    -- a pris à la fenêtre de l'agent. Sans elle, on ne peut ni classer les
    -- outils bavards ni suivre une dérive — et ce qu'on ne mesure pas ne produit
    -- aucun signal, donc aucune plainte : l'absence de plainte n'a jamais prouvé
    -- l'absence de coût.
    -- NULL = non mesurée (appel en échec, ou forme de résultat non lisible) —
    -- distinct de 0, qui est une réponse réellement vide.
    result_size INTEGER,
    -- FORME du résultat servi (oto-backend#644), vocabulaire FERMÉ, jamais le contenu :
    -- `empty` | `non_empty` | `refused(<code>)` (le refus applicatif rendu sous un
    -- `ok` vrai — `{"error": "not_found"}`). Sépare ce que `result_size` laisse
    -- ambigu. Écrite au même point que la taille (`calllog.forme_servie`) ; NULL =
    -- non mesurée (appel en échec, geste REST, forme illisible, historique).
    -- ⚠️ La BASE ferme le vocabulaire : un TEXT libre sous un nom de résultat serait
    -- de quoi garder une réponse (garde `test_runner_cle_de_modele`), la contrainte
    -- refuse toute autre valeur — même motif que `calllog._CODE_DE_REFUS`.
    -- Posée sur une base existante par la révision `0010_tool_calls_result_shape`,
    -- jamais au démarrage (table de plusieurs millions de lignes). PAS d'index.
    result_shape TEXT CONSTRAINT tool_calls_result_shape_ferme CHECK (result_shape ~ '^(empty|non_empty|refused[(][a-z][a-z_]{0,39}[)])$'),
    -- Corrélation (ADR 0017, extension OTO-LOCALE — PAS dans le contrat canonique
    -- calllog/otomata-mcp) : session_id = session mcp transport (grossier) ; run_id =
    -- déroulé/run (fin, posé par run_start, stampé ici). NULL hors run.
    session_id TEXT,
    run_id TEXT,
    -- Org sous laquelle l'appel a été émis (seam current_org au moment du call,
    -- extension OTO-LOCALE) — scope EXACT du journal d'audit org (#67). NULL hors org.
    org_id BIGINT,
    -- Application OAuth cliente porteuse du grant (`azp`/`client_id` du JWT :
    -- claude.ai, Claude Code, ChatGPT… — extension OTO-LOCALE). Télémétrie par
    -- surface, jamais une frontière d'autz. NULL en REST/dev local.
    client_id TEXT,
    -- Event Sentry du traceback de CET appel (extension OTO-LOCALE) : posé quand
    -- `SentryToolErrorMiddleware` a capturé (donc uniquement sur une erreur de CODE
    -- — les 4xx amont/refus d'entrée sont droppés). Lien direct journal → traceback,
    -- fin du détour « chercher par user.id dans Sentry ». NULL partout ailleurs.
    sentry_event_id TEXT,
    -- Discriminant PAR APPEL (#117, extension OTO-LOCALE). `session_id` désigne une
    -- conversation entière et `run_id` est souvent NULL : rien n'identifiait UN appel,
    -- donc rien ne permettait de dire quelle réponse est partie à quelle requête — ni
    -- de prouver un cross-talk, ni de prouver qu'un correctif l'a fermé.
    --   request_id    = l'identifiant de la requête entrante, tel que le client l'a émis.
    --   call_uid      = le nôtre, frappé à l'entrée du middleware : deux requêtes qui
    --                   porteraient le même identifiant client restent distinguables.
    --   effective_sub = le compte relu APRÈS exécution du handler, là où `sub` est celui
    --                   capturé à l'ENTRÉE. Les deux doivent être égaux ; une divergence
    --                   EST le défaut, et la ligne le porte. C'est le seul champ qui
    --                   puisse trahir une réponse servie sous une autre identité.
    request_id TEXT,
    call_uid TEXT,
    effective_sub TEXT,
    -- oto#25 lot (b1) : résultat de `error_taxonomy.classify()` (son `.code`, ex.
    -- `not_authorized`) sur un échec — écrit par `calllog._record`. NULL sur un
    -- succès et sur tout l'historique antérieur à ce lot. PAS d'index (même
    -- raison que les trois colonnes ci-dessus : enquête, pas chemin chaud).
    error_kind TEXT,
    -- 2026-09-05 : QUEL JETON a servi, jamais sa valeur. Le journal n'attribuait
    -- que les sessions JWT (`_claimed_sub` ne décode que celles-là) : tout appel
    -- par jeton API ou par délégation du runner s'écrivait SANS compte, anonyme
    -- dans le seul endroit où l'on cherche qui a fait quoi. `sub` est réparé côté
    -- middleware ; ces deux colonnes disent en plus PAR QUEL MOYEN, ce que le seul
    -- compte ne dit pas : deux appels du même utilisateur par deux jetons étaient
    -- indistinguables, et une délégation ressemblait à une session humaine.
    -- NULL sur une session interactive (pas de jeton nommé) et sur tout
    -- l'historique. PAS d'index : enquête, pas chemin chaud.
    token_id BIGINT,
    token_kind TEXT,
    -- Nombre d'items TRAITÉS par cet appel (extension OTO-LOCALE, 2026-08-21) —
    -- un appel bulk (ex. linkedin_aiark_search jusqu'à 100 résultats,
    -- fullenrich_enrich_linkedin jusqu'à 100 contacts SOUMIS) compte pour PLUS
    -- qu'UN appel côté métrage/facturation. NULL = non tracé pour ce tool
    -- (l'écrasante majorité — un consommateur doit traiter NULL comme 1, PAS
    -- comme 0). Posé via le même seam que `_TRACED_ARGS`
    -- (`session_org.note_call_trace(quantity=N)`), mais dans SA PROPRE colonne
    -- plutôt que fondu dans `args` : c'est une donnée de premier ordre pour un
    -- consommateur de facturation (celui du partenaire), pas une trace de debug —
    -- une colonne INTEGER indexable bat une extraction JSONB pour ce qu'un tel
    -- consommateur en fait (sommer/filtrer par org/période).
    quantity INTEGER,
    -- SOUS QUELLE CLÉ l'appel est passé — le `mode` du credential gagnant de la
    -- cascade (`user|group|org|tenant|platform`, ADR 0024), posé au résolveur
    -- unique donc valable pour tout tool keyed sans travail par tool.
    -- Le `mode` et NON le booléen `is_platform` : celui-ci écrase quatre origines
    -- distinctes en « pas plateforme », or une facture peut avoir à distinguer
    -- une clé d'org d'une clé de membre.
    -- NULL = aucun credential résolu (outil méta, open data) ou ligne antérieure
    -- à cette colonne. ⚠️ Un consommateur de facturation ne doit RIEN facturer
    -- sur NULL : contrairement à `quantity` (NULL = 1), ici l'absence signifie
    -- « on ne sait pas à qui attribuer », et on ne facture pas ce qu'on ne sait
    -- pas attribuer. Non reconstructible sur l'historique.
    key_mode TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_created_at ON tool_calls(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_calls_sub ON tool_calls(sub);
-- ⚠️ Nom HÉRITÉ de l'ancienne table `tool_call_log` : c'est le nom que porte la base
-- de prod, et le redéclarer sous un nom propre y créerait un SECOND index identique.
-- Il n'était déclaré NULLE PART jusqu'au 2026-08-27 — la prod l'avait, un environnement
-- neuf ne l'aurait pas eu, alors que `tool = %s` est un filtre de trois lectures et que
-- cet index sert 11,6 M fois. Le DDL et la base avaient divergé dans les deux sens : il
-- manquait celui-ci, et il déclarait `idx_tool_calls_server_tool (server, tool, …)`,
-- retiré le même jour — 82 Mo, ZÉRO lecture depuis la création de la base, car `server`
-- n'est jamais un critère de filtre : en tête d'index composite, il rendait l'index
-- inutilisable pour le seul filtre qui existe.
CREATE INDEX IF NOT EXISTS idx_tool_call_log_tool ON tool_calls(tool);
-- Lentilles d'activité du datastore (ADR 0046 b4) : corrélation par `ns_id` résolu.
-- Index d'EXPRESSION partiel — seules les lignes `data_*` portent un ns_id, donc
-- l'index reste petit et la lecture d'un tableau ne scanne plus tout le journal.
-- `args` existe depuis la création de la table (contrat calllog) : sûr ici, contrairement
-- aux colonnes ajoutées par ALTER (cf. bloc ci-dessous).
CREATE INDEX IF NOT EXISTS idx_tool_calls_ns ON tool_calls ((args->>'ns_id'), created_at DESC)
    WHERE args->>'ns_id' IS NOT NULL;
-- idx_tool_calls_run (run_id) ET idx_tool_calls_org (org_id) créés dans le bloc
-- ALTER de init_db, APRÈS leur ADD COLUMN : sur une table existante, CREATE TABLE
-- IF NOT EXISTS est un no-op donc ces colonnes n'existent pas encore ici (un index
-- les référençant dans _SCHEMA = crash UndefinedColumn au boot, vécu le 2026-06-25).

-- Signaux d'usage volontaires (ADR 0017, barreau 3) : feedback de l'agent/humain
-- sur un outil + cas d'usage non couverts (gap). DURABLE (hors prune 30j de
-- tool_calls) : c'est le signal qui pilote révisions d'outils/doctrines + backlog.
-- Le face-agent est AUSSI un tool_call (auto-journalisé, corrélé run_id) ; cette
-- table porte le CONTENU durable. Les colonnes NÉES AVEC ELLE ont leurs indexes
-- inline ci-dessous ; celles ajoutées ensuite par ALTER ont les leurs dans init_db.
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
    -- L'ARBITRAGE (#450) : où en est ce signal. Quatre états, parce que deux ne
    -- suffisaient pas — « ouvert » confondait ce que personne n'a lu avec ce qu'on a
    -- lu sans savoir qu'en faire, et il n'existait aucune façon de dire non. Un stock
    -- où le refus est indicible ne peut que monter : on ne distingue pas le retard du
    -- désaccord. Mesuré le 27/08 : 203 ouverts, dont 125 de plus d'une semaine, et
    -- zéro arbitrage depuis le 16/08.
    --   open         reçu, personne ne l'a encore regardé
    --   acknowledged lu, décision PAS prise — l'état qui manquait
    --   declined     décidé de ne pas traiter (motif obligatoire)
    --   resolved     traité
    status TEXT NOT NULL DEFAULT 'open',
    -- ⚠️ Le trio ci-dessous porte le DERNIER ARBITRAGE, pas la seule résolution : il
    -- est posé à chaque changement d'état (sauf retour à `open`, qui l'efface). Les
    -- noms datent des deux états d'origine et n'ont pas été migrés — renommer trois
    -- colonnes d'une table servie coûterait plus que la clarté gagnée, mais c'est
    -- `status` qui dit l'état, JAMAIS `resolved_at IS NOT NULL`.
    resolved_at TIMESTAMPTZ,     -- quand l'arbitrage a été posé
    resolved_by TEXT,            -- sub de l'opérateur qui a arbitré
    resolution TEXT,             -- note libre : ce qui a été décidé, et pourquoi
    -- Le RETOUR à celui qui a signalé (#451) : date à laquelle l'arbitrage courant
    -- lui a été annoncé. NULL = il ne sait pas encore. Remis à NULL à CHAQUE
    -- changement d'état, pour qu'un signal ré-arbitré soit re-annoncé — sinon un
    -- « traité » corrigé en « refusé » resterait su sous sa première version.
    notified_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_usage_signals_signal ON usage_signals(signal, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_usage_signals_target ON usage_signals(signal, target, created_at DESC);
-- ⚠️ PAS d'index sur `status` ici. La colonne est ajoutée par ALTER dans init_db, et
-- sur une table qui EXISTE le CREATE TABLE ci-dessus est un no-op : elle n'existe donc
-- pas encore à ce point du DDL. Son index vit avec son ALTER, comme idx_tool_calls_run
-- et idx_tool_calls_org. Le commentaire du bloc précédent le dit déjà ; l'oublier a
-- coûté un boot en échec (`column "status" does not exist`) et un rollback automatique
-- de la preprod le 27/08 — le même piège que le 2026-06-25, à trois lignes de son
-- propre avertissement. La mention « table neuve → indexes inline sûrs » plus haut ne
-- vaut QUE pour les colonnes nées avec la table.
"""

# Registre des mois du journal archivés au froid (#665) — fragment séparé : la
# révision Alembic `0005_journal_archives` l'exécute tel quel (une seule écriture du DDL).
JOURNAL_ARCHIVES = """
-- Registre des mois du journal ARCHIVÉS au froid (#665, arbitrage du 23/09/2026,
-- option B). Écrit par `deploy/archive_tool_calls.py` APRÈS la relecture de l'archive
-- et AVANT la suppression : une ligne ici dit que le corps de ce mois a quitté la
-- base, où il se trouve, et depuis quand. Les faits de run (`run_start`/`run_finish`)
-- restent, eux : un run archivé garde ses bornes, et sa page lit CE registre pour dire
-- « contenu archivé le … » au lieu de servir une timeline réduite à deux lignes.
-- Table neuve, née entière (aucune colonne posée par ALTER) ; fragment à part pour que
-- la révision `0005_journal_archives` l'exécute tel quel.
CREATE TABLE IF NOT EXISTS journal_archives (
    mois TEXT PRIMARY KEY,                    -- 'YYYY-MM', mois calendaire de created_at
    cle TEXT NOT NULL,                        -- objet S3 (privé) qui porte le mois
    lignes BIGINT NOT NULL,                   -- enregistrements relus dans l'archive
    archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

# Occurrences d'un signal d'usage (oto-backend : signaux rattachés) — fragment séparé :
# la révision Alembic `0043_signal_occurrences` l'exécute tel quel (une seule écriture
# du DDL), et le démarrage d'une base neuve le pose avec le reste.
SIGNAL_OCCURRENCES = """
-- Les OCCURRENCES d'un signal d'usage. Un signal de même org, même type (`signal`) et
-- même cible (`target`) déposé alors qu'un signal de cette clé attend un arbitrage
-- (open | acknowledged) n'en crée plus un nouveau : il s'y RATTACHE, ici. Mesuré le
-- 06/10/2026 : 338 signaux en attente, dont une quarantaine de redites (douze fois la
-- même valeur absente d'une procédure, huit fois la même clé morte) — la pile comptait
-- des répétitions comme des sujets.
-- Rien n'est perdu (la règle du ré-aiguillage : un signal est un FAIT) : chaque
-- occurrence garde son auteur, sa session, son genre et son texte. La ligne de
-- `usage_signals` reste la PREMIÈRE occurrence ; le compte servi = 1 + les lignes d'ici.
-- Table neuve, née entière (aucune colonne posée par ALTER).
CREATE TABLE IF NOT EXISTS usage_signal_occurrences (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT NOT NULL REFERENCES usage_signals(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sub TEXT,
    kind TEXT NOT NULL,
    body TEXT,
    session_id TEXT,
    source TEXT NOT NULL DEFAULT 'agent'
);
CREATE INDEX IF NOT EXISTS idx_usage_signal_occurrences_signal
    ON usage_signal_occurrences(signal_id, created_at DESC);
"""

# Totaux du journal PAR JOUR UTC (oto-backend#1147) — fragment séparé : la révision
# Alembic `0050_journal_totaux_jour` l'exécute tel quel (une seule écriture du DDL).
JOURNAL_JOUR = """
-- Les totaux du journal d'appels par JOUR UTC (oto-backend#1147). Les écrans de
-- consommation et de monitoring lisent ces totaux, plus le journal brut : relire
-- `tool_calls` (12 M lignes, `args` compris) à chaque vue d'une fenêtre de 30 ou 90
-- jours a contribué à saturer la base partagée le 04/10/2026 (#1145).
-- Trois tables, alimentées ENSEMBLE par `db/journal_jour.consolider_jour`, une
-- transaction par jour (maintenance quotidienne pour J-1, rattrapage à la main pour
-- l'historique) ; lues par `db/journal_jour.source`, qui joint les jours consolidés et
-- le journal direct pour le reste de la fenêtre. Tables neuves, nées entières.
--
-- Le REGISTRE : un jour y figure = ses totaux sont COMPLETS. C'est lui, et non la
-- présence de lignes de totaux, qui dit qu'un jour est consolidé — un jour sans aucun
-- appel est consolidé et n'a aucune ligne de totaux.
CREATE TABLE IF NOT EXISTS journal_jours_consolides (
    jour DATE PRIMARY KEY,
    lignes BIGINT NOT NULL,                    -- lignes du journal agrégées ce jour-là
    consolide_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Les TOTAUX, une ligne par combinaison des dimensions que lisent les écrans. Les
-- natures agrégées sont `mcp` et `connector` (`journal_jour.KINDS`) : REST, protocole
-- et transport restent au journal. `durees` et `tailles` portent les VALEURS non
-- nulles (4 octets par valeur) : un p95 se recalcule exactement sur leur union
-- (`percentile_cont`), là où un histogramme à seaux l'aurait approché.
CREATE TABLE IF NOT EXISTS journal_totaux_jour (
    jour DATE NOT NULL REFERENCES journal_jours_consolides(jour) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    org_id BIGINT,
    sub TEXT,
    tool TEXT NOT NULL,
    ok BOOLEAN NOT NULL,
    key_mode TEXT,
    client_name TEXT,
    appels INTEGER NOT NULL,
    quantite BIGINT NOT NULL,                  -- somme de COALESCE(quantity, 1)
    duree_n INTEGER NOT NULL,
    duree_somme BIGINT NOT NULL,
    durees INTEGER[] NOT NULL,
    taille_n INTEGER NOT NULL,
    taille_somme BIGINT NOT NULL,
    tailles INTEGER[] NOT NULL,
    dernier_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_totaux_jour_jour ON journal_totaux_jour (jour);
CREATE INDEX IF NOT EXISTS idx_journal_totaux_jour_org ON journal_totaux_jour (org_id, jour);
CREATE INDEX IF NOT EXISTS idx_journal_totaux_jour_sub ON journal_totaux_jour (sub, jour);
-- Les CLÉS DISTINCTES qu'un total ne peut pas porter : un nombre de jobs distincts ne
-- s'additionne pas d'un jour à l'autre (un job relevé lundi et mardi compte une fois).
-- Les jobs fournisseur relevés par les appels facturables (`kind='mcp'`, `ok`, sous une
-- org), un par jour : le relevé compte les distincts sur l'union des jours.
CREATE TABLE IF NOT EXISTS journal_jobs_jour (
    jour DATE NOT NULL REFERENCES journal_jours_consolides(jour) ON DELETE CASCADE,
    org_id BIGINT NOT NULL,
    tool TEXT NOT NULL,
    key_mode TEXT,
    job_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_jobs_jour_org ON journal_jobs_jour (org_id, jour);
"""
