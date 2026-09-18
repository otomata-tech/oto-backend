"""Inventaire des variables d'environnement lues par ce backend — SOURCE UNIQUE.

oto-backend#968 (ADR 0070, chantier « découpage cœur/commerce »). Trois surfaces en
dérivent, pour qu'il n'existe jamais deux listes qui divergent :

1. `tests/test_env_inventory_complet.py` — un walker AST qui parcourt `oto_mcp/**/*.py`,
   trouve CHAQUE lecture d'une variable d'environnement, et échoue si l'une d'elles
   n'est pas ici. Il ne mord que dans un sens : une entrée d'ici que le code a cessé de
   lire ne fait rougir personne (même asymétrie que `test_org_store_surface_frozen.py`
   FROZEN — un retrait de lecture n'oblige pas à toucher l'inventaire dans le même
   commit).
2. `scripts/generer_env_example.py` — régénère `.env.example` depuis `NOMS_FIXES`.
3. Quiconque doit savoir, avant de démarrer une instance, ce qui est vraiment exigé.

## Les 3 classes

- **REQUISE** (a) — le boot ou le premier appel qui en dépend échoue proprement sans
  elle (soit via `config.require_env`, soit « de fait » : une lecture nue suivie d'un
  `raise` nommé au premier usage réel, pas au moment de la lecture — ce sont ces
  sites-là que `refs` pointe).
- **NOTRE_DEFAUT** (b) — absente, la valeur par défaut pointe chez NOUS (un domaine, une
  adresse, un e-mail à nous). Une instance tierce doit l'écraser sous peine de nous
  envoyer du trafic qui ne nous appartient pas. Aucune garde de boot n'est posée sur
  cette classe (décision du 15/09/2026, oto-backend#968) : elle se DOCUMENTE, elle ne
  se rend pas obligatoire.
- **REGLAGE** (c) — défaut neutre, légitime en toute instance (timeout, cadence,
  taille, rétention, interrupteur, secret optionnel dont l'absence dégrade proprement).

## Les 2 familles dynamiques

Deux endroits construisent un nom de variable au runtime (`f"{x}_ID"`, jamais un
littéral) : le quota par provider (`access/quotas.py`) et le credential de management
par annuaire tenant (`auth/facade.py`, dont `OTO_MCP_LOGTO_M2M_ID`/`_SECRET` est
l'instance du tenant PRIMAIRE — les autres viennent de `tenants.logto_mgmt.credential`
en base). Le walker les reconnaît par la FORME de la construction (préfixe/suffixe
littéral autour d'un segment dynamique), jamais par une énumération de noms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Classe(str, Enum):
    REQUISE = "requise"            # (a) boot/appel échoue proprement sans elle
    NOTRE_DEFAUT = "notre_defaut"  # (b) défaut = une valeur À NOUS si non déclarée
    REGLAGE = "reglage"            # (c) défaut neutre légitime


@dataclass(frozen=True)
class Variable:
    nom: str
    classe: Classe
    # `None` seulement pour la classe REQUISE (rien à afficher : elle n'a pas de
    # défaut). Chaîne vide légitime pour (b)/(c) quand le code teste `if raw:`.
    defaut: "str | None"
    description: str
    refs: tuple[str, ...]   # "chemin/relatif.py:ligne", au moins une entrée


@dataclass(frozen=True)
class FamilleDynamique:
    """Une famille de noms construits au runtime — jamais un littéral au call site.

    `prefixe`/`suffixe` décrivent la forme (l'un des deux peut être vide) ; `motif`
    est le regex dérivé, utilisé par le walker pour reconnaître la CONSTRUCTION
    elle-même (un `ast.JoinedStr` dont les segments constants collés bout à bout,
    trou dynamique compris, matchent ce motif) — jamais un nom déjà résolu, puisqu'il
    ne l'est pas avant l'exécution."""
    nom: str
    prefixe: str
    suffixe: str
    classe: Classe
    description: str
    refs: tuple[str, ...]

    @property
    def motif(self) -> re.Pattern[str]:
        # Le "trou" dynamique est représenté par le walker avec un caractère hors
        # texte (U+E000, zone d'usage privé Unicode) qu'aucun code source légitime
        # n'écrit — \x00+ serait tout aussi sûr, mais ambigu à l'œil dans une trace.
        return re.compile("^" + re.escape(self.prefixe) + "+" + re.escape(self.suffixe) + "$")


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSE (a) — REQUISE
# ═══════════════════════════════════════════════════════════════════════════════

_REQUISES: tuple[Variable, ...] = (
    Variable("DATABASE_URL", Classe.REQUISE, None,
             "Connexion PG managée — source unique du pool applicatif ET des "
             "migrations Alembic (décision du 15/09/2026 : un message de boot "
             "nommé plutôt qu'une erreur psycopg opaque).",
             ("oto_mcp/db/_conn.py:57", "oto_mcp/db/migrations/env.py:51")),
    Variable("OTO_PROJECT_DOMAIN", Classe.REQUISE, None,
             "Domaine racine des endpoints de projet publiés (`<slug>.mcp.<D>`, "
             "`<slug>.share.<D>`). Décision du 15/09/2026 : plus de défaut muet vers "
             "`oto.cx`, même raison que l'URL publique — une instance tierce doit "
             "déclarer le sien, prod comprise.",
             ("oto_mcp/config.py:66",)),
    Variable("LOGTO_ENDPOINT", Classe.REQUISE, None,
             "Émetteur Logto qui SIGNE (vérification JWT ES384) — distinct de "
             "l'endpoint ANNONCÉ (`LOGTO_PUBLIC_ENDPOINT`).",
             ("oto_mcp/server.py:226", "oto_mcp/auth/facade.py:50")),
    Variable("MCP_AUDIENCE", Classe.REQUISE, None,
             "Audience RFC 9728 canonique vérifiée sur chaque jeton.",
             ("oto_mcp/server.py:290",)),
    Variable("OTO_MCP_PUBLIC_URL", Classe.REQUISE, None,
             "L'adresse publique de CETTE instance — seule source des liens donnés à "
             "un tiers (redirection OAuth, rappel de paiement, jeton de "
             "téléversement, suffixe d'hôte). Lève à l'appel, pas à l'import.",
             ("oto_mcp/config.py:37", "oto_mcp/server.py:395")),
    Variable("OTO_MCP_S3_ENDPOINT", Classe.REQUISE, None,
             "Endpoint S3/Scaleway du store média.", ("oto_mcp/media_store.py:60",)),
    Variable("OTO_MCP_S3_ACCESS_KEY", Classe.REQUISE, None,
             "Clé d'accès S3 du store média.", ("oto_mcp/media_store.py:62",)),
    Variable("OTO_MCP_S3_SECRET_KEY", Classe.REQUISE, None,
             "Clé secrète S3 du store média.", ("oto_mcp/media_store.py:63",)),
    Variable("OTO_MCP_S3_BUCKET", Classe.REQUISE, None,
             "Bucket S3 du store média.", ("oto_mcp/media_store.py:70",)),
    Variable("OTO_MCP_MASTER_KEY", Classe.REQUISE, None,
             "Clé AES-256 (hex 64 ou base64) du coffre credentials — HORS DB. "
             "Obligatoire DE FAIT : la lecture rend `None` sans lever, mais "
             "`encrypt`/`decrypt` lèvent nommé au premier usage réel.",
             ("oto_mcp/crypto.py:45", "oto_mcp/crypto.py:65", "oto_mcp/crypto.py:74")),
    Variable("OTO_MCP_OAUTH_STATE_SECRET", Classe.REQUISE, None,
             "Clé qui signe l'état OAuth et les jetons courts (upload, invite, "
             "optout). Obligatoire DE FAIT à 4 des 5 sites qui la lisent (lèvent "
             "nommé) ; `journal_secrets.py` est l'exception VOLONTAIRE — sans elle "
             "(dev, tests) il dégrade vers une clé de process aléatoire, le masque "
             "reste correct et cesse seulement d'être corrélable entre deux boots.",
             ("oto_mcp/auth/flow.py:66", "oto_mcp/auth/google.py:87",
              "oto_mcp/outreach_optout.py:57", "oto_mcp/upload_tokens.py:72")),
    Variable("OTO_MCP_OAUTH_RELAY_HOSTS", Classe.REGLAGE, "",
             "Hosts déclarés du relais d'autorisation OAuth (RFC 9207, "
             "`oto_mcp/auth/relay.py`), séparés par des virgules. Absente : aucun "
             "relais actif, comportement d'origine — un opt-in par host, pas un "
             "défaut qui pointerait chez nous.", ("oto_mcp/auth/relay.py:73",)),
    Variable("GOOGLE_WORKSPACE_CLIENT_ID", Classe.REQUISE, None,
             "Client OAuth Google Workspace. Obligatoire DE FAIT : lève nommé au "
             "premier appel de `_client_id()`.", ("oto_mcp/auth/google.py:73",)),
    Variable("GOOGLE_WORKSPACE_CLIENT_SECRET", Classe.REQUISE, None,
             "Secret OAuth Google Workspace. Obligatoire DE FAIT : lève nommé au "
             "premier appel de `_client_secret()`.", ("oto_mcp/auth/google.py:80",)),
    Variable("FOD_BASE_URL", Classe.REQUISE, None,
             "Base URL du service FOD (ADR 0028 — CCN, jurisprudence, lois, "
             "règlements, DVF). Obligatoire DE FAIT : lue nue au niveau module, "
             "`_client()` lève nommé au premier appel si absente.",
             ("oto_mcp/fod/http.py:24", "oto_mcp/fod/http.py:42")),
    Variable("FOD_API_TOKEN", Classe.REQUISE, None,
             "Jeton du service FOD. Même mécanique que `FOD_BASE_URL`.",
             ("oto_mcp/fod/http.py:25", "oto_mcp/fod/http.py:42")),
    # Basculées le 16/09/2026 (Alexis, #968) — la note du 15/09 ci-dessous
    # (« ces variables se DOCUMENTENT, elles ne deviennent pas obligatoires »)
    # ne tient plus pour ces quatre-là : elles pointaient chez NOUS en silence.
    Variable("OTO_MAILER_URL", Classe.REQUISE, None,
             "Relais d'envoi d'email (TEM). Résolue paresseusement, requise dès "
             "qu'un envoi est tenté (bearer posé) — sans elle, le courrier "
             "transiterait par NOTRE infra.", ("oto_mcp/email.py:21",)),
    Variable("OTO_MAIL_FROM", Classe.REQUISE, None,
             "Expéditeur par défaut des emails composés. Même mécanique.",
             ("oto_mcp/email.py:25", "oto_mcp/scheduler.py:99",
              "oto_mcp/tools/email.py:228")),
    Variable("OTO_CONTACT_TO", Classe.REQUISE, None,
             "Boîte de réception par défaut (reply-to) d'un email composé sans "
             "`reply_to` explicite. Sans elle, le repli visait NOTRE boîte "
             "personnelle.", ("oto_mcp/email.py:365",)),
    Variable("OTO_APP_URL", Classe.REQUISE, None,
             "Première variable de la cascade `dashboard_url()` — une seule des "
             "trois suffit ; épuisées toutes les trois, le boot refuse plutôt que "
             "d'offrir silencieusement NOTRE dashboard à sa place.",
             ("oto_mcp/config.py:229", "oto_mcp/auth/flow.py:180")),
    Variable("OTO_DASHBOARD_URL", Classe.REQUISE, None,
             "Deuxième variable de la cascade `dashboard_url()` — voir `OTO_APP_URL`.",
             ("oto_mcp/config.py:229",)),
    Variable("OTO_DASHBOARD_BASE_URL", Classe.REQUISE, None,
             "Troisième variable de la cascade `dashboard_url()` — voir `OTO_APP_URL`.",
             ("oto_mcp/config.py:229",)),
)


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSE (b) — NOTRE_DEFAUT (défaut = une valeur À NOUS, à écraser pour un tiers)
# ═══════════════════════════════════════════════════════════════════════════════
# Aucune garde de boot ici (Alexis, 15/09/2026) : ces variables se DOCUMENTENT, elles
# ne deviennent pas obligatoires. Chaque défaut ci-dessous pointe explicitement chez
# NOUS — à écraser pour toute instance qui ne l'est pas.
#
# Le 16/09/2026 (#968), quatre en sont sorties vers REQUISE (mail/contact/dashboard —
# voir leur note dans `_REQUISES`). Restent ici deux cas délibérément NON basculés :
# `OTO_INVITE_BASE_URL` (hors périmètre, chantier voisin sur ce fichier) et
# `OTO_MCP_CORS_ORIGINS` (son défaut sert AUSSI de confort de développement local —
# les ports `localhost` y sont mêlés à nos domaines de prod — donc « refuser de
# démarrer sans elle » casserait le poste de tout développeur qui ne l'a pas posée,
# pas seulement une instance tierce ; à reprendre séparément si voulu).

_NOTRE_DEFAUT: tuple[Variable, ...] = (
    Variable("OTO_INVITE_BASE_URL", Classe.NOTRE_DEFAUT, "https://oto.cx",
             "NOTRE domaine dans les liens d'invitation. Hors périmètre de ce lot "
             "(#968) — un autre chantier touche ce fichier, ne pas y toucher ici.",
             ("oto_mcp/capabilities/orgs/invites.py:41",)),
    Variable("OTO_MCP_CORS_ORIGINS", Classe.NOTRE_DEFAUT, "",
             "Absente, la liste en dur ne sert QUE nos domaines `oto.*`/`ninja.*` "
             "(+ 2 origines client marquées `noqa: CLIENT`) et une dizaine de ports "
             "`localhost` de confort dev. Un env-liste s'ÉTEND, ne se remplace "
             "jamais (docs/auth-logto.md). Volontairement NON basculée en REQUISE "
             "(16/09/2026) : le défaut mélange un risque « pointe chez nous » et un "
             "confort de dev local légitime — les deux n'appellent pas le même "
             "traitement, à trancher séparément.",
             ("oto_mcp/api/base.py:43",)),
)


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSE (c) — REGLAGE (défaut neutre légitime : timeouts, cadences, tailles,
# rétention, interrupteurs, secrets/URLs optionnels dont l'absence dégrade proprement)
# ═══════════════════════════════════════════════════════════════════════════════

_REGLAGES: tuple[Variable, ...] = (
    # -- déclaratif / neutre --------------------------------------------------
    Variable("OTO_ENV", Classe.REGLAGE, None,
             "`prod`|`preprod`, DÉCLARÉ jamais deviné (ADR 0070). Absente → `None` "
             "(poste de dev, tests) : rien n'engage un tiers. Une valeur hors des "
             "deux lève `EnvironnementAmbigu`.",
             ("oto_mcp/config.py:108",)),
    Variable("OTO_FUNCTIONS_SANDBOX_DIR", Classe.REGLAGE, "",
             "Répertoire du bac à sable des fonctions (ADR 0073), posé par "
             "`scripts/installer_bac_a_sable.py`. Absente : l'instance n'exécute pas de "
             "fonction — `oto_function` op=run/test/publish rend 503 "
             "`sandbox_unavailable`, le reste répond.",
             ("oto_mcp/functions/executor.py:60",)),
    Variable("OTO_EGRESS_ALLOW", Classe.REGLAGE, "",
             "Destinations internes autorisées en egress (`nom=adresse:port`, "
             "virgules). Absente = aucune exception : fail-closed, jamais un défaut "
             "qui ouvre la machine.",
             ("oto_mcp/egress.py:138",)),
    Variable("OTO_ORIGINE_REFUS_LE", Classe.REGLAGE, None,
             "Déplace la date par défaut (`datastore/schema.py:ORIGINE_REFUS_LE`, "
             "2026-10-01) à partir de laquelle poser la couche `origine` sans la "
             "déclarer est refusé. Une valeur illisible LÈVE plutôt que de retomber "
             "sur le défaut.",
             ("oto_mcp/datastore/champs_reserves.py:96",)),
    Variable("OTO_NULL_REFUSE_LE", Classe.REGLAGE, None,
             "Sœur exacte d'`OTO_ORIGINE_REFUS_LE`, même mécanique, pour le refus "
             "d'écrire `null` sans le déclarer.",
             ("oto_mcp/datastore/fin_du_null.py:66",)),
    # -- timeouts / cadences / tailles / rétention -----------------------------
    Variable("OTO_SLOW_CALLBACK_WARN", Classe.REGLAGE, "1.0",
             "Seuil (s) d'avertissement d'un callback lent (event loop).",
             ("oto_mcp/hang_watch.py:200", "oto_mcp/loop_watch.py:35")),
    Variable("OTO_SLOW_CALLBACK_SENTRY", Classe.REGLAGE, "10.0",
             "Seuil (s) au-delà duquel un callback lent part vers Sentry.",
             ("oto_mcp/loop_watch.py:36",)),
    Variable("OTO_JOURNAL_RETENTION_DAYS", Classe.REGLAGE, "90",
             "Rétention (jours) du journal d'appels avant purge en maintenance.",
             ("oto_mcp/maintenance.py:57",)),
    Variable("OTO_MCP_RUN_THREAD_RETENTION_DAYS", Classe.REGLAGE, "30",
             "Rétention (jours) des fils de run avant purge.",
             ("oto_mcp/maintenance.py:63",)),
    Variable("LOG_LEVEL", Classe.REGLAGE, "INFO",
             "Niveau de log du process.",
             ("oto_mcp/server.py:887", "oto_mcp/cli.py:40")),
    Variable("OTO_MCP_CLAIM_DEADLOCK_ATTEMPTS", Classe.REGLAGE, "3",
             "Nombre d'essais de `datastore_claim_next` sur `DeadlockDetected` "
             "(oto-backend#990, Sentry PYTHON-STARLETTE-8Z) — victime d'un cycle "
             "contre une migration de boot, rien n'est jamais posé côté claim, "
             "un rejeu est aussi sûr qu'un premier essai.",
             ("oto_mcp/db/rowlock.py:173",)),
    Variable("OTO_MCP_UNIPILE_DEFAULT_LIMIT", Classe.REGLAGE, "5",
             "Taille de page par défaut des lectures Unipile.",
             ("oto_mcp/unipile_connect.py:49",)),
    Variable("OTO_MCP_UPLOAD_MAX_BYTES", Classe.REGLAGE, "26214400",
             "Plafond dur (octets) d'un contenu poussé par jeton de téléversement — "
             "25 Mo, distinct du plafond image S3.",
             ("oto_mcp/upload_tokens.py:67", "oto_mcp/upload_tokens.py:49")),
    Variable("MIN_TOKEN_IAT", Classe.REGLAGE, "0",
             "Horodatage Unix plancher : un jeton émis avant est refusé (révocation "
             "globale par date).",
             ("oto_mcp/server.py:293",)),
    Variable("MCP_TRANSPORT", Classe.REGLAGE, "streamable_http",
             "Transport MCP (`streamable_http` ou `stdio` en local).",
             ("oto_mcp/server.py:918",)),
    Variable("HOST", Classe.REGLAGE, "127.0.0.1",
             "Adresse d'écoute du serveur.", ("oto_mcp/server.py:945",)),
    Variable("PORT", Classe.REGLAGE, "9103",
             "Port d'écoute du serveur.", ("oto_mcp/server.py:946",)),
    Variable("FR_DIRECTORS_CADENCE_S", Classe.REGLAGE, "0.2",
             "Cadence (s) entre appels dans le scan dirigeants FR.",
             ("oto_mcp/tools/fr.py:432",)),
    Variable("OTO_UNIPILE_FEED_TTL_SECONDS", Classe.REGLAGE, "600",
             "TTL (s) du cache de feed Unipile.", ("oto_mcp/tools/unipile.py:362",)),
    Variable("OTO_ANON_RATE_PER_MIN", Classe.REGLAGE, "120",
             "Débit anonyme (req/min) par endpoint de projet publié.",
             ("oto_mcp/subdomain_project.py:132",)),
    Variable("OTO_ANON_RATE_BURST", Classe.REGLAGE, "60",
             "Rafale anonyme autorisée en plus du débit régulier.",
             ("oto_mcp/subdomain_project.py:139",)),
    Variable("OTO_MCP_MAX_ORGS_PER_USER", Classe.REGLAGE, "10",
             "Plafond d'orgs créées par un même compte.",
             ("oto_mcp/capabilities/orgs/core.py:19",)),
    Variable("OTO_MCP_INVITE_TTL_DAYS", Classe.REGLAGE, "7",
             "Durée de vie (jours) d'une invitation d'org.",
             ("oto_mcp/capabilities/orgs/invites.py:34",)),
    Variable("OTO_MCP_DB_IDLE_TX_TIMEOUT_MS", Classe.REGLAGE, "60000",
             "`idle_in_transaction_session_timeout` du pool applicatif.",
             ("oto_mcp/db/_conn.py:73",)),
    Variable("OTO_MCP_DB_STATEMENT_TIMEOUT_MS", Classe.REGLAGE, "0",
             "`statement_timeout` du pool applicatif — opt-in, off par défaut (un "
             "DDL de migration ne doit pas être coupé au boot).",
             ("oto_mcp/db/_conn.py:74",)),
    Variable("OTO_MCP_DDL_LOCK_TIMEOUT_MS", Classe.REGLAGE, "5000",
             "`lock_timeout` du DDL à chaud (`CREATE INDEX CONCURRENTLY`).",
             ("oto_mcp/db/_conn.py:97", "oto_mcp/db/_conn.py:104")),
    Variable("OTO_MCP_DDL_STATEMENT_TIMEOUT_MS", Classe.REGLAGE, "60000",
             "`statement_timeout` du DDL à chaud.",
             ("oto_mcp/db/_conn.py:98", "oto_mcp/db/_conn.py:105")),
    Variable("OTO_MCP_DB_POOL_MAX", Classe.REGLAGE, "24",
             "Taille max du pool de connexions PG applicatif.",
             ("oto_mcp/db/_conn.py:126",)),
    Variable("OTO_MCP_DB_POOL_TIMEOUT", Classe.REGLAGE, "5",
             "Attente max (s) d'une connexion du pool avant `PoolTimeout`.",
             ("oto_mcp/db/_conn.py:134",)),
    Variable("FOD_DVF_TIMEOUT_S", Classe.REGLAGE, "20",
             "Timeout (s) des requêtes DVF (FOD).", ("oto_mcp/fod/foncier.py:29",)),
    Variable("OTO_MCP_INIT_DB_ATTEMPTS", Classe.REGLAGE, "3",
             "Tentatives de `init_db()` au boot avant abandon.",
             ("oto_mcp/db/_init.py:53",)),
    Variable("OTO_MCP_INIT_DB_LOCK_TIMEOUT_MS", Classe.REGLAGE, "5000",
             "`lock_timeout` du verrou consultatif d'init DB au boot.",
             ("oto_mcp/db/_init.py:162",)),
    Variable("FOD_RETRY_ATTEMPTS", Classe.REGLAGE, "3",
             "Tentatives sur un appel FOD en échec.", ("oto_mcp/fod/http.py:34",)),
    Variable("FOD_RETRY_BACKOFF_S", Classe.REGLAGE, "0.5",
             "Backoff (s) entre tentatives FOD.", ("oto_mcp/fod/http.py:35",)),
    Variable("FOD_RETRY_AFTER_MAX_S", Classe.REGLAGE, "10",
             "Plafond (s) d'un `Retry-After` FOD honoré.",
             ("oto_mcp/fod/http.py:134",)),
    Variable("OTO_MCP_S3_REGION", Classe.REGLAGE, "fr-par",
             "Région S3/Scaleway du store média.", ("oto_mcp/media_store.py:61",)),
    Variable("OTO_MCP_S3_PRESIGN_EXPIRY", Classe.REGLAGE, "3600",
             "Durée de vie (s) d'une URL S3 présignée.",
             ("oto_mcp/media_store.py:139",)),
    Variable("OTO_MCP_S3_MAX_IMAGE_BYTES", Classe.REGLAGE, "2097152",
             "Plafond (octets) d'une image poussée au store média — 2 Mo.",
             ("oto_mcp/media_store.py:46",)),
    Variable("MCP_AUDIENCE_ALT", Classe.REGLAGE, "",
             "Audiences MCP secondaires (coexistence multi-domaine), liste "
             "séparée par des virgules. Vide = no-op.",
             ("oto_mcp/config.py:197",)),
    Variable("OTO_MCP_DCR_ALLOWED_REDIRECTS", Classe.REGLAGE, None,
             "Redirect URIs supplémentaires tolérés par la façade DCR.",
             ("oto_mcp/auth/facade.py:162",)),
    # -- interrupteurs booléens (défaut neutre = comportement historique) -----
    Variable("OTO_HANG_WATCH_ENABLED", Classe.REGLAGE, "1",
             "Surveillance des callbacks lents de l'event loop.",
             ("oto_mcp/hang_watch.py:68", "oto_mcp/hang_watch.py:85")),
    Variable("OTO_SCHEDULER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : ordonnanceur.",
             ("oto_mcp/boucles_de_fond.py:126",)),
    Variable("OTO_EMBED_WORKER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : worker d'embeddings.",
             ("oto_mcp/boucles_de_fond.py:131",)),
    Variable("OTO_FILE_EXTRACT_WORKER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : worker d'extraction de fichiers.",
             ("oto_mcp/boucles_de_fond.py:136",)),
    Variable("OTO_RANK_BACKFILL_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : backfill de classement.",
             ("oto_mcp/boucles_de_fond.py:140",)),
    Variable("OTO_FORMULA_BACKFILL_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : recalcul des colonnes formule (oto-backend#1008 v2).",
             ("oto_mcp/boucles_de_fond.py:146",)),
    Variable("OTO_BILLING_RUNNER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : runner de facturation — AGIT sur un tiers "
             "(prélèvement), donc lu aussi par `est_la_production()`.",
             ("oto_mcp/boucles_de_fond.py:105",)),
    Variable("OTO_BILLING_ENABLED", Classe.REGLAGE, "0",
             "Expose la facturation (REST/MCP/dashboard/runner) — off par défaut.",
             ("oto_mcp/billing.py:78",)),
    Variable("OTO_RUNNER_TICK_ENABLED", Classe.REGLAGE, "1",
             "Tick du runner d'agents.", ("oto_mcp/runner_tick.py:163",)),
    Variable("OTO_L7_SHADOW", Classe.REGLAGE, "1",
             "Écrit l'observation shadow de la fenêtre L7 sans en changer l'issue.",
             ("oto_mcp/access/chain_shadow.py:93",)),
    Variable("OTO_L7_DECIDE", Classe.REGLAGE, "",
             "Fait PASSER la fenêtre L7 de l'observation à la décision — vide = "
             "observe seulement.",
             ("oto_mcp/access/chain_shadow.py:231",)),
    Variable("OTO_ALERTE_CREDENTIAL", Classe.REGLAGE, "",
             "Cible d'alerte quand un credential attendu manque en maintenance.",
             ("oto_mcp/maintenance.py:337", "oto_mcp/maintenance.py:341")),
    # -- secrets / URLs optionnels (absence = dégradation propre) -------------
    Variable("LOGODEV_TOKEN", Classe.REGLAGE, None,
             "Jeton Logo.dev — absent, la résolution de logo dégrade sans lui.",
             ("oto_mcp/logodev.py:17",)),
    Variable("BROWSERBASE_API_KEY", Classe.REGLAGE, None,
             "Clé Browserbase (API privée cookie-bound).",
             ("oto_mcp/browserbase.py:36",)),
    Variable("BROWSERBASE_PROJECT_ID", Classe.REGLAGE, None,
             "Projet Browserbase.", ("oto_mcp/browserbase.py:43",)),
    Variable("MISTRAL_API_KEY", Classe.REGLAGE, None,
             "Clé Mistral — absente, le worker d'embeddings ne tourne pas.",
             ("oto_mcp/embeddings.py:62",)),
    Variable("OTO_MAILER_SEND_BEARER", Classe.REGLAGE, None,
             "Bearer du relais mailer commun (byo-org a le sien en coffre).",
             ("oto_mcp/email.py:48",)),
    Variable("MOLLIE_API_KEY", Classe.REGLAGE, None,
             "Clé Mollie plateforme (ADR 0043).", ("oto_mcp/mollie_client.py:58",)),
    Variable("OTO_MCP_S3_PUBLIC_BASE_URL", Classe.REGLAGE, None,
             "Base publique alternative d'un objet S3 — absente, dérivée de "
             "`OTO_MCP_S3_ENDPOINT` (virtual-hosted Scaleway).",
             ("oto_mcp/media_store.py:87",)),
    Variable("OTO_SENTRY_DSN", Classe.REGLAGE, "",
             "DSN Sentry — vide, Sentry est no-op.", ("oto_mcp/sentry_setup.py:90",)),
    Variable("OTO_SENTRY_ENV", Classe.REGLAGE, "",
             "Environnement annoncé à Sentry. Sert AUSSI de seconde déclaration "
             "croisée avec `OTO_ENV` dans `est_la_production()` — vide, il ne vote "
             "pas.",
             ("oto_mcp/sentry_setup.py:102", "oto_mcp/config.py:151")),
    Variable("OTO_SENTRY_RELEASE", Classe.REGLAGE, None,
             "Release annoncée à Sentry (ex. SHA du deploy).",
             ("oto_mcp/sentry_setup.py:103",)),
    Variable("OTO_SENTRY_TRACES_SAMPLE_RATE", Classe.REGLAGE, "0",
             "Taux d'échantillonnage du tracing de perf Sentry — 0 = off.",
             ("oto_mcp/sentry_setup.py:115",)),
    Variable("OTO_MCP_TENANT_MIGRATION_ISS", Classe.REGLAGE, None,
             "Issuer additionnel toléré pendant une migration de tenant.",
             ("oto_mcp/tenant_migration.py:60", "oto_mcp/tenant_migration.py:71")),
    Variable("OTO_MCP_CLAUDE_APP_ID", Classe.REGLAGE, None,
             "App ID Claude — posé, active la façade DCR (PRM → nous plutôt que "
             "Logto direct).",
             ("oto_mcp/server.py:401",)),
    Variable("OTO_MCP_ADMIN_SUB", Classe.REGLAGE, None,
             "Sub d'un compte admin plateforme, pour des scripts hors requête.",
             ("oto_mcp/access/scope.py:47",)),
    Variable("OTO_MCP_DEV_SUB", Classe.REGLAGE, None,
             "Repli d'identité DEV local, sans jeton — refusé au boot s'il coexiste "
             "avec `LOGTO_ENDPOINT` (`verifier_repli_identite_dev`).",
             ("oto_mcp/auth/hooks.py:164", "oto_mcp/config.py:179")),
    Variable("LOGTO_ENDPOINT_ALT", Classe.REGLAGE, "",
             "Second émetteur Logto toléré (coexistence multi-domaine).",
             ("oto_mcp/server.py:228",)),
    Variable("LOGTO_PUBLIC_ENDPOINT", Classe.REGLAGE, "",
             "Endpoint Logto ANNONCÉ aux clients — distinct de celui qui SIGNE "
             "(`LOGTO_ENDPOINT`). Vide, on annonce celui qui signe.",
             ("oto_mcp/auth/facade.py:68",)),
    Variable("OTO_DEPLOY_REF", Classe.REGLAGE, None,
             "Réf. git du deploy en cours, affichée par `/api/version`.",
             ("oto_mcp/version.py:61", "oto_mcp/version.py:122")),
    Variable("OTO_DEPLOY_SHA", Classe.REGLAGE, None,
             "SHA git du deploy en cours.", ("oto_mcp/version.py:62",)),
    Variable("OTO_DEPLOY_AT", Classe.REGLAGE, None,
             "Horodatage du deploy en cours.", ("oto_mcp/version.py:63",)),
)


NOMS_FIXES: tuple[Variable, ...] = _REQUISES + _NOTRE_DEFAUT + _REGLAGES


FAMILLES_DYNAMIQUES: tuple[FamilleDynamique, ...] = (
    FamilleDynamique(
        nom="quota_provider",
        prefixe="OTO_MCP_QUOTA_", suffixe="_DAILY",
        classe=Classe.REGLAGE,
        description="Quota journalier de la clé plateforme d'UN connecteur "
                     "(`OTO_MCP_QUOTA_<PROVIDER>_DAILY`, provider en MAJUSCULES). "
                     "Normalisé vers le porteur du credential.",
        refs=("oto_mcp/access/quotas.py:119",),
    ),
    FamilleDynamique(
        nom="credential_management_annuaire",
        prefixe="", suffixe="_ID",
        classe=Classe.REQUISE,
        description="Moitié `_ID` du credential de management d'un annuaire "
                     "tenant (`<CREDENTIAL>_ID`) — le primaire est "
                     "`OTO_MCP_LOGTO_M2M_ID`, les autres viennent de "
                     "`tenants.logto_mgmt.credential` en base. Obligatoire DE FAIT "
                     "pour l'annuaire visé : `_mgmt_token()` lève nommé si absent.",
        refs=("oto_mcp/auth/facade.py:253", "oto_mcp/auth/facade.py:327",
              "oto_mcp/auth/facade.py:334"),
    ),
    FamilleDynamique(
        nom="credential_management_annuaire_secret",
        prefixe="", suffixe="_SECRET",
        classe=Classe.REQUISE,
        description="Moitié `_SECRET` du même credential — voir "
                     "`credential_management_annuaire`.",
        refs=("oto_mcp/auth/facade.py:253", "oto_mcp/auth/facade.py:328",
              "oto_mcp/auth/facade.py:335"),
    ),
)


def par_nom() -> dict[str, Variable]:
    return {v.nom: v for v in NOMS_FIXES}


def est_couverte(nom: str) -> bool:
    """`nom` (déjà résolu — un vrai nom de variable, jamais un motif) est-il connu de
    l'inventaire : littéralement, ou par la FORME d'une famille dynamique ? Utile pour
    valider un nom résolu à la main (ex. outillage admin) — le walker AST, lui, ne
    résout jamais un nom dynamique en entier : il reconnaît la CONSTRUCTION elle-même
    via `FamilleDynamique.motif`."""
    if nom in par_nom():
        return True
    return any(nom.startswith(f.prefixe) and nom.endswith(f.suffixe)
               and len(nom) > len(f.prefixe) + len(f.suffixe)
               for f in FAMILLES_DYNAMIQUES)
