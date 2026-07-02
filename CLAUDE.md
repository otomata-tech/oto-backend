# oto-mcp

MCP server (Streamable HTTP) qui expose les connecteurs **oto-core** (`oto.tools`,
importés directement — **plus aucune dép à la CLI**) comme tools, branchable dans
claude.ai et Claude Code. Public : `https://mcp.oto.ninja/mcp` (box Scaleway
dédiée — cf. §Infra).

**Positionnement : oto-mcp = le produit central, déployable** (SaaS hébergé OU
on-premise pour un client — image `Dockerfile`, config 100% par env). oto-cli =
façade locale basse priorité (fallback LinkedIn browser). Tout open source.

La page de gestion utilisateur (cookie LinkedIn, etc.) vit dans le site Vue
oto.ninja sous `/account` et parle au MCP via REST.

## Stack

- Python 3.10 (target `>=3.10` — c'est ce que tuls.me a)
- `fastmcp>=3.4.2` (plancher = dernier ; prod aligné au deploy via `pip install -e .`) + `mcp` SDK
- **`oto-core[browser]` PINNÉ sur un tag git** (`@ git+…@vX.Y.Z` dans `pyproject.toml`, plus `@main` flottant ni dép `oto-cli`) : une version déployée = coordonnée reproductible. ⚠️ **`pip` ne réinstalle PAS une dép VCS déjà présente** (`oto-core` "satisfait" quelle que soit sa version) → `pip install -e .` seul ne monte JAMAIS oto-core au tag bumpé. Le deploy **force-réinstalle** oto-core depuis le tag lu du `pyproject` (`pip install --force-reinstall …@$tag`). Bump connecteurs = tag oto-core + édit du pin + deploy (PAS de `git pull` box). Cf. ADR 0020. (⚠️ box `otomata-0` a un VIEUX oto-mcp décommissionné/stoppé avec un editable legacy `oto-cli` pré-split — ne pas s'y fier, le runtime live est la box dédiée.)
- `psycopg[binary]` + `psycopg-pool` (PostgreSQL managed Scaleway `otomata-main`, DB `oto_mcp`) pour le state par utilisateur — migré depuis SQLite le 2026-05-20. Row factory custom dans `db/_conn.py` (`_str_dict_row`) qui normalise `datetime`/`date` → strings "YYYY-MM-DD HH:MM:SS" : sinon `JSONResponse` crash sur `/api/me` car le code historique attend des strings comme avec SQLite. ⚠️ **Les rows sont des DICTS (accès par nom de colonne `r["col"]`), JAMAIS positionnel `r[0]`** (→ `KeyError: 0`). Vécu 2026-06-25 : deux fonctions RBAC en `r[0]` plantaient à chaque appel, **masqué** par leur fail-open + des tests qui stubbaient ces fonctions → bug invisible jusqu'à un seed réel. Leçon : un **fail-open silencieux + des tests stubbés cachent un bug de forme de row** ; exercer le vrai chemin (cf. [[feedback_verify_empirically]]).
- Auth = JWT Logto (`RemoteAuthProvider + JWTVerifier(jwks_uri=…, algorithm="ES384")`)

## Architecture

```
oto_mcp/
├── server.py         # FastMCP + uvicorn, _SERVER_INSTRUCTIONS, routes /api, tools
├── tools/            # 1 module par connecteur, chacun expose register(mcp)
├── api_routes.py     # /api/me, /api/settings/*, /api/admin/* (CORS oto.ninja)
├── access.py         # rôles member/admin, resolve_api_key, quotas, status_for
├── db/               # store PG (package) : _conn (pool/connexion), _schema (DDL), _init (migrations) + 1 module/domaine (users, keys, usage, datastore, projects, opendata…). Surface plate `db.<fn>` via __init__

├── auth_hooks.py     # current_user_sub_from_token() pour le contexte tool
└── config.py         # require_env

deploy/
├── oto-mcp.service       # systemd, User=root, /opt/oto-mcp, port 9103
├── Caddyfile.snippet     # mcp.oto.ninja → 9103 (pas de bearer-gate, masquerait WWW-Authenticate)
└── DEPLOY.md             # procédure DNS + Caddy + systemd + Claude.ai

```

L'extension Chrome (Oto Companion) vit dans `oto-app/extension/` (repo
`otomata-tech/oto-app`, monorepo des fronts). Elle parle au backend via REST :
`POST /api/settings/linkedin` + endpoints `/api/whatsapp/pair/*` (SSE).

## Couches (ADR 0004 — topologie réversible)

oto-mcp porte aujourd'hui 4 métiers ; ils sont des **couches à frontière à sens unique** (ADR 0004) :

- **backend-core** (le centre) : `db`, `credentials_store`, `org_store`, `access`, `crypto`, `connectors`, `auth_hooks`. Identité (`sub`), coffre, orgs, grants/quotas, résolution.
- **adaptateur MCP** : `server`, `tools/*`, `middleware`, `tool_visibility`.
- **adaptateur REST** : `api_routes`.
- **runtime connecteurs** : `tools/*` (in-process) + `tools/remote` (forward bridges).

**Règle** : adaptateurs + runtime → dépendent du backend-core, **jamais l'inverse** ; et ils l'appellent **par interface** (`access.resolve_*`), pas par accès table croisé — pour qu'un seam puisse devenir un service (broker de credentials) sans réécriture. ✅ Le seam **résolution** (le candidat broker) est consolidé dans `access` : `resolve_api_key` / `resolve_credential_fields` / `resolve_crunchbase_session`. C'est la frontière qui doit rester nette (elle peut devenir un service). `tools/meta` (visibilité) et `tools/datastore` (partage) appellent `db` en direct, et **c'est OK** : par le principe ADR 0004 (« pas de discipline d'interface sans force ») ils ne sont pas des candidats-services → pas de reroute dogmatique.

### Couche capacité (`oto_mcp/capabilities/`, ADR 0009)

Pour les opérations exposées sur **deux faces** (MCP + REST), arrêter de câbler les adaptateurs 2× à la main (drift de surface + autz divergente — ex. `oto_use_org` jadis absent en REST, IDOR cross-org). Une **capacité** = un descripteur co-déclaré : `handler` core + `Input` pydantic (seule validation) + règle `authz` **obligatoire** + bindings `mcp`/`rest` (multi-binding possible). Les adaptateurs `_mcp_adapter`/`_rest_adapter` **bouclent** sur `registry.CAPABILITIES` et appliquent **validation → autz → handler** ; le refus est un `AuthzDenied` neutre traduit par chaque face (`McpError` / `json_error`+CORS). `authz` = combinateurs fermés (`SUB_ONLY`, `ORG_MEMBER`, `ORG_ADMIN`, `ORG_MEMBER_OF`, `PLATFORM_ADMIN`, `SUPER_ADMIN`, `ORG_ADMIN_OF`, `GROUP_MEMBER_OF`, `GROUP_ADMIN_OF`) — `ORG_MEMBER`/`ORG_ADMIN` scopent l'**org active** (lecture/écriture self-service `/api/me/*`), les `*_OF(field)` une org/groupe ciblé par id de path. Schéma MCP **plat** via `apply_flat_signature` (gotcha pydantic single-param, cf. memory). **Écho `_org`** (2026-07-02) : `_mcp_adapter` injecte `_org` `{id, name}` dans tout payload dict de capacité org-sensible (`ctx.org_id` posé) — le client voit l'org effective à chaque réponse (désambiguïse post-`oto_use_org` ; face MCP seulement, le REST connaît son contexte). Montés dans `server._build_mcp` + `api_routes.make_routes` (no-op si registre vide). **Domaines orgs + doctrine/instructions 100% migrés** : orgs (use_org, membres, secrets, create, lectures) → 100% en capacités, `api_routes_orgs.py` supprimé ; doctrine (`capabilities/orgs_instructions.py` : get/list/set/delete/versions/revert/usage membre `/api/me/instructions*` + outils `oto_*_doctrine`, et palier admin cross-org `oto_admin_*_doctrine` / `/api/admin/orgs/{id}/instructions*`) — `tools/orgs.py` supprimé, bloc doctrine d'`api_routes.py` retiré. ⚠️ Handler async supporté par les deux adaptateurs (`inspect.isawaitable`) ; le manifeste `referenced_tools` (ADR 0014) résout l'instance FastMCP via `tool_registry.bind(instance)` (posé au boot dans `_build_mcp`). **Domaine user-admin migré** (`capabilities/users_admin.py`) : retrouver/lister un user (`oto_admin_list_users`, filtre `query`), fiche (`oto_admin_user_detail`, par email **ou** sub), rôle (`oto_admin_set_role`), grant de clé plateforme user **et** org (`oto_admin_grant_key`/`oto_admin_grant_org_key` + revoke), option payante comp (`oto_admin_set_option`) — les handlers REST écrits main correspondants ont été retirés d'`api_routes.py` (mêmes chemins servis par les capacités → dashboard inchangé). Donne la face MCP au **setup complet d'un compte depuis Claude**.

**Console admin consolidée par concept (`*_op`, 2026-06-25, commit 92462fe).** Les outils admin ci-dessus sont fusionnés de **36 → 12 `oto_admin_*`** — un outil par objet métier, verbe en param `op` : `oto_admin_{org,org_member,user,access,key_grant}`. Les handlers de domaine sont **réutilisés tels quels** (zéro duplication ; `capabilities/admin_console.py` construit l'`Input` spécifique et appelle `_create_org`/`_add_member`/…). Quand les paliers d'autz divergent dans un même outil (ex. `org` : create=`SUPER_ADMIN`, list=`PLATFORM_ADMIN`), le **combinateur op-aware `ADMIN_BY_OP({op: règle})`** (`_authz.py`) choisit la règle fermée selon `inp.op` → l'autz reste **déclarée au niveau capacité**, jamais redescendue dans le handler (esprit ADR 0009 préservé). ⚠️ Les faces **REST restent par-verbe** (idiomatique + dashboard) → l'autz d'un verbe fusionné est désormais déclarée 2× (MCP op-aware + route REST), même combinateur/handler dessous. **Règle de design — secret brut jamais en argument MCP** (il transiterait dans le contexte LLM) : la **pose** de secret (`set_org_secret`, `delete_org_secret`, `set_platform_key`, `set_quota`) est **dashboard-only** (binding `mcp` retiré, REST conservé) ; le MCP ne porte que les **droits/grants** (`oto_admin_key_grant`).

## Auth — Logto

JWT Logto **ES384** (défaut RS256 = tout rejeté), discovery RFC 9728 sur 401,
façade DCR self-service (`oauth_facade.py`) pour les clients sans DCR (Claude/ChatGPT/
Mistral). **Détail : `docs/auth-logto.md`** (gotchas, env, onboarding).

> **Coexistence multi-domaine (2026-07-02)** : `https://mcp.oto.cx/mcp` sert le MCP
> en plus de `mcp.oto.ninja` — env **`MCP_AUDIENCE_ALT`** (liste d'audiences
> canoniques secondaires, vide = no-op), resource Logto dédiée, PRM Host-aware
> (`config.mcp_audience_alt_hosts`). Le 401 `WWW-Authenticate` pointe la PRM
> canonique .ninja (fastmcp `base_url`, non Host-aware) — fonctionne car l'audience
> canonique est acceptée sur .cx. DNS mcp.oto.cx = grey+ACME direct box.

## Rôles + résolution de clé API

3 paliers `member < admin < super_admin` (accès admin UI). Résolution de clé par
appel : `clé membre (sub, org) > group_secret > org_secret > platform_grant` (chemin
platform gaté sur `auth_modes`). **Détail : `docs/roles-and-resolution.md`** (paliers,
grants/quota, platform keys, providers byo-only).

> **Scope MEMBRE (ADR 0033, 2026-07-02).** Plus de credential per-user org-agnostique :
> la clé BYO d'un membre est keyée **(sub, org)** — coffre `entity_type='member'`,
> `entity_id="{org}:{sub}"` (AAD dérivé → liée crypto à son org). Posée dans l'org A,
> elle ne résout PAS depuis l'org B (fini « ta clé perso te suit partout et écrase la
> clé d'org »). L'org de scope = seam `current_org` (0023), à la pose (`/api/settings/
> api-keys`, sessions browser) comme à la résolution ; helpers `db.{get,has,set,clear}_
> member_api_key(sub, org_id, provider)` — la couche db ne lit JAMAIS `current_org`
> elle-même. Valeurs de contrat inchangées (`mode="user"`, `user_key_configured`) —
> seule la sémantique change. **B3 (google)** : comptes Google multi-comptes scopés
> (sub, org) — `db/google.py` en entité member, l'org du DÉMARRAGE du flow OAuth voyage
> dans le **state HMAC** jusqu'au callback (qui vient de Google, sans headers de
> consultation). **B4 (unipile)** : `unipile_accounts` au grain **(sub, org_id, provider)**
> — `org_id` = org de CONTEXTE du binding (la facturation des sièges plateforme a sa
> colonne `platform_seat` ; les BYO ne comptent pas dans le plafond) ; migration PK
> one-shot `db.backfill_unipile_member_scope()` (⚠️ le cycle de vie du PK lui appartient,
> pas à `_init.py`). **Seuls les mounts oauth fédérés** (memento/atlassian/folkmcp)
> restent scope `('user', sub)` ; tripwire `test_member_credential_scope.py` interdit
> toute autre écriture scope user. Migration coffre = `credentials_store.
> backfill_member_scope()` au boot (re-chiffrement — l'AAD change, pas d'UPDATE ;
> destination = org maison ; ligne indéchiffrable laissée inerte).

**Seam substrat (ADR 0024)** : `access.resolve_credential(provider, want, sub?)` marche la cascade UNE fois → `ResolvedCredential{key, is_platform, mode, config, fields}` ; `resolve_api_key`/`resolve_credential_fields` = vues minces dessus (les ~15 tools keyed inchangés). `config` = **config non-secrète appariée à la clé gagnante** (endpoint/host : `dsn` unipile, `base_url` n8n/make, `data_center` zoho — `config_fields` `secret=False` ∪ meta public) → ne JAMAIS recâbler un résolveur d'endpoint par-connecteur. `access.credential_mode_for(sub, provider)` = le `mode` sans déchiffrer (détection BYO = `mode ∈ {user,group,org}`, jamais un check user-only).

## REST API (consommée par le dashboard / oto.ninja)

Endpoints `/api/*` (compte, settings, orgs, admin, datastore…), même
`JWTVerifier` que `/mcp`. **Inventaire : `docs/rest-api.md`**.

## Browser automation — substrat HÉBERGÉ Browserbase (ADR 0026)

⚠️ **Plus de browser in-process sur la box, plus de délégation à un conteneur
o-browser-full** (`OBROWSER_URL`/`RemoteBrowser` = 0 référence — jamais portée). Le
harnais browser server-side = **service navigateur HÉBERGÉ Browserbase**
(`oto_mcp/browserbase.py`, seam à sens unique ADR 0004), réutilisable par tout
connecteur d'**API privée cookie-bound**. État réel (2026-06-24) :

- **LinkedIn** : `tools/linkedin.py` **supprimé** — passe par **Unipile** (hébergé,
  connecteur `unipile`). Le browser LinkedIn local ne survit que dans oto-cli
  (fallback) et oto-core (`oto.tools.browser.linkedin/`).
- **Substrat `browserbase.py`** : Chrome HÉBERGÉ (off-box → anti-OOM) + **Context**
  per-user (profil persistant = la session loguée, = le credential coffre) + **Live
  View** pour le login interactif (l'user gère SSO/captcha/2FA — pas d'export de
  cookie, la session naît native, zéro `li_at` kill). Exécution = `run_fetch(ctx,
  method, path, body, *, base, app)` : ouvre une session éphémère sur le Context, charge
  une page `app` puis exécute un `fetch(base+path)` **same-origin** (la session vivante
  porte les cookies). `base`/`app` sont **propres au connecteur** (le substrat n'en
  hardcode aucun). Creds plateforme env `BROWSERBASE_API_KEY`/`BROWSERBASE_PROJECT_ID`.
- **Connexion = depuis le DASHBOARD** (voie produit, pas MCP) : bouton « Connecter »
  → Live View Browserbase **en iframe** ; l'user se logue ; « vérifier » persiste le
  Context. Servie en REST `POST /api/me/connectors/{name}/session/{start,finalize}`
  ET en MCP (`<name>_connect_start`/`_connect_status`) par **un seul corps de logique**
  (`browser_session.py`, seam : `start()` générique + `finalize()` avec **verify
  par-connecteur enregistré** — brevo=cookie `auth`, crunchbase=sonde API ; les tools
  MCP ne sont que de minces délégations). ⚠️ **Un connecteur browser-session s'enregistre
  avec une `login_url`** (`browser_session.register(name, verify, login_url=…)`) : `start()`
  amène la session **sur cette page** avant d'afficher la Live View (best-effort). Sans elle,
  la Live View reste sur `about:blank` (l'user ne sait pas où se loguer — vécu pennylaneged
  2026-07-01). **Sécu** : la session émise est liée au `sub`
  (`_PENDING`, anti-IDOR — `finalize` refuse un Context tiers) et **aucune exception
  brute n'est renvoyée** (l'URL CDP porte `?apiKey=…` → loggué, message propre). L'état
  (`configured` + `session_set_at`) sort dans `me.providers[name]` via `status_for`
  (les connecteurs `secret_kind="cookie"`) — ADR 0026 avait retiré `me.crunchbase` sans
  jamais câbler ce relais (UX cassée bout-en-bout, corrigé 2026-06-30). Déconnexion =
  DELETE générique `/api/settings/api-keys/{name}` (byo_user, plus de route dédiée).
- **Connecteurs sur le substrat** (tous deux : Live View ci-dessus, Context au coffre,
  family dérivée=`api`, plus aucun browser local) :
  - **`brevo`** (`tools/brevo.py`) — automations marketing via l'API privée
    `workflow-apis.brevo.com/v1` (cookie `auth` httpOnly). **Prouvé 200** le 2026-06-24.
  - **`crunchbase`** (`tools/crunchbase.py`) — fiches société/personne via l'API privée
    du frontend `www.crunchbase.com/v4/data` (schéma v4 sans `user_key` ; lookup
    `entities/organizations|people/{slug}` + cards `founders`/`raised_funding_rounds`,
    recherche via `autocompletes`). **Migré du scraping DOM in-process** (ADR 0026,
    `BROWSER_PROVIDERS` désormais vide, plus de `CrunchbaseClient` o-browser ni de Chrome
    sur la box). ⚠️ **Reste à smoke en live** (Browserbase + login crunchbase réel) :
    confirmer `field_ids`/`card_ids`/`collection_ids` et l'absence de header anti-CSRF.
  - **`pennylaneged`** (`tools/pennylaneged.py`, issue otomata-private#31) — GED (DMS)
    Pennylane via l'API interne de la SPA (`/companies/{cid}/dms/…`, CSRF tournant lu
    in-page à chaque appel via `browserbase.run_page_eval` — l'eval générique, `run_fetch`
    ne suffit pas). Upload = control plane seul (URL S3 présignée), les octets PUT **en
    local** (RGPD). **GED cible (une par client)** : `company_id` optionnel sur tous les
    tools, défaut = la société choisie via le **sélecteur d'identité générique** (ADR
    0024) — backend enregistré par `connector_identities.register()` (patron
    `browser_session`), identités = les sociétés du cabinet (`/crm/flow_companies`),
    sélection validée anti-binding (tree 200 sur LA session) puis mémorisée au `meta` du
    credential (`default_identity_id`/`default_identity_label`, exposés par `status_for`
    → picker de la carte dashboard sans louer de session ; `identities` au catalogue).
    ⚠️ **Reste à smoker en live** (login Pennylane réel + forme exacte de
    `flow_companies`).
- **Leçons empiriques (toujours valides)** : (1) un `httpx`/curl brut est **rejeté
  (403)** — transport obligatoirement **browser-driven** (`page.evaluate(fetch())`) ;
  (2) une session **ne se transplante pas** par export de cookie (le faux négatif « auth
  cookie missing » venait d'une extraction sur **profil déconnecté**) → login-en-place
  via Live View ; (3) capter/vérifier sur une session **vivante** (fetch sanity = 200).

## LinkedIn cookies

⚠️ **Isolation de session (constaté 2026-06-04, issue #5 ouverte)** : injecter le
cookie `li_at` d'un user **côté serveur** (IP datacenter ≠ son IP) **déconnecte sa
propre session LinkedIn** (LinkedIn invalide/rotate le `li_at` partagé). Le vrai
Chrome règle l'empreinte TLS mais PAS ce partage de session. → l'outreach par un
user réel doit passer par une **session dédiée** (profil/VNC côté serveur, ou CLI
local sur son device), pas par son cookie injecté côté serveur. ✅ Le scraping
serveur est désormais **profil-only** (fallback cookie **supprimé**) et délégué au
conteneur (voir §Browser automation). #5 reste ouvert pour le pairing/CLI local.

Le couple `(li_at, user_agent)` est stocké par `sub` en PG. Le UA
matche le browser d'origine (capturé via `navigator.userAgent` au moment du
save) — sinon LinkedIn flag rapidement les sessions cookie/UA mismatch.

Si le user n'a rien configuré, les tools `linkedin_*` lèvent une `McpError`
qui pointe vers `https://app.oto.ninja/`.

Pour les non-tech : extension Chrome Oto Companion (repo `oto-app/extension/`,
MV3) qui capture le couple `(li_at, user_agent)` et le push automatiquement
via `POST /api/settings/linkedin` (auth Logto PKCE). Auto-resync via
`chrome.cookies.onChanged` quand LinkedIn rotate la session.

## SIRENE stock (DuckDB sur parquet INSEE — lu depuis S3/httpfs)

Stock complet (~43M établissements, parquet ~2GB) interrogé via DuckDB :
- **Source = Object Storage** (ADR 0002 résolu 2026-06-22) : la box dédiée n'est PAS
  co-localisée avec le parquet → `SIRENE_STOCK_PARQUET_PATH=s3://oto-media/sirene/StockEtablissement.parquet`,
  lu en **httpfs** (range reads, pruning de row groups). Creds DuckDB via env
  `SIRENE_STOCK_S3_{ENDPOINT,REGION,KEY_ID,SECRET,URL_STYLE}` (url_style=`path` pour
  Scaleway — `vhost` 3× plus lent). Le module accepte aussi un chemin local ou une URL
  `https://` publique. **Perfs box (2 vCPU)** : lookup point ~2s, scan filtré ~20-30s.
  ⚠️ Pour CHERCHER des boîtes (secteur/zone/taille), préférer **`fr_search`**
  (API recherche-entreprises indexée, <1s, filtre `categorie_entreprise` PME/ETI/GE) ;
  le parquet = lookups ponctuels + **bulk** (cf. ci-dessous) + énumération exhaustive >10k.
- Refresh : data.gouv republie mensuellement (URL datée → `deploy/refresh_sirene_stock_s3.sh`
  résout l'URL via l'API data.gouv puis push S3, à lancer sur otomata-0 ; **cron non installé** —
  le parquet bouge lentement, refresh manuel quand ça compte).
- Query layer : `france_opendata.sirene_stock` (lib PyPI `france-opendata[stock]`, **>=0.11** = support s3:///httpfs).
- MCP tools `fr_stock_*` (ex-`sirene_stock_*`, fusionnés dans le connecteur `sirene` le 2026-06-22 — même domaine entreprises FR, namespace `fr`) : **`fr_stock_enrich(sirens=[...])`** (bulk — sièges d'une LISTE en UN scan), `fr_stock_siege`, `fr_stock_etablissements`, `fr_stock_siret`, `fr_stock_search` (`sieges_only=True` = siège strict). Pendant parquet des `fr_*` live.
- REST `/api/sirene/{headquarters(POST,batch),siege,etablissements,siret,search,info}` (noms de routes **inchangés** — `oto-cli`/`oto-core` en dépendent ; orthogonaux aux noms MCP).
- Consommé par `oto-cli` (`SireneStock` HTTP client, oto-core >=1.8 — `get_headquarters_addresses` = 1 POST batch, plus N appels) — voir ADR 0001 + 0002 dans le privé `otomata-private`.

## Datastore (spine natif PG, ADR 0016)

Spine plateforme de stockage structuré (PG/JSONB natif, plus Google
Sheets). Surfaces : tools `data_*` (MCP) + REST `/api/datastore/*` ; OAuth Google
per-user (Gmail/Tasks, multi-compte) câblé ici. **Détail : `docs/datastore.md`**
(surfaces, OAuth multi-compte + scopes restricted/CASA, setup GCP, env vars).

## Propriété de ressource — primitive `ownership` (ADR 0030)

Le datastore n'est **plus scopé par `sub`** : il est le **pilote** de la primitive
d'ownership générique. `ownership.py` est le **seam unique** : une ressource
`(resource_type, resource_id)` est possédée par `(owner_type∈{user,group,org},
owner_id)` (colonnes sur la ressource — pour le datastore : `user_datastores.owner_*`,
`resource_id = id::text`, **stable au renommage**) ; le partage cross-type vit dans
**`resource_grants`** (deny-by-default, remplace `datastore_shares`). Deux plans, jamais
confondus : **`can_access`** (CONTENU = owner-match ∪ grant ; *privacy by default* — pas
d'escalade admin sur du perso) et **`can_govern`** (GOUVERNANCE = owner ∪ escalade
`roles.py` : transférer/lister/partager **sans lire**). La lecture opérateur du contenu
perso reste le **view-as audité** (ADR 0023). `DatastorePg._resolve` passe par
`can_access` ; le share/transfert/delete par `can_govern` (un super_admin/org_admin
gouverne donc un datastore tiers). ⚠️ **Scoping des LISTES de contenu** : une liste de
ressources possédées (datastore `list_namespaces`, projets `op=list`) scope sur
**`ownership.active_owner(current_org)`** (= l'org active, le pendant `ownership` de
`current_org`/ADR 0023), **JAMAIS** sur `accessor_scope().owner_pairs()` (= union de
TOUTES les orgs de l'acteur, réservé au plan **gouvernance** `oto_resource list` +
découverte/modèles). Les confondre = fuite cross-org *fail-open* (le superset montre
plus que le contexte chargé) — vécu 2026-06-30 (projets/datastore d'une autre org
visibles dans le dashboard). Garde-fou : `tests/test_owner_scope_tripwire.py` fige les
call-sites `owner_pairs()`. **org-owned activé** : `data_create_namespace` /
`POST /api/datastore/namespaces` acceptent un `owner` (classeur d'équipe). Capacité
générique **`oto_resource`** (`capabilities/resources.py`, op `list/get/transfer/share/
unshare`, autz combinateur `RESOURCE_GOVERN`) = chemin de gouvernance MCP+REST + alimente
l'object-browser admin. Catalogue du registre : **`GET /api/admin/capabilities`**
(`capabilities_catalog.py`, `PLATFORM_ADMIN`, JSON Schema dérivé des Input pydantic) →
UI admin **dérivée**. ⚠️ **Migration en cours** : `user_datastores.sub` + colonnes Sheets
sont des reliques nullable, **DROP différé** (Phase H) après cutover prod vérifié.

> **Suppression du « perso » (2026-06-30, amende ADR 0015/0023/0030).** Plus d'état
> **org-less** (`org_id=0` / `current_org`=None) : **tout user est TOUJOURS dans une org**.
> Chaque user a une **org perso dédiée** (`orgs.personal_of=sub`, privée mono-membre) —
> `org_store.ensure_personal_org` (créée au 1er insert d'`upsert_user` + au boot par
> `backfill_personal_orgs`, **reclaim sûr** : ne marque une org existante comme perso que
> si c'est la SEULE org du user, créée par lui ; sinon org fraîche → multi-org intact, zéro
> fuite). Les ressources `owner_type='user'` ont **migré** vers l'org perso ; les **défauts
> de création** (datastore/projet) vont dans l'**org active** (`current_org`, toujours posé).
> Plus de retour-perso (`clear_active_org` retiré ; `oto_clear_org` REST → org perso, MCP →
> maison). Filets gardés : `ownership` accepte encore `owner_type='user'` **en lecture**
> (reliquat) ; `session_visibility` `prof_org = active_org or 0` (défensif). `org_id=0`
> purgé des profils de visibilité.

## Projet — couche d'organisation (ADR 0030, modèle produit 2026-06-27)

Conteneur de travail **possédé** (owned resource ADR 0030) : un but + ses entités. Tables
`projects` (owner_type/owner_id, `brief_md` = doc d'entrée, soft-delete `archived_at`),
`project_links` (pointeur typé `target_type∈{tableau,procedure,connecteur,base}` + `target_ref`
+ label, pas de FK cross-store), `docs` (pages markdown **en arbre** `parent_id`, héritent de
l'accès du projet — pas d'ownership propre), `project_activity` (journal best-effort).
Capacités co-déclarées : **`oto_project`** (`capabilities/projects.py`, op create/list/get/
update/archive/link/unlink/activity, `POST /api/me/projects`), **`oto_doc`** (`capabilities/
docs.py`, op create/list/get/update/delete/move, `POST /api/me/docs`). Partage/transfert via
**`oto_resource`** (resource_type=`project` ajouté au dispatch `_OPS`).

> **Livraison d'un projet COMPLET vers l'org d'un client (otomata-private#52).**
> `oto_resource` : share/unshare acceptent un principal **org** (`org_id`, sans exigence
> d'appartenance — on donne un accès) ; **`cascade=true`** sur share/transfer d'un projet
> répercute le geste sur les `project_links` avec rapport par entité — **tableau** = même
> geste (grant/transfert du namespace), **procédure** = grant `read` au partage (modèle
> licence : oto garde le master) / **copie chez la cible + re-pointage du lien** au
> transfert (`org_store.copy_instruction_to_org`, l'originale intacte), **connecteur** =
> `recipient_credential` (le client branche SA clé ; la surcharge identité/instructions du
> lien voyage avec le projet) ; docs/fichiers suivent d'office (héritage d'accès). Kind
> **`doctrine`** enregistré sur la primitive ownership (owner **dérivé** d'`org_instructions.
> org_id`, resource_id = id surrogate) → lecture cross-org **par id** `oto_get_doctrine(
> doctrine_id=…)` / `GET /api/me/doctrines/{id}`, gatée `ownership.can_access`. Un projet
> livré remonte chez le client dans `oto_project(op=list)` (flag `shared`+`permission`) ET
> dans le bloc C du handshake (#50) — ouvrable en un message. Reste à cadrer : push des màj
> post-livraison (re-share = re-grant idempotent, mais pas de notification). UI : `oto-dashboard`
`/projects` + page dédiée `/projects/:id` (`ProjectDetailView`, ADR 0030). Reliquats du modèle
(MCP-App rendu, édition temps réel/lock, pré-set vendable=copie) **non faits**.

> **Partage public CHIFFRÉ d'un projet (ADR 0032 §3, zero-knowledge).** Un projet peut être
> publié en lecture seule derrière un lien, avec le contenu **chiffré côté navigateur**
> (AES-256-GCM, `oto-dashboard` `lib/crypto.ts`). Le backend ne stocke QUE le ciphertext
> (`project_public_shares`, une part par projet, token opaque) — la clé vit dans le **fragment**
> de l'URL (`/p/p/<token>#<clé>`) et n'atteint jamais le serveur → la plateforme ne peut pas lire
> un projet partagé. **REST-only** (le ciphertext vient du front, l'agent MCP ne peut pas chiffrer
> dans le navigateur → pas de binding MCP, esprit « secret jamais en argument MCP ») :
> `POST|DELETE /api/me/projects/{id}/public-share` (autz `can_access write`) + lecture publique
> sans auth `GET /api/public/projects/{token}`. Re-publier fait **tourner** le token+la clé
> (ancien lien caduc). `oto_project(op=get)` expose `public_shared`/`public_shared_at` (présence
> seule, jamais la clé). Pendant du partage public de doc rendu (#4a) mais **chiffré**.

> **Endpoint MCP par projet — `<slug>.mcp.oto.cx` (ADR 0032, amende #44).** Un projet
> se **publie** comme serveur MCP dédié sur son propre sous-domaine (le « preset » de
> l'ADR 0032 §7). Colonnes `projects.mcp_slug`/`mcp_access`(`off|anonymous|org`)/`mcp_tools[]` ;
> capacité `oto_project` op **`publish_mcp`/`unpublish_mcp`** (autz `can_govern`) ; **garde de
> publication** : un preset `anonymous` n'accepte que des tools **credential-less** (`secret_kind=none`)
> ou dont le connecteur a une clé résoluble pour l'org propriétaire. **Deux modes** :
> - **`anonymous`** (sans login, contourne 100 % du blocage Logto #44) : allowlist **figée = `mcp_tools`**
>   (fail-closed, aucun autre tool visible), credential résolu via l'**org propriétaire** du projet
>   (`access.current_org(None)`→org du projet, `_resolve_credential_anon` : org_secret > grant > clé
>   plateforme, **sans quota**), rate-limité (token-bucket in-memory par IP+projet).
> - **`org`** : JWT Logto, **épingle l'org** ; le sous-domaine est enregistré comme **resource Logto**
>   (`oauth_facade.ensure_api_resource`) + verifier **multi-audience** + PRM **host-aware**.
>
> **Host-routing** (`subdomain_project.HostDispatch`, monté `root_app` dans `server.main`) : une **2ᵉ app
> FastMCP sans auth** (`anon_mcp = mcp`, **réutilise l'instance no-auth module-level** — ne PAS en
> re-build une 3ᵉ, doublait register_all/mounts/init_db → boot timeout) sert les sous-domaines anonymes ;
> tout le reste → app authentifiée **inchangée**. **Même URL, 2 publics** : navigateur (`GET`+`Accept:
> text/html`) → **landing HTML** rendue **live depuis la ligne projet** (`anon_landing.render`, name/brief_md/
> mcp_tools) ; Claude/Mistral (`POST`) → MCP (rewrite path `/`→`/mcp`, `_root_to_mcp` — Claude tape la racine).
> Fichiers : `subdomain_project.py` (routing + rate-limit + `/api/mcp/tls-check` + `/api/public/mcp-projects`),
> `anon_visibility.py` (allowlist fail-closed), `anon_oauth.py` (shim OAuth **auto-approve**, `.well-known/*`
> + `/register` + `/authorize`→302 sans login + `/token`→`anon-…`), `anon_landing.py` (HTML charté).
> **Infra** : **wildcard** `*.mcp.oto.cx` (CF-proxied) + Caddy **on-demand TLS** gaté par `/api/mcp/tls-check`
> (200 uniquement pour un slug **publié** → borne l'émission de certs). `publish_mcp` est la **seule** action
> par projet — **zéro DNS** à chaque publication. **Surface web** : annuaire public **oto.ninja/apps**
> (`web/AppsView.vue`) via `GET /api/public/mcp-projects` (CORS `*`, liste les projets `anonymous` publiés).

## WhatsApp / Telegram / Instagram (messagerie via Unipile)

Tools `whatsapp_*` / `telegram_*` / `instagram_*` (`list_chats`/`read_chat`/
`send_message`) = messagerie **hébergée Unipile**, sous le connecteur `unipile`
(`modules`/namespaces = `unipile, whatsapp, telegram, instagram`). Générés par la
factory `tools/unipile.register_messaging_tools(mcp, channel)` — l'API `/chats`
d'Unipile est channel-agnostic ; chaque tool résout l'`account_id` du canal pour le
user (no-fallback, `tools/unipile.unipile_client(provider)`).

Connexion = hosted-auth Unipile (dashboard, `?channel=whatsapp|telegram|instagram`),
`account_id` per-membre dans `unipile_accounts` (PK `(sub, org_id, provider)` — scope
membre ADR 0033 B4 : le binding vaut dans l'org de contexte, un canal se connecte par
org). Même gate d'option par org que LinkedIn (comp admin `access.has_option` ; plus
de paiement).

> **Baileys archivé** (ex-WhatsApp self-hosted) : wrappers backend retirés
> (`tools/whatsapp.py` réécrit Unipile, `pairing.py` + routes `/api/whatsapp/pair/*`
> supprimés). L'engine Baileys survit dans **oto-core** (`oto/tools/whatsapp/` + Node)
> + la **CLI `oto whatsapp`** (fallback).

> **Mode plateforme unipile** (revente) : `auth_modes` inclut `platform` → la clé
> Unipile se partage en **clé plateforme + grant** (pas de copie par org) ;
> `access.unipile_api_key_for` a le fallback platform-grant. Le gate d'option reste
> par org (un grant donne la clé, ne débloque pas l'option). **Débloquer l'option
> = comp** : `db.set_option_comp("org", id, "unipile")` (débloque `access.has_option`).
> ⚠️ Les deux couches (clé=2, option=3) sont **orthogonales en base** mais l'**action
> admin les compose** (`capabilities/users_admin._set_option`) : `oto_admin_set_option`
> `on=true` sur un connecteur en mode plateforme **grant aussi la clé plateforme** (sinon
> `has_option`=true mais aucune clé → 404 au `/connect`, bouton « Connecter » inerte = état
> mort), `on=false` la révoque ; le champ `platform_key` du retour rend l'effet explicite
> (`granted`/`no_platform_key`/`byo_inert`/`revoked`). N'applique PAS à un connecteur keyed
> sans option (serpapi…) : lui se grant via la fiche admin (bouton « grant key » par
> provider, auto-résout la clé unique) ou `oto_admin_key_grant` (par `key_id`).

> **DSN par credential + sélecteur d'identité (ADR 0024).** Chaque clé Unipile est liée
> à SON sous-domaine `api<NN>.unipile.com:port` ; le DSN vit dans le `meta` du credential
> et voyage avec la clé via `resolve_credential` (défaut env `UNIPILE_DSN`=api25, instance
> plateforme). Une clé BYO porte N comptes → capacités génériques **`connectors.identities`/
> `set_default_identity`** (REST `/api/connectors/{c}/identities[/default]`, registre
> `connector_identities.py` ; unipile = `list_accounts` sur clé+DSN, **valide id∈liste**
> anti-binding, **BYO-only** — en revente la liste est vide, hosted-auth conservé). Vue admin
> **sièges clé plateforme** `GET /api/admin/unipile/seats` (super_admin, `db.unipile_account_owners`) :
> réconcilie les comptes de l'instance partagée ↔ leur owner oto (flag **orphelin**).

> **Compte partagé autorisé (otomata-private#55).** Le **propriétaire** d'un compte
> Unipile accorde à un **membre nommé** (d'une org commune, anti-IDOR `users_share_org`)
> le droit d'**opérer son compte** sur un canal — la **SEULE exception** au no-fallback
> anti-usurpation (#5). Table `connector_account_grants` (PK `(owner_sub, provider,
> grantee_sub)`, patron ADR 0025, `granted_by`/`granted_at` ; l'`account_id` stocké =
> snapshot d'audit, la résolution relit le handle **LIVE** → owner déconnecté = grant
> inerte). Le grantee bascule via le **sélecteur d'identité** (le compte accordé
> apparaît « compte de X » ; le select pose le **pointeur** `unipile_operated_accounts`,
> il n'écrase JAMAIS sa ligne `unipile_accounts`) ou un **pin projet** (garde étendue
> aux comptes accordés). Résolution : `connector_identities.resolve_operated_account_id`
> — pointeur **revalidé contre les grants vivants À CHAQUE appel** (révocation =
> effet immédiat) ; pointeur révoqué = **erreur explicite, jamais de repli** sur le
> compte propre. Capacité `capabilities/connectors_account_grants.py`
> (`oto_{list,grant,revoke}_account_*`, REST `/api/me/connector-accounts/*` ; autz
> `SUB_ONLY`, owner := ctx.sub par construction — pas d'escalade org_admin). ⚠️ La clé
> du grantee doit joindre le compte (clé partagée org/plateforme OK ; owner sur une clé
> BYO perso ≠ celle du grantee → 404 Unipile surfacé).

## Monitoring des appels MCP

`ToolCallLogger` (lib otomata-calllog) journalise chaque appel dans `tool_calls`
(`db.insert_tool_call`, best-effort, identité = `sub` du JWT via
`current_user_sub_from_token`) ; surface admin `/api/admin/monitoring/{summary,calls}`.
**Détail : `docs/monitoring.md`**. ⚠️ **Ne trace QUE les invocations d'outils MCP** —
pas la connexion du connecteur, pas le `tools/list`, pas les appels REST/dashboard.
Donc **compte actif ≠ usage** : un user qui a un compte (table `users`) mais 0 ligne
`tool_calls` n'a jamais déclenché d'outil (connecté-mais-idle, OU handshake OAuth du
connecteur jamais réussi → diagnostiquer via `journalctl` 401). Vécu 2026-06-22 (JB,
Julien : comptes actifs, 0 appel ; le monitoring marchait, eux n'avaient rien invoqué).

## Error tracking (Sentry)

Exceptions backend → **Sentry SaaS** (gaté `OTO_SENTRY_DSN`, no-op si absent →
le serveur boote sans). Deux captures : **500 des routes REST `/api/*`** via
l'intégration Starlette (auto) ; **exceptions des tools MCP** via
`SentryToolErrorMiddleware` (`sentry_setup.py`) — une erreur de tool est une erreur
JSON-RPC en **HTTP 200**, invisible à l'intégration Starlette, donc capturée là où
l'exception est vivante (vrai traceback, tag `mcp.tool` + `user.id=sub`). RGPD :
`send_default_pii=False`, **jamais** les args d'appel dans l'event. `before_send`
**droppe les 4xx amont** (`HTTP 4xx` d'une API tierce = input rejeté, pas un bug
backend). Env box : `OTO_SENTRY_{DSN,ENV,RELEASE,TRACES_SAMPLE_RATE}` ; région **EU**
`de.sentry.io` (org slug `otomata-vz`). Surveillance/triage = doctrine oto
`surveillance-erreurs` (token API en SOPS `sentry_api_token`).

## Onboarding = un projet « Découverte » (ADR 0032 §7)

**Plus de mode d'accueil spécial** (retiré le 2026-07-01) : pas de booléen `onboarded`,
pas de checklist dashboard, pas de tool d'onboarding scripté. L'onboarding est **un
projet** comme un autre — un projet « Découverte » porteur d'un brief d'accueil, **semé
à la création de l'org perso** (`discovery.seed_for_org`, appelé par
`org_store.ensure_personal_org` sur la branche création, best-effort). Il remonte à
l'agent via la ligne « Projets récents » du bloc C des instructions (`instructions.py`) ;
l'agent l'ouvre (`oto_use_project`) et déroule l'accueil depuis son brief.

**La fiche « situation avec oto » reste** (qui est l'user, son métier, ses objectifs, son
CRM, les connecteurs voulus, son ton) — découplée de l'accueil, c'est un data model libre
relu à chaque session :
- `tools/profile.py` expose `oto_profile(op="get"|"update", fields=…)` (spine, hors gate,
  **toujours visible** via `PROTECTED_TOOLS`) — l'agent l'entretient au fil de l'eau.
- DB : table `user_account_profile(sub PK, profile jsonb, created_at, updated_at)`
  (`db.get_account_profile` / `db.update_account_profile`). **Injectée au handshake**
  (bloc C, section « Ce que tu sais de l'utilisateur ») → enfin utilisée, plus seulement
  collectée. N'est plus exposée sur `/api/me` (le bloc `onboarding` a été retiré).

`tools/whoami.py` (spine, chargé explicitement dans `register_all`, hors gate
d'activation, **toujours visible** via `PROTECTED_TOOLS`) expose `oto_whoami()`
(lecture) — l'**identité MCP courante** sous laquelle Claude agit : compte (`sub` +
email + rôle plateforme) × **org active** (id/name/rôle) × **groupe actif**, plus un
résumé des connecteurs configurés et l'état Memento. C'est le pendant agent du badge
« identité MCP » du dashboard ; à appeler pour confirmer le contexte avant une action
sensible. Pour basculer : `oto_use_org`.

## Boucle d'usage (ADR 0017)

Flux d'événements de session unifié : calllog (involontaire) + feedback volontaire
d'agent (`feedback`, signal=tool_feedback|gap) + runs / déroulés (`run_start/finish`,
`doctrine` optionnel → doctrine nommée ou run one-shot). **Détail : `docs/usage-loop.md`**.

> **Runs persistés (#50, amende le « state-only » d'ADR 0017).** La métadonnée
> sémantique d'un run (label / doctrine / outcome) vit désormais dans la table `runs`
> (`db.insert_run`/`finish_run`/`recent_runs`) — la pile session-scopée de
> `doctrine_run.py` reste la **source du run actif** (stampe `tool_calls.run_id`),
> `run_start`/`run_finish` y ajoutent la trace durable (best-effort, off-loop). Sert
> l'anticipation du contexte injecté (instructions bloc C) + la boucle d'usage dashboard.

## Email (envoi per-org, PAR CONNECTEUR)

Envoi d'email modélisé **par connecteur** (la config/gestion email s'exprime comme
celle d'un connecteur, pas une page à part). **Deux connecteurs** (`providers.py`) :
`scaleway` (**BYO-org depuis le 2026-07-01** : `auth_modes={byo_org}`,
`secret_kind="fields"` — `secret_key`+`project_id`+`region` du compte Scaleway TEM
de L'ORG ; transport = API TEM en direct `email.send_via_scaleway_tem`, plus de
service mailer ni de clé plateforme ; master ON **sûr** car la propriété du domaine
est garantie PAR Scaleway — l'API refuse un `from` dont le domaine n'est pas vérifié
dans le compte de l'org, ce qui rend #64 caduque) + `resend` (BYOK,
`auth_modes={byo_org}`). **Le transport DÉRIVE du connecteur** :
`providers.EMAIL_CONNECTOR_TRANSPORT={scaleway:scaleway, resend:resend}` (pas de
champ transport sur l'expéditeur).

- `email_send` (`tools/email.py`) = **spine** (pas un connecteur) : route
  `sender→connecteur→transport` ; autz dynamique (membre d'org pour une adresse
  déclarée ; super_admin pour le repli marque `oto@otomata.tech`). `email.py` =
  `send_composed_email` (mailer.oto.zone, env `OTO_MAILER_SEND_BEARER`) +
  `send_via_resend` (httpx direct, clé org). `scaleway`/`resend` = providers
  credential/config-only (`tools/{scaleway,resend}.py` = `register()` no-op).
- **Config = `orgs.email_settings` JSONB keyé PAR CONNECTEUR** :
  `{<connector>:{senders:[{email,name?,reply_to?}], quiet_hours?}}` (calqué sur
  `field_filters`). `org_store.get/set_org_email_settings(org, connector)`,
  `resolve_sender(org, from)→(sender, connector)`, `org_email_quiet_hours`. Capacité
  `orgs_email_settings` : GET bundle + `PUT /api/orgs/{id}/email-settings/{connector}`.
- **Envoi différé** : params `send_at`/`force_now` + garde-fou **quiet hours par
  connecteur** (défaut Europe/Paris 20h–8h). `scheduler.py` : `compute_scheduled_at`
  (pure, testée) + boucle asyncio démarrée via le lifespan (`server.py`), batch isolé
  en `asyncio.to_thread` (ne bloque pas l'event loop) ; table `scheduled_emails`
  (claim `FOR UPDATE SKIP LOCKED`, retry ×3). Gestion : `oto_list/cancel_scheduled_emails`.
- **Vérif de domaine d'envoi = déléguée au provider** (les deux connecteurs sont
  BYO) : Scaleway TEM comme Resend refusent un `from` hors domaine vérifié dans le
  compte de l'org → pas de vérif côté oto (#64 sans objet depuis le passage BYO).
  Otomata (org 2) envoie avec sa clé TEM dédiée (app IAM `oto-email-scaleway`,
  vault `SCW_TEM_*`).

> **Invariant connecteurs (corrigé 2026-06-24)** : `_org_list` (vue ORG
> `/org/connectors`) ne liste QUE les connecteurs **activés par la plateforme**
> (master ON, ou forcé par l'override d'org), comme la surface USER
> (`_visible_catalog`). Master-OFF non accordé → invisible (fin du levier inerte
> « coupé par la plateforme »). Filtre sur le **cap master**, pas sur `effective`
> (un override OFF d'org doit rester réactivable).

## Visibility per-user

`UserDisabledToolsMiddleware` (`middleware.py`) applique au handshake `initialize` les visibility rules natives fastmcp (`disable_components` via `_visibility_rules` session state). Plus de filtrage manuel `on_list_tools`/`on_call_tool` — fastmcp émet `tools/list_changed` automatiquement quand les rules changent. Le **calcul** de la denylist `(sub, org active)` + son application vivent dans **`session_visibility.py`** (`compute_hidden_tools` / `apply_session_visibility(ctx, sub, *, reset=…)`), partagés entre le middleware (handshake) et le **refresh à chaud** post-bascule.

Source de vérité = tables PG `user_disabled_tools(sub, tool_name)` (négatif) + `user_enabled_tools(sub, tool_name)` (override positif). Table sœur `user_presets(sub, name, enabled_tools[])` pour les snapshots nommés.

**Masqués par défaut** (`is_default_hidden`) : invisibles par défaut sur la surface authentifiée, **self-activables**. Deux grains : `tool_visibility.py::DEFAULT_HIDDEN_TOOLS` (noms individuels) et `DEFAULT_HIDDEN_NAMESPACES` (namespaces entiers, **dérivé du registre** — champ `default_hidden` de `connectors.py`). Cas actuel : **`attio_*`** (le MCP Attio officiel est préféré ; code conservé pour implems custom). Règle effective (`is_tool_visible`) : override positif prime > désactivé > masqué-par-défaut > visible. `oto_enable_tool` pose l'override, `oto_disable_tool` le lève, `apply_preset` le réplique (même logique côté REST `/api/me/tools/{name}`). **Stdio local (sub=None) = accès complet**, le masquage ne vise que le multi-user. Masquer un connecteur entier = poser `default_hidden=True` au registre ; un tool isolé = `DEFAULT_HIDDEN_TOOLS`.

Méta-tools exposés (`tools/meta.py`) : `oto_list_my_tools`, `oto_disable_tool`, `oto_enable_tool`, `oto_list_presets`, `oto_save_preset`, `oto_apply_preset`, `oto_delete_preset`. **`PROTECTED_TOOLS`** (`tool_visibility.py`, source unique) = trois familles jamais masquables (baseline/preset/default-hidden) **ni désactivables** : méta-toolset + identité (`oto_list_my_tools`/`oto_enable_tool`/`oto_apply_preset`/`oto_whoami`/`oto_profile`), échappatoires de contexte (`oto_use_org`/`oto_clear_org`/`oto_list_orgs`/`oto_use_group`/`oto_clear_group` — anti-lockout, vécu Sentry 2026-06-30), boucle d'usage (`feedback`/`run_start`/`run_finish` — mandatés par les instructions plateforme ADR 0017 : un preset qui les masque rend le gap invisible). Garde des deux faces (2026-07-02) : `oto_disable_tool` refuse, `POST /api/me/tools/{name}` → 400 `protected_tool` ; `GET /api/me/tools` expose `protected:bool` (toggle inerte dashboard).

`oto_save_preset` (et `POST /api/me/presets/{name}`) accepte 2 modes : snapshot (par défaut, capture l'état courant) ou explicit (param `enabled_tools=[...]`, sauve sans altérer l'état courant — utile pour provisionner par script).

**Refresh à chaud de la toolbox sur bascule de profil** : une capacité qui change le profil de visibilité déclare `refresh_visibility=True` (`Capability`) ; l'adaptateur MCP (`capabilities/_mcp_adapter.py`) rejoue alors `apply_session_visibility(reset=True)` sur la session **courante** après le handler → `tools/list_changed` live. Posé sur `org.use_org`/`org.clear`/`org.create`/`org.set_home` + `group.use`/`group.clear`/`group.set_home`. Donc **`oto_use_org <org>` recharge la toolbox dans la conversation en cours** (les credentials, eux, basculent déjà — `resolve_api_key` relit l'org **via le seam `current_org`** à chaque appel, cf. §ADR 0023 ci-dessous).

**Limite connue** : ça ne vaut QUE pour la face MCP (même session). Un toggle/bascule via **REST** (dashboard) passe par une connexion séparée → ne notifie pas une conversation Claude déjà ouverte (visible à la prochaine session). Pousser dashboard→session MCP demanderait un registre `sub → sessions actives` + push hors-requête (non fait).

## Org/équipe : session vs maison vs consultation (ADR 0023, amende 0015)

Le pointeur unique « org active » est scindé en **3 notions**, résolues par le **seam unique `access.current_org(sub)`** (mirroir `access.current_group(sub)` pour l'équipe) = `session ?? consultation ?? maison`. **TOUTE résolution d'action passe par ce seam** (`resolve_api_key`, visibilité `session_visibility`, field-filters, doctrine de groupe, `/api/me`, whoami, et l'injection `org_id` des règles d'autz `_authz`) — ne plus lire `org_store.get_active_org` en direct dans un chemin de résolution (**tripwire** `tests/test_org_seam_tripwire.py` : les call-sites légitimes de la maison sont figés en allowlist ; vécu 2026-07-02 — catalogue + toggles/presets REST scopaient la maison, le switch d'org du dashboard était ignoré, fixé `25e9f22`. Pendant front : `orgScope.spec.ts` d'oto-dashboard interdit un `fetch` nu hors du client central qui injecte `X-Oto-Org`).

⚠️ **Ce seam est scopé sur l'ACTEUR courant** : session/consultation sont stockées **par requête**, le `sub` ne sert qu'au repli `home_org`. Donc `current_org(autre_sub)` renvoie le contexte du **requérant**, pas du tiers — **NE JAMAIS** l'utiliser (ni `status_for`/`has_option`/`credential_mode_for` qui en dérivent) pour calculer l'état d'un **tiers** (écran admin). Passer son org/groupe **explicitement** via le kwarg `org`/`group` (sentinelle `access._UNSET` = défaut `current_org`, self inchangé), source = `org_store.get_active_org(target)`. Bug vécu 2026-06-24 (fiche admin montrant l'option de l'org du requérant). L'état d'un user est par ailleurs souvent **per-org** (∈ N orgs) → préférer une vue par org (cf. `tools/unipile.admin_status_by_org`).

- **Org de session** (éphémère, MCP) — override posé par `oto_use_org`/`oto_clear_org` (devenus **session-scopés**, ne touchent plus la colonne) dans `session_org.py` (store sync keyé par `ctx.session_id` — `get_state` async est inutilisable depuis `resolve_api_key` sync). Meurt avec la conversation ; repose sur l'isolation des sessions claude.ai par conversation. **Pas de jeton rejoué par appel** (bracelet serveur, pas de discipline LLM).
- **Org maison** (`org_store.get_active_org`, ex-« active_org ») — défaut persistant des **nouvelles** conversations. Posée explicitement : `oto_set_home_org` (MCP) ou `PUT /api/me/active-org` (REST/dashboard) ; **jamais** par navigation dashboard.
- **Org de consultation** (REST, view-as) — header `X-Oto-Org` (équipe : `X-Oto-Group`), posé par le **middleware ASGI `api_routes.ViewAsMiddleware`** (brut, n'altère pas le streaming `/mcp`) APRÈS **validation d'appartenance** (anti-IDOR : `roles.is_org_member`/`can_read_group`) dans un contextvar lu par `current_org`. Le dashboard consulte n'importe quelle org **sans muter l'identité MCP** — mais « consultation » = **org de TRAVAIL de l'onglet, lecture ET écriture** (poser une clé, éditer les settings y atterrissent), gatée par le rôle réel dans l'org ciblée ; le seul mode read-only est le view-as USER ci-dessous.
- **« Voir en tant que » (axe USER, REST, lecture seule)** — header `X-Oto-View-As=<sub>` posé par le même `ViewAsMiddleware`, gaté **opérateur plateforme + cible existe + méthode GET** (mutations → 403 `view_as_read_only`). `_authenticate` renvoie alors le **sub cible** (param `apply_view_as`, contextvar `session_org.current_view_user`) → tout `/api/me/*` (capacités incluses) rend la vue de la cible. **REST-only** : le MCP ne lit jamais ce contextvar (zéro impersonation dans Claude). Front : bouton sur la fiche admin + bandeau `ViewAsBanner` (`lib/viewOrg.ts`).

**Invariant groupe⊂org dérivé** : un override/consultation d'org **sans** groupe explicite ⇒ niveau org (jamais le `home_group` d'une autre org) ; toute bascule d'org de session retire l'override de groupe. `/api/me` expose `active_org`/`active_group` (effectifs) **et** `home_org`/`home_group` (défauts) distinctement. `oto_whoami` montre l'org effective + `scope: home|session`.

## Doctrines & instructions d'org

Prose opératoire métier par org (skills à la Claude Code, slug + versionnée).
**Détail : `docs/doctrines.md`**.

> **Livraison au LLM = injection, plus un appel d'outil (otomata-private#49 puis #50, amende ADR 0014).**
> Le canal FIABLE de bootstrap = les `instructions` du `initialize` (FastMCP les relit par
> session ; Claude rehandshake par conversation). `DynamicInstructionsMiddleware.on_initialize`
> (`middleware.py`) **remplace** `result.instructions` par `instructions.compose_session(sub, org_id)`
> — un **artefact composé de 2 blocs** (`instructions.py`, #50 ; l'ex-bloc B onboarding a été
> retiré le 2026-07-01 — l'onboarding est un projet, ADR 0032 §7) :
> - **bloc A « secret sauce »** (posture + boucle d'usage + **catalogue de namespaces** dérivé) —
>   prose en DB `platform_instructions['secret_sauce']`, éditable admin plateforme, **inviolable par
>   l'org**, toujours injecté (seedé depuis la constante = fallback) ; le catalogue est appendé à la composition ;
> - **bloc C « contexte dynamique »** par-(sub, org) — section de contexte résolu (org / équipe /
>   connecteurs actifs / N derniers projets / derniers déroulés via `db.recent_runs` / fiche profil
>   « situation avec oto » de l'user) + doctrine de base de l'org (`claude_md`) avec substitution
>   `{{org}}`/`{{user}}`/`{{équipe}}`/`{{connecteurs_actifs}}`.
>
> Donc **ne plus prescrire « appelle `oto_get_doctrine()` au démarrage »** — la doctrine est injectée.
> Les **doctrines nommées (skills)** ne sont pas des outils → absentes de `tools/list` → `on_list_tools`
> **enrichit la description de `oto_get_doctrine`** avec leur index per-org (`instructions.skills_index_md`,
> Tool non-frozen → `model_copy`). `render()` reste la surface STATIQUE (boot / fallback, sans DB).
> Tout **fail-open** (pas de sub/org/doctrine/DB → surface statique). Édition des blocs A/B : capacité
> `oto_admin_platform_instructions` (+ REST `/api/admin/platform-instructions`, `PLATFORM_ADMIN`) →
> éditeur dashboard `/platform/instructions`. Transparence : `/api/me/agent-context` rend le même
> artefact composé. **Reste (#54)** : anticipation **pilotée** (message proactif amorcé par l'admin).

## Groupes (départements) & hiérarchie de droits (ADR 0012)

Une org se subdivise en **groupes** (départements/équipes) avec un **chef
d'équipe** (`group_role='group_admin'`). La gestion des droits est **centralisée**
dans `roles.py` (escalade descendante, source unique) :

```
platform_admin ⊇ org_admin ⊇ group_admin (chef) ⊇ member
```

Les combinateurs d'autz (`capabilities/_authz.py`) délèguent à `roles`
(`is_org_admin`, `can_admin_group`, `can_read_group`, `effective_group_role`) —
plus d'escalade recopiée à la main. Combinateurs : `GROUP_ADMIN_OF`,
`GROUP_MEMBER_OF` (en plus de `ORG_*`).

Un groupe **gouverne 3 ressources** par délégation de l'org :
- **secrets partagés** — coffre `connector_credentials` (entity_type='group') ;
  cascade `resolve_api_key` = **user_key > secret groupe actif > secret org active > grant plateforme**.
- **doctrine & skills** — `org_group_instructions` (+ revisions) ; `oto_get_doctrine()`
  sert org **puis** groupe actif (complément, chaque skill taggée `scope`).
- **preset de toolset** — `org_groups.default_tools` (NULL = pas de baseline) ;
  baseline de visibilité au handshake (les toggles perso priment).

**Groupe actif** : ≤1 par sub (`org_group_members.is_active`, index partiel),
**invariant** = appartient à l'org active. `set_active_group` pose aussi l'org
active ; `set_active_org` efface le groupe actif. `oto_use_group` /
`PUT /api/me/active-group` (+ `oto_clear_group` / `DELETE`).

Stores : `group_store.py` (miroir d'`org_store` au grain groupe). `org_store`
n'importe PAS `group_store` (SQL direct pour l'invariant org↔groupe → pas de
cycle). Surfaces : capacités `capabilities/groups*.py` (REST `/api/orgs/{id}/groups`,
`/api/groups/{id}*`, `/api/me/active-group` + MCP `oto_*_group*`). `/api/me`
expose `active_group`/`active_group_name`/`group_role` ; `providers[].mode` peut
valoir `group`. **Détails : `docs/groups-and-roles.md`.**

## Fédération MCP & comptes (otomata#16)

Deux mécanismes : **mount** (MCP distant fédéré, token OAuth per-user, pilote
memento — systématique) vs **remote** (bridge data-driven ADR 0003, token M2M d'org,
pilote = un connecteur remote client). **Détail : `docs/federation.md`**.

## MCP Apps — UI rendue (SEP-1865)

Certains tools renvoient une **interface rendue** (carte/table dans un iframe
sandbox côté host : claude.ai, VS Code…) au lieu de JSON brut, via l'extension
MCP Apps (SEP-1865, stable). Implémenté avec **`prefab_ui`** (extra
`fastmcp[apps]`, déclaré dans `pyproject.toml` → installé par le `pip install -e .`
du deploy) : un tool `@mcp.tool(app=True)` renvoie un composant `prefab_ui`
(`Card`/`Column`/`Heading`/`Text`/`DataTable`) que le host peint ; dégradation
gracieuse en texte pour les clients sans support.

**Convention** : variantes **flagship `*_app`** (≠ remplacer les tools JSON), où
un visuel aide vraiment l'utilisateur. Les tools JSON équivalents restent la voie
par défaut/agent (« si le rendu échoue, utiliser le tool JSON équivalent »).
L'import de `prefab_ui` est **optionnel et guardé** dans le module (si l'extra
manque, les `*_app` ne s'enregistrent pas, les tools JSON restent). Premier jeu :
`tools/foncier.py` → `foncier_site_app` (fiche site : géocodage + parcelle +
bâti), `foncier_comparables_app` (ventes comparables DVF autour d'une adresse),
`foncier_prix_m2_app` (stats €/m² d'une commune). Mêmes clients open-data que les
tools JSON ; rendu **défensif** (colonnes dérivées des clés réelles) pour ne pas
dépendre d'un nom de champ. Gatés par le connecteur (namespace `foncier`).

## Conventions

- Nouveau connecteur = (1) un fichier `tools/<service>.py` exposant `register(mcp)`,
  (2) une **entrée au registre `providers.py`**. `register_all` (`tools/__init__.py`)
  **DÉRIVE le chargement du registre** (#24, fin de la liste hardcodée) : il boucle
  sur les providers `kind="tools"` et importe `Connector.modules` (défaut = nom du
  provider ; renseigner `modules` si module ≠ nom, ou plusieurs modules par provider —
  ex. `sirene`→`fr`, `google`→`gmail`/`datastore`/`tasks`). Chaque import en
  try/except (un connecteur cassé ne fait pas tomber le serveur). `meta`/`orgs`
  (spine) + `remote`/`mount` (génériques) restent chargés explicitement. ⚠️ Le
  namespace déclaré doit matcher `namespace_of(tool)` (1er token avant `_`) — pas de
  namespace multi-mot (`culture_spectacle`→`culture`), sinon fail-open du gate.
  Le garde-fou `test_tools_module_derivation_matches_filesystem` (`tests/test_capabilities_drift.py`)
  est **auto-maintenu** (croise `tools/*.py` au registre) — ajouter un connecteur
  (fichier + entrée registre) le garde vert SANS rien y toucher ; il casse seulement
  sur un **fichier orphelin** (connecteur posé mais pas déclaré → dort invisible) ou un
  **module fantôme** (faute dans `modules=`/nom). Seul un **module spine** chargé
  explicitement (rare) s'ajoute à `_EXPLICIT_TOOL_MODULES`. ⚠️ **Aucune CI de test sur
  les PR** (`gh pr checks` = vide ; seul `deploy.yml` tourne sur push main) → un test
  rouge atterrit sur `main` sans rien bloquer. Lancer les tests à la main.
- **PERF — un handler de tool fait du I/O bloquant ⟹ il est `def` SYNC, jamais `async def`.**
  Le serveur est **mono-event-loop** (`uvicorn.run(app)`, pas de `workers=`). FastMCP route
  un `def` sync en **threadpool** (`call_sync_fn_in_threadpool`) mais exécute un `async def`
  **dans la boucle**. Nos connecteurs appellent des libs **synchrones** (`requests` via
  france_opendata, DuckDB, clients HTTP sync) → un `async def` **sans `await`** gèle TOUTE la
  boucle le temps de l'appel (vécu 2026-06-25 : `/health` à 110 s, p95 `fr_stock_search` 218 s ;
  fix `async`→`def` sur `fr.py`/`fr_stock.py` → `/health` ~0,1 s). Règle : un handler `tools/*.py`
  qui n'`await` rien doit être `def`. Ne garder `async def` que s'il `await` réellement (httpx
  async, etc.). NE PAS ajouter de workers uvicorn (état de session streamable_http en mémoire).
  **Lot connecteurs bouclé le 2026-06-29** (361 handlers convertis ; cause re-vue = un flot
  de `serper_scrape` gelant la boucle, `/.well-known` à 1,4–10,5 s sur une box à 0,2 de load).
  **CI-enforcé** : `tests/test_no_blocking_async_handlers.py` casse si un `@mcp.tool` async
  n'`await` rien dans son **propre scope** (AST own-scope, auto-maintenu, pas de whitelist) ;
  un `client_factory` awaité par FastMCP (`mount.factory`) reste async — « pas d'await » ne
  suffit pas, vérifier que c'est un handler, pas un callback. Bornes connexions PG posées au
  passage (`db._connect_options` : `idle_in_transaction_session_timeout` anti-zombie-lock).
- **Cran d'activation (ADR 0010/0011)** : déclarer un connecteur ne l'expose PAS —
  gate DB `connector_activation.py` (master global ± override org, deny-by-default).
  Gate à la **VISIBILITÉ par session** (`UserDisabledToolsMiddleware` + `connector_
  activation`, **fail-open**) : `register_all` charge tout inconditionnellement, le
  middleware masque les tools d'un connecteur non activé pour l'org → (dés)activer
  prend effet à la session suivante **sans restart**, override par org OK. Filtre
  aussi `/api/connectors` (catalogue) ; overlays catalogue `family` (dérivée) +
  `category` (curée) + `publisher` (curé, `_PUBLISHER_BY_CONNECTOR`) + `logo_url`
  (dérivé du **CDN logo.dev** par `Connector.logo_url_for` : domaine de marque curé
  `_LOGO_DOMAIN_BY_CONNECTOR` + token publishable `LOGODEV_TOKEN` en env ; pas de S3,
  pas de seed. open-data/maison sans domaine → pas de logo, monogramme côté UI).
  Surface admin `/api/admin/connectors/activation`
  (`api_routes_connectors.py`) + écran dashboard « connector activation ».
- **Connecteur client-sensible = JAMAIS de code ici** : pont via le connecteur
  **`bridge` universel** (ADR 0034, amende 0003/0011) — UNE entrée générique au
  registre (`kind="remote"`), tools fixes `bridge_describe`/`bridge_call`
  (`tools/remote.py`). L'identité du service ponté vit dans la **CONFIG d'org**
  (champs standard `base_url`/`token`/`label`, `resolve_credential_fields`),
  **jamais dans le namespace** → montrable au catalogue sans nom client (l'ex-fuite
  /tools/mm venait du namespace-par-client). Le bridge distant détient le
  credential métier (contrat ADR 0003 §4 inchangé : `/describe`+`/call`, bearer
  M2M, lecture seule bornée côté bridge, audit `X-Oto-Sub`). Visibilité = régime
  commun (activation × masque, `default_hidden` → self-activable) ; sans
  credential, l'exécution lève proprement. Pilote : le bridge back-office
  Movinmotion (repo privé), migré du legacy per-namespace `mm_*` le 2026-07-02
  (découverte `meta.base_url`, règle de visibilité dédiée et
  `resolve_remote_credential` retirés en B4).
- **Tool API-keyé = déclarer le connecteur dans le registre `connectors.py`**
  (avec `keyed=True` + `auth_modes`) — `KEY_PROVIDERS` et tout le reste en
  dérivent. Le coffre `connector_credentials` est générique (pas de colonne
  par provider) : aucune migration de schéma à ajouter. Sinon `resolve_api_key`
  lève `Unknown provider` à l'appel. Puis poser la clé plateforme en DB via
  `oto_admin_set_platform_key` (plus de bootstrap SOPS — le provider sans clé
  DB n'a simplement pas de mode plateforme).
- **Credential = champs déclarés (modèle générique multi-champs, ADR 0011)** : un
  provider porte `credential_fields` (`CredentialField` name/label/secret/reveal) ou
  les dérive de `secret_kind` (`api_key`=1 champ, `basic_auth`=2). Le coffre encode
  les champs dans l'unique `secret_enc` via `credentials_store.pack_secret`/
  `unpack_secret` (3 formats : valeur brute 1 champ / base64 `email:password` /
  json ≥2). L'endpoint `/api/settings/api-keys/{provider}`, le formulaire dashboard
  et `status_for` bouclent sur `secret_fields` — **zéro branche par connecteur** ;
  un nouveau connecteur multi-secrets = une déclaration. Résolution : `resolve_api_key`
  (1 clé keyed + platform/quota) **ou** `resolve_credential_fields` (byo multi-champs
  sans quota, ex. `silae` : client_id/client_secret/subscription_key). `cookie`/`oauth`
  (linkedin/google/memento) ont des flux dédiés → `secret_fields` vide.
- Docstrings = contrat LLM (le modèle choisit les tools là-dessus). Précis, pas verbeux.
- **Aucune résolution de secret côté serveur hors DB/env de process** : pas de
  `get_secret`/`require_secret` oto.config dans le code serveur (l'unit pose
  `OTO_CONFIG_DISABLE_SOPS=1`, tout résidu échoue fort).
- LinkedIn nécessite le **vrai Google Chrome système** (`google-chrome-stable`, apt)
  sur l'host — PAS le Chromium bundlé Patchright (empreinte TLS ≠ Chrome de bureau
  → bloqué par LinkedIn). `_require_chrome_channel` (`tools/linkedin.py`) force
  `channel="chrome"` et lève une erreur si absent.
- WhatsApp/Telegram/Instagram = messagerie **Unipile** (cf. §WhatsApp) — aucune dép
  Node côté backend. Le Baileys Node (`oto-core/.../whatsapp/node/`) ne sert plus
  qu'à la CLI `oto whatsapp` (fallback archivé).
- Attio (`tools/attio.py`) expose CRUD complet : records (companies/people/deals),
  notes (sauf update body, limite API), tasks, lists, entries, workspace_members,
  comments, threads, meetings, call_recordings + meta (objects, attributes). Pas
  de quota plateforme — chaque user pose sa clé sur `/account`. **Gotcha** :
  `attio_list_threads` renvoie 400 sans `parent_object`/`parent_record_id` —
  toujours filtrer par parent.

## Commands

```bash
# Transport stdio RETIRÉ (2026-06-13) : oto-mcp ne se sert qu'en streamable_http
# (toujours authentifié Logto). Usage local = CLI `oto`. Pour un serveur local,
# lancer en http avec les LOGTO_* et taper avec un bearer.

# Tests — le venv .venv N'A PAS pytest (extra `dev` non installé) et `uv run pytest`
# crée un env éphémère SANS les deps projet (piège, ModuleNotFoundError). Recette :
uv pip install --python .venv/bin/python "pytest>=8.0" "pytest-asyncio>=0.24"
.venv/bin/python -m pytest -q

# Deploy — push main déclenche `.github/workflows/deploy.yml` : workflow unique
# CI/CD (job `test` pytest → job `deploy` `needs: test`). Deploy = SSH box dédiée :
# git reset --hard origin/main + pip install -e . + **force-reinstall oto-core
# depuis le tag pinné** (lu du pyproject ; pip saute sinon une dép VCS déjà
# présente) + restart + **smoke HTTP** (GET 200 /.well-known/oauth-authorization-server)
# + **rollback auto** vers le commit précédent si install/restart/smoke échoue. Le
# restart relance start-encrypted (refetch master key). ⚠️ start-encrypted.sh
# untracked → survit au git reset.
git push origin main

# Logs
ssh -i ~/.ssh/alexis root@<box> "journalctl -u oto-mcp -f"

# DB inspect (PG managed) — depuis la box (env du process inclut DATABASE_URL via .env)
# ⚠️ `psql` n'est PAS installé sur la box dédiée → passer par le venv + psycopg :
ssh -i ~/.ssh/alexis root@<box> 'cd /opt/oto-mcp && set -a; . .env; set +a; ./.venv/bin/python -c "
import os, psycopg
with psycopg.connect(os.environ[\"DATABASE_URL\"]) as c:
    for r in c.execute(\"SELECT sub, email, role FROM users\"): print(r)
"'

# ⚠️ Déchiffrer un credential ad-hoc (crypto.decrypt / _reveal / credential_status) :
# `OTO_MCP_MASTER_KEY` n'est PAS dans .env — start-encrypted.sh la fetch au boot
# depuis Scaleway Secret Manager. Un script qui ne source que .env voit
# `encryption_enabled()=False` → tous les déchiffrements lèvent RuntimeError (FAUX
# négatif, ≠ InvalidTag). Pour reproduire le runtime, répliquer le fetch :
#   set -a; . .env; . /etc/oto-mcp/scw.env; set +a
#   RESP=$(curl -s -H "X-Auth-Token: $SCW_SECRET_KEY" \
#     ".../secret-manager/v1beta1/regions/fr-par/secrets/<id>/versions/latest_enabled/access")
#   export OTO_MCP_MASTER_KEY=$(echo "$RESP" | python3 -c 'import json,sys,base64; print(base64.b64decode(json.load(sys.stdin)["data"]).decode())')
# Vécu 2026-06-22 (triage Sentry InvalidTag : 1 ligne memento corrompue, écrite
# avec une clé ≠ courante — les autres lignes déchiffraient → pas un souci de clé ;
# fix = purge → re-OAuth). `status_for` doit utiliser `credential_status` (présence
# sans déchiffrer), jamais `get_credential_with_meta`, pour ne pas 500 /api/me.
```

## Infra

Déployé sur une **box Scaleway dédiée** (ADR 0002, depuis 2026-06-11) : oto-backend isolé + Caddy + chiffrement du coffre actif, sert `mcp.oto.ninja`. **DB** = PostgreSQL managé partagé (`otomata-main`, DB `oto_mcp`). Le coffre `connector_credentials` est chiffré au repos (AES-256-GCM, master key en Secret Manager fetchée au boot, 0 plaintext). Object Storage S3 pour avatars/logos (`media_store.py`).

> **Détails machine = repo privé `otomata-tech/infra`** (IPs, IDs de secrets/zone/instance, systemd, runbook deploy, env de process) — pas ici (ce repo est public). Voir `infra/docs/oto-platform-state.md` + docs ciblés (`scaleway-managed-db.md`, `caddy.md`, `cloudflare.md`, `deploy-keys.md`). Toute intervention prod = skill `prod-init`.

## Docs

- `docs/connector-model.md` — **carte d'ensemble** : les **3 couches** d'un connecteur (disponibilité / authentification / option de connecteur), la matrice des niveaux (user/groupe/org/plateforme), le vocabulaire canonique, le seam `access.has_option`. **À lire en premier** avant de toucher activation/clés/options (les autres docs ci-dessous = le détail par couche).
- `docs/connector-vault.md` — **archi centrale** : registre source unique (`connectors.py`), coffre chiffré unique `connector_credentials` (clés API + platform_keys + sessions linkedin/crunchbase/google multi-compte), enveloppe AES-256-GCM **obligatoire** (pas de plaintext), résolution + palier org. À lire avant de toucher credentials/registre/résolution.
- `docs/roles-and-resolution.md` — rôles (3 paliers) + cascade de résolution de clé / grants / platform keys.
- `docs/doctrines.md` — doctrine & skills d'org (oto_get_doctrine, versionnée).
- `docs/auth-logto.md` — auth Logto ES384, discovery RFC 9728, façade DCR.
- `docs/rest-api.md` — inventaire des endpoints REST `/api/*`.
- `docs/federation.md` — fédération MCP : mount (per-user) vs remote/bridge (org).
- `docs/usage-loop.md` — boucle d'usage ADR 0017 (calllog + feedback + déroulés).
- `docs/monitoring.md` — monitoring des appels MCP (tool_call_log + surface admin).
- `docs/datastore.md` — datastore spine PG (`data_*`) + OAuth Google per-user (setup GCP, scopes).
- `docs/groups-and-roles.md` — groupes/départements & hiérarchie de droits (ADR 0012).
- `docs/redaction.md` — **rédaction de champs** : middleware unique (FieldRedactionMiddleware), rien par défaut + templates 1-clic, **schéma OBSERVÉ** (capture passive `connector_schemas` — passthrough d'API tierces → on observe au lieu de déclarer), dry-run preview, moteur `FieldFilter` (oto-core).
