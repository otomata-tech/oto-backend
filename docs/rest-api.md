---
title: REST API (consommée par le tableau de bord)
type: reference
description: >-
  Inventaire des endpoints REST /api/* de oto-backend : profil /api/me (billing,
  onboarding, connecteurs), settings LinkedIn/API-keys/tools, guide org
  /api/me/instructions*, palier org (CRUD orgs, membres, secrets, invitations,
  entitlements namespace), admin users/grants/tokens/monitoring, billing Stripe,
  bibliothèque publique de guides (`/api/guide-library`, visibilité public/unlisted).
  Détaille les règles CORS (oto.ninja, app.oto.ninja, dashboard.oto.ninja), l'autz
  (même JWTVerifier ES384 que /mcp, audience mcp.oto.ninja), et les gotchas secrets
  (jamais la clé en réponse, providers per-user refusés en org secrets). À charger
  pour implémenter ou déboguer un endpoint REST ou comprendre le contrat front/back.
---

# REST API (consommée par le tableau de bord)

## Où vit chaque famille (découpe du 2026-08-27)

`api/routes.py` **assemble** et ne contient plus aucun handler : `make_routes` monte les
modules de routes, monte la couche capacité, et rend la table ordonnée des chemins écrits
à la main. L'ORDRE de cette table est un contrat — Starlette prend le PREMIER match, donc
`…/tools/registry` doit précéder `…/tools/{name}`. Elle est figée par
`tests/api/test_api_routes_table_frozen.py` : retirer, ajouter ou réordonner un chemin fait
rouge, et régénérer `tests/api/api_routes_table.txt` EST la déclaration de l'ajout.

✅ **La dette REST vaut ZÉRO depuis le 2026-08-27.** Les 38 routes écrites à la main
qu'il restait sont devenues des capacités en huit lots ; les **36 chemins encore montés
à la main sont tous de NATURE**, chacun avec sa raison écrite dans
`tests/test_rest_modules_are_capabilities.py::_KNOWN` — callback de redirection,
webhook, surface anonyme, API consommée par un programme externe, corps **multipart**,
réponse **non-JSON**. Ce n'est pas le domaine qui tranche, c'est la **forme** : sur
`/api/me/avatar`, le `DELETE` est une capacité et le `POST` multipart n'en sera jamais une.

Le garde-fou ne mesure donc plus une dette, il est devenu un **cliquet**
(`test_rest_debt_stays_at_zero`) : une route neuve écrite à la main a **deux issues, et
deux seulement** — naître capacité, ou être classée `NATURE` **avec sa raison**. La
classer `DEBT` rouvrirait ce qui vient d'être fermé : c'est un acte, pas un réglage.
Avec `test_no_new_handwritten_rest_route`, qui refuse toute route absente de `_KNOWN`,
il devient impossible d'ajouter une route à la main sans le déclarer.

| famille | où | régime |
| --- | --- | --- |
| ~200 chemins générés (**le compte** `/api/me`, **la toolbox** `/api/me/tools*`, **la session navigateur**, **la messagerie hébergée**, **les verbes OAuth fédérés**, **les jetons API**, **les fichiers de projet**, projets, pages, procédures, ressources, orgs, guide, monitoring, datastore, billing…) | `capabilities/` + `_rest_adapter` | **capacité** : un descripteur, deux faces (ADR 0009/0042) |
| primitives (`_authenticate`, CORS, `_json`/`_json_error`, `OPTIONS`, `bind`) | `api/base.py` | partagées par tous les modules ; **ré-exportées** par `api.routes` |
| favicon, `/api/version`, `/api/mcp/catalog`, `openapi.json`, `/api/connectors`, bibliothèques de procédures & de guides, aperçu d'invitation, docs partagés (`/api/public/docs/{token}`, `/p/d/{token}`) | `api/public.py` | **sans auth** — l'adaptateur capacité authentifie toujours. ⚠️ `/api/connectors` est la seule **MIXTE** : anonyme pour la vitrine, authentifiée pour le dashboard, et depuis le 2026-09-01 (#732) l'en-tête **change ce qu'elle rend** — org de contexte ⟹ `auth.cardinality` effective |
| `POST /api/me/avatar`, `POST /api/orgs/{id}/logo` | `api/media.py` | **multipart** → hors du moule par CONSTRUCTION (classé `NATURE`) |
| `POST` d'un fichier de projet, `/api/me/projects/{id}/export` | `api/projects.py` | **multipart / ZIP** → hors du moule (classé `NATURE`) |
| `/api/upload/{token}` (PUT/POST/GET) | `api/uploads.py` | **pas de JWT** : le jeton de l'URL fait foi |
| SIRENE (`api/sirene.py`), accords (`api/accords.py`), webhook Mollie (`api/billing.py`), **callbacks OAuth** zoho/google/atlassian/folk/salesforce (`api/{zoho,datastore,atlassian,folk,salesforce}.py`) | `api/<nom>.py` (antérieurs à la découpe) | gardent leur patron : `make_routes(...)` reçoit les primitives en paramètres. ⚠️ **Le datastore n'y est plus** depuis le 2026-08-12 (#302) : ses 24 chemins sont des capacités (bloc ci-dessous) ; `api/datastore.py` est un nom vestige qui ne porte QUE le callback Google |

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
- `POST|DELETE /api/me/avatar` — upload (multipart `file`, png/jpeg/webp ≤ 2 Mo) / efface
  l'avatar user → Scaleway Object Storage, URL publique en DB. ⚠️ **Les deux verbes ne
  vivent plus au même endroit** : le `DELETE` est la capacité `me.avatar.clear` depuis le
  2026-08-27 ; le `POST`, multipart, reste écrit à la main et **classé `NATURE`** —
  l'adaptateur lit du JSON, un corps binaire est hors du moule par construction. C'est la
  FORME qui tranche, pas le domaine. Effacer purge aussi l'objet stocké (pas d'orphelin).
- `GET|DELETE /api/me/projects/{project_id}/files` + `POST …/files/{file_id}/public` —
  **les fichiers bruts d'un projet**, capacités `me.project_file.{list,delete,set_public}`
  depuis le 2026-08-27 (`capabilities/media_and_files.py`). Le **dépôt** (`POST …/files`,
  multipart) et l'**export ZIP** (`GET /api/me/projects/{id}/export`) restent écrits à la
  main, classés `NATURE`. ⚠️ **`s3_key` ne sort jamais** : chaque ligne rend une
  `download_url` **signée et temporaire** (elle expire — ne pas la mettre en cache),
  tandis que `public_url` est permanente tant que le partage est ouvert. Un stockage muet
  rend `download_url: null` sans faire disparaître la ligne. Lecture bornée à l'org de
  **consultation** (ADR 0023) : un projet visible via une AUTRE de mes orgs rend `404
  unknown_project`, non-disclosante. ⚠️ **`public` est désormais REQUIS** sur la bascule
  de partage : le handler d'origine traitait un corps sans `public` comme « rendre
  privé », donc un client mal formé **départageait un fichier en silence, avec un 200**.
  Un corps sans `public` (ou illisible) rend maintenant `400 invalid_input` — on refuse
  plutôt qu'on agit.
- ⚠️ **Duplication assumée** entre `me.project_file.{list,delete}` (REST, dashboard) et
  `me.project_files` (MCP `oto_project_files`, agent) : les RÉPONSES sont les mêmes, ce
  qui diffère est la forme des REFUS (la face MCP joint un message à ses 404) et l'entrée
  (`op` + `project_id` contre des paramètres de chemin). Les fusionner changerait les
  corps servis au dashboard — même famille de décision que la toolbox (oto-backend#429).
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
  un détail actionnable). ⚠️ **Corrigé le 2026-08-27** : ce paragraphe annonçait qu'un
  corps JSON **malformé** rendait `400 missing_params` — l'adaptateur de capacités
  l'avalait alors comme un corps absent, et c'était le site B4 de l'inventaire des
  silences. Il rend maintenant `400 invalid_json` (corps illisible) ou `400 invalid_body`
  (JSON valide mais pas un objet), sur les ~200 routes générées.
  `missing_params` accusait l'appelant de n'avoir rien envoyé alors qu'il avait envoyé
  quelque chose d'inexploitable.
- `GET|POST|DELETE /api/settings/api-keys/{provider}` — **ton credential** pour un
  connecteur, dans l'org de contexte (capacités `me.credential.{get,set,clear}` depuis
  le 2026-08-27 ; **pas de face MCP** — un secret brut ne passe pas en argument d'outil).
  ⚠️ **RUPTURE DE CONTRAT le 2026-08-31 (#671) : le `GET` ne rend plus la valeur d'un
  champ secret.** Il la rendait en clair pour tout champ déclaré `reveal=True` — le
  défaut de 55 connecteurs. Sa clé est désormais **ABSENTE du corps** (pas `null`, pas
  `""` : un champ vidé se lirait « rien de posé » et un appelant continuerait sur du
  vide). À la place, de quoi reconnaître la clé sans la lire : `configured`,
  `read_set_at`, `read_set_by`, et `read_fingerprints` — `{champ: 4 caractères}`, HMAC
  lié à la ligne du coffre, jamais des caractères du secret. Les champs **non secrets**
  (`base_url`, `auth_mode`, `region`, email) continuent de sortir tels quels, donc la
  modification partielle de #448 est intacte. Demander la valeur (`?reveal=1`) rend
  `403 secret_never_revealed`. **Pour changer une clé, on la repose** — le POST
  conserve les champs qu'il ne reçoit pas.
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
- `GET /api/me/instructions` (index des procédures ; le readme d'org est un guide `delivery=init`, plus servi ici) + `GET|PUT|DELETE /api/me/instructions/{slug}` + `GET /api/me/instructions/{slug}/versions` + `POST /api/me/instructions/{slug}/revert` — procédures de l'**org active** (le slug `claude_md` est RÉSERVÉ au readme et refusé ici) (cf. §Guides). Lecture = membre ; écriture = `org_admin` (ou platform admin). Édité par le dashboard (`/procedures`). ⚠️ Le bundle sert **trois** droits de l'appelant : `can_edit` (administrer l'org) et — depuis la suite de #695 — `can_write_instructions` / `can_delete_instructions`, un par verbe. Ce sont ces deux-là que les boutons d'une procédure lisent, ici comme sur `GET /api/groups/{id}/instructions` où ils ne valent PAS la même chose (membre pour écrire, chef pour supprimer). ⚠️ Le `PUT` renvoie un **`diagram_warning`** (toujours présent ; `null` = rien à signaler) quand le corps n'embarque pas le SCHÉMA requis de la procédure — non bloquant, comme `unresolved_tools`/`slot_warnings` (cf. §Guides).
- `GET|PUT|DELETE /api/me/guides/{scope}/{slug}` (+ `GET /api/me/guides`) — **la prose d'instruction**, un seul primitif sur deux axes (ADR 0042 §Convergence des surfaces) : `scope` ∈ platform|org|group|user × `delivery` ∈ `on-demand` (défaut, un how-to chargé au besoin) | `init` (**readme injecté à chaque session**, slug canonique `readme` — corps vide = couche effacée). Miroir REST d'`oto_guide`, mêmes handlers. Écriture gatée par scope (platform_admin / org_admin / chef d'équipe / self). Variantes par-id pour viser une org/équipe précise plutôt que l'active : `GET|PUT /api/orgs/{id}/guides/{scope}/{slug}` et `/api/groups/{id}/guides/{scope}/{slug}`. *(Remplace `GET|PUT /api/me/agent-readme`, retiré le 2026-07-28.)*
- `POST|DELETE /api/me/projects/{id}/public-share` — **partage public CHIFFRÉ** d'un projet (ADR 0032 §3, zero-knowledge). Le dashboard chiffre le snapshot (brief + pages) côté navigateur et POSTe uniquement `{ciphertext}` ; renvoie `{token, public_base_url}`. Écriture = `ownership.can_access(project, write)`. La clé de déchiffrement n'atteint JAMAIS le serveur (fragment d'URL).
- `GET /api/public/projects/{token}` — **sans auth** : renvoie `{ciphertext, updated_at}` du snapshot chiffré. Déchiffrement côté navigateur (route `/p/p/{token}#<clé>`). Pendant public de `GET /api/public/docs/{token}` (#4a).
- `PUT|POST|GET /api/upload/{token}` — **réception d'un upload signé out-of-bande** (issue #105), **pas de JWT** : le `{token}` est un jeton HMAC scellant `(sub, org, cible)` + TTL court + usage unique (émis par `oto_upload_url`, module `upload_tokens.py`). **PUT** = un agent avec shell y pousse le corps brut (`curl --data-binary @fichier`) ; **POST** multipart `file` = le formulaire humain ; **GET** = page HTML d'upload autoportée (fallback quand l'agent n'a pas de shell, ex. claude.ai : il transmet le lien à l'humain — le jeton n'est PAS consommé au GET). Le backend matérialise dans la cible en **réappliquant** son autz, consomme le jeton (anti-rejeu), renvoie un **accusé léger** (id + compteurs), jamais le body. Cibles : page Documents (`doc`), fichier brut de projet (`project_file`, autz `ownership.can_access(project, write)`), lot de lignes datastore (`datastore` — NDJSON/CSV batch-upsert sur clé, autz `ownership.can_access(datastore_namespace, write)`, ns_id scellé au mint). Évite de faire transiter du gros contenu par le contexte du LLM.
- `POST /api/datastore/namespaces/{ns}/rows` — **UNE ligne** : le corps EST la ligne (un objet, une clé par colonne ; capacité `me.datastore.append_row`, 201). **Il n'y a pas de lot JSON sur REST** : un corps dont l'unique clé porte une liste d'objets est refusé `400 batch_body` — sans schéma il faisait une ligne imbriquée en 201 muet, sous `strict: true` une ligne imbriquée avec relevé `hors_schema` (oto#48, mesuré le 04/09/2026) ; une colonne DÉCLARÉE sous ce nom reste écrivable. Le lot = `data_write(rows=[…])` côté agent, ou l'upload signé NDJSON/CSV ci-dessus pour les volumes. Garde : `tests/datastore/test_lot_dans_ligne_oto48.py`.
- **Datastore — les 24 chemins sous `/api/datastore/namespaces`** (= `NS` ; relevés le 2026-09-05 depuis l'`openapi.json` **servi** : `curl -s https://mcp.oto.ninja/api/openapi.json`), tous capacités `me.datastore.*` de `capabilities/datastore/` — aucune route écrite à la main depuis le 2026-08-12 (#302). `{namespace}` = le nom du tableau, `slot:<nom>` et `@claimed` compris (même résolution que la face agent, sinon `400 jeton_mal_place`) ; un tableau d'une autre de mes orgs rend `404 namespace_not_found` **avec l'org où il vit** (rejouer avec `X-Oto-Org`).
  - **tableaux** : `GET NS` (possédés + partagés ; seule réponse *filtrée* par la portée d'un jeton porté) · `POST NS` (créer) · `PATCH NS/{namespace}` (renommer — id, URL et partages stables) · `DELETE NS/{namespace}` (gouvernance) · `GET NS/{namespace}/url` (deep-link dashboard) ;
  - **lignes** : `GET NS/{namespace}/rows` (page `offset` + `limit` ≤ 500, `total` du jeu filtré, **pas de curseur** — la fin se calcule, un `offset` au-delà rend `rows: []` en 200 ; `filter`/`filters` = JSON dans UNE chaîne de query ; **pas de `fields`**, la ligne entière) · `POST …/rows` (UNE ligne, 201, ci-dessus) · `GET|PATCH|DELETE …/rows/{row_id}` (`?readonly_override=true` sur `POST`/`PATCH`, propriétaire ou gouvernant, journalisé) ;
  - **schéma** : `GET|PUT|PATCH NS/{namespace}/schema` (`PUT` pose, ou retire avec `schema: null` ; `PATCH` par clé) · `POST NS/{namespace}/drop_column` (destructif, `confirm=true`) ;
  - **file de travail** : `POST NS/{namespace}/claim_next` · `POST …/rows/{row_id}/claim` (409 si bail d'un autre) · `POST …/rows/{row_id}/release` (gardée avec `worker`, forcée sans) · `GET NS/{namespace}/queue` (lignes sous bail, lecture seule) ;
  - **agrégat** : `GET NS/{namespace}/aggregate` (`group_by` = UNE colonne ; `"a,b"` refusé `400 invalid_aggregate`, oto#50 — la forme liste n'existe que sur `data_aggregate`) ;
  - **partage** : `GET|POST|DELETE NS/{namespace}/share` (gouvernance) ;
  - **activité** : `GET NS/{namespace}/activity` · `GET …/rows/{row_id}/activity` — **sans face MCP** (opt-out explicite : lecture de cockpit).
  Les couches d'une colonne (`comment`/`link`/`origine`) sont servies **à plat** par défaut (`champ.origine`) sur les deux faces ; `?layers=nested` sur `GET …/rows` et `GET …/rows/{row_id}` rend la forme d'écriture `{valeur, + couches renseignées}` (oto#53 ; autre valeur → `400 invalid_layers` ; le défaut basculera vers `nested` avec préavis daté) ; les refus sont nommés (`row_invalid`, `business_key_required`, `invalid_row_input`, `batch_body`, `unknown_fields`, `403 namespace_read_only`, `404 row_not_found`). La sémantique complète et les divergences MCP/REST sont dans le guide servi `datastore-semantics` (oto#51).
- `POST /api/me/unipile/connect` + `POST …/reconcile` + `GET|DELETE /api/me/unipile` —
  **la messagerie hébergée côté membre**, capacités
  `me.unipile.{connect,reconcile,status,disconnect}` depuis le 2026-08-27
  (`capabilities/unipile_me.py`). **Il n'y a plus de webhook de liaison** :
  `POST /api/unipile/webhook` a été retiré le 2026-08-29 (#581, dormant depuis la v2
  du fournisseur — zéro appel sur les 31 jours de journal retenus) ; la liaison passe
  par la réconciliation, sous le JWT de la personne.
  ⚠️ **`connect` a DEUX formes de succès** : `{url}` d'ordinaire, et
  `{adopted, channel, account_name}` **sans `url`** quand le compte était déjà connecté
  sous la même identité dans une autre org — il vient d'être rattaché ici, il n'y a rien
  à consentir, le front doit rafraîchir. ⚠️ **Les refus `409` et `502` servent leur
  MESSAGE dans `error`**, pas leur code machine (forme historique conservée : le front
  affiche `error` tel quel, et pour ces deux-là le message est ce qui est actionnable) ;
  les autres statuts exposent bien leur jeton.
  `GET /api/me/unipile` **réconcilie avant de répondre** (self-heal : c'est LE chemin
  de liaison) — best-effort, jamais fatal pour le statut, no-op sans pending. ⚠️ Son champ `channels` ne montre QUE les comptes liés à l'org **courante** :
  un canal vu déconnecté peut l'être ailleurs, et `elsewhere` dit alors ce qui est
  adoptable ici en un clic. `DELETE` est une **soft**-déconnexion, par org et par canal
  (`?channel=`, défaut `linkedin`) : le compte survit chez le fournisseur et la ligne
  survit comme preuve de propriété, ce qui rend la reconnexion déterministe.
  **Pas de face MCP** : la face agent de ce geste est `me.connector_connect`
  (`POST /api/me/connectors/{name}/connect`), qui **supersède** `…/unipile/connect` —
  celui-ci vit jusqu'à la bascule du front.
- `GET /api/{atlassian,folkmcp,google}/oauth/start` + `…/oauth/status` +
  `DELETE /api/{atlassian,folkmcp,google}/oauth` + `POST /api/google/oauth/default` —
  **les VERBES du consentement OAuth per-user**, capacités
  `me.federation.{atlassian,folkmcp,google}.*` depuis le 2026-08-27
  (`capabilities/federated_oauth.py`). Les **callbacks** (`…/oauth/callback`, un par
  fournisseur) restent écrits à la main et le resteront : le fournisseur y redirige le
  NAVIGATEUR (302, sans en-tête d'auth), or l'adaptateur authentifie toujours et répond
  en JSON — hors du moule par construction.
  ⚠️ **LA convention de retour, après ce consentement, est UNIQUE depuis
  oto-backend#670** : `?connector=<nom>&connect=connected|error|forbidden`, généralisée
  depuis la forme salesforce et fabriquée une seule fois
  (`auth.flow.connector_return_suffix`/`connector_return_url`) plutôt que composée à la
  main dans chaque callback — c'était le cas avant ce lot, avec cinq formes différentes
  et deux replis cassés (une f-string à accolades doublées sur atlassian/folk, qui
  rendait une chaîne littérale au lieu d'une URL). `zoho` et `google` servaient déjà un
  suffixe LU par le dashboard (`?zoho=connected`, `?google=connected`) : il coexiste
  avec le neuf dans la MÊME redirection, le temps d'un préavis distinct de celui du
  renommage doctrine→guide (`deprecations.ANNONCE_RETOUR_OAUTH`/`RETRAIT_RETOUR_OAUTH`,
  `docs/alias-deprecies.md`) — **la date de retrait n'est PAS encore fixée** : elle ne
  peut être posée qu'au tag qui met ce lot en production (main = preprod), pas avant ;
  tant qu'elle est absente, le doublage reste actif sans discontinuer. `atlassian` et
  `folkmcp` gagnent `connect=` en pur ajout (leur `connector=` déjà servi ne bouge pas) ;
  ils ne servaient AUCUNE distinction succès/échec avant ce lot (le repli cassé rendait
  toujours la même destination), donc rien n'y avait de lecteur à préserver.
  **Deux familles, pas trois.** `atlassian` et `folkmcp` fédèrent un MCP distant (jeton
  per-user au coffre, injecté par `tools/mount.py`) : leur surface est identique au champ
  près, et cette symétrie est **contractuelle** — le dashboard les pilote par un client
  GÉNÉRIQUE (`/api/${name}/oauth/…`). `google` est à part : multi-compte, donc un statut
  plus riche et un verbe de plus.
  ⚠️ **Les champs racine de `google/oauth/status` (`granted_at`, `scopes`) décrivent le
  compte PAR DÉFAUT**, pas l'union des comptes — héritage du mono-compte ; la vérité
  multi-compte est `accounts`. Sans défaut posé, la racine est vide alors que `connected`
  est vrai. ⚠️ **`DELETE /api/google/oauth` SANS `?account=` révoque TOUS les comptes** —
  `account: null` en réponse veut dire « tous », pas « aucun ». Un `500
  oauth_misconfigured:` signale une app OAuth mal configurée côté PLATEFORME, pas une
  erreur de l'appelant.
  ⚠️ **Ces chemins NOMMENT leur connecteur**, ce que `test_connector_flow.py` interdit
  depuis que zoho et salesforce sont passés au chemin fixe (v1.19.0). Ils y sont tolérés
  NOMMÉMENT : les vider suppose que le widget de fédération du dashboard cesse de
  construire son URL à partir du nom du connecteur — **dette de front**, pas de backend.
  **Pas de face MCP** : ouvrir une page de consentement demande un navigateur, et le
  pendant agent générique existe (`me.connector_connect`).
- `GET /api/me/connectors/{name}/oauth-status` + `DELETE /api/me/connectors/{name}/oauth`
  — **le statut et la déconnexion OAuth GÉNÉRIQUES**, capacités
  `me.connector_status`/`me.connector_disconnect` (`capabilities/connectors/oauth_status.py`,
  depuis le 2026-09-04, oto-dashboard#125 items 2/3) : le chemin fixe qui ne nomme pas
  le connecteur, symétrique de `me.connector_connect` — `{name}` ∈ atlassian, folkmcp,
  google (les seuls connecteurs OAuth fédérés) ; un autre nom rend `400 no_oauth_status`.
  ⚠️ **`me.federation.*` ci-dessus RESTE en place** : le retrait est un lot séparé, une
  fois le dashboard basculé sur ces deux-là (même discipline que #519/#670).
  **Contrainte 1** (bloquante) : `me.connector_status` dérive `{connected, set_at,
  health_ko, health_reason}` de la MÊME lecture que `/api/me` (`access.status_for`),
  JAMAIS d'un second appel à `atlassian_oauth.status_for`/`folk_oauth.status_for`/
  `google_oauth.list_accounts` qui pourrait diverger. ⚠️ Pour **google** spécifiquement,
  `access.status_for` ne porte qu'UNE identité par défaut (mono-compte hérité) : ce
  contrat commun ne rend donc RIEN de spécifique à google au-delà de ces quatre champs —
  forcer un `accounts` ici recréerait la seconde vérité que la contrainte interdit. La
  richesse multi-compte reste `connectors.identities` (op=list), hors de ce lot.
  **Contrainte 2** (décision d'Alexis) : `me.connector_disconnect` est **irréversible,
  en UN SEUL appel** — révoque chez le fournisseur quand le mécanisme le permet
  (reprend EXACTEMENT `federated_oauth._federation()._disconnect`/`._google_revoke`) et
  DANS TOUS LES CAS retire la ligne locale, jamais d'état intermédiaire « en attente de
  confirmation ». Sortie `FederationDisconnected{ok, disconnected}`, réutilisée telle
  quelle. **Pas de face MCP** sur les deux (`mcp=None`, comme `me.federation.*`).
- `GET|POST|DELETE /api/admin/connectors/activation` + `GET|POST /api/admin/connectors/{provider}/platform-access`
  — **le palier PLATEFORME des connecteurs**, capacités
  `platform.connector.{activation_list,activation_set,activation_clear,access_list,access_set}`
  depuis le 2026-08-27 (`capabilities/platform_connectors.py`). C'était l'étage qui
  manquait : les paliers **org** et **équipe** de la même famille étaient déjà des
  capacités (`connectors.activation.{set_org,set_group}`).
  **Activation** (ADR 0010 B4) : le code DÉCLARE les connecteurs, la DB décide lesquels
  sont EXPOSÉS. ⚠️ `enabled: null` = **OFF** (jamais posé, deny-by-default), pas
  « indéterminé ». ⚠️ **Le master GLOBAL ne prend effet qu'au prochain redémarrage** (le
  chargement des tools est résolu au boot) — `restart_required` le dit ; un **override
  d'org** prend effet tout de suite. Refus : `400 unknown_connector`,
  `400 enabled_must_be_bool`, et sur le `DELETE` (query `?connector=&org_id=`)
  `400 connector_and_org_id_required` / `400 org_id_must_be_int`.
  **Accès plateforme** (ADR 0044 §H) : vue connecteur-centrique unique qui remplace les
  leviers dispersés `/platform/orgs` et `/platform/users`. Le `POST` est un **acte
  unique** — il pose ENSEMBLE l'option offerte (couche 3) et le grant de clé plateforme
  (couche 2), ce que le backend couplait déjà. Aucun secret n'en sort. ⚠️ Si
  `open_tier` est vrai, une instance en partage `open` sert le connecteur à **tous sans
  grant** : `beneficiaries` ne dit alors plus la population servie. Lecture =
  `PLATFORM_ADMIN`, écriture = `SUPER_ADMIN`. Refus : `404 unknown_connector`,
  `400 invalid_body`, `404 unknown_org`/`unknown_user`, `400 no_platform_access`.
  **Pas de face MCP** : basculer le master global est un acte de déploiement, ouvrir un
  accès est un acte commercial — les paliers qu'un utilisateur pilote (org, équipe) sont
  déjà servis par `oto_connector_activation`.
- `GET /api/admin/users` + `POST /api/admin/users/{sub}/role` — admin only
- `POST /api/admin/users/{sub}/reset-mfa` — super admin only ; efface TOUS les facteurs de
  double authentification d'un compte (récupération : appli ET codes de secours perdus,
  aucun autre moyen de rentrer). La politique MFA de son org, si mandatoire, continue de
  s'appliquer — il configurera un facteur neuf à sa prochaine connexion.
- `POST /api/admin/users/{sub}/grants/{key_id}` body `{daily_quota}` — set/update quota par grant (admin only)
- `GET|POST /api/me/tokens` + `DELETE …/{token_id}`, `GET|POST /api/admin/users/{sub}/tokens`
  + `DELETE …/{token_id}`, `GET|POST /api/admin/platform-keys` + `DELETE …/{provider}/{label}`
  — **les jetons API et les clés plateforme**, capacités `me.token.*`,
  `platform.token.*` et `platform.key.{list,create,delete}` depuis le 2026-08-27
  (`capabilities/api_tokens.py` ; `api_routes_admin.py` supprimé).
  ⚠️ **Les six routes de jetons portent `RestBinding.allow_api_token=False`** : un jeton
  `oto_` y est refusé (`403 api_token_forbidden`). Un jeton qui peut en créer d'autres
  rend sa fuite auto-entretenue — révoquer le jeton fuité ne suffit plus. C'est ce cran,
  absent de l'adaptateur jusque-là, qui retenait ces routes en écriture manuelle ; il
  vit désormais sur le binding, et un test le vérifie en JOUANT les six routes (pas en
  relisant le descripteur : c'est son application qui est la garde).
  ⚠️ **Le secret d'un jeton n'est rendu QU'À LA CRÉATION** — il n'est stocké que haché.
  `scopes: null` = jeton non porté (pleins pouvoirs du sub) ; sinon il est borné à des
  tableaux/projets nommés (`auth.token_scopes`). **Trois asymétries membre/admin conservées** :
  la création membre rend **201** et l'admin **200** ; le `DELETE` membre rend `{ok}` et
  l'admin `{ok, id}` ; seul le palier MEMBRE refuse un tableau que l'émetteur ne voit pas
  (`400 unknown_namespace` — sinon le jeton serait muet et on le croirait branché), et
  seul le palier ADMIN accepte un `ttl_days` (qui n'est retenu que s'il est fait de
  chiffres : `-1` ou un texte donnent « pas d'expiration »).
  Les **clés plateforme** (ADR 0044 §F) ne rendent jamais leur secret — provider, libellé,
  date de pose. Refus : `400 invalid_provider`, `400 missing_fields`,
  `400 invalid_platform_provider`, `404 unknown_key`. ⚠️ Un corps JSON **illisible** rend
  désormais `400 invalid_provider` et non `400 invalid_json` (même statut).
  **Aucune face MCP** sur les neuf : la garde des jetons n'aurait aucun sens si un outil
  faisait le même geste, et `api_key` est un secret brut.
- `GET /api/admin/monitoring/summary?days=` + `GET /api/admin/monitoring/calls?limit=&sub=&tool=&errors=&days=` — journal des appels MCP, agrégats + brut (admin only, cf. §Monitoring)
- `GET /api/orgs/{id}/monitoring/{summary,calls,calls/{call_id},connectors,adoption,runs,runs/{run_id},gaps,tool-quality}` — **les mêmes lentilles au niveau ORG** (`capabilities/org_monitoring.py`, autz `ORG_ADMIN_OF`, face MCP `oto_org_monitoring`). Scope = `tool_calls.org_id`/`usage_signals.org_id` (ce qui a été émis SOUS l'org), jamais l'appartenance des membres. `adoption` n'existe qu'à cet étage (membre par membre : actif / jamais actif / bloqué par un connecteur). ⚠️ `calls/{call_id}` et `runs/{run_id}` rendent **404** hors de l'org (id séquentiel devinable). Sert la page dashboard `/org/monitoring`. Cf. `docs/monitoring.md` §Trois étages. ⚠️ `calls` rend aussi `scope` / `hors_scope` / `hors_scope_hint` : les appels des runs de l'org résolus sous une AUTRE org (axe `_org` absent) ne sont pas dans `calls` — le compte l'est (#630). ⚠️ `calls` rend par ligne `arg_keys` (les CLÉS des arguments, triées, `[]` sans argument — jamais une valeur) et pas `args` ; `calls/{call_id}` rend `call.args` **tels que journalisés** (tronqués, jetons masqués #582, `null` sans argument). Aucune clé `arguments` n'existe sur aucune des deux (#634, 2026-08-30). ⚠️ `runs/{run_id}` sert les **mêmes** arguments sous la même règle (même colonne, même voie d'écriture — le contrat de `RunCall.args` le dit depuis le 01/09/2026). La borne est de **300 caractères par valeur** plus une ellipse, les valeurs composées étant stringifiées AVANT d'être coupées ; un argument déclaré secret part en empreinte `#` + 12 hexadécimaux. ⚠️ **Forward-only** : 112 lignes écrites entre le 14/08 et le 01/09/2026 par la trace du dispatch universel dépassent cette borne (jusqu'à 4 383 caractères, 82 servies dans une timeline) — elles sortent de la fenêtre de rétention (90 j) mi-novembre. Un affichage se borne de son côté.
- `GET /api/admin/tenants?days=` + `GET /api/admin/tenants/{slug}?days=` — **suivi de l'étage tenant** (ADR 0052 ; `capabilities/tenants_admin.py`, autz `PLATFORM_ADMIN`, face MCP `oto_admin_tenant` op=list|get). Par tenant : configuration d'annuaire (émetteur, jwks, hosts, client OAuth, dashboard, chemins de lien), **état dans le process** (`loaded` / `pending_restart` — le registre d'émetteurs est bâti AU BOOT, donc un tenant déclaré depuis n'authentifie encore personne), et l'empreinte sur la fenêtre (orgs via `orgs.tenant_id`, comptes via la qualification du sub, comptes actifs, appels MCP). ⚠️ Ces deux rattachements sont des sources INDÉPENDANTES : `orgs_desalignees` compte les orgs du tenant créées par un compte relevant d'un autre, et la fiche en donne la liste. **Lecture seule** — déclarer un tenant est un runbook de provisioning, pas une route. Sert la page dashboard `/platform/tenants`.
- **Palier org** (100 % en CAPACITÉS — `capabilities/orgs*.py` ; ⚠️ cette ligne renvoyait à `api_routes_orgs.py` jusqu'au 2026-08-27, fichier **supprimé** lors de la migration : projection 1:1 des meta-tools `oto_admin_*org*` / `oto_list_orgs`) :
  - self-service : `GET|POST /api/me/orgs` (**`POST` = `org.create` self-serve**, créateur→org_admin, cap `OTO_MCP_MAX_ORGS_PER_USER`) ; `GET /api/orgs/{id}` ; `POST|DELETE /api/orgs/{id}/members[/{sub}]` + `PUT|DELETE /api/orgs/{id}/secrets/{provider}` (org_admin)
  - **invitations — feature cascade plateforme/org/équipe** (le scope est DÉRIVÉ des cibles : org_id NULL = plateforme, org_id seul = org, org_id+group_id = équipe). Trois faces émettrices, une seule acceptation :
    - **org** : `POST|GET /api/orgs/{id}/invitations` + `DELETE …/{inv}` (org_admin ; `oto_org` op=invite). Le POST refuse en 409 une adresse déjà membre (`already_member`) ou déjà invitée avec une invitation valide (`already_invited`, l'existante dans `details`) — #622, 29/08/2026.
    - **équipe** : `POST|GET /api/groups/{id}/invitations` + `DELETE …/{inv}` (group_admin ; `oto_group` op=invite). L'invité rejoint l'org parente PUIS l'équipe à l'acceptation.
    - **plateforme** : `POST|GET /api/admin/invitations` + `DELETE …/{inv}` (platform_admin ; `oto_admin_invite` op=create/list/revoke). `org_id` optionnel (vide = onboarding pur, sinon rattachement direct).
    - **acceptation commune** : `POST /api/me/invitations/accept` (`SUB_ONLY`, token/code + expiry). ⚠️ **Modèle BEARER** : le secret suffit, l'identité de l'accepteur n'est PAS confrontée à l'email invité — cette ligne a longtemps dit « match email vérifié », ce qui était faux du code servi (corrigé le 2026-09-01, cf. le commentaire de `_invite_accept`). Email via `oto_mcp/email.py` (otomata-mailer `mailer.oto.zone/api/send`, env `OTO_MAILER_SEND_BEARER`, best-effort → `invite_url` en repli ; **plus de Resend**).
    - **refus commun** : `POST /api/me/invitations/reject` (`SUB_ONLY`, token/code — mêmes entrées que l'acceptation ; `oto_org` op=reject_invite), depuis le 2026-09-01 (#654). Ferme l'invitation (`org_invitations.declined_at`/`.declined_sub`) **sans créer ni retirer aucune appartenance** : elle quitte l'inbox de l'invité, la file de l'émetteur, et plus rien ne peut la consommer — ni `accept`, ni la reprise automatique au signup par l'email (`reconcile_signup_with_invitation`). ⚠️ **Le refus n'est PAS bearer, contrairement à l'acceptation** : l'invitation doit être adressée à l'email du compte appelant (403 `not_the_invitee` sinon, invitation anonyme comprise) — accepter avec un secret qu'on détient est un geste sur soi, refuser détruirait l'invitation d'un tiers. Le refus **ne notifie pas** l'émetteur (aucune préférence de notification n'existe encore) et **ne bloque pas** une réinvitation : le 409 `already_invited` de #622 ne voit plus une invitation refusée.
  - **fiche admin user** : `GET /api/admin/users/{sub}` = identité + accès effectif par provider (`status_for`) + grants + namespaces + orgs (membership).
  - platform admin : `GET|POST /api/admin/orgs`, `GET /api/admin/orgs/{id}` (+ entitlements), `…/members*`, `…/secrets/{provider}`, `POST|DELETE /api/admin/orgs/{id}/entitlements/{namespace}`, `GET /api/admin/namespace-grants`, `POST|DELETE /api/admin/users/{sub}/namespace-grants/{namespace}`
  - secrets : jamais la clé en réponse (provider/base_url/set_at/set_by) ; providers per-user (slack/linkedin/google/whatsapp) refusés en `400` ; listing lu du coffre canonique `credentials_store` (legacy `org_secrets` plus dual-written sous chiffrement). Gating org_admin/membre via `org_store.get_org_role` (platform admin toujours autorisé). Révocation lazy sur sessions MCP ouvertes. Contrat front : `oto-app/docs/ORG_API_CONTRACT.md`.
- **Bibliothèque publique de guides** (marketplace de skills, table `doctrine_library`) :
  capacités `library.*` (`capabilities/guide_library.py`, montage auto MCP+REST) —
  `library.list/get` (`SUB_ONLY`, MCP `oto_procedure` op=library_list/library_get + REST
  `GET /api/me/guide-library[/{slug}]`), `library.publish`/`library.fork` (`ORG_MEMBER` +
  gate org_admin en handler, MCP `oto_procedure` op=publish/fork + REST
  `POST /api/me/guide-library/{publish,fork}`), `library.unpublish` (auteur/PLATFORM_ADMIN,
  `DELETE /api/me/guide-library/{id}`). **Auteur** = `otomata` si publieur platform-operator,
  sinon l'`org`. **Fork** réutilise `org_store.set_instruction` → skill d'org versionné. Surface
  ANONYME pour la vitrine : routes écrites à la main `GET /api/guide-library[/{slug}]`
  (deny-by-default `visibility='public'`, l'adaptateur capacité authentifie toujours).
  ⚠️ **`/api/guide-library` ≠ `/api/guides/library`** : le premier est le MARCHÉ des guides
  publiés par les orgs (forkables), le second les guides PLATEFORME. Deux objets, deux
  tables — la ressemblance des noms est ancienne, elle ne dit pas une parenté.
  ⚠️ Ces chemins s'appelaient `/api/[me/]doctrines/…` jusqu'au 2026-08-28 (#519) ; les
  anciens répondent **308** jusqu'au retrait — cf. `docs/alias-deprecies.md`.
  **`visibility`** : `public` (dans le catalogue) vs `unlisted` = **lien non listé** (style
  YouTube) — servie par `library.get` (slug exact, tout user authentifié) mais **jamais**
  listée (`list` force `include_unlisted=False`) ni servie en anonyme. Partage par lien, pas
  un secret d'org : un guide sensible ne se publie pas (reste un skill d'org privé).
- CORS : `oto.ninja`, `app.oto.ninja`, `dashboard.oto.ninja` (+ localhosts dev) — défaut dans `_allowed_origins`, override `OTO_MCP_CORS_ORIGINS`. `account.oto.zone` retiré (surface compte décommissionnée → dashboard.oto.ninja)
- Même `JWTVerifier` que `/mcp` — partage l'audience `https://mcp.oto.ninja/mcp`

## Surface nœuds (PROVISOIRE) — `/api/me/shell`, `/api/me/nodes/{id}`, `/api/me/nodes/{id}/rows`

Trois lectures précoces du modèle de nœuds (`capabilities/shell.py`, `node_view.py`,
`node_rows.py` ; faces MCP `oto_shell`, `oto_node`, `oto_node_rows`), marquées
`x-oto-provisoire` dans l'OpenAPI : les FORMES se contractent, le stockage reste variable.
`type` est une NATURE dérivée d'un rôle (`page` | `table` | `agent` | `execution`), jamais un
`kind` de plus. 404 indistinct entre inexistant et interdit.

**Un nœud `agent` porte la référence de sa procédure — depuis le 29/08 (#417).** Sur le
`RailNode` du rail comme sur la fiche : `procedure: {id, slug, scope}` — `id` est
l'identifiant STABLE que `GET /api/me/guides/{guide_id}` (et `oto_procedure`) accepte,
`slug` la référence lisible (0059-D3 : les deux, toujours), `scope` le propriétaire du nœud
(`org` | `group`), parce que le rail sert aussi les procédures d'ÉQUIPE et qu'un front sans
le scope frappe la mauvaise route de fiche. Lu dans les propriétés du nœud, jamais
reconstruit ni joint. **Absent (rail) / `null` (fiche) sur toute autre nature et sur un
agent sans référence lisible** — jamais un id deviné. Jusque-là, aucun chemin serveur ne
menait d'un nœud agent à sa fiche : son `nod_*` est dérivé (`md5('prc:' || id)`) et la fiche
de guide refuse un `nod_*` — un front devait recalculer un md5 ou apparier par titre. La
fiche de guide ne résout toujours PAS un `nod_*` (laissé de côté, cf. #417).

**`…/rows` refuse ce qu'il ne peut pas appliquer, et son `total` compte la page servie —
depuis le 01/09 (#621, relevé au passage de #418).** Trois défauts de la même famille que
`?filter=` tronqué : *un chemin qui répond juste sur ce qu'il n'a pas regardé*.

| avant | maintenant |
|---|---|
| une entrée de `filter` sans `:` était **ignorée** — la page partait non filtrée, en 200 | `400 invalid_filter`, qui nomme la forme `colonne:valeur` **et** l'entrée fautive |
| `InvalidCursor` n'était rattrapé nulle part → **500** sur un curseur tronqué ou repassé d'un régime de tri dans l'autre | `400 invalid_cursor` (« reprends la liste sans `cursor` »), le MÊME code sur les deux provenances de tableau — le natif rendait `curseur_invalide`, personne ne pouvait prévoir lequel |
| `total` était compté sur les filtres **non résolus**, la page sur les résolus | le compte passe par `store.count_rows`, qui résout comme la page |

Le troisième ne se voit que sur un schéma à **double service** (`contact1_nom` servi en
lecture pour `contacts[0].nom`, oto#22 §6) : le pied du tableau comptait un autre jeu que
celui qu'il coiffait, et rien n'échouait. Les trois refus sont **déclarés**
(`Capability.errors`), donc publiés dans `/openapi.json` : trois gestes différents derrière
un même 400, et un client ne devrait pas avoir à les distinguer en lisant une phrase.

## Renommer un chemin servi : on double, on date, on retire

Un chemin `/api/*` est un contrat avec des appelants qui vivent **hors de ce dépôt**.
Le renommer sec ne casse rien en CI — ça casse en production, chez quelqu'un d'autre,
sans trace. La forme retenue (#519, lot B) :

1. le **nouveau chemin** est la vraie route (capacité, autz, décrite dans l'OpenAPI) ;
2. l'**ancien** reste monté et répond **308** — même méthode, même corps, query string
   reportée. Ni 301 ni 302 : ils autorisent le client à retomber en `GET`, ce qui
   transforme un `POST` en no-op silencieux ;
3. il porte les en-têtes `Deprecation` / `Sunset`, est marqué `deprecated: true` dans
   `/openapi.json` avec son remplaçant et sa date, et **s'en va à une date écrite** ;
4. il est monté **en dernier** — un alias ne capture que ce que rien d'autre ne sert ;
5. le préflight `OPTIONS` n'est **jamais** redirigé, et la 308 porte les en-têtes CORS :
   un navigateur vérifie CORS sur chaque réponse d'une chaîne de redirections.

Déclaration unique : `oto_mcp/deprecations.REST` (montage `api/alias_routes.py`,
document `openapi._alias_deprecies`, garde `tests/api/test_alias_deprecies_rest.py`).
**Table des alias en cours et de leur date : `docs/alias-deprecies.md`.**

## Le contrat dit ce que le serveur rend — retours d'un front tiers (29/08/2026)

Un front tiers, consommateur pur de cette API, a dérivé son comportement du contrat
servi (`/openapi.json` + docstrings) et s'est heurté à quatre endroits où **le contrat
ne disait pas, ou disait faux**. Tout est additif ; rien n'a changé de comportement.

- **`GET /api/me/nodes/{id}` rend `doc_id` et `project_id`** (`NodeOut`, face MCP
  `oto_node` idem — même vue). Ce sont les poignées que `POST /api/me/docs`
  (`op=backlinks`, `op=update`) et `POST /api/resources` (`op=get`, `resource_type:
  "project"`, `resource_id: str(project_id)`) prennent en entrée : sans elles, un nœud
  ouvert ne s'éditait ni ne se partageait. Page = les deux ; projet = `project_id` ;
  tableau / procédure = le projet qui les range, lu sur le fil ; nœud natif = `null`.
  Conséquence : `rev` a tourné une fois pour tous les nœuds.
- **Un bloc `role: list` porte `ordered: true|false`** — la liste de CE bloc est-elle
  numérotée (décidé par son premier item). Dérivé de `md` à la lecture
  (`db/blocks.ordered_of`), jamais stocké : aucune rotation de marqueur. ⚠️ Le
  docstring de `_role_de` refusait ce champ jusque-là pour une collision de sens avec
  le front (« un pas d'une suite ») ; le front l'a demandé avec NOTRE sens, le refus
  est daté et levé.
- **Les refus se déclarent** : `Capability.errors=(DeclaredError(status, code,
  quand), …)` sort dans `/openapi.json` comme une réponse par statut, enveloppe
  `Erreur` (`{error, detail, details?}`, composant unique) et énuméré des `error`.
  DÉCRIT, ne fait rien : `tests/test_capability_declared_errors.py` exige que chaque
  code déclaré soit levé dans le module du handler, et chaque déclaration a son test
  qui rejoue le refus **sur la route servie** (`tests/api/test_rest_contract_front_tiers.py`,
  vrai PostgreSQL). Déclarés ce jour : `PATCH /api/groups/{id}` → **409 `group_exists`**
  (le docstring `GroupUpdated` disait le contraire — corrigé et daté) ; `PUT
  /api/me/guides/{scope}/{slug}` → **400 `body_too_large`**, borne **65 536 octets UTF-8**
  publiée en `maxLength` sur `body_md` (nécessaire, pas suffisant : un caractère
  accentué pèse deux octets) ;
  ⚠️ **`PUT /api/me/instructions/{slug}` porte le même code mais PAS la même borne
  depuis le 03/09/2026 : 131 072 octets**, le double. Deux bornes, deux raisons, et
  c'est la raison qui décide — le corps d'un **guide** est injecté dans CHAQUE session
  (sa borne protège un budget réel), celui d'une **procédure** est chargé à la demande
  par `oto_procedure op=get`. La justification historique « injecté à chaque session »
  ne valait donc pas sur ce second chemin, qui refuse d'ailleurs explicitement le slug
  du README. Relevée pour débloquer une procédure de mission qui était à sept octets
  de l'ancienne borne. 128 Ko de français ≈ 34 000 jetons, soit environ un sixième
  d'une fenêtre de 200 k — au plafond, une procédure reste chargeable avec de quoi
  travailler autour ; doubler plutôt que décupler garde la borne comme signal qu'il
  est temps de découper. ⚠️ **La garde est COMMUNE aux deux faces** (`.set`, `.create`
  et `.admin_set` passent tous par `_set_instruction`, que `oto_procedure` atteint via
  l'adaptateur MCP) : le relèvement vaut donc aussi côté connecteur, ce qui est un
  effet constaté avant, pas découvert après.
  ⚠️ **En revanche la borne n'est PUBLIÉE que sur la face REST** — mesuré le 03/09 sur
  le montage réel : la face MCP sert `body_md` en `{anyOf: [string, null]}`, sans
  `maxLength` ni description. La cause est écrite dans `capabilities/_types.py` :
  l'aplatissement construit un `Field` NEUF et « rien d'autre ne voyage — ni examples,
  ni json_schema_extra, ni les contraintes ». Un agent du connecteur découvre donc
  toujours la borne en s'y cognant. `tests/test_param_description_servie.py` fige ce
  manque plutôt que de le laisser croire comblé, et tombera le jour où il sera corrigé ; `DELETE /api/me/orgs/{id}/membership` → **404
  `unknown_org`, 409 `personal_org`, 404 `not_a_member`, 409 `last_org_admin`**, dans
  l'ordre des gardes. La liste d'une opération n'est pas exhaustive : les 400 de
  l'adaptateur (`invalid_input`, `unknown_fields`, `invalid_json`, `invalid_body`) valent
  partout — le préambule du document le dit.
- **`POST /api/orgs/{id}/invitations` sur une adresse déjà membre → 409
  `already_member` ; déjà invitée (invitation encore valide) → 409 `already_invited`,
  avec `details.invitation = {id, created_at, expires_at}` pour la renvoyer — jamais son
  code** (#622, tranché et livré le 29/08/2026 ; même seam pour `oto_org op=invite`).
  L'adresse est comparée normalisée (strip + minuscules) ; « déjà membre » = un compte
  membre de l'org porte cet email ; une invitation expirée, consommée ou révoquée ne
  bloque pas (nouvelle invitation, 200). Sans adresse (code à partager), rien à comparer.
  ⚠️ **Jusqu'au 29/08/2026 la même requête rendait 200 et une invitation DE PLUS, code
  neuf** — la ligne du #618 (matin du même jour) qui déclarait ce 200 tel quel est
  remplacée par celle-ci. Accepter en étant déjà membre n'abaisse toujours pas le rôle
  (#297).
- **`POST /api/me/invitations/reject` → 400 `missing_token`, 410 `invalid_or_expired`,
  403 `not_the_invitee`**, dans l'ordre des gardes (#654, 2026-09-01). `410` couvre
  d'un seul jeton l'inconnue, l'expirée, la déjà acceptée et la refusée par quelqu'un
  d'autre — comme sur l'acceptation, et pour la même raison : ne pas dire à qui sonde
  lequel des quatre. `403` couvre aussi l'invitation **anonyme** (émise sans adresse) :
  elle n'est adressée à personne, donc à personne en particulier. Refuser deux fois la
  même invitation est **idempotent** (200, même réponse).
- **`POST /api/resources` déclare sa 200 en UNION DISCRIMINÉE** (#659, 2026-09-01), plus
  onze refus. La forme dépend de `resource_type` — `row_count` pour un tableau,
  `archived_at` pour un projet, `version` pour un guide — donc le document rend un
  `oneOf` + `discriminator: resource_type` pour `op=get`, à l'intérieur d'un `anyOf` qui
  couvre les cinq verbes (six branches : `op=share` en a deux, grant vs publication).
  Modèles dans `capabilities/resources_contract.py`. ⚠️ **Une union PLATE aurait déclaré
  `row_count` sur un projet** : une carte qui ment est pire qu'une carte absente, un
  client généré s'y branche. ⚠️ **`capability_output_debt.txt` avait classé cette
  surface « indéclarable » le 11/08** sur une mesure JUSTE (l'intersection des sept
  `return` est vide) mais qui répondait à une autre question : une intersection vide
  disqualifie l'**enveloppe**, pas l'**union**. La leçon vaut pour `me.project` et
  `me.doc`, qui restent en dette pour la même raison mal lue.
- **Les deux pièges d'entrée sont corrigés sur une SURFACE DOUBLÉE, `POST
  /api/resources/v2`** (2026-09-01) — pas sur `/api/resources`, qui ne bouge pas.
  - `resource_type` y est **OBLIGATOIRE** (`Literal`, énuméré publié). Sur l'héritée il
    vaut `datastore_namespace` par défaut, relique du pilote ADR 0030 : un appelant qui
    vise un projet et omet le champ interroge silencieusement une autre famille, et sur
    `transfer`/`share` **agit sur une autre ressource**.
  - `resource_id` y porte un motif `^\d+$` **publié dans le schéma**. ⚠️ Ce défaut ne se
    corrigeait PAS dans le handler, contrairement à ce que l'issue supposait : la levée
    se produit dans la règle d'autz (`RESOURCE_GOVERN` → `ownership.can_govern` →
    `int(rid)`), qui tourne AVANT lui — mesuré, `owner_getter('abc')` lève `ValueError`
    pour un tableau et pour un projet (**500**, l'adaptateur REST ne rattrape
    qu'`AuthzDenied`) et rend `None` pour un guide (**403**, son owner_getter a son
    propre `isdigit()`).
  - Les deux surfaces ne déclarent donc pas les mêmes refus : l'héritée seule publie
    `unsupported_resource_type` (son champ est un `str` libre, la famille inconnue
    atteint le handler) ; la stricte la refuse à la validation.

  **Pourquoi doublée et pas durcie — et ce que la première version a raté.** #756 avait
  rendu le champ obligatoire **sur `/api/resources`**. Le champ étant déclaré sans défaut
  sur le modèle d'entrée, il devenait obligatoire sur **toutes** les op, pas seulement
  `op=get` : plus large que ce que le lot annonçait. Le journal des appels a montré de
  vrais appelants sur cette route, dont un `op=list` qui serait passé de « fonctionne » à
  « refusé » sans préavis. Reverté (#774) avant d'atteindre un tag — la rupture n'a donc
  jamais été servie en production. Arbitrage : **un contrat servi se double, il ne se
  durcit pas en place**, et l'héritée porte son défaut connu **dans sa description
  servie**, avec le nom de sa remplaçante.

  ⚠️ **CORRECTION DATÉE (2026-09-01).** Cette page affirmait que rendre `resource_type`
  obligatoire faisait *« sortir `scripts/contrat-front.py` en rouge »*. **C'est faux, et
  la façon dont c'est faux compte** : le front consommateur épingle 63 opérations et
  `/api/resources` n'en fait pas partie — mesuré, le contrôle rend le même verdict
  (sortie 0, un seul avertissement préexistant sur `POST /api/me/docs`) avec ou sans la
  rupture. **Un contrôle vert sur une route qu'il n'épingle pas ne dit rien de cette
  route.** Ce qui a réellement attrapé la rupture est le journal des appels, pas ce
  script ; et ce qui l'empêche de revenir est un cliquet dédié
  (`tests/resources_input_legacy.json`, fige le schéma d'entrée servi de l'héritée).

## Une adresse web ne transporte pas de liste (#367, garde posée le 28/08)

L'adaptateur verse la query string telle quelle — `dict(request.query_params)`, donc **des
chaînes**. Pydantic coerce `str`→`int`/`bool`, **jamais `str`→`list`** : un `Input` qui
déclare `list[...]` sur une capacité liée en `GET`/`DELETE` répond `400 invalid_input`, et
le refus ne nomme même pas le champ. `?include=procedures` sur `me.project_read` a vécu
ainsi quinze jours — déclaré, testé, documenté, inatteignable (livré le 13/08 par
`c46d81e`, réparé le 28/08 par `22b7dc9`) : les tests d'alors vérifiaient que le champ
était DÉCLARÉ, personne n'avait tapé l'URL.

**Le patron de la maison** : déclarer `Optional[list[str] | str]` — c'est la forme RÉELLE
de l'entrée, pas une facilité — et normaliser une fois, au bord ; ou poser un
`field_validator(mode="before")` qui découpe. **Deux formes disent une liste dans une
URL, et les deux sont servies** :

- **la virgule** dans une valeur unique — `?include=spine,procedures` — découpée par le
  champ lui-même ;
- **la clé répétée** — `?filter=statut:actif&filter=ville:Paris` — assemblée en liste par
  l'adaptateur, dans l'ordre de l'URL, **dès que le champ déclare une liste** (`list[...]`
  quelque part dans l'annotation, `Optional`/`Union` traversés). C'est la sérialisation
  par défaut d'un paramètre `array` en OpenAPI (`style: form`, `explode: true`), donc ce
  qu'un client généré depuis `/openapi.json` envoie.

⚠️ **Corrigé le 29/08 (#418)** : jusque-là, cette section disait « la forme répétée est
inutilisable » — et c'était vrai : l'adaptateur faisait `dict(request.query_params)`, qui
ne garde que la DERNIÈRE valeur. `?filter=a:1&filter=b:2` perdait `a` **sans erreur**,
quand la face MCP recevait la liste entière : deux faces, deux résultats pour la même
demande, et l'OpenAPI promettait la forme perdue. Les deux formes se combinent
(`?k=a,b&k=c` → `["a,b", "c"]`, puis le champ découpe s'il le fait).

**Une clé répétée sur un champ SCALAIRE est REFUSÉE — `400 repeated_scalar`, qui nomme
la clé** — jamais réduite à sa dernière valeur. C'est la même règle que `unknown_fields`
et que le corps illisible : refuser plutôt qu'ignorer. `filters` de
`GET /api/datastore/namespaces/{ns}/rows` est un JSON dans UNE chaîne : le répéter est une
erreur de forme, pas deux filtres. Une clé inconnue répétée reste `unknown_fields`.
Garde : `tests/test_rest_query_repeated_param.py`.

**Garde au seam** : `tests/test_rest_query_list_fields.py` balaye le registre, exige une
**valeur d'exemple valide** par champ liste (`EXEMPLES`) et exerce le vrai handler avec
cette URL. Un champ liste neuf sans exemple fait rouge — c'est cette ligne qui oblige à
taper l'URL une fois, l'étape qui a manqué à #367. Trois champs concernés au 28/08 :
`me.project_read.include`, `me.search.kinds`, `me.node.rows.filter`, tous atteignables.

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

## Version servie — `GET /api/version` (+ `X-Oto-Version`)

**Sans auth**, comme le descriptif ci-dessus : un ref git, un SHA, deux horodatages —
aucune valeur. Il rend l'étiquette de ce que **le processus exécute** (`v1.2.3+6d5bf16b`),
le tag **oto-core réellement installé**, et l'instant du déploiement comme celui du
démarrage. La même étiquette part dans `info.version` de l'OpenAPI et en **en-tête
`X-Oto-Version` de chaque réponse** — l'endpoint sert à qui pense à demander, l'en-tête
à qui relit son journal après coup.

⚠️ Elle ne désigne PAS ce que le dernier workflow a déployé, et ⚠️ **`pip show oto-core`
ment** (numéro gelé à 1.100.0). Le pourquoi de chaque refus, l'ordre de résolution
(env > `.oto-deploy.json` > `"unknown"`) et le piège des deux copies du script de
déploiement : **`docs/version-servie.md`**.

## Jetons API `oto_` — gestion et portée

- **La gestion des jetons demande une session interactive.** `GET|POST /api/me/tokens`,
  `DELETE /api/me/tokens/{id}` et leurs miroirs admin `/api/admin/users/{sub}/tokens*`
  refusent un porteur de jeton (`403 api_token_forbidden`) : seul un JWT Logto y passe.
  Sinon une fuite est **auto-entretenue** — l'attaquant s'émet un second jeton (non-expirant)
  avant qu'on révoque le premier, et peut révoquer les jetons légitimes. Émettre un jeton
  redevient un acte humain, ce qui borne la gravité réelle d'une fuite à la portée du jeton.
- **Portée opt-in** (`auth/token_scopes.py`, colonne `user_api_tokens.scopes` JSONB) : à la
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
  - La table des routes autorisées (`auth.token_scopes._ALLOWED`) est la **seule** porte : une
    route ajoutée demain est refusée sans qu'on ait à y penser.
  - ⚠️ La portée nomme le tableau par son **nom** (ce que l'URL adresse), pas par son id :
    après un renommage, ré-émettre le jeton.
- **`label` : au plus 32 caractères, REFUSÉ au-delà** (`400 label_too_long`, qui donne la
  longueur reçue **et** la borne). Jusqu'au 03/09/2026 il partait en base en `strip()[:32]`
  sans que rien ne le dise, **et la réponse rendait le brut de l'appelant** — sur un libellé
  long, l'émetteur repartait donc avec la confirmation d'une valeur qui n'existait nulle part,
  puis retrouvait 32 caractères dans `GET /api/me/tokens`, c'est-à-dire sur la liste même où
  l'on décide de révoquer. La colonne est en `TEXT` : cette borne est un choix de surface,
  donc elle est **publiée** (`maxLength` dans le schéma servi, pour qu'un front tiers en
  dérive sa garde de saisie) et un dépassement se refuse au lieu de se raboter — une coupe
  sur une ÉCRITURE ne se répare pas par un drapeau, la fin ne survit nulle part (oto#42,
  4ᵉ règle ; le raisonnement complet est dans `conventions.md`, « Projeter ≠ tronquer »).
  La réponse rend désormais **le libellé écrit**, jamais le brut. ⚠️ 32 reste court et n'a
  jamais été calibré : la coupe muette empêchait de l'apprendre (un libellé raboté ne produit
  pas de plainte, il produit des listes de jetons qu'on ne sait plus distinguer). Un refus, lui,
  se signale — si la borne gêne, on le saura, et la relever ne coûte qu'une constante.

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
> (`auth/token_scopes.py`, `user_api_tokens.scopes`) : deny-by-default borné à des tableaux
> nommés en read/write. `scopes` NULL = jeton historique, inchangé. Depuis le 03/08 la
> portée nomme aussi des **projets** (`{"projects": {"12": "read"}}`), servis par
> `GET /api/me/projects/{id}` — la forme POST porte sa cible dans le CORPS, donc aucune
> portée ne peut la borner : **ce qu'un jeton porté atteint doit se lire dans le chemin.**
> C'est la règle à garder en tête avant d'ouvrir une nouvelle surface aux intégrations.

## CORS — la liste du code est MORTE en prod comme en preprod

⚠️ **CORS : la liste du code est MORTE en prod comme en preprod.** `_allowed_origins()`
(`api/routes.py`) n'est qu'un **fallback** — les DEUX box posent `OTO_MCP_CORS_ORIGINS`
dans leur `.env`, qui **écrase** la liste. Ajouter une origine au code, la déployer et
constater que rien ne change est un piège vécu (30/07, front d'un tenant tiers) : le tag prod avait
été posé pour une raison inexacte. **Ajouter une origine = éditer l'env des deux box +
restart** (`/opt/oto-mcp/.env`, `/opt/oto-mcp-canari/.env`) ; le code ne sert qu'aux
environnements neufs. Diagnostic en 1 appel, sans lire le `.env` : `curl -X OPTIONS
https://mcp.oto.cx/api/mcp/catalog -H 'Origin: <x>'` → l'en-tête `Access-Control-Allow-Origin`
revient si l'origine passe. ⚠️ Ne pas déduire « c'est la liste du code » du seul fait qu'une
origine du défaut est acceptée : l'override en contient une copie.

⚠️ **Et le piège SYMÉTRIQUE, constaté le 03/09/2026 : conclure qu'une origine est
BLOQUÉE sans le vérifier.** Un lot a retiré `POST /api/contact` en motivant le
retrait par « la route était de toute façon morte, l'origine du site n'est pas dans
`OTO_MCP_CORS_ORIGINS`, donc le navigateur bloquait avant d'arriver ici ».
**Mesuré en production le jour même : l'origine EST autorisée**, le serveur rend son
`Access-Control-Allow-Origin`, et une origine inconnue ne l'obtient pas — le contrôle
est donc bien discriminant. La route était vivante. Ce qui rendait le retrait sûr
était autre chose, et de vérifiable : le formulaire du site avait basculé vers le
service de messagerie une minute plus tôt.

Le geste était bon, le motif était faux — et c'est le motif qui voyage. « Morte par
blocage d'origine » resservira à retirer une autre route, et sera faux la prochaine
fois aussi. Le diagnostic est le même `curl -X OPTIONS` d'un paragraphe plus haut, il
coûte une seconde, **et il répond dans les DEUX sens** : ce qui vaut pour « je crois
que ça passe » vaut pour « je crois que c'est bloqué ». Une croyance qui autorise un
retrait mérite le même contrôle qu'une croyance qui autorise un ajout.

## Le `content-type` d'une réponse JSON porte son charset (#472, 29/08)

`api.base._json` construit une `JSONResponse` : son `media_type` ne commence pas par
`text/`, donc **Starlette n'y ajoute aucun charset de lui-même**. Depuis #472, une couche
ASGI unique (`oto_mcp/response_charset.py`, posée dans `server.build_root_app`) complète
l'en-tête de toute réponse `application/json` ou `text/event-stream` en
`; charset=utf-8` — `/api/*` et `/mcp` par le même geste.

Elle **complète, elle ne réécrit pas** : un `content-type` qui porte déjà un `charset=`
(ex. `text/markdown; charset=utf-8` d'`api/public.py`) est laissé intact à l'octet près,
et les réponses binaires (`application/pdf` d'une facture, `application/zip` d'un export,
`image/svg+xml`) ne sont pas touchées. Un handler n'a donc **rien à faire** pour en
bénéficier — et rien à défaire s'il pose son propre charset. Le pourquoi (défaut
ISO-8859-1 de HTTP/1.1 pour `text/*`, constantes en dur du SDK MCP) est dans
`docs/mcp-spec-watch.md` §Relevé 4.

## Un nom est une donnée, servie telle quelle (relevé du 29/08/2026)

Un front tiers a signalé « `&` arrive `&amp;` dans `/api/me/shell` et `/api/me/nodes/{id}`
— double échappement à l'écriture côté backend ». **Faux, et prouvé sur le chemin servi**
(`tests/api/test_nom_est_une_donnee_servie_telle_quelle.py`, vrai PostgreSQL) : un groupe,
un projet et une page nommés `Finance & Administratif <R&D> "Q1"` ressortent au caractère
près sur chaque route de lecture, corps JSON brut sans entité. Le relevé en base : **0**
groupe et **0** org avec une entité HTML dans le nom, **1** projet et **5** pages — et le
journal des appels montre `&amp;` **dans les arguments reçus** (`oto_doc` / `oto_project
op=create`), le `brief_md` du même appel portant un `&` nu : c'est le client qui échappe son
champ `name` à l'écriture. Le serveur stocke ce qu'il reçoit, ce qui est son rôle ; il ne
déséchappe rien à la lecture, et **aucune réparation en base n'a été faite** — six titres
envoyés ainsi par leurs auteurs (trois d'une session de test, trois d'un agent), à corriger
par eux s'ils le veulent. L'échappement appartient au RENDU HTML (`share_ui`,
`public_doc_page`, l'email), jamais au stockage ni à une réponse JSON.
