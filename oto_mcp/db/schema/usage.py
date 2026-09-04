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
    error_kind TEXT
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
