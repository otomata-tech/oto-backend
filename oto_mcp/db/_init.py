"""Initialisation du schéma + migrations idempotentes au boot.

Extrait de l'ex-monolithe `db.py` (barreau 2). `init_db()` applique `_SCHEMA`
puis les ALTER/backfill idempotents. Appelé une fois au démarrage du serveur.
"""
from __future__ import annotations

import json
import logging
import re
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

    **Ne fait plus QUE le schéma** (ADR 0065, lot 0). Les quatre travaux de maintenance
    qui suivaient la transaction — purge du journal, purge du fil des runs, parse des
    blocs de nœuds, index de clé métier par namespace — sont partis en commandes
    explicites (`oto-mcp maintenance …`, cf. `oto_mcp/maintenance.py`) tirées par un
    timer. Ils avaient la forme d'un cron et le coût d'un cron : le seul poste du boot
    qui grandit avec la base, dans une fenêtre de healthcheck finie (120 s).

    ⚠️ Cette fonction n'a **pas** de garde par process : deux appels de suite RE-JOUENT
    la séquence, et c'est ce qui permet aux tests de prouver son idempotence
    (`init_db(); init_db()` doit être un no-op). La garde « une fois par boot » est au
    point d'appel, dans `server._prepare_database`, où elle couvre aussi les backfills.
    """
    attempts = max(1, int(os.environ.get("OTO_MCP_INIT_DB_ATTEMPTS", "3")))
    for attempt in range(1, attempts + 1):
        debut = time.monotonic()
        try:
            _init_db_once()
            logger.info("boot: init_db %.0f ms", (time.monotonic() - debut) * 1000)
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


def _init_db_once() -> None:
    with _connect() as conn:
        apply_boot_schema(conn)


def replay_boot_schema_dry(conn: psycopg.Connection) -> None:
    """Rejoue l'ordre du boot sur `conn`, dans une transaction **ANNULÉE**.

    Le garde-fou qui manquait le 2026-08-27 (#450, oto-backend#426) : un `CREATE INDEX`
    posé dans le DDL de base sur une colonne qui naît d'un `ALTER` plus bas. Le DDL
    assemblé passait de son côté, la migration passait du sien — **c'est leur ORDRE
    qui échouait**, et rien ne jouait cet ordre ailleurs qu'au démarrage d'un vrai
    serveur, donc ni la CI ni un push ne pouvaient l'attraper. Ici il se joue partout,
    y compris contre une base SERVIE, sans y laisser de trace.

    Ce n'est PAS une lecture seule : les ordres s'exécutent réellement (c'est tout
    l'intérêt — un `ALTER` sur une colonne absente doit lever). Ils sont annulés au
    sortir. Sur une base servie, le prix est celui d'une transaction de DDL qui prend
    ses verrous puis les rend : à jouer hors heures de pointe, comme un boot.
    """
    with conn.transaction(force_rollback=True):
        apply_boot_schema(conn)


def _poser_domaine(conn, table: str, nom: str, colonne: str,
                   valeurs: tuple[str, ...]) -> bool:
    """Pose une contrainte de domaine — **et seulement si elle a changé**.

    ⚠️ Un `ADD CONSTRAINT … CHECK` **valide la contrainte sur toute la table**,
    sous verrou `ACCESS EXCLUSIVE`. Ce n'est pas une opération de métadonnée :
    c'est un parcours complet. Le faire à chaque démarrage, y compris quand la
    contrainte est déjà exactement celle qu'on repose, est un coût qui **croît
    avec la table** — mesuré le 03/09 : 3 ms à 11 000 lignes, 11 ms à 100 000, et
    quelques secondes de verrou exclusif à dix millions.

    ⚠️ Pourquoi pas `ADD CONSTRAINT IF NOT EXISTS` : ça n'existe pas en
    PostgreSQL, et le motif conditionnel déjà employé ailleurs dans ce fichier
    (`IF NOT EXISTS (SELECT 1 FROM pg_constraint …)`) ne convient pas ici — il ne
    ferait **rien** quand la contrainte existe avec une définition PÉRIMÉE, ce qui
    est précisément le cas qu'on doit traiter : ajouter une valeur au domaine
    d'une table déjà déployée.

    D'où la comparaison des VALEURS, pas du texte : `pg_get_constraintdef`
    normalise sa sortie (espaces, parenthèses), donc une égalité littérale serait
    fragile. L'égalité d'ENSEMBLES détecte l'ajout comme le retrait.

    Rend True si la contrainte a été (re)posée — c'est ce que le banc mesure.
    """
    row = conn.execute(
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint WHERE conname = %s",
        (nom,),
    ).fetchone()
    if row and set(re.findall(r"'([^']*)'", row["def"])) == set(valeurs):
        return False
    liste = ", ".join(f"'{v}'" for v in valeurs)
    conn.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {nom}")
    conn.execute(f"ALTER TABLE {table} ADD CONSTRAINT {nom} "
                 f"CHECK ({colonne} IN ({liste}))")
    logger.info("contrainte %s (re)posée sur %s", nom, table)
    return True


def apply_boot_schema(conn: psycopg.Connection) -> None:
    """Le DDL du boot, en UNE transaction, sur la connexion qu'on lui passe.

    Extrait de `_init_db_once` (ADR 0065, lot 0) pour une seule raison : le rendre
    REJOUABLE AILLEURS QUE SUR LE CHEMIN DU DÉMARRAGE. Tant que la séquence était
    soudée à sa propre connexion, ni la CI ni une session ne pouvaient la jouer
    contre une base servie pour voir si elle passe — et c'est exactement ce qui a
    manqué le 2026-08-27 (#450) : un index posé dans le DDL sur une colonne née
    d'un ALTER. Ni le DDL seul ni la migration seule n'échouaient ; leur ORDRE si.
    `replay_boot_schema_dry` s'en sert pour rejouer cet ordre dans une transaction
    annulée. Le corps n'a pas changé d'une ligne — seulement d'indentation.
    """
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
    # L7 (blueprint ADR 0053) — QUI a écrit cette observation. Prod et preprod
    # partagent la MÊME base : sans cette colonne, leurs compteurs se mélangent et
    # « une fenêtre en prod » n'est pas lisible. Dérivée de ce que le process sait de
    # lui-même (`config.origine_du_process`), jamais d'un réglage à poser.
    # **NULLABLE, et c'est le point** : les lignes écrites avant cette colonne
    # restent NULL — elles sont réellement ambiguës, et on ne réécrit pas l'histoire
    # en leur prêtant une origine qu'on ne connaît pas.
    # ⚠️ La clé primaire, elle, ne bouge PAS ici : l'y étendre est un ordre NON
    # additif (DROP CONSTRAINT), donc une commande explicite — `scripts/
    # migrate_shadow_origine.py`, ADR 0065. Tant qu'elle n'a pas tourné, deux
    # origines partagent une ligne ; l'écriture le sait et se dégrade au
    # comportement d'avant (cf. `db/access_shadow.bump_shadow`).
    conn.execute("ALTER TABLE access_shadow_l7 ADD COLUMN IF NOT EXISTS origine TEXT")
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
    # Chantier runner R4 (fleet comme produit) : un job dit à quelle FLOTTE il
    # appartient. La table `runner_fleets` est créée par le DDL ; sur une base qui
    # existe déjà, seule la colonne manque — et sans elle un passage n'est lisible
    # qu'en corrélant des horodatages à la main.
    # ⚠️ La FK voyage AVEC l'ALTER, sinon une base fraîche (qui la reçoit par le
    # CREATE TABLE) et la prod (qui ne reçoit que l'ALTER) divergent pour toujours,
    # et rien ne le rattraperait. Même forme que `org_invitations.group_id`.
    conn.execute("ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS fleet_id BIGINT "
                 "REFERENCES runner_fleets(id) ON DELETE SET NULL")
    # L'index SUIT l'ALTER : dans `_schema` il s'exécuterait avant que la colonne
    # existe sur une base déjà construite, et le boot mourrait (piège du 20/07).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_runner_jobs_fleet "
                 "ON runner_jobs(fleet_id) WHERE fleet_id IS NOT NULL")
    # `expired` (02/09) : un travail programmé que personne n'a pris dans son
    # cycle. ⚠️ Le CHECK d'une base existante refuserait la valeur — et le refus
    # tomberait au TICK, pas au boot, donc loin de sa cause.
    # `sub` (02/09) : l'identité que l'agent porte en exécutant ce travail.
    conn.execute("ALTER TABLE runner_jobs ADD COLUMN IF NOT EXISTS sub TEXT")
    _poser_domaine(conn, "runner_jobs", "runner_jobs_status_check", "status",
                   ("pending", "claimed", "done", "failed", "expired"))
    # Chantier runner R4b : l'INTENTION se sépare du FAIT. `armed` (on a demandé
    # que ça tourne) ≠ `running` (un ordonnanceur l'a prise) ; `stopping` (l'arrêt
    # est demandé) ≠ `stopped` (il a été accusé). ⚠️ Un `CREATE TABLE IF NOT
    # EXISTS` NE MET PAS À JOUR la contrainte d'une table qui existe déjà : sans
    # ce remplacement, le boot passerait en vert et la base refuserait les deux
    # nouveaux états — un lot « déployé » dont la moitié est rejetée à l'écriture.
    # Le dénominateur de l'avancement d'un passage (cf. le DDL). Sur une base qui
    # existe déjà, le CREATE TABLE est sauté — seule la colonne manque.
    conn.execute("ALTER TABLE runner_fleets ADD COLUMN IF NOT EXISTS rows_at_launch INT")
    conn.execute("ALTER TABLE runner_fleets ADD COLUMN IF NOT EXISTS armed_at TIMESTAMPTZ")
    conn.execute("ALTER TABLE runner_fleets ADD COLUMN IF NOT EXISTS stopping_at TIMESTAMPTZ")
    _poser_domaine(conn, "runner_fleets", "runner_fleets_status_check", "status",
                   ("draft", "armed", "running", "stopping", "stopped", "done",
                    "failed"))
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
    # (03/09/2026) La PALETTE de ce tenant, pour ce qu'on lui dessine — aujourd'hui
    # les emails. Elle vivait en dur dans `email_brand.MARQUES`, comme l'adresse de
    # son tableau de bord y avait vécu avant : accueillir un second partenaire
    # demandait d'éditer notre code et de le redéployer pour lui. Même raison, même
    # remède, même colonne de configuration par tenant. `{}` = notre charte.
    # ⚠️ Ce n'est PAS un thème complet : seulement les sept teintes que le rendu
    # d'email consomme (`email_brand.Marque`), validées à la lecture. Une clé
    # inconnue est ignorée, une valeur qui n'est pas une couleur aussi — un email
    # ne doit jamais casser parce qu'une configuration est mal remplie.
    conn.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS brand JSONB "
                 "NOT NULL DEFAULT '{}'::jsonb")
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
    # oto#25 lot (b1) : le RÉSULTAT de `error_taxonomy.classify()` (son `.code`,
    # ex. `not_authorized`), écrit par `calllog._record` sur un échec — un refus
    # d'authentification amont devient un FAIT lisible dans la colonne, plus un
    # texte à interpréter. NULL sur un succès (rien à rapporter) et sur tout
    # l'historique antérieur (non reconstructible depuis le texte tronqué de
    # `error`). ⚠️ PAS d'index ici, même raison que `request_id`/`call_uid`/
    # `effective_sub` ci-dessus : lecture d'enquête, pas un chemin chaud — un
    # index de plus sur `tool_calls` se paie à CHAQUE appel journalisé.
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS error_kind TEXT")
    # ⚠️ La base est PARTAGÉE prod/preprod : le `CREATE TABLE` est sauté sur une
    # table qui existe déjà, donc une colonne neuve n'arrive QUE par ici.
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS token_id BIGINT")
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS token_kind TEXT")
    # #340 : la taille du texte SERVI à l'appelant, en caractères. La durée dit ce
    # qu'un appel a coûté au serveur, jamais ce qu'il a coûté à la fenêtre de
    # l'agent. NULL sur tout l'historique — non reconstructible, et c'est `sized`
    # dans les agrégats qui dit sur combien d'appels la mesure porte.
    # ⚠️ Cet ALTER était d'abord posé dans `_migrate_tool_call_log`, qui a l'air
    # d'être « l'endroit des colonnes de tool_calls » et qui n'en est PAS un : elle
    # sort dès la première ligne quand `tool_call_log` n'existe plus, ce qui est le
    # cas partout depuis juin 2026. La colonne ne serait jamais arrivée en base, et
    # le tableau de bord aurait affiché des vides qu'on aurait lus « ces outils ne
    # servent rien ». Un banc joue désormais ce scénario (DROP COLUMN puis init_db).
    conn.execute("ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS result_size INTEGER")
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
    # #487 : le journal des acceptations. LOT A, ADDITIF — la table
    # `legal_acceptances` et sa PK `(sub, doc_slug)` restent INTACTES, parce que
    # le code servi en PRODUCTION y fait encore son `ON CONFLICT (sub, doc_slug)`
    # et que prod et preprod partagent la base (`docs/live-migrations.md`).
    # Retirer cette unicité — ce qu'un historique dans la même table exigerait —
    # casserait le `POST /api/me/legal/accept` de la prod, c'est-à-dire le gate
    # CGU de l'INSCRIPTION, pendant toute la fenêtre entre le déploiement preprod
    # et le tag. Le journal est donc une table NEUVE (`_schema.py`), et
    # `legal_acceptances` devient une projection maintenue en écriture double le
    # temps de la fenêtre. Son retrait est l'issue #507, et il a sa garde : ne pas
    # l'exécuter tant que la prod ne sert pas le code qui lit le journal.
    #
    # RECOPIE À CHAQUE BOOT, et non un one-shot : pendant la fenêtre, la prod
    # écrit dans la projection SEULE (elle ne connaît pas le journal). Sans cette
    # reprise, une acceptation donnée en prod entre le boot preprod et le tag ne
    # rejoindrait JAMAIS le journal — et comme le journal est ce que le gate lit,
    # elle serait invisible : on redemanderait ses CGU à quelqu'un qui vient de
    # les accepter. Le boot du tag de prod rattrape donc tout ce que la fenêtre a
    # produit. C'est le patron « copie legacy→cible à CHAQUE boot » du playbook ;
    # après le drop de #507, la garde `to_regclass` la rend inerte.
    #
    # Idempotente par anti-jointure sur (sub, doc, version, accepted_at) : une
    # ligne déjà recopiée ne l'est pas deux fois, et une acceptation RÉÉCRITE par
    # la prod (nouvelle version, ou même version restampée) entre bien, puisque
    # son `accepted_at` a changé. L'index `idx_legal_events_dernier` sert
    # l'anti-jointure comme il sert la lecture du gate.
    #
    # `context`/`ip`/`user_agent`/`org_id` restent NULS sur ces lignes : la
    # projection ne les a jamais portés, et les inventer ferait mentir une trace
    # dont tout l'intérêt est de servir de preuve.
    if conn.execute("SELECT to_regclass('legal_acceptances') AS t").fetchone()["t"]:
        conn.execute("""
            INSERT INTO legal_acceptance_events (sub, doc_slug, version, accepted_at)
            SELECT l.sub, l.doc_slug, l.version, l.accepted_at
              FROM legal_acceptances l
             WHERE NOT EXISTS (
                 SELECT 1 FROM legal_acceptance_events e
                  WHERE e.sub = l.sub AND e.doc_slug = l.doc_slug
                    AND e.version = l.version AND e.accepted_at = l.accepted_at)
        """)
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
    # ⚠️ REMONTÉE ICI le 2026-09-01 (#781) — elle vivait plus bas, avec son
    # backfill (ADR 0042, barreau 1). Les index de recherche construits juste
    # après portent `WHERE delivery = 'on-demand'` : sur une base qui existe
    # déjà, la colonne n'arrive que par cet ALTER, donc l'ordre est une
    # contrainte d'exécution, pas une mise en page. Le backfill des readmes
    # `init`, lui, reste à sa place.
    conn.execute("ALTER TABLE guides ADD COLUMN IF NOT EXISTS delivery TEXT NOT NULL DEFAULT 'on-demand'")
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
    # ⚠️ Ce littéral est de l'HISTOIRE, pas la constante `capabilities.kb.KB_NAME` —
    # qui vaut « Knowledge base » depuis le 2026-09-03 (#527). Le remplacer par la
    # constante ferait rater l'ancre de toutes les KB posées avant cette date, qui
    # sont précisément les seules que ce backfill vise.
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
    # #605 (2026-08-29) : périmètre d'URL du projet — motifs canoniques (`hôte/chemin/`
    # ou `hôte/*`) que les outils de recherche écartent et que les outils d'extraction
    # refusent. Une seule lecture : `url_perimeter.perimeter_of_call`.
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS excluded_url_prefixes TEXT[] NOT NULL DEFAULT '{}'")
    # ADR 0043 : id du mandat (mdt_xxx Mollie) sur l'abonnement — la table
    # existait déjà (B1) quand la colonne est arrivée.
    conn.execute("ALTER TABLE org_subscriptions ADD COLUMN IF NOT EXISTS mandate_id TEXT")
    # #829 : POURQUOI une échéance n'a pas pu être prélevée. Le runner butait en
    # silence (une `log.error` dans un journal qui ne remonte qu'à ~24 h), sans
    # avancer le cycle ni fermer le droit : le service continuait gratuitement, et
    # plus aucune donnée ne disait depuis quand. `block_since` est cette donnée.
    for _col, _type in (("block_code", "TEXT"), ("block_detail", "TEXT"),
                        ("block_since", "TIMESTAMPTZ"),
                        ("block_seen_at", "TIMESTAMPTZ")):
        conn.execute(f"ALTER TABLE org_subscriptions "
                     f"ADD COLUMN IF NOT EXISTS {_col} {_type}")
    # 2026-09-02 : ÉCHÉANCE d'un don d'option. Nullable sans défaut, donc
    # instantané et sans réécriture (PG 11+) sur une base partagée avec la prod —
    # et surtout ADDITIF au sens du droit : les 35 dons déjà posés restent à NULL,
    # c'est-à-dire perpétuels, exactement ce qu'ils étaient. Poser une date est un
    # acte admin explicite, ligne par ligne ; la retirer rouvre.
    conn.execute("ALTER TABLE option_comps ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ")
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
    # ⚠️ REMONTÉE ICI le 2026-09-01 (#781) — elle vivait plus bas (ADR 0032 §6 /
    # 0029, B6 : mode typé optionnel d'un namespace). La conversion #317 juste
    # après LIT `d.schema` : sur une base qui existe déjà, la colonne n'arrive
    # que par cet ALTER, et l'`UPDATE` mourait avant lui.
    conn.execute("ALTER TABLE user_datastores ADD COLUMN IF NOT EXISTS schema JSONB")
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
    # (La colonne `delivery` elle-même est posée BEAUCOUP plus haut : les index
    # de recherche la lisent dans leur prédicat — cf. le renvoi là-bas, #781.)
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
    #
    # ⚠️ **La SEULE projection qui survit à l'arrêt de la recopie (2026-09-01).**
    # Les cinq autres — projets, pages, procédures, tableaux, lignes — sont
    # retirées plus bas ; celle-ci reste, et pour une raison qui n'est pas la
    # symétrie : `db/guides.py` écrit ses cinq gestes DIRECTEMENT dans `nodes`,
    # la table `guides` n'a donc plus d'écrivain applicatif — mais elle en garde
    # UN, le seed `secret_sauce` semé quelques lignes plus haut, et ce seed est
    # le seul chemin par lequel le readme plateforme arrive sur une base NEUVE.
    # Mesuré en prod le 2026-09-01 : 23 lignes, toutes déjà dans `nodes`, aucune
    # plus récente que son nœud — donc no-op stable sur la base vivante, et
    # indispensable au premier boot d'une base vide.
    # Elle s'en ira quand le seed sèmera nativement dans `nodes` ; la retirer
    # avant, c'est retirer une chose sans lui donner de remplaçant.
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
    # (ADR 0032 §6 / 0029, B6 : `user_datastores.schema` est posée BEAUCOUP plus
    # haut — la conversion #317 la lit avant ce point. Cf. le renvoi là-bas, #781.)
    # gap #4a : partage public d'un doc (token de lien public, lookup indexé).
    conn.execute("ALTER TABLE docs ADD COLUMN IF NOT EXISTS public_token TEXT")
    # ADR 0032 (« stop using slug ») : id surrogate stable + globalement unique pour
    # les guides. `org_instructions` garde (org_id, slug) comme clé naturelle
    # interne ; l'`id` devient l'identité PUBLIQUE (URL, project_links, runs). Backfill
    # des lignes existantes via une séquence (idempotent).
    conn.execute("ALTER TABLE org_instructions ADD COLUMN IF NOT EXISTS id BIGINT")
    conn.execute("CREATE SEQUENCE IF NOT EXISTS org_instructions_id_seq OWNED BY org_instructions.id")
    conn.execute("UPDATE org_instructions SET id = nextval('org_instructions_id_seq') WHERE id IS NULL")
    conn.execute("ALTER TABLE org_instructions ALTER COLUMN id SET DEFAULT nextval('org_instructions_id_seq')")
    conn.execute("ALTER TABLE org_instructions ALTER COLUMN id SET NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_org_instructions_id ON org_instructions(id)")
    # ADR 0068 (04/09/2026) — le palier PERSONNEL des procédures, phase 2 de #681.
    # `org_id` était NOT NULL : elle porte l'org PARENTE du propriétaire (une org est
    # la sienne, une équipe tient la sienne dans `org_groups`) et la cascade de
    # suppression. Une personne n'a pas d'org parente — s'y ranger l'org de contexte
    # ferait supprimer une procédure PERSONNELLE avec l'org, ce que le store refusait
    # explicitement d'écrire plutôt que de poser une ligne bancale. On relâche donc la
    # colonne : une procédure perso porte `org_id = NULL`, ce qui est le fait, et non
    # une org qui ne la possède pas.
    # ⚠️ Geste RELÂCHANT : il n'invalide aucune ligne existante et se rejoue sans
    # effet. Les deux tables bougent ensemble — l'historique porte la même clé de
    # propriétaire que la table vivante, et le laisser NOT NULL ferait échouer la
    # PREMIÈRE écriture d'une procédure perso, pas sa création.
    conn.execute("ALTER TABLE org_instructions ALTER COLUMN org_id DROP NOT NULL")
    conn.execute("ALTER TABLE org_instruction_revisions ALTER COLUMN org_id DROP NOT NULL")
    # Archivage (soft-delete) d'une procédure : masquée de tous les listings —
    # y compris ceux que l'IA lit (`skills_index_md`, `oto_procedure op=list`,
    # l'index de guide) — mais la ligne ET son historique de révisions
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
    # B3 : migrer les liens projet→procédure de slug vers l'id de guide (org-owned ;
    # les projets user-owned gardent le slug, résolu à la lecture côté front). Idempotent
    # (guard `!~ '^[0-9]+$'` = pas déjà un id ; JOIN = seulement si le guide existe).
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
    # REFUS de l'invité (oto-backend#654) : jusqu'ici seul l'ÉMETTEUR pouvait retirer
    # une invitation (révocation), l'invité ne pouvait que ne pas l'accepter — donc
    # garder un badge qu'il ne pouvait pas éteindre. État PROPRE, jamais `accepted_at`
    # (cf. le commentaire du DDL). Deux ADD COLUMN idempotents, sans réécriture de
    # table : pas de travail au boot, la fenêtre du healthcheck n'en voit rien.
    conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS declined_at TIMESTAMPTZ")
    conn.execute("ALTER TABLE org_invitations ADD COLUMN IF NOT EXISTS declined_sub TEXT")
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
    # Mise en pause d'un compte (2026-09-03) : neutraliser sans détruire. NULL
    # partout à la pose — la colonne n'a d'effet que sur acte d'opérateur.
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_at TIMESTAMPTZ")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_by TEXT")
    conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS suspended_reason TEXT")
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
    # instance (oto, un tenant tiers). NULL = oto (le défaut, l'écrasante majorité) ; posé
    # = l'org vit sous un front tiers, dont les liens sortants et la marque des
    # mails doivent porter SES couleurs, pas les nôtres :
    #   front_base_url = base des liens publics (ex. https://app.<tenant>.ai)
    #   front_brand    = marque écrite dans les mails (ex. "<tenant>")
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
    # `kind` (04/09) : distinguer les jetons de l'UTILISATEUR de ceux de
    # l'EXÉCUTION. Les jetons de délégation émis avant cette colonne prennent le
    # défaut `user` — ils sont donc encore listés. Ils sont expirés depuis
    # longtemps ; la purge des expirés de type `delegation` ne les attrapera pas
    # (ils portent `user`), et c'est assumé : réétiqueter d'après un libellé
    # serait exactement le filtre sur texte libre qu'on refuse.
    conn.execute("ALTER TABLE user_api_tokens ADD COLUMN IF NOT EXISTS "
                 "kind TEXT NOT NULL DEFAULT 'user'")
    # L6 pièce 2 : le MOTIF d'un archivage d'instance. La base est PARTAGÉE
    # prod/preprod — le `CREATE TABLE` du schéma ne sert qu'aux installs vierges,
    # une table déjà là ne reçoit ses colonnes que par cet `ALTER`. Additif,
    # nullable, sans index : aucun travail au boot (ADR 0065).
    conn.execute("ALTER TABLE connector_instances "
                 "ADD COLUMN IF NOT EXISTS revoked_reason TEXT")
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
    # #295 — les sélections d'un connecteur DÉPOSÉ suivent son renommage. #279
    # (lot 3) a déposé `linkedin` au profit d'`aiark` (même fournisseur, même
    # client, même pool de crédits : la distinction n'était qu'un mode d'auth),
    # mais 119 lignes de sélection sont restées sur l'ancien nom — qui ne résout
    # plus rien, donc ne monte aucun outil : depuis le tag v1.69.0, ces membres
    # avaient perdu la toolbox LinkedIn sans un mot. Ici et pas en one-shot
    # manuel : la base est partagée preprod/prod, un boot doit pouvoir rejouer.
    # Sûr depuis v1.69.0 — plus aucun code servi ne lit `'linkedin'`.
    _conn_sel.rename_selection(conn, "linkedin", "aiark")
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
    #
    # ⚠️ SOUS SENTINELLE, et c'est le correctif du 2026-08-29. Les gestes se
    # croyaient rejouables parce qu'ils sont en `ON CONFLICT DO NOTHING` —
    # protection des lignes PRÉSENTES, alors que retirer un connecteur SUPPRIME la
    # sienne (`unselect` est un DELETE). Rejoués à chaque boot, ils réinstallaient
    # donc ce qu'on venait de retirer : un canal désélectionné revenait actif au
    # redémarrage, avec ses cinq voisins. Idem pour une disponibilité éteinte à la
    # main et une ACL d'org effacée. Un déménagement est vrai UNE fois ; ce qui
    # doit rester rejouable, c'est le boot, pas l'écriture (cf.
    # `split_fanout_pending`, qui explique aussi pourquoi la prod est marquée
    # sans réécriture).
    from ..connectors import activation as _conn_act_split
    _CANAUX_UNIPILE = ("linkedin_unipile", "whatsapp", "telegram",
                       "instagram", "messenger", "twitter")
    if _conn_sel.split_fanout_pending(conn, _CANAUX_UNIPILE):
        _conn_act_split.fanout_availability(conn, "unipile", _CANAUX_UNIPILE)
        _conn_act_split.fanout_acl(conn, "unipile", _CANAUX_UNIPILE)
        _conn_sel.fanout_selection(conn, "unipile", _CANAUX_UNIPILE)
        # Proposition d'org (`orgs.default_connectors`, consultatif) : une org qui
        # RECOMMANDAIT unipile recommande ses canaux. Sous la MÊME sentinelle et
        # pour la même raison : une org qui retire un canal de sa proposition le
        # voyait revenir au boot suivant.
        conn.execute(
            "UPDATE orgs SET default_connectors = ("
            "  SELECT ARRAY(SELECT DISTINCT unnest(default_connectors || %s::text[]))"
            ") WHERE default_connectors @> ARRAY['unipile']::text[] "
            "   AND NOT default_connectors @> %s::text[]",
            (list(_CANAUX_UNIPILE), list(_CANAUX_UNIPILE)),
        )
        # Posée EN DERNIER, dans la même transaction que les quatre gestes : une
        # passe interrompue ne laisse pas une sentinelle qui prétend qu'ils ont eu
        # lieu.
        _conn_sel.mark_split_fanout(conn)
    # === Le nœud gagne sa DONNÉE et son BAIL (2026-09-01, ADR 0054/0063) ========
    #
    # **Pourquoi une colonne et pas une clé de `props`.** Jusqu'ici, un nœud-ligne
    # rangeait ses valeurs métier dans `props`, à côté du titre, de la position et
    # des marques de la recopie. Deux natures très différentes s'y mélangeaient :
    # ce que le nœud EST pour la plateforme (titre, épingle, schéma d'enfants —
    # des clés qu'oto connaît et interprète) et ce que l'utilisateur y a MIS (les
    # colonnes de son tableau — des clés dont oto ne sait rien et qu'il ne doit
    # pas interpréter). Les tenir dans le même sac, c'est laisser une donnée
    # utilisateur nommée `title` ou `position` écraser le sens d'un nœud, et
    # obliger toute lecture à connaître la liste des clés réservées pour trier.
    # La frontière est celle du datastore (« oto gère les types standards, jamais
    # l'interprétation métier d'une valeur ») : elle mérite une colonne, pas une
    # convention de nommage.
    #
    # `ADD COLUMN` avec un `DEFAULT` constant ne réécrit pas la table (PG >= 11) :
    # instantané, aucun verrou long, aucun backfill — la base est partagée avec la
    # production.
    conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS data JSONB NOT NULL "
                 "DEFAULT '{}'::jsonb")
    # Le bail de la file de travail : `nodes` en portait DEUX colonnes sur cinq
    # (posées à la création de la table, sans lecteur), `datastore_rows` les cinq.
    # Un verrou qui ne sait pas sous quel run une ligne est réservée, ni combien de
    # fois elle a été reprise, ni pourquoi elle a été abandonnée, n'est pas le même
    # verrou — c'est celui d'avant les deux corrections qui l'ont rendu sûr
    # (le run qui libère ses baux, le plafond de reprises). Les trois manquantes se
    # posent ici pour que la file soit UNE mécanique servant deux tables, et non
    # deux mécaniques qui divergeront au premier correctif appliqué d'un seul côté.
    conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS claimed_run TEXT")
    conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS claims INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE nodes ADD COLUMN IF NOT EXISTS abandon_reason TEXT")
    # oto#70 lot 2, barreau 2 : la trace de l'écriture DÉCLARÉE. La table est née au
    # barreau 1 et vit déjà en base — `CREATE TABLE IF NOT EXISTS` ne la rattraperait
    # pas. Ce sont deux compteurs, pas une clé de plus : après le 1er octobre, ils
    # séparent l'écrivain qui s'est ADAPTÉ de celui qui a DISPARU, que les écritures
    # non déclarées confondent (elles tombent à zéro dans les deux cas).
    conn.execute("ALTER TABLE origine_ecritures "
                 "ADD COLUMN IF NOT EXISTS ecritures_declarees BIGINT NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE origine_ecritures "
                 "ADD COLUMN IF NOT EXISTS derniere_declaree_at TIMESTAMPTZ")
    # ⚠️ **Pas d'index de bail ici**, et c'est délibéré : le chemin de réservation
    # lit encore `datastore_rows`. Un index sur un prédicat que personne
    # n'interroge est un coût d'écriture pur, et sa forme utile dépend d'un
    # arbitrage de contrat (toute forme indexable en partiel change l'ordre
    # observable de la file). Il se posera avec la requête qui le justifie.
    # === La RECOPIE au boot est ARRÊTÉE (2026-09-01, ADR 0054/0063) ===
    # Cinq conversions tournaient ici à chaque démarrage — projets, pages,
    # procédures, tableaux, lignes — et déposaient dans `nodes` une IMAGE des
    # tables historiques, marquée `props.legacy`. Elles avaient un sens tant que
    # le plan était de faire LIRE `nodes` à l'ancienne surface : la copie
    # préparait une bascule.
    #
    # Ce plan est abandonné. Les deux univers vivent CÔTE À CÔTE — `oto_doc` et
    # les tables historiques d'un côté, `oto_node` et son stockage natif de
    # l'autre, chacun avec ses verbes, jusqu'au décommissionnement du premier.
    # Rien ne traduit plus l'un vers l'autre : il n'y a donc plus rien à
    # recopier, et l'image déjà déposée part par le travail de maintenance
    # `residu-projete`, qui s'exécute HORS du boot (son coût suit la taille de
    # la base — ADR 0065).
    #
    # ⚠️ Ce qui ne revient pas ici : la nouvelle surface part de VIDE et se
    # remplit par ses propres verbes. Réactiver une conversion ferait rentrer
    # ~70 000 nœuds sans lecteur, et rendrait à `props.legacy` un sens que le
    # code n'a plus. Seules restent natives dans `nodes` les écritures de
    # `db/guides.py`.
    #
    # Le DDL, lui, reste : l'index d'ownership NU cède la place au partiel posé
    # par `_SCHEMA` (`idx_nodes_owner_scoped`) — sans ce DROP, la base porterait
    # les DEUX et paierait le volume deux fois. Sorti de son garde `projects`
    # avec la conversion : c'est un geste sur `nodes`, il ne dépend d'aucune
    # table historique.
    conn.execute("DROP INDEX IF EXISTS idx_nodes_owner")
    # === La suppression d'un nœud emporte son CORPS — par contrainte (#800) ===
    _pose_cascade_blocs(conn)
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

    # ── #519 lot B4 : la bibliothèque publique s'appelle `guide_library` ─────────
    #
    # La TABLE garde son nom, et ce n'est pas de la timidité : la base est PARTAGÉE
    # prod/preprod, donc un `ALTER TABLE … RENAME` mergé sur `main` renommerait sous
    # la prod qui tourne encore l'ancien code. Le renommage physique est un acte de
    # TAG — lot D (#526), et une migration NOMMÉE (ADR 0065 étage 2), pas une ligne
    # de boot. Ici, une VUE porte le nom d'aujourd'hui et tout le code passe par
    # elle : au lot D, il ne restera qu'à droper la vue et renommer la table, sans
    # toucher une ligne de Python.
    #
    # Additive et idempotente, donc à sa place dans le DDL du boot (ADR 0065 étage
    # 1) : elle n'écrit aucune donnée, ne lit aucune table, et coûte une écriture de
    # catalogue.
    #
    # ⚠️ **En DERNIER, et c'est structurel.** Une vue `SELECT *` fige la liste de ses
    # colonnes AU MOMENT de sa création (vérifié sur PostgreSQL) : créée avant un
    # `ALTER TABLE … ADD COLUMN`, elle masquerait la colonne neuve — sans erreur,
    # sans log, avec un `None` là où le code attend une valeur. La rejouer à CHAQUE
    # boot, après tous les ALTER, la rend auto-entretenue ; `CREATE OR REPLACE`
    # accepte l'ajout d'une colonne en fin de liste. Garde-fou :
    # `tests/test_guide_library_view.py` compare les colonnes des deux relations sur
    # une vraie base.
    #
    # La vue est AUTO-UPDATABLE (une table, pas d'agrégat) : `INSERT` avec DEFAULT et
    # `RETURNING`, `ON CONFLICT DO UPDATE`, `UPDATE` et `DELETE` y passent tous —
    # mesuré, pas supposé, et rejoué par le garde-fou.
    conn.execute("CREATE OR REPLACE VIEW guide_library AS "
                 "SELECT * FROM doctrine_library")


def _pose_cascade_blocs(conn: psycopg.Connection) -> None:
    """`blocks.node_id` devient une clé étrangère `ON DELETE CASCADE` vers `nodes`.

    **Ce que ça ferme** : un nœud supprimé sans ses blocs laisse un corps que plus
    aucune requête ne relie à rien — toute lecture de `blocks` part de `node_id`.
    Deux chemins l'ont produit : la purge des conversions, arrêtée le 2026-09-01 en
    même temps que la recopie qui l'hébergeait, puis `db/guides.py::delete_guide_db`
    — la suppression d'une couche de contexte NATIVE, ouverte depuis le 2026-08-12 et
    encore ouverte le jour où #800 a été écrite. Le second chemin est la preuve que la
    discipline d'appelant ne tient pas cette arête : elle a manqué deux fois, la
    seconde sur du contenu dont rien n'est copie.

    **`NOT VALID`, et c'est le cœur du geste.** La base est PARTAGÉE prod/preprod
    (`docs/live-migrations.md`) : une contrainte posée ici s'applique instantanément
    à la production. Une contrainte VALIDE d'emblée refuserait de se poser tant qu'un
    seul orphelin traîne — donc ferait ÉCHOUER le boot, sur un état qu'on ne contrôle
    pas au moment du déploiement. `NOT VALID` se pose toujours, et — vérifié, pas
    supposé — **il enclenche quand même la cascade** : PostgreSQL crée les triggers
    référentiels dans tous les cas, `NOT VALID` ne saute que le parcours de
    vérification des lignes DÉJÀ là. Une insertion violante est refusée elle aussi.
    La fuite est donc fermée dès la pose, orphelins ou pas.

    **La validation vient ensuite, et seulement si elle peut.** `VALIDATE CONSTRAINT`
    échoue tant qu'un orphelin existe ; on ne le tente donc qu'après avoir compté, et
    on laisse la contrainte `NOT VALID` sinon — en le DISANT. Ce n'est pas un repli :
    dans les deux cas la fuite est fermée, seule la promesse sur le stock existant
    diffère. Une fois validée, la sonde de tête sort au premier aller-retour et ce
    code ne coûte plus rien au boot.
    """
    etat = conn.execute(
        "SELECT convalidated FROM pg_constraint "
        "WHERE conrelid = 'blocks'::regclass AND conname = 'blocks_node_fk'"
    ).fetchone()
    if etat is not None and etat["convalidated"]:
        return
    if etat is None:
        conn.execute(
            "ALTER TABLE blocks ADD CONSTRAINT blocks_node_fk "
            "FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE NOT VALID")
    orphelins = conn.execute(
        "SELECT count(*) AS n FROM blocks b "
        "WHERE NOT EXISTS (SELECT 1 FROM nodes n WHERE n.id = b.node_id)"
    ).fetchone()["n"]
    if orphelins:
        logger.warning(
            "boot: blocks_node_fk posée NOT VALID — %d bloc(s) orphelin(s) d'AVANT "
            "la contrainte. La cascade joue déjà et plus rien n'en produit ; ce stock "
            "est clos. Aucun travail de maintenance ne le retire, et c'est délibéré "
            "(#800 ③ : un balai sans provenance emportait du natif). Le boot qui "
            "suivra son retrait validera la contrainte.", orphelins)
        return
    conn.execute("ALTER TABLE blocks VALIDATE CONSTRAINT blocks_node_fk")


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

    ⚠️ **N'a jamais tourné en production** (oto-backend#421) : son appel vivait à la
    dernière ligne d'`init_db`, APRÈS une boucle dont chaque branche `return` ou
    `raise` — aucun chemin ne l'atteignait. Le lot 0 de l'ADR 0065 a retiré l'appel
    mort ; la fonction s'appelle maintenant par `oto-mcp maintenance
    key-index-rebuild`, **délibérément hors de `all` et sans timer** : la faire
    tourner pour la première fois change l'état de la production, c'est une décision,
    pas un effet de bord de sortie de maintenance.

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
            # Travail de FOND (CLI, hors chemin de requête) : pas de borne — cf.
            # `datastore_ensure_key_index`.
            datastore_ensure_key_index(int(r["id"]), r["k"], bornee=False)
            n += 1
        except Exception:  # noqa: BLE001
            # Un namespace dont les données portent DÉJÀ un doublon sur la clé refuse
            # l'index — c'est un fait à voir, pas un boot à casser. Les autres passent.
            logger.exception("clé métier: index non migré pour le namespace %s", r["id"])
    logger.info("clé métier: %s index migrés en expression polymorphe", n)
    return n

