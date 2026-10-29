"""Inventaire des variables d'environnement lues par ce backend — SOURCE UNIQUE.

oto-backend#968 (ADR 0070, chantier « découpage cœur/commerce »). Trois surfaces en
dérivent, pour qu'il n'existe jamais deux listes qui divergent :

1. `tests/test_env_inventory_complet.py` — un walker AST qui parcourt `oto_mcp/**/*.py`,
   trouve CHAQUE lecture d'une variable d'environnement, et échoue si l'une d'elles
   n'est pas ici. Il ne mord que dans un sens : une entrée d'ici que le code a cessé de
   lire ne fait rougir personne (même asymétrie que `test_org_store_surface_frozen.py`
   FROZEN — un retrait de lecture n'oblige pas à toucher l'inventaire dans le même
   commit).
   Il parcourt AUSSI oto-core au pin : une variable lue par la dépendance au nom du
   serveur se déclare ici comme les autres, et une lecture d'oto-core qui ne concerne
   pas le serveur se range dans `LUES_PAR_OTO_CORE_HORS_SERVEUR`, avec sa raison.
2. `scripts/generer_env_example.py` — régénère `.env.example` depuis `NOMS_FIXES`.
3. Quiconque doit savoir, avant de démarrer une instance, ce qui est vraiment exigé.

## Les 3 classes

- **REQUISE** (a) — le boot ou le premier appel qui en dépend échoue proprement sans
  elle (soit via `config.require_env`, soit « de fait » : une lecture nue suivie d'un
  `raise` nommé au premier usage réel, pas au moment de la lecture — ce sont ces
  sites-là que `refs` pointe).
- **IDENTITE** (b) — ce que l'instance émet SOUS SON NOM : ses adresses, ses emails, sa
  marque, ses contrats. Ces variables avaient un défaut, et ce défaut était le NÔTRE —
  une instance servie ailleurs démarrait en envoyant ses utilisateurs chez nous.
  **Décision du 28/09/2026 (Alexis, #968) — elle remplace celle du 15/09/2026, qui les
  laissait documentées mais facultatives : refus partout.** Aucune n'a de défaut ;
  toute instance, notre production comprise, les déclare (en développement, dans le
  `.env`), et `identite_instance.verifier` refuse le démarrage s'il en manque une. Pas
  de marqueur « instance tierce » : il serait lui-même un défaut à deviner.
- **REGLAGE** (c) — défaut neutre, légitime en toute instance (timeout, cadence,
  taille, rétention, interrupteur, secret optionnel dont l'absence dégrade proprement).
  Un défaut de réglage ne pointe jamais chez nous :
  `tests/test_env_inventory_complet.py` refuse toute lecture dont le défaut littéral
  porte un de nos domaines.

## Les 2 familles dynamiques

Deux endroits construisent un nom de variable au runtime (`f"{x}_ID"`, jamais un
littéral) : le quota par provider (`access/quotas.py`) et le credential de management
par annuaire tenant (`auth/annuaire.py`, dont `OTO_MCP_LOGTO_M2M_ID`/`_SECRET` est
l'instance du tenant PRIMAIRE — les autres viennent de `tenants.logto_mgmt.credential`
en base). Le walker les reconnaît par la FORME de la construction (préfixe/suffixe
littéral autour d'un segment dynamique), jamais par une énumération de noms.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Classe(str, Enum):
    REQUISE = "requise"    # (a) boot/appel échoue proprement sans elle
    IDENTITE = "identite"  # (b) émis sous le nom de l'instance : déclaré, vérifié au boot
    REGLAGE = "reglage"    # (c) défaut neutre légitime


@dataclass(frozen=True)
class Variable:
    nom: str
    classe: Classe
    # Toujours `None` pour REQUISE et IDENTITE (elles n'ont pas de défaut). Chaîne vide
    # légitime pour (c) quand le code teste `if raw:`.
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
    Variable("LOGTO_ENDPOINT", Classe.REQUISE, None,
             "Émetteur Logto qui SIGNE (vérification JWT ES384), et que la découverte "
             "ANNONCE comme serveur d'autorisation (+ `/oidc`) : une seule adresse, "
             "celle de l'`issuer`. Le Logto doit avoir CIMD activé.",
             ("oto_mcp/server.py:243", "oto_mcp/auth/decouverte.py:35",
              "oto_mcp/auth/annuaire.py:70")),
    Variable("MCP_AUDIENCE", Classe.REQUISE, None,
             "Audience RFC 9728 canonique vérifiée sur chaque jeton.",
             ("oto_mcp/server.py:290",)),
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
    Variable("GOOGLE_ACCOUNT_SERVICES", Classe.REGLAGE, "",
             "Services Google que demande la carte du COMPTE sous notre app, sur cette "
             "instance : sous-ensemble séparé par des virgules de sheets, drive, gmail, "
             "tasks, calendar, chat, ou `none` (l'identité seule). Absente ou vide = les "
             "six. Pour une instance dont le projet Google n'est vérifié que pour "
             "certains scopes ; un service inconnu lève.",
             ("oto_mcp/auth/google.py:140",)),
    Variable("GOOGLE_WORKSPACE_CLIENT_ID", Classe.REQUISE, None,
             "Client OAuth Google Workspace — NOTRE app, servie à tout compte dont le "
             "tenant n'a pas posé la sienne en app d'éditeur (`google_oauth.app_for`). "
             "Obligatoire DE FAIT : lève nommé au premier appel de `_client_id()`.",
             ("oto_mcp/auth/google.py:92",)),
    Variable("GOOGLE_WORKSPACE_CLIENT_SECRET", Classe.REQUISE, None,
             "Secret OAuth Google Workspace (même repli). Obligatoire DE FAIT : lève "
             "nommé au premier appel de `_client_secret()`.",
             ("oto_mcp/auth/google.py:99",)),
    Variable("FOD_BASE_URL", Classe.REQUISE, None,
             "Base URL du service FOD (ADR 0028 — CCN, jurisprudence, lois, "
             "règlements, DVF). Obligatoire DE FAIT : lue nue au niveau module, "
             "`_client()` lève nommé au premier appel si absente.",
             ("oto_mcp/fod/http.py:24", "oto_mcp/fod/http.py:42")),
    Variable("FOD_API_TOKEN", Classe.REQUISE, None,
             "Jeton du service FOD. Même mécanique que `FOD_BASE_URL`.",
             ("oto_mcp/fod/http.py:25", "oto_mcp/fod/http.py:42")),
    Variable("OTO_ENTITLEMENT_DEFAULTS", Classe.REQUISE, None,
             "Les défauts des droits déclarés (ADR 0070 §7, #1066) : la valeur de chaque "
             "droit du catalogue pour qui n'en a aucun posé, en JSON — "
             '`{"unipile": 0, "platform_unmetered": 0, "unipile_seats": 5, '
             '"members_max": "unlimited", "platform_key:*": 0, "platform_key:<c>": n}` '
             "(oui/non = 1/0, nombre >= 0 ou `unlimited`, joker `platform_key:*` et "
             "surcharges par connecteur). Une clé du catalogue ni déclarée ni couverte "
             "refuse le démarrage. `scripts/defauts_des_droits.py` imprime ceux de "
             "l'instance historique, dérivés du registre.",
             ("oto_mcp/access/entitlements.py:42", "oto_mcp/server.py:997")),
)


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSE (b) — IDENTITE (émis sous le nom de l'instance : sans défaut, vérifié au boot)
# ═══════════════════════════════════════════════════════════════════════════════
# Décision du 28/09/2026 : refus partout — voir l'en-tête. Chaque entrée est lue par
# un accesseur qu'appelle `identite_instance.verifier` au démarrage.

_IDENTITE: tuple[Variable, ...] = (
    Variable("OTO_MCP_PUBLIC_URL", Classe.IDENTITE, None,
             "L'adresse publique de CETTE instance — seule source des liens donnés à "
             "un tiers (redirection OAuth, rappel de paiement, jeton de "
             "téléversement, suffixe d'hôte, contact des sondes sortantes).",
             ("oto_mcp/config.py:37", "oto_mcp/server.py:418")),
    Variable("OTO_PROJECT_DOMAIN", Classe.IDENTITE, None,
             "Domaine racine des endpoints de projet publiés (`<slug>.mcp.<D>`, "
             "`<slug>.share.<D>`).",
             ("oto_mcp/config.py:66",)),
    Variable("OTO_APP_URL", Classe.IDENTITE, None,
             "Première variable de la cascade `config.dashboard_url()` (adresse du "
             "tableau de bord servie aux utilisateurs) — une seule des trois suffit.",
             ("oto_mcp/config.py:229", "oto_mcp/auth/flow.py:189")),
    Variable("OTO_DASHBOARD_URL", Classe.IDENTITE, None,
             "Deuxième variable de la cascade `dashboard_url()` — voir `OTO_APP_URL`.",
             ("oto_mcp/config.py:229",)),
    Variable("OTO_DASHBOARD_BASE_URL", Classe.IDENTITE, None,
             "Troisième variable de la cascade `dashboard_url()` — voir `OTO_APP_URL`.",
             ("oto_mcp/config.py:229",)),
    Variable("OTO_DASHBOARD_LINK_PATHS", Classe.REGLAGE, "",
             "Chemins des liens servis vers le tableau de bord de CETTE instance, en JSON "
             "`{type: chemin}` fusionné sur `links.DEFAULT_PATHS` (ex. "
             "`{\"table\": \"/org/{org}/tables/{id}\"}`). Vide = les chemins par défaut. "
             "Une déclaration illisible ou un type de lien inconnu refuse le démarrage "
             "(`links.verifier_reglages`).",
             ("oto_mcp/links.py:95",)),
    Variable("OTO_INVITE_BASE_URL", Classe.IDENTITE, None,
             "Base des liens d'invitation d'une org sans front déclaré "
             "(`orgs.front_base_url` gagne quand il est posé).",
             ("oto_mcp/config.py:246",)),
    Variable("OTO_MCP_CORS_ORIGINS", Classe.IDENTITE, None,
             "Origines navigateur autorisées sur `/api/*`, séparées par des virgules — "
             "les fronts de l'instance, et en développement les ports `localhost` du "
             "poste. Un env-liste s'ÉTEND, ne se remplace jamais (docs/auth-logto.md).",
             ("oto_mcp/config.py:255",)),
    Variable("OTO_MAILER_URL", Classe.IDENTITE, None,
             "Relais mailer des emails de plateforme (endpoint `/api/send`, bearer "
             "`OTO_MAILER_SEND_BEARER`). Un des deux transports de l'instance : laissée "
             "vide quand elle envoie par son API Scaleway TEM (`OTO_TEM_SECRET_KEY`, "
             "`OTO_TEM_PROJECT_ID`, `OTO_TEM_REGION`). Aucun des deux, ou les deux : "
             "refus de démarrer (`email.transport_plateforme`).",
             ("oto_mcp/email.py:49",)),
    Variable("OTO_MAIL_FROM", Classe.IDENTITE, None,
             "Expéditeur par défaut des emails composés.",
             ("oto_mcp/email.py:74", "oto_mcp/scheduler.py:99",
              "oto_mcp/tools/email.py:228")),
    Variable("OTO_CONTACT_TO", Classe.IDENTITE, None,
             "Boîte de réception (reply-to) d'un email composé sans `reply_to` "
             "explicite.", ("oto_mcp/email.py:79",)),
    Variable("OTO_LEGAL_DOCS", Classe.IDENTITE, None,
             "Les documents légaux que l'instance fait accepter, en JSON — un objet par "
             "slug de `legal_docs.CONTEXTS` : "
             '`{"terms": {"version": "1.0", "label": "CGU", "url": "https://…"}, '
             '"cgv": {…}, "dpa": {…}}`. Un slug manquant ou en trop, un champ vide : '
             "refus de démarrer. Bumper `version` redemande l'acceptation.",
             ("oto_mcp/legal_docs.py:66",)),
    Variable("OTO_BRAND_NAME", Classe.IDENTITE, None,
             "Nom du produit du tenant primaire, tel qu'il s'imprime dans les emails "
             "et les pages publiques.", ("oto_mcp/email_brand.py:135",)),
    Variable("OTO_BRAND_SITE", Classe.IDENTITE, None,
             "Site du tenant primaire, hôte nu (`exemple.tld`) : pied des emails, lien "
             "du pied des pages publiques.", ("oto_mcp/email_brand.py:139",)),
    Variable("OTO_TENANT_PRIMAIRE_SLUG", Classe.IDENTITE, None,
             "Slug du tenant PRIMAIRE de l'instance (#969, ADR 0070 §7.2) : la ligne 1 "
             "de `tenants`, semée à la naissance de la base sous ce slug et le nom "
             "`OTO_BRAND_NAME`, dont les subs restent nus. Plus de constante « nous ». "
             "Une base existante dont la ligne 1 porte un autre slug refuse le "
             "démarrage. L'instance historique déclare `oto`.",
             ("oto_mcp/tenancy.py:74", "oto_mcp/identite_instance.py:45",
              "oto_mcp/db/_tenant_primaire.py:45")),
)


# ═══════════════════════════════════════════════════════════════════════════════
# CLASSE (c) — REGLAGE (défaut neutre légitime : timeouts, cadences, tailles,
# rétention, interrupteurs, secrets/URLs optionnels dont l'absence dégrade proprement)
# ═══════════════════════════════════════════════════════════════════════════════

_REGLAGES: tuple[Variable, ...] = (
    # -- déclaratif / neutre --------------------------------------------------
    Variable("OTO_BRAND_LANGUE", Classe.REGLAGE, "",
             "Langue (`fr`|`en`) des emails du tenant primaire pour un destinataire "
             "sans préférence connue (un invité sans compte). Vide = FR ; toute autre "
             "valeur lève `LangueInconnue` à l'envoi.",
             ("oto_mcp/email_brand.py:131",)),
    Variable("OTO_ENV", Classe.REGLAGE, None,
             "`prod`|`preprod`, DÉCLARÉ jamais deviné (ADR 0070). Absente → `None` "
             "(poste de dev, tests) : rien n'engage un tiers. Une valeur hors des "
             "deux lève `EnvironnementAmbigu`.",
             ("oto_mcp/config.py:108",)),
    Variable("OTO_FUNCTIONS_SANDBOX_DIR", Classe.REGLAGE, "",
             "Répertoire du bac à sable des fonctions (ADR 0073), posé par "
             "`scripts/installer_bac_a_sable.py`. Absente : `<arbre>/.bac-a-sable`, que le "
             "déploiement bleu/vert pose dans l'arbre de chaque couleur ; s'il n'existe "
             "pas non plus, l'instance n'exécute pas de fonction — `oto_function` et les "
             "recettes-code rendent 503 `sandbox_unavailable`, le reste répond.",
             ("oto_mcp/functions/executor.py:92",)),
    Variable("OTO_FUNCTIONS_ISOLATION", Classe.REGLAGE, "systemd",
             "Isolement du processus du bac à sable des fonctions (`functions/isolement.py`). "
             "`systemd` (défaut) : unité transitoire durcie sous l'utilisateur dédié "
             "`OTO_FUNCTIONS_USER`, pour un service qui peut lancer une unité sous un autre "
             "uid. `bwrap` : espace de noms utilisateur non privilégié, pour un service "
             "sans privilège. "
             "`direct` : sans isolement système, à DEMANDER (poste de dev, CI) — jamais en "
             "prod. Une autre valeur, ou un outil ou un utilisateur absent, lève : 503 "
             "`sandbox_unavailable`, jamais de repli.",
             ("oto_mcp/functions/isolement.py:99",)),
    Variable("OTO_FUNCTIONS_USER", Classe.REGLAGE, "oto-fonctions",
             "L'utilisateur dédié, non privilégié, sous lequel le mode `systemd` lance le "
             "bac à sable. Absent de la machine, ou privilégié : refus (503 "
             "`sandbox_unavailable`).",
             ("oto_mcp/functions/isolement.py:126",)),
    Variable("OTO_ABONNEMENT_OUVERT_A_TOUS", Classe.REGLAGE, "",
             "Agents sur l'abonnement Claude de la personne : ouverts à TOUT compte "
             "de l'instance (`1`, `true`, `yes`, `on`). Absente, vide ou un non = "
             "seulement les porteurs de l'option `claude_subscription` ; toute autre "
             "valeur lève.",
             ("oto_mcp/capabilities/_abonnement.py:205",)),
    Variable("OTO_REPLI_API_PAR_DEFAUT", Classe.REGLAGE, "",
             "Repli d'un travail d'abonnement Claude épuisé sur la clé API DÉPOSÉE par "
             "son org, pour une org qui ne l'a jamais réglé (`1`, `true`, `yes`, `on`). "
             "Absente, vide ou un non = ces travaux attendent la réinitialisation ; "
             "l'org peut toujours l'ouvrir ou le couper. Toute autre valeur lève.",
             ("oto_mcp/db/org_subscription_pool.py:75",)),
    Variable("OTO_CONNECTEURS_ACTIFS_PAR_DEFAUT", Classe.REGLAGE, "",
             "Ouvre à la plateforme, à chaque boot, tout connecteur du registre qui n'y "
             "a encore AUCUNE ligne (`1`, `true`, `yes`, `on`) — arriéré compris : au "
             "premier boot, tous les connecteurs jamais décidés, pas seulement ceux de la "
             "release. Les connecteurs à clé plateforme (`platform_key_open`) sont exclus "
             "et restent fermés tant qu'une ligne plateforme ne les ouvre pas. Un OFF "
             "explicite reste OFF ; le boot journalise les ouverts et les exclus. Posée "
             "par rôle, elle écrit la BASE : si préprod et prod partagent la base, la "
             "poser sur un rôle ouvre pour les deux. Absente, vide ou un non = "
             "deny-by-default. Toute autre valeur lève.",
             ("oto_mcp/connectors/activation.py:150",)),
    Variable("OTO_EQUIPE_PAR_DEFAUT_TENANTS", Classe.REGLAGE, "",
             "Slugs de tenants (virgules) dont les membres d'équipe ne sont jamais "
             "« sans équipe » : faute d'équipe désignée, l'équipe du sub dans l'org "
             "résolue. Absente = aucun tenant, le niveau org de toujours.",
             ("oto_mcp/access/scope.py:156",)),
    Variable("OTO_PROCEDURE_APP_BUNDLE", Classe.REGLAGE, "",
             "URL du bundle JS (`…/process-graph.js`, sa feuille `.css` à côté) qui "
             "peint le graphe d'une procédure dans l'hôte MCP (`oto_procedure_app`). "
             "Absente = l'app ne s'enregistre pas : une instance sans front qui la "
             "porte ne sert aucune page qui pointerait vers personne.",
             ("oto_mcp/procedure_diagram.py:439",)),
    Variable("OTO_DATA_APP_BUNDLE", Classe.REGLAGE, "",
             "URL du bundle JS d'un front (`…/process-graph.js`, sa feuille `.css` à "
             "côté) qui peint `data_app` dans l'hôte MCP, au lieu de la carte Prefab. "
             "Absente = la carte Prefab, inchangée.",
             ("oto_mcp/tools/datastore_table_app.py:51",)),
    Variable("OTO_EGRESS_ALLOW", Classe.REGLAGE, "",
             "Destinations internes autorisées en egress (`nom=adresse:port`, "
             "virgules). Absente = aucune exception : fail-closed, jamais un défaut "
             "qui ouvre la machine.",
             ("oto_mcp/egress.py:138",)),
    Variable("OTO_EGRESS_IP", Classe.REGLAGE, "",
             "Adresse IP sortante de l'instance, servie par le marqueur `{{egress_ip}}` "
             "des docs connecteur (liste blanche d'IP chez le fournisseur, ex. "
             "Salesforce). Absente = la doc dit « demandez-la au support », jamais "
             "l'adresse d'une autre instance.",
             ("oto_mcp/connectors/docs_reader.py:87",)),
    Variable("OTO_VERROU_ORG_DELEGATION", Classe.REGLAGE, None,
             "Verrou d'org des jetons de délégation (`verrou_org.py`) : `enforce` "
             "(défaut) refuse toute résolution du porteur hors de l'org de son travail, "
             "`report` la journalise et la laisse passer, `off` le coupe. Une valeur "
             "inconnue vaut `enforce`.",
             ("oto_mcp/verrou_org.py:141",)),
    Variable("OTO_VERROU_ROLES_PLATEFORME", Classe.REGLAGE, "report",
             "Verrou des rôles PLATEFORME des jetons de délégation (`verrou_org.py`) : "
             "`enforce` lit `member` le porteur `admin`/`super_admin` d'un travail du "
             "runner (aucune garde plateforme, aucun `oto_admin_*` — sauf, pendant leur "
             "appel, ceux que son agent nomme : `platform_tools`), `report` (défaut) "
             "journalise ce qui serait retiré et ne retire rien, `off` le coupe. Une "
             "valeur inconnue vaut `report`. Indépendant d'`OTO_VERROU_ORG_DELEGATION`.",
             ("oto_mcp/verrou_org.py:226",)),
    Variable("OTO_COPILOT_SUBS", Classe.REGLAGE, "",
             "L'agent intégré à l'application (`capabilities/copilot`, docs/copilot.md) : "
             "les `sub` autorisés, séparés par des virgules. Vide (défaut) = personne — les "
             "quatre routes `/api/copilot/*` répondent 403 `copilot_not_enabled`. `*` = "
             "tout compte (session console, membre de l'org, jamais en « voir en tant "
             "que »). Lu à chaque requête.",
             ("oto_mcp/capabilities/copilot/routes.py:61",)),
    Variable("OTO_COPILOT_DAILY_CAP_USD", Classe.REGLAGE, "20",
             "L'agent intégré à l'application : plafond de dépense au modèle PAR COMPTE ET "
             "PAR JOUR UTC, en dollars (prix publics du fournisseur × jetons rapportés, "
             "`capabilities/copilot/depense.py`), quelle que soit la clé et quel que soit "
             "`OTO_CREDITS_MODE`. Atteint : 429 `copilot_daily_cap` avant le flux, refus "
             "nommé dans le flux avant l'appel suivant. Lu à chaque appel ; une valeur "
             "qui n'est pas un montant positif lève (jamais le défaut en silence).",
             ("oto_mcp/capabilities/copilot/depense.py:59",)),
    Variable("OTO_COPILOT_VOICE_ID", Classe.REGLAGE, "EXAVITQu4vr4xnSDxMaL",
             "L'agent intégré à l'application : la voix ElevenLabs qui lit ses réponses "
             "quand le panneau demande `speak` (`capabilities/copilot/voix.py`, modèle "
             "`eleven_flash_v2_5`, par la clé ElevenLabs de l'org ou la clé plateforme "
             "octroyée). Absente ou vide : la voix prédéfinie « Sarah ». Lu à chaque tour.",
             ("oto_mcp/capabilities/copilot/voix.py:47",)),
    Variable("OTO_CREDITS_MODE", Classe.REGLAGE, "report",
             "Budget d'unités au moment de l'appel (`access/credits.py`) : `report` "
             "(défaut) réserve et règle sans jamais refuser, et journalise ce "
             "qu'`enforce` refuserait ; `enforce` refuse (`insufficient_credits`, "
             "`credits_unpriced`, `spend_ledger_unavailable`) ; `off` ne réserve rien. "
             "Illisible : LÈVE au boot.",
             ("oto_mcp/access/credits.py:94",)),
    Variable("OTO_CREDITS_MODE_FUNCTIONS", Classe.REGLAGE, None,
             "Budget d'unités : le mode PAR FONCTION du barème, qui l'emporte sur "
             "`OTO_CREDITS_MODE` pour elle (`access/credits_modes.mode_de`) — liste "
             "`<connecteur>.<fonction>=<mode>` séparée par des virgules, `*` final = "
             "toute fonction qui commence ainsi ; un nom exact l'emporte toujours sur "
             "un motif, et entre motifs le préfixe le plus long. Ex. "
             "`anthropic.messages:*=enforce` : les appels au modèle (agents hébergés, "
             "agent intégré) refusent, le reste reste en `report` (#1376, E). Absente "
             "ou vide : aucune. Illisible, nommée deux fois, ou qui ne nomme aucune "
             "fonction tarifable (coût déclaré d'un connecteur, modèle du catalogue) : "
             "LÈVE au boot.",
             ("oto_mcp/access/credits_modes.py:39",)),
    Variable("OTO_CREDITS_JOB_FLOOR", Classe.REGLAGE, None,
             "Budget d'unités : le solde disponible (solde − réservations ouvertes), "
             "en unités, qu'un travail d'agent hébergé sur une clé REFACTURÉE doit "
             "avoir pour démarrer — il couvre l'entrée du premier tour, jamais "
             "réservée (`access/credits_tours.sous_le_plancher`, #1376, B). Mord dans "
             "le mode de la fonction du modèle : `enforce` arrête le travail, raison "
             "écrite (`insufficient_credits`), et rend à la file un travail dont le "
             "solde est illisible (`spend_ledger_unavailable`, rejouable) ; `report` "
             "journalise. Absente ou 0 : aucun plancher. Illisible ou négative : LÈVE "
             "au boot.",
             ("oto_mcp/access/credits_modes.py:119",)),
    Variable("OTO_CREDITS_HOLD_MAX_S", Classe.REGLAGE, None,
             "Durée maximale d'une réservation sans échéance connue (travail "
             "asynchrone sans `within_s`, tranche de fond `jev_rows`, appel synchrone "
             "quand OTO_BORNE_OUTIL_S=0 et sans `timeout_s`), en secondes (défaut 86400 "
             "dans le code, 1 au moins). Illisible : LÈVE au boot.",
             ("oto_mcp/access/credits.py:107",)),
    Variable("OTO_CREDITS_SYNC_MARGIN_S", Classe.REGLAGE, None,
             "Marge de l'échéance d'un appel synchrone : max(OTO_BORNE_OUTIL_S, "
             "`timeout_s` déclaré) + cette marge, en secondes (défaut 60 dans le code, 1 "
             "au moins). Illisible : LÈVE au boot.",
             ("oto_mcp/access/credits.py:113",)),
    Variable("OTO_PLATFORM_KEY_ORG_RATE", Classe.REGLAGE, "report",
             "Équité d'une clé PLATEFORME partagée entre orgs (F-051, "
             "`access/platform_rate.py`) : `enforce` refuse l'appel d'une org au-delà "
             "de sa part de la fenêtre, `report` (défaut) journalise une fois par "
             "fenêtre l'org qui la dépasse et ne refuse rien, `off` ne compte rien. "
             "Une valeur inconnue LÈVE au boot.",
             ("oto_mcp/access/platform_rate.py:64",)),
    Variable("OTO_PLATFORM_KEY_ORG_RATE_LIMIT", Classe.REGLAGE, None,
             "Appels d'UNE org sur UNE clé plateforme partagée par fenêtre (défaut 120 "
             "dans le code ; 0 = sans limite). Illisible : LÈVE au boot.",
             ("oto_mcp/access/platform_rate.py:111",)),
    Variable("OTO_PLATFORM_KEY_ORG_RATE_WINDOW_S", Classe.REGLAGE, None,
             "Durée en secondes de la fenêtre fixe de l'équité des clés plateforme "
             "(défaut 60 dans le code, 1 au moins). Illisible : LÈVE au boot.",
             ("oto_mcp/access/platform_rate.py:87",)),
    Variable("OTO_PLATFORM_KEY_ORG_RATE_OVERRIDES", Classe.REGLAGE, "",
             "Limites par connecteur porteur de la clé, `serper=600,aiark=30` ; "
             "`0` = sans limite pour ce connecteur. Vide = la limite commune partout. "
             "Une paire illisible LÈVE au boot.",
             ("oto_mcp/access/platform_rate.py:97",)),
    Variable("OTO_JOURNAL_REVISIONS", Classe.REGLAGE, "on",
             "Interrupteur du journal des révisions de ligne du datastore (oto#273) : "
             "`off` le coupe pour les écritures de CE processus, sans redéployer (un "
             "redémarrage suffit). Toute autre valeur que `on`/`off` LÈVE au boot.",
             ("oto_mcp/db/journal_revisions.py:70",)),
    # -- timeouts / cadences / tailles / rétention -----------------------------
    Variable("OTO_SLOW_CALLBACK_WARN", Classe.REGLAGE, "1.0",
             "Seuil (s) d'avertissement d'un callback lent (event loop).",
             ("oto_mcp/hang_watch.py:200", "oto_mcp/loop_watch.py:35")),
    Variable("OTO_SLOW_CALLBACK_SENTRY", Classe.REGLAGE, "10.0",
             "Seuil (s) au-delà duquel un callback lent part vers Sentry.",
             ("oto_mcp/loop_watch.py:36",)),
    Variable("OTO_EMAIL_PLATFORM_DAILY_RECIPIENTS", Classe.REGLAGE, "200",
             "Plafond quotidien de destinataires (`to` + `cc`) d'`email_send` par org sur "
             "le transport commun (le relais de l'instance) ; une org qui envoie avec sa "
             "clé n'en a aucun. Une valeur illisible lève au premier envoi.",
             ("oto_mcp/tools/email.py:78",)),
    Variable("OTO_JOURNAL_RETENTION_DAYS", Classe.REGLAGE, "30",
             "Rétention EN LIGNE (jours) du journal d'appels : au-delà, chaque mois clos "
             "est archivé sur S3 puis supprimé (`deploy/archive_tool_calls.py`).",
             ("oto_mcp/maintenance.py:98",)),
    Variable("OTO_JOURNAL_REVISIONS_RETENTION_DAYS", Classe.REGLAGE, "90",
             "Rétention (jours) du journal des révisions de ligne du datastore "
             "(oto#273) : `oto-mcp maintenance revisions` purge au-delà, sauf les "
             "révisions `import` d'une ligne qui existe encore. Lue à chaque tir ; "
             "illisible (pas un entier ≥ 1), le travail LÈVE.",
             ("oto_mcp/db/journal_revisions.py:116",)),
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
             ("oto_mcp/db/rowlock.py:215",)),
    Variable("OTO_MCP_CLAIM_DEFAULT_MAX_CLAIMS", Classe.REGLAGE, "3",
             "Plafond de reprises d'un tableau qui ne déclare pas "
             "`lifecycle.max_claims` (oto#101) : une ligne réservée autant de fois "
             "sans écriture est mise de côté (`abandon_reason`, statut inchangé). "
             "Illisible (pas un entier ≥ 1), la réservation LÈVE.",
             ("oto_mcp/db/rowabandon.py:67",)),
    Variable("OTO_MCP_UNIPILE_DEFAULT_LIMIT", Classe.REGLAGE, "5",
             "Plafond par défaut de comptes hébergés (clé plateforme) par org, quand "
             "l'org ne pose pas le sien (`orgs.unipile_account_limit`) : garde contre "
             "l'emballement des sièges que la plateforme paie. `0` = sans plafond.",
             ("oto_mcp/unipile_connect.py:49",)),
    Variable("OTO_MCP_UPLOAD_MAX_BYTES", Classe.REGLAGE, "26214400",
             "Plafond dur (octets) d'un contenu poussé par jeton de téléversement — "
             "25 Mo, distinct du plafond image S3.",
             ("oto_mcp/upload_tokens.py:67", "oto_mcp/upload_tokens.py:49")),
    Variable("OTO_PENNYLANE_FIRM_UPLOAD_MAX_BYTES", Classe.REGLAGE, "104857600",
             "Plafond (octets) d'un fichier relayé vers la GED Pennylane (cabinet) — "
             "100 Mo, PROVISOIRE : à relever après mesure de la limite de Pennylane. "
             "Distinct de OTO_MCP_UPLOAD_MAX_BYTES.",
             ("oto_mcp/tools/pennylane_firm_relais.py:50",)),
    Variable("OTO_PENNYLANE_FIRM_RELAY_CONCURRENCY", Classe.REGLAGE, "4",
             "Relais d'upload Pennylane (cabinet) simultanés par processus ; au-delà, "
             "503 `relay_busy` réessayable.",
             ("oto_mcp/tools/pennylane_firm_relais.py:55",)),
    Variable("OTO_HIGGSFIELD_MAX_USD_PAR_APPEL", Classe.REGLAGE, "5",
             "Connecteur Higgsfield : plafond, en dollars, du `max_usd` qu'une génération "
             "peut déclarer (`higgsfield_act` op=create_generation). Au-dessus : refus "
             "nommé `max_usd_above_cap`, avant tout appel à Higgsfield. Lu à chaque "
             "appel ; une valeur qui n'est pas un montant positif lève (jamais le défaut "
             "en silence).",
             ("oto_mcp/tools/higgsfield.py:82",)),
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
    Variable("OTO_ANON_RATE_PER_MIN", Classe.REGLAGE, "120",
             "Débit anonyme (req/min) par endpoint de projet publié.",
             ("oto_mcp/subdomain_project.py:132",)),
    Variable("OTO_ANON_RATE_BURST", Classe.REGLAGE, "60",
             "Rafale anonyme autorisée en plus du débit régulier.",
             ("oto_mcp/subdomain_project.py:139",)),
    Variable("OTO_DEBIT_MODE", Classe.REGLAGE, "report",
             "Limite de débit applicative (`limite_debit.py`, F-025) : `report` (défaut) "
             "mesure et journalise ce qui serait refusé sans rien refuser, `enforce` "
             "refuse en 429, `off` la coupe ; une valeur inconnue empêche le démarrage. "
             "`enforce` SEULEMENT derrière un proxy qui écrase `CF-Connecting-IP` et "
             "`X-Forwarded-For` (sinon les classes par adresse se contournent). Le MÊME "
             "interrupteur gouverne les bornes globales du processus (`bornes_processus`, "
             "#1232 : corps, délai d'outil, `statement_timeout`, inactivité des sessions "
             "MCP) : `report` les mesure, `enforce` les applique. Lue au démarrage : en "
             "changer demande un redémarrage.",
             ("oto_mcp/limite_debit.py:179", "oto_mcp/bornes_processus.py:108")),
    Variable("OTO_BORNE_CORPS_OCTETS", Classe.REGLAGE, "4194304",
             "Borne du processus (#1232) : taille max (octets) du corps d'une requête "
             "HTTP hors routes d'upload. Mesurée en `report`, `413` en `enforce` "
             "(`OTO_DEBIT_MODE`) ; `0` désarme ; une valeur illisible empêche le démarrage.",
             ("oto_mcp/bornes_processus.py:110",)),
    Variable("OTO_BORNE_CORPS_UPLOAD_OCTETS", Classe.REGLAGE, "115343360",
             "Borne du processus (#1232) : taille max (octets) du corps sur une route "
             "d'upload (reconnue à son contrat : corps déclaré non JSON). Mesurée en "
             "`report`, `413` en `enforce` ; `0` désarme ; illisible : refus de démarrer.",
             ("oto_mcp/bornes_processus.py:113",)),
    Variable("OTO_BORNE_OUTIL_S", Classe.REGLAGE, "120",
             "Borne du processus (#1232) : durée max (s) d'un appel d'outil MCP, la même "
             "pour tous. Mesurée en `report`, erreur MCP `tool_timeout` en `enforce` ; "
             "`0` désarme ; illisible : refus de démarrer.",
             ("oto_mcp/bornes_processus.py:114",)),
    Variable("OTO_BORNE_SESSION_INACTIVE_S", Classe.REGLAGE, "1800",
             "Borne du processus (#1232) : inactivité max (s) d'une session MCP. Mesurée "
             "en `report` (session reprise après ce délai), fermée par l'SDK `mcp` en "
             "`enforce` ; `0` désarme ; illisible : refus de démarrer.",
             ("oto_mcp/bornes_processus.py:119",)),
    Variable("OTO_DEBIT_ADRESSES_EXEMPTEES", Classe.REGLAGE, "",
             "Adresses ou CIDR (séparés par des virgules) de NOS machines, exemptés des "
             "classes de débit par adresse et du budget d'échecs ; leurs jetons paient "
             "toujours la classe par jeton. Vide par défaut ; une entrée illisible "
             "empêche le démarrage. Lue au démarrage.",
             ("oto_mcp/limite_debit.py:190",)),
    Variable("OTO_PROXIES_DE_CONFIANCE", Classe.REGLAGE, "",
             "Adresses ou CIDR (séparés par des virgules) des proxies dont on croit les "
             "en-têtes d'adresse (`CF-Connecting-IP`, `X-Forwarded-For`) : lus seulement "
             "si le pair TCP en fait partie. Vide par défaut : on lit le pair TCP — derrière "
             "un proxy, TOUT le trafic aurait alors son adresse. Une entrée illisible "
             "empêche le démarrage. Lue au démarrage.",
             ("oto_mcp/client_trace.py:56",)),
    Variable("OTO_DEBIT_JETON_PAR_MIN", Classe.REGLAGE, "1200",
             "Débit (req/min) par jeton porté (`Authorization`), rafale = la moitié ; "
             "`0` désarme la classe.",
             ("oto_mcp/limite_debit.py:221",)),
    Variable("OTO_DEBIT_ANONYME_PAR_MIN", Classe.REGLAGE, "600",
             "Débit (req/min) par adresse d'une requête sans jeton, hors routes "
             "sensibles et webhooks (DCR compris) ; rafale = la moitié ; `0` désarme.",
             ("oto_mcp/limite_debit.py:223",)),
    Variable("OTO_DEBIT_SENSIBLE_PAR_MIN", Classe.REGLAGE, "60",
             "Débit (req/min) par adresse sur les routes à secret dans le chemin et les "
             "gestes d'authentification interactifs (invitation, partage public, "
             "autorisation, retours OAuth) ; rafale = la moitié ; `0` désarme.",
             ("oto_mcp/limite_debit.py:225",)),
    Variable("OTO_DEBIT_WEBHOOK_PAR_MIN", Classe.REGLAGE, "300",
             "Débit (req/min) par (adresse, chemin) des webhooks entrants, plus un "
             "plafond par adresse à dix fois ce débit ; rafale = la moitié ; `0` désarme.",
             ("oto_mcp/limite_debit.py:227",)),
    Variable("OTO_DEBIT_ECHECS_PAR_MIN", Classe.REGLAGE, "120",
             "Budget (par min) de réponses 401/403 (et 404 sur routes à secret) par "
             "adresse ; épuisé, l'adresse voit refuser ses jetons inconnus et ses routes "
             "à secret — jamais un jeton déjà accepté, ni l'anonyme, ni un webhook ; "
             "`0` désarme ce budget.",
             ("oto_mcp/limite_debit.py:228",)),
    Variable("OTO_MCP_MAX_ORGS_PER_USER", Classe.REGLAGE, "10",
             "Plafond d'orgs créées par un même compte (hors super_admin et admin de son tenant).",
             ("oto_mcp/capabilities/orgs/core.py:19",)),
    Variable("OTO_MCP_INVITE_TTL_DAYS", Classe.REGLAGE, "7",
             "Durée de vie (jours) d'une invitation d'org.",
             ("oto_mcp/capabilities/orgs/invites.py:34",)),
    Variable("OTO_MCP_DB_IDLE_TX_TIMEOUT_MS", Classe.REGLAGE, "60000",
             "`idle_in_transaction_session_timeout` du pool applicatif.",
             ("oto_mcp/db/_conn.py:73",)),
    Variable("OTO_MCP_DB_STATEMENT_TIMEOUT_MS", Classe.REGLAGE, "30000",
             "Borne du processus (#1232) : `statement_timeout` (ms) du pool applicatif. "
             "En `report` (`OTO_DEBIT_MODE`), rien n'est coupé : les ordres qui la "
             "dépassent sont comptés ; en `enforce`, posée sur chaque connexion du pool "
             "(la préparation de la base au démarrage, le DDL à chaud et le travail de "
             "fond hors pool en sont exemptés). `0` désarme ; illisible : refus de démarrer.",
             ("oto_mcp/bornes_processus.py:116",)),
    Variable("OTO_MCP_DDL_LOCK_TIMEOUT_MS", Classe.REGLAGE, "5000",
             "`lock_timeout` du DDL à chaud (`CREATE INDEX CONCURRENTLY`).",
             ("oto_mcp/db/_conn.py:97", "oto_mcp/db/_conn.py:104")),
    Variable("OTO_MCP_DDL_STATEMENT_TIMEOUT_MS", Classe.REGLAGE, "60000",
             "`statement_timeout` du DDL à chaud.",
             ("oto_mcp/db/_conn.py:98", "oto_mcp/db/_conn.py:105")),
    Variable("OTO_MIGRATION_INDEX_MAX_LIGNES", Classe.REGLAGE, "",
             "Plafond de lignes jusqu'où une RÉVISION construit elle-même un index "
             "CONCURRENTLY — posé par l'appelant de `migrer` (la porte d'une instance "
             "cible, #1195) ; absent = le plafond de l'index (100 000). Le démarrage ne "
             "le lit pas.",
             ("oto_mcp/db/index_concurrent.py:156",)),
    Variable("OTO_MCP_DB_POOL_MAX", Classe.REGLAGE, "24",
             "Taille max du pool de connexions PG applicatif.",
             ("oto_mcp/db/_conn.py:126",)),
    Variable("OTO_MCP_DB_POOL_TIMEOUT", Classe.REGLAGE, "5",
             "Attente max (s) d'une connexion du pool avant `PoolTimeout`.",
             ("oto_mcp/db/_conn.py:134",)),
    Variable("OTO_MCP_DB_POOL_MAX_IDLE", Classe.REGLAGE, "60",
             "Durée (s) après laquelle une connexion inactive au-delà de `min_size` est "
             "rendue à la base (#1141).",
             ("oto_mcp/db/_conn.py:160",)),
    Variable("OTO_MCP_DB_HORS_POOL_MAX", Classe.REGLAGE, "2",
             "Plafond des connexions HORS pool (DDL à chaud, verrou de session) par "
             "processus (#1141).",
             ("oto_mcp/db/_conn.py:316",)),
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
             "Hôtes https de rappel supplémentaires tolérés par le shim OAuth "
             "anonyme des projets publiés, séparés par des virgules.",
             ("oto_mcp/auth/anon.py:59",)),
    # -- interrupteurs booléens (défaut neutre = comportement historique) -----
    Variable("OTO_HANG_WATCH_ENABLED", Classe.REGLAGE, "1",
             "Surveillance des callbacks lents de l'event loop.",
             ("oto_mcp/hang_watch.py:68", "oto_mcp/hang_watch.py:85")),
    Variable("OTO_SCHEDULER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : ordonnanceur.",
             ("oto_mcp/boucles_de_fond.py:147",)),
    Variable("OTO_EMBED_WORKER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : worker d'embeddings.",
             ("oto_mcp/boucles_de_fond.py:152",)),
    Variable("OTO_FILE_EXTRACT_WORKER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : worker d'extraction de fichiers.",
             ("oto_mcp/boucles_de_fond.py:157",)),
    Variable("OTO_RANK_BACKFILL_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : backfill de classement.",
             ("oto_mcp/boucles_de_fond.py:161",)),
    Variable("OTO_FORMULA_BACKFILL_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : recalcul des colonnes formule (oto-backend#1008 v2).",
             ("oto_mcp/boucles_de_fond.py:166",)),
    Variable("OTO_TRANSCRIPTION_WORKER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : worker de transcription (ADR 0074, #674) — prod "
             "seule, donc lu aussi par `est_la_production()`.",
             ("oto_mcp/boucles_de_fond.py:174",)),
    Variable("OTO_JEV_JOBS_WORKER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : `jev_rows(background=true)` — dépense sur la clé Jev "
             "partagée, prod seule.",
             ("oto_mcp/boucles_de_fond.py:180",)),
    Variable("OTO_CREDITS_ECHEANCES_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : règle au montant réservé les réservations du budget "
             "d'unités passées leur échéance (`credits_echeances.py`). N'écrit qu'en "
             "base, tous environnements.",
             ("oto_mcp/boucles_de_fond.py:185",)),
    Variable("OTO_ALERTE_AGENTS_ENABLED", Classe.REGLAGE, "0",
             "Boucle de fond : prévient l'admin d'une org quand un de ses agents "
             "programmés est bloqué (`alerte_agents_bloques.py`) — AGIT sur un tiers "
             "(courriel), donc production seulement. ÉTEINTE par défaut : `1` "
             "l'allume, chaque instance le décide.",
             ("oto_mcp/boucles_de_fond.py:212",)),
    Variable("OTO_MEDECIN_AGENTS_ENABLED", Classe.REGLAGE, "0",
             "Boucle de fond : soigne un agent hébergé en panne quand la panne est la "
             "nôtre — relance un travail mort (`medecin_agents.py`) ; AGIT sur un tiers "
             "(l'agent relancé), donc production seulement. ÉTEINTE par défaut : `1` "
             "l'allume, chaque instance le décide.",
             ("oto_mcp/boucles_de_fond.py:222",)),
    Variable("OTO_BILLING_RUNNER_ENABLED", Classe.REGLAGE, "1",
             "Boucle de fond : runner de facturation — AGIT sur un tiers "
             "(prélèvement), donc lu aussi par `est_la_production()`.",
             ("oto_mcp/boucles_de_fond.py:126",)),
    Variable("OTO_BILLING_ENABLED", Classe.REGLAGE, "0",
             "Expose la facturation (REST/MCP/dashboard/runner) — off par défaut.",
             ("oto_mcp/billing.py:78",)),
    Variable("OTO_FERME_URL", Classe.REQUISE, None,
             "L'agent de la ferme des sandboxes d'abonnement (réseau privé du parc, "
             "`http://172.16.16.2:8190`). Sans lui, se connecter ou effacer son sandbox "
             "échoue en 502 nommé ; rien d'autre n'en dépend.",
             ("oto_mcp/ferme.py:45",)),
    Variable("OTO_FERME_TOKEN", Classe.REQUISE, None,
             "Le jeton de l'agent de la ferme (1Password « Ferme de Claude — jeton de "
             "l agent »). Même sort que OTO_FERME_URL.",
             ("oto_mcp/ferme.py:45",)),
    Variable("OTO_RUNNER_TICK_ENABLED", Classe.REGLAGE, "1",
             "Tick du runner d'agents.", ("oto_mcp/runner_tick.py:163",)),
    Variable("OTO_L7_SHADOW", Classe.REGLAGE, "1",
             "Écrit l'observation shadow de la fenêtre L7 sans en changer l'issue.",
             ("oto_mcp/access/chain_shadow.py:89",)),
    Variable("OTO_L7_DECIDE", Classe.REGLAGE, "",
             "Fait PASSER la fenêtre L7 de l'observation à la décision — vide = "
             "observe seulement.",
             ("oto_mcp/access/chain_shadow.py:209",)),
    Variable("OTO_ALERTE_CREDENTIAL", Classe.REGLAGE, "",
             "Ouvre l'envoi du travail `alertes-credential` : un courriel par org à "
             "ses admins quand une clé est retirée, refusée ou à sec sous des agents "
             "programmés actifs. Absente : le travail compte ce qui partirait, "
             "n'envoie rien.",
             ("oto_mcp/maintenance.py:374", "oto_mcp/maintenance.py:378")),
    Variable("OTO_UNIPILE_FIN_DE_DROIT", Classe.REGLAGE, "",
             "Ouvre le travail `unipile-fin-de-droit` (#806) : préavis puis suppression "
             "chez unipile des comptes sur la clé plateforme d'une org sans droit "
             "`unipile`. Absente : le travail compte ce qu'il ferait, n'écrit rien. "
             "À poser seulement une fois `org_entitlements` rempli par oto-commerce, "
             "qui pose seul les droits depuis la coupure du cœur (#1097) : une "
             "ligne qu'il n'a pas posée ferait supprimer le compte d'un client qui "
             "paie.", ("oto_mcp/unipile_fin_de_droit.py:64",)),
    Variable("OTO_ACTIVATION", Classe.REGLAGE, "",
             "L'email d'activation par tenant, en JSON : `{slug: {reply_to, cc, "
             "app_url, mcp_url, help_url?, exclude_domains, delay_hours, window_days, "
             "sequence_days, max_per_run, mailer_url?, link_base?, steps?, "
             "agent_label?}}` ; l'expéditeur est celui de la marque du tenant "
             "(`tenants.brand.expediteur`), un `sender` ici lève, et le slug du tenant "
             "primaire est refusé. Absente : le "
             "travail `activation` ne lit rien. Le détail est dans `activation.py`.",
             ("oto_mcp/activation.py:99",)),
    Variable("OTO_ACTIVATION_ENVOI", Classe.REGLAGE, "",
             "Ouvre l'envoi réel du travail `activation`. Absente : le passage "
             "dit qui recevrait l'email, n'envoie ni n'écrit rien. Même ouverte, rien ne "
             "part sans un essai reçu pour le contenu servi.",
             ("oto_mcp/activation.py:64",)),
    Variable("OTO_ACTIVATION_MAILER_BEARER", Classe.REGLAGE, "",
             "Jeton du relais d'envoi qu'un tenant déclare (`mailer_url` dans "
             "`OTO_ACTIVATION`). Absent alors qu'un relais est déclaré : l'envoi est "
             "refusé, jamais reporté sur le relais de l'instance.",
             ("oto_mcp/activation.py:236",)),
    Variable("OTO_DIGEST_LECTEURS", Classe.REGLAGE, "",
             "Ouvre le travail `digest-lecteurs` : un mail par jour au propriétaire "
             "d'une procédure partagée par lien qui a de nouveaux lecteurs visibles. "
             "Absente (ou autre que 1/true/on) : le travail compte ce qu'il enverrait, "
             "n'envoie ni ne marque rien.", ("oto_mcp/digest_lecteurs.py:34",)),
    Variable("OTO_UNIPILE_FIN_DE_DROIT_DELAI_JOURS", Classe.REGLAGE, "7",
             "Jours entre le premier constat de la perte du droit `unipile` et la "
             "suppression du compte chez unipile. Entier ≥ 1, sinon le travail lève.",
             ("oto_mcp/unipile_fin_de_droit.py:70",)),
    # -- secrets / URLs optionnels (absence = dégradation propre) -------------
    Variable("BLS_API_KEY", Classe.REGLAGE, None,
             "Clé d'enregistrement BLS de l'exploitant — lève le quota de l'API publique "
             "(500 requêtes/jour au lieu de 25, partagées par toute la plateforme). "
             "Absente, le connecteur open data `bls` sert le régime sans clé.",
             ("oto_mcp/tools/bls.py:69",)),
    Variable("LOGODEV_TOKEN", Classe.REGLAGE, None,
             "Jeton Logo.dev — absent, la résolution de logo dégrade sans lui.",
             ("oto_mcp/logodev.py:17",)),
    Variable("BROWSERBASE_API_KEY", Classe.REGLAGE, None,
             "Clé Browserbase (API privée cookie-bound).",
             ("oto_mcp/browserbase.py:36",)),
    Variable("BROWSERBASE_PROJECT_ID", Classe.REGLAGE, None,
             "Projet Browserbase.", ("oto_mcp/browserbase.py:43",)),
    Variable("OTO_SCRUB_MODEL_URL", Classe.REGLAGE, None,
             "Racine (sans `/chat/completions`) du point d'accès compatible OpenAI du modèle "
             "qui désigne noms, adresses et identifiants dans le texte (action `scrub`). "
             "Va avec `OTO_SCRUB_MODEL` et `OTO_SCRUB_MODEL_KEY`, les trois ou aucune : "
             "absentes, une règle qui demande le modèle est refusée à la pose.",
             ("oto_mcp/scrub_model.py:23",)),
    Variable("OTO_SCRUB_MODEL", Classe.REGLAGE, None,
             "Nom du modèle servi par `OTO_SCRUB_MODEL_URL` (action `scrub`).",
             ("oto_mcp/scrub_model.py:24",)),
    Variable("OTO_SCRUB_MODEL_KEY", Classe.REGLAGE, None,
             "Clé du point d'accès `OTO_SCRUB_MODEL_URL` (action `scrub`).",
             ("oto_mcp/scrub_model.py:25",)),
    Variable("MISTRAL_API_KEY", Classe.REGLAGE, None,
             "Clé Mistral — absente, le worker d'embeddings ne tourne pas.",
             ("oto_mcp/embeddings.py:62",)),
    Variable("OTO_ANTHROPIC_ADMIN_KEY", Classe.REGLAGE, None,
             "Clé d'ADMIN Anthropic (`sk-ant-admin…`) de l'organisation qui porte la clé "
             "`anthropic` de plateforme — absente, `oto_admin_model_spend` refuse "
             "(`admin_key_not_configured`) : le relevé d'Anthropic ne se rapproche pas du journal.",
             ("oto_mcp/anthropic_usage.py:56",)),
    Variable("OTO_MAILER_SEND_BEARER", Classe.REGLAGE, None,
             "Bearer du relais mailer commun (byo-org a le sien en coffre).",
             ("oto_mcp/email.py:151",)),
    Variable("OTO_TEM_SECRET_KEY", Classe.REGLAGE, None,
             "Clé d'API Scaleway TEM de l'instance : ses emails de plateforme partent "
             "en direct par l'API TEM, sans relais mailer. Va avec `OTO_TEM_PROJECT_ID` "
             "et `OTO_TEM_REGION`, les trois ou aucune, et exclut `OTO_MAILER_URL`. "
             "Le domaine de `OTO_MAIL_FROM` doit être vérifié dans ce projet TEM : "
             "Scaleway refuse l'envoi sinon.",
             ("oto_mcp/email.py:50",)),
    Variable("OTO_TEM_PROJECT_ID", Classe.REGLAGE, None,
             "Projet Scaleway du TEM de l'instance — voir `OTO_TEM_SECRET_KEY`.",
             ("oto_mcp/email.py:51",)),
    Variable("OTO_TEM_REGION", Classe.REGLAGE, None,
             "Région Scaleway du TEM de l'instance (`fr-par`) — voir "
             "`OTO_TEM_SECRET_KEY`.",
             ("oto_mcp/email.py:52",)),
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
             "Release annoncée à Sentry — vide, l'étiquette de la version servie "
             "(`v1.2.3+6d5bf16b`, celle de `/api/version`).",
             ("oto_mcp/sentry_setup.py:103",)),
    Variable("OTO_SENTRY_TRACES_SAMPLE_RATE", Classe.REGLAGE, "0",
             "Taux d'échantillonnage du tracing de perf Sentry — 0 = off.",
             ("oto_mcp/sentry_setup.py:115",)),
    Variable("OTO_MCP_TENANT_MIGRATION_ISS", Classe.REGLAGE, None,
             "Issuer additionnel toléré pendant une migration de tenant.",
             ("oto_mcp/tenant_migration.py:60", "oto_mcp/tenant_migration.py:71")),
    Variable("OTO_MCP_ADMIN_SUB", Classe.REGLAGE, None,
             "Sub d'un compte admin plateforme, pour des scripts hors requête.",
             ("oto_mcp/access/scope.py:47",)),
    Variable("OTO_MCP_DEV_SUB", Classe.REGLAGE, None,
             "Repli d'identité DEV local, sans jeton — refusé au boot s'il coexiste "
             "avec `LOGTO_ENDPOINT` (`verifier_repli_identite_dev`).",
             ("oto_mcp/auth/hooks.py:164", "oto_mcp/config.py:179")),
    Variable("LOGTO_ENDPOINT_ALT", Classe.REGLAGE, "",
             "Second émetteur Logto toléré (coexistence multi-domaine), pour les "
             "personnes comme pour les services.",
             ("oto_mcp/server.py:228", "oto_mcp/auth/service_identity.py:91")),
    Variable("OTO_EXPORT_CLE_CIBLE", Classe.REGLAGE, None,
             "Clé maîtresse (hex 64 ou base64) de l'instance CIBLE d'un export par "
             "périmètre, pour cette seule exécution de `oto-mcp perimetre export` : les "
             "secrets y sont rechiffrés depuis `OTO_MCP_MASTER_KEY`. Absente, un périmètre "
             "qui porte des secrets refuse. Jamais dans l'environnement du serveur.",
             ("oto_mcp/export_perimetre/commande.py:49",)),
    Variable("OTO_DEPLOY_REF", Classe.REGLAGE, None,
             "Réf. git du deploy en cours, affichée par `/api/version`.",
             ("oto_mcp/version.py:61", "oto_mcp/version.py:122")),
    Variable("OTO_DEPLOY_SHA", Classe.REGLAGE, None,
             "SHA git du deploy en cours.", ("oto_mcp/version.py:62",)),
    Variable("OTO_DEPLOY_AT", Classe.REGLAGE, None,
             "Horodatage du deploy en cours.", ("oto_mcp/version.py:63",)),
)


NOMS_FIXES: tuple[Variable, ...] = _REQUISES + _IDENTITE + _REGLAGES


# Lectures d'environnement d'OTO-CORE qui ne concernent PAS ce serveur : le client qui
# les lit n'est jamais instancié par `oto_mcp/` (outil du CLI local, ou client de l'API
# oto elle-même). Le walker parcourt aussi oto-core (au pin) : toute lecture littérale
# qu'il y trouve est soit dans `NOMS_FIXES` (lue au nom du serveur), soit ici avec la
# raison pour laquelle le serveur n'en dépend pas. Une lecture neuve d'oto-core, rangée nulle part, fait rougir.
LUES_PAR_OTO_CORE_HORS_SERVEUR: dict[str, str] = {
    "OTO_API_URL": "base des clients oto-core qui appellent l'API oto elle-même "
                   "(`accords`, `datastore`, `ninja`, `sirene.stock`) — le serveur EST "
                   "cette API et n'instancie aucun de ces clients.",
    "LINKEDIN_NO_RATE_LIMIT": "client LinkedIn par navigateur (`tools/browser`), outil "
                              "du CLI local : l'extra `browser` n'est pas installé en "
                              "serveur.",
    "GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON": "bibliothèque Slides d'oto-core "
                                         "(`tools/google/slides`), que le serveur "
                                         "n'importe pas.",
}


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
        refs=("oto_mcp/auth/annuaire.py:98", "oto_mcp/auth/annuaire.py:105"),
    ),
    FamilleDynamique(
        nom="credential_management_annuaire_secret",
        prefixe="", suffixe="_SECRET",
        classe=Classe.REQUISE,
        description="Moitié `_SECRET` du même credential — voir "
                     "`credential_management_annuaire`.",
        refs=("oto_mcp/auth/annuaire.py:99", "oto_mcp/auth/annuaire.py:106"),
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
