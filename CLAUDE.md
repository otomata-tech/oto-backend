# oto-mcp

MCP server (Streamable HTTP) qui expose les connecteurs **oto-core** (`oto.tools`,
importés directement — **plus aucune dép à la CLI**) comme tools, branchable dans
claude.ai et Claude Code. Public **prod** = `https://mcp.oto.cx/mcp` (box Scaleway
dédiée ; `mcp.oto.ninja` = **preprod** depuis le cutover ADR 0040 — cf. `docs/auth-logto.md`).

**Positionnement : oto-mcp = le produit central, déployable** (SaaS hébergé OU
on-premise pour un client — image `Dockerfile`, config 100% par env). oto-cli =
façade locale basse priorité (fallback LinkedIn browser). Tout open source.

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

> **Ce fichier est une CARTE, pas un journal.** Il porte les conventions, les garde-fous
> et les pointeurs — ce qu'un agent doit avoir en tête à chaque session. Le détail dense
> (schémas, inventaires, incidents datés et leurs leçons) vit dans `docs/` — index en bas.
> **Un lot qui change un concept met à jour le doc du concept dans le même commit**, pas
> cette carte. Journal daté et récits d'incident migrés dans `docs/` le 2026-08-27.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=3.4.2` (plancher = dernier ; prod aligné au deploy via `pip install -e .`) + `mcp` SDK
- **`oto-core[browser]` PINNÉ sur un tag git** (`@ git+…@vX.Y.Z` dans `pyproject.toml`) : une version déployée = coordonnée reproductible. ⚠️ **`pip` ne réinstalle PAS une dép VCS déjà présente** → le deploy force-réinstalle depuis le tag lu du `pyproject`. ⚠️ **Le pin est un champ que TOUTES les sessions // éditent** : toujours bumper en **superset** (tag haut ⊇ tags bas), et à la moindre divergence de pin en merge/rebase, **garder la version haute**. Symptômes trompeurs en local (venv en retard ou en avance sur le pin) : `docs/commands.md` §Pin oto-core.
- `psycopg[binary]` + `psycopg-pool` (PostgreSQL managed Scaleway `otomata-main`, DB `oto_mcp`) pour le state par utilisateur. ⚠️ **Les rows sont des DICTS (accès par nom de colonne `r["col"]`), JAMAIS positionnel `r[0]`** (→ `KeyError: 0`). Row factory `_str_dict_row` + leçon du fail-open masqué par des tests stubbés : `docs/conventions.md`.
- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)

## Architecture

⚠️ **Le dossier d'un fichier EST son domaine** (tranché le 27/08) : une famille de
≥ 4 fichiers au même marqueur devient un package et les fichiers y perdent ce marqueur
(`datastore/schema.py` → `datastore/schema.py`), `tests/` en est le miroir, et un
déplacement ne laisse **jamais** de ré-export à l'ancien chemin. La règle complète (le
critère, le module « nu » en `core.py`, les trois façades d'exception, où naît un
fichier neuf) : **`docs/conventions.md` §Où vit un fichier**.

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, _SERVER_INSTRUCTIONS, routes /api, tools
├── capabilities/     # les CAPACITÉS (ADR 0009), sous-rangées par domaine : `orgs/`,
│                     #   `connectors/`, `datastore/`, `groups/`. Le socle commun
│                     #   (`registry`, `_authz`, `_types`, `_mcp_adapter`,
│                     #   `_rest_adapter`) reste à la racine du package.
├── api/              # la face REST `/api/*` : `routes` (la TABLE — son ordre est un
│                     #   contrat), `base` (auth, CORS, `_json`, préflight, `bind`),
│                     #   puis un module de handlers par domaine. ⚠️ Une route neuve
│                     #   naît CAPACITÉ, pas ici : la dette REST vaut ZÉRO.
├── auth/             # QUI parle au serveur, et comment un credential s'ACQUIERT.
│                     #   Entrant : hooks (le sub du jeton), facade (façade DCR devant
│                     #   Logto), token_scopes (portée d'un jeton `oto_…`), anon (shim
│                     #   OAuth des endpoints publics). Sortant : flow (la danse
│                     #   `authorization_code`, écrite UNE fois), pkce, puis un module
│                     #   par fournisseur — atlassian, folk, google, salesforce, zoho.
├── connectors/       # le connecteur côté PLATEFORME : activation, selection, identities,
│                     #   link, flow, verify, field_schema, schema_store + `docs/` (la
│                     #   fiche how-to en markdown) et `docs_reader`. ⚠️ trois voisins à
│                     #   ne pas confondre : `providers/` = le REGISTRE, `tools/` = les
│                     #   outils servis à l'agent, `connectors/` = la gouvernance.
├── fod/              # les clients du service FOD (ADR 0028) : `http` = le transport
│                     #   partagé, un module par domaine de données publiques FR.
├── datastore/        # le spine de records typés : core (le store qui COMPOSE), schema,
│                     #   schema_ops, columns, errors, journal. `__init__` sans code.
├── middleware/       # la chaîne MCP, 1 module par middleware (alias, empty_result,
│                     #   call_context, field_redaction, error_envelope, disabled_tools,
│                     #   dynamic_instructions). L'ORDRE d'enregistrement est un contrat
│                     #   et vit dans `server.py` — figé par
│                     #   `tests/middleware/test_middleware_order.py`.
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── providers/        # le REGISTRE : 1 module de déclaration par connecteur
│                     #   (`CONNECTOR = _c(…)`), `__init__` AGRÈGE, `_model` = la forme.
│                     #   Séparé de tools/ : le registre doit rester PUR (tools/ importe
│                     #   access → le registre, et se charge en try/except — une dép
│                     #   optionnelle manquante retirerait le connecteur du catalogue).
├── access/           # rôles, contexte, cascade de credentials, quotas (package depuis le 27/08 — 2 000 lignes en 7 modules). Surface plate `access.<fn>` via __init__, cf. ci-dessous
│   ├── scope.py      #   qui agit : rôle plateforme, current_org/group/project, ce que le projet ÉPINGLE
│   ├── quotas.py     #   ce qui est métré (quota jour, usage) et ce qui est payé (option, comp, abonnement)
│   ├── cascade.py    #   le WALKER unique perso > cross-org > équipe > org > plateforme + ses 3 sondes
│   ├── rbac.py       #   qui a le droit : RBAC connecteur org/équipe, tools masqués, garde d'instance, redaction
│   ├── resolve.py    #   la résolution réelle d'un credential (chemin chaud) + l'endpoint anonyme
│   ├── views.py      #   vues minces : resolve_api_key/_fields, mount, credential_mode_for, option_open
│   └── status.py     #   le snapshot par connecteur de /api/me
├── db/               # store PG (package) : _conn (pool/connexion), _schema (DDL), _init (migrations) + 1 module/domaine (users, keys, usage, datastore, projects, opendata…). Surface plate `db.<fn>` via __init__
├── org_store/        # palier ORG (package, découpé le 2026-08-27) : orgs (la fiche), members (appartenance + MAISON),
│                     #   vault (secrets), settings (redaction/email/MFA), personal (org perso + boot),
│                     #   invitations (plateforme/org/équipe), instructions (procédures), library (doctrines publiées).
│                     #   DAG à 2 étages : {personal, invitations} → {orgs, members} ; library → instructions.
│                     #   Surface plate `org_store.<fn>` via __init__, qui REDESCEND les écritures sur le module
│                     #   propriétaire (sinon un monkeypatch de test serait mort en silence). Cliquet :
│                     #   `tests/test_org_store_surface_frozen.py` (surface, signatures, DAG, 500 lignes).

└── config.py         # require_env

deploy/
├── oto-mcp.service       # systemd, User=root, /opt/oto-mcp, port 9103
├── Caddyfile.snippet     # mcp.oto.ninja → 9103 (pas de bearer-gate, masquerait WWW-Authenticate)
└── DEPLOY.md             # procédure DNS + Caddy + systemd + Claude.ai

```

L'extension Chrome (Oto Companion) vit dans `oto-app/extension/` (repo
`otomata-tech/oto-app`, monorepo des fronts). Elle parle au backend via REST :
`POST /api/settings/linkedin` + endpoints `/api/whatsapp/pair/*` (SSE).

**4 couches à frontière à sens unique** (ADR 0004) : **backend-core** (`db`,
`credentials_store`, `org_store`, `access`, `crypto`, `providers`, `auth.hooks`) —
**adaptateur MCP** — **adaptateur REST** — **runtime connecteurs**. Adaptateurs et runtime
dépendent du backend-core, **jamais l'inverse**, et l'appellent **par interface**
(`access.resolve_*`), pas par accès table croisé.

Une opération exposée sur **deux faces** s'écrit UNE fois : une **capacité**
(`oto_mcp/capabilities/`, ADR 0009) co-déclare handler core + `Input` pydantic (seule
validation) + règle `authz` **obligatoire** + bindings `mcp`/`rest`. Jamais un
`@mcp.tool()` main-écrit doublé d'une route REST (ADR 0042 §Convergence des surfaces,
garde-fou CI). La validation **REFUSE un champ inconnu** côté REST (400 `unknown_fields`) :
garde posée au SEAM, **sans allowlist**, tripwire `test_rest_rejects_unknown_fields.py`.
⚠️ L'autz reste **déclarée au niveau capacité**, jamais redescendue dans le handler.
⚠️ **Secret brut jamais en argument MCP** — la pose de secret est dashboard-only (binding
`mcp` retiré) ; le MCP ne porte que les droits/grants.
⚠️ **`capabilities/` est SOUS-RANGÉ par domaine depuis le 28/08** : `orgs/`,
`connectors/`, `datastore/`, `groups/` (les quatre familles de ≥ 4 modules), le socle
commun (`registry`, `_authz`, `_types`, les deux adaptateurs) restant à la racine. Le
hub `capabilities/__init__.py` déclare chaque module en **import absolu** — il ne lie
aucun nom court, donc `orgs/core` et `groups/core` ne se disputent rien et l'ordre des
déclarations reste celui qu'on lit.
**Détail : `docs/couches-et-capacites.md`.**

La face REST **assemble** et n'implémente plus : depuis le 2026-08-27, `api/routes.py`
ne contient aucun handler (`make_routes` : 1 370 lignes et 52 handlers imbriqués → ~180
lignes de montage), les handlers vivent par domaine dans `api_routes_<domaine>.py`.
⚠️ La table est FIGÉE par `tests/api/test_api_routes_table_frozen.py` — retirer, ajouter ou
RÉORDONNER un chemin fait rouge (Starlette prend le premier match), et régénérer
`tests/api/api_routes_table.txt` EST la déclaration de l'ajout. **Détail : `docs/rest-api.md`.**

## Auth — Logto

JWT Logto **ES384** (défaut RS256 = tout rejeté), discovery RFC 9728 sur 401,
façade DCR self-service (`auth/facade.py`) pour les clients sans DCR (Claude/ChatGPT/
Mistral). **Détail : `docs/auth-logto.md`** (jetons API, registre d'émetteurs, env, onboarding).

⚠️ **Logto = 2 instances** : la vraie prod/preprod = **`auth.oto.ninja`** (creds SOPS
`LOGTO_NINJA_MGMT_*`), PAS `auth.oto.zone`.

⚠️ **`MCP_AUDIENCE_ALT` est une LISTE (virgules) : ÉTENDRE, jamais remplacer.** Chaque
environnement a SA liste (`/opt/oto-mcp/.env` ≠ `/opt/oto-mcp-canari/.env`) : poser une
audience sur l'un ne la pose PAS sur l'autre. Même règle pour tout env-liste partagé
(`OTO_MCP_CORS_ORIGINS`, `MAILER_FROM_DOMAINS`, SPF, redirect URIs OAuth) : lire la
valeur, y ajouter, réécrire. Cutover `.ninja`↔`.cx` + incident vécu : `docs/auth-logto.md`
§CUTOVER.

### Tenants (ADR 0052)

Un tenant tiers est **servi** depuis le 13/08 : émetteur dédié, host, client OAuth, socle
d'instructions et **préfixe d'outils** (`tenants.tool_prefix`, traduit au bord du protocole
seulement — le nom canonique est rétabli avant que quoi que ce soit d'autre ne le lise).
- ⚠️ **La découverte annonce la FAÇADE sur le host du tenant, JAMAIS son émetteur en direct.**
- ⚠️ **Pas de patron, pas de lien** (`links.py`, `tenants.link_paths`) : ne jamais coller
  nos chemins sous le domaine d'un partenaire.
- ⚠️ **OPS — une bascule de tenant ABANDONNE les clés personnelles** (l'AAD dérive de
  l'entité) : toute fenêtre s'accompagne de la LISTE « qui repose quelles clés », prévenue avant.
- Suivi = écran `/platform/tenants`, **lecture seule par construction** ; prise d'effet par
  `oto_admin_tenant op=reload`, ⚠️ **par-process** (recharger la preprod ne recharge pas la prod).

**Détail : `docs/tenants.md`.**

## Rôles + résolution de clé API

3 paliers `member < admin < super_admin` (accès admin UI). Résolution de clé par appel :
`clé membre (sub, org) > group_secret > org_secret > platform_grant` (chemin platform gaté
sur `auth_modes`). Tout connecteur dont le credential se **POSE** est **multi-compte**
(comptes nommés membre/équipe/org, sélection `_account=` / `account` / `is_default`).
⚠️ Un compte nommé introuvable partout ⇒ « introuvable » après la marche, **jamais un repli
plateforme silencieux**. ⚠️ **Le nombre de champs du credential ne dit RIEN de la cardinalité** —
un connecteur vraiment mono s'exclut par `single_account` dans son entrée de registre, jamais
par une liste transverse. ⚠️ **Scope MEMBRE (ADR 0033)** : la clé BYO est keyée `(sub, org)`,
jamais org-agnostique ; l'org de scope = seam `current_org`, à la pose comme à la résolution.
⚠️ La cascade = walker unique `access.walk_cascade` — **ne jamais la recopier dans un
call-site** ; `access.resolve_credential` rend aussi la **config non-secrète appariée à la clé
gagnante**, donc **ne JAMAIS recâbler un résolveur d'endpoint par-connecteur** (ADR 0024).
⚠️ Le dashboard en porte un **MIROIR d'affichage** (`lib/keyStack.ts`) qu'aucun test ne relie :
en changer l'ordre ne casse rien, **ça fait mentir l'UI**.
⚠️ **Un credential se LIT à son palier et s'ÉCRIT par MERGE depuis le 27/08**
(oto-backend#448) : `me.credential.get` prend un `scope` (`member` défaut / `group` /
`org`, admin du palier exigé) et rend les champs `reveal` — jamais un secret ; et un
`fields` partiel est **complété côté serveur** (clé absente = conservée, clé présente
et vide = effacée, donc le formulaire qui poste tout reste un remplacement). Les deux
n'ont de sens qu'ensemble : lecture impossible + remplacement total = piège à perte de
données, vécu sur un pont client. ⚠️ L'éligibilité de ces routes suit le PALIER
(`is_byo_user` au membre, `is_org_shareable` au-dessus) — elle testait `is_byo_user`
partout, ce qui rendait « inconnu » tout connecteur `byo_org` pur, donc **tous les
ponts clients**. Détail : `docs/connector-vault.md`.
⚠️ **L'instance de connecteur est un OBJET depuis le 27/08** (`connector_instances`, lot L6 du
chantier tenant & instances — R1 tranché « la table à côté ») : elle a un **id stable**,
`inst:{id}`, servi à côté du `ref` composé par `GET /api/me/connector-instances`. La table est
posée **à côté** du coffre (l'AAD lie le ciphertext aux 4 colonnes de SA ligne, pas à sa table
⟹ zéro rechiffrement), et le lien est le **quadruplet** `owner_type/owner_id/connector/account`
= la PK du coffre, en FK **logique**. **Rien ne la lit encore côté résolution** — ni
`walk_cascade`, ni `resolve_credential`, ni le coffre : c'est le lot L7, et un garde-fou AST
(`tests/test_connector_instances_l6.py`) fait tomber le premier lecteur hors allowlist.
⚠️ L'instance naît **au boot** (backfill idempotent d'`_init`), pas à la pose : `id` peut donc
manquer sur une clé toute neuve, et **`ref` reste la référence à repasser** (`inst:` se parse
mais se fait refuser nommément par les deux gardes de pose). `label`, `config`, `visibility`
(R9) et `parent_id` (sous-instances) sont **posés et inertes** : leur domicile reste `meta`,
leur dérivation est un lot. Détail : `docs/connector-vault.md`.
⚠️ **`access` est un PACKAGE depuis le 27/08** (7 modules par sujet, arbre ci-dessus) : on
édite le module du sujet, jamais un fourre-tout. La surface reste **plate** — `access.<nom>`
rend ce qu'il rendait, privés compris, et une écriture sur la façade
(`monkeypatch.setattr(access, …)`) traverse jusqu'au sous-module qui définit le nom.
Deux règles à l'intérieur : un frère s'appelle **par son module** (`scope.current_org(...)`,
jamais un nom importé — c'est ce qui garde le point de patch unique), et les dépendances
descendent (`scope < quotas/cascade < rbac < resolve/status < views`) — un besoin qui
referme la boucle **descend** le symbole partagé d'un étage. Cliquet :
`tests/test_access_surface_frozen.py`.
**Détail : `docs/roles-and-resolution.md`.**

## REST API (consommée par le dashboard / oto.ninja)

Endpoints `/api/*` (compte, settings, orgs, admin, datastore…), même `JWTVerifier` que
`/mcp`. `GET /openapi.json` sert un OpenAPI **dérivé** du registre de capacités (sans auth,
`/api/admin/*` exclu) ; un jeton `oto_` peut naître **porté** (deny-by-default borné), et sa
**gestion** exige une session interactive — un jeton ne fabrique plus de jeton.
⚠️ Avant d'ouvrir une nouvelle surface aux intégrations : **ce qu'un jeton porté atteint doit
se lire dans le chemin.**
⚠️ **CORS : la liste du code est MORTE en prod comme en preprod** — les deux box posent
`OTO_MCP_CORS_ORIGINS` dans leur `.env`, qui **écrase** la liste. **Ajouter une origine =
éditer l'env des deux box + restart.** Diagnostic en 1 appel :
`curl -X OPTIONS https://mcp.oto.cx/api/mcp/catalog -H 'Origin: <x>'`.
**Inventaire + incidents : `docs/rest-api.md`.**

## Browser automation & LinkedIn — substrat hébergé Browserbase (ADR 0026)

Plus AUCUN browser sur la box : les connecteurs d'**API privée cookie-bound** (`brevo`,
`crunchbase`, `pennylaneged`) passent par **Browserbase** (Chrome hébergé, Context per-user =
la session loguée au coffre, Live View pour le login interactif). LinkedIn = **Unipile** ;
l'injection de cookie `li_at` côté serveur déconnecte l'user (#5). S'y ajoute le connecteur
**générique `browser`** : lire N sites derrière login sans un connecteur par site —
**un site = un compte du coffre** ; `browser_eval` masqué par défaut.
**Détail : `docs/browser-automation.md`.**

## SIRENE stock (DuckDB sur parquet INSEE)

Stock complet (~43M établissements, parquet ~2 Go) interrogé via DuckDB depuis l'Object
Storage (httpfs, ADR 0002) ; tools MCP `fr_stock_*` (namespace `fr`, connecteur `sirene`) +
REST `/api/sirene/*` (noms de routes **inchangés** — `oto-cli`/`oto-core` en dépendent).
⚠️ Pour **chercher** des boîtes (secteur/zone/taille), préférer **`fr_search`** (API indexée,
<1 s) ; le parquet = lookups ponctuels, **bulk** (`fr_stock_enrich` = 1 scan) et énumération
exhaustive >10k. **Détail : `docs/sirene-stock.md`.**

⚠️ **`categorie_entreprise` (PME/ETI/GE) est calculée par l'INSEE sur le périmètre GROUPE,
jamais sur l'entité** — une filiale minuscule sort en « GE ». C'est un faux négatif de
ciblage vécu (4 leads sur 5 écartés à tort). **`fr_groupe`** (`tools/fr_groupe.py`, #337)
sépare les deux : il remonte/descend la chaîne par les **mandataires personnes morales du
RNE**, commissaires aux comptes exclus, chaque lien qualifié (`forte` = détention impliquée /
`moyenne` = mandat social / `faible` = ni l'un ni l'autre). ⚠️ **Le RBE est fermé au public
depuis le 31/07/2024** : une SAS sans mandataire personne morale sort `indeterminee`, ce qui
ne veut PAS dire « indépendante » — et le descendant s'appuie sur l'index plein texte amont
(qui indexe les dirigeants), donc il rend un ÉCHANTILLON dès `candidats_tronques=true`. Le
contrat amont vérifié (ce que `q` sait faire, les bornes 25/page et 10 000) vit dans la
docstring du module.

## Recherche transverse & KB projets (suivi oto-private#67)

**`oto_search`** (capacité `me.search`, MCP + `GET /api/me/search`) = LE verbe « retrouver »,
un seul chemin de code : fusion RRF **lexical + sémantique** (embeddings Mistral, dégradation
gracieuse — jamais un prérequis). **Invariant « cherchable ⇔ lisible »**, avec **tripwire par
source = critère de merge** (`test_search_scope_tripwire.py`).
⚠️ **Deux grains distincts pour un tableau** : `tableau` = le CONTENEUR (nom + labels de
colonnes), `ligne` = le CONTENU. Un **fichier** est matché sur `filename+title+description`,
**jamais son contenu**. **Détail : `docs/search-and-kb.md`.**

## Datastore (spine natif PG, ADR 0016)

Spine plateforme de stockage structuré (PG/JSONB natif, plus Google Sheets). Surfaces : tools
`data_*` (MCP) + REST `/api/datastore/*` (**100 % dérivée** depuis le 12/08, donc soumise au
refus de champ inconnu) ; OAuth Google per-user câblé ici. Où poser un lot : le datastore est
**découpé par coutures** — `db/paths` · `db/query` (**PUR**) · `db/rowlock` · `db/datastore_ns`
(le TABLEAU) · `db/datastore` (les LIGNES) · `datastore/errors` (**aucune dépendance**) ·
`datastore/columns` · `datastore/schema` · `datastore/schema_ops` · `datastore/core.py`
(le store qui COMPOSE).
⚠️ La surface plate `db.<fn>` est un **cliquet** (`tests/test_db_surface_frozen.py` : on peut
ajouter, jamais retirer). ⚠️ Oto gère les **types standards**, **jamais l'interprétation métier
d'une VALEUR** — entre les deux, l'ordre des `options` déclarées au schéma est honoré, parce
que c'est une DEMANDE adressée à oto, pas une compréhension du métier. Même frontière pour
`field.pattern` (28/08) : la forme d'un code se contraint, une grammaire structurée se refuse.
⚠️ **Une expression régulière posée par un appelant s'exécute dans la boucle UNIQUE** : un
motif à explosion combinatoire coûte le serveur entier, et un garde syntaxique ne suffit pas
(mesuré : `.*.*.*.*.*.*.*z` sur 60 caractères = 14,8 s, sans un seul groupe quantifié). D'où
un BUDGET calculé sur l'arbre du motif contre la borne du champ — ce qui rend `max_length`
obligatoire avec `pattern`, et refuse **à la pose**, en nommant, ce qu'on ne sait pas majorer.
⚠️ **Une pose de schéma REMPLACE** : sa réponse porte `declarations_effacees` (ce qu'elle
vient de retirer, valeurs comprises — elle en est la seule copie) et `enforced` (les clés de
validation que CETTE version applique, établies en faisant tourner le validateur, jamais
listées). Pour ÉDITER, `data_patch_schema` fusionne par clé et ne peut pas détruire.
**Détail : `docs/datastore.md`.**

## Propriété de ressource — primitive `ownership` (ADR 0030)

`ownership.py` = seam unique : ressource possédée par `(owner_type∈{user,group,org},
owner_id)` + partages `resource_grants` (deny-by-default), audience × rôle (ADR 0048).
**Deux plans jamais confondus** : `can_access` (contenu, privacy by default) vs `can_govern`
(gouvernance, escalade roles.py) ; le **transfert** reste `owner ∪ escalade`, jamais un gérant.
⚠️ **Une LISTE de contenu scope sur `active_owner(current_org)`, JAMAIS `owner_pairs()`**
(union de toutes les orgs = fuite fail-open ; tripwire `test_owner_scope_tripwire.py`).
Plus de « perso » : tout user a une org perso dédiée (`orgs.personal_of`).
**Détail : `docs/ownership.md`.**

## Projet — couche d'organisation (ADR 0030/0032)

Conteneur de travail **possédé** : brief + liens typés (tableau/procédure/connecteur/doc) +
docs en arbre. Capacités `oto_project`/`oto_doc` ; partage/transfert via `oto_resource`. Un
projet publié est servi sur son sous-domaine dédié — **anonymous** (`<slug>.mcp.oto.cx`) /
**secret** (`<slug>.share.oto.cx`, UI navigable lecture seule rendue server-side) / **org**.
La page navigable est un **canal d'acquisition** : « Ajouter à mon Oto » → `me.import_project`,
qui forke un projet publié dans l'org active — **structure only, jamais de credentials**.
**Détail : `docs/projects.md`.**

## Messagerie & LinkedIn (Unipile)

**Sept connecteurs depuis le 2026-08-28** : `unipile` = le **compte** chez le
fournisseur (la clé, le quota, la clé plateforme, l'option couche-3) ; `linkedin`,
`whatsapp`, `telegram`, `instagram`, `messenger`, `twitter` = les **connexions**, une
carte et un flux hébergé chacune. ⚠️ **Deux questions, deux noms** : ce qui GATE
(activation, ACL, sélection, visibilité) prend le nom NU ; ce qui touche à la CLÉ prend
`providers.credential_provider(nom)` → `unipile` (`Connector.credential_of`, normalisé
**dans `walk_cascade` seulement**). Les confondre rejoue la divergence du 2026-07-07.

Tools `{whatsapp,telegram,instagram,messenger,twitter}_chat` + **`linkedin_*`** =
**Unipile hébergé** (factory channel-agnostic, `account_id` per-membre `(sub, org_id,
provider)` ADR 0033, **no-fallback anti-usurpation**). Mode plateforme (clé partagée + grant +
option comp), DSN par credential, sélecteur d'identité, comptes partagés autorisés (#55 —
grants revalidés à chaque appel, **jamais de repli silencieux**).
⚠️ **`aiark` n'est PAS touché** : mêmes tools `linkedin_aiark_*`, même clé, même grant
plateforme, tels qu'en prod. Ce qui change, c'est que le nom NU `linkedin_*` va désormais à
la session hébergée — il avait été libéré le 10/08 par le dépôt de l'ex-connecteur `linkedin`
(#231), il n'est pris à personne. Les deux cohabitent par `namespace_of` (**plus long préfixe
DÉCLARÉ au registre**, pas le 1er token) : `linkedin_aiark_person` → `aiark`, `linkedin_post`
→ `linkedin`. Sans cette règle, les tools d'AI Ark tomberaient sous la session — mauvaise clé,
mauvaise activation, mauvaise sélection. **C'est LE cas d'usage de la règle, et il est vivant.**
Et ce sont deux CHOSES différentes, pas deux fournisseurs interchangeables : AI Ark VEND de la
donnée (email vérifié, mobile, reverse-lookup) dont LinkedIn n'est qu'une source — sa famille
est dropcontact/fullenrich ; la session, elle, EST LinkedIn. Tripwire `tests/test_linkedin.py`.
**Détail : `docs/unipile.md`.**

## Monitoring des appels MCP

`ToolCallLogger` (middleware inliné `oto_mcp/calllog.py`) journalise chaque appel dans
`tool_calls` (best-effort, identité = `sub` du JWT). Trois étages de lentilles — membre / org
(`oto_org_monitoring`) / plateforme (`oto_admin_monitoring`).
⚠️ **Ne trace QUE les invocations d'outils MCP** — pas la connexion du connecteur, pas le
`tools/list`, pas les appels REST. Donc **compte actif ≠ usage**.
⚠️ Les colonnes de corrélation dépendent de **l'ordre des middlewares** (§Conventions).
⚠️ **Ce n'est PAS une purge de logs** : la table est aussi la **source de vérité des
exécutions** — `run_start`/`run_finish` sont **exemptés de toute suppression** par la rétention
90 jours + archivage froid mensuel (hors du processus MCP, mono-boucle).
⚠️ **`client_id` n'identifie PAS le front d'où vient l'utilisateur** : énumérer avant d'en
tirer une population. **Détail : `docs/monitoring.md`.**

## Error tracking (Sentry)

Exceptions backend → **Sentry SaaS** (gaté `OTO_SENTRY_DSN`, no-op si absent). Deux captures :
500 des routes REST (intégration Starlette) et exceptions des tools MCP
(`SentryToolErrorMiddleware` — une erreur de tool est un JSON-RPC en HTTP 200, invisible à
Starlette). ⚠️ RGPD : `send_default_pii=False`, **jamais** les args d'appel dans l'event ;
`before_send` droppe les 4xx amont. Région **EU** `de.sentry.io` ; triage = doctrine oto
`surveillance-erreurs`. **Détail : `docs/monitoring.md` §Error tracking.**

## Onboarding = un projet « Découverte » (ADR 0032 §7)

**Plus de mode d'accueil spécial** (retiré le 2026-07-01) : pas de booléen `onboarded`, pas de
checklist, pas de tool scripté — l'onboarding est **un projet** semé à la création de l'org
perso. La fiche « situation avec oto » reste, découplée : capacité `me.profile`, deux faces
(⚠️ divergence **VOULUE** — `op=update` (agent) filtre les valeurs vides pour qu'un agent
n'efface pas la fiche par mégarde ; le `PUT` (humain) écrit tel quel). `oto_whoami` expose
l'**identité MCP courante** — à appeler avant une action sensible.
**Détail : `docs/onboarding-et-profil.md`.**

## Runner hébergé & automatisations

Le backend porte l'**ÉTAT** du runner d'agents (fil `run_messages`, file `runner_jobs`,
déclencheurs `runner_triggers` + leurs capacités) ; la **BOUCLE** vit dans le repo public
**`otomata-tech/oto-runner`**. ⚠️ Les jetons de contexte (`_project`…) sont advertisés **PAR
TOOL** : un client les pose d'après le schéma du tool, jamais à l'aveugle. ⚠️ La reprise
inter-agents lit le **JOURNAL**, jamais le fil. Le connecteur `routine` déclenche une routine
Claude Code hébergée — ⚠️ **il relaie, il n'apporte rien d'autre**, et il n'y a **aucune API
publique de création de routine ni de génération de jeton**.
**Détail : `docs/runner-et-automatisations.md`.**

## Boucle d'usage (ADR 0017)

Flux d'événements de session unifié : calllog (involontaire) + feedback volontaire d'agent
(`feedback`, signal=tool_feedback|gap) + runs / déroulés (`run_start`/`run_finish`).
⚠️ **Le vocabulaire d'issue vient de l'ADR, pas de la mesure : la mesure tranche ce que l'ADR
laisse ouvert, jamais ce qu'elle a fermé.** Le silence d'un run (48 h sans appel rattaché) est
**dérivé à la lecture**, jamais stocké. **Détail : `docs/usage-loop.md`.**

## Email (envoi per-org, par connecteur)

Deux connecteurs **BYO-org** : `scaleway` (API TEM directe, fields — domaine garanti
par Scaleway) + `resend` (BYOK). `email_send` =
spine qui route `sender→connecteur→transport` ; config `orgs.email_settings` par connecteur
(senders + quiet hours) ; envoi différé (`scheduler.py`, quiet hours 20h–8h défaut).
⚠️ Le front qui héberge une org (`orgs.front_base_url`/`front_brand`) est **dérivé de l'org
CIBLE, jamais déclaré par l'appelant** — sinon c'est un champ d'API publique qu'il faudra
retirer à l'arrivée de l'étage tenant. ⚠️ Aucune surface n'édite ces colonnes (UPDATE à la
main). **Détail : `docs/email.md`.**

## Visibilité des outils (per-user, org/équipe, socle)

La toolbox d'une session = denylist calculée `(sub, org active)` dans `session_visibility.py`,
appliquée au handshake par `UserDisabledToolsMiddleware` (visibility rules natives fastmcp).
Régime **NOMINAL « non-sélectionné = masqué »** (ADR 0019/0050 ; socle de départ **VIDE**).
Règle effective : override positif prime > désactivé > masqué par un admin > masqué-par-défaut
plateforme > visible.
⚠️ **`PROTECTED_TOOLS` (`tool_visibility.py`, source unique) = quatre familles jamais
masquables ni désactivables** : méta-toolset + identité, échappatoires de contexte
(anti-lockout), boucle d'usage, dispatch universel.
⚠️ **Gouvernance de visibilité, PAS une barrière de sécurité** (ADR 0031) — additif entre
paliers : une équipe ne peut jamais RÉVÉLER un tool que l'org a masqué.
⚠️ **Un outil admin est masqué par son AUTORISATION, jamais par son nom** (28/08) : le
plancher de rôle plateforme est déclaré au niveau capacité (`_authz.platform_floor`), et
celui d'un `ADMIN_BY_OP` est **le plus BAS de ses branches**. Le test de préfixe
`oto_admin_*` rendait `op=remove` (autz `ORG_ADMIN_OF`) injoignable au responsable
d'organisation — et le masquage bloque l'APPEL, pas seulement la liste.
⚠️ **Stdio local (sub=None) = accès complet.** **Détail : `docs/tool-visibility.md`.**

## Org/équipe : session vs maison vs consultation (ADR 0023, amende 0015)

Le pointeur unique « org active » est scindé en **3 notions** — session (éphémère, MCP) /
consultation (REST, header `X-Oto-Org`) / maison (défaut persistant) — résolues par le **seam
unique `access.current_org(sub)`** = `session ?? consultation ?? maison` (miroir
`current_group`). **TOUTE résolution d'action passe par ce seam** — ne plus lire
`org_store.get_active_org` en direct dans un chemin de résolution (**tripwire**
`tests/test_org_seam_tripwire.py`).
⚠️ **Ce seam est scopé sur l'ACTEUR courant** : `current_org(autre_sub)` renvoie le contexte du
**requérant**, pas du tiers — **NE JAMAIS** l'utiliser (ni `status_for`/`has_option`/
`credential_mode_for` qui en dérivent) pour calculer l'état d'un **tiers** (écran admin).
Passer son org/groupe **explicitement** via le kwarg `org`/`group`.
**Détail : `docs/org-context.md`.**

## Agent readme (cumulable) & procédures

**Agent readme** = prose libre **injectée à chaque session**, cumulée du général au spécifique
(plateforme → org → équipe active → user) ; les 4 étages vivent dans `guides` delivery='init'
et **s'éditent par UNE surface**, la capacité `me.guide{,s}` (ADR 0042). **Procédure** =
doctrine nommée, chargée à la demande ; les identifiants de code gardent le mot *doctrine*.
⚠️ **Une procédure s'OUVRE sur son digest et embarque son SCHÉMA — deux sections requises.**
Le digest n'est **jamais fabriqué** (sourcé sur le journal des runs, ou rien). La grammaire du
dessin est un **CONTRAT** (reparsé en graphe) : **UN** seul bloc fencé **non tagué** — guide
plateforme `procedure-flowchart`. **Détail : `docs/doctrines.md`.**

## Groupes (départements) & hiérarchie de droits (ADR 0012)

Une org se subdivise en **groupes** (départements/équipes) avec un **chef d'équipe**
(`group_role='group_admin'`). Droits **centralisés dans `roles.py`** (escalade descendante,
source unique) : `platform_admin ⊇ org_admin ⊇ group_admin ⊇ member`. Un groupe **gouverne 3
ressources** par délégation de l'org : secrets partagés (cascade `user_key > groupe actif > org
active > plateforme`), doctrine & skills, gouvernance de connecteur.
⚠️ **Invariant monotone** : l'équipe RÉTRÉCIT ce que l'org expose, jamais l'inverse
(platform ⊇ org ⊇ group). ⚠️ **Groupe actif** : ≤1 par sub, il appartient à l'org active.
⚠️ **Aucun module du package `org_store/` n'importe `group_store`** (SQL direct dans
`org_store/members.py` pour l'invariant org↔groupe, pas de cycle) — la règle a survécu à la
découpe du 2026-08-27 et est désormais **vérifiée** (`test_org_store_surface_frozen.py`). ⚠️ Migrations vivantes sur la DB partagée = playbook `docs/live-migrations.md`.
**Détail : `docs/groups-and-roles.md`.**

## Fédération MCP & comptes (otomata#16)

Deux mécanismes : **mount** (MCP distant fédéré, token OAuth per-user, pilote atlassian) vs
**remote** (bridge data-driven ADR 0003, token M2M d'org). **Plus aucun mount monté d'office**
(fédération en sommeil, masters atlassian/justicelibre OFF en prod ; le connecteur `memento` a
été RETIRÉ le 2026-07-30 — la mémoire est native `oto_kb`) : un mount suit le régime commun
d'activation (DB `connector_activation` ∪ env `OTO_MCP_MOUNTS_ENABLED`).
**Détail : `docs/federation.md`.**

## MCP Apps — UI rendue (SEP-1865)

Certains tools renvoient une **interface rendue** (iframe sandbox côté host) au lieu de JSON
brut, via `prefab_ui` (extra `fastmcp[apps]`). **Convention** : variantes **flagship `*_app`**
(≠ remplacer les tools JSON), import **optionnel et guardé**.
⚠️ **Pas d'annotation de retour `-> Card`** sur un tool `app=True` (NameError fatal au boot).
⚠️ **Guides = tout-DB** : la table `guides` est la source de vérité ; les fichiers
`oto_mcp/guides/*.md` ne sont que des **seeds de boot** — une édition durable doit AUSSI
retoucher le fichier seed. **Détail : `docs/mcp-apps.md`.**

## Veille protocole MCP — suivre les SEP en amont (acté 2026-07-30)

**Règle : on suit les SEP, pas les specs.** Une spec publiée est un fait accompli ; un SEP en
discussion est une décision qu'on peut anticiper (voire influencer). Revue périodique des SEP
`proposed`/`accepted` qui touchent **ce qu'on utilise** ; on ignore ce qu'on n'a jamais adopté.
**Détail : `docs/mcp-spec-watch.md`** — d'où vient la règle, et les 4 points à traiter d'ici la
migration vers la spec `2026-07-28`.

## Conventions

**`docs/conventions.md`** — les règles de travail, chacune née d'un incident daté :
test qui décrit le système et non l'intention, garde-fou exercé sur le montage RÉEL,
aucune adresse en dur, jetons de contexte réservés, budget de ce qu'un outil renvoie,
**où vit un fichier** (le dossier = le domaine, familles de ≥ 4 en package, `tests/` en
miroir), ordre des middlewares, contrainte MONO-LOOP, et le cycle complet d'un connecteur.
**À lire avant d'écrire du code ici.**

⚠️ **Le refus est bruyant, la divergence est muette — et le CI le vérifie.** Un
`except Exception` qui ne re-lève pas, ne journalise pas et ne rend pas de refus nommé
transforme une panne en succès : l'appelant reçoit une valeur de repli et croit avoir
été servi. `scripts/lint_silences.py` (tripwire `tests/test_no_silent_except.py`) le
refuse ; l'unique échappatoire est `# noqa: SILENT — <raison>`, sur la ligne du
`except` ou juste au-dessus, **raison obligatoire**. Les 168 sites existants sont
annotés : c'est de la dette DÉCLARÉE, pas un permis. **Détail et inventaire :
`docs/silences-2026-08-27.md`.**

## Commands

Tests, déploiement, logs, inspection DB : **`docs/commands.md`** — avec les pièges qui coûtent une heure (le venv sans pytest, le clone qui teste en réalité le tree partagé, le registre d'outils vide hors serveur).

## Infra

Déployé sur une **box Scaleway dédiée** (ADR 0002, depuis 2026-06-11) : oto-backend isolé + Caddy + chiffrement du coffre actif, sert `mcp.oto.ninja`. **DB** = PostgreSQL managé partagé (`otomata-main`, DB `oto_mcp`). Le coffre `connector_credentials` est chiffré au repos (AES-256-GCM, master key en Secret Manager fetchée au boot, 0 plaintext). Object Storage S3 pour avatars/logos (`media_store.py`).

> **Détails machine = repo privé `otomata-tech/infra`** (IPs, IDs de secrets/zone/instance, systemd, runbook deploy, env de process) — pas ici (ce repo est public). Voir `infra/docs/oto-platform-state.md` + docs ciblés (`scaleway-managed-db.md`, `caddy.md`, `cloudflare.md`, `deploy-keys.md`). Toute intervention prod = skill `prod-init`.

> ⚠️ **PROD et PREPROD partagent la MÊME base.** Une donnée écrite depuis la preprod est
> **la donnée de prod** (pas un bac à sable) ; et toute config portée par une COLONNE ne
> peut avoir qu'**une** valeur pour les deux environnements — ce qui exclut de distinguer
> prod/preprod par la base. Vérifier avant de raisonner dessus : comparer les DSN par hash,
> jamais en les lisant en clair. Détail : `docs/live-migrations.md`.

## Docs

**À lire en premier** : `docs/conventions.md` (les règles de travail) puis
`docs/connector-model.md` (la carte d'ensemble d'un connecteur).

| doc | ce qu'il porte |
|---|---|
| `conventions.md` | les **règles de travail** du backend, chacune née d'un incident daté, et **où vit un fichier** (le dossier = le domaine). À lire avant d'écrire du code ici. |
| `commands.md` | recettes tests / deploy / logs / inspection DB + leurs pièges, et le **pin oto-core**. |
| `silences-2026-08-27.md` | inventaire AST des `except` muets, les 10 « succès déguisés » corrigés, et le garde-fou `lint_silences` + sa convention `# noqa: SILENT`. |
| `couches-et-capacites.md` | les 4 couches ADR 0004 et la **couche capacité** ADR 0009 (deux faces, une déclaration ; refus de champ inconnu ; console admin `*_op`). |
| `connector-model.md` | **carte d'ensemble** : les 3 couches d'un connecteur (disponibilité / authentification / option), la matrice des niveaux, le vocabulaire canonique, le seam `access.has_option`. **À lire en premier** avant de toucher activation/clés/options. |
| `connector-vault.md` | registre source unique (package `providers/`, 1 module de déclaration par connecteur), coffre chiffré unique `connector_credentials`, enveloppe AES-256-GCM **obligatoire**, résolution + palier org, **l'instance objet `connector_instances` + `inst:{id}` (lot L6)**, credentials qui se consomment à l'usage (rotation), application d'org ≠ jeton d'identité. |
| `roles-and-resolution.md` | rôles (3 paliers), cascade de résolution de clé, grants/platform keys, multi-compte, scope MEMBRE, walker de cascade. |
| `auth-logto.md` | auth Logto ES384, discovery RFC 9728, façade DCR, jetons `oto_`, MFA par org, cutover `.ninja`↔`.cx`. |
| `tenants.md` | l'étage d'identité au-dessus des orgs (ADR 0052) : émetteur dédié, préfixe d'outils, écran de suivi, pièges d'une bascule de compte. |
| `org-context.md` | org/équipe : session vs maison vs consultation, seam `current_org` scopé sur l'acteur, view-as USER en lecture seule. |
| `groups-and-roles.md` | groupes/départements & hiérarchie de droits (ADR 0012). |
| `ownership.md` | primitive de ressource possédée (`can_access`/`can_govern`, tripwire `owner_pairs`, partage audience × rôle, abolition du perso). |
| `tool-visibility.md` | visibilité des outils : denylist de session, paliers org/équipe, sélection par membre, `PROTECTED_TOOLS`. |
| `rest-api.md` | inventaire des endpoints REST `/api/*`, OpenAPI dérivé, jetons portés, CORS. |
| `datastore.md` | datastore spine PG (`data_*`), OAuth Google per-user (setup GCP, scopes), coutures des modules, face REST dérivée. |
| `datastore-colonne-tableau.md` | spec de la colonne-tableau (oto#22 barreau 2) : forme servie, couches d'un item, fonctions natives, non-définitions, chemin de migration en double-service. |
| `projects.md` | projet (liens typés, docs), livraison client cascade, endpoint MCP + partage navigable par projet (`<slug>.{mcp,share}.oto.cx`). |
| `search-and-kb.md` | `oto_search` : RRF lexical+sémantique, grains matchés, invariant « cherchable ⇔ lisible », épine, backlinks, propositions. |
| `doctrines.md` | doctrine & skills d'org (`oto_procedure`, versionnée), forme d'une procédure (digest + schéma), agent readme, **renommer un outil = migrer les procédures** (refs `<tool:slug>` en DB, angle mort du CI). |
| `onboarding-et-profil.md` | onboarding = un projet « Découverte », fiche « situation avec oto » (`me.profile`), `oto_whoami`. |
| `unipile.md` | **le split compte/canaux (28/08)**, mode plateforme, DSN, sélecteur d'identité, comptes partagés (#55), historique des renommages LinkedIn. |
| `browser-automation.md` | substrat Browserbase (Context/Live View/run_fetch), connecteurs brevo/crunchbase/pennylaneged, connecteur générique `browser`, LinkedIn isolation de session. |
| `email.md` | envoi per-org par connecteur (scaleway BYO TEM + resend), différé/quiet hours, front qui héberge l'org. |
| `federation.md` | fédération MCP : mount (per-user) vs remote/bridge (org). |
| `mcp-apps.md` | MCP Apps (SEP-1865) : `prefab_ui`, convention `*_app`, gotchas, guides tout-DB. |
| `mcp-spec-watch.md` | veille protocole MCP : on suit les SEP, pas les specs ; les 4 points à traiter d'ici la spec `2026-07-28`. |
| `runner-et-automatisations.md` | runner hébergé (l'état ici, la boucle dans `oto-runner`) + connecteur `routine`. |
| `usage-loop.md` | boucle d'usage ADR 0017 (calllog + feedback + déroulés), runs persistés, run silencieux. |
| `monitoring.md` | monitoring & investigation des appels (trois étages, recette d'enquête, rétention 90 j + archivage froid, Sentry). |
| `event-loop-perf.md` | les **3** modes de gel mono-loop + protections + recettes py-spy/aiodebug. ⚠️ Le 3ᵉ (27/08) n'est pas un I/O mal placé mais une **requête lente** : même signature py-spy que le 2ᵉ, remède opposé — indexer, pas déplacer. |
| `redaction.md` | rédaction de champs **et rendu du résultat servi** : middleware unique, rien par défaut + templates 1-clic, **schéma OBSERVÉ**, dry-run preview, moteur `FieldFilter` (oto-core) — et la règle **« un résultat VIDE se sert en PHRASE, jamais en structure nue »** (oto#32, 27/08/2026). |
| `live-migrations.md` | migrations vivantes sur la DB partagée canari/prod : la danse en N lots promus, les techniques, les pièges — et le fait que **prod et preprod partagent la base**. |
| `sirene-stock.md` | stock SIRENE en DuckDB sur parquet INSEE : source S3, perfs, refresh, `fr_stock_*`, routes REST. |
| `connector-test-gate-theirstack-origami.md` | la porte de test LOCALE des deux connecteurs de prospection (unitaire, lecture live, écriture sur un espace jetable) : ce qu'un contributeur externe doit avoir fait passer avant de pousser, sans serveur qui tourne. |
| `billing.md` | abonnement par org (ADR 0043) : le modèle sans objet subscription Mollie, **le mandat qui naît quelques minutes APRÈS l'encaissement** (une course, pas un échec), les 3 invariants anti-double-débit, les deux files de reprise du runner, **la règle de TVA par pays et le montant débité en TTC** (#486) — et l'incident du 25/08/2026. |
