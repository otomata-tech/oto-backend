# oto-backend — paquet `oto-mcp`

Le backend d'oto, **produit central et déployable** (SaaS ou on-premise : `Dockerfile`, config 100 % par env) : un
serveur MCP (Streamable HTTP, toujours authentifié Logto) qui expose les connecteurs **oto-core** (`oto.tools`, importés
directement — aucune dépendance à la CLI) et une face REST `/api/*` sur le même service. **Prod** = `mcp.oto.cx`,
**preprod** = `mcp.oto.ninja` (ADR 0040) ; le tableau de bord servi aux utilisateurs vient de `config.dashboard_url()`.

> **Ce fichier est une CARTE, pas un journal** : où vit quoi, les règles en vigueur, les pointeurs — ni date ni récit
> d'incident ; l'histoire qui a produit chaque règle vit dans `docs/` (index en bas). **Un lot qui change un concept
> met à jour le doc du concept dans le même commit**, pas la carte. **`docs/conventions.md` se lit avant d'écrire du
> code ici.** **Ce dépôt est PUBLIC** : aucun nom de client, de personne ni de domaine client (`acme`, `Jane Doe`,
> TLD `.test`) — hygiène tenue à la relecture, sans contrôle automatique.

## Stack & environnement

Python `>=3.10` · `fastmcp[apps]>=3.4.2,<3.5` (plancher ET plafond : monter de version est un acte) · `psycopg[binary]`
+ `psycopg-pool` · JWT Logto ES384 · `oto-core[anonymize]` **pinné sur un tag git** (`pyproject.toml`).
⚠️ `pip` ne réinstalle pas une dép VCS déjà présente (le deploy force-réinstalle au tag) ; le pin est édité par toutes
les sessions parallèles → bumper en **superset**, garder la version haute à tout conflit.
⚠️ `.venv` est **partagé** et porte une copie figée d'oto-core : une grappe de rouges sur les connecteurs récents est un
venv en retard sur le pin — la suite le dit (bannière `PIN oto-core`, tests `exige_pin_oto_core` non concluants) ; ne
pas trier au message, ne pas muter le venv : `docs/commands.md` §Pin oto-core → « Faux rouge ».
⚠️ Les rows PG sont des **dicts** — `r["col"]`, jamais `r[0]`.

## Architecture

```
oto_mcp/   server.py (FastMCP + uvicorn, montage /api + tools) · config.py (require_env, domaines, dashboard_url)
  capabilities/ les CAPACITÉS (ADR 0009), par domaine · api/ la TABLE de routes (ordre = contrat) + 1 handler/domaine
  auth/ qui parle, comment un credential s'acquiert · connectors/ la GOUVERNANCE (activation, sélection, identités)
  providers/ le REGISTRE, 1 déclaration/connecteur, reste PUR · tools/ 1 module/connecteur · fod/ clients FOD (ADR 0028)
  datastore/ le spine de records typés · middleware/ la chaîne MCP — l'ORDRE d'enregistrement est un contrat
  access/ rôles, contexte, cascade, quotas (surface plate) · org_store/ le palier ORG · db/ le store PG (surface plate)
deploy/    unités et timers systemd (/opt/oto-mcp, :9103), Caddyfile.snippet, scripts de déploiement et d'ingestion
```
⚠️ **Le dossier d'un fichier EST son domaine** : ≥ 4 fichiers au même marqueur → package, `tests/` en miroir, jamais de
ré-export à l'ancien chemin (`docs/conventions.md` §Où vit un fichier). **4 couches à sens unique** (ADR 0004) :
backend-core (`db`, `credentials_store`, `org_store`, `access`, `crypto`, `providers`, `auth.hooks`) ← adaptateur MCP,
adaptateur REST, runtime connecteurs — jamais l'inverse, et par interface. ⚠️ Une opération sur deux faces s'écrit UNE
fois, comme **capacité** (ADR 0009) : **une route neuve naît capacité**, pas dans `api/` ; **secret brut jamais en
argument MCP** ; la table de routes est **figée** (`docs/architecture.md`, `docs/couches-et-capacites.md`).

## Auth, rôles, coffre, REST & version servie

JWT Logto **ES384**, discovery RFC 9728, façade DCR ; au-dessus des orgs, l'étage **tenant** (ADR 0052 : un partenaire
sert oto sous sa marque). ⚠️ Logto prod/preprod = **`auth.oto.ninja`**, pas `.zone` · ⚠️ **un env-liste s'ÉTEND, ne se
remplace jamais** (`MCP_AUDIENCE_ALT`, `OTO_MCP_CORS_ORIGINS`, `MAILER_FROM_DOMAINS`, SPF, redirect URIs), chaque env
ayant la sienne (`docs/auth-logto.md`, `docs/tenants.md`).
Paliers `member < admin < super_admin` ; clé résolue à chaque appel par le **walker unique** `access.walk_cascade`
(`perso > cross-org > équipe active > org > tenant > plateforme`), jamais recopié ; un credential qui se **pose** est
**multi-compte** · ⚠️ compte nommé introuvable ⇒ « introuvable », **jamais un repli plateforme silencieux**
(`docs/roles-and-resolution.md`, `docs/connector-vault.md`).
**Mettre un compte en pause** (03/09/2026) : le cran qui manquait entre « vivant » et « supprimé » — et **la
suppression n'existe pas** comme geste de produit, le seul `DELETE FROM users` étant l'étape 4 de `migrate_sub`. Un
compte en pause ne peut plus rien faire **dès la requête suivante, jeton déjà émis compris** (les deux branches
d'`api.base._authenticate`, la levée d'`upsert_user`, et un middleware MCP sur **`on_request`** — pas `on_call_tool`,
sinon un compte sorti lirait encore les instructions d'org du handshake) · ⚠️ **rien n'est détruit ni détaché** :
appartenances, projets, documents, coffre et journal restent et le désignent toujours (ADR 0062-D4) · ⚠️ **aucune
résurrection automatique** : `upsert_user` refuse de recréer une ligne dont la **chaîne** d'alias mène à un compte en
pause, `migrate_sub` refuse la fusion **dans les deux sens, acte d'opérateur compris** · ⚠️ **le drain d'alias ne s'en
charge PAS** (son prédicat de vivacité est l'existence de la ligne, qu'une pause conserve) · ⚠️ **les sièges ne
bougent pas parce qu'il n'y en a aucun** (forfaits plats par org) · ⚠️ **pas un org_admin** — un compte n'appartient
pas à une org ; c'est l'admin de **tenant**, sur les comptes du sien (`docs/comptes-en-pause.md`).
`/api/*` sous le même `JWTVerifier` que `/mcp` ; `GET /openapi.json` **dérivé** du registre de capacités ; un jeton
`oto_` peut naître **porté** · ⚠️ **CORS : la liste du code est morte**, chaque box pose `OTO_MCP_CORS_ORIGINS` dans son
`.env` (`docs/rest-api.md`). Une étiquette de version unique sur trois surfaces (`GET /api/version`, `info.version`
OpenAPI, en-tête **`X-Oto-Version` de chaque réponse**) · ⚠️ elle dit **ce que le processus exécute**, pas ce qu'un run
vert a déployé (`docs/version-servie.md`).

## Org active, équipes & propriété

« Org active » = trois notions — session (MCP) / consultation (REST, `X-Oto-Org`) / maison — résolues par le **seam
unique** `access.current_org(sub)` · ⚠️ toute résolution d'action passe par ce seam, **scopé sur l'acteur courant**,
jamais pour l'état d'un tiers (ADR 0023, `docs/org-context.md`). Groupes avec chef d'équipe, droits centralisés dans
`roles.py` (`platform_admin ⊇ org_admin ⊇ group_admin ⊇ member`), procédures d'équipe via le store unifié
`org_store.<fn>('group', …)`, **la garde suit le verbe** (écrire = membre, supprimer = chef) · ⚠️ **invariant
monotone** : l'équipe rétrécit ce que l'org expose, jamais l'inverse (ADR 0012, `docs/groups-and-roles.md`).
`ownership.py` = seam unique de la ressource possédée (`owner_type∈{user,group,org}` + `resource_grants`
deny-by-default) ; **deux plans**, `can_access` (contenu) vs `can_govern` (gouvernance) ; le **projet** est le
conteneur de travail possédé · ⚠️ une liste de contenu scope sur `active_owner(current_org)`, **jamais
`owner_pairs()`** — fuite fail-open (ADR 0030/0032, `docs/ownership.md`, `docs/projects.md`).

## Outils servis : visibilité, guides, journal

Denylist `(sub, org active)` dans `session_visibility.py`, appliquée au handshake ; régime **« non-sélectionné =
masqué »** ; `PROTECTED_TOOLS` (`tool_visibility.py`) = jamais masquables ; stdio local = accès complet · gouvernance,
**pas une barrière de sécurité** (ADR 0031) · ⚠️ `BETA_TOOLS` = population **choisie** (option `beta` posée par un
admin), **fail-closed**, **noms neufs seulement** · ⚠️ **un contrat servi ne se durcit pas en place, il se double** :
l'héritée garde son défaut écrit dans sa description, la stricte l'exige (ADR 0019/0050, `docs/tool-visibility.md`).
**Agent readme** = prose injectée à chaque session, cumulée plateforme → org → équipe → user, éditée par la seule
surface `me.guide{,s}` (ADR 0042) ; **procédure** = guide nommé chargé à la demande, qui s'ouvre sur son digest et
embarque son schéma · ⚠️ l'injection au handshake **n'est pas garantie** : le bloc A est un socle ≤ 2 000 c. (budget CI
`tests/test_instructions_budget.py`) qui pointe le guide `notice` et `oto_context` · ⚠️ guides = **tout-DB**,
`oto_mcp/guides/*.md` sont des seeds (`docs/guides.md`, `docs/alias-deprecies.md`).
`ToolCallLogger` journalise chaque appel dans `tool_calls` (identité = `sub`), lu par trois lentilles (membre / org /
plateforme) ; exceptions vers **Sentry** · ⚠️ ne trace ni la connexion d'un connecteur ni `tools/list` → **compte actif
≠ usage** · jamais un jeton en clair ; la table est la **source de vérité des exécutions** (ADR 0017, `docs/monitoring.md`).

## Données & autres sous-systèmes

- **SIRENE stock** (ADR 0002) : le stock INSEE complet, DuckDB sur parquet depuis l'Object Storage — tools `fr_stock_*`
  + REST `/api/sirene/*` (**routes figées**, `oto-cli`/`oto-core` en dépendent) · ⚠️ chercher une boîte = `fr_search`,
  le parquet sert lookups et bulk · ⚠️ `categorie_entreprise` est celle du **groupe** (`docs/sirene-stock.md`).
- **Datastore** (ADR 0016) : PG/JSONB natif, tools `data_*` + REST `/api/datastore/*` (100 % dérivée), découpé par
  coutures (`db/datastore_ns` = le tableau, `db/datastore` = les lignes, `datastore/core` compose) · ⚠️ **une pose de
  schéma remplace**, éditer = `data_patch_schema` · ⚠️ **écrire la couche `origine` se DÉCLARE**
  (`origine_override=true`, les deux faces + au mint d'un upload signé) : à partir du **1er octobre 2026** la poser
  sans le dire est refusé — ce n'est pas un droit à obtenir, la date vit dans le code et `OTO_ORIGINE_REFUS_LE` la
  déplace (`docs/datastore.md`).
- **Browser & cookie-bound** (ADR 0026) : aucun browser sur la box — **Browserbase** pour l'API privée cookie-bound,
  Unipile pour LinkedIn, le générique `browser` traite un site comme un compte du coffre (`docs/browser-automation.md`).
- **Messagerie** : `unipile` = le **compte**, plus six **connexions** au nom du réseau, noms de tools inchangés ·
  ⚠️ `namespace_of` résout au **plus long préfixe déclaré**, pas au 1er token (`docs/unipile.md`).
- **Email per-org** : `scaleway` (TEM) et `resend` en BYO-org, `email_send` route `sender → connecteur → transport` ·
  ⚠️ le front qui héberge une org est **dérivé de l'org cible** (`docs/email.md`).
- **Relance des comptes jamais actifs** : **REST seule** (`oto_admin_outreach`) · ⚠️ comptée par **boîte mail**, jamais
  par compte ni par org — un humain s'inscrit deux fois, et l'index unique `(campagne, sub)` ne voit pas ce doublon-là ;
  tenant partenaire écarté **par la requête** ; la langue se choisit, ne se devine pas (`docs/relance-comptes.md`).
- **Facturation & avantages offerts** : `billing.status` porte `granted[]` pour les deux façons d'offrir (abonnement
  `comp`, don d'option) · ⚠️ l'avantage **se nomme** et **est un avantage ce qui est vendu** — un drapeau de population
  comme `beta` n'est pas un cadeau · ⚠️⚠️ rien qui s'adresse au titulaire d'une org ne touche une org d'un **tenant
  tiers** · ⚠️ le discriminant est `db.org_tenant_slug`, **union de trois axes** — `orgs.tenant_id` porte désormais,
  mais il est ÉCRIT par quelqu'un quand les deux autres se DÉRIVENT du jeton, donc jamais seul · ⚠️ l'usage inclus **ne refuse
  rien** et ne sert aucun ratio (`docs/billing.md`).
- **Recherche & KB** : `oto_search` = LE verbe « retrouver », fusion RRF lexicale + sémantique · ⚠️ invariant
  **« cherchable ⇔ lisible »**, tripwire par source = critère de merge (`docs/search-and-kb.md`).
- **Onboarding & profil** (ADR 0032 §7) : pas de mode d'accueil, un projet « Découverte » semé à la création de l'org
  perso ; `oto_whoami` avant une action sensible (`docs/onboarding-et-profil.md`).
- **Runner** : l'**état** ici (`run_messages`, `runner_jobs`, `runner_triggers`), la **boucle** dans `otomata-tech/oto-runner`
  · ⚠️ la reprise inter-agents lit le **journal**, jamais le fil (`docs/runner-et-automatisations.md`).
- **Fédération, MCP Apps, veille** : **mount** (OAuth per-user) vs **remote** (bridge M2M d'org), aucun mount monté
  d'office ; `prefab_ui` rend les `*_app` (`docs/federation.md`, `docs/mcp-apps.md`, `docs/mcp-spec-watch.md`).

## Démarrage, silences & infra

**Construire n'est pas démarrer** : `_build_mcp` monte le catalogue, `main()` seul prépare la base
(`_prepare_database()`, une fois par process) et demande les catalogues fédérés (`include_mounts=True`). **Aucune
instance au niveau module** — un import doit définir, pas travailler (`test_server_construction.py`) · **Au boot, le
DDL additif et rien d'autre**, fail-open étape par étape : un backfill qui casse ne doit pas empêcher le serveur de
répondre, mais il le **dit** dans le journal · ⚠️ **la fenêtre du healthcheck est finie (120 s)** : un travail one-shot
ajouté au boot se mesure **avant** de poser son tag, ce qui n'a rien à faire au boot va en maintenance (`oto-mcp
maintenance …`, timer quotidien **prod seulement**), et **tout ce qui attend un tiers doit avoir un délai maximal à son
propre niveau** — une borne posée au plus profond ne borne rien si les enveloppes au-dessus attendent sans délai
(oto-backend#892) · ⚠️ **le boot se juge sur une base qui existe déjà**, jamais sur une base vierge —
`test_boot_order_replay.py` rejoue la séquence sans les colonnes posées par `ALTER` (ADR 0065,
`docs/live-migrations.md`, `docs/migrations-versionnees.md`).
⚠️ **Le refus est bruyant, la divergence est muette** : `scripts/lint_silences.py` (joué par la suite) refuse un
`except Exception` qui ne re-lève, ne journalise ni ne rend un refus nommé ; échappatoire unique `# noqa: SILENT —
<raison>` (`docs/silences-2026-08-27.md`).
**Box Scaleway dédiée** (ADR 0002) : oto-backend isolé + Caddy ; DB = PG managé partagé (`otomata-main`, DB `oto_mcp`) ;
coffre `connector_credentials` chiffré au repos (AES-256-GCM, master key en Secret Manager au boot) ; S3 pour
avatars/logos · ⚠️ **PROD et PREPROD partagent la MÊME base** : ce qu'on écrit depuis la preprod est la donnée de prod
(`docs/live-migrations.md`) · **détails machine et procédure de déploiement = repo privé `otomata-tech/infra`**, pas ici
(ce repo est public) ; intervention prod = skill `prod-init`.

## Docs

- `conventions.md` — règles de travail, où vit un fichier · **à lire en premier**
- `connector-model.md` — un connecteur : disponibilité / auth / option · **puis**
- `commands.md` — tests, deploy, logs, inspection DB, pin oto-core, et les pièges qui coûtent une
  heure (venv sans pytest, clone qui teste en réalité le tree partagé, **faux rouges d'un venv en
  retard sur le pin**, registre d'outils vide)
- `architecture.md` — l'arbre des modules, les 4 couches
- `couches-et-capacites.md` — ADR 0004 + capacités
- `connector-vault.md` — registre, coffre, instances
- `roles-and-resolution.md` — paliers, cascade de clé
- `groups-and-roles.md` — hiérarchie de droits
- `org-context.md` — session / maison / consultation
- `ownership.md` — `can_access`/`can_govern`, partages
- `tool-visibility.md` — denylist, `PROTECTED_TOOLS`
- `auth-logto.md` — Logto, DCR, jetons `oto_`
- `tenants.md` — l'identité au-dessus des orgs
- `rest-api.md` — endpoints, OpenAPI, jetons, CORS
- `version-servie.md` — dater un changement : les 3 surfaces, les 3 coordonnées qui mentent
- `noeuds.md` — le NOUVEL univers de contenu : page/tableau/ligne, `props` vs `data`, les deux
  univers côte à côte, l'arrêt de la recopie
- `datastore.md` — spine PG `data_*`, OAuth Google
- `datastore-colonne-tableau.md` — sa spec
- `projects.md` — liens, partage, périmètre d'URL
- `search-and-kb.md` — `oto_search`, RRF, grains
- `guides.md` — guides & skills d'org, procédure
- `alias-deprecies.md` — noms doublés, date de retrait
- `onboarding-et-profil.md` — Découverte, `me.profile`
- `unipile.md` — split compte/canaux, DSN, identités
- `browser-automation.md` — Browserbase, cookie-bound
- `email.md` — envoi per-org, quiet hours
- `relance-comptes.md` — relancer qui n'a jamais rien fait : le comptage, l'exclusion partenaire, l'absence de signal de langue
- `federation.md` — mount vs remote/bridge
- `mcp-apps.md` — `prefab_ui`, convention `*_app`
- `mcp-spec-watch.md` — les SEP, pas les specs
- `runner-et-automatisations.md` — l'état ici, la boucle ailleurs
- `usage-loop.md` — calllog, feedback, déroulés
- `monitoring.md` — enquête, rétention, Sentry
- `event-loop-perf.md` — les 4 gels mono-loop
- `silences-2026-08-27.md` — `except` muets, `# noqa: SILENT`
- `redaction.md` — rédaction de champs, résultat servi
- `live-migrations.md` — migrations vivantes, base partagée
- `migrations-versionnees.md` — ce que le boot exécute
- `sirene-stock.md` — DuckDB sur parquet INSEE
- `connector-test-gate-theirstack-origami.md` — porte de test locale
- `billing.md` — abonnement par org, Mollie, TVA, **avantage offert / échéance / usage inclus**
