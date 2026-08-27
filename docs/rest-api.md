---
title: REST API (consommée par oto.ninja /account)
type: reference
description: >-
  Inventaire des endpoints REST /api/* de oto-backend : profil /api/me (billing,
  onboarding, connecteurs), settings LinkedIn/API-keys/tools, doctrine org
  /api/me/instructions*, palier org (CRUD orgs, membres, secrets, invitations,
  entitlements namespace), admin users/grants/tokens/monitoring, billing Stripe,
  bibliothèque publique de doctrines (doctrine_library, visibilité public/unlisted).
  Détaille les règles CORS (oto.ninja, app.oto.ninja, dashboard.oto.ninja), l'autz
  (même JWTVerifier ES384 que /mcp, audience mcp.oto.ninja), et les gotchas secrets
  (jamais la clé en réponse, providers per-user refusés en org secrets). À charger
  pour implémenter ou déboguer un endpoint REST ou comprendre le contrat front/back.
---

# REST API (consommée par oto.ninja /account)

## Où vit chaque famille (découpe du 2026-08-27)

`api_routes.py` **assemble** et ne contient plus aucun handler : `make_routes` monte les
modules de routes, monte la couche capacité, et rend la table ordonnée des chemins écrits
à la main. L'ORDRE de cette table est un contrat — Starlette prend le PREMIER match, donc
`…/tools/registry` doit précéder `…/tools/{name}`. Elle est figée par
`tests/test_api_routes_table_frozen.py` : retirer, ajouter ou réordonner un chemin fait
rouge, et régénérer `tests/api_routes_table.txt` EST la déclaration de l'ajout.

| famille | où | régime |
| --- | --- | --- |
| ~200 chemins générés (**le compte** `/api/me`, **la toolbox** `/api/me/tools*`, **la session navigateur**, projets, pages, procédures, ressources, orgs, doctrine, monitoring, datastore, billing…) | `capabilities/` + `_rest_adapter` | **capacité** : un descripteur, deux faces (ADR 0009/0042) |
| primitives (`_authenticate`, CORS, `_json`/`_json_error`, `OPTIONS`, `bind`) | `api_routes_base.py` | partagées par tous les modules ; **ré-exportées** par `api_routes` |
| favicon, `/api/mcp/catalog`, `openapi.json`, `/api/connectors`, bibliothèques doctrines & guides, aperçu d'invitation, docs partagés (`/api/public/docs/{token}`, `/p/d/{token}`) | `api_routes_public.py` | **sans auth** — l'adaptateur capacité authentifie toujours |
| `/api/me/avatar`, `/api/orgs/{id}/logo` | `api_routes_media.py` | multipart → hors couche capacité |
| fichiers bruts d'un projet, `/api/me/projects/{id}/export` | `api_routes_projects.py` | multipart / binaire |
| `/api/upload/{token}` (PUT/POST/GET) | `api_routes_uploads.py` | **pas de JWT** : le jeton de l'URL fait foi |
| `/api/admin/platform-keys*`, `/api/admin/users/{sub}/tokens*` | `api_routes_admin.py` | `allow_api_token=False` : un jeton ne fabrique pas de jeton |
| SIRENE, accords, datastore, contact, connecteurs, webhook Mollie, OAuth zoho/atlassian/folk/salesforce | `api_routes_<nom>.py` (antérieurs à la découpe) | gardent leur patron : `make_routes(...)` reçoit les primitives en paramètres |

- `GET /api/me` + `GET /api/me/calls` + `GET /api/me/activity-summary` — **le compte**,
  capacités `me.{get,calls,activity_summary}` depuis le 2026-08-27
  (`capabilities/me_account.py` ; `api_routes_account.py` supprimé). **Pas de face MCP** —
  l'identité d'une session MCP est `oto_whoami`, l'activité se lit par les lentilles de
  monitoring. `GET /api/me` rend profil + rôle plateforme + `providers` (accès effectif
  par connecteur : mode/clé/quota) + `features.billing` + le couple **`active_*` / `home_*`** :
  `active_org`/`active_group` sont les valeurs EFFECTIVES de la requête (seam `current_org`,
  ADR 0023 — la consultation `X-Oto-Org` l'emporte), `home_*` le défaut persistant. Un front
  qui scope ses vues sur `home_org` affiche les données d'une autre org que celle qu'il
  annonce. `active_org_readonly` = opérateur plateforme en consultation (bandeau + écran en
  lecture). Les deux lentilles d'activité sont scopées **(sub, org active)** : jamais un
  autre membre, jamais une autre org — filtres `?limit=` (défaut 200, plafond dur 1000),
  `?tool=` (nom EXACT), `?errors=1|true` (littéral : `?errors=yes` ne filtre pas), `?days=`.
  ⚠️ **Le repli de saisie est conservé** : `?days=abc` rend 200 avec la fenêtre par défaut,
  jamais un 400 — c'est le comportement servi depuis toujours. En revanche un **paramètre
  inconnu** est désormais refusé (400 `unknown_fields`, champ nommé) là où il était ignoré
  en silence : c'est la garde de l'adaptateur, et c'est le seul écart visible de la migration.
- `POST|DELETE /api/me/avatar` — upload (multipart `file`, png/jpeg/webp ≤ 2 Mo) / efface l'avatar user → Scaleway Object Storage, URL publique en DB
- `POST|DELETE /api/orgs/{id}/logo` — upload / efface le logo **uploadé** d'org (org_admin, multipart `file`). Le logo AFFICHÉ (`logo_url` des lectures + `active_org_logo_url` de `/api/me`) est l'**effectif** : upload sinon dérivé du CDN logo.dev via le `domain` déclaré (`org_store.effective_logo_url`, token `LOGODEV_TOKEN`) ; `logo_custom` (fiche org) dit si un upload existe.
- `PATCH /api/orgs/{id}` (+ miroir `/api/admin/orgs/{id}`) — profil d'org (org_admin) : `name`, `description`, **`domain`** (domaine de marque, normalisé `org_store.normalize_domain` — `""` efface, saisie URL tolérée, invalide → 400 `invalid_domain`), `industry`, `location`. Capacité `org.update` (MCP `oto_org(op='update')`, console ADR 0047).
- `POST|DELETE /api/settings/linkedin` — cookie li_at + UA
- `POST /api/me/connectors/{name}/session/start` + `POST …/session/finalize` — **la
  connexion par SESSION NAVIGATEUR** (Live View Browserbase, ADR 0026), capacités
  `me.browser_session.{start,finalize}` depuis le 2026-08-27
  (`capabilities/browser_sessions.py` ; `api_routes_credentials.py` supprimé).
  `start` ouvre une vraie fenêtre de navigateur hébergé où **l'utilisateur se connecte à
  la main** et rend `{live_view_url, context_id, session_id}` ; `finalize` vérifie le
  login puis persiste la session au coffre — c'est un credential comme un autre.
  ⚠️ **`connected: false` est une 200** : « pas encore logué », pas un échec — la session
  vit, rien n'a été écrit, on réessaie. **Pas de face MCP** : un `context_id` EST le
  credential, et le geste exige un humain ; le pendant agent est
  `POST /api/me/connectors/{name}/connect` (capacité `me.connector_connect`).
  `scope` ∈ `member` (défaut) | `org` | `group` — les deux partagés exigent d'être admin
  du palier ET un connecteur partageable, et **l'ordre des refus est un contrat** :
  `400 no_org_context` → `400 not_org_shareable` → `403 forbidden` (d'où une escalade au
  handler plutôt qu'une règle d'autz, qui trancherait trop tôt). `account` = le compte du
  coffre visé (connecteur générique : le host — une ligne PAR SITE) ; `force` persiste
  sans le verify générique, réservé aux connecteurs account-aware. Autres refus :
  `404 not_a_session_connector`, `400 missing_params`, `400 invalid_scope`,
  `503 browserbase_unavailable`, `502 session_verify_failed` (les deux derniers portent
  un détail actionnable). ⚠️ Un corps JSON **malformé** rend désormais `400 missing_params`
  et non `400 invalid_json` (même statut), et un corps **non-objet** rend `400` là où il
  levait une **500**.
- `GET|POST|DELETE /api/settings/api-keys/{provider}` — **ton credential** pour un
  connecteur, dans l'org de contexte (capacités `me.credential.{get,set,clear}` depuis
  le 2026-08-27 ; **pas de face MCP** — un secret brut ne passe pas en argument d'outil).
  Le **corps du POST est plat et dynamique** : ses clés sont les `credential_fields` du
  connecteur (publiés par `GET /api/connectors`), plus `account` — le nom du compte visé
  quand le connecteur en porte plusieurs (le mot d'usage est dans `auth.account_noun` :
  « workspace » pour Slack). D'où le cran `body_field` du binding : sans lui, la garde de
  champ inconnu refuserait chaque clé de credential. Refus nommés : `404 unknown_provider`,
  `403 connector_restricted` (RBAC ADR 0025 — la pose suit l'usage), `400 missing_credentials`
  (le champ vide est NOMMÉ), `409 account_required` (pose anonyme là où des comptes nommés
  existent), `400 single_account_connector` (compte nommé sur un connecteur qui ne les
  résout pas), `400 verify_failed` (sonde avant persistance, #106). `DELETE` prend
  `?scope=member|org|group` (admin du palier pour les deux derniers) et `?account=`.
- `GET /api/me/tools` + `GET /api/me/tools/registry` + `POST|DELETE /api/me/tools/{name}`
  + `GET …/{name}/detail` + `POST …/{name}/call` — **la toolbox du membre**, capacités
  `me.tools.{list,registry,disable,enable,detail,call}` depuis le 2026-08-27
  (`capabilities/tools_me.py` ; `api_routes_tools.py` supprimé).
  ⚠️ **`POST` DÉSACTIVE, `DELETE` RÉACTIVE** — le chemin nomme la ligne de denylist, pas
  le tool : la poser masque, la retirer démasque. Contre-intuitif, historique, figé par
  test. La bascule est **visibilité-only** (ADR 0031) : `enabled` est une préférence
  d'affichage, jamais une autorisation — l'accès réel reste gardé au call-time
  (credential + RBAC connecteur ADR 0025 + activation). Un tool **protégé** (anti-lockout)
  refuse le masquage en `400 protected_tool:<nom>`.
  `…/registry` = le registre BOOT (ADR 0014), immunisé à la visibilité de session : il dit
  ce qui EXISTE, pas ce qui m'est visible ; sa `description` est un résumé d'une ligne, la
  fiche complète est `…/detail`. **`…/registry` doit précéder `…/{name}`** dans la table,
  sinon `registry` est servi comme un nom d'outil — les six ont migré EN BLOC pour ça.
  `…/{name}/call` exécute un outil **testable** (open-data en lecture seule) sous
  l'identité de l'appelant ; son corps est **libre** (il EST les arguments, nus ou
  enveloppés dans `{"arguments": {…}}`, d'où `body_field`) et ⚠️ **l'erreur de l'outil
  revient en DONNÉE — `ok:false` en 200** : la voir est le but du test. Les 4xx disent
  qu'on n'a pas pu lancer (`403 not_testable:` — qui passe **avant** la résolution du nom,
  donc un outil inconnu rend 403 et non 404 —, `400 bad_arguments:`).
  **Pas de face MCP** : `oto_list_my_tools`/`oto_enable_tool`/`oto_disable_tool` restent
  écrits à la main, leurs formes diffèrent de celles-ci — réconciliation suivie en
  oto-backend#429.
- `GET /api/me/instructions` (index des procédures ; le readme d'org est un guide `delivery=init`, plus servi ici) + `GET|PUT|DELETE /api/me/instructions/{slug}` + `GET /api/me/instructions/{slug}/versions` + `POST /api/me/instructions/{slug}/revert` — procédures de l'**org active** (le slug `claude_md` est RÉSERVÉ au readme et refusé ici) (cf. §Doctrines). Lecture = membre ; écriture = `org_admin` (ou platform admin). Édité par le dashboard (`/procedures`). ⚠️ Le `PUT` renvoie un **`diagram_warning`** (toujours présent ; `null` = rien à signaler) quand le corps n'embarque pas le SCHÉMA requis de la procédure — non bloquant, comme `unresolved_tools`/`slot_warnings` (cf. §Doctrines).
- `GET|PUT|DELETE /api/me/guides/{scope}/{slug}` (+ `GET /api/me/guides`) — **la prose d'instruction**, un seul primitif sur deux axes (ADR 0042 §Convergence des surfaces) : `scope` ∈ platform|org|group|user × `delivery` ∈ `on-demand` (défaut, un how-to chargé au besoin) | `init` (**readme injecté à chaque session**, slug canonique `readme` — corps vide = couche effacée). Miroir REST d'`oto_guide`, mêmes handlers. Écriture gatée par scope (platform_admin / org_admin / chef d'équipe / self). Variantes par-id pour viser une org/équipe précise plutôt que l'active : `GET|PUT /api/orgs/{id}/guides/{scope}/{slug}` et `/api/groups/{id}/guides/{scope}/{slug}`. *(Remplace `GET|PUT /api/me/agent-readme`, retiré le 2026-07-28.)*
- `POST|DELETE /api/me/projects/{id}/public-share` — **partage public CHIFFRÉ** d'un projet (ADR 0032 §3, zero-knowledge). Le dashboard chiffre le snapshot (brief + pages) côté navigateur et POSTe uniquement `{ciphertext}` ; renvoie `{token, public_base_url}`. Écriture = `ownership.can_access(project, write)`. La clé de déchiffrement n'atteint JAMAIS le serveur (fragment d'URL).
- `GET /api/public/projects/{token}` — **sans auth** : renvoie `{ciphertext, updated_at}` du snapshot chiffré. Déchiffrement côté navigateur (route `/p/p/{token}#<clé>`). Pendant public de `GET /api/public/docs/{token}` (#4a).
- `PUT|POST|GET /api/upload/{token}` — **réception d'un upload signé out-of-bande** (issue #105), **pas de JWT** : le `{token}` est un jeton HMAC scellant `(sub, org, cible)` + TTL court + usage unique (émis par `oto_upload_url`, module `upload_tokens.py`). **PUT** = un agent avec shell y pousse le corps brut (`curl --data-binary @fichier`) ; **POST** multipart `file` = le formulaire humain ; **GET** = page HTML d'upload autoportée (fallback quand l'agent n'a pas de shell, ex. claude.ai : il transmet le lien à l'humain — le jeton n'est PAS consommé au GET). Le backend matérialise dans la cible en **réappliquant** son autz, consomme le jeton (anti-rejeu), renvoie un **accusé léger** (id + compteurs), jamais le body. Cibles : page Documents (`doc`), fichier brut de projet (`project_file`, autz `ownership.can_access(project, write)`), lot de lignes datastore (`datastore` — NDJSON/CSV batch-upsert sur clé, autz `ownership.can_access(datastore_namespace, write)`, ns_id scellé au mint). Évite de faire transiter du gros contenu par le contexte du LLM.
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- `POST /api/admin/users/{sub}/grants/{key_id}` body `{daily_quota}` — set/update quota par grant (admin only)
- `GET|POST /api/admin/users/{sub}/tokens` + `DELETE /api/admin/users/{sub}/tokens/{token_id}` — issue/list/revoke tokens API on behalf of a user (admin only)
- `GET /api/admin/monitoring/summary?days=` + `GET /api/admin/monitoring/calls?limit=&sub=&tool=&errors=&days=` — journal des appels MCP, agrégats + brut (admin only, cf. §Monitoring)
- `GET /api/orgs/{id}/monitoring/{summary,calls,calls/{call_id},connectors,adoption,runs,runs/{run_id},gaps,tool-quality}` — **les mêmes lentilles au niveau ORG** (`capabilities/org_monitoring.py`, autz `ORG_ADMIN_OF`, face MCP `oto_org_monitoring`). Scope = `tool_calls.org_id`/`usage_signals.org_id` (ce qui a été émis SOUS l'org), jamais l'appartenance des membres. `adoption` n'existe qu'à cet étage (membre par membre : actif / jamais actif / bloqué par un connecteur). ⚠️ `calls/{call_id}` et `runs/{run_id}` rendent **404** hors de l'org (id séquentiel devinable). Sert la page dashboard `/org/monitoring`. Cf. `docs/monitoring.md` §Trois étages.
- `GET /api/admin/tenants?days=` + `GET /api/admin/tenants/{slug}?days=` — **suivi de l'étage tenant** (ADR 0052 ; `capabilities/tenants_admin.py`, autz `PLATFORM_ADMIN`, face MCP `oto_admin_tenant` op=list|get). Par tenant : configuration d'annuaire (émetteur, jwks, hosts, client OAuth, dashboard, chemins de lien), **état dans le process** (`loaded` / `pending_restart` — le registre d'émetteurs est bâti AU BOOT, donc un tenant déclaré depuis n'authentifie encore personne), et l'empreinte sur la fenêtre (orgs via `orgs.tenant_id`, comptes via la qualification du sub, comptes actifs, appels MCP). ⚠️ Ces deux rattachements sont des sources INDÉPENDANTES : `orgs_desalignees` compte les orgs du tenant créées par un compte relevant d'un autre, et la fiche en donne la liste. **Lecture seule** — déclarer un tenant est un runbook de provisioning, pas une route. Sert la page dashboard `/platform/tenants`.
- **Palier org** (100 % en CAPACITÉS — `capabilities/orgs*.py` ; ⚠️ cette ligne renvoyait à `api_routes_orgs.py` jusqu'au 2026-08-27, fichier **supprimé** lors de la migration : projection 1:1 des meta-tools `oto_admin_*org*` / `oto_list_orgs`) :
  - self-service : `GET|POST /api/me/orgs` (**`POST` = `org.create` self-serve**, créateur→org_admin, cap `OTO_MCP_MAX_ORGS_PER_USER`) ; `GET /api/orgs/{id}` ; `POST|DELETE /api/orgs/{id}/members[/{sub}]` + `PUT|DELETE /api/orgs/{id}/secrets/{provider}` (org_admin)
  - **invitations — feature cascade plateforme/org/équipe** (le scope est DÉRIVÉ des cibles : org_id NULL = plateforme, org_id seul = org, org_id+group_id = équipe). Trois faces émettrices, une seule acceptation :
    - **org** : `POST|GET /api/orgs/{id}/invitations` + `DELETE …/{inv}` (org_admin ; `oto_org` op=invite).
    - **équipe** : `POST|GET /api/groups/{id}/invitations` + `DELETE …/{inv}` (group_admin ; `oto_group` op=invite). L'invité rejoint l'org parente PUIS l'équipe à l'acceptation.
    - **plateforme** : `POST|GET /api/admin/invitations` + `DELETE …/{inv}` (platform_admin ; `oto_admin_invite` op=create/list/revoke). `org_id` optionnel (vide = onboarding pur, sinon rattachement direct).
    - **acceptation commune** : `POST /api/me/invitations/accept` (`SUB_ONLY`, token/code, match email vérifié + expiry). Email via `oto_mcp/email.py` (otomata-mailer `mailer.oto.zone/api/send`, env `OTO_MAILER_SEND_BEARER`, best-effort → `invite_url` en repli ; **plus de Resend**).
  - **fiche admin user** : `GET /api/admin/users/{sub}` = identité + accès effectif par provider (`status_for`) + grants + namespaces + orgs (membership).
  - platform admin : `GET|POST /api/admin/orgs`, `GET /api/admin/orgs/{id}` (+ entitlements), `…/members*`, `…/secrets/{provider}`, `POST|DELETE /api/admin/orgs/{id}/entitlements/{namespace}`, `GET /api/admin/namespace-grants`, `POST|DELETE /api/admin/users/{sub}/namespace-grants/{namespace}`
  - secrets : jamais la clé en réponse (provider/base_url/set_at/set_by) ; providers per-user (slack/linkedin/google/whatsapp) refusés en `400` ; listing lu du coffre canonique `credentials_store` (legacy `org_secrets` plus dual-written sous chiffrement). Gating org_admin/membre via `org_store.get_org_role` (platform admin toujours autorisé). Révocation lazy sur sessions MCP ouvertes. Contrat front : `oto-app/docs/ORG_API_CONTRACT.md`.
- **Bibliothèque publique de doctrines** (marketplace de skills, table `doctrine_library`) :
  capacités `library.*` (`capabilities/doctrine_library.py`, montage auto MCP+REST) —
  `library.list/get` (`SUB_ONLY`, MCP `oto_procedure` op=library_list/library_get + REST
  `GET /api/me/doctrines/library[/{slug}]`), `library.publish`/`library.fork` (`ORG_MEMBER` +
  gate org_admin en handler, MCP `oto_procedure` op=publish/fork + REST
  `POST /api/me/doctrines/{publish,fork}`), `library.unpublish` (auteur/PLATFORM_ADMIN, `DELETE
  /api/me/doctrines/library/{id}`). **Auteur** = `otomata` si publieur platform-operator, sinon
  l'`org`. **Fork** réutilise `org_store.set_instruction` → skill d'org versionné. Surface
  ANONYME pour la vitrine : routes écrites à la main `GET /api/doctrines/library[/{slug}]`
  (deny-by-default `visibility='public'`, l'adaptateur capacité authentifie toujours).
  **`visibility`** : `public` (dans le catalogue) vs `unlisted` = **lien non listé** (style
  YouTube) — servie par `library.get` (slug exact, tout user authentifié) mais **jamais**
  listée (`list` force `include_unlisted=False`) ni servie en anonyme. Partage par lien, pas
  un secret d'org : une doctrine sensible ne se publie pas (reste un skill d'org privé).
- CORS : `oto.ninja`, `app.oto.ninja`, `dashboard.oto.ninja` (+ localhosts dev) — défaut dans `_allowed_origins`, override `OTO_MCP_CORS_ORIGINS`. `account.oto.zone` retiré (surface compte décommissionnée → dashboard.oto.ninja)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## Descriptif OpenAPI — `GET /openapi.json` (aussi `/api/openapi.json`)

**Sans auth**, comme `/api/mcp/catalog` : un descriptif décrit des FORMES, aucune valeur.
**Dérivé à chaque requête** (`openapi.py`) de deux sources — le registre de capacités
(chemin + verbe + description + JSON Schema de l'`Input` pydantic) et la **table de routes
vivante** de l'app pour les routes encore écrites à la main (chemin + méthodes, sans schéma,
taggées `_legacy`). Rien n'est saisi à la main, donc rien ne peut mentir. `/api/admin/*` en
est retiré (console de la plateforme, pas d'intégrateur tiers).

⚠️ **À lire avant de conclure qu'une surface manque.** La consolidation ADR 0047 a déplacé le
verbe dans le CORPS : `POST /api/me/projects {"op":"list"}` n'est pas « créer un projet »,
c'est **toute** la surface projet (list/get/runs/inventory/link/publish_mcp…). Un intégrateur
qui sonde `/api/projects` obtient 404 et conclut « les projets ne sont pas sur REST » — c'est
arrivé (brief scout, 08/2026). Le descriptif rend l'énuméré `op` lisible, ce que le sondage de
chemins ne donnera jamais. Même forme pour `/api/me/docs`, `/api/me/kb`, `/api/resources`.

## Jetons API `oto_` — gestion et portée

- **La gestion des jetons demande une session interactive.** `GET|POST /api/me/tokens`,
  `DELETE /api/me/tokens/{id}` et leurs miroirs admin `/api/admin/users/{sub}/tokens*`
  refusent un porteur de jeton (`403 api_token_forbidden`) : seul un JWT Logto y passe.
  Sinon une fuite est **auto-entretenue** — l'attaquant s'émet un second jeton (non-expirant)
  avant qu'on révoque le premier, et peut révoquer les jetons légitimes. Émettre un jeton
  redevient un acte humain, ce qui borne la gravité réelle d'une fuite à la portée du jeton.
- **Portée opt-in** (`token_scopes.py`, colonne `user_api_tokens.scopes` JSONB) : à la
  création, `POST /api/me/tokens {"label":"scout", "scopes":{"namespaces":{"leads":"read"}}}`
  rend un jeton **porté** — deny-by-default, il n'ouvre QUE les tableaux nommés, en `read`
  ou `write` (write ⊃ read), et **rien d'autre** : ni `/api/me`, ni les connecteurs, ni les
  projets, ni la gouvernance du tableau (créer/supprimer/renommer/partager). Hors portée →
  `403 token_scope_forbidden`. C'est la forme à confier à une intégration tierce ; sans elle,
  un jeton **est** le sub et ouvre toute l'organisation.
  - `scopes` absent ⇒ jeton NON porté = comportement historique. Aucun jeton existant n'est
    touché, aucune migration.
  - Seule réponse **filtrée** plutôt que refusée : `GET /api/datastore/namespaces` rend les
    tableaux de la portée, droits **rabattus** sur ceux du jeton (`permission`/`can_write`/
    `can_govern`) — sans lui une intégration n'aurait pas le schéma de son tableau
    (`page_rows` ne le rend pas) et ne pourrait pas peindre ses colonnes.
  - La table des routes autorisées (`token_scopes._ALLOWED`) est la **seule** porte : une
    route ajoutée demain est refusée sans qu'on ait à y penser.
  - ⚠️ La portée nomme le tableau par son **nom** (ce que l'URL adresse), pas par son id :
    après un renommage, ré-émettre le jeton.

## Descriptif dérivé + jetons portés (03/08)

> **Descriptif dérivé + jetons portés (03/08).** `GET /openapi.json` (et
> `/api/openapi.json`) sert un OpenAPI **dérivé** du registre de capacités + de la table
> de routes vivante (`openapi.py`) — sans auth, `/api/admin/*` exclu. Il existe parce que
> la surface était *indescriptible* : après l'ADR 0047, le verbe vit dans le corps (`op`),
> donc un intégrateur qui sonde `/api/projects` tombe sur 404 et conclut « pas de REST »,
> alors que `POST /api/me/projects {"op":"list"}` sert tout le métier projet. Côté sécurité,
> deux crans sur les jetons `oto_` : leur **gestion** exige une session interactive
> (`allow_api_token=False` sur `/api/me/tokens*` + miroirs admin — un jeton ne fabrique
> plus de jeton, une fuite n'est plus auto-entretenue), et un jeton peut naître **porté**
> (`token_scopes.py`, `user_api_tokens.scopes`) : deny-by-default borné à des tableaux
> nommés en read/write. `scopes` NULL = jeton historique, inchangé. Depuis le 03/08 la
> portée nomme aussi des **projets** (`{"projects": {"12": "read"}}`), servis par
> `GET /api/me/projects/{id}` — la forme POST porte sa cible dans le CORPS, donc aucune
> portée ne peut la borner : **ce qu'un jeton porté atteint doit se lire dans le chemin.**
> C'est la règle à garder en tête avant d'ouvrir une nouvelle surface aux intégrations.

## CORS — la liste du code est MORTE en prod comme en preprod

⚠️ **CORS : la liste du code est MORTE en prod comme en preprod.** `_allowed_origins()`
(`api_routes.py`) n'est qu'un **fallback** — les DEUX box posent `OTO_MCP_CORS_ORIGINS`
dans leur `.env`, qui **écrase** la liste. Ajouter une origine au code, la déployer et
constater que rien ne change est un piège vécu (30/07, front Tulina) : le tag prod avait
été posé pour une raison inexacte. **Ajouter une origine = éditer l'env des deux box +
restart** (`/opt/oto-mcp/.env`, `/opt/oto-mcp-canari/.env`) ; le code ne sert qu'aux
environnements neufs. Diagnostic en 1 appel, sans lire le `.env` : `curl -X OPTIONS
https://mcp.oto.cx/api/mcp/catalog -H 'Origin: <x>'` → l'en-tête `Access-Control-Allow-Origin`
revient si l'origine passe. ⚠️ Ne pas déduire « c'est la liste du code » du seul fait qu'une
origine du défaut est acceptée : l'override en contient une copie.
